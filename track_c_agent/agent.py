"""
Track C - Agent Brain

Ties Track A (geometry) and Track B (value) together with real triage logic:
- Calls geometry first; skips the value calculation entirely if geometry
  fails hard (don't price something that can't be built)
- Treats "escalate" as a real third outcome, not a forced pass/fail
- Produces one combined, reasoned verdict per floor

Two ways to run this:
1. `run_pipeline(...)` - deterministic orchestration, no LLM needed. This is
   the actual decision logic and the thing to trust for correctness.
2. `run_agent_llm(...)` - wraps the same two functions as tools for Claude to
   call. Requires ANTHROPIC_API_KEY. Use this for the demo to show genuine
   tool-calling, but `run_pipeline` is what's actually being exercised either
   way - the LLM is choosing when to call it, not replacing its logic.
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


# ---------------------------------------------------------------------------
# LLM tool-calling wrapper (optional - needs ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "run_geometry",
        "description": (
            "Checks whether a floor's shape allows office-to-residential conversion "
            "(window-to-core depth). Cheap and local - always safe to call first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"floor_name": {"type": "integer"}},
            "required": ["floor_name"],
        },
    },
    {
        "name": "run_value",
        "description": (
            "Prices a conversion using real CoStar lease and comp data. Skip this "
            "if run_geometry already returned a hard fail - there's nothing to price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"floor_name": {"type": "integer"}, "convertible_sqft": {"type": "number"}},
            "required": ["floor_name", "convertible_sqft"],
        },
    },
]


class AgentError(Exception):
    """Raised with a message safe to show a user - never a raw SDK traceback."""


def run_agent_llm(property_id: int, floor_name: int, vexcel_response: dict, query_lat: float,
                   query_lon: float, real_floor_area_sqft: float, real_column_depth_ft: float | None) -> dict:
    """Lets Claude decide the tool-call sequence itself, rather than following
    the fixed order in run_pipeline(). Requires ANTHROPIC_API_KEY.

    Returns {"trace": [...], "final_text": str} - the trace is the actual
    sequence of the model's own text, which tool it chose to call and with
    what arguments, and the real tool result it got back. This is what makes
    it provably agentic for a demo: the UI can show the model's own decisions,
    not a pre-written Python string dressed up as reasoning."""
    import random
    import time

    import anthropic

    api_key = "".join(os.environ.get("ANTHROPIC_API_KEY", "").split())
    if not api_key:
        raise AgentError("ANTHROPIC_API_KEY is not set in this process's environment.")

    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)

    def call_run_geometry(args):
        try:
            geo = run_feasibility(
                floor_id=f"{property_id}-floor-{args['floor_name']}", vexcel_response=vexcel_response,
                query_lat=query_lat, query_lon=query_lon, real_floor_area_sqft=real_floor_area_sqft,
                real_column_depth_ft=real_column_depth_ft,
            )
            return json.dumps(asdict(geo), default=str)
        except Exception as e:
            # Hand the model a real error string instead of crashing the
            # whole run - a tool failing is something the agent should be
            # able to reason about and report, not something that should take
            # down the request.
            return json.dumps({"error": f"run_geometry failed: {e}"})

    def call_run_value(args):
        try:
            val = run_value(property_id=property_id, floor_name=args["floor_name"],
                             convertible_sqft=args["convertible_sqft"], query_lat=query_lat, query_lon=query_lon)
            return json.dumps(asdict(val), default=str)
        except Exception as e:
            return json.dumps({"error": f"run_value failed: {e}"})

    tool_fns = {"run_geometry": call_run_geometry, "run_value": call_run_value}

    def create_with_retry(**kwargs):
        """Retries only on genuinely transient failures (rate limit, server
        overload, connection blip) - never on a bad request or auth failure,
        which would just fail identically every time."""
        max_retries = 4
        for attempt in range(max_retries + 1):
            try:
                return client.messages.create(**kwargs)
            except anthropic.AuthenticationError as e:
                raise AgentError(f"Anthropic API rejected the key (401): {e}") from e
            except anthropic.PermissionDeniedError as e:
                raise AgentError(f"Anthropic API permission denied: {e}") from e
            except anthropic.BadRequestError as e:
                raise AgentError(f"Anthropic API rejected the request: {e}") from e
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                if attempt == max_retries:
                    raise AgentError(f"Anthropic API still failing after {max_retries} retries: {e}") from e
                wait = min(2 ** attempt, 20) + random.random()
                time.sleep(wait)
        raise AgentError("Unreachable")  # loop always returns or raises above

    messages = [{
        "role": "user",
        "content": (
            f"Evaluate property {property_id}, floor {floor_name} for office-to-residential "
            f"conversion feasibility. Call run_geometry first. Only call run_value if geometry "
            f"did not fail hard - use the geometry result's own viable_leasable_sqft field as "
            f"convertible_sqft, NOT convertible_fraction x floor area (convertible_fraction is the "
            f"raw geometric zone before real apartment-size filtering; viable_leasable_sqft already "
            f"excludes slivers too small/narrow to be a real studio or 1BR, so it's the honest number "
            f"to price). End with a one-paragraph verdict and dollar estimate, or a clear explanation "
            f"of why it was skipped."
        ),
    }]

    trace = []
    for _ in range(5):  # bounded loop, not infinite
        response = create_with_retry(
            model="claude-sonnet-5-5", max_tokens=1024, tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                trace.append({"type": "assistant_text", "text": block.text})
            elif block.type == "tool_use":
                trace.append({"type": "tool_call", "tool": block.name, "input": block.input})

        if response.stop_reason != "tool_use":
            final_text = "".join(b.text for b in response.content if b.type == "text")
            return {"trace": trace, "final_text": final_text}

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = tool_fns[block.name](block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                trace.append({"type": "tool_result", "tool": block.name, "result": json.loads(result)})
        messages.append({"role": "user", "content": tool_results})

    return {"trace": trace, "final_text": "Agent did not converge within the tool-call budget."}


if __name__ == "__main__":
    with open("output/property.json") as f:
        vexcel_data = json.load(f)

    result = run_pipeline(
        property_id=9008561, floor_name=37, vexcel_response=vexcel_data,
        query_lat=29.995495, query_lon=-95.579652,
        real_floor_area_sqft=23531.0, real_column_depth_ft=40.0,
    )
    print(json.dumps(asdict(result), indent=2))
