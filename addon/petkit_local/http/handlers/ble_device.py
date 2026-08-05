"""dev_ble_device — the BLE accessories a WiFi device proxies for.

A K3 Pura Air spray or W5 fountain has no WiFi of its own; it is reached over BLE by a
mains-powered neighbour (a T4, say), which relays its data to us. This endpoint
is how that WiFi device learns which accessories it is supposed to talk to, so
an empty answer here means the device stops relaying.

K3 sprays are deliberately excluded: they are reported inside the parent's
`dev_device_info` payload instead (see devices/ble.py).
"""
from __future__ import annotations

from aiohttp import web

from petkit_local.http.handlers._common import no_device_response, request_device


async def handle_ble_device(request: web.Request) -> web.Response:
    """List the BLE accessories this device should scan for and relay.

    Returns:
        ``{"result": {"list": [...], "nextTick": 3600}}`` where each entry comes
        from `BLEDevice.to_ble_list_entry()`, or the standard empty result when
        the device is unknown or has no non-K3 accessories linked to it.
    """
    device = request_device(request)
    if not device:
        return no_device_response()

    ble_registry = request.app.get("ble_registry")
    ble_list = []

    if ble_registry:
        for ble_dev in ble_registry.non_k3_for_parent(device.petkit_id):
            ble_list.append(ble_dev.to_ble_list_entry())

    if not ble_list:
        return no_device_response()

    return web.json_response({
        "result": {
            "list": ble_list,
            "nextTick": 3600,
        }
    })
