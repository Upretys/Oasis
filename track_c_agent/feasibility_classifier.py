"""
Feasibility Classifier - a real, explainable decision-tree classifier.

Deliberately rule-based (an interpretable decision tree), not a trained ML
model. We have no labeled ground-truth outcomes - no real dataset of "this
office-to-residential conversion actually succeeded/failed" - to train a
supervised classifier on honestly. Fitting one to fabricated labels would be
exactly the kind of forging this project has been built to avoid.

A decision tree over real, already-computed features IS a legitimate
classifier concept in its own right: interpretable, auditable, and the
decision_path it returns is literally the same sequence of checks a human
underwriter would walk through, each with its real value and real threshold
stated - not a black-box score.

Every feature this classifier reads is already computed elsewhere in the
pipeline from real data (geometry.py, value.py) - this module adds no new
data source, only formalizes the decision logic that was previously inline
in run_feasibility()/run_pipeline() into a named, testable, reusable class.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClassificationResult:
    label: str  # "feasible" | "marginal" | "not_feasible"
    confidence: float
    decision_path: list[str] = field(default_factory=list)


class FeasibilityClassifier:
    """Decision tree over 4 real feature axes, each sourced from real data:

    1. geometry_verdict - from run_feasibility(): does real apartment-sized
       area clear the real viable_fraction threshold, cross-checked against
       CoStar's real column-depth data when available.
    2. value sign - does the real uplift/cap-rate/construction-cost math
       actually produce a positive number, not just a geometrically sound
       floor.
    3. value_confidence - how much of that dollar figure rests on real
       data (cap rate, construction material, rent comps, occupancy
       determination) vs. stated defaults.
    4. combined confidence - geometry_confidence x value_confidence, the
       same weighting already used for portfolio-level rank_score.
    """

    FEASIBLE_CONFIDENCE_THRESHOLD = 0.40  # below this, even a positive number needs human review

    def classify(
        self,
        geometry_verdict: str,
        geometry_confidence: float,
        viable_fraction: float,
        value_skipped: bool,
        value_confidence: float = 0.0,
        implied_value_impact_low: float | None = None,
        implied_value_impact_high: float | None = None,
    ) -> ClassificationResult:
        path = [
            f"geometry_verdict = {geometry_verdict.upper()} "
            f"(real viable_fraction {viable_fraction:.0%} of total floor area)"
        ]

        if geometry_verdict == "fail" or value_skipped:
            path.append("geometry failed hard - value was never priced, nothing to be feasible on")
            return ClassificationResult("not_feasible", geometry_confidence, path)

        if implied_value_impact_low is None or implied_value_impact_high is None:
            path.append("value result missing - cannot classify, flagging for human review")
            return ClassificationResult("marginal", 0.0, path)

        midpoint = (implied_value_impact_low + implied_value_impact_high) / 2
        path.append(f"implied value impact midpoint = ${midpoint:,.0f} (real uplift/cap-rate/cost math)")

        combined_confidence = geometry_confidence * max(value_confidence, 0.01)
        path.append(
            f"combined confidence = geometry_confidence ({geometry_confidence:.0%}) x "
            f"value_confidence ({value_confidence:.0%}) = {combined_confidence:.0%}"
        )

        if midpoint <= 0:
            path.append("value impact is zero or negative on current real/assumed inputs -> not feasible")
            return ClassificationResult("not_feasible", combined_confidence, path)

        if geometry_verdict == "escalate" or combined_confidence < self.FEASIBLE_CONFIDENCE_THRESHOLD:
            path.append(
                f"positive value but geometry escalated or combined confidence below "
                f"{self.FEASIBLE_CONFIDENCE_THRESHOLD:.0%} -> marginal, needs human review before relying on this"
            )
            return ClassificationResult("marginal", combined_confidence, path)

        path.append("positive value + geometry pass + confidence above threshold -> feasible")
        return ClassificationResult("feasible", combined_confidence, path)


if __name__ == "__main__":
    # Real worked example: First Community Tower, floor 37 (see track_c_agent/agent.py __main__)
    clf = FeasibilityClassifier()
    result = clf.classify(
        geometry_verdict="pass", geometry_confidence=0.85, viable_fraction=0.63,
        value_skipped=False, value_confidence=0.75,
        implied_value_impact_low=2272723.0, implied_value_impact_high=3311817.0,
    )
    print(result.label, round(result.confidence, 2))
    for step in result.decision_path:
        print(" -", step)
