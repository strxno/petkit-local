import asyncio
from unittest.mock import AsyncMock

from petkit_local.devices.base import Device
from petkit_local.devices.pending import KEY, completed, remember
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.commands import make_mqtt_property_set
from petkit_local.mqtt.bridge import MQTTBridge


def test_latest_edit_survives_restart_and_preserves_actions():
    d = Device(device_type="w7h", petkit_id=1)
    action = {"method": "thing.service.start", "params": {"action": 1}}
    d.command_queue = [make_mqtt_property_set({"toneMode": 1}), action]
    remember(d, {"toneMode": 0})
    assert d.command_queue == [action]
    restored = Device.from_dict(d.to_dict())
    assert restored.config[KEY] == {"toneMode": 0}
    completed(d, {"toneMode": 1})
    assert d.config[KEY] == {"toneMode": 0}
    completed(d, {"toneMode": 0})
    assert d.config[KEY] == {}
    assert d.command_queue == [action]


async def test_pending_settings_recover_without_replaying_actions():
    d = Device(device_type="w7h", petkit_id=1)
    remember(d, {"toneMode": 0})
    d.mqtt_connected = True
    action = {"method": "thing.service.start", "params": {"action": 1}}
    d.command_queue = [action, make_mqtt_property_set({"toneMode": 0})]
    reg = DeviceRegistry()
    reg.all = lambda: [d]
    bridge = MQTTBridge(reg)
    bridge.publish_to_device = AsyncMock()
    task = asyncio.create_task(bridge._recover_settings())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    bridge.publish_to_device.assert_awaited_once()
    assert d.config[KEY] == {}
    assert d.command_queue == [action]


async def test_failed_send_remains_pending():
    d = Device(device_type="w7h", petkit_id=1)
    remember(d, {"toneMode": 0})
    d.mqtt_connected = True
    reg = DeviceRegistry()
    reg.all = lambda: [d]
    bridge = MQTTBridge(reg)
    bridge.publish_to_device = AsyncMock(side_effect=RuntimeError("offline"))
    task = asyncio.create_task(bridge._recover_settings())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert d.config[KEY] == {"toneMode": 0}
