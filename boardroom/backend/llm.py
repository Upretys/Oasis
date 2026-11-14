"""call_statement: one forced tool call, validated by code, retried once, cached on disk, golden fallback.

Never logs the API key. No temperature: the current API rejects sampling parameters with a 400.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from backend.contracts import Evidence, Run, SeatId, Statement, StatementEvent

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
GOLDEN = HERE / "fixtures" / "golden_run.json"
TIMEOUT_S = 25
PFX = {"engineer": "ENG", "planner": "PLN", "analyst": "ANL"}
load_dotenv(HERE.parent / ".env")

TOOL = {"name": "submit_statement",
        "description": "Submit your statement for this round. Call exactly once.",
        "input_schema": Statement.model_json_schema()}
# These models reject a forced tool_choice (400); they get "auto" and the prompt's "call it exactly once".
NO_FORCED_TOOL = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2",
                  "claude-mythos-5-1", "claude-mythos-5-2"}


def model() -> str:
    return os.environ.get("BOARD_MODEL", "claude-haiku-5-5")


# --- validation: code decides what a seat may say ---------------------------------------------------

_IDS = re.compile(r"\b(?:E-[A-Z]{3}-\d+|[A-Z]{3}-\d-\d+)\b")
_NUM = re.compile(r"(\$?)(\d[\d,]*(?:\.\d+)?)\s*([MK]\b)?")
_UNIT_AFTER = [("pct", r"\s*(%|percent)"), ("ft", r"\s*-?(ft|feet|foot)\b"), ("sf", r"\s*(sf|sq)\b"),
               ("months", r"\s*-?months?\b"), ("years", r"\s*-?years?\b"), ("bays", r"\s*bays?\b"),
               ("far", r"\s*FAR\b")]
_FAR_BEFORE = re.compile(r"\bFAR\s*(of|at|above|below|under)?\s*$")
EVIDENCE_UNIT = {"%": "pct", "ft": "ft", "sf": "sf", "months": "months", "years": "years", "bays": "bays",
                 "FAR": "far", "$": "usd", "$/sf/yr": "usd", "$/sf/month": "usd"}
Number = tuple[float, str | None]   # (value, unit); unit None = not stated, matches any unit


def numerals(text: str) -> list[Number]:
    """Numbers in text with their unit, ignoring IDs: '$107.1M' -> (107100000, 'usd'), '60%' -> (60, 'pct')."""
    text, out = _IDS.sub("", text), []
    for m in _NUM.finditer(text):
        value = float(m.group(2).replace(",", "")) * {"M": 1e6, "K": 1e3}.get(m.group(3), 1)
        unit = "usd" if m.group(1) else next((u for u, p in _UNIT_AFTER if re.match(p, text[m.end():], re.I)), None)
        if unit is None and _FAR_BEFORE.search(text[:m.start()]):
            unit = "far"
        out.append((value, unit))
    return out


def _facts(cited: list[Evidence]) -> list[Number]:
    out = [(float(e.value), EVIDENCE_UNIT.get(e.unit)) for e in cited if not isinstance(e.value, str)]
    for e in cited:
        out += numerals(e.label) + (numerals(e.value) if isinstance(e.value, str) else [])
    return out


def _same(n: Number, fact: Number) -> bool:
    (v, u), (a, au) = n, fact
    return abs(v - a) <= max(0.5, 0.02 * abs(a)) and (u is None or au is None or u == au)


def _unsupported(text: str, cited: list[Evidence]) -> list[Number]:
    facts = _facts(cited)
    return [n for n in numerals(text) if n[0] > 3 and not any(_same(n, f) for f in facts)]   # 0-3: plain counts


def _show(n: Number) -> str:
    return f"{n[0]:.10g}" + {"pct": "%", "usd": " dollars", None: ""}.get(n[1], f" {n[1]}")


def _drop_addresses(text: str, names: tuple[str, ...]) -> str:
    """Remove street numbers that belong to the building's own name ('210 Commerce'), so they are not checked."""
    for name in names:
        for num, word in re.findall(r"\b(\d+)\s+([A-Za-z]+)", name):
            text = re.sub(rf"\b{num}\s+{word}\b", word, text, flags=re.I)
    return text


def check(seat: SeatId, st: Statement, visible: list[Evidence], prior_claim_ids: set[str],
          names: tuple[str, ...] = ()) -> list[str]:
    """Every rule the statement breaks; an empty list means valid."""
    by_id = {e.id: e for e in visible}
    errors = []
    items = [(c.id, c.text, c.evidence_ids) for c in st.claims] + \
            [(f"condition {i}", c.text, c.evidence_ids) for i, c in enumerate(st.conditions, 1)]
    for label, text, ids in items:
        text = _drop_addresses(text, names)
        if hidden := [i for i in ids if i not in by_id]:
            errors.append(f"{label} cites {hidden}: not in your evidence list")
        for n in _unsupported(text, [by_id[i] for i in ids if i in by_id]):
            holders = [e.id for e in visible if any(_same(n, f) and abs(n[0] - f[0]) <= 0.02 * abs(f[0])
                                                    for f in _facts([e]))]   # close match only, no rounding slack
            hint = (f"add {holders[0]} to its evidence_ids" if holders else
                    "remove it: no evidence holds it, and ratios or other arithmetic you compute are not evidence")
            errors.append(f"{label} states {_show(n)}, which its cited evidence does not contain: {hint}")
    for n in _unsupported(_drop_addresses(st.would_change_my_mind, names), visible):   # no citations: check against all it can see
        errors.append(f"would_change_my_mind states {_show(n)}, which none of your evidence holds: "
                      "use a value from your evidence or say it without a number")
    for c in st.claims:
        if c.kind == "rebuttal" and not c.target_claim_id:
            errors.append(f"{c.id} is a rebuttal without target_claim_id")
        if c.target_claim_id and c.target_claim_id not in prior_claim_ids:
            errors.append(f"{c.id} targets {c.target_claim_id}, which is not an earlier claim")
        elif c.target_claim_id and c.target_claim_id.startswith(PFX[seat]):
            errors.append(f"{c.id} targets your own claim {c.target_claim_id}; answer another seat")
    return errors


def parse(seat: SeatId, rnd: int, raw: dict | None, visible: list[Evidence], prior_claim_ids: set[str],
          names: tuple[str, ...] = ()) -> Statement:
    if raw is None:
        raise ValueError("you did not call submit_statement")
    try:
        st = Statement.model_validate({**raw, "seat": seat, "round": rnd})
    except ValidationError as e:
        raise ValueError(str(e)) from None
    for k, c in enumerate(st.claims, 1):
        c.id = f"{PFX[seat]}-{rnd}-{k}"            # the backend assigns claim IDs
    if errors := check(seat, st, visible, prior_claim_ids, names):
        raise ValueError("; ".join(errors))
    return st


# --- the call ------------------------------------------------------------------------------------------

def _create(messages: list[dict], system: str, client) -> list[dict]:
    """One API call, cached by sha256 of the full request."""
    m = model()
    req = {"model": m, "max_tokens": 4096, "system": system, "messages": messages, "tools": [TOOL]}
    if m in NO_FORCED_TOOL:
        req["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    else:
        req |= {"tool_choice": {"type": "tool", "name": TOOL["name"]}, "thinking": {"type": "disabled"}}
    path = CACHE / f"{hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if client is None:
        import anthropic
        client = anthropic.Anthropic(timeout=TIMEOUT_S)   # key comes from the environment
    content = [b for b in client.messages.create(**req).model_dump()["content"] if b["type"] in ("text", "tool_use")]
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(content, indent=1), encoding="utf-8")
    return content


def golden_statement(building_id: str, seat: SeatId, rnd: int) -> Statement:
    run = Run.model_validate_json(GOLDEN.read_text(encoding="utf-8"))
    for e in run.events:
        if run.building_id == building_id and isinstance(e, StatementEvent) \
                and (e.statement.seat, e.statement.round) == (seat, rnd):
            return e.statement
    raise LookupError(f"golden_run.json has no {seat} round {rnd} statement for {building_id}")


def call_statement(seat: SeatId, round: int, system: str, user: str, evidence_visible: list[Evidence],
                   prior_claim_ids: set[str] = frozenset(), building_id: str = "hero",
                   names: tuple[str, ...] = (),
                   client=None) -> StatementEvent:
    messages = [{"role": "user", "content": user}]
    try:
        for attempt in (1, 2):
            content = _create(messages, system, client)
            raw = next((b["input"] for b in content if b["type"] == "tool_use"), None)
            try:
                return StatementEvent(statement=parse(seat, round, raw, evidence_visible, prior_claim_ids, names))
            except ValueError as err:
                if attempt == 2:
                    raise
                print(f"[llm] {seat} r{round} rejected, retrying: {str(err)[:300]}")
                use = next((b for b in content if b["type"] == "tool_use"), None)
                fix = f"Rejected: {err}\nFix every problem and call submit_statement again."
                messages = messages + [{"role": "assistant", "content": content}, {"role": "user", "content":
                    [{"type": "tool_result", "tool_use_id": use["id"], "is_error": True, "content": fix}] if use else fix}]
    except Exception as err:   # invalid twice, no key, timeout: the demo must not break
        print(f"[llm] {seat} r{round} falling back to golden: {type(err).__name__}: {str(err)[:300]}")
    return StatementEvent(statement=golden_statement(building_id, seat, round), fallback_used=True)
