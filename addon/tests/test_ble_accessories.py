"""Pairing a BLE accessory, and the chain it unlocks.

A K3 purifier or W5 fountain has no network identity: a mains-powered
neighbour relays for it. The device does NOT discover accessories — it pulls a
list from the cloud and scans for exactly those MACs, and no firmware has any
way to report a newly-found one upward. Pairing happens in PetKit's app, i.e.
in the cloud, so with the app gone the cloud is us and the pairing has to be
entered here.

Everything downstream of that already existed and was completely untested,
which is how it stayed unreachable for so long.
"""
import json
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.ble import MIN_BLE_REPLY_BYTES, BLERegistry, normalize_mac
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.server import create_app
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

HDR = {"X-Device": "id=10&sn=SN10"}
DEVICE_CONFIG = {"api_url": "http://x/6/", "mqtt_port": 1883, "proxy_mode": False,
                 "proxy_upstream": "", "proxy_block_run_cmd": True, "capture": False}


def _panel(reg=None, ble=None):
    reg = reg or DeviceRegistry()
    ble = ble or BLERegistry()
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    return create_panel_app(reg, ble, EventHub(), cfg, None), reg, ble


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def _pair(c, **over):
    body = {"ble_type": "w5", "petkit_id": 700, "mac": "AA:BB:CC:DD:EE:FF",
            "secret": "s3cret", "interval": 240, "link_with": 10}
    body.update(over)
    return await c.post("/api/ble", data=json.dumps(body))


# --- MAC handling -----------------------------------------------------------

@pytest.mark.parametrize("written, seen", [
    ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
    ("aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF"),
    ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
])
def test_a_mac_matches_however_either_side_spelled_it(written, seen):
    """The MAC arrives from two directions that do not agree on formatting —
    typed by a person, and read out of a relayed frame — and a mismatch is
    invisible: the frame is dropped by a debug log with nothing to show."""
    reg = BLERegistry()
    reg.register(ble_type="w5", petkit_id=700, mac=normalize_mac(written), link_with=10)
    assert reg.get_by_mac(seen) is not None


@pytest.mark.parametrize("bad", ["", "not-a-mac", "AA:BB:CC", "AA:BB:CC:DD:EE:GG", "1234567890123"])
def test_an_unusable_mac_is_rejected_rather_than_stored(bad):
    assert normalize_mac(bad) == ""


# --- pairing ----------------------------------------------------------------

async def test_pairing_makes_the_accessory_appear_everywhere_at_once():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        body = await (await _pair(c)).json()
        assert [a["petkit_id"] for a in body["accessories"]] == [700]

        dev = ble.get(700)
        assert dev.ble_type == "w5" and dev.link_with == 10
        # Stored canonical, so a frame in any spelling still matches.
        assert dev.mac == "AABBCCDDEEFF"
    finally:
        await c.close()


async def test_the_wire_entry_is_exactly_the_five_keys_the_firmware_parses():
    """`ble_relay_network.c` logs `dev[%d],id/mac/secret/interval/type` — those
    five names ARE the protocol, so the form is not a UI convenience."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        body = await (await _pair(c)).json()
        entry = body["accessories"][0]["wire_entry"]
        assert set(entry) == {"id", "mac", "secret", "interval", "type"}
        assert entry["id"] == 700 and entry["type"] == 14  # 14 = W5, per localkit
    finally:
        await c.close()


async def test_an_id_that_is_already_a_real_device_is_refused():
    """An accessory shares the `petkit_{id}` HA identity and the
    `petkit-local/{id}/state` topic with real devices, so a collision makes two
    devices fight over one entity set."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        r = await _pair(c, petkit_id=10)
        assert r.status == 409
        assert ble.get(10) is None
    finally:
        await c.close()


async def test_one_mac_cannot_be_paired_twice():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        assert (await _pair(c)).status == 200
        # Same MAC, different spelling, different id.
        assert (await _pair(c, petkit_id=701, mac="aabbccddeeff")).status == 409
    finally:
        await c.close()


@pytest.mark.parametrize("over, status", [
    ({"ble_type": "zz"}, 400),          # no entities would ever be published
    ({"ble_type": ""}, 400),
    ({"mac": "nope"}, 400),
    ({"petkit_id": -1}, 400),
    ({"link_with": 999}, 400),          # parent does not exist
])
async def test_bad_input_is_refused_with_a_reason(over, status):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        r = await _pair(c, **over)
        assert r.status == status
        assert (await r.json())["error"]
    finally:
        await c.close()


async def test_unpairing_removes_it_and_says_what_it_did_not_do():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        await _pair(c)
        body = await (await c.delete("/api/ble/700")).json()
        assert body["ok"] is True and body["accessories"] == []
        assert ble.get(700) is None
        # HA keeps the entities — nothing publishes an empty discovery payload.
        assert "Home Assistant" in body["note"]
        assert (await c.delete("/api/ble/700")).status == 404
    finally:
        await c.close()


# --- what pairing unlocks: the relay list -----------------------------------

async def test_the_device_is_told_to_scan_for_a_paired_accessory():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type="w5", petkit_id=700, mac="AABBCCDDEEFF",
                 secret="s3cret", interval=240, link_with=10)
    app = create_app(reg, dict(DEVICE_CONFIG))
    app["ble_registry"] = ble
    c = await _client(app)
    try:
        body = await (await c.get("/6/t5/dev_ble_device", headers=HDR)).json()
        assert body["result"]["nextTick"] == 3600
        # Lowercase on the wire: stored uppercase for comparison, sent in the
        # shape every captured cloud `dev_ble_device` uses.
        assert body["result"]["list"] == [
            {"id": 700, "mac": "aabbccddeeff", "secret": "s3cret",
             "interval": 240, "type": 14}]
    finally:
        await c.close()


async def test_nothing_paired_sends_an_empty_list():
    """Match PetKit's cloud: `list: []` with nextTick, not a bare `{}`.
    Omitting `list` logs `ble list len too short` on D4SH — and so does a
    no devices to relay."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app = create_app(reg, dict(DEVICE_CONFIG))
    app["ble_registry"] = BLERegistry()
    c = await _client(app)
    try:
        resp = await c.get("/6/t5/dev_ble_device", headers=HDR)
        raw = await resp.read()
        http_body = json.loads(raw)
        assert http_body["result"] == {}
    finally:
        await c.close()

    bridge = MQTTBridge(reg, None, BLERegistry())
    mqtt_body = bridge._user_get_payload(reg.get(10), "dev_ble_device")
    assert mqtt_body == http_body, "the two transports answer the same question differently"


async def test_a_k3_is_never_put_in_the_relay_list():
    """It is attached by naming it on the parent instead; listing it as well
    makes the firmware treat it as a second, unpaired device."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type="k3", petkit_id=555, mac="AABBCCDDEE01", link_with=10)
    app = create_app(reg, dict(DEVICE_CONFIG))
    app["ble_registry"] = ble
    c = await _client(app)
    try:
        body = await (await c.get("/6/t5/dev_ble_device", headers=HDR)).json()
        assert body["result"] == {}
    finally:
        await c.close()


# --- what pairing unlocks: a relayed frame reaching HA ----------------------

class _FakePublisher:
    def __init__(self):
        self.states = []

    async def publish_ble_discovery(self, dev):
        pass

    async def publish_ble_state(self, dev):
        self.states.append(dev.petkit_id)

    async def publish_state(self, device):
        pass

    async def publish_availability(self, device):
        pass


async def test_a_relayed_w5_frame_reaches_home_assistant():
    """The whole chain, which had no test: a paired accessory, a frame arriving
    under its MAC, decoded into the state the entity value_paths read."""
    import base64

    reg = DeviceRegistry()
    parent = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type="w5", petkit_id=700, mac="AABBCCDDEEFF", link_with=10)
    pub = _FakePublisher()
    bridge = MQTTBridge(reg, pub, ble)

    # cmd 230 status frame: powerStatus=1, mode=2, runningStatus=1, filter=65%.
    data = bytes([1, 2, 0, 0, 1, 0, 0, 0, 0, 0, 65, 1])
    await bridge._handle_event(parent, "ble_response", {"params": {"content": json.dumps({
        "device": {"mac": "aa:bb:cc:dd:ee:ff"},   # a different spelling on purpose
        "payload": [{"cmd": 230, "data": base64.b64encode(data).decode()}],
    })}})

    dev = ble.get(700)
    assert dev.state["states"]["powerStatus"] == 1
    assert dev.state["consumables"]["filterPercentage"] == 65
    assert 700 in pub.states


async def test_k3_consumables_ride_in_on_the_parents_own_report():
    reg = DeviceRegistry()
    parent = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type="k3", petkit_id=555, mac="AABBCCDDEE01", link_with=10)
    pub = _FakePublisher()
    bridge = MQTTBridge(reg, pub, ble)

    await bridge._handle_event(parent, "property", {"params": {"battery": 88, "liquid": 60}})
    k3 = ble.get(555)
    assert k3.state["consumables"] == {"battery": 88, "liquid": 60}
    assert 555 in pub.states


# --- what pairing unlocks: the K3 block in device_info ----------------------

def test_a_linked_k3_is_named_in_the_parents_device_info():
    reg = DeviceRegistry()
    parent = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type="k3", petkit_id=555, mac="AABBCCDDEE01",
                 serial_number="K3SN", secret="k3s", link_with=10)

    info = parent.to_device_info(ble)["result"]
    assert info["withK3"] == 1 and info["k3Id"] == 555
    assert info["k3Device"]["mac"] == "AABBCCDDEE01"
    assert info["k3Device"]["sn"] == "K3SN"


def test_no_k3_says_so_explicitly():
    reg = DeviceRegistry()
    parent = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    assert parent.to_device_info(BLERegistry())["result"]["withK3"] == 0


# --- pairing a K3 tells the parent about it ---------------------------------

async def test_pairing_a_k3_writes_k3id_on_the_parent():
    """A K3 is not in the relay list, so this property is the only thing that
    links it. Queued for the heartbeat when the parent is not on MQTT."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        await _pair(c, ble_type="k3", petkit_id=555, mac="AABBCCDDEE01")
        queued = [json.loads(x) if isinstance(x, str) else x
                  for x in reg.get(10).command_queue]
        assert any(q.get("params", {}).get("k3Id") == 555 for q in queued)
    finally:
        await c.close()


async def test_unpairing_a_k3_clears_it_on_the_parent():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        await _pair(c, ble_type="k3", petkit_id=555, mac="AABBCCDDEE01")
        reg.get(10).command_queue.clear()
        await c.delete("/api/ble/555")
        queued = [json.loads(x) if isinstance(x, str) else x
                  for x in reg.get(10).command_queue]
        assert any(q.get("params", {}).get("k3Id") == 0 for q in queued)
    finally:
        await c.close()


# --- persistence ------------------------------------------------------------

def test_a_pairing_survives_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ble_devices.json"
        reg = BLERegistry(persist_path=path)
        reg.register(ble_type="w5", petkit_id=700, mac="AABBCCDDEEFF",
                     secret="s3cret", link_with=10)
        reg.save()

        fresh = BLERegistry(persist_path=path)
        dev = fresh.get(700)
        assert dev is not None and dev.secret == "s3cret" and dev.link_with == 10


def test_removal_survives_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ble_devices.json"
        reg = BLERegistry(persist_path=path)
        reg.register(ble_type="w5", petkit_id=700, mac="AABBCCDDEEFF", link_with=10)
        reg.remove(700)
        assert BLERegistry(persist_path=path).get(700) is None


# --- the id is ours to choose -----------------------------------------------

async def test_an_omitted_id_is_allocated_rather_than_demanded():
    """The id is a handle for our side — it becomes the Home Assistant device
    id — and the firmware reports accessories back by MAC only, never by id.
    So there is nothing for a user to go and look up, and the form should not
    ask."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        body = await (await _pair(c, petkit_id=0)).json()
        allocated = body["accessories"][0]["petkit_id"]
        assert allocated >= 900001
        assert ble.get(allocated) is not None
    finally:
        await c.close()


async def test_allocated_ids_do_not_collide_with_each_other_or_a_device():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN10")
    reg.get_or_create(petkit_id=900001, device_type="t5", serial_number="ODD")
    app, reg, ble = _panel(reg=reg)
    c = await _client(app)
    try:
        first = (await (await _pair(c, petkit_id=0)).json())["accessories"][0]["petkit_id"]
        await _pair(c, petkit_id=0, mac="AABBCCDDEE02")
        ids = sorted(a["petkit_id"] for a in
                     (await (await c.get("/api/ble")).json())["accessories"])
        assert len(set(ids)) == 2
        # 900001 is taken by a (contrived) real device, so it was skipped.
        assert 900001 not in ids and first >= 900002
    finally:
        await c.close()


# --- which models have no network at all ------------------------------------

def test_the_fountains_without_wifi_are_marked_as_such():
    """Only the W7H has a radio that can reach us.

    W4, W5 and CTW2 are one BLE accessory family — `phldgmn/ha-petkit-ble`
    serves all three from one GATT profile and one parser, and it is a
    cloud-less integration. The CT-W3 manual is explicit that remote access
    needs a PetKit feeder or litter box within ~8 m acting as the WiFi master.
    They were listed as network devices only because PetKit's cloud API models
    them that way, and the account-side view is the same either way.
    """
    from petkit_local.devices.base import Device

    for codename in ("w4", "w5", "ctw2", "ctw3", "k2", "k3"):
        assert Device(device_type=codename, petkit_id=1).is_ble_only, codename
    for codename in ("w7h", "t5", "d4sh", "t3"):
        assert not Device(device_type=codename, petkit_id=1).is_ble_only, codename


def test_a_ble_only_model_registering_over_the_network_says_so(caplog):
    """It cannot happen, and if it does the table above is wrong about that
    model — so the log has to name it rather than let a wrong entity set be the
    first symptom. Registered anyway: a device is never told no."""
    import logging

    reg = DeviceRegistry()
    with caplog.at_level(logging.WARNING):
        device = reg.get_or_create(petkit_id=42, device_type="ctw2")
    assert device.device_type == "ctw2"          # never refused
    assert "BLE-only" in caplog.text
    assert "ctw2" in caplog.text


def test_a_normal_model_registers_quietly(caplog):
    import logging

    reg = DeviceRegistry()
    with caplog.at_level(logging.WARNING):
        reg.get_or_create(petkit_id=43, device_type="t5")
    assert "BLE-only" not in caplog.text


def test_the_w5_frame_decoder_covers_its_whole_family():
    """One protocol, one entity set. The `w5` string was hardcoded in three
    places, so a paired W4 or CTW2 would have decoded to nothing."""
    from petkit_local.devices.ble import W5_PROTOCOL, get_ble_entities, parser_for

    assert W5_PROTOCOL == {"w4", "w5", "ctw2"}
    for codename in W5_PROTOCOL:
        assert get_ble_entities(codename), codename
        assert parser_for(codename).__name__ == "parse_w5_ble_response", codename


def test_the_ctw3_is_not_read_with_the_w5_offsets():
    """Different block length, different layout, big-endian integers. Reading
    one with the other's offsets does not fail — it produces confident
    nonsense, which is worse."""
    from petkit_local.devices.ble import W5_PROTOCOL, get_ble_entities, parser_for

    assert "ctw3" not in W5_PROTOCOL
    assert parser_for("ctw3").__name__ == "parse_ctw3_ble_response"
    assert get_ble_entities("ctw3")


def test_the_captured_scan_types_are_the_captured_ones():
    """`dev_ble_device` hands the parent a `type` int to scan for. 14 was read
    off a real W5 pairing and 24 off a real CTW3 one (issue #4). The remaining
    two reuse the number of their own product line, which is an assumption and
    is marked as one."""
    from petkit_local.devices.ble import BLE_TYPE_CONFIRMED, BLE_TYPE_MAP, BLE_TYPES

    assert set(BLE_TYPES) == {"w5", "k3", "w4", "ctw2", "ctw3"}
    assert BLE_TYPE_MAP["w5"] == 14
    assert BLE_TYPE_MAP["ctw3"] == 24
    assert BLE_TYPE_CONFIRMED == {"w5", "ctw3"}
    # The guesses follow the product line, not the other line: a CTW2 sits with
    # the CTW3 rather than with the W5 it shares a BLE protocol with.
    assert BLE_TYPE_MAP["ctw2"] == BLE_TYPE_MAP["ctw3"]
    assert BLE_TYPE_MAP["w4"] == BLE_TYPE_MAP["w5"]


def test_a_guessed_scan_type_says_it_is_guessed():
    """A wrong `type` fails silently at both ends, so the panel has to be able
    to show which accessories are running on an invented number."""
    from petkit_local.devices.ble import BLEDevice

    assert BLEDevice(ble_type="ctw2", petkit_id=1).scan_type_is_guessed
    assert BLEDevice(ble_type="w4", petkit_id=1).scan_type_is_guessed
    assert not BLEDevice(ble_type="w5", petkit_id=1).scan_type_is_guessed
    assert not BLEDevice(ble_type="ctw3", petkit_id=1).scan_type_is_guessed
    # K3 is never in the scan list at all; its 0 is a placeholder, not a guess.
    assert not BLEDevice(ble_type="k3", petkit_id=1).scan_type_is_guessed


def test_the_owner_of_the_hardware_can_correct_the_guess():
    """The one person who can find out which value works is the one holding the
    fountain. An override beats waiting for a capture that may never come."""
    from petkit_local.devices.ble import BLEDevice

    default = BLEDevice(ble_type="ctw2", petkit_id=1, mac="AABBCCDDEEFF")
    assert default.to_ble_list_entry()["type"] == 24

    corrected = BLEDevice(ble_type="ctw2", petkit_id=1, mac="AABBCCDDEEFF",
                          scan_type=17)
    assert corrected.to_ble_list_entry()["type"] == 17
    assert not corrected.scan_type_is_guessed
    # And it survives a restart, or the correction is lost on every reload.
    assert BLEDevice.from_dict(corrected.to_dict()).scan_type == 17
    # And this is how 24 arrived: a CTW3 owner reported the value their own
    # parent was handed, and it came back into the table as evidence.


# --- CTW3 (EverSweet Max Cordless) ------------------------------------------
#
# The protocol map and this frame both come from issue #4, captured off a real
# CTW3 relayed by a D4SH. Everything below is checked against those bytes
# rather than against the prose, so a mis-typed offset fails here.

#: One `payload[].data` from a live `ble_response`, cmd 230.
CTW3_CMD230 = "AQEBAgAAAAAAABAgdzsBAAFGBAAUwBBvZABgCU8JAwMBLASwAQMAAAAA"


def _ctw3(data=CTW3_CMD230, cmd=230):
    from petkit_local.devices.ble import parse_ctw3_ble_response
    # `device.type` is deliberately the WRONG value here: the reporter had 14
    # in their relay list and the parent echoed it back. Matching is by MAC.
    return parse_ctw3_ble_response(
        {"device": {"type": 14, "mac": "a4c138aabbcc"}, "payload": [{"cmd": cmd, "data": data}]})


def test_a_real_ctw3_status_frame_decodes_field_for_field():
    st = _ctw3()["states"]
    assert st["powerStatus"] == 1
    assert st["suspendStatus"] == 1        # 1 is WORKING, not paused
    assert st["mode"] == 1                 # continuous
    assert st["electricStatus"] == 2       # not a boolean; 2 is the AC path
    assert st["runStatus"] == 1
    assert st["detectStatus"] == 0
    assert st["batteryPercent"] == 100
    assert _ctw3()["consumables"]["filterPercent"] == 59


def test_the_multi_byte_fields_are_big_endian():
    """Read the other way round these are astronomically wrong rather than
    slightly wrong, which is the one saving grace of getting it backwards."""
    st = _ctw3()["states"]
    assert st["waterPumpRunTime"] == 1056887
    assert st["todayPumpRunTime"] == 83460
    assert st["supplyVoltage"] == 5312     # mV, mains
    assert st["batteryVoltage"] == 4207    # mV, matches the cloud's 4223 sample


def test_the_config_tail_of_a_long_frame_is_decoded():
    """cmd 230 carries 12 more bytes than cmd 210, in the same layout a cmd-221
    write uses — which is what makes changing one setting possible without
    inventing the others."""
    st = _ctw3()["states"]
    assert st["energyInterval"] == 300     # 0x012C, five minutes
    assert st["sleepTime"] == 1200         # 0x04B0, twenty minutes
    assert st["lightSwitch"] == 1
    assert st["brightness"] == 3           # high
    assert st["noDisturbingSwitch"] == 0


def test_a_short_frame_is_dropped_rather_than_half_read():
    """A block with a known length that arrives short is a broken frame, and
    emitting the fields that happen to fit turns it into a confident reading.
    Both families now refuse; the W5 one used to be the permissive example."""
    import base64

    short = base64.b64encode(bytes(range(10))).decode()
    assert _ctw3(data=short) == {}


def test_the_older_ctw3_firmware_that_stops_at_26_bytes_is_still_read():
    """Every field the decoder names fits in 26. This demanded 30 and dropped
    the shorter block whole, which is the length aavdberg/ha-petkit accepts
    from real hardware."""
    import base64

    from petkit_local.devices.ble import CTW3_CONFIG_OFFSET

    full = base64.b64decode(CTW3_CMD230)
    short = base64.b64encode(full[:26]).decode()
    st = _ctw3(data=short)["states"]
    assert st["powerStatus"] == 1
    assert st["moduleStatus"] == 0
    # No config tail at that length, so none is claimed.
    assert "lightSwitch" not in st
    assert len(full) >= CTW3_CONFIG_OFFSET

    assert _ctw3(data=base64.b64encode(full[:25]).decode()) == {}


def test_a_transient_mode_of_zero_is_not_taken_as_the_mode():
    """A CTW3 reports 0 in the sleep half of its smart cycle. Stored, it turns
    the next touch of the power switch into cmd 220 with mode 0 — off, in no
    mode — because that frame is rebuilt from the last reading."""
    import base64

    full = bytearray(base64.b64decode(CTW3_CMD230))
    full[2] = 0
    st = _ctw3(data=base64.b64encode(bytes(full)).decode())["states"]
    assert "mode" not in st

    full[2] = 2
    assert _ctw3(data=base64.b64encode(bytes(full)).decode())["states"]["mode"] == 2


def test_a_non_status_cmd_is_ignored():
    """220/221 ACKs and 251/252 share the channel with the status frames."""
    import base64

    ack = base64.b64encode(bytes([1])).decode()
    assert _ctw3(data=ack, cmd=220) == {}


def test_the_ctw3_entities_read_what_the_decoder_writes():
    """The failure this prevents is silent: an entity whose `value_path` names
    a section the parser never fills reads unknown forever.

    A button is exempt and has to be: it names no path because it is an action,
    not a reading.
    """
    from petkit_local.devices.ble import get_ble_entities

    fragment = _ctw3()
    for entity in get_ble_entities("ctw3"):
        if entity.component == "button":
            assert not entity.value_path, entity.key
            continue
        section, _, field = entity.value_path.partition(".")
        assert section in fragment, entity.key
        assert field in fragment[section], entity.key


# --- getting the accessory to report at all ---------------------------------

class _FakeClient:
    """Records what the bridge publishes, in order."""

    def __init__(self):
        self.sent = []

    async def publish(self, topic, payload):
        self.sent.append((topic, payload))


def _bridge_with(parent_type, ble_type="ctw3", interval=0):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=10, device_type=parent_type, serial_number="SN10")
    ble = BLERegistry()
    ble.register(ble_type=ble_type, petkit_id=700, mac="AABBCCDDEEFF",
                 secret="s", interval=interval, link_with=10)
    bridge = MQTTBridge(reg, None, ble)
    bridge._client = _FakeClient()
    return bridge, reg, ble


async def test_an_accessory_paired_to_a_feeder_still_gets_polled():
    """The whole of issue #4's silence, in one assertion.

    An accessory reports only when we push `thing/service/connect` to its
    parent, and the only thing that used to push one was the handler for the
    parent's `property/post`. A feeder never sends that topic — so a fountain
    relayed by a D4SH was polled zero times, for ever, while looking perfectly
    paired in the panel.
    """
    bridge, reg, _ = _bridge_with("d4sh")
    await bridge._poll_ble_accessories(reg.get(10))

    sent = bridge._client.sent
    assert len(sent) == 1, "the parent was never asked to open a session"
    topic, payload = sent[0]
    assert topic.endswith("/thing/service/connect")
    assert json.loads(payload)["params"]["connect_action"] == 1


async def test_the_poll_carries_the_scan_type_the_accessory_was_paired_with():
    bridge, reg, _ = _bridge_with("d4sh")
    await bridge._poll_ble_accessories(reg.get(10))
    params = json.loads(bridge._client.sent[0][1])["params"]
    assert params["device"] == {"type": 24, "mac": "aabbccddeeff"}


async def test_a_k3_is_never_asked_to_open_a_relay_session():
    """It is not in the relay list, so a `connect` for it named `type: 0` —
    a scan for a device the parent has never been told about."""
    bridge, reg, _ = _bridge_with("t5", ble_type="k3")
    await bridge._poll_ble_accessories(reg.get(10))
    assert bridge._client.sent == []


async def test_the_interval_still_throttles():
    bridge, reg, _ = _bridge_with("d4sh", interval=240)
    await bridge._poll_ble_accessories(reg.get(10))
    await bridge._poll_ble_accessories(reg.get(10))
    assert len(bridge._client.sent) == 1


async def test_the_session_is_closed_once_the_reading_is_in():
    """Left open, the parent holds its radio on the accessory until something
    else happens to end it."""
    bridge, reg, ble = _bridge_with("d4sh")
    await bridge._handle_ble_response(reg.get(10), {
        "content": json.dumps({"device": {"mac": "AABBCCDDEEFF"},
                               "payload": [{"cmd": 230, "data": CTW3_CMD230}]}),
    })
    assert ble.get(700).state["states"]["runStatus"] == 1
    assert json.loads(bridge._client.sent[-1][1])["params"]["connect_action"] == 0


# --- writing to a CTW3 ------------------------------------------------------
#
# The other direction of the same relay, and the first write path any accessory
# has had. Frame layout from issue #4; every byte below is asserted rather than
# described, because a frame the accessory does not understand is answered with
# silence and looks exactly like one that never arrived.

def _paired_ctw3(**state):
    from petkit_local.devices.ble import BLEDevice
    dev = BLEDevice(ble_type="ctw3", petkit_id=700, mac="AABBCCDDEEFF", link_with=10)
    dev.state = {"states": state}
    return dev


def _decode_frame(encoded):
    import base64
    import urllib.parse
    return base64.b64decode(urllib.parse.unquote(encoded))


def test_an_outbound_frame_has_the_shape_the_accessory_answers_with():
    """The length is 16-bit LITTLE-endian, and this used to write one byte.

    Everything after it was therefore shifted by one: the accessory read our
    payload's first byte as the high half of the length, so a 12-byte config
    write announced 0x030C = 780 bytes and was still being waited for when the
    session closed. Silence at both ends, which is why no settings write to a
    CTW3 has ever been seen to take.
    """
    from petkit_local.devices.ble import build_ble_frame

    raw = _decode_frame(build_ble_frame(220, 7, bytes([1, 1, 2])))
    assert raw[:3] == bytes([0xFA, 0xFC, 0xFD])
    assert raw[3] == 220           # cmd, and the opcode: the same number
    assert raw[4] == 0x01          # type: request
    assert raw[5] == 7             # sequence
    assert raw[6:8] == bytes([3, 0])   # length, little-endian
    assert raw[8:11] == bytes([1, 1, 2])
    assert raw[-1] == 0xFB


def test_the_frame_builder_and_the_frame_reader_agree():
    """They never did. `_ble_unframe` has always read the payload from offset
    8 while the builder put it at 7, so this module encoded and decoded to two
    different specifications."""
    from petkit_local.devices.ble import _ble_unframe, build_ble_frame

    payload = bytes(range(20))
    cmd, data = _ble_unframe(_decode_frame(build_ble_frame(221, 3, payload)))
    assert cmd == 221
    assert data == payload


def test_a_command_we_have_no_frame_for_is_not_invented():
    from petkit_local.devices.ble import ble_command_frame

    assert ble_command_frame(999, 0, b"") is None
    assert ble_command_frame(221, 0, b"\x01") is not None


def test_setting_the_mode_restates_power_and_suspend():
    """cmd 220 carries all three. Sending it with only the field that changed
    would switch the fountain off as a side effect of changing its mode."""
    from petkit_local.devices.ble import CMD_SET_MODE, ble_command_for

    dev = _paired_ctw3(powerStatus=1, suspendStatus=1, mode=1)
    cmd, payload = ble_command_for(dev, "ctw3_mode", 2)
    assert cmd == CMD_SET_MODE
    assert payload == bytes([1, 1, 2])


def test_picking_a_mode_means_running_in_it():
    """Reading power back out of the last status sends `power=0` whenever the
    fountain was caught in the sleep half of its smart cycle, which leaves the
    pump off and makes the mode select look like it did nothing."""
    from petkit_local.devices.ble import ble_command_for

    asleep = _paired_ctw3(powerStatus=0, suspendStatus=0, mode=2)
    assert ble_command_for(asleep, "ctw3_mode", 1)[1] == bytes([1, 1, 1])
    assert ble_command_for(asleep, "ctw3_mode", 2)[1] == bytes([1, 1, 2])


def test_switching_a_ctw3_off_leaves_nothing_suspended():
    from petkit_local.devices.ble import ble_command_for

    dev = _paired_ctw3(powerStatus=1, suspendStatus=1, mode=1)
    assert ble_command_for(dev, "ctw3_power", 0)[1] == bytes([0, 0, 1])


def test_setting_the_brightness_restates_the_whole_config_block():
    """Twelve bytes, in the order PetKit's own app writes them
    (`CTW3DataConvertor.changeSmartMode`, app 13.8.1), which is the same order
    the status tail is read in — so the two really are one layout, as the real
    cmd-230 frame from issue #4 already implied.

    1.6.0 sent ten bytes with 6-8 reordered, following aavdberg/ha-petkit's
    capture. The app is the other end of that conversation and needs no
    interpreting; this is the assertion that would have caught it.
    """
    from petkit_local.devices.ble import CMD_SET_CONFIG, ble_command_for

    dev = _paired_ctw3(smartWorkingTime=3, smartSleepTime=3, energyInterval=300,
                       sleepTime=1200, lightSwitch=1, brightness=3,
                       noDisturbingSwitch=0, childLock=0,
                       smartInductiveSwitch=1, batteryInductiveSwitch=0)
    cmd, payload = ble_command_for(dev, "ctw3_brightness", 1)
    assert cmd == CMD_SET_CONFIG
    assert len(payload) == 12
    assert payload[0:2] == bytes([3, 3])                # smart cycle, restated
    assert payload[2:4] == (300).to_bytes(2, "big")     # 012C, unchanged
    assert payload[4:6] == (1200).to_bytes(2, "big")    # 04B0, unchanged
    assert payload[6] == 1                              # light still on
    assert payload[7] == 1                              # the one field asked for
    assert payload[8] == 0                              # do not disturb, off
    assert payload[9] == 0                              # child lock
    assert payload[10] == 1                             # smart inductive
    assert payload[11] == 0                             # battery inductive


def test_a_config_write_is_refused_before_the_first_full_status():
    """The block goes whole. Filling the unknown half with zeros would turn the
    light off and reset both intervals as a side effect of one change.

    The mode select is the exception and has to be: it states all three of
    cmd 220's fields itself, so it needs nothing read back.
    """
    from petkit_local.devices.ble import Refused, ble_command_for

    with pytest.raises(Refused):
        ble_command_for(_paired_ctw3(), "ctw3_brightness", 2)
    with pytest.raises(Refused):
        ble_command_for(_paired_ctw3(powerStatus=1), "ctw3_power", 0)
    assert ble_command_for(_paired_ctw3(), "ctw3_mode", 2)[1] == bytes([1, 1, 2])


def test_a_key_that_is_not_writable_is_refused_not_guessed():
    from petkit_local.devices.ble import Refused, ble_command_for

    with pytest.raises(Refused):
        ble_command_for(_paired_ctw3(), "ctw3_battery", 50)


def test_a_ctw3_key_is_refused_on_a_w5_and_the_other_way_round():
    """One dispatcher serves both families now, and a key that belongs to the
    other one has to be refused rather than half-recognised."""
    from petkit_local.devices.ble import BLEDevice, Refused, ble_command_for

    w5 = BLEDevice(ble_type="w5", petkit_id=701, mac="AABBCCDDEEFF")
    w5.state = {"states": {"powerStatus": 1, "mode": 1}}
    with pytest.raises(Refused):
        ble_command_for(w5, "ctw3_power", 1)
    with pytest.raises(Refused):
        ble_command_for(_paired_ctw3(powerStatus=1), "w5_power", 1)


def test_the_filter_reset_needs_no_reading_at_all():
    """It carries no payload built from state, so it is the one write that
    works on an accessory that has never reported."""
    from petkit_local.devices.ble import CMD_RESET_FILTER, ble_command_for

    assert ble_command_for(_paired_ctw3(), "ctw3_reset_filter", 0)[0] == CMD_RESET_FILTER


def test_every_writable_entity_has_a_frame_behind_it():
    """A switch HA can toggle but nothing can send is worse than no switch."""
    from petkit_local.devices.ble import (
        CTW3_WRITABLE, W5_WRITABLE, get_ble_entities,
    )

    assert {e.key for e in get_ble_entities("ctw3") if e.is_settable} == CTW3_WRITABLE
    assert {e.key for e in get_ble_entities("w5") if e.is_settable} == W5_WRITABLE


# --- the W5 family's own settings and writes --------------------------------
#
# Layout from mr-ransel's protocol notes, matching aavdberg/ha-petkit's parser
# and builder field for field. Nobody here owns a W4, W5 or CTW2, so every byte
# below is asserted against those two sources rather than against a capture —
# which is exactly why it is asserted rather than described.

def _paired_w5(**state):
    from petkit_local.devices.ble import BLEDevice

    dev = BLEDevice(ble_type="w5", petkit_id=701, mac="AABBCCDDEEFF", link_with=10)
    dev.state = {"states": state}
    return dev


#: One settings block: smart 5/10, ring on at brightness 7 from 08:00 to 22:00,
#: quiet from 23:00 to 07:00, child lock on.
W5_CONFIG = bytes([5, 10, 1, 7, 0x01, 0xE0, 0x05, 0x28, 1,
                   0x05, 0x64, 0x01, 0xA4, 1])


def test_a_w5_settings_frame_decodes_field_for_field():
    """cmd 211 was ignored entirely, so everything a settings write has to
    restate — both schedules included — had nowhere to come from."""
    import base64

    from petkit_local.devices.ble import parse_w5_ble_response

    st = parse_w5_ble_response({
        "device": {"mac": "aabbccddeeff"},
        "payload": [{"cmd": 211, "data": base64.b64encode(W5_CONFIG).decode()}],
    })["states"]
    assert st["smartWorkingTime"] == 5
    assert st["smartSleepTime"] == 10
    assert st["lampRingSwitch"] == 1
    assert st["lampRingBrightness"] == 7
    assert st["lampRingLightUpTime"] == 480      # 08:00
    assert st["lampRingGoOutTime"] == 1320       # 22:00
    assert st["noDisturbingSwitch"] == 1
    assert st["noDisturbingStartTime"] == 1380   # 23:00
    assert st["noDisturbingEndTime"] == 420      # 07:00
    assert st["isLock"] == 1


def test_a_w5_settings_write_restates_the_schedules_it_did_not_change():
    """The block goes whole and carries both schedules as minutes from
    midnight. Rebuilding it from anything but the last reading would erase
    them, and nothing else in the relayed traffic carries them."""
    from petkit_local.devices.ble import (
        CMD_SET_CONFIG, _decode_w5_config, ble_command_for,
    )

    dev = _paired_w5(**_decode_w5_config(W5_CONFIG))
    cmd, payload = ble_command_for(dev, "w5_brightness", 3)
    assert cmd == CMD_SET_CONFIG
    assert len(payload) == 14
    assert payload[3] == 3                       # the one field asked for
    assert payload == W5_CONFIG[:3] + bytes([3]) + W5_CONFIG[4:]


def test_a_w5_settings_write_is_refused_until_a_settings_frame_arrives():
    """A status frame carries the smart-cycle times but neither schedule, so
    reporting is not the same as being writable here."""
    from petkit_local.devices.ble import Refused, ble_command_for

    with pytest.raises(Refused):
        ble_command_for(_paired_w5(smartWorkingTime=5, smartSleepTime=10), "w5_light", 1)


def test_a_w5_power_write_names_the_mode_because_one_byte_carries_both():
    """cmd 220 byte 0 is 0 off, 1 normal, 2 smart — there is no separate power
    field, so switching on means naming a mode."""
    from petkit_local.devices.ble import CMD_SET_MODE, ble_command_for

    smart = _paired_w5(mode=2)
    assert ble_command_for(smart, "w5_power", 1) == (CMD_SET_MODE, bytes([2, 0]))
    assert ble_command_for(smart, "w5_power", 0) == (CMD_SET_MODE, bytes([0, 0]))
    assert ble_command_for(smart, "w5_mode", 1) == (CMD_SET_MODE, bytes([1, 0]))
    # Never reported: normal, rather than a fountain that will not switch on.
    assert ble_command_for(_paired_w5(), "w5_power", 1) == (CMD_SET_MODE, bytes([1, 0]))


def test_the_w5_filter_reset_is_the_same_command_on_both_families():
    from petkit_local.devices.ble import CMD_RESET_FILTER, ble_command_for

    assert ble_command_for(_paired_w5(), "w5_reset_filter", 0)[0] == CMD_RESET_FILTER


async def test_a_command_reaches_the_parent_as_a_ble_service_call():
    bridge, reg, ble = _bridge_with("d4sh")
    dev = ble.get(700)
    dev.state = {"states": {"powerStatus": 1, "suspendStatus": 1, "mode": 1}}

    from petkit_local.devices.ble import ble_command_for
    cmd, payload = ble_command_for(dev, "ctw3_power", 0)
    assert await bridge.publish_ble_command(reg.get(10), dev, cmd, payload)

    topic, body = bridge._client.sent[-1]
    assert topic.endswith("/thing/service/ble")
    params = json.loads(body)["params"]
    assert params["device"] == {"type": 24, "mac": "aabbccddeeff"}
    assert params["payload"]["cmd"] == 220
    assert _decode_frame(params["payload"]["data"])[8:11] == bytes([0, 0, 1])


async def test_the_sequence_number_advances_per_accessory():
    bridge, reg, ble = _bridge_with("d4sh")
    dev = ble.get(700)
    for _ in range(3):
        await bridge.publish_ble_command(reg.get(10), dev, 220, bytes([0, 1, 1, 1]))
    seqs = [_decode_frame(json.loads(b)["params"]["payload"]["data"])[5]
            for _, b in bridge._client.sent]
    assert seqs == [0, 1, 2]


async def test_an_accessory_remembers_when_it_last_spoke():
    """`BLEDevice` had no timestamp of any kind, so there was no way to tell
    one that has never reported from one reporting every four minutes — which
    is the first question to ask when its scan type is a guess."""
    bridge, reg, ble = _bridge_with("d4sh")
    assert ble.get(700).last_seen == 0.0

    await bridge._handle_ble_response(reg.get(10), {
        "content": json.dumps({"device": {"mac": "AABBCCDDEEFF"},
                               "payload": [{"cmd": 230, "data": CTW3_CMD230}]}),
    })
    stamped = ble.get(700).last_seen
    assert stamped > 0
    # It has to survive a restart: an accessory reports only when polled, so a
    # freshly loaded registry would otherwise read "never" for minutes.
    from petkit_local.devices.ble import BLEDevice
    assert BLEDevice.from_dict(ble.get(700).to_dict()).last_seen == stamped


def test_the_refused_a_write_raises_is_the_one_the_panel_catches():
    """Two classes of one name reaching the same `except` is a trap, and the
    panel imports one of them from `ha.commands` while the accessory raises
    the other."""
    from petkit_local.devices.base import Refused as Base
    from petkit_local.devices.ble import Refused as Ble
    from petkit_local.ha.commands import Refused as Ha

    assert Ble is Ha is Base


# --- coverage against the protocol map in issue #4 --------------------------

def test_the_mac_goes_out_in_the_shape_the_cloud_uses():
    """Uppercase is our canonical form for comparing one; every captured cloud
    frame carries lowercase. Nothing says the parent compares case-sensitively,
    but nothing says it does not, and a MAC it will not match is a pairing that
    fails with no symptom."""
    from petkit_local.devices.ble import BLEDevice

    dev = BLEDevice(ble_type="ctw3", petkit_id=1, mac="A4C138AABBCC")
    assert dev.mac == "A4C138AABBCC"          # stored, for matching
    assert dev.wire_mac == "a4c138aabbcc"     # sent
    assert dev.to_ble_list_entry()["mac"] == "a4c138aabbcc"


async def test_relay_plumbing_stays_out_of_the_timeline():
    """`ble_relay_start` and `ble_relay_over` bracket every reading. With a
    poll timer that is two rows per accessory per interval, for ever, in a
    Timeline that is supposed to be about the animal.

    `codes.MQTT_TRANSPORT_TOPICS` names all six and asks the bridge to read it;
    the bridge had drifted to a hardcoded three."""
    from petkit_local.events import codes

    assert {"ble_relay_start", "ble_relay_over", "property_post"} <= codes.MQTT_TRANSPORT_TOPICS
    src = (Path(__file__).resolve().parents[1]
           / "petkit_local" / "mqtt" / "bridge.py").read_text()
    assert "event_type not in codes.MQTT_TRANSPORT_TOPICS" in src
    assert 'event_type not in ("property", "data_get", "ble_response")' not in src


async def test_a_write_acknowledgement_is_named_rather_than_dropped(caplog):
    """A write comes back as a bare `01` on its own cmd. Silently ignoring it
    left "the switch flipped back" as the only symptom of a frame the
    accessory did not accept."""
    import base64
    import logging

    bridge, reg, ble = _bridge_with("d4sh")
    ack = base64.b64encode(bytes([1])).decode()
    with caplog.at_level(logging.INFO):
        await bridge._handle_ble_response(reg.get(10), {
            "content": json.dumps({"device": {"mac": "AABBCCDDEEFF"},
                                   "payload": [{"cmd": 220, "data": ack}]}),
        })
    assert "cmd 220" in caplog.text
