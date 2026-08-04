"""BLE accessory devices: the Pura Air spray and the EverSweet fountains.

None of these has a network of its own (`utils/const.py::DEVICE_TYPES_BLE_ONLY`).
Each pairs over BLE to a mains-powered WiFi device — a litter box or a feeder —
which relays for it, so everything here arrives inside that parent's traffic:
K3 readings ride along in its `property/post`, and the fountains' arrive as
`ble_response/post` frames carrying a binary protocol this module decodes.

Pairing is ours to hold because the parent does not discover anything: it asks
the cloud for a list of MACs and scans for exactly those, and no firmware can
report a new one upward.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from petkit_local.devices.base import Refused
from petkit_local.devices.registry import PersistedRegistry
from petkit_local.ha.discovery import EntityDef

log = logging.getLogger(__name__)

# The `type` int the firmware expects in a `dev_ble_device` list entry. K3 is 0
# because it is never listed there at all (it travels inside the parent's
# device_info instead), so its value is only ever a placeholder.
#
# Two of these are evidence. 14 was read off a real W5 pairing; 24 came from a
# CTW3 owner who captured their own `dev_ble_device` list (issue #4).
#
# `w4` and `ctw2` are still assumptions, and the assumption is that a model
# shares its number with its own product line rather than with the other one:
# `ctw2` sits with `ctw3` (both cordless CT-series EverSweets), `w4` with `w5`.
# Nothing in the parent's firmware settles it — `pk_schmg_parse_ble_dev_list`
# (D4SH `ctrl`, `ble_relay_network.c`) reads `type` straight out of the JSON,
# logs `dev[%d],type:%d` and stores it, and the parent then scans by MAC. The
# value only picks which protocol it speaks once connected.
#
# A wrong guess fails silently at both ends, which is why `BLEDevice.scan_type`
# exists: the owner of the hardware can override it without a code change, the
# panel shows the exact entry that will be sent, and whatever value turns out to
# work can be brought back here as a real one — which is exactly how 24 arrived.
#
# NOT the same number as the `typeCode` in mr-ransel's protocol notes, which
# gives W5 1, W5C 2 and W5N 3. That one is the accessory's own idea of what it
# is, read over its BLE session; this one is what a PARENT is told to scan for.
# Two namespaces, and the resemblance is a trap: "correcting" 14 to 1 from that
# table would throw away the only value anybody has confirmed.
BLE_TYPE_MAP = {"w5": 14, "w4": 14, "ctw3": 24, "ctw2": 24, "k3": 0}

#: Which of those values is evidence and which is a working assumption.
#: Read by the panel so the guess is visible where somebody can act on it.
BLE_TYPE_CONFIRMED = frozenset({"w5", "ctw3"})

#: The accessory kinds that produce HA entities. Anything else registers fine
#: and then appears nowhere, so callers validate against this rather than
#: letting a typo create an invisible device.
BLE_TYPES = tuple(BLE_TYPE_MAP)

#: The EverSweets that speak the W5 BLE protocol.
#:
#: One pair of GATT UUIDs and one frame parser serve all of them in
#: `phldgmn/ha-petkit-ble` and in `aavdberg/ha-petkit`, both of which advertise
#: for `Petkit_W5`, `Petkit_W5C`, `Petkit_W5N`, `Petkit_W4X`, `Petkit_W4XUVC`
#: and `Petkit_CTW2` — so a frame from any of them decodes with
#: `parse_w5_ble_response` and reads through `W5_ENTITIES`. The variants are
#: not separate `ble_type`s here for that reason: there is nothing to branch
#: on, and each new one would need a `BLE_TYPE_MAP` scan number nobody has.
#:
#: CTW3 does NOT belong here — its status block is a different length with a
#: different layout, so reading it with these offsets would produce confident
#: nonsense, and `aavdberg/ha-petkit` keeps it on a separate parser for the
#: same reason. It has its own decoder further down.
W5_PROTOCOL = frozenset({"w5", "w4", "ctw2"})


def normalize_mac(mac: str) -> str:
    """A MAC in one canonical form: uppercase hex, no separators.

    A BLE MAC reaches us from two directions that do not agree on formatting —
    typed by a person when pairing, and read out of a relayed frame's
    `content.device.mac` — and the only thing that matters is that the two
    match. Comparing canonical forms means `aa:bb:cc:dd:ee:ff`,
    `AA-BB-CC-DD-EE-FF` and `aabbccddeeff` are the same accessory, which is
    what a user means and what avoids a silently dropped frame.

    Returns "" for anything that is not 12 hex digits, so a caller can reject
    it rather than store a MAC no frame will ever match.
    """
    cleaned = "".join(c for c in (mac or "") if c.isalnum()).upper()
    if len(cleaned) != 12 or any(c not in "0123456789ABCDEF" for c in cleaned):
        return ""
    return cleaned


#: `pk_schmg_parse_ble_dev_list` (D4SH `ctrl` @ 0x004598ac, T6 `ctrl_t6` @
#: 0x0046d954 — byte-identical, same `ble_relay_network.c` source, confirmed
#: by embedded debug strings) rejects the WHOLE response before any JSON
#: parsing if the raw serialized body is under 0x28 (40) bytes: logs `ble list
#: len too short`, returns, and the relay list update is silently dropped —
#: not a crash, but the same silent no-op an omitted `list` key produces, just
#: one gate further into the function. `{"list": [], "nextTick": 3600}` is 38
#: bytes under the compact separators `mqtt/bridge.py::_dumps` uses — under
#: the gate. 48 leaves margin: landing exactly on 40 means the next incidental
#: change (`nextTick` losing a digit, a shorter field) silently regresses back
#: under the gate with nothing to catch it.
MIN_BLE_REPLY_BYTES = 48


def build_ble_device_result(ble_list: list[dict[str, Any]]) -> dict[str, Any]:
    """The `result` object for `dev_ble_device`, padded past the firmware's length gate.

    HTTP (`http/handlers/ble_device.py`, aiohttp's default spaced `json.dumps`)
    and MQTT (`mqtt/bridge.py::_dumps`, compact separators) do not serialize the
    same dict to the same byte count, and only one of them clears
    `MIN_BLE_REPLY_BYTES` by accident of its separators. Both transports go
    through this one function instead of each depending on its own serializer's
    whitespace habits — the two had already drifted once (see the git history
    of this endpoint) from being written independently.

    The padding lands in an explicit `_reserved` key rather than inflating
    `nextTick` or similar: `pk_schmg_parse_ble_dev_list` only ever does
    targeted `cJSON_GetObjectItem(result, "list"/"nextTick")` lookups — the
    same pattern every other endpoint traced this session follows — so an
    unrecognized key is silently ignored, never rejected. That makes an
    explicit filler field a legitimate pad, not a hack.
    """
    result: dict[str, Any] = {"list": ble_list, "nextTick": 3600}
    while len(json.dumps({"result": result}, separators=(",", ":")).encode("utf-8")) < MIN_BLE_REPLY_BYTES:
        result["_reserved"] = result.get("_reserved", "") + "x"
    return result


@dataclass
class BLEDevice:
    """A BLE-only accessory, reachable only through the WiFi device it is paired to.

    `link_with` is the `petkit_id` of that parent, and is what every lookup here
    keys on: the accessory has no network identity of its own, so it has no
    credentials, no heartbeat and no liveness of its own either — `state` is
    only ever updated as a side effect of the parent reporting. `last_seen` is
    the one exception and is about the accessory itself: when it last said
    anything at all, through whoever relayed it.
    """

    ble_type: str
    petkit_id: int
    serial_number: str = ""
    mac: str = ""
    secret: str = ""
    link_with: int = 0
    interval: int = 240
    #: Overrides `BLE_TYPE_MAP` for this one accessory. 0 means "use the table".
    #:
    #: Two of the table's values are evidence; the rest are a working
    #: assumption (see `BLE_TYPE_MAP`). A wrong one produces a pairing
    #: that fails with no symptom at either end, and the person who can find out
    #: which value is right is the one holding the fountain — not us. So it is
    #: settable per accessory, and persisted with the rest.
    scan_type: int = 0
    #: When a frame from this accessory last decoded. Not a network liveness —
    #: it has no network — but the answer to the only question worth asking
    #: about a relayed device: has it ever spoken, and how long ago. With a
    #: `scan_type` that may be a guess, "never" is the symptom to look for.
    last_seen: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def wire_mac(self) -> str:
        """The MAC as the cloud puts it on the wire: lowercase, no separators.

        Stored uppercase because that is the canonical form for COMPARING one
        (`normalize_mac`), and a frame's MAC reaches us in whatever shape the
        firmware felt like. Outbound is the other question, and the answer is
        the real cloud's: every captured `dev_ble_device` entry and every
        `connect` carries lowercase. Nothing says the parent compares case
        sensitively — but nothing says it does not, and matching the cloud
        costs nothing.
        """
        return self.mac.lower()

    @property
    def ble_type_int(self) -> int:
        """This accessory's `type` code for the `dev_ble_device` list."""
        return self.scan_type or BLE_TYPE_MAP.get(self.ble_type, 0)

    @property
    def scan_type_is_guessed(self) -> bool:
        """Whether this accessory is being scanned for on an invented number."""
        return not self.scan_type and self.ble_type not in BLE_TYPE_CONFIRMED \
            and self.ble_type != "k3"

    def to_ble_list_entry(self) -> dict[str, Any]:
        """One entry of the `dev_ble_device` response: what the parent must scan for."""
        return {
            "id": self.petkit_id,
            "secret": self.secret,
            "type": self.ble_type_int,
            "mac": self.wire_mac,
            "interval": self.interval,
        }

    def to_dict(self) -> dict[str, Any]:
        """The persisted form. Unlike `Device`, `state` IS kept.

        An accessory only reports when its parent happens to poll it, so
        dropping the last reading would leave its HA entities unknown for
        minutes after every restart.
        """
        return {
            "ble_type": self.ble_type,
            "petkit_id": self.petkit_id,
            "serial_number": self.serial_number,
            "mac": self.mac,
            "secret": self.secret,
            "link_with": self.link_with,
            "interval": self.interval,
            "scan_type": self.scan_type,
            "last_seen": self.last_seen,
            "state": self.state,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BLEDevice:
        """Rebuild from `to_dict`, ignoring keys this version no longer has."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


K3_ENTITIES = [
    EntityDef(component="sensor", key="k3_liquid", name="K3 Liquid",
              value_path="consumables.liquid", unit="%", icon="mdi:water-percent"),
    EntityDef(component="sensor", key="k3_battery", name="K3 Battery",
              value_path="consumables.battery", unit="%", device_class="battery",
              icon="mdi:battery"),
]

#: W5 / W4 / CTW2. One protocol serves the whole family, so one list does too.
#:
#: `w5_power` is a switch and used to be a binary sensor. A switch shows the
#: state AND sets it, so keeping both would put the same fact on screen twice;
#: the cost is that an existing `binary_sensor.…_w5_power` is orphaned, which
#: is a real break for anyone already running one and is in the changelog.
#:
#: The settings half — light, brightness, do-not-disturb, child lock, both
#: smart-cycle times — is only ever populated by a cmd-211 frame, which nothing
#: here asks for. Until one arrives those entities sit at unknown and a write to
#: them is refused rather than guessed (`w5_config_payload`).
W5_ENTITIES = [
    EntityDef(component="sensor", key="w5_filter", name="Filter",
              value_path="consumables.filterPercentage", unit="%", icon="mdi:filter"),
    EntityDef(component="switch", key="w5_power", name="Power",
              value_path="states.powerStatus", icon="mdi:power"),
    # 0 is off, which is why it is not among the options: this select says which
    # mode the fountain runs in, and the power switch says whether it runs.
    EntityDef(component="select", key="w5_mode", name="Mode",
              value_path="states.mode", icon="mdi:water-pump",
              options=["normal", "smart"], option_values=[1, 2]),
    EntityDef(component="binary_sensor", key="w5_running", name="Pump Running",
              value_path="states.runningStatus", device_class="running",
              icon="mdi:pump"),
    EntityDef(component="binary_sensor", key="w5_water_missing", name="Water Low",
              value_path="states.warningWaterMissing", device_class="problem",
              icon="mdi:water-off"),
    EntityDef(component="binary_sensor", key="w5_filter_warn", name="Filter Warning",
              value_path="states.warningFilter", device_class="problem",
              icon="mdi:filter-remove"),
    EntityDef(component="binary_sensor", key="w5_breakdown", name="Breakdown",
              value_path="states.warningBreakdown", device_class="problem",
              icon="mdi:alert"),
    EntityDef(component="binary_sensor", key="w5_dnd_active", name="Quiet Now",
              value_path="states.dndState", icon="mdi:sleep"),
    EntityDef(component="switch", key="w5_light", name="Light",
              value_path="states.lampRingSwitch", icon="mdi:lightbulb"),
    # A byte, and the protocol notes say so; aavdberg/ha-petkit's slider stops
    # at 10 because that is what their fountains report. Bounding it at what the
    # field can hold means a device that reports 100 is displayed rather than
    # rejected as out of range.
    EntityDef(component="number", key="w5_brightness", name="Brightness",
              value_path="states.lampRingBrightness", icon="mdi:brightness-6",
              min_value=0, max_value=255, step=1, entity_category="config"),
    EntityDef(component="switch", key="w5_dnd", name="Do Not Disturb",
              value_path="states.noDisturbingSwitch", icon="mdi:sleep"),
    EntityDef(component="switch", key="w5_child_lock", name="Child Lock",
              value_path="states.isLock", icon="mdi:lock", entity_category="config"),
    EntityDef(component="number", key="w5_smart_work", name="Smart Mode Run Time",
              value_path="states.smartWorkingTime", unit="min", icon="mdi:timer-outline",
              min_value=1, max_value=60, step=1, entity_category="config"),
    EntityDef(component="number", key="w5_smart_sleep", name="Smart Mode Rest Time",
              value_path="states.smartSleepTime", unit="min", icon="mdi:timer-sand",
              min_value=1, max_value=60, step=1, entity_category="config"),
    EntityDef(component="button", key="w5_reset_filter", name="Reset Filter",
              icon="mdi:filter-check"),
    EntityDef(component="sensor", key="w5_pump_runtime", name="Pump Runtime",
              value_path="states.pumpRuntime", unit="s", device_class="duration",
              icon="mdi:timer-outline", entity_category="diagnostic"),
    EntityDef(component="sensor", key="w5_pump_today", name="Pump Runtime Today",
              value_path="states.todayPumpRunTime", unit="s", device_class="duration",
              icon="mdi:timer-outline", entity_category="diagnostic"),
]


#: `electricStatus` value meaning mains power rather than the battery.
CTW3_ELECTRIC_AC = 2

#: The mode values that are real. A CTW3 reports 0 in the sleep half of its
#: smart cycle (aavdberg/ha-petkit issue #57) and it does NOT mean "off": the
#: pump is idle between runs and the mode has not changed. Latching it would be
#: harmless if `mode` were only ever displayed, but `ble_command_for` rebuilds
#: cmd 220 from the last reading, so a stored 0 turns the next touch of the
#: power switch into "off, in mode nothing".
CTW3_MODES = (1, 2)


#: CTW3 (EverSweet Max Cordless). Named after the fields the decoder produces,
#: which are PetKit's own — see the block above `_decode_ctw3_status`.
#:
#: `electricStatus` is a plain sensor, not a binary one: 2 has been observed and
#: nobody knows what 3 would mean. `detectStatus` is the opposite case — the
#: value varies (0x02 seen) but the question it answers is yes/no, so it is a
#: binary sensor with a non-zero test rather than a raw byte.
CTW3_ENTITIES = [
    EntityDef(component="sensor", key="ctw3_filter", name="Filter",
              value_path="consumables.filterPercent", unit="%", icon="mdi:filter"),
    EntityDef(component="sensor", key="ctw3_battery", name="Battery",
              value_path="states.batteryPercent", unit="%", device_class="battery",
              icon="mdi:battery"),
    # Writable. A switch shows the state AND sets it, so these replace the
    # binary sensors they would otherwise duplicate; `ble_command_for` maps the
    # key to the frame.
    EntityDef(component="switch", key="ctw3_power", name="Power",
              value_path="states.powerStatus", icon="mdi:power"),
    EntityDef(component="switch", key="ctw3_working", name="Working",
              value_path="states.suspendStatus", icon="mdi:play-pause"),
    # PetKit's own two names for these, and both other implementations use
    # them. They were "continuous"/"intermittent" here, which describes what
    # the pump does but matches nothing a user can compare against.
    EntityDef(component="select", key="ctw3_mode", name="Flow Mode",
              value_path="states.mode", icon="mdi:water-pump",
              options=["normal", "smart"], option_values=[1, 2]),
    EntityDef(component="switch", key="ctw3_light", name="Light",
              value_path="states.lightSwitch", icon="mdi:lightbulb"),
    EntityDef(component="select", key="ctw3_brightness", name="Brightness",
              value_path="states.brightness", icon="mdi:brightness-6",
              options=["low", "medium", "high"], option_values=[1, 2, 3]),
    EntityDef(component="switch", key="ctw3_dnd", name="Do Not Disturb",
              value_path="states.noDisturbingSwitch", icon="mdi:sleep"),
    EntityDef(component="switch", key="ctw3_child_lock", name="Child Lock",
              value_path="states.childLock", icon="mdi:lock",
              entity_category="config"),
    EntityDef(component="number", key="ctw3_energy_interval", name="Battery Work Time",
              value_path="states.energyInterval", unit="s", icon="mdi:timer-outline",
              min_value=15, max_value=3600, step=15, entity_category="config"),
    EntityDef(component="number", key="ctw3_sleep_time", name="Battery Sleep Time",
              value_path="states.sleepTime", unit="s", icon="mdi:timer-sand",
              min_value=60, max_value=7200, step=60, entity_category="config"),
    EntityDef(component="number", key="ctw3_smart_work", name="Smart Mode Run Time",
              value_path="states.smartWorkingTime", unit="min", icon="mdi:timer-outline",
              min_value=1, max_value=60, step=1, entity_category="config"),
    EntityDef(component="number", key="ctw3_smart_sleep", name="Smart Mode Rest Time",
              value_path="states.smartSleepTime", unit="min", icon="mdi:timer-sand",
              min_value=1, max_value=60, step=1, entity_category="config"),
    EntityDef(component="button", key="ctw3_reset_filter", name="Reset Filter",
              icon="mdi:filter-check"),
    # Read-only: the pump is not something you switch, it is what the fountain
    # is doing right now.
    EntityDef(component="binary_sensor", key="ctw3_running", name="Pump Running",
              value_path="states.runStatus", device_class="running", icon="mdi:pump"),
    EntityDef(component="binary_sensor", key="ctw3_drinking", name="Drinking",
              value_path="states.detectStatus", icon="mdi:cup-water"),
    EntityDef(component="binary_sensor", key="ctw3_water_lack", name="Water Low",
              value_path="states.lackWarning", device_class="problem",
              icon="mdi:water-off"),
    EntityDef(component="binary_sensor", key="ctw3_filter_warn", name="Filter Warning",
              value_path="states.filterWarning", device_class="problem",
              icon="mdi:filter-remove"),
    EntityDef(component="binary_sensor", key="ctw3_breakdown", name="Breakdown",
              value_path="states.breakdownWarning", device_class="problem",
              icon="mdi:alert"),
    EntityDef(component="binary_sensor", key="ctw3_low_battery", name="Low Battery",
              value_path="states.lowBattery", device_class="battery",
              icon="mdi:battery-alert"),
    # 2 is mains, and that is the only value with a meaning behind it
    # (aavdberg/ha-petkit gates its power estimate on it). 0 is taken to be the
    # battery. Anything else renders as the raw number rather than as a label
    # somebody invented.
    EntityDef(component="sensor", key="ctw3_power_source", name="Power Source",
              value_path="states.electricStatus", icon="mdi:power-plug",
              options=["battery", "mains"], option_values=[0, CTW3_ELECTRIC_AC],
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="ctw3_module_status", name="Module Status",
              value_path="states.moduleStatus", icon="mdi:chip",
              entity_category="diagnostic"),
    # `duration` rather than a bare second count: 1056887 says nothing, and
    # both the panel and HA render the class as something readable.
    EntityDef(component="sensor", key="ctw3_pump_runtime", name="Pump Runtime",
              value_path="states.waterPumpRunTime", unit="s",
              device_class="duration", icon="mdi:timer-outline",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="ctw3_pump_today", name="Pump Runtime Today",
              value_path="states.todayPumpRunTime", unit="s",
              device_class="duration", icon="mdi:timer-outline",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="ctw3_battery_voltage", name="Battery Voltage",
              value_path="states.batteryVoltage", unit="mV",
              device_class="voltage", entity_category="diagnostic"),
    EntityDef(component="sensor", key="ctw3_supply_voltage", name="Supply Voltage",
              value_path="states.supplyVoltage", unit="mV",
              device_class="voltage", entity_category="diagnostic"),
]


def get_ble_entities(ble_type: str) -> list[EntityDef]:
    """HA entity definitions for a BLE accessory type; empty for an unknown one."""
    if ble_type == "k3":
        return list(K3_ENTITIES)
    if ble_type == "ctw3":
        return list(CTW3_ENTITIES)
    if ble_type in W5_PROTOCOL:
        return list(W5_ENTITIES)
    return []


# The framing both fountain families share, carried as urlencode(base64(...))
# inside a relayed `ble_response`:
#
#     FA FC FD | cmd | type | seq | len_lo | len_hi | payload | FB
#
# Only the DATA layout inside it differs per model, so the decode/unframe
# helpers below are shared and the offset tables are not.
#
# The length is a 16-bit LITTLE-endian field, and getting that wrong is not a
# cosmetic detail: `build_ble_frame` used to emit a single length byte, so an
# accessory read the first byte of our PAYLOAD as the high half of the length.
# A 4-byte mode write announced 4 bytes and delivered 3 plus the trailer; a
# 12-byte config write announced 0x030C = 780 and was still being waited for
# when the session closed. Both fail in silence, which is why no settings write
# to a CTW3 has ever been seen to take effect.
#
# Three sources agree on the 8-byte header: mr-ransel's W5 protocol notes,
# aavdberg/ha-petkit's builder and parser, and `_ble_unframe` immediately
# below — which has always read the payload from offset 8, so this module was
# encoding and decoding to two different specifications.
BLE_FRAME_HEADER = bytes([0xFA, 0xFC, 0xFD])
BLE_FRAME_TRAILER = 0xFB
BLE_FRAME_HEADER_LEN = 8

#: The `type` byte: 1 request, 2 response, 3 request wanting no answer. We only
#: ever send the first.
BLE_TYPE_REQUEST = 0x01

# The commands, in the one numbering that runs in both directions. `cmd` and
# the opcode inside the frame are THE SAME NUMBER — 220 is 0xDC — which is
# worth stating because a table here once mapped one to the other as though
# they were different, and reading it that way makes 222 look like a value
# nobody has.
CMD_GET_STATE = 210         # short status block, on request
CMD_GET_CONFIG = 211        # settings block. A CTW3 never answers it.
CMD_SET_MODE = 220          # power / mode. CTW3 adds a suspend byte.
CMD_SET_CONFIG = 221        # the settings block, written whole
CMD_RESET_FILTER = 222      # filter life back to 100%
CMD_DEVICE_STATUS = 230     # status the accessory pushes unasked

#: The commands we know how to build a frame for. Anything else is refused
#: rather than sent as a frame whose length or payload we would be inventing.
BLE_SENDABLE = frozenset({CMD_SET_MODE, CMD_SET_CONFIG, CMD_RESET_FILTER})

# --- W5 / W4 / CTW2 ---------------------------------------------------------
#
# Offsets from mr-ransel's protocol notes, matching aavdberg/ha-petkit's
# parser field for field. The block is 12 bytes on every firmware, 16 where
# `todayPumpRunTime` is supported (typeCode 2 from firmware 24, the rest from
# 35) and 18 where the smart-cycle times are appended.
_W5_STATUS_STATE_OFFSETS = {
    "powerStatus": 0,
    "mode": 1,
    "dndState": 2,
    "warningBreakdown": 3,
    "warningWaterMissing": 4,
    "warningFilter": 5,
    "runningStatus": 11,
}
_W5_STATUS_FILTER_OFFSET = 10  # filterPercentage, raw byte 0-100 for cmd 230

#: The shortest block that is a status rather than a fragment. Reading fewer
#: bytes than this used to turn a one-byte ACK into a confident `powerStatus`,
#: because every field was emitted whenever its offset happened to be in range.
_W5_MIN_STATUS_LEN = 12


def _ble_decode_data(blob: Any) -> bytes | None:
    """Decode one `ble_response` frame's `data` field into raw bytes.

    The field is urlencode(base64(bytes)) — localkit's W5/Device.php reads it as
    `base64_decode(urldecode(payload))`. Returns None for anything that is
    neither that nor the observed hex fallback, so a garbled frame is skipped
    rather than decoded into nonsense.
    """
    if isinstance(blob, (bytes, bytearray)):
        return bytes(blob)
    if not isinstance(blob, str):
        return None
    s = urllib.parse.unquote(blob).strip()
    try:
        return base64.b64decode(s)
    except Exception:
        # Not every payload is base64; the hex form is the observed fallback.
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None


def _ble_unframe(raw: bytes) -> tuple[int | None, bytes]:
    """Split a W5 BLE frame into `(cmd, data_bytes)`.

    Handles both a full `FA FC FD` frame and the pre-split case where `data`
    already IS the DATA payload; in the latter case the command is None and the
    caller falls back to the `cmd` the JSON carried alongside it.
    """
    if len(raw) >= 9 and raw[:3] == BLE_FRAME_HEADER:
        return raw[3], raw[8:-1]
    return None, raw


def _iter_ble_frames(content: Any) -> Iterator[tuple[Any, bytes]]:
    """Yield `(cmd, data_bytes)` for every frame in a `ble_response` content.

    The content shape is `{device: {mac}, payload: [{cmd, data}, ...]}`; a
    single loose blob under `data`/`value`/`frame` is accepted too, because the
    proxying firmware does not always wrap one frame in a list. Undecodable
    entries are skipped silently — a BLE accessory is a best-effort extra, and
    one bad frame must not cost the parent's whole report.
    """
    if not isinstance(content, dict):
        return
    payload = content.get("payload")
    items = payload if isinstance(payload, list) else []
    # tolerate a single loose blob too
    for alt in ("data", "value", "frame"):
        if alt in content and content[alt] is not None:
            items = items + [{"cmd": None, "data": content[alt]}]
    for item in items:
        if not isinstance(item, dict) or "data" not in item:
            continue
        raw = _ble_decode_data(item.get("data"))
        if raw is None:
            continue
        framed_cmd, data = _ble_unframe(raw)
        yield (framed_cmd if framed_cmd is not None else item.get("cmd"), data)


def _decode_w5_status(data: bytes) -> dict[str, dict[str, int]]:
    """Decode a cmd-210/230 DATA block into `{"states": {...}, "consumables": {...}}`.

    The first 12 bytes are the block every firmware sends and are read or the
    frame is dropped. What follows is genuinely optional — older builds stop at
    12 — so those fields are read only when they are there, and their absence
    leaves the previous reading alone rather than publishing a zero.
    """
    if len(data) < _W5_MIN_STATUS_LEN:
        log.warning("W5 status frame is %d bytes, expected at least %d - dropped",
                    len(data), _W5_MIN_STATUS_LEN)
        return {"states": {}, "consumables": {}}

    states = {name: data[off] for name, off in _W5_STATUS_STATE_OFFSETS.items()}
    states["pumpRuntime"] = int.from_bytes(data[6:10], "big")
    consumables = {"filterPercentage": data[_W5_STATUS_FILTER_OFFSET]}

    if len(data) >= 16:
        states["todayPumpRunTime"] = int.from_bytes(data[12:16], "big")
    if len(data) >= 17:
        states["smartWorkingTime"] = data[16]
    if len(data) >= 18:
        states["smartSleepTime"] = data[17]
    return {"states": states, "consumables": consumables}


#: The cmd-211 settings block, `(offset, width)` big-endian. 13 bytes on every
#: firmware that answers at all, 14 where the child lock exists.
#:
#: Nothing here asks for this frame — the parent decides what it polls — so the
#: decoder sits idle until one arrives. It costs nothing to have, and without
#: it a settings write can never be anything but a guess: the block is written
#: whole, so the fields nobody is changing have to come from somewhere.
_W5_CONFIG_BYTE_FIELDS = {
    "smartWorkingTime": 0,       # minutes the pump runs in smart mode
    "smartSleepTime": 1,         # minutes it then rests
    "lampRingSwitch": 2,
    "lampRingBrightness": 3,
    "noDisturbingSwitch": 8,
}
_W5_CONFIG_INT_FIELDS = {
    "lampRingLightUpTime": (4, 2),    # minutes from midnight
    "lampRingGoOutTime": (6, 2),
    "noDisturbingStartTime": (9, 2),
    "noDisturbingEndTime": (11, 2),
}
W5_CONFIG_LEN = 13
W5_CONFIG_LOCK_OFFSET = 13


def _decode_w5_config(data: bytes) -> dict[str, int]:
    """Decode a cmd-211 settings block. Empty for anything shorter than 13 bytes."""
    if len(data) < W5_CONFIG_LEN:
        return {}
    out = {name: data[off] for name, off in _W5_CONFIG_BYTE_FIELDS.items()}
    for name, (off, width) in _W5_CONFIG_INT_FIELDS.items():
        out[name] = int.from_bytes(data[off:off + width], "big")
    if len(data) > W5_CONFIG_LOCK_OFFSET:
        out["isLock"] = data[W5_CONFIG_LOCK_OFFSET]
    return out


def parse_w5_ble_response(content: Any) -> dict[str, dict[str, Any]]:
    """Decode a W5 `ble_response` into the state a `W5_ENTITIES` value_path reads.

    Accepts a structured firmware payload (fields already named) OR the real
    binary frame(s), and merges both. Decoded frames are applied last and
    therefore win on any field both forms carry.

    Returns:
        `{"states": {...}, "consumables": {...}}`, matching the `value_path`
        prefixes in `W5_ENTITIES`. A section with nothing in it is DROPPED, so
        an empty dict means nothing was decodable and the caller should leave
        the previous state alone instead of publishing blanks.
    """
    result: dict[str, dict[str, Any]] = {"states": {}, "consumables": {}}

    if isinstance(content, dict):
        for section in ("states", "consumables"):
            if isinstance(content.get(section), dict):
                result[section].update(content[section])
        for key in ("powerStatus", "runningStatus", "warningWaterMissing", "warningFilter", "mode"):
            if key in content:
                result["states"][key] = content[key]
        if "filterPercentage" in content:
            result["consumables"]["filterPercentage"] = content["filterPercentage"]

    for cmd, data in _iter_ble_frames(content):
        if not data:
            continue
        if cmd in (CMD_DEVICE_STATUS, CMD_GET_STATE):
            dec = _decode_w5_status(data)
            result["states"].update(dec["states"])
            result["consumables"].update(dec["consumables"])
        elif cmd == CMD_GET_CONFIG:
            result["states"].update(_decode_w5_config(data))

    return {k: v for k, v in result.items() if v}


# --- CTW3 (EverSweet Max Cordless) ------------------------------------------
#
# A different DATA layout in the same framing, contributed with the hardware in
# hand (issue #4, firmware 111). Do NOT read it with the W5 offsets: the blocks
# are 30 bytes (cmd 210) or 42 (cmd 230, with a 12-byte config tail), against
# the W5's much shorter one, and the multi-byte values here are big-endian.
#
# The names are PetKit's own, taken from the account API's `kv` for this model
# rather than translated into the W5's vocabulary — `runStatus` not
# `runningStatus`, `lackWarning` not `warningWaterMissing`. Where the two
# families disagree, matching the cloud is what lets a report be compared with
# what the PetKit app shows.

#: Everything the decoder names fits in 26 bytes, and 26 is what an older
#: firmware sends — aavdberg/ha-petkit accepts that length from real hardware.
#: This used to demand 30 and drop the shorter block whole.
_CTW3_MIN_STATUS_LEN = 26

#: Single-byte fields, offset into the DATA block.
_CTW3_BYTE_FIELDS = {
    "powerStatus": 0,
    # Inverted against the English: 0 is paused/sleeping, 1 is working.
    "suspendStatus": 1,
    "mode": 2,                  # 1 normal (continuous), 2 smart (cycling)
    # NOT a boolean. 2 is the AC path — the fountain is on mains.
    "electricStatus": 3,
    "noDisturbingSwitch": 4,
    "breakdownWarning": 5,
    "lackWarning": 6,
    "lowBattery": 7,
    "filterWarning": 8,
    "filterPercent": 13,
    "runStatus": 14,
    # 0 is nobody; anything else (0x02 observed) is a pet at the bowl.
    "detectStatus": 19,
    "batteryPercent": 24,
    # Undecoded. Kept because it is the last byte of the short block and a
    # value nobody can see is a value nobody can explain.
    "moduleStatus": 25,
}

#: Multi-byte fields as `(offset, width)`, big-endian.
_CTW3_INT_FIELDS = {
    "waterPumpRunTime": (9, 4),
    "todayPumpRunTime": (15, 4),
    "supplyVoltage": (20, 2),
    "batteryVoltage": (22, 2),
}

#: Where the 12-byte config tail starts in a cmd-230 block.
CTW3_CONFIG_OFFSET = 30
CTW3_CONFIG_LEN = 12


def decode_ctw3_config(tail: bytes) -> dict[str, int]:
    """The 12-byte config tail of a cmd-230 status, as named fields.

    Everything a cmd-221 write has to restate comes from here, which is what
    makes changing one setting possible without inventing the rest.

    SETTLED, and it was briefly unsettled for no good reason.

    PetKit's own app builds this exact block for a cmd-221 write, field by
    field (`CTW3DataConvertor.changeSmartMode` / `changeBatteryMode`, app
    13.8.1): smart working time, smart sleep time, battery working time and
    battery sleep time as big-endian shorts, then lamp ring switch, lamp ring
    brightness, do-not-disturb, child lock, and two inductive switches. Twelve
    bytes, in the order below.

    So the status tail and the write payload ARE one layout, which is what the
    real cmd-230 frame from issue #4 already said — 1, 3, 0 at bytes 6, 7, 8 is
    a fountain with the ring on at high and quiet hours off.
    aavdberg/ha-petkit reads those three as do-not-disturb / light / brightness
    and writes ten bytes rather than twelve; 1.6.0 followed them for the write
    and was wrong to. The app is the other end of that conversation and does
    not need interpreting.
    """
    if len(tail) < CTW3_CONFIG_LEN:
        return {}
    return {
        "smartWorkingTime": tail[0],
        "smartSleepTime": tail[1],
        "energyInterval": int.from_bytes(tail[2:4], "big"),
        "sleepTime": int.from_bytes(tail[4:6], "big"),
        "lightSwitch": tail[6],
        "brightness": tail[7],          # 1 low, 2 medium, 3 high
        "noDisturbingSwitch": tail[8],
        # Written by the app only on hardware+firmware/100 >= 1.35
        # (`CTW3Utils.isSupportLockVersion`), and left at 0 below it.
        "childLock": tail[9],
        "smartInductiveSwitch": tail[10],
        "batteryInductiveSwitch": tail[11],
    }


def _decode_ctw3_status(data: bytes) -> dict[str, dict[str, int]]:
    """Decode a CTW3 cmd-210/230 DATA block.

    Refuses anything shorter than the 26 bytes the short form is defined to be,
    rather than emitting the handful of fields that happen to fit — a one-byte
    ACK read permissively becomes a confident `powerStatus`. The block length is
    known, so a short one is a broken frame and is dropped.
    """
    if len(data) < _CTW3_MIN_STATUS_LEN:
        log.warning("CTW3 status frame is %d bytes, expected at least %d - dropped",
                    len(data), _CTW3_MIN_STATUS_LEN)
        return {"states": {}, "consumables": {}}

    states: dict[str, int] = {name: data[off] for name, off in _CTW3_BYTE_FIELDS.items()}
    for name, (off, width) in _CTW3_INT_FIELDS.items():
        states[name] = int.from_bytes(data[off:off + width], "big")

    # A mode outside 1/2 is the smart cycle's sleep half, not a new mode. Drop
    # the key rather than store it: the caller merges, so absence keeps what was
    # there, and what was there is the last mode the fountain really had.
    if states.get("mode") not in CTW3_MODES:
        log.debug("CTW3 reported mode=%s - keeping the last real one",
                  states.get("mode"))
        states.pop("mode", None)

    # `filterPercent` is the one field a human treats as a consumable rather
    # than a state, and the entity reads it from there.
    consumables = {"filterPercent": states.pop("filterPercent")}

    if len(data) >= CTW3_CONFIG_OFFSET + CTW3_CONFIG_LEN:
        tail = data[CTW3_CONFIG_OFFSET:CTW3_CONFIG_OFFSET + CTW3_CONFIG_LEN]
        # `noDisturbingSwitch` appears in both halves; the tail is the one a
        # config write round-trips, so let it win.
        states.update(decode_ctw3_config(tail))

    return {"states": states, "consumables": consumables}


def parse_ctw3_ble_response(content: Any) -> dict[str, dict[str, Any]]:
    """Decode a CTW3 `ble_response` into the state its entities read.

    Same contract as `parse_w5_ble_response`: an empty dict means nothing was
    decodable, and the caller leaves the previous state alone.

    cmd 211 is deliberately not handled. On the W5 family it is the settings
    block; a CTW3 does not answer it at all, and its settings arrive as the tail
    of a long cmd-230 instead.
    """
    result: dict[str, dict[str, Any]] = {"states": {}, "consumables": {}}
    for cmd, data in _iter_ble_frames(content):
        if cmd in (CMD_DEVICE_STATUS, CMD_GET_STATE) and data:
            dec = _decode_ctw3_status(data)
            result["states"].update(dec["states"])
            result["consumables"].update(dec["consumables"])
    return {k: v for k, v in result.items() if v}


# --- writing to an accessory -------------------------------------------------
#
# The other direction of the same relay: we publish `thing/service/ble` to the
# PARENT, which forwards the bytes over its open BLE session and does not
# interpret them.
#
# Every write here restates a whole block. The device has no "set one field"
# frame, so each builder starts from the accessory's last decoded status and
# replaces one value — and refuses outright when that status has never
# arrived. Filling the unknown half with zeros would turn the light off and
# reset both intervals as a side effect of changing the brightness.


def build_ble_frame(cmd: int, seq: int, payload: bytes) -> str:
    """One outbound BLE frame, encoded the way `thing/service/ble` carries it.

    `FA FC FD | cmd | type | seq | len_lo | len_hi | payload | FB`, then base64,
    then urlencode — the exact inverse of what `_ble_decode_data` and
    `_ble_unframe` undo on the way in.

    `seq` wraps at a byte. Nothing has been observed rejecting a repeat, but it
    is a sequence number and sending a constant would be the kind of detail
    that works until it does not.
    """
    body = bytes([*BLE_FRAME_HEADER, cmd & 0xFF, BLE_TYPE_REQUEST, seq & 0xFF,
                  len(payload) & 0xFF, (len(payload) >> 8) & 0xFF,
                  *payload, BLE_FRAME_TRAILER])
    return urllib.parse.quote(base64.b64encode(body).decode())


def ble_command_frame(cmd: int, seq: int, payload: bytes) -> str | None:
    """The encoded frame for one command, or None if it is not one we send."""
    if cmd not in BLE_SENDABLE:
        return None
    return build_ble_frame(cmd, seq, payload)


def ctw3_mode_payload(power: int, suspend: int, mode: int) -> bytes:
    """cmd 220 on a CTW3 — the three settings that share one frame.

    All three travel together, so changing one means restating the other two.
    `suspend` is 1 for WORKING and 0 for paused, inverted against its name; a
    fountain being switched off has nothing to suspend, so 0 is forced there
    rather than left at whatever the last reading held.

    Three bytes, matching aavdberg/ha-petkit's `build_ctw3_mode_payload`. This
    used to emit four, with a leading zero — which was the frame's missing
    `len_hi` byte, compensated for in the wrong place.
    """
    if not power:
        suspend = 0
    return bytes([power & 0xFF, suspend & 0xFF, mode & 0xFF])


def ctw3_config_payload(state: dict[str, Any]) -> bytes | None:
    """cmd 221 on a CTW3 — the settings block, rebuilt from the last status.

    Twelve bytes, the same layout `decode_ctw3_config` reads and the same one
    PetKit's app writes. 1.6.0 sent ten in a different order, on the strength of
    a third-party capture; the app settles it.

    Returns None when the accessory has never reported a long status.
    """
    needed = ("smartWorkingTime", "smartSleepTime", "energyInterval", "sleepTime",
              "lightSwitch", "brightness", "noDisturbingSwitch")
    if any(k not in state for k in needed):
        return None
    return bytes([
        int(state["smartWorkingTime"]) & 0xFF,
        int(state["smartSleepTime"]) & 0xFF,
        *int(state["energyInterval"]).to_bytes(2, "big"),
        *int(state["sleepTime"]).to_bytes(2, "big"),
        int(state["lightSwitch"]) & 0xFF,
        int(state["brightness"]) & 0xFF,
        int(state["noDisturbingSwitch"]) & 0xFF,
        int(state.get("childLock", 0)) & 0xFF,
        int(state.get("smartInductiveSwitch", 0)) & 0xFF,
        int(state.get("batteryInductiveSwitch", 0)) & 0xFF,
    ])


def w5_mode_payload(mode: int) -> bytes:
    """cmd 220 on a W5/W4/CTW2 — `[mode, submode]`.

    One byte carries power and mode together: 0 off, 1 normal, 2 smart. There
    is no separate power field, so switching the fountain off is mode 0 and
    switching it on is whichever mode it was last in.
    """
    return bytes([mode & 0xFF, 0x00])


def w5_config_payload(state: dict[str, Any]) -> bytes | None:
    """cmd 221 on a W5/W4/CTW2 — the 14-byte settings block.

    Returns None until a cmd-211 has been decoded for this accessory. Both
    schedules live in this block as minutes from midnight, and writing it from
    defaults would erase them; nothing else in the relayed traffic carries
    them, so there is nothing to reconstruct them from.
    """
    if any(k not in state for k in _W5_CONFIG_BYTE_FIELDS):
        return None
    if any(k not in state for k in _W5_CONFIG_INT_FIELDS):
        return None
    return bytes([
        int(state["smartWorkingTime"]) & 0xFF,
        int(state["smartSleepTime"]) & 0xFF,
        int(state["lampRingSwitch"]) & 0xFF,
        int(state["lampRingBrightness"]) & 0xFF,
        *int(state["lampRingLightUpTime"]).to_bytes(2, "big"),
        *int(state["lampRingGoOutTime"]).to_bytes(2, "big"),
        int(state["noDisturbingSwitch"]) & 0xFF,
        *int(state["noDisturbingStartTime"]).to_bytes(2, "big"),
        *int(state["noDisturbingEndTime"]).to_bytes(2, "big"),
        int(state.get("isLock", 0)) & 0xFF,
    ])


#: Entity key -> the field of `states` it sets, per frame. A block restates
#: every field it carries, so which frame a key belongs to decides what else
#: has to be read back out of the last status.
_CTW3_MODE_FIELDS = {
    "ctw3_power": "powerStatus",
    "ctw3_working": "suspendStatus",
    "ctw3_mode": "mode",
}
_CTW3_CONFIG_FIELDS = {
    "ctw3_light": "lightSwitch",
    "ctw3_brightness": "brightness",
    "ctw3_dnd": "noDisturbingSwitch",
    "ctw3_child_lock": "childLock",
    "ctw3_energy_interval": "energyInterval",
    "ctw3_sleep_time": "sleepTime",
    "ctw3_smart_work": "smartWorkingTime",
    "ctw3_smart_sleep": "smartSleepTime",
}
_W5_CONFIG_ENTITY_FIELDS = {
    "w5_light": "lampRingSwitch",
    "w5_brightness": "lampRingBrightness",
    "w5_dnd": "noDisturbingSwitch",
    "w5_child_lock": "isLock",
    "w5_smart_work": "smartWorkingTime",
    "w5_smart_sleep": "smartSleepTime",
}

#: The buttons, and the command each one is. No payload is built from state —
#: they carry none.
_RESET_FILTER_KEYS = {"ctw3_reset_filter", "w5_reset_filter"}

#: Empty. mr-ransel's notes say so and PetKit's app agrees — its
#: `getCTW3ResetFilterElement` is `PetkitBleMsg(222, new byte[0])`.
#: aavdberg/ha-petkit sends a single zero; 1.6.1 copied that.
_RESET_FILTER_PAYLOAD = b""

CTW3_WRITABLE = (frozenset(_CTW3_MODE_FIELDS) | frozenset(_CTW3_CONFIG_FIELDS)
                 | {"ctw3_reset_filter"})
W5_WRITABLE = (frozenset(_W5_CONFIG_ENTITY_FIELDS)
               | {"w5_power", "w5_mode", "w5_reset_filter"})


def _ctw3_command_for(ble_dev: BLEDevice, key: str, value: int) -> tuple[int, bytes]:
    """`(cmd, payload)` for one CTW3 entity. See `ble_command_for`."""
    states = dict(ble_dev.state.get("states") or {})

    if key in _CTW3_MODE_FIELDS:
        states[_CTW3_MODE_FIELDS[key]] = value
        if key == "ctw3_mode":
            # Picking a mode means "run in it": PetKit's app sends power 1 and
            # pause 1 with the chosen mode and reads nothing back
            # (`CTW3HomePresenter.changeDeviceMode`, app 13.8.1). Taking power
            # from the last status instead sends 0 whenever the fountain was
            # caught in the sleep half of its smart cycle, leaving the pump off
            # and the select looking like it did nothing (aavdberg/ha-petkit
            # issue #54) — they fixed the power half and derived pause from the
            # mode, which the app does not do.
            states["powerStatus"] = 1
            states["suspendStatus"] = 1
        missing = [f for f in ("powerStatus", "suspendStatus", "mode") if f not in states]
        if missing:
            raise Refused(f"no reading yet for {', '.join(missing)}")
        return CMD_SET_MODE, ctw3_mode_payload(
            states["powerStatus"], states["suspendStatus"], states["mode"])

    states[_CTW3_CONFIG_FIELDS[key]] = value
    payload = ctw3_config_payload(states)
    if payload is None:
        raise Refused("no full status reported yet - the settings block is "
                      "written whole, and the rest of it is not known")
    return CMD_SET_CONFIG, payload


def _w5_command_for(ble_dev: BLEDevice, key: str, value: int) -> tuple[int, bytes]:
    """`(cmd, payload)` for one W5/W4/CTW2 entity. See `ble_command_for`."""
    states = dict(ble_dev.state.get("states") or {})

    if key in ("w5_power", "w5_mode"):
        if key == "w5_mode":
            mode = value
        elif value:
            # One byte is both power and mode, so switching on means naming a
            # mode. Normal is the fallback when none has been reported.
            mode = states.get("mode") or 1
        else:
            mode = 0
        return CMD_SET_MODE, w5_mode_payload(mode)

    states[_W5_CONFIG_ENTITY_FIELDS[key]] = value
    payload = w5_config_payload(states)
    if payload is None:
        raise Refused("no settings block reported yet - it is written whole, "
                      "and it carries both schedules")
    return CMD_SET_CONFIG, payload


def ble_command_for(ble_dev: BLEDevice, key: str, value: int) -> tuple[int, bytes]:
    """The `(cmd, payload)` that sets one accessory entity to `value`.

    No frame carries a single field, so each is built from the accessory's last
    decoded status with one value replaced.

    Raises:
        Refused: when the key is not writable on this accessory, or when the
            block it belongs to has never been reported in full. Both mean the
            same thing from Home Assistant — the control snaps back — so both
            say which it was.
    """
    if key in _RESET_FILTER_KEYS:
        return CMD_RESET_FILTER, _RESET_FILTER_PAYLOAD
    if ble_dev.ble_type == "ctw3" and key in CTW3_WRITABLE:
        return _ctw3_command_for(ble_dev, key, value)
    if ble_dev.ble_type in W5_PROTOCOL and key in W5_WRITABLE:
        return _w5_command_for(ble_dev, key, value)
    raise Refused(f"{key} is not a writable {ble_dev.ble_type.upper()} field")


#: Which decoder an accessory's frames go through.
BLE_PARSERS = {
    "ctw3": parse_ctw3_ble_response,
}


def parser_for(ble_type: str):
    """The frame decoder for an accessory kind, or None if it has none."""
    if ble_type in W5_PROTOCOL:
        return parse_w5_ble_response
    return BLE_PARSERS.get(ble_type)


class BLERegistry(PersistedRegistry):
    """Every BLE accessory, keyed by its PetKit id, persisted to ble_devices.json.

    Separate from `DeviceRegistry` because the two have different lifecycles: an
    accessory is created from whatever its parent reports, has no credentials of
    its own, and is looked up by parent (`link_with`) far more often than by id.
    """

    _label = "BLE registry"

    def __init__(self, persist_path: str | Path | None = None, *,
                 flush_interval: float | None = None) -> None:
        """See `PersistedRegistry.__init__` for the arguments."""
        self._devices: dict[int, BLEDevice] = {}
        super().__init__(persist_path, flush_interval=flush_interval)

    def get(self, petkit_id: int) -> BLEDevice | None:
        """The accessory with this id, or None."""
        return self._devices.get(petkit_id)

    def get_by_mac(self, mac: str) -> BLEDevice | None:
        """The accessory with this BLE MAC, or None.

        Compared in canonical form (see `normalize_mac`) rather than verbatim.
        This used to be an exact string match, which made the single most
        likely pairing mistake invisible: a MAC typed as `aa:bb:...` never
        matches a frame carrying `AA-BB-...`, and the frame is then dropped by
        a `log.debug` in `mqtt/bridge.py` with nothing to show for it.
        """
        wanted = normalize_mac(mac)
        if not wanted:
            return None
        for d in self._devices.values():
            if normalize_mac(d.mac) == wanted:
                return d
        return None

    def remove(self, petkit_id: int) -> bool:
        """Unpair an accessory. Returns whether it existed.

        The parent stops being told to scan for it on its next
        `dev_ble_device`, which is the whole of "unpairing" from the device's
        point of view — there is no command that revokes one.
        """
        if petkit_id not in self._devices:
            return False
        dev = self._devices.pop(petkit_id)
        log.info("BLE device removed: %s id=%d mac=%s", dev.ble_type, petkit_id, dev.mac)
        self.save()
        return True

    def get_linked(self, parent_id: int) -> list[BLEDevice]:
        """Every accessory paired to this WiFi device."""
        return [d for d in self._devices.values() if d.link_with == parent_id]

    def get_linked_k3(self, parent_id: int) -> BLEDevice | None:
        """This device's K3 purifier, if it has one.

        Its own lookup because K3 is the one accessory that is NOT served
        through `dev_ble_device`: it is embedded in the parent's device_info
        (`withK3`/`k3Device`) instead. Only the first is returned — the firmware
        has one K3 slot.
        """
        for d in self._devices.values():
            if d.link_with == parent_id and d.ble_type == "k3":
                return d
        return None

    def register(self, **kwargs: Any) -> BLEDevice:
        """Create the accessory, or update the one already stored under this id.

        On update, only TRUTHY values overwrite: the parent re-reports the whole
        accessory on every poll and pads fields it did not read this time with
        empty values, which must not erase what we already know.

        Args:
            kwargs: `BLEDevice` field values; `petkit_id` is the key and
                defaults to 0 if absent.
        """
        pid = kwargs.get("petkit_id", 0)
        if pid in self._devices:
            dev = self._devices[pid]
            changed = False
            for k, v in kwargs.items():
                if v and hasattr(dev, k) and getattr(dev, k) != v:
                    setattr(dev, k, v)
                    changed = True
            if changed:
                # Same bug as DeviceRegistry.get_or_create had: the update was
                # only ever written by some later, unrelated save().
                self.mark_dirty()
            return dev
        dev = BLEDevice(**kwargs)
        self._devices[pid] = dev
        log.info("BLE device registered: %s id=%d mac=%s linked=%d",
                 dev.ble_type, pid, dev.mac, dev.link_with)
        self.save()
        return dev

    def all(self) -> list[BLEDevice]:
        """A snapshot list of every accessory, safe to iterate while mutating."""
        return list(self._devices.values())

    def non_k3_for_parent(self, parent_id: int) -> list[BLEDevice]:
        """The accessories that belong in this device's `dev_ble_device` list.

        K3 is excluded on purpose: listing it there as well as in the parent's
        device_info makes the firmware treat it as a second, unpaired device.
        """
        return [d for d in self._devices.values()
                if d.link_with == parent_id and d.ble_type != "k3"]

    def _serialize(self) -> dict[str, Any]:
        """`{"<petkit_id>": BLEDevice.to_dict()}` — the shape of ble_devices.json."""
        return {str(pid): d.to_dict() for pid, d in self._devices.items()}

    def _restore(self, data: Any) -> None:
        """Load `_serialize`'s shape, skipping entries that cannot be read."""
        if not isinstance(data, dict):
            log.error("BLE registry at %s is not a JSON object - starting empty",
                      self._persist_path)
            return
        for key, d_data in data.items():
            try:
                dev = BLEDevice.from_dict(d_data)
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                log.warning("Skipping unreadable BLE device entry %r: %s", key, e)
                continue
            self._devices[dev.petkit_id] = dev
        log.info("BLE registry loaded: %d devices", len(self._devices))
