from petkit_local.devices.base import Device
import pytest

from petkit_local.devices.registry import get_entities_for_device
from petkit_local.ha.commands import (
    ALL_ACTIONS,
    _coerce_number,
    _coerce_switch,
    handle_ha_command,
)


def _settable_index(device):
    return {e.unique_id_suffix: e for e in get_entities_for_device(device) if e.is_settable}


def _litter():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", d.default_settings())
    return d, _settable_index(d)


def _feeder():
    d = Device(device_type="d4sh", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", d.default_settings())
    return d, _settable_index(d)


def test_switch_updates_settings_and_returns_mqtt():
    d, idx = _litter()
    res = handle_ha_command(d, idx["auto_work"], "OFF")
    assert res is not None
    suffix, payload = res
    assert suffix == "property/set"
    assert payload["params"] == {"autoWork": 0}
    assert d.config["settings"]["autoWork"] == 0

    handle_ha_command(d, idx["auto_work"], "ON")
    assert d.config["settings"]["autoWork"] == 1


def test_number_coercion():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["volume"], "7")
    assert payload["params"] == {"volume": 7}
    assert d.config["settings"]["volume"] == 7


def test_coerce_switch_returns_int_not_bool():
    # The value is JSON-encoded straight into a property.set params dict and
    # the device's settings fields are integers — `true` is not the same wire
    # value as `1`.
    for on in ("ON", "on", "TRUE", "true", "1", 1, True):
        assert _coerce_switch(on) == 1
        assert type(_coerce_switch(on)) is int
    for off in ("OFF", "off", "FALSE", "0", 0, False, "garbage", "", None):
        assert _coerce_switch(off) == 0
        assert type(_coerce_switch(off)) is int


def test_coerce_number_stays_polymorphic():
    # An integral value must come back as int so the device sees {"volume": 7},
    # never {"volume": 7.0}; a fractional one keeps its float.
    assert _coerce_number("21.0") == 21 and type(_coerce_number("21.0")) is int
    assert _coerce_number("21") == 21 and type(_coerce_number("21")) is int
    assert _coerce_number("21.5") == 21.5 and type(_coerce_number("21.5")) is float
    assert _coerce_number(" -3 ") == -3 and type(_coerce_number(" -3 ")) is int
    assert _coerce_number("2e3") == 2000 and type(_coerce_number("2e3")) is int


def test_coerce_number_rejects_non_finite_setpoints():
    # Behaviour change vs. the bare float() this used to call: inf/NaN are no
    # longer accepted. json.dumps renders them as bare Infinity/NaN, which is
    # invalid JSON that the device cannot read back, and no device setpoint is
    # non-finite. The caller drops the command instead.
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e400"):
        assert _coerce_number(bad) is None, bad


def test_coerce_number_rejects_underscore_digit_separators():
    assert _coerce_number("1_0") is None


def test_non_numeric_number_payload_is_dropped_not_written():
    d, idx = _litter()
    before = dict(d.config["settings"])
    for bad in ("nan", "not a number", ""):
        assert handle_ha_command(d, idx["volume"], bad) is None, bad
    assert d.config["settings"] == before


def test_number_maps_to_correct_field():
    d, idx = _litter()
    # HA key 'cleaning_delay' writes device field 'stillTime'
    _, payload = handle_ha_command(d, idx["cleaning_delay"], "120")
    assert payload["params"] == {"stillTime": 120}


def test_select_index_default():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["sand_type"], "tofu")
    assert payload["params"] == {"sandType": 2}  # option_values [1,2,3]


def test_select_explicit_values():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["cleaning_interval"], "1h")
    assert payload["params"] == {"autoIntervalMin": 60}


def test_surplus_level_writes_the_pair_not_just_control():
    """`surplusControl` alone is binary and can't carry a level by itself
    (docs/SETTINGS_SCHEMA.md Part 2) — the generic single-field select path
    used to send only `{"surplusControl": 1}` for every non-disabled choice,
    so less/moderate/full were indistinguishable on the wire."""
    d, idx = _feeder()
    _, payload = handle_ha_command(d, idx["surplus_level"], "moderate")
    assert payload["params"] == {"surplusControl": 1, "surplusStandard": 2}
    assert d.config["settings"]["surplusControl"] == 1
    assert d.config["settings"]["surplusStandard"] == 2

    _, payload = handle_ha_command(d, idx["surplus_level"], "disabled")
    assert payload["params"] == {"surplusControl": 0}
    assert d.config["settings"]["surplusControl"] == 0
    # Previous level stays — only meaningful while surplusControl is 1.
    assert d.config["settings"]["surplusStandard"] == 2


def test_surplus_level_rejects_an_unknown_label():
    d, idx = _feeder()
    assert handle_ha_command(d, idx["surplus_level"], "extreme") is None
    assert "surplusControl" not in d.config["settings"]


def test_button_returns_mqtt_service_envelope():
    d, idx = _litter()
    suffix, env = handle_ha_command(d, idx["cleaning_start"], "")
    assert suffix == "start"
    assert env["method"] == "thing.service.start"
    assert env["params"] == {"start_action": 0}


def test_litter_action_codes_match_reference():
    d, idx = _litter()
    expected = {
        "cleaning_start": ("start", "start_action", 0),
        "dump_litter": ("start", "start_action", 1),
        "deodorize": ("start", "start_action", 2),
        "maintenance_start": ("start", "start_action", 9),
        "maintenance_stop": ("end", "end_action", 9),
        "level_litter": ("start", "start_action", 4),
        "reset_n60": ("start", "start_action", 10),
    }
    for key, (suffix, akey, code) in expected.items():
        s, env = handle_ha_command(d, idx[key], "")
        assert s == suffix, key
        assert env["params"] == {akey: code}, key


def test_feed_uses_feed_realtime_topic():
    d = Device(device_type="d4h", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    suffix, env = handle_ha_command(d, idx["feed"], "")
    assert suffix == "feed_realtime"
    assert env["method"] == "thing.service.feed_realtime"
    assert env["params"]["amount"] == 10
    assert env["params"]["id"].startswith("r_")


def test_the_feed_id_carries_its_number_twice():
    """`r_20260802_882_882-1` and `r_20260802_4057_4057-1`, both captured off
    PetKit's cloud talking to a D4 (PR #10). This was written from localkit's
    `FeedRealtime`, which has the number once; two captures agreeing settles it
    against a reimplementation. Two independent random numbers would match by
    chance about once in eighty million times."""
    import re

    d = Device(device_type="d4h", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    for _ in range(5):
        _, env = handle_ha_command(d, idx["feed"], "")
        m = re.fullmatch(r"r_(\d{8})_(\d+)_(\d+)-1", env["params"]["id"])
        assert m, env["params"]["id"]
        assert m.group(2) == m.group(3)


def test_every_button_maps_to_an_action():
    # Coherence: every button entity across all device types must resolve to a
    # known action, else pressing it silently does nothing.
    for dtype in ("t5", "t4", "d4h", "d4", "w7h", "w5"):
        d = Device(device_type=dtype, petkit_id=99, serial_number="X")
        for e in get_entities_for_device(d):
            if e.component == "button":
                assert e.key in ALL_ACTIONS, f"{dtype}: button '{e.key}' has no action"


def test_capability_switch_writes_config_not_settings_and_pushes_nothing():
    d, idx = _litter()
    res = handle_ha_command(d, idx["capability_full_video"], "OFF")
    assert res is None  # no MQTT/heartbeat push — STS is the control point
    assert d.config["capabilities"]["fullVideo"] is False
    assert "fullVideo" not in d.config.get("settings", {})

    handle_ha_command(d, idx["capability_full_video"], "ON")
    assert d.config["capabilities"]["fullVideo"] is True


def test_every_settable_control_has_settings_path():
    # Coherence: every switch/number/select must write to settings.<field>,
    # EXCEPT capability toggles — those write to config["capabilities"] and
    # are never pushed to the device (the STS response is the control point,
    # see ha/commands.py::CAPABILITY_VALUE_PREFIX), so they're exempt.
    for dtype in ("t5", "t4", "d4h", "d4", "w7h", "w5", "k3"):
        d = Device(device_type=dtype, petkit_id=99, serial_number="X")
        for e in get_entities_for_device(d):
            if e.component in ("switch", "number", "select"):
                if e.value_path.startswith("capabilities."):
                    continue
                assert e.value_path.startswith("settings."), \
                    f"{dtype}: {e.component} '{e.key}' value_path={e.value_path!r}"
                assert e.setting_field, f"{dtype}: {e.key} has empty setting_field"


def test_a_command_id_stays_within_signed_int32():
    """`id` must stay < 2**31. Every id observed from the real cloud is signed-
    int32 (47214543..2144539517, never the 2**31..2**32-1 half), and a raw ms
    timestamp overflows it — so the envelope wraps into the signed range so a
    firmware signed-atoi never reads it negative."""
    from petkit_local.ha.commands import ALL_ACTIONS
    for name, make in ALL_ACTIONS.items():
        result = make()
        if result is None:
            continue
        _suffix, envelope = result
        if not isinstance(envelope, dict) or "id" not in envelope:
            continue
        assert envelope["id"].isdigit(), name
        assert int(envelope["id"]) < 2**31, f"{name} id out of signed-int32 range"


def test_a_number_outside_its_range_is_refused_not_clamped():
    """`min_value`/`max_value` bound Home Assistant's control and the panel's
    spinner; neither binds a raw API call. And the value does not merely render:
    it lands in `config["settings"]`, which `to_device_info` serves back to the
    device, so an out-of-range number would be pushed to hardware.

    Refused rather than clamped, because writing a number nobody asked for is
    the failure this project avoids everywhere else.
    """
    from petkit_local.devices.base import Device
    from petkit_local.devices.registry import get_entities_for_device

    dev = Device(device_type="t5", petkit_id=1, serial_number="SN")
    volume = next(e for e in get_entities_for_device(dev) if e.key == "volume")
    before = dict(dev.config.get("settings", {}))

    from petkit_local.ha.commands import Refused

    # `Refused`, not None: None already means "applied, nothing to send to the
    # device", so returning it here made the panel answer {"ok": true} to a
    # value it had just thrown away.
    for bad in (volume.max_value + 1, volume.min_value - 1):
        with pytest.raises(Refused):
            handle_ha_command(dev, volume, str(bad))
    assert dev.config.get("settings", {}) == before, "a refused write must change nothing"

    # The bounds themselves are valid, and a value inside them still works.
    assert handle_ha_command(dev, volume, str(volume.max_value)) is not None
    assert dev.config["settings"][volume.setting_field] == volume.max_value
