"""HA `number` entities — the numeric device settings, per device category.

Each one reads `settings.<field>` from the state document and writes the same
field back via `property.set`. `min_value`/`max_value`/`step` are HA-side
validation only: the device does its own clamping, so these bounds exist to
stop an obviously-wrong value from ever being sent, not to guarantee one.

`devices/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_NUMBERS = [
    EntityDef(component="number", key="cleaning_delay", name="Cleaning Delay",
              value_path="settings.stillTime", icon="mdi:timer-sand",
              unit="s", min_value=0, max_value=3600, step=60),
]

FEEDER_NUMBERS = [
]

#: Seeded by `Device.default_settings()` only inside its `is_camera` branch,
#: so on a non-camera model these render blank forever. Two sources agree
#: they belong to the camera hardware: the defaults table and the state
#: parsers (`_parse_litter_camera` is documented as "the ESP32 litter set
#: PLUS the camera, spray and package fields").
#:
#: Range 1-9, decompile-confirmed on D4SH/T6/W7H (`docs/SETTINGS_SCHEMA.md`
#: Part 2, `parse_compare_device_resinfo`) — not the 0-9 this used to carry.
#: 0 was never independently observed (508 captured replies all showed `1`)
#: and traced back to localkit's validator for the YumShare Solo, a
#: DIFFERENT, non-camera feeder whose own picker actually offers 1-9.
LITTER_CAMERA_NUMBERS = [
    EntityDef(component="number", key="volume", name="Volume",
              value_path="settings.volume", icon="mdi:volume-high",
              min_value=1, max_value=9, step=1),
]

FEEDER_CAMERA_NUMBERS = [
    EntityDef(component="number", key="volume", name="Volume",
              value_path="settings.volume", icon="mdi:volume-high",
              min_value=1, max_value=9, step=1),
]

FOUNTAIN_NUMBERS = [
    EntityDef(component="number", key="fountain_time", name="Fountain Time",
              value_path="settings.fountainTime", icon="mdi:clock-outline",
              unit="h", min_value=1, max_value=24, step=1),
    EntityDef(component="number", key="sleep_time", name="Sleep Time",
              value_path="settings.sleepTime", icon="mdi:sleep",
              unit="h", min_value=1, max_value=24, step=1),
]
