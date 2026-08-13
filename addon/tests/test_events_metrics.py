"""events/metrics.py — the Insights tab's pure aggregation.

Visit rows here are shaped like `EventStore.visit_summaries` output; pet rows
like `web/api/_common.py::_pets_by_id` (a `faces` list already attached).
"""
import json

from petkit_local.events.metrics import build_insights


def _visit(pet_id=None, weight=None, ts=100.0, device_id=1, related=None):
    content = {}
    if weight is not None:
        content["pet_weight"] = weight
    return {
        "device_id": device_id, "device_type": "t6", "ts": ts,
        "related_event": related, "pet_id": pet_id, "pet_ref": None,
        "event_type": "10", "content_json": json.dumps(content),
    }


def test_empty_input_produces_an_empty_but_well_shaped_response():
    out = build_insights([], [], {}, start_ts=0.0, end_ts=100.0, tz_offset=0.0,
                         retention_cutoff_ts=None)
    assert out["visits"] == []
    assert out["pets"] == []
    assert out["start"] == 0.0 and out["end"] == 100.0
    assert out["retention_cutoff_ts"] is None


def test_a_face_confirmed_visit_carries_pet_id_and_no_attributed_pet_id():
    pets = [{"id": 1, "name": "Milo", "weight": 3200.0, "faces": []}]
    visits = [_visit(pet_id=1, weight=3200.0)]
    out = build_insights(pets, visits, {}, start_ts=0.0, end_ts=200.0, tz_offset=0.0,
                         retention_cutoff_ts=None)
    row = out["visits"][0]
    assert row["pet_id"] == 1
    assert row["attributed_pet_id"] is None
    assert row["grade"] == "confirmed"
    assert row["weight"] == 3200.0


def test_an_unconfirmed_visit_within_a_pets_band_gets_attributed_pet_id():
    # Five confirmed visits at ~3200g corroborate Milo's entered weight.
    confirmed = [_visit(pet_id=1, weight=w, ts=float(i))
                for i, w in enumerate((3190, 3210, 3200, 3205, 3195))]
    guess = _visit(pet_id=None, weight=3215.0, ts=50.0)
    pets = [{"id": 1, "name": "Milo", "weight": 3200.0, "faces": []}]
    out = build_insights(pets, confirmed + [guess], {}, start_ts=0.0, end_ts=100.0,
                         tz_offset=0.0, retention_cutoff_ts=None)
    guessed_row = next(r for r in out["visits"] if r["ts"] == 50.0)
    assert guessed_row["pet_id"] is None
    assert guessed_row["attributed_pet_id"] == 1
    assert guessed_row["grade"] == "inferred"


def test_an_out_of_range_visit_stays_unattributed():
    pets = [{"id": 1, "name": "Milo", "weight": 3200.0, "faces": []}]
    visits = [_visit(pet_id=None, weight=9000.0)]
    out = build_insights(pets, visits, {}, start_ts=0.0, end_ts=200.0, tz_offset=0.0,
                         retention_cutoff_ts=None)
    row = out["visits"][0]
    assert row["pet_id"] is None
    assert row["attributed_pet_id"] is None
    assert row["grade"] is None


def test_duration_falls_back_to_pet_in_pairing():
    visit = _visit(pet_id=1, weight=3200.0, ts=156.0, related="r1")
    pets = [{"id": 1, "name": "Milo", "weight": 3200.0, "faces": []}]
    out = build_insights(pets, [visit], {"r1": 100.0}, start_ts=0.0, end_ts=200.0,
                         tz_offset=0.0, retention_cutoff_ts=None)
    assert out["visits"][0]["duration_sec"] == 56.0


def test_pet_row_carries_observed_stats_even_with_no_weight_set():
    """A pet with NO entered weight still gets a calibration summary from its
    confirmed visits — that is the zero-input bootstrap the Insights tab
    offers ("suggest from confirmed visits")."""
    confirmed = [_visit(pet_id=1, weight=w, ts=float(i))
                for i, w in enumerate((3190, 3210, 3200, 3205, 3195))]
    pets = [{"id": 1, "name": "Milo", "weight": None, "faces": []}]
    out = build_insights(pets, confirmed, {}, start_ts=0.0, end_ts=100.0, tz_offset=0.0,
                         retention_cutoff_ts=None)
    pet = out["pets"][0]
    assert pet["weight"] is None
    assert pet["observed"]["n_confirmed"] == 5
    assert pet["observed"]["median"] == 3200.0


def test_pet_row_photo_url_from_first_face():
    pets = [{"id": 1, "name": "Milo", "weight": None,
            "faces": [{"id": 9, "url": "api/pets/1/faces/9/photo"}]}]
    out = build_insights(pets, [], {}, start_ts=0.0, end_ts=100.0, tz_offset=0.0,
                         retention_cutoff_ts=None)
    assert out["pets"][0]["photo_url"] == "api/pets/1/faces/9/photo"


def test_retention_cutoff_is_echoed_back_unchanged():
    out = build_insights([], [], {}, start_ts=0.0, end_ts=100.0, tz_offset=0.0,
                         retention_cutoff_ts=42.0)
    assert out["retention_cutoff_ts"] == 42.0
