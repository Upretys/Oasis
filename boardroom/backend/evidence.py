"""Turn a building + its jurisdiction into numbered, anchored facts. Pure code, deterministic."""
from datetime import date

import numpy as np
import shapely
from shapely.geometry import shape
from shapely.ops import unary_union

from backend.contracts import Anchor, Building, Evidence, Jurisdiction

COST_PER_SF = 350.0   # ASSUMPTION: all-in office-to-residential conversion cost per sf of GBA
NOI_MARGIN = 0.60     # ASSUMPTION: residential NOI as a share of gross rent
PREFIX = {"geometry": "GEO", "aerial": "AER", "costar": "CST", "zoning": "ZON", "derived": "DER"}


def plan_metrics(b: Building) -> dict:
    """Floor plate area, deepest point from any window (vertices + 1 ft grid), daylit share."""
    feats = b.plan["features"]
    plate = next(shape(f["geometry"]) for f in feats if f["properties"]["kind"] == "floor_plate")
    windows = unary_union([shape(f["geometry"]) for f in feats if f["properties"]["kind"] == "window"])
    x0, y0, x1, y1 = plate.bounds
    gx, gy = np.meshgrid(np.arange(np.floor(x0), np.ceil(x1) + 1), np.arange(np.floor(y0), np.ceil(y1) + 1))
    gx, gy = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(plate, gx, gy)
    verts = np.array(plate.exterior.coords[:-1])
    xs = np.concatenate([verts[:, 0], gx[inside]])
    ys = np.concatenate([verts[:, 1], gy[inside]])
    dist = shapely.distance(shapely.points(xs, ys), windows)
    i = int(np.argmax(dist))
    daylit = plate.intersection(windows.buffer(b.aerial.daylight_limit_ft)).area
    return {"plate_sf": plate.area, "deepest_ft": float(dist[i]), "deepest_xy": (float(xs[i]), float(ys[i])),
            "daylit_share": daylit / plate.area}


def _lease_groups(b: Building) -> list[list]:
    """Consecutive upper floors (2+) sharing one tenant and expiry. Ground floor stays retail."""
    groups: list[list] = []
    for row in sorted(b.floors, key=lambda r: r.floor):
        if row.floor < 2 or row.tenant is None:
            continue
        last = groups[-1][-1] if groups else None
        if last and last.floor == row.floor - 1 and (last.tenant, last.lease_expiry) == (row.tenant, row.lease_expiry):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def build_evidence(b: Building, j: Jurisdiction) -> list[Evidence]:
    out: list[Evidence] = []
    counts: dict[str, int] = {}

    def add(layer, label, value, unit, **anchor):
        counts[layer] = counts.get(layer, 0) + 1
        out.append(Evidence(id=f"E-{PREFIX[layer]}-{counts[layer]:02d}", layer=layer, label=label,
                            value=value, unit=unit, anchor=Anchor(**anchor)))

    m = plan_metrics(b)
    n_floors = len(b.floors)
    gba = m["plate_sf"] * n_floors
    limit = b.aerial.daylight_limit_ft
    c = b.costar

    # geometry (Vexcel footprint -> plan feet)
    add("geometry", "Typical floor plate area", round(m["plate_sf"]), "sf", kind="plan_region", ref="floor_plate")
    add("geometry", "Deepest point from a window", round(m["deepest_ft"], 1), "ft",
        kind="plan_point", x=m["deepest_xy"][0], y=m["deepest_xy"][1])
    add("geometry", "Share of floor plate within the daylight limit", round(100 * m["daylit_share"], 1), "%",
        kind="plan_region", ref="daylit_zone")

    # aerial (daylight context); a fact nobody measured is left out, not filled in
    limit_label = ("Daylight limit from a window (assumed rule of thumb, no obstruction data)"
                   if b.aerial.daylight_limit_assumed else "Daylight limit from a window, given obstructions")
    for label, value in [(limit_label, limit),
                         ("Height of the facing building", b.aerial.facing_building_height_ft),
                         ("Street width", b.aerial.street_width_ft)]:
        if value is not None:
            add("aerial", label, value, "ft", kind="aerial_region", ref=b.aerial.region_id)

    # costar
    if c.column_depth_ft is not None:
        add("costar", "Column bay depth", c.column_depth_ft, "ft", kind="costar_row", ref=c.row_id)
    if c.office_asking_rent_psf_yr is not None:
        add("costar", "Office asking rent", c.office_asking_rent_psf_yr, "$/sf/yr", kind="costar_row", ref=c.row_id)
    add("costar", "Multifamily comp rent", c.mf_rent_psf_month, "$/sf/month", kind="costar_row", ref=c.row_id)
    add("costar", "Cap rate (assumed default, no sale comp)" if c.cap_rate_assumed else "Cap rate",
        round(100 * c.cap_rate, 2), "%", kind="costar_row", ref=c.row_id)
    as_of = date.fromisoformat(b.data_as_of)
    for g in _lease_groups(b):
        span = f"Floor {g[0].floor}" if len(g) == 1 else f"Floors {g[0].floor}-{g[-1].floor}"
        if g[0].lease_expiry is None:   # occupied, but no end date on record
            add("costar", f"{span} lease: end date not on record", "unknown", None,
                kind="costar_row", floor=g[0].floor, ref=g[0].costar_row)
            continue
        days = (date.fromisoformat(g[0].lease_expiry) - as_of).days
        if days < 730:
            add("costar", f"{span} lease: months to expiry", round(days / 30.44, 1), "months",
                kind="costar_row", floor=g[0].floor, ref=g[0].costar_row)
        else:
            add("costar", f"{span} lease: years remaining", round(days / 365.25, 1), "years",
                kind="costar_row", floor=g[0].floor, ref=g[0].costar_row)

    # zoning (only fields the rules file actually publishes)
    zone = dict(kind="map_region", ref=j.zoning_district)
    add("zoning", "Conversion pathway", j.conversion_pathway, None, **zone)
    for field, label, unit in [("approval_months", "Approval timeline", "months"),
                               ("max_far", "Maximum FAR", "FAR"),
                               ("max_height_ft", "Maximum height", "ft"),
                               ("set_aside_pct", "Affordable set-aside", "%")]:
        if getattr(j, field) is not None:
            add("zoning", label, getattr(j, field), unit, **zone)
    for inc in j.incentives:
        text = f"{inc.name}: {inc.benefit}" + (f"; obligation: {inc.obligation}" if inc.obligation else "")
        add("zoning", "Published incentive", text, None, **zone)

    # derived (code arithmetic on the facts above)
    beyond = max(0.0, m["deepest_ft"] - limit)
    add("derived", "Depth beyond the daylight limit", round(beyond, 1), "ft", kind="plan_region", ref="dark_core")
    if c.column_depth_ft:
        add("derived", "Column bays beyond the daylight limit", round(beyond / c.column_depth_ft, 1), "bays",
            kind="plan_region", ref="dark_core")
    if b.lot_area_sf:
        add("derived", "Existing FAR", round(gba / b.lot_area_sf, 1), "FAR", kind="building")
    add("derived", f"Conversion cost ({n_floors} floors x {m['plate_sf']:,.0f} sf x assumed ${COST_PER_SF:.0f}/sf)",
        round(gba * COST_PER_SF), "$", kind="building")
    value = gba * m["daylit_share"] * c.mf_rent_psf_month * 12 * NOI_MARGIN / c.cap_rate
    cap = "assumed cap rate" if c.cap_rate_assumed else "CoStar cap rate"
    add("derived", f"Residential value (daylit area only, assumed {NOI_MARGIN:.0%} NOI margin, {cap})",
        round(value), "$", kind="building")
    return out
