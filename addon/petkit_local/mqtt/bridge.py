"""MQTT bridge: subscribes to device topics on the embedded broker,
parses Aliyun IoT events, and publishes state to HA MQTT.

This is the real-time half of the device conversation. HTTP (`http/handlers/`)
carries registration, polling and file uploads; MQTT carries everything that
must not wait for the next ~15s heartbeat — property posts, visit and cleaning
events, BLE relay frames — plus the only path for pushing a command to a device
the moment a user flips a control in HA (`publish_to_device`).

The bridge is a client of the EMBEDDED broker (`mqtt/broker.py`), not of Home
Assistant's; it hands what it parses to `ha/publisher.py`, which owns the HA
side. It holds ONE wildcard subscription covering every device, which is the
single most important thing to know here: anything that escapes per-message
handling takes down the bridge for all devices at once (see `_consume`).

The device-side event_type strings this module dispatches on (`pet_in`,
`clean_over`, ...) are NOT capture-confirmed — see `events/ingest.py`'s header
for what is. An unrecognised one degrades to "record it, publish state" rather
than failing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterable

from petkit_local.devices.ble import (
    BLEDevice, _iter_ble_frames, ble_command_frame, build_ble_device_result, parser_for,
)
from petkit_local.devices.registry import DeviceRegistry, get_setting_fields
from petkit_local.events import codes
from petkit_local.events.ingest import (apply_derived_state, apply_state_snapshot,
                                        entity_for_event, telemetry_only)
from petkit_local.mqtt.topics import (
    parse_topic, event_reply_topic, is_server_published, service_topic, user_get_topic,
)
from petkit_local.utils.dicts import dig

if TYPE_CHECKING:
    from petkit_local.ai.pets import PetRegistry
    from petkit_local.devices.base import Device
    from petkit_local.devices.ble import BLERegistry
    from petkit_local.events.store import EventStore
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.mqtt.upstream import UpstreamMQTT
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

# The embedded broker needs a moment to bind before the first connect attempt;
# without it every startup wastes a full reconnect delay.
STARTUP_DELAY_SECONDS = 2.0


def _error_text(err: object, device_type: str) -> str:
    """An `error_start` payload rendered the way an `err{}` block is.

    The field arrives in more than one shape -- one flag name, several of them,
    or a bit object like the one a property post carries -- and all three end in
    the same Error sensor, so all three get the same vocabulary. An unknown name
    survives as itself, exactly as `error_flag_label` promises.
    """
    if isinstance(err, dict):
        names = [str(k) for k, v in err.items() if v]
    elif isinstance(err, (list, tuple)):
        names = [str(v) for v in err if v]
    else:
        names = [part.strip() for part in str(err).split(",") if part.strip()]
    return ", ".join(codes.error_flag_label(n, device_type) for n in names)


RECONNECT_DELAY_SECONDS = 5.0

# Video/photo notifications and captured frames can be large, and a broken
# frame is exactly the thing that gets logged — bound what reaches the log.
PAYLOAD_LOG_CHARS = 200


def _dumps(payload: object) -> str:
    """Serialize a device-facing MQTT frame as COMPACT JSON (no whitespace).

    The T5's Aliyun LinkSDK data-model parser (`__dm_recv_handler` ->
    `on_iot_dm_recv_service_invoke`) silently DROPS a `thing/service/*` frame
    whose JSON carries spaces after `:` / `,`, before any handler runs or any
    log line is written — so a spaced command and a lost command look identical.
    Confirmed on hardware by an A/B test on one connection 12s apart: the spaced
    `thing.service.start` did nothing, the byte-compact one drove the motor and
    the box scooped. `json.dumps` defaults to `", "`/`": "`; the real cloud and
    localkit both emit compact, so every broker->device publish must too.
    """
    return json.dumps(payload, separators=(",", ":"))


def _payload_text(payload: object) -> str:
    """Decode a raw MQTT payload to text, never raising on a binary frame."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


def _excerpt(payload: object, limit: int = PAYLOAD_LOG_CHARS) -> str:
    """Render a raw MQTT payload as bounded, log-safe text."""
    text = _payload_text(payload)
    return (text[:limit] + "...") if len(text) > limit else text


def _event_content(params: dict) -> dict:
    """Many events carry their real data as a JSON string in params.content
    (e.g. pet_out -> {"pet_weight": ...}, error_start -> {"err": ...}).

    Returns an empty dict for anything that isn't a decodable JSON object, so
    a caller can `.get()` the fields it wants without a shape check.
    """
    c = params.get("content") if isinstance(params, dict) else None
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            return {}
    return c if isinstance(c, dict) else {}

# Which HA `event` entity an event fires now lives in
# `events/ingest.py::entity_for_event`. It used to be a literal map of MQTT
# topic names here, which the HTTP handler imported too — and which silently
# never matched there, because over HTTP `event_type` is a numeric code rather
# than a name. Resolving through `codes.lookup` handles both namespaces and
# reproduces every entry of the old table exactly.


class MQTTBridge:
    """Bridges device MQTT messages to HA state updates.

    Every collaborator except the device registry is optional, and each is
    checked before use: the bridge has to run in an install with no HA broker,
    no BLE accessories and no event store, and it is also constructed bare in
    the tests. `self._client` is None until `start()` connects, so anything
    that publishes downstream (BLE polls, user/get replies, event acks) is
    silently skipped while offline rather than queued — a stale reply is worse
    than none.

    Not thread-safe and not meant to be: the whole add-on runs on one asyncio
    loop, which is what lets the handlers mutate device state without locking.
    """

    def __init__(self, registry: DeviceRegistry, ha_publisher: HAPublisher | None = None,
                 ble_registry: BLERegistry | None = None,
                 capture: bool = False, capture_dir: str = "/data/capture",
                 api_url: str = "", hub: EventHub | None = None,
                 event_store: EventStore | None = None,
                 pet_registry: PetRegistry | None = None,
                 live_config: dict[str, Any] | None = None,
                 upstream: UpstreamMQTT | None = None) -> None:
        """Wire the bridge's collaborators; nothing connects until `start()`.

        Args:
            registry: The only required collaborator — a message that cannot be
                attributed to a known device is dropped, so without it the
                bridge would have nothing to do.
            ha_publisher: HA side. None means parse and record, publish nothing.
            ble_registry: Enables the BLE relay paths (`_poll_ble_accessories`,
                `_handle_ble_response`, K3 piggyback merging).
            capture: Starting value for capture mode. Superseded by
                `live_config` when one is given — see that argument.
            api_url: This add-on's own base URL, needed only to answer a
                `data_get` for `dev_serverinfo`; empty means decline that one.
            hub: Web-panel event hub for the live MQTT log.
            event_store: Persists discrete events. None means real-time only.
            pet_registry: Used to refresh a pet's HA device when an event is
                attributed to it; needs `ha_publisher` to have any effect.
            live_config: The SAME dict the HTTP handlers and the panel share, so
                `capture` and proxy mode take effect here without a restart.
                Before this existed, `capture` was frozen at construction and
                the panel's toggle reached HTTP immediately but MQTT never.
            upstream: The proxy-mode bridge to the real Aliyun broker
                (`mqtt/upstream.py`). None disables that half entirely.
        """
        self._registry = registry
        self._ha_publisher = ha_publisher
        self._ble_registry = ble_registry
        self._live_config = live_config if live_config is not None else {}
        # Fallback for the constructor arguments, used only when no live config
        # was wired (the bare bridges the tests build).
        self._capture_default = capture
        self._capture_dir_default = capture_dir
        self._api_url = api_url
        self._hub = hub
        self._event_store = event_store
        self._pet_registry = pet_registry
        self._upstream = upstream
        self._ble_poll_ts: dict[int, float] = {}
        #: Per-accessory BLE frame sequence, wrapping at a byte.
        self._ble_seq: dict[int, int] = {}
        self._client = None

    @property
    def _capture(self) -> bool:
        """Whether to write frames to disk, re-read per message.

        A property and not a stored flag: the panel flips capture live, and this
        is the value that used to need a restart to notice.
        """
        return bool(self._live_config.get("capture", self._capture_default))

    @property
    def _capture_dir(self) -> str:
        """Where `_capture` writes; the live config wins when it names one."""
        return self._live_config.get("capture_dir") or self._capture_dir_default

    async def start(self, broker_host: str = "localhost", broker_port: int = 1883) -> None:
        """Connect to the embedded broker and consume forever, reconnecting.

        Returns immediately only when aiomqtt is missing, which disables the
        bridge outright (HTTP heartbeat still works, just without real-time
        events or command push).
        """
        try:
            import aiomqtt
        except ImportError:
            log.warning("aiomqtt not installed - MQTT bridge disabled")
            return

        await asyncio.sleep(STARTUP_DELAY_SECONDS)

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=broker_host,
                    port=broker_port,
                    identifier="petkit-local-bridge",
                ) as client:
                    # The INNER finally is the point: `self._client` has to be
                    # cleared the moment the session ends, not once the reconnect
                    # handler below has finished — a `finally` on the outer
                    # try/except runs only after that handler's `sleep`, leaving
                    # the attribute pointing at a dead client for the whole delay.
                    # In that window `publish_to_device` would publish instead of
                    # raising, and its caller's fallback to the heartbeat queue —
                    # the only other way a command reaches the device — never
                    # happens.
                    self._client = client
                    try:
                        log.info("MQTT bridge connected to broker at %s:%d",
                                 broker_host, broker_port)

                        await client.subscribe("#")

                        # aiomqtt signals a lost connection by raising MqttError
                        # out of the message iterator — that one must reach the
                        # handler below, unlike a per-message failure.
                        await self._consume(client.messages, (aiomqtt.MqttError,))
                    finally:
                        self._client = None

            except Exception as e:
                log.warning("MQTT bridge connection lost (%s), reconnecting in %ds...",
                            e, RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _consume(self, messages: AsyncIterable[Any],
                       fatal: tuple[type[BaseException], ...] = ()) -> None:
        """Dispatch incoming messages, surviving a failure on any single one.

        The bridge holds ONE wildcard subscription for every device, so letting
        a per-message exception escape to the reconnect handler would drop the
        subscription for all of them — one device publishing a malformed frame
        could take the whole bridge down in a reconnect loop.

        Args:
            messages: Async iterable of aiomqtt messages.
            fatal: Exception types that mean the connection itself is gone and
                must therefore propagate to the reconnect handler.
        """
        async for message in messages:
            try:
                await self._handle_message(message)
            except asyncio.CancelledError:
                raise  # shutdown, not a bad message
            except fatal:
                raise
            except Exception:
                log.exception("Dropped MQTT message on %s: %s",
                              message.topic, _excerpt(getattr(message, "payload", b"")))

    async def _handle_message(self, message: Any) -> None:
        """Route one broker message to the device it belongs to.

        Also the point where an event is acknowledged. The Aliyun protocol
        pairs every `thing/event/*/post` with a server `post_reply`, so the
        ack goes out for every event topic — including event types this bridge
        does nothing else with, since the device is owed a reply either way.
        """
        topic = str(message.topic)
        raw = message.payload
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            payload = None

        if self._capture:
            from petkit_local.utils.capture import capture_record
            capture_record(self._capture_dir, "mqtt", {
                "topic": topic,
                # Capture is a reverse-engineering aid: keep undecodable frames
                # in full, they are the ones worth studying.
                "payload": payload if isinstance(payload, (dict, list)) else _payload_text(raw),
            })

        # Every handler below reads the Aliyun envelope by key, so a frame that
        # isn't a JSON object has nowhere to go. Dropping it here (rather than
        # passing raw bytes on and letting `.get()` blow up mid-handler) keeps
        # the failure to the one device that sent it.
        if not isinstance(payload, dict):
            log.warning("Ignoring non-object MQTT payload on %s: %s", topic, _excerpt(raw))
            return

        parsed = parse_topic(topic)
        if not parsed:
            return

        if is_server_published(parsed):
            # Our own frame, handed back by the wildcard subscription. The
            # publish side already logged it, so dropping it here costs nothing
            # and keeps it from being counted as device traffic, stamping the
            # device's liveness, or appearing in the log as something the device
            # said.
            return

        device = self._registry.by_mqtt_name(parsed.product_key, parsed.device_name)
        if not device:
            log.debug("Message for unknown device: %s/%s", parsed.product_key, parsed.device_name)
            return

        device.online = True
        # The liveness stamp for a device that has stopped polling HTTP because
        # it is talking to us here instead — see `Device.last_mqtt`.
        device.last_mqtt = time.time()
        if self._hub is not None:
            self._hub.record_mqtt(device.petkit_id, topic, payload,
                                  client=device.mqtt_device_name)

        # Proxy mode: relay what the DEVICE said to the real cloud. In a
        # `finally`, for two reasons that pull in opposite directions and are
        # both satisfied here. LAST, so a slow publish to Aliyun cannot delay
        # this frame's ingestion, its HA publish, or the `post_reply` the device
        # is waiting for. But UNCONDITIONALLY, because the local handling above
        # can raise — an event-store write failing, the HA publisher erroring, a
        # `post_reply` hitting a dropped broker — and an observation mode that
        # silently stops observing whenever something else breaks is worse than
        # useless. `forward_up` swallows everything itself, so it cannot mask the
        # exception on its way past.
        try:
            if parsed.category == "event":
                await self._handle_event(device, parsed.detail, payload)

                if self._client:
                    reply_topic = event_reply_topic(parsed.product_key, parsed.device_name,
                                                    parsed.detail)
                    msg_id = payload.get("id", str(int(time.time())))
                    reply = {
                        "code": 200,
                        "data": {},
                        "id": msg_id,
                        "message": "success",
                        "method": f"thing.event.{parsed.detail}.post",
                        "version": "1.0.0",
                    }
                    await self._client.publish(reply_topic, _dumps(reply))

            elif parsed.category == "ota":
                log.info("OTA inform from %s (id=%d)", device.device_type, device.petkit_id)
        finally:
            if self._upstream is not None:
                await self._upstream.forward_up(device, topic, payload)

    async def _handle_event(self, device: Device, event_type: str, payload: dict) -> None:
        """Apply one `thing/event/{event_type}/post` to the device and HA.

        The branches below only enrich `device.state` with what the entity
        templates read. Persistence, HA event entities and the state/
        availability re-publish at the end run for EVERY event type, known or
        not, so an unrecognised one is still recorded and still marks the
        device online.
        """
        log.info("Event '%s' from %s (id=%d): %s",
                 event_type, device.device_type, device.petkit_id,
                 json.dumps(payload)[:200])

        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}

        content = _event_content(params)

        # An event carries a full state snapshot, and it is sometimes the only
        # carrier of a value that just changed: an N60 reset from PetKit's app
        # moved `sprayResetTime` and announced it inside `liquid_reset_over`
        # while the `property` stream stayed silent for 74 minutes either side.
        # Applied BEFORE apply_derived_state, the same order the HTTP path uses,
        # so a derived timestamp is not overwritten by the snapshot.
        if apply_state_snapshot(device, params.get("state")):
            self._registry.mark_dirty()

        # Last Clean / Last Visit / Last Feed / Pet Weight exist only as a
        # consequence of an event, so they are derived here rather than in any
        # state parser. Shared with the HTTP path (`handlers/stubs.py`) so the
        # two transports cannot drift — which is exactly what had happened.
        apply_derived_state(device, event_type, content)

        if event_type == "property":
            if params:
                # Raw nested, for the panel's Debug info and the echoes -- but
                # WITHOUT the transport envelope. Every property post carries
                # `XDevice`, the signed request credential, and merging it here
                # put it in `device.state` and straight into the panel's raw
                # state view.
                device.state.update(telemetry_only(params))
                # Overlay the flat keys the entities read (MQTT nests differently
                # from the HTTP state_report).
                from petkit_local.devices.state_parsers import (apply_consumable_state,
                                                                normalize_property_params)
                device.state.update(normalize_property_params(device.device_type, params))
                apply_consumable_state(device)
                self._sync_settings_from_device(device, params)
                self._registry.mark_dirty()
                k3 = self._update_linked_k3(device, params)
                if k3 and self._ha_publisher:
                    await self._ha_publisher.publish_ble_discovery(k3)
                    await self._ha_publisher.publish_ble_state(k3)
                await self._poll_ble_accessories(device)

        # NOTE: the work / pet / feed / move / pet_detect branches used to copy
        # their whole raw `params` into `device.state` as `<name>_event` and
        # `<name>_params`, plus a catch-all `f"{event_type}_params"`. Nothing
        # ever read any of them -- the event itself is already persisted by
        # `store.upsert_event`, shown in the Timeline and published as an HA
        # event entity. All they did was inflate `device.state`, which is merged
        # into and never pruned, and then get dumped verbatim into the panel's
        # "Raw parsed state JSON" -- including an `XDevice` string carrying the
        # request signature. `state` holds what entities read; nothing else.
        if event_type in ("error_start", "error_over"):
            err = content.get("err", content.get("errorMsg"))
            if err is not None:
                # Through the same table `err{}` bits go through. An
                # `error_start` carries the fault as ONE of those names rather
                # than as a set of bits, so writing it raw meant the Error
                # sensor read "Tray full" when the fault arrived on a property
                # post and `taryF` when the same fault arrived as an event.
                device.state["errorMsg"] = "" if event_type == "error_over" \
                    else _error_text(err, device.device_type)

        elif event_type == "data_get":
            await self._reply_user_get(device, params)

        elif event_type == "ble_response":
            await self._handle_ble_response(device, params)

        # Persist to the event store, skipping pure protocol/telemetry messages:
        # "property" is continuous state rather than a discrete event, and
        # data_get / ble_response / ble_relay_* are plumbing. Mirrors
        # http/handlers/stubs.py's dev_event_report handling so events land
        # regardless of transport.
        #
        # Read from `codes.MQTT_TRANSPORT_TOPICS` rather than repeated here,
        # which is what that constant asks for and what the two had drifted
        # apart on: three of its six names were missing, so every BLE relay
        # session opened and closed put two rows in the Timeline. Harmless
        # while nothing polled; a steady stream once `poll_ble_loop` existed.
        if self._event_store is not None and event_type not in codes.MQTT_TRANSPORT_TOPICS:
            from petkit_local.events import ingest
            row = ingest.from_mqtt(device, event_type, params)
            # Same rule as the HTTP path: only an identity we can prove is ours
            # becomes `pet_id` (see ai/pets.py::resolve_pet_ref).
            if self._pet_registry is not None:
                row["pet_id"] = await self._pet_registry.resolve_pet_ref(row.get("pet_ref"))
            await self._event_store.upsert_event(row)
            if self._hub is not None:
                self._hub.publish("event", device.petkit_id, f"{event_type} ({row['event_kind']})")
            if row.get("pet_id") is not None and self._pet_registry is not None and self._ha_publisher:
                pet = await self._pet_registry.get(row["pet_id"])
                if pet:
                    await self._ha_publisher.publish_pet_discovery(pet)
                    await self._ha_publisher.publish_pet_state(pet, self._event_store)

        # Fire the matching HA event entity (momentary).
        entity_suffix = entity_for_event(event_type, device.device_type)
        if entity_suffix and self._ha_publisher:
            await self._ha_publisher.publish_event(device, entity_suffix, event_type, params)

        if self._ha_publisher:
            await self._ha_publisher.publish_state(device)
            await self._ha_publisher.publish_availability(device)

    def _sync_settings_from_device(self, device: Device, params: dict) -> None:
        """Route device-originated setting keys into config["settings"] so a
        physical/on-device change reflects in the HA controls.

        Only keys the device's own entity definitions declare as settings are
        copied — a property post carries telemetry and settings in one flat
        dict, and letting telemetry into `settings` would make it look like a
        user-set value and get echoed back on the next write.
        """
        fields = get_setting_fields(device)
        if not fields:
            return
        settings = device.config.setdefault("settings", {})
        for k, v in params.items():
            if k in fields:
                settings[k] = v

    async def publish_ble_command(self, device: Device, ble: BLEDevice,
                                 cmd: int, payload: bytes) -> bool:
        """Write one framed command to an accessory, through its parent.

        The mirror of `_handle_ble_response`: `thing/service/ble` carries the
        MQTT `cmd` alongside the same `FA FC FD ... FB` frame the accessory
        answers with. The parent forwards the bytes; it does not interpret them.

        Returns False when nothing could be sent, so a caller does not report
        success for a command that never left.
        """
        if not self._client:
            return False
        seq = self._ble_seq.get(ble.petkit_id, 0)
        self._ble_seq[ble.petkit_id] = (seq + 1) & 0xFF
        data = ble_command_frame(cmd, seq, payload)
        if data is None:
            log.warning("No BLE opcode known for cmd %s (%s id=%d)",
                        cmd, ble.ble_type, ble.petkit_id)
            return False
        now = int(time.time())
        envelope = {
            "method": "thing.service.ble",
            "id": str(now),
            "params": {
                "device": {"type": ble.ble_type_int, "mac": ble.wire_mac},
                "payload": {"cmd": cmd, "data": data},
                "timestamp": now,
            },
            "version": "1.0.0",
        }
        topic = service_topic(device.mqtt_product_key, device.mqtt_device_name, "ble")
        body = _dumps(envelope)
        await self._client.publish(topic, body)
        if self._hub:
            self._hub.record_mqtt(device.petkit_id, topic, body, outbound=True)
        log.info("BLE cmd %d -> %s (mac=%s) via parent %d",
                 cmd, ble.ble_type, ble.mac, device.petkit_id)
        return True

    async def request_ble_reading(self, device: Device, ble: BLEDevice) -> bool:
        """Ask the parent for a reading from this accessory, right now.

        Bypasses the `interval` throttle on purpose: this exists for the moment
        somebody is standing in front of the panel asking "is it alive", and
        making them wait up to four minutes for the timer is the opposite of an
        answer. The throttle is then re-armed so the periodic loop does not
        immediately ask again.
        """
        if not self._client:
            return False
        await self._ble_connect(device, ble, action=1)
        self._ble_poll_ts[ble.petkit_id] = time.time()
        return True

    async def poll_ble_loop(self, period: float = 30.0) -> None:
        """Ask every paired accessory's parent for a reading, forever.

        An accessory reports only when the server tells its parent to open a
        BLE session -- the parent never does it unprompted. Until this existed
        the only thing that asked was the handler for the parent's own
        `property/post`, which meant an accessory paired to a FEEDER never
        reported at all: a feeder does not send that topic. Its owner saw an
        accessory that paired, appeared in Home Assistant, and stayed unknown.

        A timer rather than a reaction, because that is what the cloud does and
        because it is the only trigger that does not depend on the parent
        having something of its own to say. `_poll_ble_accessories` still holds
        each accessory's `interval`, so this loop can tick often without
        talking often; `period` is only how finely those intervals are honoured.
        """
        while True:
            try:
                await asyncio.sleep(period)
                if not self._client or not self._ble_registry:
                    continue
                for ble in self._ble_registry.all():
                    if not ble.link_with:
                        continue
                    parent = self._registry.get(ble.link_with)
                    if parent is not None:
                        await self._poll_ble_accessories(parent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not end the loop
                log.exception("BLE poll loop")

    async def _ble_connect(self, device: Device, ble: BLEDevice, action: int) -> None:
        """Open (`action=1`) or close (`action=0`) the parent's BLE session.

        `thing.service.connect` is the only way an accessory reports at all:
        the parent does not poll on its own initiative, so nothing arrives
        until this is pushed. Closing matters too — an accessory whose session
        is never ended keeps the parent's radio busy between readings.
        """
        if not self._client:
            return
        now = int(time.time())
        envelope = {
            "method": "thing.service.connect",
            "id": str(now),
            "params": {
                "connect_action": action,
                "device": {"type": ble.ble_type_int, "mac": ble.wire_mac},
                "timestamp": now,
            },
            "version": "1.0.0",
        }
        topic = service_topic(device.mqtt_product_key, device.mqtt_device_name, "connect")
        payload = _dumps(envelope)
        await self._client.publish(topic, payload)
        # Recorded like any other outbound frame. Without this the whole BLE
        # conversation was invisible in the panel's live log, which is the one
        # place somebody debugging a silent accessory would look.
        if self._hub:
            self._hub.record_mqtt(device.petkit_id, topic, payload, outbound=True)
        log.info("BLE %s -> %s (mac=%s) via parent %d",
                 "connect" if action else "disconnect",
                 ble.ble_type, ble.mac, device.petkit_id)

    async def _poll_ble_accessories(self, device: Device) -> None:
        """Ask the parent to open a BLE relay session for each linked accessory
        so it starts posting ble_response frames. Without this poll the parent
        never relays W5 data. Throttled per accessory `interval`.

        Mirrors localkit ServiceConnect (thing/service/connect, connect_action=1).
        """
        if not self._client or not self._ble_registry:
            return
        # `non_k3_for_parent`, not `get_linked`: a K3 is never in the relay list
        # at all (it travels inside the parent's own device_info), so asking a
        # parent to open a BLE session for one sent a `connect` with `type: 0`.
        linked = self._ble_registry.non_k3_for_parent(device.petkit_id)
        if not linked:
            return
        now = time.time()
        for ble in linked:
            interval = getattr(ble, "interval", 240) or 240
            if now - self._ble_poll_ts.get(ble.petkit_id, 0) < interval:
                continue
            self._ble_poll_ts[ble.petkit_id] = now
            await self._ble_connect(device, ble, action=1)

    async def _reply_user_get(self, device: Device, params: dict) -> None:
        """Answer an MQTT data_get by publishing the requested resource to
        /{pk}/{dn}/user/get (localkit UserGet)."""
        if not self._client:
            return
        data_type = dig(params, "dataType", default="")
        payload = self._user_get_payload(device, data_type)
        if payload is None:
            log.debug("data_get: unsupported/empty dataType %r for device %d", data_type, device.petkit_id)
            return
        topic = user_get_topic(device.mqtt_product_key, device.mqtt_device_name)
        await self._client.publish(topic, _dumps(payload))
        log.info("Replied user/get %s for device %d", data_type, device.petkit_id)

    async def publish_user_get(self, device: Device, payload: dict) -> bool:
        """Push one server-initiated frame to /{pk}/{dn}/user/get.

        The same topic `_reply_user_get` answers a data_get on, used here to
        START a conversation rather than answer one — it is the only downstream
        channel that carries the firmware's `msgType` envelope, which is what a
        heartbeat command is (`patchers/common.py::build_run_cmd`).

        Two properties matter and both are already established: the topic is in
        `topics.downstream_filters`, so the broker subscribes the device to it
        on connect (a T5 sends no SUBSCRIBE of its own), and the firmware
        dispatches it through `__mqtt_recv_handler`/`on_iot_recv_from_topic`,
        which — unlike the `thing/service/*` data-model parser — tolerates
        whitespace in the JSON. It still goes out compact, like everything else.

        Returns:
            True if it was published. False when the bridge has no client, so
            the caller can fall back rather than assume it landed.
        """
        if not self._client:
            return False
        topic = user_get_topic(device.mqtt_product_key, device.mqtt_device_name)
        await self._client.publish(topic, _dumps(payload))
        if self._hub is not None:
            self._hub.record_mqtt(device.petkit_id, topic, _dumps(payload),
                                  outbound=True, client=device.mqtt_device_name)
        return True

    def _user_get_payload(self, device: Device, data_type: str) -> dict | None:
        """Build the answer to one MQTT data_get.

        `dataType` names the same resources the HTTP API serves, and the
        payloads are deliberately the same objects those handlers return, so
        a device gets identical config over either transport.

        Returns:
            The resource dict, or None when the type is unsupported or the
            resource simply isn't configured (no schedule, no server URL) —
            the caller then stays silent rather than replying with an empty
            resource the device would take as authoritative.
        """
        if data_type == "dev_device_info":
            return device.to_device_info(self._ble_registry)
        if data_type == "dev_multi_config":
            return device.to_multi_config()
        if data_type == "dev_serverinfo":
            return device.to_serverinfo(self._api_url) if self._api_url else None
        if data_type == "dev_schedule_get":
            sched = device.config.get("schedule")
            return {"result": sched} if sched is not None else None
        if data_type == "dev_feed_get":
            feed = device.config.get("feed_schedule")
            return {"result": feed} if feed is not None else None
        if data_type == "dev_ble_device":
            # Same shape as `http/handlers/ble_device.py`: always include `list`,
            # empty when nothing is paired. Omitting it made the firmware log
            # "ble list len too short"; PetKit's cloud sends `list: []` routinely.
            # Goes through `build_ble_device_result` because THIS transport's
            # compact separators land under the firmware's byte-length gate on
            # their own — see that function's docstring.
            lst = []
            if self._ble_registry:
                for b in self._ble_registry.non_k3_for_parent(device.petkit_id):
                    lst.append(b.to_ble_list_entry())
            return {"result": build_ble_device_result(lst)}
        return None

    def _update_linked_k3(self, device: Device, params: dict) -> BLEDevice | None:
        """Merge K3 consumable fields piggybacked on the parent's property post.

        The K3 spray is BLE-only and never posts anything itself: its
        battery and liquid level ride along in the parent litter box's own
        property post, which is why they are picked out here rather than in
        `_handle_ble_response`.

        Returns:
            The K3 BLEDevice if anything changed (the caller re-publishes it to
            HA), else None.
        """
        if not self._ble_registry:
            return None
        k3 = self._ble_registry.get_linked_k3(device.petkit_id)
        if not k3:
            return None

        updated = False
        if "battery" in params:
            k3.state.setdefault("consumables", {})["battery"] = params["battery"]
            updated = True
        if "liquid" in params:
            k3.state.setdefault("consumables", {})["liquid"] = params["liquid"]
            updated = True

        if not updated:
            return None
        self._ble_registry.mark_dirty()
        log.info("Updated K3 (id=%d) from parent device %d: battery=%s liquid=%s",
                 k3.petkit_id, device.petkit_id,
                 params.get("battery", "?"), params.get("liquid", "?"))
        return k3

    async def _handle_ble_response(self, device: Device, params: dict) -> None:
        """Apply one relayed BLE frame to the accessory it came from.

        The accessory is identified by MAC, not by the relaying parent: the
        same W5 can in principle be reachable through more than one WiFi
        device, and the MAC is the only stable key in the frame.
        """
        if not self._ble_registry:
            return

        content = params.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                content = {}

        ble_mac = dig(content, "device", "mac", default="")

        ble_dev = self._ble_registry.get_by_mac(ble_mac) if ble_mac else None
        if not ble_dev:
            log.debug("BLE response with no matching accessory (mac=%s)", ble_mac)
            return

        parse = parser_for(ble_dev.ble_type)
        fragment = parse(content) if parse is not None else {}
        if fragment:
            for section, vals in fragment.items():
                ble_dev.state.setdefault(section, {}).update(vals)
            ble_dev.last_seen = time.time()
            self._ble_registry.mark_dirty()
            log.info("Decoded %s (id=%d) from parent %d: %s",
                     ble_dev.ble_type.upper(), ble_dev.petkit_id,
                     device.petkit_id, json.dumps(fragment)[:120])
        else:
            # Not a status frame, or one we cannot read. Either way, name what
            # came back: a write is answered with a bare `01` on its own cmd,
            # or a short 251/252, and dropping those in silence left "the
            # switch flipped back" as the only symptom of a frame the accessory
            # did not accept.
            replies = [(cmd, bytes(data).hex() or "(empty)")
                       for cmd, data in _iter_ble_frames(content)]
            if replies:
                log.info("%s (id=%d) replied: %s", ble_dev.ble_type.upper(),
                         ble_dev.petkit_id,
                         ", ".join(f"cmd {c} {d}" for c, d in replies))
            else:
                log.info("%s ble_response not decodable yet (id=%d) - turn capture on in the "
                         "panel (Setup -> Settings) to collect frames",
                         ble_dev.ble_type.upper(), ble_dev.petkit_id)

        # The reading is in; let the parent close its radio rather than holding
        # the session until something else happens to end it.
        await self._ble_connect(device, ble_dev, action=0)

        if self._ha_publisher:
            await self._ha_publisher.publish_ble_discovery(ble_dev)
            await self._ha_publisher.publish_ble_state(ble_dev)

    async def publish_to_device(self, device: Device, topic_suffix: str, payload: dict) -> None:
        """Push a command to a device over `thing/service/{topic_suffix}`.

        The one method here called from outside: `ha/publisher.py` wires the
        bridge in as its command sink. It RAISES when the bridge has no broker
        connection, deliberately — the caller's fallback is to queue the
        command for the next HTTP heartbeat, and it can only do that if a
        failure is visible.

        Raises:
            ConnectionError: the bridge is not connected to the broker.
        """
        if not self._client:
            raise ConnectionError("MQTT bridge not connected")

        topic = service_topic(device.mqtt_product_key, device.mqtt_device_name, topic_suffix)
        await self._client.publish(topic, _dumps(payload))
        log.info("Published to device %d: %s -> %s", device.petkit_id, topic, _dumps(payload)[:100])
        # The other half of the conversation. Without this the panel's log shows
        # a device's frames but not the ones sent back, and a command that went
        # out over MQTT is visible only as the `cmd` line, which carries neither
        # topic nor payload.
        if self._hub is not None:
            self._hub.record_mqtt(device.petkit_id, topic, payload, outbound=True,
                                  client=device.mqtt_device_name)
