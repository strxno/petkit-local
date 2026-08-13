"""The Timeline's weight-inferred badge: `web/api/timeline.py`'s attribution
pass over visits the device's own face recognition left unattributed.

`ai/weight.py` has its own unit tests for the matching rule itself; these
cover the wiring — that a confirmed visit is never touched, that an
unconfirmed one gets `attributed_pet_id`/`attribution_grade` when a profile
claims it, and that the profile is trained from a WIDER window than the one
day being displayed.
"""
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.media.retention import RetentionConfig
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


def _midday() -> float:
    from datetime import datetime
    now = datetime.now()
    return now.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def _panel(tmp):
    reg = DeviceRegistry()
    device = reg.get_or_create(petkit_id=1, device_type="t6", serial_number="SN")
    store = EventStore(Path(tmp) / "petkit.db")
    pet_registry = PetRegistry(store, str(Path(tmp) / "faces"))
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "data_dir": tmp, "media_root": str(Path(tmp) / "media")}
    app = create_panel_app(reg, None, EventHub(), cfg, event_store=store,
                           retention_config=RetentionConfig(), pet_registry=pet_registry)
    return app, device, store, pet_registry


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def test_a_confirmed_visit_is_never_given_an_attributed_pet_id():
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        pet = await pet_registry.create("Milo", device_ids=[1], weight=3200.0)
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": _midday(), "related_event": "r1", "pet_id": pet["id"],
            "content_json": '{"pet_weight": 3220}',
        })
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            s = body["sessions"][0]
            assert s["pet_id"] == pet["id"]
            assert s["attributed_pet_id"] is None
            assert s["attribution_grade"] is None
        finally:
            await c.close()


async def test_an_unconfirmed_visit_gets_a_weight_inferred_badge():
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        pet = await pet_registry.create("Milo", device_ids=[1], weight=3200.0)
        now = _midday()
        # Five confirmed visits, spread across the last month, corroborate
        # Milo's entered weight -- ai/weight.py::MIN_CORROBORATING_SAMPLES.
        for i, w in enumerate((3190, 3210, 3200, 3205, 3195)):
            await store.upsert_event({
                "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
                "ts": now - (i + 1) * 86400, "related_event": f"r{i}", "pet_id": pet["id"],
                "content_json": f'{{"pet_weight": {w}}}',
            })
        # Today's visit: the camera saw nobody, but the box weighed it.
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": now, "related_event": "r_today",
            "content_json": '{"pet_weight": 3215}',
        })
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            assert len(body["sessions"]) == 1
            s = body["sessions"][0]
            assert s["pet_id"] is None
            assert s["attributed_pet_id"] == pet["id"]
            assert s["attributed_pet_name"] == "Milo"
            assert s["attribution_grade"] == "inferred"
        finally:
            await c.close()


async def test_an_out_of_range_visit_gets_no_badge():
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        await pet_registry.create("Milo", device_ids=[1], weight=3200.0)
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": _midday(), "related_event": "r1",
            "content_json": '{"pet_weight": 9000}',
        })
        c = await _client(app)
        try:
            s = (await (await c.get("/api/timeline")).json())["sessions"][0]
            assert s["attributed_pet_id"] is None
            assert s["attribution_grade"] is None
        finally:
            await c.close()


async def test_a_visit_with_no_weight_at_all_gets_no_badge():
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        await pet_registry.create("Milo", device_ids=[1], weight=3200.0)
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": _midday(), "related_event": "r1",
        })
        c = await _client(app)
        try:
            s = (await (await c.get("/api/timeline")).json())["sessions"][0]
            assert s["attributed_pet_id"] is None
        finally:
            await c.close()


async def test_no_pets_means_no_attribution_query_and_no_crash():
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": _midday(), "related_event": "r1",
            "content_json": '{"pet_weight": 3200}',
        })
        c = await _client(app)
        try:
            r = await c.get("/api/timeline")
            assert r.status == 200
            s = (await r.json())["sessions"][0]
            assert s["attributed_pet_id"] is None
        finally:
            await c.close()


async def test_profile_training_reaches_beyond_the_displayed_day():
    """The badge's whole point: a day with no OTHER visits of its own can
    still corroborate a pet's weight from visits on other days within
    PROFILE_WINDOW_DAYS — the training query is deliberately wider than the
    single day being rendered."""
    with tempfile.TemporaryDirectory() as tmp:
        app, device, store, pet_registry = _panel(tmp)
        pet = await pet_registry.create("Milo", device_ids=[1], weight=3200.0)
        now = _midday()
        for i, w in enumerate((3190, 3210, 3200, 3205, 3195)):
            await store.upsert_event({
                "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
                "ts": now - (i + 10) * 86400, "related_event": f"old{i}", "pet_id": pet["id"],
                "content_json": f'{{"pet_weight": {w}}}',
            })
        await store.upsert_event({
            "device_id": 1, "event_type": "pet_out", "event_kind": "toilet_visit",
            "ts": now, "related_event": "r_today",
            "content_json": '{"pet_weight": 3215}',
        })
        c = await _client(app)
        try:
            body = await (await c.get("/api/timeline")).json()
            # Only today's session is IN the response (the old ones are
            # outside the queried day) -- but it is still attributed.
            assert len(body["sessions"]) == 1
            assert body["sessions"][0]["attributed_pet_id"] == pet["id"]
        finally:
            await c.close()
