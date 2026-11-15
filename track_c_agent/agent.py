"""
Track C - Agent Brain

Ties Track A (geometry) and Track B (value) together with real triage logic:
- Calls geometry first; skips the value calculation entirely if geometry
  fails hard (don't price something that can't be built)
- Treats "escalate" as a real third outcome, not a forced pass/fail
- Produces one combined, reasoned verdict per floor

"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_a_geometry"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_b_value"))

from geometry import run_feasibility  # noqa: E402
from value import run_value, check_building_constraints  # noqa: E402


@dataclass
class PipelineResult:
    floor_id: str
    geometry_verdict: str
    geometry_confidence: float
    window_to_core_max_ft: float
    convertible_fraction: float
    value_skipped: bool
    value_uplift_low: float | None
    value_uplift_high: float | None
    implied_value_impact_low: float | None
    implied_value_impact_high: float | None
    reasoning_text: str
    notes: list[str]


def run_pipeline(
    property_id: int,
    floor_name: int,
    vexcel_response: dict,
    query_lat: float,
    query_lon: float,
    real_floor_area_sqft: float,
    real_column_depth_ft: float | None,
) -> PipelineResult:
    """The real triage decision. Geometry runs first and is cheap (local
    computation). Value requires reading CoStar's lease/comp files, so it's
    skipped entirely when geometry has already failed hard - a real decision,
    not a fixed sequence."""

    floor_id = f"{property_id}-floor-{floor_name}"
    geo = run_feasibility(
        floor_id=floor_id,
        vexcel_response=vexcel_response,
        query_lat=query_lat, query_lon=query_lon,
        real_floor_area_sqft=real_floor_area_sqft,
        real_column_depth_ft=real_column_depth_ft,
    )

    notes = list(geo.notes)

    if geo.verdict == "fail":
        reasoning = (
            f"Geometry fails outright ({geo.window_to_core_max_ft:.1f}ft depth, "
            f"threshold {50.0:.0f}ft) - skipping the value calculation, no point "
            f"pricing a floor that can't be converted as shaped."
        )
        return PipelineResult(
            floor_id=floor_id, geometry_verdict=geo.verdict, geometry_confidence=geo.confidence,
            window_to_core_max_ft=geo.window_to_core_max_ft, convertible_fraction=geo.convertible_fraction,
            value_skipped=True, value_uplift_low=None, value_uplift_high=None,
            implied_value_impact_low=None, implied_value_impact_high=None,
            reasoning_text=reasoning, notes=notes,
        )

    # Use real, apartment-sized viable square footage - not the raw geometric
    # convertible fraction, which includes slivers too small/narrow to
    # actually fit a kitchen+bath+bedroom. This is what makes the dollar
    # figure trustworthy rather than optimistic.
    convertible_sqft = geo.viable_leasable_sqft
    val = run_value(property_id=property_id, floor_name=floor_name, convertible_sqft=convertible_sqft,
                     query_lat=query_lat, query_lon=query_lon)
    notes.extend(val.notes)

    unit_mix = ", ".join(f"{count} {t}" for t, count in geo.unit_type_counts.items()) or "no viable units"
    constraints = check_building_constraints(property_id)
    # Correction: Houston has no citywide zoning (confirmed - City of
    # Houston Planning Dept's own "No Zoning Letter"). CoStar's
    # zoningDescription is not a real conversion gate here; the real one is
    # deed restrictions, which we have no data source for. Say so honestly
    # instead of flagging a zoning change that isn't the real-world blocker.
    notes.append(
        "Houston has no citywide zoning - CoStar's zoning field isn't a real gate here. "
        "The real constraint is private deed restrictions, which aren't in this dataset."
    )
    if constraints.parking_flag == "likely_short":
        notes.append(f"Parking ratio ({constraints.parking_ratio}/1000sf) may be short for residential use.")
    notes.append(constraints.column_spacing_note)

    reasoning = (
        f"Geometry {geo.verdict} ({geo.window_to_core_max_ft:.1f}ft depth, "
        f"confidence {geo.confidence:.0%}). Real unit mix: {unit_mix} "
        f"({geo.viable_leasable_sqft:,.0f} sqft of actual apartment-sized space, not the raw convertible zone). "
        f"{val.reasoning_text}"
    )
    if geo.verdict == "escalate":
        reasoning += " Flagging for human review rather than forcing a pass/fail."

    return PipelineResult(
        floor_id=floor_id, geometry_verdict=geo.verdict, geometry_confidence=geo.confidence,
        window_to_core_max_ft=geo.window_to_core_max_ft, convertible_fraction=geo.convertible_fraction,
        value_skipped=False,
        value_uplift_low=round(val.value_uplift_low, 0), value_uplift_high=round(val.value_uplift_high, 0),
        implied_value_impact_low=round(val.implied_value_impact_low, 0),
        implied_value_impact_high=round(val.implied_value_impact_high, 0),
        reasoning_text=reasoning, notes=notes,
    )


if __name__ == "__main__":
    with open("output/property.json") as f:
        vexcel_data = json.load(f)

    result = run_pipeline(
        property_id=9008561, floor_name=37, vexcel_response=vexcel_data,
        query_lat=29.995495, query_lon=-95.579652,
        real_floor_area_sqft=23531.0, real_column_depth_ft=40.0,
    )
    print(json.dumps(asdict(result), indent=2))
