"""Weight-based visit attribution — a pure, unit-tested guess.

`events.pet_id` means FACE-CONFIRMED and nothing else (`ai/pets.py::
resolve_pet_ref`'s own invariant: "writing an unresolved ref into `pet_id`
fabricates an HA pet device for a row that does not exist"). This module never
writes there. It answers a narrower, weaker question at query time — "given
the weight this visit reported and what we know about each pet's weight,
which pet was probably here" — for the Insights tab and a Timeline badge to
show ALONGSIDE face confirmation, never in place of it.

Everything here is a plain function over plain data: no `EventStore`, no
`PetRegistry`, no I/O. `build_profiles` takes pet rows and visit rows the
caller already fetched (`events/metrics.py` needs that same data for its own
aggregation, so fetching it twice would be pure waste) rather than querying
again itself. That is what makes the decision logic — the part someone will
keep correcting as real weight data comes in — testable with plain dicts and
no database.

## The tolerance is not a scale-accuracy number

`band()` decides how far an observed weight may sit from a pet's known weight
and still count as a match. The device's own weight sensor is documented
elsewhere (`patchers/waste.py`) as accurate to a few grams — this tolerance is
NOT absorbing instrument noise. It is absorbing biology and behaviour: litter
stuck to paws, a partial read while the cat is still settling, and — over the
weeks a profile is trusted for — the cat's own weight actually changing. A
future reader shrinking this toward measurement precision would make the
matcher wrong in exactly the cases it exists for.

## Band membership, not nearest-neighbour

Two pets whose tolerance bands overlap genuinely cannot be told apart by
weight alone in the overlap — there is no honest single answer. Picking
whichever profile is numerically closest would manufacture a confident-looking
answer right there and be correct only by chance. Band membership is instead
counted: zero bands contain the observation -> out of range; exactly one ->
that pet, gradeed by how much evidence backs its weight; two or more -> the
pets are not separable at this weight and the visit stays unattributed,
naming its rivals so the caller can say why.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from petkit_local.events import codes
from petkit_local.utils.coerce import to_float

#: Relative tolerance, with an absolute floor so a very light pet is not
#: matched to within a handful of grams by percentage alone (8% of 800 g is
#: 64 g, tighter than the box itself resolves reliably) and a very heavy one
#: is not given a swing wide enough to swallow a second cat (8% of 7 kg is
#: 560 g). Neither bound is measured from this household's data — nobody's
#: weight distribution was available this session — so the Insights tab's
#: calibration histogram is what actually validates these numbers in
#: practice; treat them as a starting point, not a derived constant.
REL_TOLERANCE = 0.08
ABS_FLOOR_G = 100.0

#: Below this many face-confirmed visits, a pet's weight profile is
#: UNVERIFIED even when a visit matches it cleanly — five is enough to reject
#: an obvious typo (500 g instead of 5000 g) without demanding months of
#: history before the matcher does anything at all.
MIN_CORROBORATING_SAMPLES = 5


@dataclass(frozen=True)
class WeightProfile:
    """One pet's weight, as `attribute()` sees it.

    `corroborated` says whether FACE-CONFIRMED visits for this pet actually
    weigh close to `weight_g` — ground truth the device already gave us,
    never a claim `attribute()` invents about its own guess. A pet with no
    confirmed history yet, or whose confirmed visits disagree with its
    entered weight, is not corroborated: the matcher still uses it, but grades
    a match `UNVERIFIED` rather than `INFERRED`.
    """
    pet_id: int
    weight_g: float
    device_ids: frozenset[int]
    corroborated: bool


@dataclass(frozen=True)
class Attribution:
    """The result of `attribute()`/`attribute_visit()`.

    `grade` is always one of `codes.GRADES` (or None, for a visit this could
    not attribute at all) — never a value invented for this module, because
    AGENTS.md already records what happens when two similarly-named scores
    drift apart ("its two `score`s are not the same number... that confusion
    is how the endpoint was misread for a year"). `basis` says WHERE the
    attribution came from: `"face"` (from `events.pet_id`, always `CONFIRMED`,
    never produced or overridden by this module) or `"weight"`.
    """
    pet_id: int | None
    basis: str | None       # "face" | "weight" | None
    grade: str | None       # one of codes.GRADES, or None
    delta_g: float | None = None      # observed - profile.weight_g, signed
    rivals: tuple[int, ...] = field(default_factory=tuple)
    reason: str | None = None         # "out_of_range" | "ambiguous" | None


def band(weight_g: float) -> tuple[float, float]:
    """The acceptance range around `weight_g` — see the module docstring for
    why this is relative-with-a-floor rather than either alone."""
    tol = max(ABS_FLOOR_G, REL_TOLERANCE * weight_g)
    return (weight_g - tol, weight_g + tol)


def _scoped_profiles(profiles: list[WeightProfile],
                     device_id: int | None) -> list[WeightProfile]:
    """Profiles for pets assigned to `device_id`, or every profile if none are.

    Device scoping is free disambiguation on a multi-box household — two cats
    of similar weight on different boxes never compete — and costs nothing on
    a single-box one, where every pet is assigned (or none are, which is
    exactly the fallback case: an install where nobody has filled in device
    assignments must not see zero candidates on every visit).
    """
    if device_id is None:
        return profiles
    scoped = [p for p in profiles if not p.device_ids or device_id in p.device_ids]
    return scoped if scoped else profiles


def attribute(observed_g: float | None, profiles: list[WeightProfile],
             device_id: int | None = None) -> Attribution:
    """Guess which pet a visit's OBSERVED weight belongs to.

    Never returns `grade=CONFIRMED` — that grade belongs to face recognition
    alone. Use `attribute_visit` when a visit might already be face-confirmed;
    call this directly only when you know it is not (e.g. the calibration
    histogram, which buckets every visit regardless of `pet_id`).
    """
    if observed_g is None or not profiles:
        return Attribution(pet_id=None, basis=None, grade=None,
                           reason="no_data" if observed_g is None else "no_profiles")

    candidates = [
        p for p in _scoped_profiles(profiles, device_id)
        if band(p.weight_g)[0] <= observed_g <= band(p.weight_g)[1]
    ]

    if not candidates:
        return Attribution(pet_id=None, basis=None, grade=None, reason="out_of_range")

    if len(candidates) > 1:
        return Attribution(
            pet_id=None, basis=None, grade=codes.CONFLICTED,
            rivals=tuple(sorted(p.pet_id for p in candidates)), reason="ambiguous")

    winner = candidates[0]
    grade = codes.INFERRED if winner.corroborated else codes.UNVERIFIED
    return Attribution(pet_id=winner.pet_id, basis="weight", grade=grade,
                       delta_g=observed_g - winner.weight_g)


def attribute_visit(visit: dict[str, Any], profiles: list[WeightProfile],
                    device_id: int | None = None) -> Attribution:
    """`attribute()`, but a face-confirmed visit short-circuits first.

    `visit` is a row shaped like `EventStore.visit_summaries` (or any dict
    carrying `pet_id` and an observed weight under `weight_g`/`pet_weight`/
    `petWeight`). Weight is never even consulted once `pet_id` is set — the
    short-circuit is unconditional, so an empty or nonsensical `profiles` list
    still returns the confirmed pet.
    """
    if visit.get("pet_id") is not None:
        return Attribution(pet_id=visit["pet_id"], basis="face", grade=codes.CONFIRMED)

    observed = to_float(
        visit.get("weight_g", visit.get("pet_weight", visit.get("petWeight"))), None)
    return attribute(observed, profiles, device_id)


def _visit_weight(visit: dict[str, Any]) -> float | None:
    """The observed weight on a `visit_summaries`-shaped row, if any."""
    return to_float(
        visit.get("weight_g", visit.get("pet_weight", visit.get("petWeight"))), None)


@dataclass(frozen=True)
class WeightStats:
    """Summary of a pet's face-confirmed observed weights.

    `spread` is the median absolute deviation, not a standard deviation —
    same reasoning as using the median for the centre: one outlier (two cats
    briefly sharing the box) must not blow up the summary a household actually
    reads on the Insights calibration view.
    """
    median: float
    spread: float
    n: int


def weight_stats(weights: list[float]) -> WeightStats | None:
    """Median and spread of `weights`, or None for an empty list."""
    if not weights:
        return None
    sorted_w = sorted(weights)
    n = len(sorted_w)
    median = (sorted_w[n // 2] if n % 2 else (sorted_w[n // 2 - 1] + sorted_w[n // 2]) / 2)
    deviations = sorted(abs(w - median) for w in weights)
    dn = len(deviations)
    mad = (deviations[dn // 2] if dn % 2 else (deviations[dn // 2 - 1] + deviations[dn // 2]) / 2)
    return WeightStats(median=median, spread=mad, n=n)


def corroborated(weight_g: float, confirmed_weights: list[float]) -> bool:
    """Whether face-confirmed visits actually support `weight_g`.

    Median rather than mean: one outlier (two cats briefly sharing the box, a
    toy left inside) must not drag a whole profile's grade down. Requires
    `MIN_CORROBORATING_SAMPLES` before saying yes at all — a single lucky
    match proves nothing.
    """
    if len(confirmed_weights) < MIN_CORROBORATING_SAMPLES:
        return False
    stats = weight_stats(confirmed_weights)
    lo, hi = band(weight_g)
    return lo <= stats.median <= hi


def build_profiles(pets: list[dict[str, Any]],
                   visits: list[dict[str, Any]]) -> list[WeightProfile]:
    """One `WeightProfile` per pet with a weight set.

    `visits` is scanned once for every pet's face-confirmed observations
    (`corroborated()`'s input) — a single pass rather than one query or one
    filter per pet, since this runs over the same page `events/metrics.py`
    already built for its own aggregation.

    A pet with `weight IS NULL` is skipped entirely: no profile means every
    visit falls through to `out_of_range` for that pet rather than matching
    against a fabricated zero.
    """
    confirmed_by_pet: dict[int, list[float]] = {}
    for visit in visits:
        pid = visit.get("pet_id")
        w = _visit_weight(visit)
        if pid is not None and w is not None:
            confirmed_by_pet.setdefault(pid, []).append(w)

    profiles = []
    for pet in pets:
        weight = pet.get("weight")
        if weight is None:
            continue
        try:
            device_ids = frozenset(json.loads(pet.get("device_ids_json") or "[]"))
        except (ValueError, TypeError):
            device_ids = frozenset()
        weight_g = float(weight)
        profiles.append(WeightProfile(
            pet_id=pet["id"], weight_g=weight_g, device_ids=device_ids,
            corroborated=corroborated(weight_g, confirmed_by_pet.get(pet["id"], [])),
        ))
    return profiles
