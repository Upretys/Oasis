"""Triage a building into pass / escalate / fail before the board convenes."""
from typing import Optional

from backend.contracts import Building, Jurisdiction, Verdict
from backend.jurisdiction import place

PASS_AT = 0.5
ESCALATE_AT = 0.2


def triage(building: Building, jurisdiction: Optional[Jurisdiction] = None) -> Verdict:
    j = jurisdiction or place(building)
    if j.conversion_pathway == "not_allowed":
        return "fail"
    if building.viability >= PASS_AT:
        return "pass"
    if building.viability >= ESCALATE_AT:
        return "escalate"
    return "fail"
