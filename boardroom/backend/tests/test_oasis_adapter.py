"""The adapter turns Oasis's real Houston results into Buildings without inventing facts."""
import pytest

from backend.evidence import build_evidence, plan_metrics
from backend.jurisdiction import place
from backend.oasis_adapter import cached_buildings, load_saved_building, viability
from backend.seats import visible
from backend.triage import triage

AS_OF = "2026-11-14"
NAMES = [b["name"] for b in cached_buildings()]
EXPECTED_TRIAGE = {"feasible": "pass", "marginal": "escalate", "not_feasible": "fail"}


@pytest.fixture(scope="module", params=NAMES)
def building(request):
    return load_saved_building(request.param, AS_OF)


def test_all_five_featured_buildings_load():
    assert len(NAMES) == 5


def test_plate_matches_costar_floor_area(building):
    cached = next(b for b in cached_buildings() if b["name"] == building.name)
    assert plan_metrics(building)["plate_sf"] == pytest.approx(cached["real_floor_area_sqft"], rel=0.001)


def test_houston_is_by_right_with_a_real_source(building):
    j = place(building)
    assert (j.jurisdiction_id, j.zoning_district, j.conversion_pathway) == ("houston", "none", "by_right")
    assert j.source_url.startswith("https://houstontx.gov/")


def test_no_invented_aerial_or_lot_facts(building):
    labels = [e.label for e in build_evidence(building, place(building))]
    assert not any(l.startswith(("Height of the facing building", "Street width", "Existing FAR")) for l in labels)
    assert any(l.startswith("Daylight limit from a window (assumed") for l in labels)


def test_assumed_cap_rate_is_labelled():
    for name in NAMES:
        b = load_saved_building(name, AS_OF)
        cap = next(e for e in build_evidence(b, place(b)) if e.label.startswith("Cap rate"))
        assert ("assumed" in cap.label) == b.costar.cap_rate_assumed


def test_occupied_floor_without_end_date_is_stated_not_dropped():
    b = load_saved_building("Williams Tower", AS_OF)
    lease = [e for e in build_evidence(b, place(b)) if "lease" in e.label]
    assert [(e.label, e.value) for e in lease] == [("Floor 18 lease: end date not on record", "unknown")]


def test_triage_follows_the_oasis_classifier():
    import json
    from backend.oasis_adapter import SAVED_RESULTS
    labels = {r["building_name"]: r["classification"] for r in json.loads(SAVED_RESULTS.read_text())}
    for name in NAMES:
        b = load_saved_building(name, AS_OF)
        assert triage(b) == EXPECTED_TRIAGE[labels[name]], name


@pytest.mark.parametrize("label", list(EXPECTED_TRIAGE))
@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_viability_stays_inside_its_triage_band(label, confidence):
    from backend.triage import ESCALATE_AT, PASS_AT
    v = viability(label, confidence)
    band = "pass" if v >= PASS_AT else "escalate" if v >= ESCALATE_AT else "fail"
    assert band == EXPECTED_TRIAGE[label]


def test_every_seat_sees_evidence(building):
    ev = build_evidence(building, place(building))
    for seat in ("engineer", "planner", "analyst"):
        assert visible(seat, ev), seat
