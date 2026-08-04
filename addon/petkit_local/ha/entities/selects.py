"""HA `select` entities — the enum device settings, per device category.

`options` are the labels HA shows and validates against; `option_values` are
the numbers the device actually uses, positionally paired with them. Give
`option_values` whenever the device enum is not a plain 0-based index
(sandType is 1/2/3, autoIntervalMin is a minute count) — the label/value
translation in both directions depends on it, see
`ha/discovery.py::_select_value_template` and `ha/commands.py::_select_value`.

`devices/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_SELECTS = [
    EntityDef(component="select", key="sand_type", name="Litter Type",
              value_path="settings.sandType", icon="mdi:grain",
              options=["bentonite", "tofu", "mixed"],
              option_values=[1, 2, 3]),
    EntityDef(component="select", key="cleaning_interval", name="Avoid Repeat Interval",
              value_path="settings.autoIntervalMin", icon="mdi:timer-sand",
              options=["disabled", "5min", "10min", "15min", "30min", "45min",
                       "1h", "1h15min", "1h30min", "1h45min", "2h"],
              option_values=[0, 5, 10, 15, 30, 45, 60, 75, 90, 105, 120]),
]

FEEDER_SELECTS = [
    # `surplusControl` is in real D4SH firmware (`ctrl`, `libbase.so`), so the
    # control is genuine — but it is NOT seeded in `Device.default_settings()`,
    # because `to_device_info` serves seeded settings back to the device and a
    # made-up value would change the owner's setting.
    #
    # A PAIR, decompile-confirmed (`docs/SETTINGS_SCHEMA.md` Part 2):
    # disabled=surplusControl:0; less/moderate/full=surplusControl:1 +
    # surplusStandard:1/2/3. `surplusControl` alone is binary and can never by
    # itself distinguish less/moderate/full — both the write AND the displayed
    # state need both fields, so this entity is special-cased by `key` in
    # `ha/discovery.py::_value_template` (read) and
    # `ha/commands.py::handle_ha_command` (write) rather than going through the
    # generic single-field select path every other entity here uses.
    EntityDef(component="select", key="surplus_level", name="Surplus Level",
              value_path="settings.surplusControl", icon="mdi:bowl-mix",
              options=["disabled", "less", "moderate", "full"]),
]

FOUNTAIN_SELECTS = [
    EntityDef(component="select", key="flow_mode", name="Flow Mode",
              value_path="settings.fountainMode", icon="mdi:water-pump",
              options=["do_not_flow", "continuous", "intermittent", "motion_activated"]),
]
