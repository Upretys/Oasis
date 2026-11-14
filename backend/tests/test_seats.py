"""Live seat tests: every seat on every fixture, round 0, against the real API.  Run: pytest -m live"""
import pytest

from backend import seats
from backend.jurisdiction import load_building

pytestmark = pytest.mark.live
BUILDINGS = ["hero", "clear_pass", "clear_fail"]
_runs: dict = {}


def run(seat, name):
    if (seat, name) not in _runs:
        b = load_building(name)
        ev = seats._evidence(b)
        _runs[seat, name] = (seats.run_seat(seat, b, 0), {e.id: e for e in ev}, {e.id for e in seats.visible(seat, ev)})
    return _runs[seat, name]


def cited(st):
    return [i for c in st.claims for i in c.evidence_ids] + [i for c in st.conditions for i in c.evidence_ids]


@pytest.mark.parametrize("name", BUILDINGS)
@pytest.mark.parametrize("seat", list(seats.SEATS))
def test_valid_statement_citing_only_visible_evidence(seat, name):
    event, _, vis = run(seat, name)
    assert not event.fallback_used, "the model output failed validation twice"
    st = event.statement
    assert (st.seat, st.round) == (seat, 0) and 1 <= len(st.claims) <= 4
    assert set(cited(st)) <= vis


def test_engineer_on_hero_raises_deepest_point_risk():
    event, ev, _ = run("engineer", "hero")
    assert any(c.kind == "risk" and any(ev[i].label == "Deepest point from a window" for i in c.evidence_ids)
               for c in event.statement.claims)


def test_analyst_on_hero_cites_a_lease_expiry():
    event, ev, _ = run("analyst", "hero")
    assert any("lease" in ev[i].label for i in cited(event.statement))


def test_planner_on_hero_cites_the_conversion_pathway():
    event, ev, _ = run("planner", "hero")
    assert any(ev[i].label == "Conversion pathway" for i in cited(event.statement))


@pytest.mark.parametrize("seat", list(seats.SEATS))
def test_every_seat_on_hero_proposes_a_condition_in_its_domain(seat):
    event, _, vis = run(seat, "hero")
    assert any(c.evidence_ids and set(c.evidence_ids) <= vis for c in event.statement.conditions)
