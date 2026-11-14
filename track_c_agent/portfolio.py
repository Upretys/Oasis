"""
Portfolio ranking - runs run_pipeline() across multiple real buildings and
ranks them. No new data sources, no fabricated numbers - just aggregating
real per-building results from Track A/B/C into one shortlist, the way a
developer or city would actually want to see a submarket screened.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))
from agent import run_pipeline  # noqa: E402


def rank_buildings(candidates: list[dict]) -> list[dict]:
    """Each candidate dict must carry: property_id, floor_name, vexcel_response
    (already-loaded real JSON, not a path), query_lat, query_lon,
    real_floor_area_sqft, real_column_depth_ft (None if not available - never
    fabricated). Ranks by confidence-weighted value impact midpoint; a 'fail'
    verdict always sorts last regardless of any dollar figure, since nothing
    was priced for it."""
    results = []
    for c in candidates:
        r = run_pipeline(
            property_id=c["property_id"], floor_name=c["floor_name"],
            vexcel_response=c["vexcel_response"], query_lat=c["query_lat"], query_lon=c["query_lon"],
            real_floor_area_sqft=c["real_floor_area_sqft"], real_column_depth_ft=c.get("real_column_depth_ft"),
        )
        row = asdict(r)
        row["building_name"] = c.get("building_name", f"Property {c['property_id']}")
        if row["value_skipped"]:
            row["rank_score"] = float("-inf")
        else:
            midpoint = (row["implied_value_impact_low"] + row["implied_value_impact_high"]) / 2
            row["rank_score"] = midpoint * row["geometry_confidence"]
        results.append(row)

    results.sort(key=lambda r: r["rank_score"], reverse=True)
    return results


def print_shortlist(ranked: list[dict]) -> None:
    print(f"{'Rank':<5}{'Building':<22}{'Floor':<7}{'Verdict':<10}{'Conf':<6}{'Value impact (mid)':<20}")
    for i, r in enumerate(ranked, 1):
        if r["value_skipped"]:
            mid_str = "skipped (geometry fail)"
        else:
            mid = (r["implied_value_impact_low"] + r["implied_value_impact_high"]) / 2
            mid_str = f"${mid:,.0f}"
        floor = r["floor_id"].split("-floor-")[-1]
        print(f"{i:<5}{r['building_name']:<22}{floor:<7}{r['geometry_verdict']:<10}{r['geometry_confidence']:<6.0%}{mid_str:<20}")


if __name__ == "__main__":
    base = os.path.join(os.path.dirname(__file__), "..", "output")

    with open(os.path.join(base, "property.json")) as f:
        fct_vexcel = json.load(f)
    with open(os.path.join(base, "property_29.7375702_-95.461339.json")) as f:
        williams_vexcel = json.load(f)

    candidates = [
        {
            "building_name": "First Community Tower", "property_id": 9008561, "floor_name": 37,
            "vexcel_response": fct_vexcel, "query_lat": 29.995495, "query_lon": -95.579652,
            "real_floor_area_sqft": 23531.0, "real_column_depth_ft": 40.0,
        },
        {
            "building_name": "Williams Tower", "property_id": 233903, "floor_name": 18,
            "vexcel_response": williams_vexcel, "query_lat": 29.7375702, "query_lon": -95.461339,
            "real_floor_area_sqft": 22700.0, "real_column_depth_ft": None,
        },
    ]

    # Third building (TC Energy Center) added automatically once the file exists.
    tc_path = os.path.join(base, "property_29.7607687_-95.366467.json")
    if os.path.exists(tc_path):
        with open(tc_path) as f:
            tc_vexcel = json.load(f)
        candidates.append({
            "building_name": "TC Energy Center", "property_id": 233905, "floor_name": 24,
            "vexcel_response": tc_vexcel, "query_lat": 29.7607687, "query_lon": -95.366467,
            "real_floor_area_sqft": 22875.0, "real_column_depth_ft": None,
        })

    ranked = rank_buildings(candidates)
    print_shortlist(ranked)
    print()
    print(json.dumps(ranked, indent=2, default=str))
