"""Insights tab aggregation — visit history turned into pet-attributed rows.

`web/api/insights.py` is the thin HTTP wrapper (`api/timeline.py` is its
sibling); this module does the actual work, the same split
`events/sessions.py` has with the Timeline. It stays free of I/O and of
`web/` — the caller fetches rows (`EventStore.visit_summaries`,
`EventStore.pet_in_starts`) and pet rows first, and hands them in, which is
what makes `build_insights` a plain function over plain data and testable
without a database.

`build_insights` returns the FLAT visit list rather than any day/hour
bucketing of its own: 180 days is a couple of thousand small objects, well
inside what a browser aggregates instantly, and doing it client-side means
toggling a pet or a chart never triggers a re-fetch. Bucketing is
`static/js/charts.js`'s job.
"""
from __future__ import annotations

from typing import Any

from petkit_local.ai import weight as weight_mod
from petkit_local.events import ingest


def _pet_photo_url(pet: dict[str, Any]) -> str | None:
    """First mugshot URL for a pet row carrying `faces` (see
    `web/api/_common.py::_pets_by_id`), or None. Duplicated rather than
    imported from `web/api/_common.py`: `events/` does not depend on `web/`
    (see ARCHITECTURE.md) and this is two lines."""
    faces = pet.get("faces") or []
    return faces[0]["url"] if faces else None


def _confirmed_weight_stats(pets: list[dict[str, Any]],
                            visits: list[dict[str, Any]]) -> dict[int, weight_mod.WeightStats]:
    """Per-pet face-confirmed weight stats, for EVERY pet, weight set or not.

    `ai/weight.py::build_profiles` only builds a profile for a pet whose
    `weight` is already set, because that is all attribution needs. The
    calibration view needs the opposite case too — "here's what we've
    observed for Milo even though nobody has typed a weight for him yet" is
    exactly the number that lets a user set one from evidence instead of a
    guess — so this scans independently rather than reading it off
    `WeightProfile`.
    """
    by_pet: dict[int, list[float]] = {}
    for visit in visits:
        pid = visit.get("pet_id")
        w = ingest._weight_of(visit)
        if pid is not None and w is not None:
            by_pet.setdefault(pid, []).append(w)
    stats: dict[int, weight_mod.WeightStats] = {}
    for pid, weights in by_pet.items():
        s = weight_mod.weight_stats(weights)
        if s is not None:
            stats[pid] = s
    return stats


def build_insights(pets: list[dict[str, Any]], visits: list[dict[str, Any]],
                   pet_in_starts: dict[str, float], *, start_ts: float, end_ts: float,
                   tz_offset: float, retention_cutoff_ts: float | None) -> dict[str, Any]:
    """Assemble the `/api/insights` response.

    Args:
        pets: Every pet row, `faces` already attached (`_pets_by_id` shape).
        visits: One row per toilet visit (`EventStore.visit_summaries`),
            oldest first.
        pet_in_starts: `{related_event: earliest pet_in ts}`, for durations on
            visits with no `time_in`/`time_out` of their own (MQTT `pet_out`).
        retention_cutoff_ts: Where event history actually stops (now minus the
            events retention window), or None if it is uncapped — echoed back
            so the UI can mark a range that runs past it as "history ends
            here" rather than "the pet stopped visiting".
    """
    # `ai/weight.py` reads an observed weight off a TOP-LEVEL `pet_weight` key
    # (its own docstring: "or any dict carrying pet_id and an observed weight
    # under weight_g/pet_weight/petWeight") — it knows nothing about
    # `content_json`, deliberately, since it has no I/O and no wire-format
    # knowledge of its own. A `visit_summaries` row carries the weight nested
    # inside `content_json`, so it is flattened here, once, rather than
    # teaching the pure attribution module about the store's row shape.
    flattened = [{**v, "pet_weight": ingest._weight_of(v)} for v in visits]
    profiles = weight_mod.build_profiles(pets, flattened)
    observed_stats = _confirmed_weight_stats(pets, visits)

    visit_rows = []
    for visit in visits:
        related = visit.get("related_event")
        pet_in_ts = pet_in_starts.get(related) if related else None
        pet_in = {"ts": pet_in_ts} if pet_in_ts is not None else None

        observed_weight = ingest._weight_of(visit)
        duration = ingest._duration_of(visit, pet_in)
        display_ts = ingest._started_at(visit, pet_in) or visit.get("ts")

        attribution = weight_mod.attribute_visit(
            {"pet_id": visit.get("pet_id"), "pet_weight": observed_weight},
            profiles, device_id=visit.get("device_id"))

        visit_rows.append({
            "ts": visit.get("ts"),
            "display_ts": display_ts,
            "device_id": visit.get("device_id"),
            "duration_sec": duration,
            "weight": observed_weight,
            # Face-confirmed identity. `attributed_pet_id` carries the SAME
            # pet when attribution's basis is "weight" and stays None here so
            # a card never shows two different-looking chips for one pet.
            "pet_id": visit.get("pet_id"),
            "attributed_pet_id": attribution.pet_id if attribution.basis == "weight" else None,
            "grade": attribution.grade,
        })

    pet_rows = []
    for pet in pets:
        stats = observed_stats.get(pet["id"])
        pet_rows.append({
            "id": pet["id"],
            "name": pet.get("name"),
            "photo_url": _pet_photo_url(pet),
            "weight": pet.get("weight"),
            "observed": None if stats is None else {
                "median": stats.median, "spread": stats.spread, "n_confirmed": stats.n,
            },
        })

    return {
        "start": start_ts, "end": end_ts, "tz_offset": tz_offset,
        "retention_cutoff_ts": retention_cutoff_ts,
        "pets": pet_rows,
        "visits": visit_rows,
    }
