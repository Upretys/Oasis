"""Seat agent tests. Offline: a fake client stands in for the API."""
import copy

import pytest

from backend import llm, seats
from backend.contracts import Run, StatementEvent
from backend.evidence import build_evidence
from backend.jurisdiction import FIXTURES, load_building, place

HERO = load_building("hero")
EV = build_evidence(HERO, place(HERO))

GOOD = {"verdict": "escalate", "confidence": 0.7, "changed_mind": False,
        "claims": [{"text": "The deepest point is 153 ft from a window; daylight reaches only 50 ft.",
                    "evidence_ids": ["E-GEO-02", "E-AER-01"], "kind": "risk"}],
        "conditions": [{"text": "Cut a rear lightwell 3 bays deep", "evidence_ids": ["E-DER-02"]}],
        "would_change_my_mind": "A daylight study showing light past 50 ft."}
BAD = {**GOOD, "claims": [{"text": "Zoning is fine.", "evidence_ids": ["E-ZON-01"], "kind": "support"}]}


class FakeClient:
    def __init__(self, *inputs):
        self.inputs, self.requests = list(inputs), []
        self.messages = self

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        item = self.inputs.pop(0)
        if isinstance(item, Exception):
            raise item
        block = {"type": "tool_use", "id": f"toolu_{len(self.requests)}", "name": "submit_statement", "input": item}
        return type("Resp", (), {"model_dump": lambda self: {"content": [block]}})()


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llm")
    monkeypatch.setattr(seats, "STATEMENTS", tmp_path / "statements")
    monkeypatch.setenv("BOARD_MODEL", "claude-haiku-5-5")


def ids(seat):
    return {e.id for e in seats.visible(seat, EV)}


def test_each_seat_sees_only_its_layers():
    assert ids("planner") == {e.id for e in EV if e.layer == "zoning"} | {"E-DER-03"}
    assert not any(i.startswith("E-ZON") for i in ids("engineer") | ids("analyst"))
    assert {"E-CST-01", "E-DER-01", "E-DER-02"} <= ids("engineer")
    assert {"E-DER-04", "E-DER-05"} <= ids("analyst") and "E-DER-01" not in ids("analyst")
    assert ids("engineer") | ids("planner") | ids("analyst") == {e.id for e in EV}


def test_system_prompt_lists_only_visible_evidence():
    text = seats.system_prompt("planner", EV)
    assert "Market Analyst" not in text and "{" + "mandate}" not in text
    assert all(i in text for i in ids("planner")) and "E-GEO-02" not in text


def test_golden_statements_pass_the_same_checks():
    run = Run.model_validate_json((FIXTURES / "golden_run.json").read_text(encoding="utf-8"))
    prior = set()
    for e in run.events:
        if isinstance(e, StatementEvent):
            assert seats.check(e.statement.seat, e.statement, EV, prior) == [], e.statement.seat
            prior |= {c.id for c in e.statement.claims}


def test_valid_call_assigns_ids_and_is_cached():
    fake = FakeClient(GOOD)
    ev = seats.speak("hero", "engineer", 0, EV, [], fake)
    assert not ev.fallback_used and ev.statement.claims[0].id == "ENG-0-1"
    assert fake.requests[0]["tool_choice"] == {"type": "tool", "name": "submit_statement"}
    again = seats.speak("hero", "engineer", 0, EV, [], FakeClient())   # empty fake: must not be called
    assert again.statement == ev.statement


def test_invalid_call_is_retried_with_the_error():
    fake = FakeClient(BAD, GOOD)
    ev = seats.speak("hero", "engineer", 0, EV, [], fake)
    assert not ev.fallback_used and len(fake.requests) == 2
    feedback = fake.requests[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and feedback["is_error"] and "E-ZON-01" in feedback["content"]


def test_two_failures_fall_back_to_golden():
    ev = seats.speak("hero", "analyst", 0, EV, [], FakeClient(BAD, BAD))
    assert ev.fallback_used and ev.statement.seat == "analyst" and ev.statement.claims[0].id == "ANL-0-1"


def test_api_error_falls_back():
    ev = seats.speak("hero", "planner", 1, EV, [], FakeClient(RuntimeError("network down")))
    assert ev.fallback_used and ev.statement.round == 1


def test_invented_number_is_rejected():
    raw = {**GOOD, "claims": [{"text": "Conversion would cost $90M.", "evidence_ids": ["E-DER-04"], "kind": "risk"}]}
    with pytest.raises(ValueError, match="numbers"):
        seats.parse("analyst", 0, raw, EV, set())


def test_rebuttal_must_target_an_earlier_claim():
    raw = {**GOOD, "claims": [{"text": "Daylight stops at 50 ft.", "evidence_ids": ["E-AER-01"],
                               "kind": "rebuttal", "target_claim_id": "ANL-0-9"}]}
    with pytest.raises(ValueError, match="not an earlier claim"):
        seats.parse("engineer", 1, raw, EV, {"ANL-0-1"})
    with pytest.raises(ValueError, match="needs target_claim_id"):
        seats.parse("engineer", 1, {**raw, "claims": [{**raw["claims"][0], "target_claim_id": None}]}, EV, set())
