"""
Live interactive demo server.

Two kinds of buildings, both run through the exact same real pipeline
(run_pipeline / run_feasibility / run_value - nothing special-cased for the
demo):

  - "cached": the 7 buildings already tested tonight. Vexcel's response is
    read from a saved output/*.json file (no new network call) so these are
    instant.
  - "live": any other real Houston office building from the CoStar kit. Its
    Vexcel footprint has never been fetched before - this hits the real
    Vexcel /property/extract endpoint over the network, live, in front of
    whoever is watching. Requires VEXCEL_API_KEY in this terminal's
    environment; if it's missing, the UI says so honestly instead of faking
    a result.

Run: COSTAR_KIT_PATH=... VEXCEL_API_KEY=... .venv/bin/python3 demo/webapp.py
Then open http://127.0.0.1:5050
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_a_geometry"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_b_value"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_c_agent"))

from flask import Flask, jsonify, request, send_from_directory

from vexcel_live import fetch_vexcel_property, VexcelKeyMissing, VexcelNoPropertyData  # noqa: E402
from value import list_houston_office_buildings_broad  # noqa: E402
from render_floor import render, BUILDINGS as CACHED_BUILDINGS  # noqa: E402
from agent import run_agent_llm  # noqa: E402

app = Flask(__name__, static_folder=None)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
CACHED_BY_ID = {b["property_id"]: b for b in CACHED_BUILDINGS}
PORTFOLIO_SCAN_PATH = os.path.join(os.path.dirname(__file__), "portfolio_scan.json")


@app.route("/api/portfolio_scan")
def api_portfolio_scan():
    """Pre-screened, real results (demo/portfolio_scan.py) across a blind
    batch of real Houston office buildings - not cherry-picked, not run live
    on stage (that would take minutes of real network calls per building).
    Served instantly here; the run itself was real and is reproducible."""
    try:
        with open(PORTFOLIO_SCAN_PATH) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify([])


@app.route("/")
def index():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), "index.html")


@app.route("/api/buildings")
def api_buildings():
    cached = [{
        "property_id": b["property_id"], "name": b["name"], "source": "cached",
        "floor_name": b["floor_name"], "latitude": b["query_lat"], "longitude": b["query_lon"],
        "typical_floor_sqft": b["real_floor_area_sqft"], "column_depth_ft": b["real_column_depth_ft"],
    } for b in CACHED_BUILDINGS]

    live = []
    try:
        broad = list_houston_office_buildings_broad()
        for c in broad:
            if c["property_id"] in CACHED_BY_ID:
                continue  # already covered by the cached list above
            live.append({
                "property_id": c["property_id"], "name": c["name"], "source": "live",
                "floor_name": c["floor_names_available"][0],
                "latitude": c["latitude"], "longitude": c["longitude"],
                "typical_floor_sqft": c["typical_floor_sqft"], "column_depth_ft": c["column_depth_ft"],
                "address": c["address"], "stories": c["stories"],
            })
    except FileNotFoundError as e:
        pass  # COSTAR_KIT_PATH not reachable - cached list still works

    return jsonify({"cached": cached, "live": live, "vexcel_key_set": bool("".join(os.environ.get("VEXCEL_API_KEY", "").split()))})


def _resolve_vexcel(source, property_id, lat, lon, t0, timeline):
    """Shared by both endpoints: load the cached Vexcel response, or fetch it
    live and cache it for next time. Returns None (with a jsonify error tuple
    already written to timeline's caller) on failure - callers check first."""
    if source == "cached":
        b = CACHED_BY_ID.get(property_id)
        vexcel_path = os.path.join(OUTPUT_DIR, b["vexcel_file"])
        with open(vexcel_path) as f:
            vexcel_response = json.load(f)
        timeline.append({"step": f"Loaded cached Vexcel footprint for {b['name']} (already fetched tonight)",
                          "elapsed_s": round(time.time() - t0, 2)})
        return vexcel_response, None

    try:
        vexcel_response = fetch_vexcel_property(lat, lon)
    except VexcelKeyMissing as e:
        return None, (jsonify({"error": "vexcel_key_missing", "message": str(e)}), 400)
    except VexcelNoPropertyData as e:
        return None, (jsonify({"error": "no_property_data", "message": str(e)}), 404)
    timeline.append({"step": f"Live Vexcel /property/extract call at ({lat}, {lon}) - real-time network request",
                      "elapsed_s": round(time.time() - t0, 2)})
    cache_name = f"property_{lat}_{lon}.json"
    try:
        with open(os.path.join(OUTPUT_DIR, cache_name), "w") as f:
            json.dump(vexcel_response, f)
    except OSError:
        pass
    return vexcel_response, None


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(force=True)
    property_id = int(body["property_id"])
    source = body["source"]
    floor_name = body["floor_name"]
    lat = float(body["latitude"])
    lon = float(body["longitude"])
    real_floor_area_sqft = float(body["typical_floor_sqft"])
    real_column_depth_ft = body.get("column_depth_ft")

    timeline = []
    t0 = time.time()

    vexcel_response, err = _resolve_vexcel(source, property_id, lat, lon, t0, timeline)
    if err:
        return err

    try:
        png_bytes, result, geo = render(
            property_id=property_id, floor_name=floor_name, vexcel_response=vexcel_response,
            query_lat=lat, query_lon=lon, real_floor_area_sqft=real_floor_area_sqft,
            real_column_depth_ft=real_column_depth_ft, out_path=None,
        )
    except Exception as e:
        return jsonify({"error": "pipeline_failed", "message": str(e)}), 500

    timeline.append({"step": "Geometry: Vexcel footprint scaled to CoStar's real floor area, "
                              "sliced into real apartment-sized units",
                      "elapsed_s": round(time.time() - t0, 2)})
    if not result.value_skipped:
        timeline.append({"step": "Value: priced against real CoStar lease/comp data and nearest real apartment comps",
                          "elapsed_s": round(time.time() - t0, 2)})
    else:
        timeline.append({"step": "Value calculation skipped - geometry failed hard, nothing to price",
                          "elapsed_s": round(time.time() - t0, 2)})

    return jsonify({
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
        "verdict": result.geometry_verdict,
        "confidence": result.geometry_confidence,
        "window_to_core_max_ft": result.window_to_core_max_ft,
        "convertible_fraction": result.convertible_fraction,
        "viable_leasable_sqft": geo.viable_leasable_sqft,
        "unit_type_counts": geo.unit_type_counts,
        "value_skipped": result.value_skipped,
        "value_impact_low": result.implied_value_impact_low,
        "value_impact_high": result.implied_value_impact_high,
        "reasoning_text": result.reasoning_text,
        "notes": result.notes,
        "timeline": timeline,
        "total_elapsed_s": round(time.time() - t0, 2),
    })


@app.route("/api/analyze_agent", methods=["POST"])
def api_analyze_agent():
    """Real tool-calling LLM run - the model itself decides when to call
    run_geometry vs run_value and writes its own verdict, instead of the
    fixed Python sequence in run_pipeline(). Requires ANTHROPIC_API_KEY."""
    body = request.get_json(force=True)
    property_id = int(body["property_id"])
    source = body["source"]
    floor_name = body["floor_name"]
    lat = float(body["latitude"])
    lon = float(body["longitude"])
    real_floor_area_sqft = float(body["typical_floor_sqft"])
    real_column_depth_ft = body.get("column_depth_ft")

    if not "".join(os.environ.get("ANTHROPIC_API_KEY", "").split()):
        return jsonify({"error": "anthropic_key_missing",
                         "message": "ANTHROPIC_API_KEY is not set in the terminal running this server."}), 400

    timeline = []
    t0 = time.time()

    vexcel_response, err = _resolve_vexcel(source, property_id, lat, lon, t0, timeline)
    if err:
        return err

    try:
        png_bytes, _result, _geo = render(
            property_id=property_id, floor_name=floor_name, vexcel_response=vexcel_response,
            query_lat=lat, query_lon=lon, real_floor_area_sqft=real_floor_area_sqft,
            real_column_depth_ft=real_column_depth_ft, out_path=None,
        )
    except Exception as e:
        return jsonify({"error": "pipeline_failed", "message": str(e)}), 500

    try:
        agent_out = run_agent_llm(
            property_id=property_id, floor_name=floor_name, vexcel_response=vexcel_response,
            query_lat=lat, query_lon=lon, real_floor_area_sqft=real_floor_area_sqft,
            real_column_depth_ft=real_column_depth_ft,
        )
    except Exception as e:
        return jsonify({"error": "agent_failed", "message": str(e)}), 500

    timeline.append({"step": "Live LLM agent run - the model chose its own tool-call sequence",
                      "elapsed_s": round(time.time() - t0, 2)})

    return jsonify({
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
        "trace": agent_out["trace"],
        "final_text": agent_out["final_text"],
        "timeline": timeline,
        "total_elapsed_s": round(time.time() - t0, 2),
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
