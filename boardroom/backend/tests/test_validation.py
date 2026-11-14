"""Offline tests for the seat plumbing: a fake client stands in for the API."""
import copy

import pytest

from backend import llm, seats
from backend.contracts import Run, StatementEvent
from backend.jurisdiction import FIXTURES, load_building

HERO = load_building("hero")
EV = seats._evidence(HERO)
GOOD = {"seat": "engineer", "round": 0, "verdict": "escalate", "confidence": 0.7,
        "claims": [{"text": "The deepest point is 153 ft from a window; daylight reaches only 50 ft.",
                    "evidence_ids": ["E-GEO-02", "E-AER-01"], "kind": "risk"}],
        "conditions": [{"text": "Cut a rear lightwell 3 bays deep", "evidence_ids": ["E-DER-02"]}],
        "would_change_my_mind": "A daylight study showing light past 50 ft."}
HIDDEN = {**GOOD, "claims": [{"text": "Zoning is fine.", "evidence_ids": ["E-ZON-01"], "kind": "support"}]}


class FakeClient:
    def __init__(self, *inputs):
        self.inputs, self.requests, self.messages = list(inputs), [], self

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        item = self.inputs.pop(0)
        if isinstance(item, Exception):
            raise item
        block = {"type": "tool_use", "id": f"toolu_{len(self.requests)}", "name": "submit_statement", "input": item}
        return type("Resp", (), {"model_dump": lambda self: {"content": [block]}})()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE", tmp_path)
    monkeypatch.setenv("BOARD_MODEL", "claude-haiku-5-5")


def ids(seat):
    return {e.id for e in seats.visible(seat, EV)}


def test_seats_see_only_their_layers():
    assert ids("planner") == {e.id for e in EV if e.layer == "zoning"} | {"E-DER-03"}
    assert not any(i.startswith("E-ZON") for i in ids("engineer") | ids("analyst"))
    assert {"E-CST-01", "E-DER-01", "E-DER-02"} <= ids("engineer")
    assert {"E-DER-04", "E-DER-05"} <= ids("analyst") and "E-DER-01" not in ids("analyst")
    assert ids("engineer") | ids("planner") | ids("analyst") == {e.id for e in EV}
    assert all(cfg["weight"] == 1.0 for cfg in seats.SEATS.values())


def test_system_prompt_is_the_template_filled_in():
    text = seats.build_system_prompt("planner", HERO)
    assert text.startswith("You are the City Planner on an investment committee")
    assert "{" + "mandate}" not in text and "E-GEO-02" not in text
    assert all(i in text for i in ids("planner"))


def test_golden_statements_pass_the_same_checks():
    run = Run.model_validate_json((FIXTURES / "golden_run.json").read_text(encoding="utf-8"))
    prior = set()
    for e in run.events:
        if isinstance(e, StatementEvent):
            st = e.statement
            assert llm.check(st.seat, st, seats.visible(st.seat, EV), prior) == [], st.seat
            prior |= {c.id for c in st.claims}


def test_tool_schema_comes_from_the_contract():
    assert llm.TOOL["input_schema"]["title"] == "Statement"


def test_valid_call_is_cached_and_ids_assigned():
    fake = FakeClient(GOOD)
    ev = seats.run_seat("engineer", HERO, 0, client=fake)
    assert not ev.fallback_used and ev.statement.claims[0].id == "ENG-0-1"
    assert fake.requests[0]["tool_choice"] == {"type": "tool", "name": "submit_statement"}
    assert seats.run_seat("engineer", HERO, 0, client=FakeClient()).statement == ev.statement   # cache hit


def test_retry_appends_the_validation_error():
    fake = FakeClient(HIDDEN, GOOD)
    ev = seats.run_seat("engineer", HERO, 0, client=fake)
    assert not ev.fallback_used and len(fake.requests) == 2
    fix = fake.requests[1]["messages"][-1]["content"][0]
    assert fix["type"] == "tool_result" and fix["is_error"] and "E-ZON-01" in fix["content"]


def test_two_failures_fall_back_to_golden():
    ev = seats.run_seat("analyst", HERO, 0, client=FakeClient(HIDDEN, HIDDEN))
    assert ev.fallback_used and ev.statement.claims[0].id == "ANL-0-1"


def test_api_error_falls_back_to_golden():
    ev = seats.run_seat("planner", HERO, 0, client=FakeClient(TimeoutError("25s")))
    assert ev.fallback_used and ev.statement.seat == "planner"


def test_invented_numeral_is_rejected():
    raw = {**GOOD, "claims": [{"text": "Conversion would cost $90M.", "evidence_ids": ["E-DER-04"], "kind": "risk"}]}
    with pytest.raises(ValueError, match="90000000"):
        llm.parse("analyst", 0, raw, seats.visible("analyst", EV), set())


def test_numerals_carry_units():
    assert llm.numerals("$107.1M, 60%, 153 ft, a 9-month permit, 7.0 years, FAR of 10, 2.6 bays") == [
        (107100000, "usd"), (60, "pct"), (153, "ft"), (9, "months"), (7.0, "years"), (10, "far"), (2.6, "bays")]


@pytest.mark.parametrize("text,ids", [
    ("The cap rate is 7%.", ["E-CST-04", "E-CST-06"]),        # 7 matches only the 7.0-year lease
    ("Approval takes 10 months.", ["E-ZON-02", "E-DER-03"]),   # 10 matches only the FAR of 10.0
])
def test_a_number_must_match_a_cited_fact_with_the_same_unit(text, ids):
    seat = "analyst" if "E-CST-04" in ids else "planner"
    raw = {**GOOD, "claims": [{"text": text, "evidence_ids": ids, "kind": "risk"}], "conditions": []}
    with pytest.raises(ValueError, match="cited evidence does not contain"):
        llm.parse(seat, 0, raw, seats.visible(seat, EV), set())


def test_unitless_and_matching_units_still_pass():
    raw = {**GOOD, "claims": [{"text": "Cap rate 5.5% on $53.7M; office rent 38/sf/yr.",
                               "evidence_ids": ["E-CST-04", "E-DER-05", "E-CST-02"], "kind": "support"}],
           "conditions": [], "would_change_my_mind": "A value above the $107.1M cost."}
    assert llm.parse("analyst", 0, raw, seats.visible("analyst", EV), set())


@pytest.mark.parametrize("falsifier,ok", [
    ("A survey showing the daylit share above 60%.", False),   # the live case: 60 matched the 60 ft street
    ("A daylight study showing habitable light past 50 ft.", True),
    ("A daylight study showing light across most of the plate.", True),
])
def test_falsifier_numbers_must_come_from_visible_evidence(falsifier, ok):
    raw = {**GOOD, "would_change_my_mind": falsifier}
    if ok:
        assert llm.parse("engineer", 0, raw, seats.visible("engineer", EV), set())
    else:
        with pytest.raises(ValueError, match="would_change_my_mind states 60%"):
            llm.parse("engineer", 0, raw, seats.visible("engineer", EV), set())


def test_the_buildings_own_street_number_is_not_an_invented_number():
    name = (HERO.name,)   # "210 Commerce Street (demo)"
    raw = {**GOOD, "claims": [{"text": "At 210 Commerce Street the deepest point is 153 ft from a window.",
                               "evidence_ids": ["E-GEO-02"], "kind": "risk"}]}
    vis = seats.visible("engineer", EV)
    assert llm.parse("engineer", 0, raw, vis, set(), name)
    with pytest.raises(ValueError, match="states 210"):
        llm.parse("engineer", 0, raw, vis, set())                     # without the name it is flagged
    raw["claims"][0]["text"] = "The plate is 210 ft deep."               # same number, not the address
    with pytest.raises(ValueError, match="states 210 ft"):
        llm.parse("engineer", 0, raw, vis, set(), name)


@pytest.mark.parametrize("target,msg", [("ANL-0-9", "not an earlier claim"), ("ENG-0-1", "your own claim"),
                                        (None, "without target_claim_id")])
def test_rebuttal_must_target_another_seats_real_claim(target, msg):
    raw = {**GOOD, "claims": [{"text": "Daylight stops at 50 ft.", "evidence_ids": ["E-AER-01"],
                               "kind": "rebuttal", "target_claim_id": target}]}
    with pytest.raises(ValueError, match=msg):
        llm.parse("engineer", 1, raw, seats.visible("engineer", EV), {"ANL-0-1", "ENG-0-1"})
