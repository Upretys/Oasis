"""Build a boardroom Building from a real Oasis (Houston) analysis.

Oasis already produces everything the seats need, in its own shapes:
  - a pipeline result (one entry of demo/all_results.json, the same fields as
    track_c_agent.agent.PipelineResult, so a live run maps the same way),
  - the building's row in demo/render_floor.BUILDINGS (floor area, column depth),
  - the cached Vexcel /property/extract response in output/.

Nothing is invented. A fact Oasis does not have stays None and build_evidence()
leaves it out: no aerial obstruction data, usually no column depth, no lot area.
The two stand-ins that are needed are flagged as assumptions, so their evidence
labels say so: the 45 ft daylight limit (Oasis's own WINDOW_TO_CORE_THRESHOLD_FT)
and the default cap rate when no sale comp exists.

The money comes from Oasis too (Building.economics), so the board argues over
the same cost and value the Oasis UI shows, not the boardroom's flat $/sf model.

    from backend.oasis_adapter import load_saved_building
    b = load_saved_building("Williams Tower")
"""
import ast
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from backend.contracts import AerialContext, Building, CostarSummary, Economics, FloorRow

OASIS = Path(__file__).resolve().parents[2]          # repo root: track_*/, demo/, output/
SAVED_RESULTS = OASIS / "demo" / "all_results.json"
RENDER_FLOOR = OASIS / "demo" / "render_floor.py"
OUTPUT = OASIS / "output"

if str(OASIS / "track_a_geometry") not in sys.path:
    sys.path.insert(0, str(OASIS / "track_a_geometry"))
from geometry import METERS_TO_FEET, WINDOW_TO_CORE_THRESHOLD_FT, load_scaled_footprint  # noqa: E402

# The classifier's label sets the triage band; its confidence places the score
# inside that band. Bands match triage.py: pass >= 0.5, escalate >= 0.2.
VIABILITY_BANDS = {"feasible": (0.5, 1.0), "marginal": (0.2, 0.5), "not_feasible": (0.0, 0.2)}


def viability(label: str, confidence: float) -> float:
    lo, hi = VIABILITY_BANDS[label]
    hi -= 0.001                                       # stay strictly inside the band
    if label == "not_feasible":                       # more confident it fails -> lower score
        return round(hi - (hi - lo) * confidence, 3)
    return round(lo + (hi - lo) * confidence, 3)


def cached_buildings() -> list[dict]:
    """demo/render_floor.BUILDINGS, read without importing the module (it pulls
    in matplotlib and the CoStar loaders, which the boardroom doesn't need)."""
    tree = ast.parse(RENDER_FLOOR.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BUILDINGS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise LookupError("BUILDINGS not found in demo/render_floor.py")


# --- facts Oasis only states in its reasoning text ---------------------------

_OFFICE = re.compile(r"office rent \$([\d,.]+)/sf/yr \((.*?)\) vs\. residential")
_RESI = re.compile(r"residential \$([\d,.]+)/sf/yr")
_LEASE_END = re.compile(r"Current lease runs through (\d{4}-\d{2}-\d{2})")
_UPLIFT = re.compile(r"-> \$(-?[\d,.]+)/yr uplift on ([\d,.]+) convertible sf")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def economics(result: dict, reasoning: str) -> Optional[Economics]:
    """Oasis's own money model, unchanged. The cost range is read back out of
    Oasis's outputs (value - net impact) rather than recomputed from its
    $150-220/sf constants, so the two can never drift apart."""
    up, office = _UPLIFT.search(reasoning), _OFFICE.search(reasoning)
    lo, hi = result.get("implied_value_impact_low"), result.get("implied_value_impact_high")
    if result.get("value_skipped") or not (up and office) or lo is None or hi is None:
        return None
    uplift, value = _num(up.group(1)), _num(up.group(1)) / result["cap_rate_used"]
    return Economics(
        source="Oasis", convertible_sf=_num(up.group(2)),
        office_rent_given_up_psf_yr=_num(office.group(1)),
        office_rent_assumed="current tenant found" in office.group(2),
        rent_uplift_usd_yr=uplift, value_usd=value,
        cost_low_usd=value - hi, cost_high_usd=value - lo, net_low_usd=lo, net_high_usd=hi,
    )


def _office(reasoning: str) -> tuple[Optional[float], str]:
    """(asking rent if it is a real lease rate, occupancy: vacant | occupied | unknown)."""
    m = _OFFICE.search(reasoning)
    if not m:
        return None, "unknown"
    rent, source = float(m.group(1).replace(",", "")), m.group(2)
    if "confirmed vacant" in source:
        return None, "vacant"
    if "real lease deal" in source:
        return rent, "occupied"
    if "current tenant found" in source:              # tenant is real, the $24 rate is an assumption
        return None, "occupied"
    return None, "unknown"


def _plan(vexcel: dict, lat: float, lon: float, floor_sqft: float) -> dict:
    """Vexcel footprint scaled to CoStar's floor area, in plan feet from (0, 0).
    Every exterior edge counts as a window: Oasis assumes curtain-wall glazing
    (none of its sources record window positions)."""
    poly = load_scaled_footprint(vexcel, lat, lon, floor_sqft)
    if poly is None:
        raise ValueError(f"no Vexcel structure near ({lat}, {lon})")
    x0, y0, _, _ = poly.bounds
    ring = [[round((x - x0) * METERS_TO_FEET, 2), round((y - y0) * METERS_TO_FEET, 2)]
            for x, y in poly.exterior.coords]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "floor_plate"},
         "geometry": {"type": "Polygon", "coordinates": [ring]}},
        {"type": "Feature", "properties": {"kind": "window", "facade": "all (curtain wall assumed)"},
         "geometry": {"type": "LineString", "coordinates": ring}},
    ]}


def building_from_result(result: dict, cached: dict, vexcel: dict, as_of: Optional[str] = None) -> Building:
    """Map one Oasis pipeline result for one floor into a boardroom Building."""
    pid, floor = cached["property_id"], int(cached["floor_name"])
    reasoning = result.get("reasoning_text", "")
    office_rent, occupancy = _office(reasoning)
    resi = _RESI.search(reasoning)
    if not resi:
        raise ValueError(f"{cached['name']}: no residential rent in the Oasis result")
    lease_end = _LEASE_END.search(reasoning)
    tenant = {"vacant": None, "occupied": "Current tenant (name not in the Oasis result)",
              "unknown": "Occupancy not on record"}[occupancy]

    return Building(
        building_id=str(pid),
        name=cached["name"],
        lat=cached["query_lat"],
        lon=cached["query_lon"],
        viability=viability(result["classification"], result["classification_confidence"]),
        data_as_of=as_of or date.today().isoformat(),
        lot_area_sf=None,
        # One row: Oasis analyses a single floor, so the board judges that floor.
        floors=[FloorRow(floor=floor, rsf=cached["real_floor_area_sqft"], tenant=tenant,
                         lease_expiry=lease_end.group(1) if lease_end else None,
                         costar_row=f"CS-{pid}-FL{floor:02d}")],
        plan=_plan(vexcel, cached["query_lat"], cached["query_lon"], cached["real_floor_area_sqft"]),
        aerial=AerialContext(region_id=f"VEX-{pid}", daylight_limit_ft=WINDOW_TO_CORE_THRESHOLD_FT,
                             daylight_limit_assumed=True),
        costar=CostarSummary(
            row_id=f"CS-{pid}",
            column_depth_ft=cached.get("real_column_depth_ft"),
            office_asking_rent_psf_yr=office_rent,
            mf_rent_psf_month=round(float(resi.group(1).replace(",", "")) / 12, 2),
            cap_rate=result["cap_rate_used"],
            cap_rate_assumed="default assumption" in reasoning,
        ),
        economics=economics(result, reasoning),
    )


def load_saved_building(name_or_id: str | int, as_of: Optional[str] = None) -> Building:
    """A featured building from Oasis's saved results; works without the CoStar kit."""
    key = str(name_or_id).lower()
    cached = next((b for b in cached_buildings()
                   if key in (b["name"].lower(), str(b["property_id"]))), None)
    if cached is None:
        raise KeyError(f"{name_or_id!r} is not one of Oasis's featured buildings")
    saved = json.loads(SAVED_RESULTS.read_text(encoding="utf-8"))
    result = next(r for r in saved if r["floor_id"] == f"{cached['property_id']}-floor-{cached['floor_name']}")
    vexcel = json.loads((OUTPUT / cached["vexcel_file"]).read_text(encoding="utf-8"))
    return building_from_result(result, cached, vexcel, as_of)
