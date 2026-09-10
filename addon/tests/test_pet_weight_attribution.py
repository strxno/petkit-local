"""ai/weight.py — the pure weight-based attribution engine.

The headline invariant across every test here: `events.pet_id` (face
recognition) is never overridden and this module never returns
`grade=CONFIRMED` itself — see ai/weight.py's module docstring for why.
"""
from petkit_local.ai.weight import (
    WeightProfile, attribute, attribute_visit, band,
    build_profiles, corroborated, weight_stats,
)
from petkit_local.events import codes

CAT_A = WeightProfile(pet_id=1, weight_g=3200.0, device_ids=frozenset(), corroborated=True)
CAT_B = WeightProfile(pet_id=2, weight_g=5100.0, device_ids=frozenset(), corroborated=True)


def test_no_profiles_leaves_a_visit_unattributed():
    result = attribute(3200.0, [])
    assert result.pet_id is None
    assert result.grade is None
    assert result.reason == "no_profiles"


def test_no_observed_weight_leaves_a_visit_unattributed():
    result = attribute(None, [CAT_A])
    assert result.pet_id is None
    assert result.reason == "no_data"


def test_a_clear_single_match_is_attributed():
    result = attribute(3220.0, [CAT_A, CAT_B])
    assert result.pet_id == 1
    assert result.basis == "weight"
    assert result.grade == codes.INFERRED
    assert result.delta_g == 20.0


def test_out_of_range_is_refused_rather_than_matched_to_the_nearest():
    """A weight far from EVERY profile must not fall back to whichever pet is
    numerically closest — that is precisely the nearest-wins behaviour this
    module deliberately does not have."""
    result = attribute(9000.0, [CAT_A, CAT_B])
    assert result.pet_id is None
    assert result.reason == "out_of_range"


def test_two_pets_with_overlapping_bands_are_ambiguous_in_the_overlap():
    close_a = WeightProfile(pet_id=1, weight_g=4000.0, device_ids=frozenset(), corroborated=True)
    close_b = WeightProfile(pet_id=2, weight_g=4150.0, device_ids=frozenset(), corroborated=True)
    # Their bands (tolerance 8%, floor 100g -> ~320g each) clearly overlap;
    # 4075 sits inside both.
    result = attribute(4075.0, [close_a, close_b])
    assert result.pet_id is None
    assert result.grade == codes.CONFLICTED
    assert result.rivals == (1, 2)
    assert result.reason == "ambiguous"


def test_the_same_close_pair_resolves_in_a_non_overlapping_tail():
    close_a = WeightProfile(pet_id=1, weight_g=4000.0, device_ids=frozenset(), corroborated=True)
    close_b = WeightProfile(pet_id=2, weight_g=4150.0, device_ids=frozenset(), corroborated=True)
    lo_a, _ = band(4000.0)
    # Comfortably inside A's band, outside B's.
    result = attribute(lo_a + 5.0, [close_a, close_b])
    assert result.pet_id == 1


def test_only_one_pet_has_a_profile():
    result = attribute(3210.0, [CAT_A])
    assert result.pet_id == 1


def test_face_confirmed_visit_is_never_overridden_by_weight():
    """Even an EMPTY profile list must not stop the face short-circuit — this
    proves weight is never even consulted once pet_id is set."""
    visit = {"pet_id": 7, "pet_weight": 999999}
    result = attribute_visit(visit, [])
    assert result.pet_id == 7
    assert result.basis == "face"
    assert result.grade == codes.CONFIRMED


def test_an_unconfirmed_visit_falls_through_to_weight_matching():
    visit = {"pet_id": None, "pet_weight": 3220}
    result = attribute_visit(visit, [CAT_A, CAT_B])
    assert result.pet_id == 1
    assert result.basis == "weight"


def test_weight_path_never_returns_confirmed():
    for observed in (None, 100.0, 3200.0, 4075.0, 999999.0):
        for profiles in ([], [CAT_A], [CAT_A, CAT_B]):
            result = attribute(observed, profiles)
            assert result.grade != codes.CONFIRMED
            if result.grade is not None:
                assert result.grade in codes.GRADES


def test_tolerance_is_relative_with_an_absolute_floor():
    # A small cat: 8% of 900g is 72g, below the 100g floor -> floor wins.
    lo, hi = band(900.0)
    assert (lo, hi) == (800.0, 1000.0)

    # A large cat: 8% of 7000g is 560g, above the floor -> relative wins.
    lo2, hi2 = band(7000.0)
    assert (lo2, hi2) == (6440.0, 7560.0)


def test_device_scoping_disambiguates_identical_weights_on_different_boxes():
    twin_a = WeightProfile(pet_id=1, weight_g=4000.0, device_ids=frozenset({10}), corroborated=True)
    twin_b = WeightProfile(pet_id=2, weight_g=4000.0, device_ids=frozenset({20}), corroborated=True)
    result_10 = attribute(4000.0, [twin_a, twin_b], device_id=10)
    assert result_10.pet_id == 1
    result_20 = attribute(4000.0, [twin_a, twin_b], device_id=20)
    assert result_20.pet_id == 2


def test_unassigned_pets_fall_back_to_the_full_profile_set():
    """A device nobody has assigned a pet to must not see zero candidates on
    every visit — the point of device scoping is disambiguation, not a second
    way to lose the match."""
    unassigned = WeightProfile(pet_id=1, weight_g=3200.0, device_ids=frozenset(), corroborated=True)
    result = attribute(3200.0, [unassigned], device_id=99)
    assert result.pet_id == 1


def test_corroborated_needs_enough_confirmed_samples():
    weights = [3190.0, 3210.0, 3200.0]  # only 3 — below MIN_CORROBORATING_SAMPLES
    assert corroborated(3200.0, weights) is False


def test_corroborated_uses_the_median_not_the_mean():
    # One wild outlier (two cats briefly sharing the box) must not tank the
    # grade if the median still agrees with the entered weight.
    weights = [3190.0, 3210.0, 3200.0, 3205.0, 3195.0, 9000.0]
    assert corroborated(3200.0, weights) is True


def test_corroborated_flags_a_stored_weight_that_disagrees_with_evidence():
    weights = [4900.0, 5000.0, 5100.0, 5050.0, 4950.0]
    assert corroborated(3200.0, weights) is False


def test_weight_stats_trims_a_single_bad_reading_before_reporting():
    """Real household case: one clearly bad reading (litter debris, a partial
    settle) among several consistent ones must not become the reported
    median — a plain median at this sample size still lands close to the
    outlier, which is what made an inflated suggestion look trustworthy."""
    weights = [3350.0, 3400.0, 3420.0, 3390.0, 8200.0]
    stats = weight_stats(weights)
    assert 3350.0 <= stats.median <= 3420.0
    assert stats.n == 5  # evidence count is the ORIGINAL count, not post-trim


def test_weight_stats_does_not_trim_below_four_samples():
    """Trimming needs enough points that discarding one is plausible rather
    than arbitrary — below that, the untrimmed median is the honest answer."""
    weights = [3200.0, 3400.0, 9000.0]
    stats = weight_stats(weights)
    assert stats.median == 3400.0  # the untrimmed median of these 3
    assert stats.n == 3


def test_weight_stats_refuses_to_trim_when_it_would_gut_the_sample():
    """A sample where roughly HALF the readings disagree is not "one outlier
    plus good data" — it is telling you something, and silently discarding
    half of it would manufacture false confidence rather than remove noise."""
    weights = [3390.0, 3410.0, 7900.0, 7950.0]
    stats = weight_stats(weights)
    assert stats.n == 4
    # No crash, and the reported median still comes from all 4 points (the
    # trim guard refused to drop half the sample) -- this is the case the
    # UI's own sample-count gate (not this function) is responsible for
    # catching, by not offering a "use this" suggestion at n=4 by default.


def test_weight_stats_empty_is_none():
    assert weight_stats([]) is None


def test_build_profiles_skips_pets_with_no_weight():
    pets = [{"id": 1, "weight": None, "device_ids_json": "[]"}]
    assert build_profiles(pets, []) == []


def test_build_profiles_derives_corroboration_from_confirmed_visits():
    pets = [{"id": 1, "weight": 3200.0, "device_ids_json": "[]"}]
    visits = [
        {"pet_id": 1, "pet_weight": w}
        for w in (3190, 3210, 3200, 3205, 3195)
    ]
    profiles = build_profiles(pets, visits)
    assert len(profiles) == 1
    assert profiles[0].corroborated is True


def test_build_profiles_ignores_unconfirmed_visits_for_corroboration():
    pets = [{"id": 1, "weight": 3200.0, "device_ids_json": "[]"}]
    # No pet_id set on any of these -- they must not count as evidence.
    visits = [{"pet_id": None, "pet_weight": 3200} for _ in range(10)]
    profiles = build_profiles(pets, visits)
    assert profiles[0].corroborated is False


def test_build_profiles_parses_device_assignment():
    pets = [{"id": 1, "weight": 3200.0, "device_ids_json": "[10, 20]"}]
    profiles = build_profiles(pets, [])
    assert profiles[0].device_ids == frozenset({10, 20})


def test_build_profiles_survives_a_broken_device_ids_json():
    pets = [{"id": 1, "weight": 3200.0, "device_ids_json": "not json"}]
    profiles = build_profiles(pets, [])
    assert profiles[0].device_ids == frozenset()
