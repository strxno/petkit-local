"""ai/weigh_recalc.py::match_visit — matching a pushed on-device recalculation
to the real box-reported visit it belongs to.

The four-visits-in-three-minutes case is real data from this session: a cat
checking the box right after another one leaves. Nearest-TIMESTAMP alone
picked the wrong box event there; nearest-VALUE picked correctly.
"""
from petkit_local.ai.weigh_recalc import match_visit


def test_a_clean_match_within_the_window_is_found():
    candidates = [{"id": 1, "ts": 1000.0, "pet_weight": 3418.0}]
    assert match_visit(1005.0, 3421.0, candidates) == 1


def test_nothing_in_the_time_window_is_unmatched():
    candidates = [{"id": 1, "ts": 100.0, "pet_weight": 3200.0}]
    assert match_visit(10000.0, 3200.0, candidates) is None


def test_a_close_time_but_wildly_different_weight_is_unmatched():
    """Time proximity alone must not be enough -- a value far outside
    MAX_VALUE_DELTA_G means this is a different visit (or no visit at all),
    not a noisy read of the one nearby in time."""
    candidates = [{"id": 1, "ts": 1000.0, "pet_weight": 3200.0}]
    assert match_visit(1005.0, 5200.0, candidates) is None


def test_candidates_with_no_readable_weight_are_never_matched():
    candidates = [{"id": 1, "ts": 1000.0, "pet_weight": None}]
    assert match_visit(1005.0, 3200.0, candidates) is None


def test_nearest_timestamp_would_pick_the_wrong_visit_in_a_fast_cluster():
    """Real capture: box logged two toileting sessions ~90s apart
    (pet_weight 3418 at ts=1786851097, and 2691 at ts=1786851186). A push
    for the 2691g visit (recalculated weight ~2676g) arrived at ts=1786851133
    -- 36s after the FIRST box event but 53s before the SECOND. Nearest
    timestamp picks the first (wrong) one; nearest value correctly picks the
    second."""
    candidates = [
        {"id": 41, "ts": 1786851097.0, "pet_weight": 3418.0},
        {"id": 42, "ts": 1786851186.0, "pet_weight": 2691.0},
    ]
    assert match_visit(1786851133.0, 2676.0, candidates) == 42


def test_the_closest_by_value_wins_among_several_in_window_candidates():
    candidates = [
        {"id": 1, "ts": 1000.0, "pet_weight": 3200.0},
        {"id": 2, "ts": 1040.0, "pet_weight": 5200.0},
    ]
    assert match_visit(1045.0, 5195.0, candidates) == 2
