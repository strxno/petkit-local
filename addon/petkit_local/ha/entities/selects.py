"""HA `select` entities — the enum device settings, per device category.

`options` are the labels HA shows and validates against; `option_values` are
the values the device actually uses, positionally paired with them. Give
`option_values` whenever the device enum is not a plain 0-based index
(sandType is 1/2/3, autoIntervalMin is a second count) — the label/value
translation in both directions depends on it, see
`ha/discovery.py::_select_value_template` and `ha/commands.py::_select_value`.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_SELECTS = [
    EntityDef(component="select", key="sand_type", name="Litter Type",
              value_path="settings.sandType", icon="mdi:grain",
              options=["bentonite", "tofu", "mixed"],
              option_values=[1, 2, 3]),
    # SECONDS, despite the field being named for minutes. PetKit's own app was
    # captured writing `autoIntervalMin` on a T6: 300 at the UI's minimum of
    # 5 min and 7200 at its maximum of 2 h, with 0 while the feature was being
    # switched off. Listing the minute counts the name implies has "5min" ask
    # the box for a five-SECOND interval.
    #
    # The capture is from ONE model. `autoIntervalMin` is also in the T4 image
    # (`parse item autoIntervalMin=%d,time=%d` in userbin_2602003.bin), but
    # nobody has read the unit there, so applying the T6 reading to the ESP32
    # boxes is a deliberate choice rather than an established fact: one field
    # name, one meaning, until something contradicts it.
    EntityDef(component="select", key="cleaning_interval", name="Avoid Repeat Interval",
              value_path="settings.autoIntervalMin", icon="mdi:timer-sand",
              options=["disabled", "5min", "10min", "15min", "30min", "45min",
                       "1h", "1h15min", "1h30min", "1h45min", "2h"],
              option_values=[0, 300, 600, 900, 1800, 2700, 3600, 4500, 5400,
                             6300, 7200]),
]

FEEDER_SELECTS = [
    # `surplusControl` is in real D4SH firmware (`ctrl`, `libbase.so`), so the
    # control is genuine — but it is NOT seeded in `defaults.default_settings()`,
    # because `to_device_info` serves seeded settings back to the device and a
    # made-up value would change the owner's setting.
    #
    # The encoding is NOT the paired {0/1} + {1-3} model the reference
    # integration assumes. Captured live from a D4SH tracking the app's own
    # writes (2026-08-08): Off=`{surplusControl:0}`, Less=`{30,1}`,
    # Moderate=`{60,2}`, Full=`{80,3}` — `surplusControl` alone carries both
    # on/off and level, `surplusStandard` mirrors the level and is left
    # untouched (not re-zeroed) when switching off. See the paired write in
    # `ha/commands.py::handle_ha_command`.
    EntityDef(component="select", key="surplus_level", name="Surplus Level",
              value_path="settings.surplusControl", icon="mdi:bowl-mix",
              options=["disabled", "less", "moderate", "full"],
              option_values=[0, 30, 60, 80]),
]

FOUNTAIN_SELECTS = [
    EntityDef(component="select", key="flow_mode", name="Flow Mode",
              value_path="settings.fountainMode", icon="mdi:water-pump",
              options=["do_not_flow", "continuous", "intermittent", "motion_activated"]),
]

#: The one select whose `option_values` are STRINGS rather than numbers.
#:
#: `language` is a set handler on the W7H (`codes.FOUNTAIN_W7H_SET_FIELDS`) and
#: the app's Voice Language screen writes it directly; both captured values are
#: locale codes, not indices. Only two were seen, and the rest of the list is
#: deliberately absent — a locale code is exactly the kind of thing that looks
#: guessable ("de_DE"?) and is not.
FOUNTAIN_W7H_SELECTS = [
    EntityDef(component="select", key="flow_mode", name="Flow Mode",
              value_path="settings.fountainMode", icon="mdi:water-pump",
              options=["do_not_flow", "continuous", "intermittent", "motion_activated"],
              option_values=[0, 1, 2, 3]),
    EntityDef(component="select", key="voice_language", name="Voice Language",
              value_path="settings.language", icon="mdi:translate",
              options=["English", "Chinese"],
              option_values=["en_US", "zh_CN"]),
]
