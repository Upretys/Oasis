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

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in ("track_a_geometry", "track_b_value", "track_c_agent"):
    sys.path.insert(0, os.path.join(HERE, "..", sub))
sys.path.insert(0, HERE)

import oasis  # noqa: E402

oasis.load_env()  # before track_b_value is imported - it reads COSTAR_KIT_PATH at import time

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

PIPELINE_ERROR = None
try:
    from vexcel_live import fetch_vexcel_property, VexcelKeyMissing, VexcelNoPropertyData  # noqa: E402
    from value import list_houston_office_buildings_broad  # noqa: E402
    from render_floor import render, BUILDINGS as CACHED_BUILDINGS  # noqa: E402
    from agent import run_agent_llm  # noqa: E402
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
    if oasis.costar_available() and not PIPELINE_ERROR:
        try:
            for c in list_houston_office_buildings_broad():
                entry = reg.setdefault(c["property_id"], {
                    "property_id": c["property_id"], "name": c["name"], "source": "live",
                    "floor_name": c["floor_names_available"][0], "latitude": c["latitude"], "longitude": c["longitude"],
                    "typical_floor_sqft": c["typical_floor_sqft"], "column_depth_ft": c["column_depth_ft"],
                })
                entry["address"], entry["stories"] = c["address"], c["stories"]
        except Exception:
            pass
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
    if not key or not building or view not in ("ortho", "oblique-north"):
        return "", 404
    ring = building.get("footprint")
    if ring:
        coordinates = ", ".join(f"{lon} {lat}" for lon, lat in ring)
        wkt = f"POLYGON(({coordinates}))"
    else:
        wkt = f"POINT({building['longitude']} {building['latitude']})"
    oblique = view == "oblique-north"
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
        "top": sorted(positive, key=lambda b: -b["impact_low"])[:20],
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
    try:
        with open(os.path.join(OUTPUT_DIR, f"property_{b['latitude']}_{b['longitude']}.json"), "w") as f:
            json.dump(resp, f)
    except OSError:
        pass
    return resp


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
             "viable_sqft": geo.viable_leasable_sqft, "unit_counts": geo.unit_type_counts}
        source = "live"
        timeline.append({"step": "Ran the geometry and value pipeline on live CoStar data", "elapsed_s": round(time.time() - t0, 2)})
    elif b.get("saved") and floor == b.get("floor_name"):
        s = b["saved"]
        r = {"verdict": s["geometry_verdict"], "confidence": s["geometry_confidence"],
             "window_to_core_max_ft": s["window_to_core_max_ft"], "convertible_fraction": s["convertible_fraction"],
             "value_skipped": s["value_skipped"], "uplift_low": s.get("value_uplift_low"), "uplift_high": s.get("value_uplift_high"),
             "impact_low": s.get("implied_value_impact_low"), "impact_high": s.get("implied_value_impact_high"),
             "reasoning_text": s["reasoning_text"], "notes": s["notes"]}
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

    out = {"building": public(b), "floor": floor, "source": source, **oasis.summarize(r),
           "verdict": r["verdict"], "depth_ft": r["window_to_core_max_ft"], "convertible_fraction": r["convertible_fraction"],
           "reasoning_text": r["reasoning_text"], "notes": r["notes"], "image_base64": image_b64,
           "footprint": footprint, "floorplan": plan, "timeline": timeline, "total_elapsed_s": round(time.time() - t0, 2)}
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


@app.route("/api/quotes/draft", methods=["POST"])
def api_quotes_draft():
    body = json_body()
    try:
        facts = _facts(integer(body.get("property_id"), "property_id"), body.get("floor"))
    except AnalysisError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status
    draft = oasis.draft_email(facts, body.get("sender") or "")
    draft["contractors"] = oasis.load_contractors()
    draft["will_send"] = os.environ.get("OASIS_SEND_EMAIL") == "1" and oasis.has_key("SMTP_HOST") and oasis.has_key("SMTP_FROM")
    return jsonify(draft)


@app.route("/api/quotes/send", methods=["POST"])
def api_quotes_send():
    body = json_body()
    subject, text = (body.get("subject") or "").strip(), (body.get("body") or "").strip()
    ids = body.get("contractor_ids") or []
    if not subject or not text or not ids:
        return jsonify({"error": "bad_request", "message": "The email needs a subject, a body and at least one contractor."}), 400
    attachments = {}
    if body.get("report_html"):
        attachments["oasis-report.html"] = body["report_html"]
    if body.get("plan_svg"):
        attachments["tentative-floor-plan.svg"] = body["plan_svg"]
    return jsonify(oasis.send_quotes(subject, text, ids, attachments))


@app.route("/api/analyze_agent", methods=["POST"])
def api_analyze_agent():
    """Real tool-calling LLM run (kept from the first demo). Needs
    ANTHROPIC_API_KEY, the CoStar kit and a Vexcel footprint."""
    body = json_body()
    b = registry().get(integer(body.get("property_id"), "property_id"))
    if not oasis.has_key("ANTHROPIC_API_KEY"):
        return jsonify({"error": "anthropic_key_missing", "message": "ANTHROPIC_API_KEY is not set."}), 400
    if b is None or PIPELINE_ERROR or not oasis.costar_available():
        return jsonify({"error": "needs_costar", "message": "The live agent run needs the CoStar data kit."}), 409
    timeline, t0 = [], time.time()
    try:
        vexcel = _vexcel(b, timeline, t0, allow_fetch=True)
        out = run_agent_llm(property_id=b["property_id"], floor_name=int(body.get("floor_name") or b["floor_name"]),
                            vexcel_response=vexcel, query_lat=b["latitude"], query_lon=b["longitude"],
                            real_floor_area_sqft=float(b["typical_floor_sqft"]), real_column_depth_ft=b.get("column_depth_ft"))
    except AnalysisError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status
    except Exception as e:
        return jsonify({"error": "agent_failed", "message": str(e)}), 500
    return jsonify({"trace": out["trace"], "final_text": out["final_text"], "timeline": timeline,
                    "total_elapsed_s": round(time.time() - t0, 2)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")), debug=False, threaded=True)
