"""
Portfolio scan - the real innovation layer.

Runs the real pipeline across a BLIND batch of real Houston office buildings
(not the ones we already know are vacant) to prove the actual capability:
screening a city's office stock automatically, at a scale no analyst does by
hand, to surface hidden conversion opportunities nobody flagged.

Live Vexcel calls for every building not already cached - real network
requests, real footprints, real results. Saves to demo/portfolio_scan.json
so the web demo can show it instantly instead of re-running live on stage.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_a_geometry"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_b_value"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_c_agent"))

from vexcel_live import fetch_vexcel_property, VexcelNoPropertyData, VexcelKeyMissing  # noqa: E402
from value import list_houston_office_buildings_broad  # noqa: E402
from agent import run_pipeline  # noqa: E402
from render_floor import BUILDINGS as ALREADY_TESTED  # noqa: E402

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
SCAN_PATH = os.path.join(os.path.dirname(__file__), "portfolio_scan.json")

ALREADY_TESTED_IDS = {b["property_id"] for b in ALREADY_TESTED}


def run_scan(max_new: int = 25):
    candidates = list_houston_office_buildings_broad(min_stories=3, limit=300)
    blind = [c for c in candidates if c["property_id"] not in ALREADY_TESTED_IDS][:max_new]
    print(f"Scanning {len(blind)} real, previously-untested Houston office buildings...")

    results = []
    # Carry forward the 10 already-tested buildings so the final map is complete.
    for b in ALREADY_TESTED:
        path = os.path.join(OUTPUT_DIR, b["vexcel_file"])
        try:
            with open(path) as f:
                vexcel_data = json.load(f)
        except FileNotFoundError:
            continue
        r = run_pipeline(
            property_id=b["property_id"], floor_name=b["floor_name"], vexcel_response=vexcel_data,
            query_lat=b["query_lat"], query_lon=b["query_lon"],
            real_floor_area_sqft=b["real_floor_area_sqft"], real_column_depth_ft=b["real_column_depth_ft"],
        )
        results.append({
            "property_id": b["property_id"], "name": b["name"], "latitude": b["query_lat"], "longitude": b["query_lon"],
            "verdict": r.geometry_verdict, "value_skipped": r.value_skipped,
            "value_impact_low": r.implied_value_impact_low, "value_impact_high": r.implied_value_impact_high,
            "already_tested": True,
        })

    for i, c in enumerate(blind):
        name, pid, lat, lon = c["name"], c["property_id"], c["latitude"], c["longitude"]
        print(f"[{i+1}/{len(blind)}] {name} ({pid}) at ({lat}, {lon})...")
        try:
            vexcel_data = fetch_vexcel_property(lat, lon)
        except VexcelNoPropertyData:
            print("   no Vexcel data - skipping")
            continue
        except VexcelKeyMissing as e:
            print("   ABORT:", e)
            break
        except Exception as e:
            print("   fetch error:", repr(e), "- skipping")
            continue

        cache_name = f"property_{lat}_{lon}.json"
        try:
            with open(os.path.join(OUTPUT_DIR, cache_name), "w") as f:
                json.dump(vexcel_data, f)
        except OSError:
            pass

        floor_name = c["floor_names_available"][0]
        try:
            r = run_pipeline(
                property_id=pid, floor_name=floor_name, vexcel_response=vexcel_data,
                query_lat=lat, query_lon=lon, real_floor_area_sqft=c["typical_floor_sqft"],
                real_column_depth_ft=c["column_depth_ft"],
            )
        except Exception as e:
            print("   pipeline error:", repr(e), "- skipping")
            continue

        print(f"   verdict={r.geometry_verdict} value=({r.implied_value_impact_low}, {r.implied_value_impact_high})")
        results.append({
            "property_id": pid, "name": name, "latitude": lat, "longitude": lon,
            "verdict": r.geometry_verdict, "value_skipped": r.value_skipped,
            "value_impact_low": r.implied_value_impact_low, "value_impact_high": r.implied_value_impact_high,
            "already_tested": False,
        })
        time.sleep(0.3)  # be polite to the real API

    with open(SCAN_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} results to {SCAN_PATH}")

    positive = [r for r in results if not r["value_skipped"] and r["value_impact_low"] is not None and r["value_impact_low"] > 0]
    print(f"\n{len(positive)} real hidden opportunities found (positive value impact):")
    for r in sorted(positive, key=lambda x: x["value_impact_low"], reverse=True):
        print(f"  {r['name']:<30} +${r['value_impact_low']:,.0f} to +${r['value_impact_high']:,.0f}")


if __name__ == "__main__":
    run_scan()
