"""Matching a pushed on-device recalculation to the real visit it belongs to.

The optional on-device tool (see `http/handlers/weigh_recalc.py`) independently
re-derives a visit's SETTLED weight from the same raw scale signal the box
itself reads, and publishes it up to `POST_MAX_WAIT_SEC` (240s) after the real
departure — it waits for a genuine settle rather than a fixed timer, so the lag
varies visit to visit. `match_visit` finds which of the box's OWN reported
visits (`EventStore.visit_summaries`) that pushed reading actually belongs to.

Nearest-timestamp alone is not enough: verified by hand against a real
four-visits-in-three-minutes cluster (a cat checking the box right after
another one leaves), where the closest-in-time box event was the WRONG one --
the closest-in-VALUE event was correct, because publish lag varies enough
inside a fast cluster to flip which visit is actually nearer in time. So this
narrows to a time window first (wide, since lag varies) and then picks by
closest weight (tight, since two visits close enough in time to be confused are
essentially never close enough in weight too).

Pure -- no I/O, no `EventStore`, no C-side JSON parsing. The caller decodes
`content_json` into `pet_weight` before calling this, same split
`events/metrics.py` already keeps with `ai/weight.py`.
"""
from __future__ import annotations

from typing import Any

#: How far from the pushed reading's timestamp a box-reported visit may still
#: be considered -- generous because `POST_MAX_WAIT_SEC` (240s) already means
#: the push itself can lag real departure by up to 4 minutes; this adds margin
#: on top for clock drift between the device and the add-on.
MATCH_WINDOW_SEC = 300.0

#: How far the two weight readings may disagree and still count as the same
#: visit. Wide enough to comfortably cover the recalculation-vs-box-firmware
#: gap this session measured (worst case ~94g across 13 matched visits) without
#: being so wide it would happily match two DIFFERENT cats' visits that happen
#: to land in the same time window.
MAX_VALUE_DELTA_G = 300.0


def match_visit(pushed_ts: float, pushed_weight_g: float,
                candidates: list[dict[str, Any]]) -> int | None:
    """The `id` of the box-reported visit `pushed` belongs to, or None.

    Args:
        pushed_ts: The pushed reading's timestamp, in the SAME units as
            `EventStore`'s `ts` column (unix seconds) -- the on-device tool's
            `t_ms` is milliseconds and must be converted by the caller.
        pushed_weight_g: The recalculated visit weight (`during_g - pre_g`).
        candidates: Rows shaped like `{"id", "ts", "pet_weight"}` -- a
            `pet_weight` of None (a visit with no readable weight at all) is
            never a valid match and is dropped before scoring.

    Returns:
        None when nothing is close enough in time, or the closest-in-time
        window's weights are all further than `MAX_VALUE_DELTA_G` from
        `pushed_weight_g` -- an unmatched push is dropped by the caller, never
        used to fabricate a new visit (a weight-only reading the box itself
        never reported as a toilet visit is not corroborated as one).
    """
    in_window = [
        c for c in candidates
        if c.get("pet_weight") is not None and abs(c["ts"] - pushed_ts) <= MATCH_WINDOW_SEC
    ]
    if not in_window:
        return None
    best = min(in_window, key=lambda c: abs(c["pet_weight"] - pushed_weight_g))
    if abs(best["pet_weight"] - pushed_weight_g) > MAX_VALUE_DELTA_G:
        return None
    return best["id"]
