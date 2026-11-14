import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_c_agent"))
sys.path.insert(0, os.path.dirname(__file__))
from agent import run_pipeline
from dataclasses import asdict
from render_floor import BUILDINGS

results = []
base = os.path.join(os.path.dirname(__file__), "..", "output")
for b in BUILDINGS:
    path = os.path.join(base, b["vexcel_file"])
    with open(path) as f:
        vexcel_data = json.load(f)
    r = run_pipeline(
        property_id=b["property_id"], floor_name=b["floor_name"], vexcel_response=vexcel_data,
        query_lat=b["query_lat"], query_lon=b["query_lon"],
        real_floor_area_sqft=b["real_floor_area_sqft"], real_column_depth_ft=b["real_column_depth_ft"],
    )
    row = asdict(r)
    row["building_name"] = b["name"]
    row["lat"] = b["query_lat"]
    row["lon"] = b["query_lon"]
    row["image"] = b["name"].lower().replace(" ", "_") + "_render.png"
    results.append(row)
    print(f"{b['name']:<28} {r.geometry_verdict:<10} rent_comp see reasoning below")
    print("   ", r.reasoning_text[:160])

with open(os.path.join(os.path.dirname(__file__), "all_results.json"), "w") as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nsaved demo/all_results.json, {len(results)} buildings")
