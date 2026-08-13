"""The Insights tab: per-pet visit history, aggregated client-side.

Thin on purpose — `events/metrics.py::build_insights` does the actual work,
the same split `api_timeline`/`events/sessions.py` has. This module only
resolves the query into a time window, fetches the rows, and answers.
"""
from __future__ import annotations

import time

from aiohttp import web

from petkit_local.events.metrics import build_insights
from petkit_local.utils.coerce import to_int
from petkit_local.utils.timeutil import local_day_bounds, local_offset_hours
from petkit_local.web.api._common import _pets_by_id

#: Window used when the request names neither end of the range.
DEFAULT_WINDOW_DAYS = 30
#: Refused past this regardless of what `from`/`to` ask for — the panel port
#: is unauthenticated when mapped onto the LAN (see `http/bucket.py`'s own
#: caution about the same thing), and a multi-year scan is not a request this
#: tab has any legitimate reason to make. Comfortably past the 180-day default
#: retention window, so a household that raised its own retention still gets
#: a full year before this clamp is what limits them.
MAX_WINDOW_DAYS = 400
MAX_WINDOW_SEC = MAX_WINDOW_DAYS * 86400


async def api_insights(request: web.Request) -> web.Response:
    """`GET /api/insights?from=&to=&device=`.

    `from`/`to` are `YYYY-MM-DD`, each cut at LOCAL midnight
    (`utils/timeutil.local_day_bounds`) the same way the Timeline cuts a day —
    an unparseable or absent one falls back rather than erroring, and the
    response echoes the window it actually used so the panel's date picker
    stays in agreement with the server. Answers
    `{start, end, tz_offset, retention_cutoff_ts, pets, visits}` — see
    `events/metrics.py::build_insights` for the row shapes.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)

    now = time.time()
    to_str = request.query.get("to", "")
    from_str = request.query.get("from", "")

    _, end_ts, _ = local_day_bounds(to_str) if to_str else local_day_bounds(now=now)
    if from_str:
        start_ts, _, _ = local_day_bounds(from_str)
    else:
        start_ts, _, _ = local_day_bounds(now=now - DEFAULT_WINDOW_DAYS * 86400)

    # A reversed or absurdly wide range is clamped rather than rejected — the
    # date inputs are two independent pickers with no cross-validation of
    # their own, so "to" before "from" is a click away, not an attack.
    if start_ts >= end_ts:
        start_ts = end_ts - 86400
    if end_ts - start_ts > MAX_WINDOW_SEC:
        start_ts = end_ts - MAX_WINDOW_SEC

    device = request.query.get("device")
    did = to_int(device, None) if device else None

    pets = list((await _pets_by_id(request)).values())
    visits = await store.visit_summaries(start_ts, end_ts, device_id=did)

    related = sorted({v["related_event"] for v in visits if v.get("related_event")})
    pet_in_starts = await store.pet_in_starts(related) if related else {}

    retention = request.app.get("retention_config")
    events_max_age = retention.max_age_sec("events") if retention is not None else None
    retention_cutoff_ts = now - events_max_age if events_max_age else None

    result = build_insights(
        pets, visits, pet_in_starts, start_ts=start_ts, end_ts=end_ts,
        tz_offset=local_offset_hours(start_ts) * 3600, retention_cutoff_ts=retention_cutoff_ts)
    return web.json_response(result)
