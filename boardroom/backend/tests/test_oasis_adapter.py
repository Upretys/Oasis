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
    lease = [e for e in build_evidence(b, place(b)) if " lease: " in e.label]
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


# --- money: the board quotes Oasis's own cost/value model --------------------

def _saved(name):
    import json
    from backend.oasis_adapter import SAVED_RESULTS
    return next(r for r in json.loads(SAVED_RESULTS.read_text()) if r["building_name"] == name)


def _fact(b, prefix):
    return next(e.value for e in build_evidence(b, place(b)) if e.label.startswith(prefix))


def test_net_impact_matches_oasis_exactly(building):
    s = _saved(building.name)
    assert _fact(building, "Net value impact after construction, low end") == round(s["implied_value_impact_low"])
    assert _fact(building, "Net value impact after construction, high end") == round(s["implied_value_impact_high"])


def test_cost_range_is_oasis_150_to_220_per_sf_times_material(building):
    e = building.economics
    mult = float(__import__("re").search(r"([\d.]+)x baseline", _saved(building.name)["reasoning_text"]).group(1))
    assert e.cost_low_usd / e.convertible_sf == pytest.approx(150 * mult, abs=0.5)
    assert e.cost_high_usd / e.convertible_sf == pytest.approx(220 * mult, abs=0.5)


def test_flat_350_model_is_not_used_for_oasis_buildings(building):
    labels = [e.label for e in build_evidence(building, place(building))]
    assert not any("$350/sf" in l or "NOI margin" in l for l in labels)


def test_analyst_sees_the_oasis_money(building):
    seen = {e.label.split(" (")[0].split(",")[0] for e in visible("analyst", build_evidence(building, place(building)))}
    assert {"Rent uplift over office", "Conversion cost", "Residential value",
            "Net value impact after construction", "Office rent given up by converting"} <= seen


def test_assumed_office_rent_is_labelled():
    b = load_saved_building("Williams Tower", AS_OF)
    rent = next(e for e in build_evidence(b, place(b)) if e.label.startswith("Office rent given up"))
    assert rent.value == 24.0 and "assumed" in rent.label


def test_demo_fixtures_keep_the_flat_model():
    from backend.jurisdiction import load_building
    b = load_building("hero")
    assert b.economics is None
    assert any("$350/sf" in e.label for e in build_evidence(b, place(b)))


# --- vacancy: confirmed-vacant floors are stated, unknown occupancy is not -----

@pytest.mark.parametrize("name", ["TC Energy Center", "Woodway Center", "7900 Fannin", "Two Bering Park"])
def test_confirmed_vacant_floor_is_stated_and_the_analyst_sees_it(name):
    b = load_saved_building(name, AS_OF)
    seen = [e for e in visible("analyst", build_evidence(b, place(b))) if e.label.startswith("Vacant now")]
    assert [(e.label, e.value) for e in seen] == [(f"Vacant now, free to convert: floor {b.floors[0].floor}", 1.0)]


def test_occupied_floor_is_not_called_vacant():
    b = load_saved_building("Williams Tower", AS_OF)
    assert not any(e.label.startswith("Vacant now") for e in build_evidence(b, place(b)))
