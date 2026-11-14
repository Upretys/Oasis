import pytest

from backend.contracts import RunStarted, StatementEvent, RecommendationEvent, RulingEvent, Run
from backend.evidence import build_evidence, plan_metrics
from backend.jurisdiction import FIXTURES, load_building, place
from backend.triage import triage

NAMES = ["hero", "clear_pass", "clear_fail"]


@pytest.fixture(scope="module")
def placed():
    out = {}
    for n in NAMES:
        b = load_building(n)
        j = place(b)
        out[n] = (b, j, build_evidence(b, j))
    return out


@pytest.fixture(scope="module")
def golden():
    return Run.model_validate_json((FIXTURES / "golden_run.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NAMES)
def test_ids_unique_and_anchored(placed, name):
    ev = placed[name][2]
    ids = [e.id for e in ev]
    assert len(ids) == len(set(ids))
    assert all(e.anchor is not None and e.anchor.kind for e in ev)


def test_evidence_is_deterministic(placed):
    b, j, ev = placed["hero"]
    assert build_evidence(b, j) == ev


def test_hero_has_18_to_24_facts(placed):
    assert 18 <= len(placed["hero"][2]) <= 24


def test_hero_geometry_story(placed):
    m = plan_metrics(placed["hero"][0])
    assert m["deepest_ft"] == pytest.approx(153)
    assert m["daylit_share"] == pytest.approx(10000 / 30600)
    ev = {e.id: e for e in placed["hero"][2]}
    assert ev["E-GEO-02"].anchor.kind == "plan_point" and ev["E-GEO-02"].anchor.y == 153


def test_zoning_facts_only_for_published_fields(placed):
    _, j, ev = placed["clear_pass"]
    assert j.max_height_ft is None and j.set_aside_pct is None
    labels = [e.label for e in ev if e.layer == "zoning"]
    assert "Maximum height" not in labels and "Affordable set-aside" not in labels


@pytest.mark.parametrize("name,verdict", [("hero", "escalate"), ("clear_pass", "pass"), ("clear_fail", "fail")])
def test_triage(placed, name, verdict):
    assert triage(placed[name][0]) == verdict


def test_triage_fails_when_conversion_not_allowed(placed):
    b = placed["clear_pass"][0].model_copy(update={"lon": -74.52})   # moves the site into the M1 district
    j = place(b)
    assert j.conversion_pathway == "not_allowed"
    assert triage(b) == "fail"


@pytest.mark.parametrize("name,muni,zone,pathway", [
    ("hero", "demo-city", "C6", "special_permit"),
    ("clear_pass", "demo-city", "MX3", "by_right"),
    ("clear_fail", "demo-township", "O1", "variance"),
])
def test_place(placed, name, muni, zone, pathway):
    j = placed[name][1]
    assert (j.jurisdiction_id, j.zoning_district, j.conversion_pathway) == (muni, zone, pathway)
    assert j.council_district is not None


def test_place_raises_for_unknown_site(placed):
    b = placed["hero"][0].model_copy(update={"lon": 0.0, "lat": 0.0})
    with pytest.raises(ValueError):
        place(b)


def test_golden_run_validates_and_is_ordered(golden):
    types = [e.type for e in golden.events]
    assert types[0] == "run_started" and types[-2:] == ["ruling", "recommendation"]
    assert 3 <= types.count("statement") <= 9
    assert types.count("round_complete") == types.count("statement") // 3


def test_golden_run_matches_code(golden, placed):
    b, j, ev = placed["hero"]
    start = golden.events[0]
    assert isinstance(start, RunStarted)
    assert start.evidence == ev and start.jurisdiction == j and start.verdict == triage(b)


def test_golden_run_cites_only_real_evidence(golden):
    known = {e.id for e in golden.events[0].evidence}
    cited, claim_ids = [], set()
    for e in golden.events:
        if isinstance(e, StatementEvent):
            for c in e.statement.claims:
                cited += c.evidence_ids
                if c.kind == "rebuttal":
                    assert c.target_claim_id in claim_ids, f"{c.id} rebuts an unseen claim"
                claim_ids.add(c.id)
            cited += [i for c in e.statement.conditions for i in c.evidence_ids]
        if isinstance(e, RulingEvent):
            cited += [i for c in e.ruling.conditions for i in c.evidence_ids]
        if isinstance(e, RecommendationEvent):
            cited += [i for p in e.recommendation.phases for i in p.evidence_ids]
    assert cited and set(cited) <= known, set(cited) - known


def test_vacant_floors_are_one_grouped_fact(placed):
    b, _, ev = placed["hero"]
    vacant = [e for e in ev if e.label.startswith("Vacant now")]
    assert [(e.id, e.label, e.value) for e in vacant] == \
        [("E-CST-07", "Vacant now, free to convert: floors 2-3, 7, 9-10", 5.0)]
    assert vacant[0].anchor.floor == 2


def test_vacancy_fact_does_not_renumber_lease_facts(placed):
    ev = {e.id: e.label for e in placed["hero"][2]}
    assert ev["E-CST-05"] == "Floors 4-6 lease: months to expiry"
    assert ev["E-CST-06"] == "Floor 8 lease: years remaining"


def test_no_vacancy_fact_when_every_floor_is_let(placed):
    b = placed["hero"][0]
    let = b.model_copy(update={"floors": [r.model_copy(update={"tenant": "X", "lease_expiry": "2030-01-01"})
                                          for r in b.floors]})
    assert not any(e.label.startswith("Vacant now") for e in build_evidence(let, place(let)))
