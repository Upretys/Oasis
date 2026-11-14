"""The three seats: what each sees, its mandate, and its prompt.

CLI:  python -m backend.seats --seat engineer --building hero --round 0
"""
import argparse
import json
from pathlib import Path

from backend import llm
from backend.contracts import Building, Evidence, Run, SeatId, Statement, StatementEvent
from backend.evidence import build_evidence
from backend.jurisdiction import load_building, place

TEMPLATE = (Path(__file__).parent / "prompts" / "seat.txt").read_text(encoding="utf-8")   # the doc's template

SEATS: dict[SeatId, dict] = {
    "engineer": {
        "name": "Structural Engineer",
        "weight": 1.0,
        "layers": ["geometry", "aerial"],
        "also": ["Column bay depth", "Depth beyond the daylight limit", "Column bays beyond the daylight limit"],
        "layers_text": "geometry and aerial facts, plus the CoStar column bay depth and the derived depth facts",
        "mandate": "Decide whether the floor plate can become habitable apartments. Judge daylight depth and "
                   "structure. Raise each geometry problem as a risk claim. Propose at least one physical "
                   "condition, such as a lightwell or removing bays, that cites your evidence.",
    },
    "planner": {
        "name": "City Planner",
        "weight": 1.0,
        "layers": ["zoning"],
        "also": ["Existing FAR"],
        "layers_text": "zoning facts, plus the existing FAR",
        "mandate": "Decide whether the published rules allow this conversion. Always state the conversion "
                   "pathway and its approval time. Propose at least one condition that is a filing, permit "
                   "or recorded obligation, citing the zoning evidence it rests on.",
    },
    "analyst": {
        "name": "Market Analyst",
        "weight": 1.0,
        "layers": ["costar"],
        "also": ["Conversion cost", "Residential value", "Rent uplift", "Net value impact", "Apartment-sized area"],
        "layers_text": "CoStar rents, cap rate and lease rows, plus the derived cost and value",
        "mandate": "Decide whether the conversion pays. Weigh cost against value and say when floors free up: "
                   "cite each lease expiry fact. Propose at least one contractual or financial condition, such "
                   "as a lease buyout, phasing or a price cap, citing your evidence.",
    },
}


def visible(seat: SeatId, evidence: list[Evidence]) -> list[Evidence]:
    cfg = SEATS[seat]
    return [e for e in evidence if e.layer in cfg["layers"] or e.label.startswith(tuple(cfg["also"]))]


def _evidence(building: Building) -> list[Evidence]:
    return build_evidence(building, place(building))


def build_system_prompt(seat: SeatId, building: Building) -> str:
    cfg = SEATS[seat]
    facts = [{"id": e.id, "label": e.label, "value": e.value, "unit": e.unit}
             for e in visible(seat, _evidence(building))]
    return (TEMPLATE.replace("{seat_name}", cfg["name"]).replace("{mandate}", cfg["mandate"])
            .replace("{layers}", cfg["layers_text"]).replace("{evidence_json}", json.dumps(facts, indent=1)))


CHECKLIST = ("Before you submit, check: claims under 220 characters, conditions and would_change_my_mind "
             "under 160. Every number you write is the value of an evidence ID cited in that same claim or "
             "condition; quote values as given and never compute ratios, sums or differences. "
             "Proposed fixes go in conditions, not claims.")


def build_user_prompt(building: Building, rnd: int, transcript: list[Statement]) -> str:
    head = f"Building: {building.name}. Round {rnd}."
    if not transcript:
        return f"{head} Give your opening statement.\n{CHECKLIST}"
    lines = [head + " Earlier statements; you may rebut or concede another seat's claim by its ID:"]
    for st in transcript:
        lines.append(f"- {st.seat}, round {st.round}: {st.verdict} (confidence {st.confidence})")
        lines += [f"  [{c.id}] {c.kind}: {c.text}" for c in st.claims]
    return "\n".join(lines + [CHECKLIST])


def run_seat(seat: SeatId, building: Building, rnd: int, transcript: list[Statement] = (),
             client=None) -> StatementEvent:
    return llm.call_statement(
        seat, rnd, build_system_prompt(seat, building), build_user_prompt(building, rnd, list(transcript)),
        visible(seat, _evidence(building)), prior_claim_ids={c.id for st in transcript for c in st.claims},
        building_id=building.building_id, names=(building.name,), client=client)


def golden_transcript(building_id: str, before_round: int) -> list[Statement]:
    run = Run.model_validate_json(llm.GOLDEN.read_text(encoding="utf-8"))
    if run.building_id != building_id:
        return []
    return [e.statement for e in run.events if isinstance(e, StatementEvent) and e.statement.round < before_round]


def main() -> None:
    p = argparse.ArgumentParser(description="Run one seat for one round and print its Statement.")
    p.add_argument("--seat", choices=list(SEATS), required=True)
    p.add_argument("--building", required=True)
    p.add_argument("--round", type=int, choices=[0, 1, 2], default=0)
    a = p.parse_args()
    transcript = golden_transcript(a.building, a.round)   # rounds 1-2 answer the golden earlier rounds
    if a.round and not transcript:
        p.error(f"no earlier rounds to answer for {a.building}; only round 0 runs standalone")
    event = run_seat(a.seat, load_building(a.building), a.round, transcript)
    print(event.statement.model_dump_json(indent=2))
    cited = sorted({i for c in event.statement.claims for i in c.evidence_ids}
                   | {i for c in event.statement.conditions for i in c.evidence_ids})
    print("cited evidence:", ", ".join(cited))
    print("fallback_used:", event.fallback_used)


if __name__ == "__main__":
    main()
