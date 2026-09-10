"""POST /recalc/weigh/{device_id} — the optional on-device recalculation tool
pushes a finished visit here.

This is NOT a firmware endpoint: it exists for one specific, hand-built,
optional binary (not part of any official install) that a user may run
directly on a litter box to independently recompute a visit's SETTLED weight
from the same raw scale signal the box itself reads — see `ai/weigh_recalc.py`
for why that number is worth having. Most installs never call this at all.
`device_id` is the box's own `petkit_id` in the URL path rather than the
firmware's `X-Device` signing scheme: this isn't a signed-up device pretending
to be one, so there is nothing to verify beyond "is this a device we already
know" — the same trust level every other route on this LAN-only server
already extends to real device traffic.

Always answers 200: `never_fail_middleware` backstops this route like every
other one on this app, and there is no caller here that would react to
anything else — the on-device tool doesn't parse the response.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from petkit_local.ai.weigh_recalc import match_visit
from petkit_local.events import ingest
from petkit_local.utils.coerce import to_float

log = logging.getLogger(__name__)

#: Wider than `ai.weigh_recalc.MATCH_WINDOW_SEC` on both sides of the store
#: query so the query itself never clips a candidate the matcher would
#: otherwise have been allowed to consider.
QUERY_WINDOW_MARGIN_SEC = 600.0


async def handle_weigh_recalc_push(request: web.Request) -> web.Response:
    try:
        petkit_id = int(request.match_info["device_id"])
    except (KeyError, ValueError):
        return web.json_response({"result": "success"})

    registry = request.app.get("registry")
    device = registry.get(petkit_id) if registry is not None else None
    if device is None:
        return web.json_response({"result": "success"})

    try:
        body: dict[str, Any] = json.loads(await request.text())
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"result": "success"})

    t_ms = to_float(body.get("t_ms"), None)
    pre_g = to_float(body.get("pre_g"), None)
    during_g = to_float(body.get("during_g"), None)
    post_g = to_float(body.get("post_g"), None)
    if t_ms is None or pre_g is None or during_g is None or post_g is None:
        return web.json_response({"result": "success"})

    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"result": "success"})

    pushed_ts = t_ms / 1000.0
    recalc_pet_weight_g = during_g - pre_g

    candidates = await store.visit_summaries(
        start_ts=pushed_ts - QUERY_WINDOW_MARGIN_SEC,
        end_ts=pushed_ts + QUERY_WINDOW_MARGIN_SEC,
        device_id=petkit_id)
    scored = [
        {"id": c["id"], "ts": c["ts"], "pet_weight": ingest._weight_of(c)}
        for c in candidates
    ]
    event_id = match_visit(pushed_ts, recalc_pet_weight_g, scored)
    if event_id is None:
        log.debug("weigh_recalc: no matching visit for device %d push at %.0f (%.1fg)",
                  petkit_id, pushed_ts, recalc_pet_weight_g)
        return web.json_response({"result": "success"})

    await store.merge_event_content(
        event_id,
        recalc_pet_weight_g=recalc_pet_weight_g,
        recalc_shit_weight_g=post_g - pre_g,
        recalc_weight_confirmed=bool(body.get("during_confirmed")),
        recalc_waste_confirmed=bool(body.get("post_confirmed")),
    )
    return web.json_response({"result": "success"})
