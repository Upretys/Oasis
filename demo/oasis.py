"""
Helpers behind the Oasis web UI (demo/webapp.py).

Everything here either reads real data the pipeline already produced, calls a
real public source, or says plainly that the data is missing. Nothing is
invented to fill a gap:

  - analysis      real pipeline when its inputs are present; otherwise the
                  saved real result from demo/all_results.json (flagged "saved")
  - floors        real CoStar lease history per floor when the kit is present;
                  otherwise only the floor that was actually analyzed is marked
  - floor plan    real Vexcel footprint, sliced by track_a_geometry
  - places        OpenStreetMap (Overpass), cached in demo/places_houston.json
  - demographics  US Census ACS, only when CENSUS_API_KEY is set
  - boardroom     statements written by rule from the analysis
  - quotes        emails are written to output/outbox/ unless Gmail sending is enabled
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from email.message import EmailMessage
from uuid import uuid4

import quote_mail

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
OUTPUT_DIR = os.path.join(ROOT, "output")
PLACES_PATH = os.path.join(HERE, "places_houston.json")
OUTBOX_DIR = os.path.join(OUTPUT_DIR, "outbox")
AREA_CACHE_DIR = os.path.join(OUTPUT_DIR, "area_cache")

OASIS_MODEL = os.environ.get("OASIS_MODEL", "claude-opus-5-5")
DEPTH_LIMIT_FT = 45.0
FLOOR_HEIGHT_M = 3.9


def load_env() -> None:
    """Read ../.env into os.environ (without overriding what the shell set).
    Has to run before track_b_value is imported: it reads COSTAR_KIT_PATH at
    import time."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v and not os.environ.get(k.strip()):
                os.environ[k.strip()] = v


def has_key(name: str) -> bool:
    return bool("".join(os.environ.get(name, "").split()))


def costar_available() -> bool:
    kit = os.environ.get("COSTAR_KIT_PATH", "")
    return bool(kit) and os.path.exists(os.path.join(kit, "data", "commercial-property.jsonl"))


def miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Saved real results (used when the live pipeline can't run on this machine)
# ---------------------------------------------------------------------------

def load_saved_results() -> dict:
    """{building name: saved PipelineResult dict} from the last real batch run."""
    try:
        with open(os.path.join(HERE, "all_results.json")) as f:
            return {r["building_name"]: r for r in json.load(f)}
    except FileNotFoundError:
        return {}


def load_portfolio_scan() -> list:
    try:
        with open(os.path.join(HERE, "portfolio_scan.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def find_vexcel_file(lat: float, lon: float, vexcel_file: str | None = None) -> str | None:
    for name in (vexcel_file, f"property_{lat}_{lon}.json"):
        if name and os.path.exists(os.path.join(OUTPUT_DIR, name)):
            return os.path.join(OUTPUT_DIR, name)
    return None


# ---------------------------------------------------------------------------
# Geometry for the map and the floor plan
# ---------------------------------------------------------------------------

def ground_footprint_lonlat(vexcel_response: dict, lat: float, lon: float) -> list | None:
    """The real Vexcel ground footprint as a lon/lat ring, for the 3D floor stack."""
    from geometry import pick_nearest_structure
    from shapely import wkt as shapely_wkt

    s = pick_nearest_structure(vexcel_response, lat, lon)
    if s is None:
        return None
    geom = shapely_wkt.loads(s["footprint.wkt"]).simplify(0.00002, preserve_topology=True)
    return [[round(x, 7), round(y, 7)] for x, y in geom.exterior.coords]


MIX_WIDTH_FT = {"studios": 16.0, "balanced": 20.0, "larger": 28.0}


def floor_plan(vexcel_response: dict, lat: float, lon: float, floor_sqft: float, mix: str = "balanced") -> dict | None:
    """Real floor outline (Vexcel, scaled to CoStar's floor area) sliced into
    apartments. `mix` only changes the strip width the slicer uses."""
    from geometry import (METERS_TO_FEET, SIMPLIFY_TOLERANCE_M, buffer_based_check,
                          generate_unit_layout, load_scaled_footprint)
    from shapely.geometry import Polygon

    fp = load_scaled_footprint(vexcel_response, lat, lon, floor_sqft)
    if fp is None:
        return None
    fp = fp.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)
    zone, fraction, depth_ft = buffer_based_check(fp)
    units = generate_unit_layout(fp, zone, unit_width_ft=MIX_WIDTH_FT.get(mix, 20.0))
    core = fp.difference(zone)
    core_rings = []
    for g in getattr(core, "geoms", [core]):
        if not g.is_empty and g.geom_type == "Polygon":
            core_rings.append([[round(x * METERS_TO_FEET, 1), round(y * METERS_TO_FEET, 1)] for x, y in g.exterior.coords])

    # Real bug, caught visually (two unit colors overlapping at a shared
    # corner): generate_unit_layout() guarantees zero overlap at full
    # precision, but rounding each polygon's coordinates independently to
    # 1 decimal place for the JSON response can reintroduce a tiny overlap
    # when two originally-adjacent units' shared boundary straddles a
    # rounding threshold (e.g. one vertex at -55.6499 rounds to -55.6, the
    # "same" vertex on the neighboring unit at -55.6501 rounds to -55.7).
    #
    # A first attempt clipped each rounded unit against the union of
    # already-processed ones - but .difference() introduces new precise
    # intersection vertices that then need rounding too, and THAT rounding
    # can push the boundary back outward past the clip, reintroducing the
    # same overlap one step later (confirmed: it moved the bug to other
    # buildings/mixes instead of fixing it). Fixed properly instead: erode
    # (shrink) each unit by half the maximum possible rounding error
    # (0.05ft, since rounding to 1 decimal moves any coordinate by at most
    # 0.05ft) BEFORE rounding. Two originally-adjacent units each shrink
    # away from their shared boundary by 0.05ft, leaving a real 0.1ft gap
    # that's still there no matter which direction each one's rounding
    # happens to go - the rounding can never close a gap that's already
    # bigger than the rounding error that caused the problem.
    # Real bug in the erosion fix itself, caught visually (a screenshot
    # showed the SAME overlap persisting after the fix): shapely's DEFAULT
    # buffer join style is "round", which approximates the inside corner at
    # a concave vertex with an ARC of several closely-spaced points instead
    # of one sharp corner. On a concave unit shape (Williams Tower's unit Y
    # has one), that arc degenerated into ~17 near-duplicate points all at
    # the same rounded coordinate - a malformed, pinched polygon that
    # visually overlapped its neighbor despite the erosion "succeeding" by
    # shapely's own validity check. join_style=2 (mitre) produces a single
    # sharp corner instead of an arc, avoiding this whole class of artifact.
    #
    # Second real bug, also caught visually (the SAME overlap, a third
    # time): even with clean, genuinely non-overlapping FILLS, the
    # frontend draws each unit with stroke-width="1.2" (app.js), and SVG
    # strokes are centered ON the polygon edge - extending ~0.6px outward
    # on EACH side of the fill boundary. At this plan's rendering scale
    # (a fixed 760px-wide viewBox divided by the real building's width in
    # feet, roughly 3-9 px/ft across the 5 featured buildings), the old
    # 0.05ft erosion produced a fill gap of only ~0.15-0.5px - smaller
    # than the combined ~1.2px of stroke the two neighboring units draw
    # into that same gap. The FILLS never overlapped; the STROKES (the
    # colored outlines) visually did. 0.4ft comfortably exceeds the
    # stroke width even at the largest real featured building's smallest
    # px/ft ratio, while costing only a few sqft of display-only area on
    # each unit's drawn shape (the real area_sqft/unit_type numbers shown
    # to the user come from the pre-erosion calculation and are
    # unaffected).
    ERODE_FT = 0.4
    rounded_units = []
    for u in units:
        poly = Polygon(u["polygon_ft"]).buffer(-ERODE_FT, join_style=2)
        if poly.is_empty or not hasattr(poly, "exterior"):
            continue
        rounded_units.append({"id": u.get("unit_id"), "type": u.get("unit_type"),
                              "area_sqft": round(u.get("area_sqft", 0)),
                              "polygon_ft": [[round(x, 1), round(y, 1)] for x, y in poly.exterior.coords]})

    # Final defensive safety net, on top of the erosion above, not instead
    # of it: if any two units still technically overlap (an edge case the
    # erosion math above doesn't anticipate), don't try to reshape either
    # one further - just drop the smaller unit entirely. A floor plan
    # that's honestly missing one small unit is a better real result than
    # one showing two colors visibly overlapping.
    # MIN_GAP_FT matches the erosion margin above: close the gap between
    # "technically not overlapping" and "visually not touching" by also
    # dropping a unit that's merely too close to another, not just one
    # that literally intersects it - the same stroke-width reasoning as
    # the erosion fix, applied as a hard guarantee instead of relying on
    # the erosion distance alone to always be enough.
    MIN_GAP_FT = 0.3
    kept = []
    for u in sorted(rounded_units, key=lambda u: u["area_sqft"], reverse=True):
        poly = Polygon(u["polygon_ft"])
        if any(poly.distance(Polygon(k["polygon_ft"])) < MIN_GAP_FT for k in kept):
            continue
        kept.append(u)
    rounded_units = sorted(kept, key=lambda u: u["id"] or "")
    # Recount from what's actually kept, not the pre-safety-net list - if
    # the safety net above ever does drop a unit, the displayed counts and
    # total sqft must match what's actually drawn, not silently overstate it.
    counts = {}
    for u in rounded_units:
        counts[u["type"]] = counts.get(u["type"], 0) + 1

    return {
        "mix": mix if mix in MIX_WIDTH_FT else "balanced",
        "outline_ft": [[round(x * METERS_TO_FEET, 1), round(y * METERS_TO_FEET, 1)] for x, y in fp.exterior.coords],
        "core_ft": core_rings,
        "units": rounded_units,
        "unit_counts": counts,
        "viable_sqft": round(sum(u["area_sqft"] for u in rounded_units)),
        "floor_sqft": round(fp.area * METERS_TO_FEET ** 2),
        "depth_ft": round(depth_ft, 1),
    }


# ---------------------------------------------------------------------------
# One summary shape for live and saved results
# ---------------------------------------------------------------------------

UNIT_LABEL = {"studio": "studio", "1br": "one-bed", "2br": "two-bed"}


def _money(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{sign}${x / 1e3:.0f}K"
    return f"{sign}${x:.0f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def parse_reasoning(text: str) -> dict:
    """The pipeline's reasoning string is the only place some real numbers
    live once a result is saved - pull them back out."""
    out: dict = {}
    m = re.search(r"Real unit mix: (.*?) \(([\d,]+) sqft", text or "")
    if m:
        counts = {}
        for part in m.group(1).split(","):
            bits = part.strip().split(" ")
            if len(bits) == 2 and bits[0].isdigit():
                counts[bits[1]] = int(bits[0])
        out["unit_counts"] = counts
        out["viable_sqft"] = float(m.group(2).replace(",", ""))
    m = re.search(r"office rent \$([\d.]+)/sf/yr", text or "")
    if m:
        out["office_rent"] = float(m.group(1))
    m = re.search(r"residential \$([\d.]+)/sf/yr", text or "")
    if m:
        out["residential_rent"] = float(m.group(1))
    m = re.search(r"At a ([\d.]+)% cap rate \((.*?)\)", text or "")
    if m:
        out["cap_rate"] = float(m.group(1))
        out["cap_rate_source"] = m.group(2)
    m = re.search(r"Construction ([\d,]+) floor sf x \$([\d,]+)-([\d,]+)/sf = \$([\d,]+)-\$([\d,]+), "
                  r"plus \$([\d,]+)-\$([\d,]+) compliance fees", text or "")
    if m:
        n = [float(x.replace(",", "")) for x in m.groups()]
        out.update(floor_sqft=n[0], cost_psf_low=n[1], cost_psf_high=n[2], construction_low=n[3],
                   construction_high=n[4], compliance_low=n[5], compliance_high=n[6])
    m = re.search(r"apartment value \$([\d,]+)", text or "")
    if m:
        out["apartment_value"] = float(m.group(1).replace(",", ""))
    m = re.search(r"Occupancy ([\d.]+)%", text or "")
    if m:
        out["occupancy"] = float(m.group(1))
    m = re.search(r"Share of rent kept after operating costs ([\d.]+)%", text or "")
    if m:
        out["noi_share"] = float(m.group(1))
    m = re.search(r"\(income rising with ([\d.]+)% inflation.*?discounted at ([\d.]+)% a year", text or "")
    if m:
        out["inflation_pct"], out["discount_pct"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"payback from net income alone ([\d.]+ years|never) to ([\d.]+ years|never)", text or "")
    if m:   # None with payback_never True means discounted income never covers that cost
        years = lambda g: None if g == "never" else float(g.split()[0])
        out["payback_low"], out["payback_high"] = years(m.group(1)), years(m.group(2))
        out["payback_known"] = True
    cash = {}
    for y, lo, hi in re.findall(r"Cash after (\d+) years: (-?\$[\d,]+) to (-?\$[\d,]+)", text or ""):
        num = lambda t: float(t.replace("$", "").replace(",", ""))
        cash[int(y)] = [num(lo), num(hi)]
    if cash:
        out["cash_by_year"] = cash
    out["vacant"] = "confirmed vacant" in (text or "")
    m = re.search(r"real comps?: (.+?km\))\)", text or "")
    if m:
        out["comps"] = [{"name": n.strip(), "km": float(k)} for n, k in re.findall(r"([^,()]+?) \(([\d.]+)km\)", m.group(1))]
    return out


def summarize(r: dict) -> dict:
    """r: verdict, confidence, window_to_core_max_ft, convertible_fraction,
    value_skipped, uplift_low/high, impact_low/high, reasoning_text, notes,
    optionally viable_sqft / unit_counts. Returns the checks, headline and
    money numbers the UI shows."""
    parsed = parse_reasoning(r.get("reasoning_text", ""))
    viable = r.get("viable_sqft") or parsed.get("viable_sqft")
    counts = r.get("unit_counts") or parsed.get("unit_counts") or {}
    notes = r.get("notes") or []
    verdict = r.get("verdict")
    depth = r.get("window_to_core_max_ft")
    partial = r.get("partial", False)
    checks = []

    # 1 floor depth
    if depth is None:
        state = {"pass": "pass", "fail": "fail"}.get(verdict, "review")
        checks.append({"id": "depth", "title": "Floor shape", "state": state,
                       "note": {"pass": "The shape check passed in the portfolio scan",
                                "fail": "Too deep to convert as shaped"}.get(state, "Flagged for a human check in the scan")})
    else:
        frac = r.get("convertible_fraction") or 0
        if verdict == "fail":
            checks.append({"id": "depth", "title": "Floor depth", "state": "fail",
                           "note": f"{depth:.0f} ft from window to core, too deep for apartments"})
        elif depth <= DEPTH_LIMIT_FT:
            checks.append({"id": "depth", "title": "Floor depth", "state": "pass" if verdict == "pass" else "review",
                           "note": f"{depth:.0f} ft from window to core, inside the {DEPTH_LIMIT_FT:.0f} ft limit"})
        else:
            checks.append({"id": "depth", "title": "Floor depth", "state": "pass" if verdict == "pass" else "review",
                           "note": f"{depth:.0f} ft at the deepest point, {frac:.0%} of the floor within reach of windows"})

    # 2 unit layout
    total_units = sum(counts.values())
    if total_units:
        mix = ", ".join(_plural(c, UNIT_LABEL.get(t, t)) for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))
        checks.append({"id": "units", "title": "Unit layout", "state": "pass", "note": f"{total_units} apartments fit: {mix}"})
    elif partial:
        checks.append({"id": "units", "title": "Unit layout", "state": "review", "note": "Not laid out yet for this building"})
    else:
        checks.append({"id": "units", "title": "Unit layout", "state": "fail", "note": "No apartment-sized units fit"})

    # 3 column spacing
    col = next((n for n in notes if n.startswith("Column bays")), None)
    if col:
        m = re.search(r"Column bays are (\d+)ft", col)
        checks.append({"id": "columns", "title": "Column spacing", "state": "review",
                       "note": f"{m.group(1)} ft bays, column positions need a site check" if m else "Needs a site check"})
    else:
        checks.append({"id": "columns", "title": "Column spacing", "state": "review",
                       "note": "No column data for this building, needs a site check"})

    # 4 office income (title must fit all 3 real outcomes, not just vacancy -
    # "Office vacancy" read as mislabeled when the actual finding was an
    # active lease, not a vacancy, caught from a real screenshot)
    if parsed.get("vacant"):
        checks.append({"id": "vacancy", "title": "Office income", "state": "pass", "note": "Floor is vacant, so no office rent is given up"})
    elif parsed.get("office_rent"):
        checks.append({"id": "vacancy", "title": "Office income", "state": "review",
                       "note": f"Leased at ${parsed['office_rent']:.2f} a sq ft, converting gives that up"})
    else:
        checks.append({"id": "vacancy", "title": "Office income", "state": "review", "note": "Lease status not confirmed"})

    # 5 value
    lo, hi = r.get("impact_low"), r.get("impact_high")
    if r.get("value_skipped") or lo is None:
        checks.append({"id": "value", "title": "Long-term profit after construction", "state": "fail" if r.get("value_skipped") else "review",
                       "note": "Not priced, the shape check failed" if r.get("value_skipped") else "Not priced yet"})
        value_state = "fail" if r.get("value_skipped") else "review"
    elif lo > 0:
        checks.append({"id": "value", "title": "Long-term profit after construction", "state": "pass", "note": f"Profit of {_money(lo)} to {_money(hi)}"})
        value_state = "pass"
    elif hi < 0:
        checks.append({"id": "value", "title": "Long-term profit after construction", "state": "fail", "note": f"Loss of {_money(-hi)} to {_money(-lo)}"})
        value_state = "fail"
    else:
        checks.append({"id": "value", "title": "Long-term profit after construction", "state": "review", "note": f"Between {_money(lo)} and {_money(hi)}, close to break-even"})
        value_state = "review"

    if verdict == "fail" or value_state == "fail":
        headline = {"state": "fail", "label": "Not worth converting"}
    elif verdict == "escalate" or value_state == "review":
        headline = {"state": "review", "label": "Needs a closer look"}
    elif partial:
        headline = {"state": "review", "label": "Promising, not fully analyzed"}
    else:
        headline = {"state": "pass", "label": "Worth converting"}

    numbers = {
        "viable_sqft": viable, "unit_counts": counts, "total_units": total_units,
        "reno_low": parsed["construction_low"] + parsed["compliance_low"] if "construction_low" in parsed else None,
        "reno_high": parsed["construction_high"] + parsed["compliance_high"] if "construction_high" in parsed else None,
        "floor_sqft": parsed.get("floor_sqft"), "cost_psf_low": parsed.get("cost_psf_low"),
        "cost_psf_high": parsed.get("cost_psf_high"), "compliance_low": parsed.get("compliance_low"),
        "compliance_high": parsed.get("compliance_high"), "apartment_value": parsed.get("apartment_value"),
        "occupancy": parsed.get("occupancy"), "noi_share": parsed.get("noi_share"),
        "payback_low": parsed.get("payback_low"), "payback_high": parsed.get("payback_high"),
        "payback_known": parsed.get("payback_known", False), "inflation_pct": parsed.get("inflation_pct"),
        "discount_pct": parsed.get("discount_pct"),
        "cash_by_year": parsed.get("cash_by_year"),
        "uplift_low": r.get("uplift_low"), "uplift_high": r.get("uplift_high"),
        "impact_low": lo, "impact_high": hi,
        "office_rent": parsed.get("office_rent"), "residential_rent": parsed.get("residential_rent"),
        "cap_rate": parsed.get("cap_rate"), "cap_rate_source": parsed.get("cap_rate_source"),
        "vacant": parsed.get("vacant", False), "comps": parsed.get("comps", []),
    }
    passed = sum(1 for c in checks if c["state"] == "pass")
    return {"headline": headline, "checks": checks, "numbers": numbers,
            "confidence": r.get("confidence"), "checks_passed": passed, "checks_total": len(checks)}


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------

def floors_for(property_id: int, analyzed_floor: int | None, stories: int | None) -> dict:
    """Per-floor lease status. With the CoStar kit: real, from the lease
    history. Without it: only the floor we actually analyzed is known."""
    if costar_available():
        try:
            from value import _iter_jsonl, load_property
            prop = load_property(property_id)
            if prop and prop.get("floors"):
                by_id = {fl.get("propertyFloorId"): fl.get("floorName") for fl in prop["floors"]}
                status = {name: "no_record" for name in by_id.values()}
                for rec in _iter_jsonl("location-occupancy.jsonl"):
                    if rec.get("propertyId") != property_id:
                        continue
                    for s in (rec.get("spaces") or []):
                        name = by_id.get(s.get("propertyFloorId"))
                        if name is None:
                            continue
                        if rec.get("toDate") is None:
                            status[name] = "occupied"
                        elif status[name] != "occupied":
                            status[name] = "vacant"
                floors = [{"floor": n, "status": st} for n, st in status.items() if isinstance(n, int)]
                floors.sort(key=lambda f: f["floor"])
                return {"source": "costar", "floors": floors}
        except Exception:
            pass
    n = max(int(stories or 0), int(analyzed_floor or 0))
    return {"source": "estimated", "floors": [{"floor": i, "status": "unknown"} for i in range(1, n + 1)]}


# ---------------------------------------------------------------------------
# Places nearby (OpenStreetMap)
# ---------------------------------------------------------------------------

PLACE_KINDS = {
    "hospital": {"label": "Hospital", "query": 'nwr[amenity=hospital]'},
    "school": {"label": "School", "query": 'nwr[amenity=school]'},
    "grocery": {"label": "Grocery store", "query": 'nwr[shop=supermarket]'},
    "highway": {"label": "Major highway", "query": 'way[highway=motorway]'},
}
HOUSTON_BBOX = (29.50, -95.80, 30.15, -95.15)
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
_places_cache: dict | None = None
_places_fetching = False


def fetch_places() -> dict:
    """One Overpass request for the whole Houston area, saved to disk."""
    import requests
    bbox = ",".join(str(x) for x in HOUSTON_BBOX)
    out = {}
    for kind, spec in PLACE_KINDS.items():
        q = f"[out:json][timeout:120];{spec['query']}({bbox});out center tags;"
        resp, last = None, None
        for attempt in range(6):  # the public Overpass servers are often briefly busy
            url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
            try:
                resp = requests.post(url, data={"data": q}, headers={"User-Agent": "oasis-conversion-demo/0.1"}, timeout=150)
                if resp.status_code == 200:
                    break
                last = f"{resp.status_code} from {url}"
            except requests.RequestException as e:
                last = str(e)
            resp = None
            time.sleep(4 * (attempt + 1))
        if resp is None:
            raise RuntimeError(f"Overpass did not answer for {kind}: {last}")
        rows, seen = [], set()
        for el in resp.json().get("elements", []):
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            tags = el.get("tags") or {}
            name = tags.get("name") or tags.get("ref") or tags.get("brand") or ""
            if kind != "highway" and not name:
                continue
            key = (name, round(lat, 3), round(lon, 3))
            if key in seen:
                continue
            seen.add(key)
            rows.append([name or "Highway", round(lat, 5), round(lon, 5)])
        out[kind] = rows
        time.sleep(1.5)
    out["_fetched"] = time.strftime("%Y-%m-%d")
    with open(PLACES_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    return out


def places() -> dict | None:
    """The cached places file. If it is missing, a background thread fetches
    it once (several minutes on the public Overpass servers) - a request
    never waits on that; the UI just shows the filters as unavailable."""
    global _places_cache, _places_fetching
    if _places_cache is None:
        if os.path.exists(PLACES_PATH):
            with open(PLACES_PATH) as f:
                _places_cache = json.load(f)
        elif not _places_fetching:
            import threading
            _places_fetching = True

            def _bg():
                try:
                    fetch_places()
                except Exception:
                    pass

            threading.Thread(target=_bg, daemon=True).start()
    return _places_cache


def nearest_by_kind(lat: float, lon: float) -> dict:
    """{kind: miles to the nearest one} - used by the home screen filter."""
    data = places()
    if not data:
        return {}
    out = {}
    for kind in PLACE_KINDS:
        best = None
        for _name, plat, plon in data.get(kind, []):
            if abs(plat - lat) > 0.2 or abs(plon - lon) > 0.25:
                continue
            d = miles(lat, lon, plat, plon)
            if best is None or d < best:
                best = d
        if best is not None:
            out[kind] = round(best, 2)
    return out


def places_near(lat: float, lon: float, radius_mi: float = 1.5, per_kind: int = 6) -> dict:
    data = places()
    if not data:
        return {"available": False, "reason": "Could not reach OpenStreetMap for nearby places."}
    out = {"available": True, "fetched": data.get("_fetched"), "kinds": {}}
    for kind, spec in PLACE_KINDS.items():
        rows = []
        for name, plat, plon in data.get(kind, []):
            if abs(plat - lat) > 0.05 or abs(plon - lon) > 0.06:
                continue
            d = miles(lat, lon, plat, plon)
            if d <= radius_mi:
                rows.append({"name": name, "lat": plat, "lon": plon, "miles": round(d, 2)})
        rows.sort(key=lambda r: r["miles"])
        if kind == "highway":  # many short segments of the same road - keep the nearest of each
            seen, uniq = set(), []
            for r in rows:
                if r["name"] not in seen:
                    seen.add(r["name"])
                    uniq.append(r)
            rows = uniq
        out["kinds"][kind] = {"label": spec["label"], "within_1mi": sum(1 for r in rows if r["miles"] <= 1.0),
                              "items": rows[:per_kind]}
    return out


# ---------------------------------------------------------------------------
# Demographics (US Census ACS 5-year) - needs CENSUS_API_KEY
# ---------------------------------------------------------------------------

ACS_VARS = {"population": "B01003_001E", "median_age": "B01002_001E", "median_income": "B19013_001E",
            "households": "B25003_001E", "renter_households": "B25003_003E", "median_rent": "B25064_001E"}
ACS_YEARS = [2019, 2020, 2021, 2022, 2023]


def demographics(lat: float, lon: float) -> dict:
    if not has_key("CENSUS_API_KEY"):
        return {"available": False,
                "reason": "Add a free CENSUS_API_KEY to .env to load census figures for this neighbourhood."}
    import requests
    try:
        g = requests.get("https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
                         params={"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                 "vintage": "Current_Current", "format": "json"}, timeout=25).json()
        tract = g["result"]["geographies"]["Census Tracts"][0]
        state, county, tract_id = tract["STATE"], tract["COUNTY"], tract["TRACT"]
    except Exception:
        return {"available": False, "reason": "The census geocoder did not answer for this location."}

    os.makedirs(AREA_CACHE_DIR, exist_ok=True)
    cache = os.path.join(AREA_CACHE_DIR, f"acs_{state}{county}{tract_id}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)

    key = "".join(os.environ["CENSUS_API_KEY"].split())
    series = []
    for year in ACS_YEARS:
        try:
            rows = requests.get(f"https://api.census.gov/data/{year}/acs/acs5",
                                params={"get": ",".join(ACS_VARS.values()), "for": f"tract:{tract_id}",
                                        "in": f"state:{state} county:{county}", "key": key}, timeout=25).json()
            vals = dict(zip(rows[0], rows[1]))
            rec = {"year": year}
            for name, var in ACS_VARS.items():
                v = float(vals[var]) if vals.get(var) not in (None, "") else None
                rec[name] = v if v is not None and v >= 0 else None
            series.append(rec)
        except Exception:
            continue
    if not series:
        return {"available": False, "reason": "The census API returned nothing for this tract."}
    latest = series[-1]
    renters = (latest["renter_households"] / latest["households"]) if latest.get("households") else None
    out = {"available": True, "tract": tract.get("NAME"), "year": latest["year"], "latest": latest,
           "renter_share": renters, "series": series,
           "source": f"US Census ACS 5-year, {series[0]['year']} to {latest['year']}"}
    with open(cache, "w") as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------------------
# Boardroom: three specialist agents
# ---------------------------------------------------------------------------

AGENTS = [
    {"id": "structural", "name": "Structural engineer", "initials": "SE",
     "brief": "You judge whether the floor can physically become apartments: window-to-core depth, "
              "column bays, how much of the plate fits real units, what must be checked on site."},
    {"id": "planner", "name": "City planner", "initials": "CP",
     "brief": "You judge whether the conversion is allowed and good for the area: Houston has no citywide "
              "zoning, so deed restrictions and parking are the real gates; you care about housing supply."},
    {"id": "market", "name": "Market analyst", "initials": "MA",
     "brief": "You judge whether the money works: apartment rent comps, office rent given up, construction "
              "cost, cap rate, and what is unknown about demand."},
]
AGENT_BY_ID = {a["id"]: a for a in AGENTS}
STANCES = ("for", "conditional", "against")


def _facts_text(facts: dict) -> str:
    keep = {k: v for k, v in facts.items() if v not in (None, [], {}, "")}
    return json.dumps(keep, indent=1, default=str)


def scripted_turn(agent_id: str, facts: dict, rnd: int, transcript: list) -> dict:
    """Rule-written statement from the real numbers, used when no API key is set."""
    n = facts.get("numbers", {})
    depth, verdict = facts.get("depth_ft"), facts.get("verdict")
    units, viable = n.get("total_units") or 0, n.get("viable_sqft")
    lo, hi = n.get("impact_low"), n.get("impact_high")
    floor = facts.get("floor")
    others = {t["agent"]: t for t in transcript}

    if agent_id == "structural":
        col = facts.get("column_note") or "I have no column data for this building"
        if verdict == "fail":
            stance = "against"
            text = (f"The floor is {depth:.0f} ft from window to core at its deepest and almost none of it fits a real apartment. "
                    f"I would not convert it as shaped.")
        elif depth is not None and depth > DEPTH_LIMIT_FT:
            stance = "conditional"
            text = (f"Parts of floor {floor} are {depth:.0f} ft deep, {depth - DEPTH_LIMIT_FT:.0f} ft over the {DEPTH_LIMIT_FT:.0f} ft limit, "
                    f"though {units} units still fit around the perimeter. {col}. I want a site survey before anyone commits.")
        else:
            stance = "conditional" if "bays" in col or verdict == "escalate" or depth is None else "for"
            text = (f"The plate works: {depth:.0f} ft to the core is inside the limit and {units} units fit. {col}."
                    if depth is not None else f"The scan passed the shape check, but I have no depth figure. {col}.")
        if rnd == 2:
            m = others.get("market")
            text = (("The rent case does not change the structure. " if m else "") +
                    {"for": "Nothing I have seen blocks this physically. Survey the columns during design and proceed.",
                     "conditional": "My condition stands: verify column positions and the deep spots on site, then I am comfortable.",
                     "against": "No amount of rent fixes a floor plate this deep. I stay against."}[stance])
        summary = {"for": f"The floor plate works for {units} apartments.",
                   "conditional": "Workable, but wants a site check of depth and columns first.",
                   "against": "The floor is too deep to convert as shaped."}[stance]

    elif agent_id == "planner":
        parking = facts.get("parking_note")
        stance = "conditional" if parking else "for"
        text = ("Houston has no citywide zoning, so there is no rezoning to win. The real gate is private deed restrictions, "
                "which are not in our data and need a title check. ")
        text += (parking + " " if parking else "")
        text += (f"{units} new homes in an empty office floor is a good outcome for the area." if n.get("vacant")
                 else f"{units} new homes help supply, but this floor still has an office tenant to consider.")
        if rnd == 2:
            s = others.get("structural")
            text = (("I take the engineer's point about a site survey; it fits inside normal due diligence. " if s and s["stance"] != "for" else "") +
                    {"for": "Run the deed check alongside the survey. I support moving ahead.",
                     "conditional": "Sort the parking question and the deed check, then I support it."}[stance])
        summary = {"for": "No zoning barrier; check deed restrictions as normal diligence.",
                   "conditional": "No zoning barrier, but parking and deed restrictions need checking."}[stance]

    else:  # market
        rent, office = n.get("residential_rent"), n.get("office_rent")
        comps = n.get("comps") or []
        comp_txt = (f"{len(comps)} apartment buildings within {max(c['km'] for c in comps):.1f} km average ${rent:.2f} a sq ft a year"
                    if comps and rent else (f"nearby apartments rent for about ${rent:.2f} a sq ft a year" if rent else "I have no rent comps here"))
        if lo is None:
            stance, text = "against", "The shape check failed, so there is nothing to price."
        elif lo > 0:
            stance = "for"
            text = (f"{comp_txt[0].upper() + comp_txt[1:]}. " +
                    ("The floor is vacant, so no office income is lost. " if n.get("vacant") else
                     (f"Office rent of ${office:.2f} a sq ft is given up. " if office else "")) +
                    f"After construction I get {_money(lo)} to {_money(hi)} of added value. I have no local vacancy rate, so demand is not proven.")
        elif hi < 0:
            stance = "against"
            text = (f"{comp_txt[0].upper() + comp_txt[1:]}. " +
                    (f"The floor earns ${office:.2f} a sq ft as offices today. " if office else "") +
                    f"After construction the conversion loses {_money(-hi)} to {_money(-lo)}. The numbers do not work.")
        else:
            stance = "conditional"
            text = f"{comp_txt[0].upper() + comp_txt[1:]}. The result runs from {_money(lo)} to {_money(hi)}, too close to break-even to call."
        if rnd == 2:
            s = others.get("structural")
            extra = "A survey that finds column problems would add cost I have not priced. " if s and s["stance"] == "conditional" else ""
            text = extra + {"for": "Even at the high construction cost the value stays positive. I support it.",
                            "conditional": "I would want firm contractor quotes before deciding.",
                            "against": "Unless construction comes in far below $150 a sq ft, I stay against."}[stance]
        summary = {"for": f"Rents support it: {_money(lo)} to {_money(hi)} added." if lo is not None else "",
                   "conditional": "Close to break-even; needs real contractor quotes.",
                   "against": "The conversion loses money at these rents and costs." if lo is not None else "Nothing to price."}[stance]

    return {"agent": agent_id, "stance": stance, "text": text, "summary": summary, "round": rnd, "live": False}


def board_turn(agent_id: str, facts: dict, rnd: int, transcript: list) -> dict:
    if agent_id not in AGENT_BY_ID:
        raise ValueError("unknown agent")
    return scripted_turn(agent_id, facts, rnd, transcript)


# ---------------------------------------------------------------------------
# Quotes: the email agent
# ---------------------------------------------------------------------------

def load_contractors() -> list:
    return [
        {"id": str(i), "contact": f"{first} {last}", "email": email,
         "sample": quote_mail.is_sample(email)}
        for i, (first, last, email) in enumerate(quote_mail.contractors(), 1)
    ]


def draft_email(facts: dict, sender: str) -> dict:
    n = facts.get("numbers", {})
    counts = n.get("unit_counts") or {}
    mix = ", ".join(_plural(c, UNIT_LABEL.get(t, t)) for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))
    name, floor = facts.get("name"), facts.get("floor")
    where = f"floor {floor} of {name}" if floor is not None else f"a floor of {name}"
    subject = f"Quote request: office to apartments, {where.replace(' of ', ', ')}"
    lines = [
        "Hello {contractor},",
        "",
        f"We are planning to convert {where} in Houston from offices to apartments, and would like a quote from you.",
        "",
    ]
    if n.get("viable_sqft"):
        lines.append(f"The floor has about {n['viable_sqft']:,.0f} usable square feet. "
                     + (f"Our tentative plan has {n.get('total_units')} apartments: {mix}. " if mix else "")
                     + ("The floor is vacant today." if n.get("vacant") else ""))
        lines.append("")
    lines += [
        "The feasibility report is attached. Could you send a price range and a rough timeline within two weeks?",
        "",
        "Thank you,",
        sender or "[Your name]",
    ]
    body, live = "\n".join(lines), False
    if has_key("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            resp = anthropic.Anthropic().messages.create(
                model=OASIS_MODEL, max_tokens=2000, output_config={"effort": "low"},
                system=("You write short, plain quote-request emails to building contractors on behalf of a property owner. "
                        "Use only the facts given. Keep the literal token {contractor} in the greeting. "
                        "Under 130 words. Return only the email body."),
                messages=[{"role": "user", "content": f"Facts:\n{_facts_text(facts)}\n\nSign off as: {sender or '[Your name]'}\n\n"
                                                       f"Rewrite this draft, keeping every fact:\n\n{body}"}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason != "refusal" and "{contractor}" in text:
                body, live = text, True
        except Exception:
            pass
    return {"subject": subject, "body": body, "live": live}


def send_quotes(subject: str, body: str, contractor_ids: list, attachments: dict,
                *, save_draft: bool = False) -> dict:
    chosen = [c for c in load_contractors() if c["id"] in contractor_ids]
    if not chosen or set(contractor_ids) != {c["id"] for c in chosen}:
        raise ValueError("Select contractors from the demo list.")
    if "\n" in subject or "\r" in subject:
        raise ValueError("The subject must be a single line.")

    live = save_draft or os.getenv("OASIS_SEND_EMAIL") == "1"
    if live and not save_draft and any(c["sample"] for c in chosen):
        raise ValueError("Replace the sample addresses with demo inboxes before sending through Gmail.")
    smtp = live and not save_draft and bool(quote_mail.smtp_password())
    gmail = quote_mail.Gmail(quote_mail.profile_email()) if live and not smtp else None
    if not live:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
    batch = uuid4().hex
    results = []
    for contractor in chosen:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = quote_mail.profile_email()
        msg["To"] = contractor["email"]
        msg.set_content(body.replace("{contractor}", contractor["contact"]))
        for filename, content in attachments.items():
            maintype, subtype = {
                "html": ("text", "html"),
                "txt": ("text", "plain"),
                "svg": ("image", "svg+xml"),
                "png": ("image", "png"),
            }[filename.rsplit(".", 1)[-1]]
            if maintype == "text":
                msg.add_attachment(content, subtype=subtype, filename=filename)
            else:
                data = content.encode("utf-8") if isinstance(content, str) else content
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        result = {"id": contractor["id"]}
        if live:
            try:
                if smtp:
                    quote_mail.send_smtp(msg)
                else:
                    gmail.deliver(msg, save_draft)
                result["status"] = "drafted" if save_draft else "sent"
            except quote_mail.GmailError as exc:
                result.update(status="failed", error=str(exc))
        else:
            path = os.path.join(OUTBOX_DIR, f"{batch}_{contractor['id']}.eml")
            with open(path, "wb") as f:
                f.write(msg.as_bytes())
            result.update(status="saved", file=os.path.relpath(path, ROOT))
        results.append(result)
    return {"mode": "gmail-draft" if save_draft else "smtp" if smtp else "gmail" if gmail else "outbox",
            "results": results}
