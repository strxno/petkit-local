"""The Insights API: `/api/insights` window resolution and response shape.

`events/metrics.py::build_insights` owns the actual aggregation and has its
own tests; these cover the HTTP layer — query parsing, clamping, and wiring
the store/pet_registry/retention_config together.
"""
import time

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.media.retention import RetentionConfig
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


async def _panel(pet_registry: PetRegistry, store: EventStore, *,
                 retention_config: RetentionConfig | None = None) -> TestClient:
    reg = DeviceRegistry()
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, None, EventHub(), cfg, event_store=store,
                           pet_registry=pet_registry, retention_config=retention_config)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_no_event_store_answers_400():
    reg = DeviceRegistry()
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, None, EventHub(), cfg)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        r = await c.get("/api/insights")
        assert r.status == 400
    finally:
        await c.close()


async def test_default_window_is_the_last_30_days(pet_registry: PetRegistry,
                                                    event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights")
        body = await r.json()
        assert r.status == 200
        # 30 days back through the START of today PLUS all of today itself
        # (`local_day_bounds`'s end is midnight of the day AFTER "to") is a
        # 31-day span, not 30 — same off-by-one every local-day-bounds caller
        # in this repo has to account for.
        span_days = (body["end"] - body["start"]) / 86400
        assert 30.9 <= span_days <= 31.1
    finally:
        await c.close()


async def test_explicit_from_and_to_are_honoured(pet_registry: PetRegistry,
                                                  event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights?from=2026-01-01&to=2026-01-08")
        body = await r.json()
        # Inclusive of both named days: Jan 1st through the end of Jan 8th.
        span_days = round((body["end"] - body["start"]) / 86400)
        assert span_days == 8
    finally:
        await c.close()


async def test_a_reversed_range_is_clamped_not_rejected(pet_registry: PetRegistry,
                                                         event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights?from=2026-06-01&to=2026-01-01")
        assert r.status == 200
        body = await r.json()
        assert body["start"] < body["end"]
    finally:
        await c.close()


async def test_an_absurdly_wide_range_is_clamped_to_the_max_window(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights?from=2000-01-01&to=2026-01-01")
        body = await r.json()
        span_days = (body["end"] - body["start"]) / 86400
        assert span_days <= 401
    finally:
        await c.close()


async def test_retention_cutoff_is_reported_from_the_events_max_age(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store, retention_config=RetentionConfig())
    try:
        r = await c.get("/api/insights")
        body = await r.json()
        expected = time.time() - 180 * 86400
        assert abs(body["retention_cutoff_ts"] - expected) < 5
    finally:
        await c.close()


async def test_no_retention_config_means_no_cutoff(pet_registry: PetRegistry,
                                                    event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights")
        body = await r.json()
        assert body["retention_cutoff_ts"] is None
    finally:
        await c.close()


async def test_a_visit_and_its_pet_round_trip_through_the_endpoint(
        pet_registry: PetRegistry, event_store: EventStore):
    pet = await pet_registry.create("Milo", weight=3200.0)
    now = time.time()
    await event_store.upsert_event({
        "device_id": 1, "event_type": "10", "event_kind": "toilet_visit",
        "pet_id": pet["id"], "ts": now - 3600,
        "content_json": '{"pet_weight": 3210}',
    })
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights")
        body = await r.json()
        assert len(body["visits"]) == 1
        assert body["visits"][0]["pet_id"] == pet["id"]
        assert body["visits"][0]["grade"] == "confirmed"
        assert body["pets"][0]["name"] == "Milo"
    finally:
        await c.close()


async def test_device_filter_narrows_the_visit_list(pet_registry: PetRegistry,
                                                     event_store: EventStore):
    now = time.time()
    await event_store.upsert_event({"device_id": 1, "event_type": "10",
                                    "event_kind": "toilet_visit", "ts": now - 60})
    await event_store.upsert_event({"device_id": 2, "event_type": "10",
                                    "event_kind": "toilet_visit", "ts": now - 30})
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.get("/api/insights?device=1")
        body = await r.json()
        assert len(body["visits"]) == 1
        assert body["visits"][0]["device_id"] == 1
    finally:
        await c.close()
