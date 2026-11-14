"""A seat speaks once per round: render the prompt, force one tool call, validate, retry once, fall back."""
import json
import re
from pathlib import Path

from pydantic import ValidationError

from backend import llm
from backend.contracts import Evidence, Run, SeatId, Statement, StatementEvent

ROOT = Path(__file__).resolve().parent
PROMPT = (ROOT / "prompts" / "seat.txt").read_text(encoding="utf-8")
GOLDEN = ROOT / "fixtures" / "golden_run.json"
STATEMENTS = llm.ROOT / "cache" / "statements"   # last valid statement per building/seat/round
PFX = {"engineer": "ENG", "planner": "PLN", "analyst": "ANL"}

SEATS = {
    "engineer": dict(
        name="Structural Engineer",
        mandate="Decide whether the floor plate can become habitable apartments: daylight depth, structure, "
                "and the physical changes that would fix it.",
        layers="geometry and aerial facts, plus the CoStar column depth and the derived depth facts",
        sees=({"geometry", "aerial"}, ("Column bay depth", "Depth beyond", "Column bays beyond"))),
    "planner": dict(
        name="City Planner",
        mandate="Decide whether the published rules allow this conversion: pathway, approval time, "
                "bulk limits and obligations.",
        layers="zoning facts, plus the existing FAR",
        sees=({"zoning"}, ("Existing FAR",))),
    "analyst": dict(
        name="Market Analyst",
        mandate="Decide whether the conversion pays: cost against value, rents and lease timing.",
        layers="CoStar facts, plus the derived cost and value",
        sees=({"costar"}, ("Conversion cost", "Residential value"))),
}

TOOL = {
    "name": "submit_statement",
    "description": "Submit your statement for this round. Call exactly once.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "confidence", "claims", "conditions", "would_change_my_mind", "changed_mind"],
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "escalate", "fail"]},
            "confidence": {"type": "number", "description": "0 to 1"},
            "claims": {"type": "array", "description": "1 to 4 claims", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["text", "evidence_ids", "kind"],
                "properties": {
                    "text": {"type": "string", "description": "under 220 characters"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": ["support", "risk", "rebuttal", "concession"]},
                    "target_claim_id": {"type": "string", "description": "claim you answer (rebuttal/concession)"},
                }}},
            "conditions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "evidence_ids"],
                "properties": {"text": {"type": "string", "description": "under 160 characters"},
                               "evidence_ids": {"type": "array", "items": {"type": "string"}}}}},
            "would_change_my_mind": {"type": "string", "description": "under 160 characters"},
            "changed_mind": {"type": "boolean", "description": "true only if your verdict changed this round"},
        },
    },
}


def visible(seat: SeatId, evidence: list[Evidence]) -> list[Evidence]:
    layers, labels = SEATS[seat]["sees"]
    return [e for e in evidence if e.layer in layers or e.label.startswith(labels)]


def system_prompt(seat: SeatId, evidence: list[Evidence]) -> str:
    facts = [{"id": e.id, "label": e.label, "value": e.value, "unit": e.unit} for e in visible(seat, evidence)]
    s = SEATS[seat]
    return (PROMPT.replace("{seat_name}", s["name"]).replace("{mandate}", s["mandate"])
            .replace("{layers}", s["layers"]).replace("{evidence_json}", json.dumps(facts, indent=1)))


def user_prompt(rnd: int, transcript: list[Statement]) -> str:
    if not transcript:
        return "Round 0. Give your opening statement."
    lines = [f"Round {rnd}. Earlier statements (you may rebut or concede these claim IDs):"]
    for st in transcript:
        lines.append(f"- {st.seat}, round {st.round}: {st.verdict} (confidence {st.confidence})")
        lines += [f"  [{c.id}] {c.kind}: {c.text}" for c in st.claims]
    return "\n".join(lines)


# --- validation: code decides what a seat is allowed to say --------------------------------------------

_NUM = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)\s*([MK]\b)?")
_IDS = re.compile(r"\b(?:E-[A-Z]{3}-\d+|[A-Z]{3}-\d-\d+)\b")


def _numbers(text: str) -> list[float]:
    out = []
    for raw, suffix in _NUM.findall(_IDS.sub("", text)):
        v = float(raw.replace(",", ""))
        out.append(v * {"M": 1e6, "K": 1e3}.get(suffix, 1))
    return out


def _allowed(cited: list[Evidence]) -> list[float]:
    vals = [float(e.value) for e in cited if isinstance(e.value, (int, float))]
    for e in cited:
        vals += _numbers(e.label) + (_numbers(e.value) if isinstance(e.value, str) else [])
    return vals


def _numbers_ok(text: str, cited: list[Evidence]) -> list[float]:
    """Numbers in text that no cited fact supports (small counts 0-3 are allowed)."""
    allowed = _allowed(cited)
    return [n for n in _numbers(text) if n > 3 and not any(abs(n - a) <= max(0.5, 0.02 * abs(a)) for a in allowed)]


def check(seat: SeatId, st: Statement, evidence: list[Evidence], prior_ids: set[str]) -> list[str]:
    """Return every rule the statement breaks (empty list = valid)."""
    by_id = {e.id: e for e in visible(seat, evidence)}
    errors = []
    items = [(c.id, c.text, c.evidence_ids) for c in st.claims] + \
            [(f"condition {i + 1}", c.text, c.evidence_ids) for i, c in enumerate(st.conditions)]
    for label, text, ids in items:
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            errors.append(f"{label}: cites {unknown}, which are not in your evidence list")
        bad = _numbers_ok(text, [by_id[i] for i in ids if i in by_id])
        if bad:
            errors.append(f"{label}: numbers {bad} do not come from its cited evidence")
    for c in st.claims:
        if c.kind == "rebuttal" and not c.target_claim_id:
            errors.append(f"{c.id}: a rebuttal needs target_claim_id")
        if c.target_claim_id and c.target_claim_id not in prior_ids:
            errors.append(f"{c.id}: target_claim_id {c.target_claim_id} is not an earlier claim")
    if st.round == 0 and st.changed_mind:
        errors.append("changed_mind must be false in round 0")
    return errors


def parse(seat: SeatId, rnd: int, raw: dict | None, evidence: list[Evidence], prior_ids: set[str]) -> Statement:
    if raw is None:
        raise ValueError("no submit_statement call was made")
    try:
        st = Statement.model_validate({**raw, "seat": seat, "round": rnd})
    except ValidationError as e:
        raise ValueError(str(e)) from None
    for k, c in enumerate(st.claims, 1):
        c.id = f"{PFX[seat]}-{rnd}-{k}"   # the backend assigns claim IDs
    errors = check(seat, st, evidence, prior_ids)
    if errors:
        raise ValueError("; ".join(errors))
    return st


# --- speak: one model call, one retry with the error, then fallback ------------------------------------

def _fallback(building_id: str, seat: SeatId, rnd: int) -> Statement:
    cached = STATEMENTS / f"{building_id}_{seat}_{rnd}.json"
    if cached.exists():
        return Statement.model_validate_json(cached.read_text(encoding="utf-8"))
    run = Run.model_validate_json(GOLDEN.read_text(encoding="utf-8"))
    if run.building_id == building_id:
        for e in run.events:
            if isinstance(e, StatementEvent) and (e.statement.seat, e.statement.round) == (seat, rnd):
                return e.statement
    raise LookupError(f"no fallback statement for {building_id}/{seat}/round {rnd}")


def speak(building_id: str, seat: SeatId, rnd: int, evidence: list[Evidence],
          transcript: list[Statement], client=None) -> StatementEvent:
    system = system_prompt(seat, evidence)
    prior_ids = {c.id for st in transcript for c in st.claims}
    messages = [{"role": "user", "content": user_prompt(rnd, transcript)}]
    try:
        for attempt in range(2):
            content = llm.call_tool(system, messages, TOOL, client)
            try:
                st = parse(seat, rnd, llm.tool_input(content, TOOL["name"]), evidence, prior_ids)
            except ValueError as err:
                if attempt == 1:
                    raise
                use = next((b for b in content if b["type"] == "tool_use"), None)
                feedback = f"Rejected: {err}. Fix every problem and call submit_statement again."
                messages += [{"role": "assistant", "content": content}, {"role": "user", "content": (
                    [{"type": "tool_result", "tool_use_id": use["id"], "is_error": True, "content": feedback}]
                    if use else feedback)}]
                continue
            STATEMENTS.mkdir(parents=True, exist_ok=True)
            (STATEMENTS / f"{building_id}_{seat}_{rnd}.json").write_text(st.model_dump_json(indent=2),
                                                                        encoding="utf-8")
            return StatementEvent(statement=st)
    except Exception as err:   # invalid twice, no key, network error: never break the demo
        print(f"[seats] {seat} round {rnd} fell back: {type(err).__name__}: {str(err)[:200]}")
    return StatementEvent(statement=_fallback(building_id, seat, rnd), fallback_used=True)
