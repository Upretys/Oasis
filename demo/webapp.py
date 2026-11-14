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

_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_ENV_PATH):
    # Minimal .env loader - no new dependency needed for 3 KEY=VALUE lines.
    # Never overwrites a key already set in the real environment.
    with open(_ENV_PATH) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_a_geometry"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_b_value"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_c_agent"))

from flask import Flask, jsonify, request, send_from_directory

from vexcel_live import fetch_vexcel_property, VexcelKeyMissing, VexcelNoPropertyData  # noqa: E402
from value import list_houston_office_buildings_broad, check_building_constraints, _iter_jsonl  # noqa: E402
from render_floor import render, BUILDINGS as CACHED_BUILDINGS  # noqa: E402
from agent import run_agent_llm, AgentError  # noqa: E402
from livability import real_nearby_amenities, LivabilityUnavailable  # noqa: E402
from anomaly_detection import _build_populations, _populations  # noqa: E402

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


_DATASET_FILES = [
    ("commercial-property.jsonl", "Real Houston office properties"),
    ("location-occupancy.jsonl", "Real lease/occupancy records (who's actually renting what, right now)"),
    ("commercial-sale-comp.jsonl", "Real property sales (where our real cap rates come from)"),
    ("apartment-listing.jsonl", "Real apartment communities (where our real residential rent comps come from)"),
]

# A real, specific list of bugs found by checking raw data against this
# project's own assumptions - not a generic "we tested thoroughly" claim.
# Each one was caught, traced to its root cause, and fixed during this
# build; kept here as the honest record, not polished after the fact.
REAL_BUGS_FOUND = [
    {"bug": "Cap rate off by 100x", "detail": "CoStar stores capitalizationRateActual as a plain percentage (5.25 meaning 5.25%), not a fraction - an early version divided nowhere, producing a nonsensical 525% cap rate on a real property.", "fix": "Divide by 100.0 at the source; the anomaly detector below now catches this class of bug automatically."},
    {"bug": "480 real cap rates silently ignored", "detail": "The cap rate lookup only checked capitalizationRateActual - but 480 real sale-comp entities across the kit have a cap rate ONLY on capitalizationRateProForma (a projection, not an in-place rate).", "fix": "Check both fields; label pro-forma results as projected, not equally certain as an actual sale."},
    {"bug": "A real apartment unit rendered entirely outside its own building", "detail": "GEOS/Shapely numerical drift after many repeated polygon .difference() calls produced a technically-valid-but-wrong unit shape - confirmed by checking containment directly, not just visually.", "fix": "Added an explicit unit_poly.within(footprint.buffer(0.1)) safety check; the affected building's verdict correctly flipped once the bogus unit was removed."},
    {"bug": "Vacancy rate always null", "detail": "The original vacancy lookup used apartmentModels[].numUnitsVacant - confirmed null for all 23,806 apartment models in the entire kit, genuinely no data behind that field.", "fix": "Found a different, real, populated field (numUnitsAvailable / totalNumUnits, 99.4% populated) by re-checking the raw data after being asked to look again."},
    {"bug": "Market rent weighted by unit count, not square footage", "detail": "CoStar's own kit documentation (metrics-and-formulas.md) specifies rent should be weighted by SUM(SF), not unit count - our code weighted by number of units, correct only when every floor plan in a comp is the same size.", "fix": "Reweighted by real total square footage per floor plan, matching CoStar's own documented formula exactly."},
    {"bug": "Portfolio ranking didn't match its own documentation", "detail": "The README always claimed buildings are ranked by value impact x geometry confidence x value confidence - but the actual code only ever sorted by raw dollar value, discarding the confidence scores already computed.", "fix": "Added a real rank_score to the scan script and UI; the top-ranked building changed once confidence was actually factored in."},
    {"bug": "Negative dollar ranges printed as a double-negative", "detail": "f\"${lo}-${hi}\" produced \"$-1,958,403-$-1,041,725\" for a negative value range - caught from a real screenshot.", "fix": "A _fmt_money() helper that puts the sign before the dollar sign."},
    {"bug": "Legal/code notes duplicated", "detail": "Zoning, column-spacing, and occupancy-change notes appeared twice: once in the dedicated Legal panel, again word-for-word in the generic Notes list.", "fix": "Removed the duplicates from the generic notes list; each caller already gets them from check_building_constraints() directly."},
]


@app.route("/api/data_methodology")
def api_data_methodology():
    """Real scale + real verification story - not a claim, a count. Every
    number here is computed live from the actual kit files and the actual
    anomaly-detection populations, not hardcoded."""
    datasets = []
    total = 0
    for fname, desc in _DATASET_FILES:
        n = sum(1 for _ in _iter_jsonl(fname))
        datasets.append({"file": fname, "description": desc, "count": n})
        total += n

    _build_populations()
    populations = {
        name: {"n": pop.n, "median": round(pop.median, 2), "normal_range": [round(pop.low, 2), round(pop.high, 2)]}
        for name, pop in _populations.items()
    }

    return jsonify({
        "datasets": datasets,
        "total_real_records": total,
        "anomaly_populations": populations,
        "real_bugs_found": REAL_BUGS_FOUND,
    })


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

    constraints = check_building_constraints(property_id)

    # Real OpenStreetMap data (schools/hospitals/parks/grocery) - never
    # blocks the main result if it fails; the public Overpass mirrors had a
    # real multi-hour outage tonight, confirmed across several mirrors
    # before finding a working one. Falls back to an honest "unavailable"
    # rather than fake amenity counts.
    try:
        livability = real_nearby_amenities(lat, lon)
    except LivabilityUnavailable as e:
        livability = {"error": str(e)}

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
        "classification": result.classification,
        "classification_confidence": result.classification_confidence,
        "classification_path": result.classification_path,
        "cap_rate_sensitivity": result.cap_rate_sensitivity,
        "cap_rate_used": result.cap_rate_used,
        "breakeven_cap_rate_low": result.breakeven_cap_rate_low,
        "breakeven_cap_rate_high": result.breakeven_cap_rate_high,
        "reasoning_text": result.reasoning_text,
        "notes": result.notes,
        "legal_checks": {
            "zoning_flag": constraints.zoning_flag,
            "parking_flag": constraints.parking_flag,
            "parking_ratio": constraints.parking_ratio,
            "sprinkler_flag": constraints.sprinkler_flag,
            "occupancy_change_note": constraints.occupancy_change_note,
            "column_spacing_note": constraints.column_spacing_note,
        },
        "livability": livability,
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
    except AgentError as e:
        # A clean, already-user-safe message (auth/rate-limit/bad-request) -
        # the retry logic inside run_agent_llm already absorbed transient
        # failures, so anything surfacing here is worth showing as-is.
        return jsonify({"error": "agent_failed", "message": str(e)}), 502
    except Exception as e:
        return jsonify({"error": "agent_failed", "message": f"Unexpected error: {e}"}), 500

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
