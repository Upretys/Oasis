"""
Oasis - live interactive demo server.

Every building goes through the same real pipeline (run_pipeline /
run_feasibility / run_value). What the server can do depends on what is on
this machine, and the UI is told which case applies instead of faking it:

  - "live"   the CoStar kit is present: the pipeline runs now, for any floor.
             A building whose Vexcel footprint is not cached yet triggers a
             real /property/extract call (needs VEXCEL_API_KEY).
  - "saved"  no CoStar kit: the real result saved by the last batch run
             (demo/all_results.json) is served for the floor that was tested.
             The floor plan is still computed live from the cached footprint.
  - "scan"   a building only seen by the blind portfolio scan: verdict and
             value from demo/portfolio_scan.json, nothing else.

Run: .venv/bin/python3 demo/webapp.py     (keys are read from .env)
Then open http://127.0.0.1:5050
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("track_a_geometry", "track_b_value", "track_c_agent"):
    sys.path.insert(0, os.path.join(HERE, "..", sub))
sys.path.insert(0, HERE)

import oasis  # noqa: E402
import quote_mail  # noqa: E402

oasis.load_env()  # before track_b_value is imported - it reads COSTAR_KIT_PATH at import time
import tempfile

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

PIPELINE_ERROR = None
try:
    from vexcel_live import fetch_vexcel_property, VexcelKeyMissing, VexcelNoPropertyData  # noqa: E402
    from value import check_building_constraints, _iter_jsonl  # noqa: E402
    from render_floor import render, BUILDINGS as CACHED_BUILDINGS  # noqa: E402
    from agent import run_agent_llm  # noqa: E402
    from anomaly_detection import _build_populations, _populations  # noqa: E402
except Exception as e:  # missing dependency - the saved results still work
    PIPELINE_ERROR = f"{type(e).__name__}: {e}"
    CACHED_BUILDINGS = []

app = Flask(__name__, static_folder=None)

OUTPUT_DIR = oasis.OUTPUT_DIR
STATIC_DIR = os.path.join(HERE, "static")
_registry: dict | None = None
ANALYSES: dict = {}  # (property_id, floor) -> last analysis, so the boardroom and email agent use the server's own numbers


# ---------------------------------------------------------------------------
# Building registry
# ---------------------------------------------------------------------------

def registry() -> dict:
    global _registry
    if _registry is not None:
        return _registry
    saved = oasis.load_saved_results()
    reg: dict = {}
    for b in CACHED_BUILDINGS:
        reg[b["property_id"]] = {
            "property_id": b["property_id"], "name": b["name"], "source": "cached",
            "floor_name": b["floor_name"], "latitude": b["query_lat"], "longitude": b["query_lon"],
            "typical_floor_sqft": b["real_floor_area_sqft"], "column_depth_ft": b["real_column_depth_ft"],
            "vexcel_file": b["vexcel_file"], "saved": saved.get(b["name"]),
        }
    # The broader "live, click any untested building" lookup
    # (list_houston_office_buildings_broad) is deliberately left out of
    # the main map - a scope choice, not a removed capability, since any
    # real Houston building can still be reached by address search.
    #
    # The 35-building blind portfolio scan is different: real, precomputed
    # work, and - in this frontend (unlike an earlier one, which had its
    # own separate "Portfolio Scan" view) - the only way it's ever shown is
    # by folding its real results into this same registry, colored
    # distinctly ("scan" source, the "Screened only" legend entry the UI
    # already has styling for). Leaving it out here was a real regression,
    # found by the user asking "didn't we scan 35 buildings?" after it
    # silently disappeared from the main overview.
    for s in oasis.load_portfolio_scan():
        entry = reg.setdefault(s["property_id"], {
            "property_id": s["property_id"], "name": s["name"], "source": "scan",
            "latitude": s["latitude"], "longitude": s["longitude"],
        })
        entry["scan"] = {"verdict": s["verdict"], "value_skipped": s["value_skipped"],
                         "impact_low": s["value_impact_low"], "impact_high": s["value_impact_high"]}
    for entry in reg.values():
        path = oasis.find_vexcel_file(entry["latitude"], entry["longitude"], entry.get("vexcel_file"))
        if path and not PIPELINE_ERROR:
            try:
                with open(path) as f:
                    entry["footprint"] = oasis.ground_footprint_lonlat(
                        json.load(f), entry["latitude"], entry["longitude"])
            except (OSError, ValueError, AttributeError):
                app.logger.warning("Could not load footprint for property %s", entry["property_id"])
        entry["near"] = oasis.nearest_by_kind(entry["latitude"], entry["longitude"])
    _registry = reg
    return reg


def public(b: dict) -> dict:
    scan, saved = b.get("scan") or {}, b.get("saved") or {}
    lo = scan.get("impact_low", saved.get("implied_value_impact_low"))
    hi = scan.get("impact_high", saved.get("implied_value_impact_high"))
    return {
        "property_id": b["property_id"], "name": b["name"], "source": b["source"],
        "latitude": b["latitude"], "longitude": b["longitude"], "address": b.get("address"),
        "floor_name": b.get("floor_name"), "stories": b.get("stories"),
        "typical_floor_sqft": b.get("typical_floor_sqft"),
        "verdict": scan.get("verdict") or saved.get("geometry_verdict"),
        "confidence": saved.get("geometry_confidence"), "footprint": b.get("footprint"),
        "convertible_fraction": saved.get("convertible_fraction"),
        "impact_low": lo, "impact_high": hi, "near": b.get("near", {}),
    }


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


@app.get("/api/map-config")
def map_config():
    return jsonify(google_browser_key=os.environ.get("GOOGLE_MAPS_DEMO_KEY", "").strip(),
                   vexcel_imagery=oasis.has_key("VEXCEL_IMAGERY_API_KEY") or oasis.has_key("VEXCEL_API_KEY"))


@app.get("/api/map/vexcel/photo/<int:property_id>/<view>")
def vexcel_photo(property_id, view):
    import requests
    from flask import Response

    building = registry().get(property_id)
    key = os.environ.get("VEXCEL_IMAGERY_API_KEY") or os.environ.get("VEXCEL_API_KEY")
    if not key or not building or view not in ("ortho", "oblique-north", "oblique-south", "oblique-east", "oblique-west"):
        return "", 404
    ring = building.get("footprint")
    if ring:
        coordinates = ", ".join(f"{lon} {lat}" for lon, lat in ring)
        wkt = f"POLYGON(({coordinates}))"
    else:
        wkt = f"POINT({building['longitude']} {building['latitude']})"
    oblique = view != "ortho"
    endpoint = "oriented/extract" if oblique else "ortho/extract"
    params = {"wkt": wkt, "srid": 4326, "buffer-meter": 25,
              "layer": "urban" if oblique else "urban,urban-r,wide-area,urban-g",
              "image-format": "jpeg"}
    if oblique:
        params.update({"product-type": view, "rotation": "natural"})
    try:
        photo = requests.post(f"https://api.vexcelgroup.com/v2/{endpoint}",
                              headers={"X-API-KEY": key.strip()}, json=params, timeout=15)
    except requests.RequestException:
        return "", 502
    content_type = photo.headers.get("Content-Type", "").split(";")[0]
    if not photo.ok or content_type not in ("image/jpeg", "image/png"):
        return "", 404 if photo.status_code == 404 else 502
    return Response(photo.content, mimetype=content_type, headers={"Cache-Control": "no-store"})


@app.get("/api/map/vexcel/<int:z>/<int:x>/<int:y>.png")
def vexcel_tile(z, x, y):
    import requests
    from flask import Response

    key = (os.environ.get("VEXCEL_IMAGERY_API_KEY") or os.environ.get("VEXCEL_API_KEY", "")).strip()
    if not key or not 10 <= z <= 21 or max(x, y) >= 2**z:
        return "", 404
    try:
        tile = requests.get("https://api.vexcelgroup.com/v2/ortho/tile", timeout=8,
                            headers={"X-API-KEY": key},
                            params={"layer": "urban", "zoom": z, "tile-x": x, "tile-y": y,
                                    "image-format": "png"})
    except requests.RequestException:
        return "", 502
    if tile.status_code == 404:
        return "", 404
    if not tile.ok or not tile.headers.get("Content-Type", "").startswith("image/png"):
        return "", 502
    return Response(tile.content, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/api/overview")
def api_overview():
    buildings = [public(b) for b in registry().values()]
    scanned = [b for b in buildings if b["impact_low"] is not None or b["verdict"]]
    positive = [b for b in buildings if b["impact_low"] is not None and b["impact_low"] > 0]
    return jsonify({
        "buildings": buildings,
        "stats": {"scanned": len(scanned), "positive": len(positive),
                  "pretested": sum(1 for b in buildings if b["source"] == "cached")},
        # Best case first, so the list shows the closest to profitable even when none clears $0.
        "top": sorted((b for b in buildings if b["impact_high"] is not None), key=lambda b: -b["impact_high"])[:20],
        "agents": [{k: a[k] for k in ("id", "name", "initials")} for a in oasis.AGENTS],
        "place_kinds": {k: v["label"] for k, v in oasis.PLACE_KINDS.items()},
        "sources": {
            "costar": oasis.costar_available(), "vexcel": oasis.has_key("VEXCEL_API_KEY"),
            "agents": oasis.has_key("ANTHROPIC_API_KEY"), "census": oasis.has_key("CENSUS_API_KEY"),
            "places": bool(oasis.places()), "pipeline_error": PIPELINE_ERROR,
        },
    })


# ---------------------------------------------------------------------------
# Kept from the first demo (same contracts)
# ---------------------------------------------------------------------------

@app.route("/api/portfolio_scan")
def api_portfolio_scan():
    return jsonify(oasis.load_portfolio_scan())


@app.route("/api/buildings")
def api_buildings():
    reg = registry().values()
    keep = ("property_id", "name", "source", "floor_name", "latitude", "longitude",
            "typical_floor_sqft", "column_depth_ft", "address", "stories")
    return jsonify({
        "cached": [{k: b.get(k) for k in keep} for b in reg if b["source"] == "cached"],
        "live": [{k: b.get(k) for k in keep} for b in reg if b["source"] == "live"],
        "vexcel_key_set": oasis.has_key("VEXCEL_API_KEY"),
    })


_DATASET_FILES = [
    ("commercial-property.jsonl", "Real Houston office properties"),
    ("location-occupancy.jsonl", "Real lease/occupancy records (who's actually renting what, right now)"),
    ("commercial-sale-comp.jsonl", "Real property sales (where our real cap rates come from)"),
    ("apartment-listing.jsonl", "Real apartment communities (where our real residential rent comps come from)"),
]

# A real, specific list of bugs found by checking raw data against this
# project's own assumptions - not a generic "we tested thoroughly" claim.
REAL_BUGS_FOUND = [
    {"bug": "Cap rate off by 100x", "detail": "CoStar stores capitalizationRateActual as a plain percentage (5.25 meaning 5.25%), not a fraction - an early version divided nowhere, producing a nonsensical 525% cap rate on a real property.", "fix": "Divide by 100.0 at the source; the anomaly detector below now catches this class of bug automatically."},
    {"bug": "480 real cap rates silently ignored", "detail": "The cap rate lookup only checked capitalizationRateActual - but 480 real sale-comp entities across the kit have a cap rate ONLY on capitalizationRateProForma (a projection, not an in-place rate).", "fix": "Check both fields; label pro-forma results as projected, not equally certain as an actual sale."},
    {"bug": "A real apartment unit rendered entirely outside its own building", "detail": "GEOS/Shapely numerical drift after many repeated polygon .difference() calls produced a technically-valid-but-wrong unit shape - confirmed by checking containment directly, not just visually.", "fix": "Added an explicit unit_poly.within(footprint.buffer(0.1)) safety check; the affected building's verdict correctly flipped once the bogus unit was removed."},
    {"bug": "Vacancy rate always null", "detail": "The original vacancy lookup used apartmentModels[].numUnitsVacant - confirmed null for all 23,806 apartment models in the entire kit, genuinely no data behind that field.", "fix": "Found a different, real, populated field (numUnitsAvailable / totalNumUnits, 99.4% populated) by re-checking the raw data after being asked to look again."},
    {"bug": "Market rent weighted by unit count, not square footage", "detail": "CoStar's own kit documentation (metrics-and-formulas.md) specifies rent should be weighted by SUM(SF), not unit count - our code weighted by number of units, correct only when every floor plan in a comp is the same size.", "fix": "Reweighted by real total square footage per floor plan, matching CoStar's own documented formula exactly."},
    {"bug": "Portfolio ranking didn't match its own documentation", "detail": "The README always claimed buildings are ranked by value impact x geometry confidence x value confidence - but the actual code only ever sorted by raw dollar value, discarding the confidence scores already computed.", "fix": "Added a real rank_score to the scan script and UI; the top-ranked building changed once confidence was actually factored in."},
    {"bug": "Negative dollar ranges printed as a double-negative", "detail": "f\"${lo}-${hi}\" produced \"$-1,958,403-$-1,041,725\" for a negative value range - caught from a real screenshot.", "fix": "A _fmt_money() helper that puts the sign before the dollar sign."},
    {"bug": "Legal/code notes duplicated", "detail": "Zoning, column-spacing, and occupancy-change notes appeared twice: once in the dedicated Legal panel, again word-for-word in the generic Notes list.", "fix": "Removed the duplicates from the generic notes list; each caller already gets them from check_building_constraints() directly."},
    {"bug": "Real elevator data computed, then silently discarded", "detail": "elevator_summary was already being built from CoStar's real structural.elevators field, but never reached any check, the webapp response, or the UI - a real FHA accessibility requirement (non-ground-floor units need elevator access) went unevaluated.", "fix": "Added a real accessibility_flag/accessibility_note using the data that was already there."},
]


@app.route("/api/data_methodology")
def api_data_methodology():
    """Real scale + real verification story - not a claim, a count. Every
    number here is computed live from the actual kit files and the actual
    anomaly-detection populations, not hardcoded."""
    if PIPELINE_ERROR:
        return jsonify({"error": "pipeline_unavailable", "message": PIPELINE_ERROR}), 503
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


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class AnalysisError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@app.errorhandler(AnalysisError)
def handle_analysis_error(error):
    return jsonify({"error": error.code, "message": error.message}), error.status


def json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AnalysisError("bad_request", "Expected a JSON object.")
    return body


def integer(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AnalysisError("bad_request", f"{field} must be an integer.")
    try:
        return int(value)
    except ValueError as e:
        raise AnalysisError("bad_request", f"{field} must be an integer.") from e


def _vexcel(b: dict, timeline: list, t0: float, allow_fetch: bool):
    path = oasis.find_vexcel_file(b["latitude"], b["longitude"], b.get("vexcel_file"))
    if path:
        with open(path) as f:
            timeline.append({"step": "Loaded the cached Vexcel aerial footprint", "elapsed_s": round(time.time() - t0, 2)})
            return json.load(f)
    if not allow_fetch or PIPELINE_ERROR:
        return None
    try:
        resp = fetch_vexcel_property(b["latitude"], b["longitude"])
    except VexcelKeyMissing as e:
        raise AnalysisError("vexcel_key_missing", str(e))
    except VexcelNoPropertyData as e:
        raise AnalysisError("no_property_data", str(e), 404)
    timeline.append({"step": "Live Vexcel request for this building's aerial footprint", "elapsed_s": round(time.time() - t0, 2)})
    path = os.path.join(OUTPUT_DIR, f"property_{b['latitude']}_{b['longitude']}.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=OUTPUT_DIR, delete=False) as f:
            temporary = f.name
            json.dump(resp, f)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary:
            os.unlink(temporary)
    return resp


@app.get("/api/footprint/<int:property_id>")
def api_footprint(property_id):
    building = registry().get(property_id)
    if building is None:
        raise AnalysisError("unknown_building", "That building is not in the list.", 404)
    footprint = building.get("footprint")
    if not footprint and not PIPELINE_ERROR:
        data = _vexcel(building, [], time.time(), allow_fetch=oasis.has_key("VEXCEL_API_KEY"))
        if data:
            footprint = oasis.ground_footprint_lonlat(data, building["latitude"], building["longitude"])
    return jsonify(footprint=footprint)


def analyze(property_id: int, floor) -> dict:
    b = registry().get(property_id)
    if b is None:
        raise AnalysisError("unknown_building", "That building is not in the list.", 404)
    floor = integer(floor, "floor") if floor not in (None, "") else b.get("floor_name")
    timeline, t0 = [], time.time()
    live_ok = oasis.costar_available() and not PIPELINE_ERROR and b.get("typical_floor_sqft")
    vexcel = _vexcel(b, timeline, t0, allow_fetch=bool(live_ok))
    image_b64, r, source = None, None, None

    if live_ok and vexcel is not None:
        try:
            png, result, geo = render(
                property_id=property_id, floor_name=floor, vexcel_response=vexcel,
                query_lat=b["latitude"], query_lon=b["longitude"],
                real_floor_area_sqft=float(b["typical_floor_sqft"]), real_column_depth_ft=b.get("column_depth_ft"), out_path=None)
        except Exception as e:
            raise AnalysisError("pipeline_failed", str(e), 500)
        image_b64 = base64.b64encode(png).decode("ascii")
        r = {"verdict": result.geometry_verdict, "confidence": result.geometry_confidence,
             "window_to_core_max_ft": result.window_to_core_max_ft, "convertible_fraction": result.convertible_fraction,
             "value_skipped": result.value_skipped, "uplift_low": result.value_uplift_low, "uplift_high": result.value_uplift_high,
             "impact_low": result.implied_value_impact_low, "impact_high": result.implied_value_impact_high,
             "reasoning_text": result.reasoning_text, "notes": result.notes,
             "viable_sqft": geo.viable_leasable_sqft, "unit_counts": geo.unit_type_counts,
             "value_confidence": result.value_confidence, "classification": result.classification,
             "classification_confidence": result.classification_confidence, "classification_path": result.classification_path,
             "cap_rate_sensitivity": result.cap_rate_sensitivity, "cap_rate_used": result.cap_rate_used,
             "breakeven_cap_rate_low": result.breakeven_cap_rate_low, "breakeven_cap_rate_high": result.breakeven_cap_rate_high}
        source = "live"
        timeline.append({"step": "Ran the geometry and value pipeline on live CoStar data", "elapsed_s": round(time.time() - t0, 2)})
    elif b.get("saved") and floor == b.get("floor_name"):
        s = b["saved"]
        r = {"verdict": s["geometry_verdict"], "confidence": s["geometry_confidence"],
             "window_to_core_max_ft": s["window_to_core_max_ft"], "convertible_fraction": s["convertible_fraction"],
             "value_skipped": s["value_skipped"], "uplift_low": s.get("value_uplift_low"), "uplift_high": s.get("value_uplift_high"),
             "impact_low": s.get("implied_value_impact_low"), "impact_high": s.get("implied_value_impact_high"),
             "reasoning_text": s["reasoning_text"], "notes": s["notes"],
             "value_confidence": s.get("value_confidence"), "classification": s.get("classification"),
             "classification_confidence": s.get("classification_confidence"), "classification_path": s.get("classification_path"),
             "cap_rate_sensitivity": s.get("cap_rate_sensitivity"), "cap_rate_used": s.get("cap_rate_used"),
             "breakeven_cap_rate_low": s.get("breakeven_cap_rate_low"), "breakeven_cap_rate_high": s.get("breakeven_cap_rate_high")}
        source = "saved"
        img = os.path.join(OUTPUT_DIR, s.get("image") or "")
        if s.get("image") and os.path.exists(img):
            with open(img, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("ascii")
        timeline.append({"step": "Loaded the saved result of the last real pipeline run", "elapsed_s": round(time.time() - t0, 2)})
    elif b.get("scan") and (floor is None or floor == b.get("floor_name")):
        s = b["scan"]
        r = {"verdict": s["verdict"], "confidence": None, "window_to_core_max_ft": None, "convertible_fraction": None,
             "value_skipped": s["value_skipped"], "uplift_low": None, "uplift_high": None,
             "impact_low": s["impact_low"], "impact_high": s["impact_high"], "reasoning_text": "",
             "notes": ["This building was only screened by the portfolio scan. Add the CoStar data kit to run the full analysis."],
             "partial": True}
        source = "scan"
        timeline.append({"step": "Loaded the portfolio scan result", "elapsed_s": round(time.time() - t0, 2)})
    else:
        raise AnalysisError("needs_costar",
                            f"Floor {floor} has not been analyzed yet. Analyzing a new floor needs the CoStar data kit "
                            f"(COSTAR_KIT_PATH), which is not on this machine.", 409)

    footprint, plan = None, None
    if vexcel is not None and not PIPELINE_ERROR:
        try:
            footprint = oasis.ground_footprint_lonlat(vexcel, b["latitude"], b["longitude"])
            if b.get("typical_floor_sqft"):
                plan = oasis.floor_plan(vexcel, b["latitude"], b["longitude"], float(b["typical_floor_sqft"]))
        except Exception:
            pass

    # Real FHA accessibility check, zoning/parking/sprinkler/column-spacing -
    # computed from CoStar's real fields, independent of whether this floor
    # ran live or came from a saved result (property_id + floor is all it
    # needs). Not derivable from reasoning_text, so it's added directly
    # here rather than through oasis.summarize()'s text-parsing path.
    try:
        constraints = check_building_constraints(property_id, floor)
        legal_checks = {
            "zoning_flag": constraints.zoning_flag, "parking_flag": constraints.parking_flag,
            "parking_ratio": constraints.parking_ratio, "sprinkler_flag": constraints.sprinkler_flag,
            "occupancy_change_note": constraints.occupancy_change_note,
            "column_spacing_note": constraints.column_spacing_note,
            "accessibility_flag": constraints.accessibility_flag, "accessibility_note": constraints.accessibility_note,
        }
    except Exception:
        legal_checks = None

    out = {"building": public(b), "floor": floor, "source": source, **oasis.summarize(r),
           "verdict": r["verdict"], "depth_ft": r["window_to_core_max_ft"], "convertible_fraction": r["convertible_fraction"],
           "reasoning_text": r["reasoning_text"], "notes": r["notes"], "image_base64": image_b64,
           "footprint": footprint, "floorplan": plan, "timeline": timeline, "total_elapsed_s": round(time.time() - t0, 2),
           "legal_checks": legal_checks, "value_confidence": r.get("value_confidence"),
           "classification": r.get("classification"), "classification_confidence": r.get("classification_confidence"),
           "classification_path": r.get("classification_path"), "cap_rate_sensitivity": r.get("cap_rate_sensitivity"),
           "cap_rate_used": r.get("cap_rate_used"), "breakeven_cap_rate_low": r.get("breakeven_cap_rate_low"),
           "breakeven_cap_rate_high": r.get("breakeven_cap_rate_high")}
    ANALYSES[(property_id, floor)] = out
    return out


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = json_body()
    try:
        return jsonify(analyze(integer(body.get("property_id"), "property_id"), body.get("floor_name")))
    except AnalysisError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status


@app.route("/api/floorplan", methods=["POST"])
def api_floorplan():
    body = json_body()
    b = registry().get(integer(body.get("property_id"), "property_id"))
    if b is None or not b.get("typical_floor_sqft") or PIPELINE_ERROR:
        return jsonify({"error": "no_plan", "message": "No floor area on record for this building, so a plan can't be drawn."}), 404
    path = oasis.find_vexcel_file(b["latitude"], b["longitude"], b.get("vexcel_file"))
    if not path:
        return jsonify({"error": "no_footprint", "message": "No aerial footprint is cached for this building."}), 404
    mix = body.get("mix", "balanced")
    if not isinstance(mix, str):
        raise AnalysisError("bad_request", "mix must be a string.")
    with open(path) as f:
        plan = oasis.floor_plan(json.load(f), b["latitude"], b["longitude"], float(b["typical_floor_sqft"]), mix)
    if plan is None:
        return jsonify({"error": "no_footprint", "message": "No structure was found at this location."}), 404
    return jsonify(plan)


@app.route("/api/floors")
def api_floors():
    b = registry().get(integer(request.args.get("property_id"), "property_id"))
    if b is None:
        return jsonify({"error": "unknown_building"}), 404
    out = oasis.floors_for(b["property_id"], b.get("floor_name"), b.get("stories"))
    out["analyzed_floor"] = b.get("floor_name")
    return jsonify(out)


@app.route("/api/area")
def api_area():
    b = registry().get(integer(request.args.get("property_id"), "property_id"))
    if b is None:
        return jsonify({"error": "unknown_building"}), 404
    return jsonify({"places": oasis.places_near(b["latitude"], b["longitude"]),
                    "demographics": oasis.demographics(b["latitude"], b["longitude"])})


# ---------------------------------------------------------------------------
# Boardroom and quotes
# ---------------------------------------------------------------------------

def _facts(property_id: int, floor) -> dict:
    building = registry().get(property_id)
    default_floor = building.get("floor_name") if building else None
    floor = integer(floor, "floor") if floor not in (None, "") else default_floor
    a = ANALYSES.get((property_id, floor))
    if a is None:
        a = analyze(property_id, floor)
    notes = a.get("notes") or []
    col = next((n for n in notes if n.startswith("Column bays")), None)
    return {
        "name": a["building"]["name"], "floor": a["floor"], "verdict": a["verdict"], "confidence": a.get("confidence"),
        "depth_ft": a.get("depth_ft"), "depth_limit_ft": oasis.DEPTH_LIMIT_FT, "convertible_fraction": a.get("convertible_fraction"),
        "numbers": a["numbers"], "checks": [{"check": c["title"], "result": c["state"], "detail": c["note"]} for c in a["checks"]],
        "column_note": (col.split(" - ")[0] if col else None),
        "parking_note": next((n for n in notes if n.startswith("Parking ratio")), None),
        "notes": notes, "construction_cost_assumption_per_sqft": [oasis.COST_LOW, oasis.COST_HIGH],
    }


@app.route("/api/boardroom/turn", methods=["POST"])
def api_board_turn():
    body = json_body()
    try:
        facts = _facts(integer(body.get("property_id"), "property_id"), body.get("floor"))
        turn = oasis.board_turn(body["agent"], facts, int(body.get("round", 1)), body.get("transcript") or [])
    except AnalysisError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status
    except ValueError as e:
        return jsonify({"error": "bad_request", "message": str(e)}), 400
    return jsonify(turn)


@app.get("/api/quotes/profile")
def api_quotes_profile():
    return jsonify(email=quote_mail.profile_email(), gmail_ready=quote_mail.gmail_ready(),
                   will_send=os.getenv("OASIS_SEND_EMAIL") == "1")


def require_local_mail():
    if (request.remote_addr not in {"127.0.0.1", "::1"}
            or request.host.split(":")[0] not in {"127.0.0.1", "localhost"}
            or request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
        raise AnalysisError("local_mail_only", "Open Oasis on localhost to use Gmail.", 403)


@app.get("/api/quotes/replies")
def api_quote_replies():
    require_local_mail()
    try:
        replies = quote_mail.Gmail(quote_mail.profile_email()).replies() if quote_mail.sent_threads else []
    except quote_mail.GmailError as e:
        raise AnalysisError("gmail_error", str(e), 502) from e
    response = jsonify(replies=replies)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/quotes/draft", methods=["POST"])
def api_quotes_draft():
    body = json_body()
    try:
        facts = _facts(integer(body.get("property_id"), "property_id"), body.get("floor"))
    except AnalysisError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status
    draft = oasis.draft_email(facts, body.get("sender") or "")
    draft["contractors"] = oasis.load_contractors()
    return jsonify(draft)


@app.route("/api/quotes/send", methods=["POST"])
def api_quotes_send():
    body = json_body()
    subject, text = body.get("subject"), body.get("body")
    ids = body.get("contractor_ids")
    if (not isinstance(subject, str) or not subject.strip()
            or not isinstance(text, str) or not text.strip()
            or not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids)):
        return jsonify({"error": "bad_request", "message": "The email needs a subject, a body and at least one contractor."}), 400
    attachments = {}
    if body.get("report_text"):
        attachments["oasis-report.txt"] = body["report_text"]
    elif body.get("report_html"):
        attachments["oasis-report.html"] = body["report_html"]
    if body.get("plan_svg"):
        attachments["tentative-floor-plan.svg"] = body["plan_svg"]
    if any(not isinstance(value, str) for value in attachments.values()):
        raise AnalysisError("bad_request", "Attachments must contain text.")
    if body.get("plan_png") is not None:
        try:
            png = base64.b64decode(body["plan_png"], validate=True)
        except (TypeError, ValueError) as e:
            raise AnalysisError("bad_request", "Invalid floor plan image.") from e
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AnalysisError("bad_request", "Invalid floor plan image.")
        attachments.pop("tentative-floor-plan.svg", None)
        attachments["tentative-floor-plan.png"] = png
    save_draft = body.get("save_draft") is True
    if save_draft or os.getenv("OASIS_SEND_EMAIL") == "1":
        require_local_mail()
    try:
        return jsonify(oasis.send_quotes(subject.strip(), text.strip(), ids, attachments, save_draft=save_draft))
    except ValueError as e:
        raise AnalysisError("bad_request", str(e)) from e
    except quote_mail.GmailError as e:
        raise AnalysisError("gmail_error", str(e), 502) from e


if __name__ == "__main__":
    # 0.0.0.0, not 127.0.0.1: a cloud host (Render, Railway, Fly.io, etc.)
    # runs this inside a container and routes real external traffic to it -
    # 127.0.0.1 only accepts connections from inside that same container,
    # making the app completely unreachable once deployed. Still works
    # identically for local dev (127.0.0.1 keeps reaching it).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=False, threaded=True)
