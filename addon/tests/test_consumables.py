"""The N50/N60 replacement dates we record ourselves.

The N50 has no representation anywhere in the device protocol: PetKit keeps its
replacement date in their own account database, and resetting it from their app
sends the box nothing but a display poke. So "N50 Days Left" can only ever read
what WE remember, which is what these tests pin.

The N60 does have a device field (`sprayResetTime`), and the device wins there --
it is resettable from PetKit's app too. Our copy exists so the countdown
survives a restart and so `to_device_info` never echoes a zero over a live reset
date.
"""
import json
import time

from petkit_local.devices.base import Device
from petkit_local.devices.state_parsers import (CONSUMABLE_RECORD_KEY, DEODORANT_TOTAL_DAYS,
                                                SPRAY_TOTAL_DAYS, apply_consumable_state,
                                                normalize_property_params,
                                                record_consumable_reset)
from petkit_local.ha.commands import handle_ha_command
from petkit_local.devices.registry import get_entities_for_device


def _dev():
    return Device(device_type="t5", petkit_id=1, serial_number="SN")


def _entity(dev, key):
    return next(e for e in get_entities_for_device(dev) if e.key == key)


def test_pressing_reset_n50_records_the_date_and_fills_the_countdown():
    # The whole point: before the press there is no source for this sensor at
    # all, and no amount of waiting for the device would produce one.
    dev = _dev()
    assert "deodorantLeftDays" not in dev.state

    handle_ha_command(dev, _entity(dev, "reset_n50"), "PRESS")

    assert dev.state["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS
    assert dev.config[CONSUMABLE_RECORD_KEY]["n50"] > 0


def test_pressing_reset_n60_records_the_date_and_still_sends_the_real_command():
    # Unlike the N50, code 10 genuinely works on the box, so the record must be
    # an addition to the command rather than a replacement for it.
    dev = _dev()
    result = handle_ha_command(dev, _entity(dev, "reset_n60"), "PRESS")

    assert result is not None, "the device command must still be sent"
    suffix, envelope = result
    assert suffix == "start"
    assert envelope["params"] == {"start_action": 10}
    assert dev.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS
    assert dev.config[CONSUMABLE_RECORD_KEY]["n60"] > 0


def test_the_record_survives_a_restart_and_so_does_state_now():
    # `config` has always persisted. `state` now does too (devices/base.py::
    # Device.to_dict) — the firmware's own refetch timers are 8h
    # (net_dev_get_device_info/net_dev_state_report) and 24h
    # (net_dev_ble_device_list_get), confirmed from the main() decompile, so
    # "wait for the device's next contact" meant every HA entity going blank
    # for up to 8 hours after every add-on restart. `apply_consumable_state`
    # must still recompute the SAME correct value from `config` regardless —
    # config is the durable record; state is just what gets redisplayed.
    dev = _dev()
    record_consumable_reset(dev, "n50", time.time() - 10.5 * 86400)
    record_consumable_reset(dev, "n60", time.time() - 10.5 * 86400)

    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))
    # 10.5 days used, so 19.5/34.5 remain -> rounded up, a part-used day counts.
    assert restarted.state["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS - 10
    assert restarted.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 10

    apply_consumable_state(restarted)
    assert restarted.state["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS - 10
    assert restarted.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 10


def test_to_device_info_echoes_the_recorded_stamp_rather_than_zero():
    """The clobber this closes: `ctrl` has `set sprayResetTime (%d)`, so handing
    the box a 0 in the window after a restart would move the N60 countdown's
    origin to now ON THE DEVICE, costing the owner the rest of a cartridge's
    warning. PetKit's own reply carries the true value here."""
    dev = _dev()
    stamp = time.time() - 3 * 86400
    record_consumable_reset(dev, "n60", stamp)

    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))
    # state is now persisted; config still holds the authoritative record
    assert int(restarted.to_device_info()["result"]["sprayResetTime"]) == int(stamp)

    # A device we have never heard from still gets 0 -- there is nothing to
    # preserve, and PetKit sends the field rather than omitting it.
    assert _dev().to_device_info()["result"]["sprayResetTime"] == 0


def test_the_device_stamp_wins_over_ours_and_is_copied_into_the_record():
    # The N60 is resettable from PetKit's app, which moves the box's stamp
    # without telling us, so the device is authoritative. Copying it in is what
    # makes the countdown survive the next restart.
    dev = _dev()
    record_consumable_reset(dev, "n60", time.time() - 20 * 86400)   # ours: stale
    fresh = int(time.time() - 2 * 86400)                            # box: newer

    dev.state.update(normalize_property_params("t5", {"sprayResetTime": fresh}))
    apply_consumable_state(dev)

    assert dev.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 2  # exactly 2 days
    assert dev.config[CONSUMABLE_RECORD_KEY]["n60"] == fresh


def test_an_unknown_consumable_name_is_refused_rather_than_guessed():
    dev = _dev()
    assert record_consumable_reset(dev, "n99") is None
    assert not dev.config.get(CONSUMABLE_RECORD_KEY)


def test_every_state_refresh_site_recomputes_the_countdowns():
    """The recurring bug in this codebase is one transport getting a fix the
    other does not -- it has now happened three times on this exact pair of
    fields. Any module that refreshes state from a report must also recompute
    the consumables, or an N50 countdown silently vanishes on that path."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "petkit_local"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "normalize_property_params(" not in text:
            continue
        # The definition site itself, and modules that only re-export it.
        if path.name == "state_parsers.py":
            continue
        if "apply_consumable_state(" not in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "these refresh state but never recompute the consumable countdowns: "
        f"{offenders}"
    )


def test_the_countdowns_are_ready_before_the_device_says_anything():
    """The N50 has no device input that would ever refill it -- so reading
    the countdown must not depend on the device reporting first, or "N50
    Days Left" is unknown after every restart. State is now persisted, but
    the countdown still comes from config (which is the durable record)
    via apply_consumable_state in the document builders."""
    from petkit_local.web.panel import _state_doc

    dev = _dev()
    record_consumable_reset(dev, "n50", time.time() - 4 * 86400)
    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))

    doc = _state_doc(restarted)
    assert doc["state"]["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS - 4
