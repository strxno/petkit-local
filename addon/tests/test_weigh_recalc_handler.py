"""POST /recalc/weigh/{device_id} -- the optional on-device tool's push
endpoint. Registry + a real EventStore, no HTTP server needed."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.http.handlers.weigh_recalc import handle_weigh_recalc_push

PATH = "/recalc/weigh/{device_id}"


async def _client(registry: DeviceRegistry | None, store: EventStore | None) -> TestClient:
    app = web.Application()
    if registry is not None:
        app["registry"] = registry
    if store is not None:
        app["event_store"] = store
    app.router.add_post(PATH, handle_weigh_recalc_push)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _registry_with(petkit_id: int) -> DeviceRegistry:
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=petkit_id, device_type="t6")
    return reg


async def test_a_push_for_an_unknown_device_is_a_harmless_200(event_store: EventStore):
    client = await _client(_registry_with(30000005), event_store)
    try:
        r = await client.post("/recalc/weigh/99999999", data=json.dumps({
            "t_ms": 1000.0, "pre_g": 2800.0, "during_g": 6000.0, "post_g": 2810.0,
        }))
        assert r.status == 200
    finally:
        await client.close()


async def test_a_malformed_body_is_a_harmless_200(event_store: EventStore):
    client = await _client(_registry_with(30000005), event_store)
    try:
        r = await client.post("/recalc/weigh/30000005", data="not json")
        assert r.status == 200
    finally:
        await client.close()


async def test_a_matched_push_merges_recalc_fields_into_the_right_event(
        event_store: EventStore):
    eid = await event_store.upsert_event({
        "device_id": 30000005, "event_type": "10", "event_kind": "toilet_visit",
        "ts": 1000.0, "content_json": '{"pet_weight": 3418}'})

    client = await _client(_registry_with(30000005), event_store)
    try:
        r = await client.post("/recalc/weigh/30000005", data=json.dumps({
            "t_ms": 1005000.0, "pre_g": 2800.0, "during_g": 6221.4,
            "post_g": 2805.0, "during_confirmed": True, "post_confirmed": True,
        }))
        assert r.status == 200
    finally:
        await client.close()

    content = json.loads((await event_store.get_event(eid))["content_json"])
    assert content["pet_weight"] == 3418          # untouched
    assert content["recalc_pet_weight_g"] == pytest.approx(3421.4)
    assert content["recalc_shit_weight_g"] == pytest.approx(5.0)
    assert content["recalc_weight_confirmed"] is True
    assert content["recalc_waste_confirmed"] is True


async def test_a_push_with_nothing_in_the_match_window_leaves_no_trace(
        event_store: EventStore):
    eid = await event_store.upsert_event({
        "device_id": 30000005, "event_type": "10", "event_kind": "toilet_visit",
        "ts": 1000.0, "content_json": '{"pet_weight": 3418}'})

    client = await _client(_registry_with(30000005), event_store)
    try:
        r = await client.post("/recalc/weigh/30000005", data=json.dumps({
            "t_ms": 999000000.0, "pre_g": 2800.0, "during_g": 6221.4, "post_g": 2805.0,
        }))
        assert r.status == 200
    finally:
        await client.close()

    content = json.loads((await event_store.get_event(eid))["content_json"])
    assert "recalc_pet_weight_g" not in content
