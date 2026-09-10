"""Weight validation on the pets API.

`Pet.weight` is stored in GRAMS (`web/api/pets.py::_coerce_pet_weight`), the
same unit as the device's own `content.pet_weight` — a value entered here has
to be directly comparable to a visit's observed weight (`ai/weight.py`), so a
value that is not a plausible weight must be refused rather than stored as
whatever `float()`/SQLite happen to make of it.
"""
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


async def _panel(pet_registry: PetRegistry, store: EventStore) -> TestClient:
    reg = DeviceRegistry()
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, None, EventHub(), cfg, store)
    app["pet_registry"] = pet_registry
    app["event_store"] = store
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_a_pet_can_be_created_with_a_weight_in_grams(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets", json={"name": "Milo", "weight": 4280})
        body = await r.json()
        assert r.status == 200
        assert body["pet"]["weight"] == 4280.0
    finally:
        await c.close()


async def test_a_pet_can_be_created_with_no_weight(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets", json={"name": "Milo"})
        body = await r.json()
        assert r.status == 200
        assert body["pet"]["weight"] is None
    finally:
        await c.close()


async def test_a_non_numeric_weight_is_refused_on_create(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets", json={"name": "Milo", "weight": "fat"})
        assert r.status == 400
        assert await pet_registry.all() == []
    finally:
        await c.close()


async def test_an_absurd_weight_is_refused(
        pet_registry: PetRegistry, event_store: EventStore):
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets", json={"name": "Milo", "weight": 400000})
        assert r.status == 400
        r2 = await c.post("/api/pets", json={"name": "Milo", "weight": -5})
        assert r2.status == 400
        r3 = await c.post("/api/pets", json={"name": "Milo", "weight": 0})
        assert r3.status == 400
        assert await pet_registry.all() == []
    finally:
        await c.close()


async def test_a_kg_looking_value_is_stored_verbatim_not_multiplied(
        pet_registry: PetRegistry, event_store: EventStore):
    """A small-but-plausible number is stored exactly as sent — the API has no
    business guessing whether the caller meant kilograms. The panel's own
    input does the kg->g conversion before this endpoint ever sees a value
    (pets.js); a value that looks like stray kg (4.2) is correctly refused
    below, since 4.2 g is not a plausible pet weight."""
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post("/api/pets", json={"name": "Milo", "weight": 4200})
        body = await r.json()
        assert r.status == 200
        assert body["pet"]["weight"] == 4200.0

        r2 = await c.post("/api/pets", json={"name": "Kesha", "weight": 4.2})
        assert r2.status == 400
    finally:
        await c.close()


async def test_weight_can_be_updated_and_cleared(
        pet_registry: PetRegistry, event_store: EventStore):
    pet = await pet_registry.create("Milo", weight=4280)
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post(f"/api/pets/{pet['id']}", json={"weight": 5100})
        body = await r.json()
        assert r.status == 200
        assert body["pet"]["weight"] == 5100.0

        r2 = await c.post(f"/api/pets/{pet['id']}", json={"weight": None})
        body2 = await r2.json()
        assert r2.status == 200
        assert body2["pet"]["weight"] is None
    finally:
        await c.close()


async def test_a_bad_weight_on_update_is_refused_and_leaves_the_old_value(
        pet_registry: PetRegistry, event_store: EventStore):
    pet = await pet_registry.create("Milo", weight=4280)
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post(f"/api/pets/{pet['id']}", json={"weight": "heavy"})
        assert r.status == 400
        assert (await pet_registry.get(pet["id"]))["weight"] == 4280.0
    finally:
        await c.close()


async def test_updating_only_name_leaves_weight_untouched(
        pet_registry: PetRegistry, event_store: EventStore):
    pet = await pet_registry.create("Milo", weight=4280)
    c = await _panel(pet_registry, event_store)
    try:
        r = await c.post(f"/api/pets/{pet['id']}", json={"name": "Milo II"})
        body = await r.json()
        assert r.status == 200
        assert body["pet"]["weight"] == 4280.0
    finally:
        await c.close()
