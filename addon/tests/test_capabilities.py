import json

from petkit_local.devices.base import Device


def test_non_camera_device_has_no_capabilities():
    d = Device(device_type="t3", petkit_id=1, serial_number="SN")  # ESP32 litter, no camera
    assert d.enabled_capabilities() == set()


def test_camera_device_defaults_all_capabilities_on():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert d.enabled_capabilities() == set(Device.CAPABILITY_TYPES)


def test_capability_toggle_off_is_respected():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config["capabilities"] = {"fullVideo": False}
    enabled = d.enabled_capabilities()
    assert "fullVideo" not in enabled
    assert "eventImage" in enabled  # unset keys still default on


def test_oss_sts_omits_disabled_capability():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config["capabilities"] = {"highLight": False}
    sts = d.to_oss_sts("https://192.0.2.20:9000")
    types = {c["cycleType"] for c in sts["result"]["capability"]}
    assert "highLight" not in types
    assert types == {"fullVideo", "eventImage", "dynamicVideo"}


def test_oss_sts_pathprefix_is_per_capability():
    d = Device(device_type="t5", petkit_id=42, serial_number="SN")
    sts = d.to_oss_sts("https://192.0.2.20:9000")
    for c in sts["result"]["capability"]:
        assert c["pathPrefix"] == f"t5/42/{c['cycleType']}"


def test_oss_sts_names_nowhere_rather_than_somewhere_unreachable():
    """It used to fall back to `https://localhost:9000`, and a user running
    docker-compose got that in every upload URL — an address that, resolved on
    the device, is the device. Naming nowhere is the honest answer: the device
    cannot tell an unreachable address from a working one until it has tried,
    and then it keeps trying."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    sts = d.to_oss_sts("")
    assert sts["result"]["capability"] == []
    assert "localhost" not in json.dumps(sts)


def test_oss_sts_empty_when_all_capabilities_disabled():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config["capabilities"] = {ct: False for ct in Device.CAPABILITY_TYPES}
    sts = d.to_oss_sts("https://192.0.2.20:9000")
    assert sts["result"]["capability"] == []


def test_device_info_capacity_mirrors_enabled_capabilities():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config["capabilities"] = {"dynamicVideo": False}
    info = d.to_device_info()
    names = {c["name"] for c in info["result"]["capacity"]}
    assert "dynamicVideo" not in names
    assert names == {"fullVideo", "eventImage", "highLight"}


def test_feeder_and_fountain_cameras_get_cvr_capacity():
    """CVR (`cloud_cvr_start`) is gated on capacity[].indate for fullVideo.
    Only litter cameras used to receive this block — D4SH/W7H got none, so
    recording never armed. Same far-future indate STS already uses."""
    far = 4102444800
    for codename in ("d4sh", "w7h", "d4h"):
        d = Device(device_type=codename, petkit_id=1, serial_number="SN")
        info = d.to_device_info()["result"]
        assert "capacity" in info, codename
        by_name = {c["name"]: c for c in info["capacity"]}
        assert by_name["fullVideo"]["indate"] == far
        assert info["cloudProduct"]["workIndate"] == far
        # Litter-only tips stay off camera feeders/fountains.
        assert "sprayDays" not in info


def test_non_camera_still_has_no_capacity():
    d = Device(device_type="t3", petkit_id=1, serial_number="SN")
    assert "capacity" not in d.to_device_info()["result"]


def test_supports_ai_seeds_from_the_codename_list():
    assert Device(device_type="t5", petkit_id=1).supports_ai is True
    assert Device(device_type="t6", petkit_id=1).supports_ai is True
    # PetKit's own Pet Identification screen lists the EverSweet Ultra AI.
    assert Device(device_type="w7h", petkit_id=1).supports_ai is True
    assert Device(device_type="t3", petkit_id=1).supports_ai is False
    assert Device(device_type="k3", petkit_id=1).supports_ai is False
    # Feeders are deliberately unseeded: one codename covers a generation with
    # an NPU and one without, so only polling can tell them apart.
    assert Device(device_type="d4h", petkit_id=1).supports_ai is False
    assert Device(device_type="d4sh", petkit_id=1).supports_ai is False


def test_a_device_that_asks_for_recognition_data_becomes_ai_capable():
    """The gen-2 YumShare shares `d4sh` with a model that has no NPU, so the
    codename cannot answer this and the device has to."""
    d = Device(device_type="d4sh", petkit_id=1)
    assert d.supports_ai is False
    d.config["ai_observed"] = True
    assert d.supports_ai is True


def test_the_observed_flag_is_coerced_like_every_other_persisted_flag():
    """It round-trips through devices.json, where a stray "false" string would
    otherwise read as True."""
    d = Device(device_type="d4sh", petkit_id=1)
    d.config["ai_observed"] = "false"
    assert d.supports_ai is False
    d.config["ai_observed"] = "true"
    assert d.supports_ai is True
