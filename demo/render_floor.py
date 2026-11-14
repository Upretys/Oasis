"""
Phase 2 - Demo visual layer (first pass: matplotlib, not a web page)

Takes the real polygon output from Track A's run_feasibility() and draws the
footprint + generated unit layout to scale, color-coded by convertibility.
No simulation - this is static geometry we already computed, just rendered.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_a_geometry"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "track_c_agent"))

import matplotlib
matplotlib.use("Agg")  # non-interactive backend - required when called from Flask's worker threads
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon

from agent import run_pipeline  # noqa: E402
from geometry import load_scaled_footprint, METERS_TO_FEET  # noqa: E402

VERDICT_COLORS = {"pass": "#2e7d32", "escalate": "#f9a825", "fail": "#c62828"}
UNIT_TYPE_COLORS = {"studio": "#4a90d9", "1br": "#2e7d32", "2br": "#8e44ad", "not_viable": "#999999"}


def render(property_id: int, floor_name: int, vexcel_response: dict, query_lat: float,
           query_lon: float, real_floor_area_sqft: float, real_column_depth_ft: float,
           out_path: str | None = None):
    """Draws the real floor plan. Saves to out_path when given (the batch/CLI
    use), or returns (png_bytes, result, geo) in memory when out_path is None
    (the live web demo use - no temp file, no disk round-trip)."""
    result = run_pipeline(
        property_id=property_id, floor_name=floor_name, vexcel_response=vexcel_response,
        query_lat=query_lat, query_lon=query_lon, real_floor_area_sqft=real_floor_area_sqft,
        real_column_depth_ft=real_column_depth_ft,
    )

    footprint = load_scaled_footprint(vexcel_response, query_lat, query_lon, real_floor_area_sqft)
    footprint_ft = [(x * METERS_TO_FEET, y * METERS_TO_FEET) for x, y in footprint.exterior.coords]

    # Re-run geometry directly to get the real unit polygons (run_pipeline doesn't return them)
    from geometry import run_feasibility
    geo = run_feasibility(
        floor_id=f"{property_id}-floor-{floor_name}", vexcel_response=vexcel_response,
        query_lat=query_lat, query_lon=query_lon, real_floor_area_sqft=real_floor_area_sqft,
        real_column_depth_ft=real_column_depth_ft,
    )

    # Scale figure size to the floor's actual footprint extent and unit
    # count - a fixed 11x10 figure made text overlap on Houston Technology
    # Center's 66-unit floor (which has a huge real footprint). Bigger
    # figure + smaller/fewer-line labels at high unit counts keeps it legible.
    width_ft = max(p[0] for p in footprint_ft) - min(p[0] for p in footprint_ft)
    height_ft = max(p[1] for p in footprint_ft) - min(p[1] for p in footprint_ft)
    aspect = width_ft / height_ft if height_ft else 1.0
    n_units = len(geo.unit_layout_polygons)
    scale = 1.0 if n_units <= 30 else 1.6
    fig_h = 10 * scale
    fig, ax = plt.subplots(figsize=(fig_h * max(aspect, 0.7), fig_h))
    ax.set_aspect("equal")
    label_fontsize = 6.3 if n_units <= 30 else 4.6
    compact_labels = n_units > 30
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fafafa")

    # Footprint outline
    fx, fy = zip(*footprint_ft)
    ax.plot(fx, fy, color="#222", linewidth=2.2, zorder=3, label="Real floor outline (Vexcel, scaled to real area)")
    ax.fill(fx, fy, color="#e8e8e8", zorder=0)

    # Real column-bay-depth line, when CoStar has that structural field. This
    # is the ONLY real structural spacing datum available (confirmed: no
    # companion bay-width/column-grid field exists anywhere in the kit's
    # schema) - we don't have real column coordinates, so this can't be a
    # true per-column collision check. But drawing the real depth as an
    # inward-offset line, and flagging which SPECIFIC units actually sit near
    # it, is a real, honest improvement over one blanket sentence for the
    # whole floor: a reviewer can see exactly which units to double-check on
    # site, using the same real number, not an invented one.
    units_near_column_line = set()
    if real_column_depth_ft:
        from shapely.geometry import Polygon as ShapelyPolygon
        col_depth_m = real_column_depth_ft / METERS_TO_FEET
        col_line_poly = footprint.buffer(-col_depth_m)
        if not col_line_poly.is_empty:
            boundaries = [col_line_poly] if col_line_poly.geom_type == "Polygon" else list(col_line_poly.geoms)
            for i, geom in enumerate(boundaries):
                bx, by = zip(*[(x * METERS_TO_FEET, y * METERS_TO_FEET) for x, y in geom.exterior.coords])
                ax.plot(bx, by, color="#c62828", linewidth=1.3, linestyle="--", zorder=3,
                        label="Likely column line (real CoStar bay depth)" if i == 0 else None)
            tolerance_m = 3.0 / METERS_TO_FEET  # within ~3ft of the real bay depth line
            col_boundary = col_line_poly.boundary
            for unit in geo.unit_layout_polygons:
                pts_m = [(x / METERS_TO_FEET, y / METERS_TO_FEET) for x, y in unit["polygon_ft"]]
                if ShapelyPolygon(pts_m).distance(col_boundary) < tolerance_m:
                    units_near_column_line.add(unit["unit_id"])

    # Generated units, color-coded by real unit type, labeled with real sqft
    type_counts = {}
    for unit in geo.unit_layout_polygons:
        pts = unit["polygon_ft"]
        utype = unit.get("unit_type", "studio")
        area = unit.get("area_sqft", 0)
        near_column = unit["unit_id"] in units_near_column_line
        type_counts[utype] = type_counts.get(utype, 0) + 1
        color = UNIT_TYPE_COLORS.get(utype, "#999")
        edge_color = "#c62828" if near_column else "white"
        edge_width = 2.2 if near_column else 1.3
        patch = MplPolygon(pts, closed=True, facecolor=color, edgecolor=edge_color, alpha=0.88,
                            linewidth=edge_width, zorder=2)
        ax.add_patch(patch)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        label_name = {"studio": "Studio", "1br": "1BR", "2br": "2BR"}.get(utype, utype)
        flag = " ⚠" if (near_column and not compact_labels) else ""
        label_text = (f"{label_name[0]}\n{area:,.0f}" if compact_labels
                      else f"{unit['unit_id']}\n{label_name}\n{area:,.0f} sf{flag}")
        ax.text(cx, cy, label_text,
                ha="center", va="center", fontsize=label_fontsize, color="white", fontweight="bold",
                zorder=4, linespacing=1.2)

    verdict_color = VERDICT_COLORS.get(result.geometry_verdict, "#999")
    title = (
        f"Property {property_id}, Floor {floor_name}  —  verdict: {result.geometry_verdict.upper()} "
        f"({result.geometry_confidence:.0%} confidence)\n"
        f"Depth {result.window_to_core_max_ft:.1f}ft (45ft threshold)  |  "
        f"{len(geo.unit_layout_polygons)} real units, {geo.viable_leasable_sqft:,.0f} sf total"
    )
    if not result.value_skipped:
        title += f"\nValue impact: ${result.implied_value_impact_low:,.0f} to ${result.implied_value_impact_high:,.0f}"
    ax.set_title(title, fontsize=11, color=verdict_color, fontweight="bold", pad=14)
    ax.set_xlabel("feet")
    ax.set_ylabel("feet")

    # Legend: unit types actually present, plus the floor outline
    from matplotlib.patches import Patch
    legend_handles = [plt.Line2D([0], [0], color="#222", linewidth=2.2, label="Real floor outline")]
    if real_column_depth_ft:
        legend_handles.append(plt.Line2D([0], [0], color="#c62828", linewidth=1.3, linestyle="--",
                                          label=f"Likely column line ({real_column_depth_ft:.0f}ft bay depth)"))
    for utype, count in sorted(type_counts.items()):
        label_name = {"studio": "Studio", "1br": "1BR", "2br": "2BR"}.get(utype, utype)
        legend_handles.append(Patch(facecolor=UNIT_TYPE_COLORS.get(utype, "#999"),
                                     label=f"{label_name} ({count})"))
    if units_near_column_line:
        legend_handles.append(Patch(facecolor="#999", edgecolor="#c62828", linewidth=2.2,
                                     label=f"⚠ near column line ({len(units_near_column_line)})"))
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8, framealpha=0.95)
    ax.grid(True, alpha=0.15, zorder=0)

    if out_path is None:
        import io
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read(), result, geo

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {out_path}")
    print(json.dumps(result.__dict__ if hasattr(result, "__dict__") else result, indent=2, default=str))


BUILDINGS = [
    {
        "name": "First Community Tower", "property_id": 9008561, "floor_name": 37,
        "vexcel_file": "property.json", "query_lat": 29.995495, "query_lon": -95.579652,
        "real_floor_area_sqft": 23531.0, "real_column_depth_ft": 40.0,
    },
    {
        "name": "Williams Tower", "property_id": 233903, "floor_name": 18,
        "vexcel_file": "property_29.7375702_-95.461339.json", "query_lat": 29.7375702, "query_lon": -95.461339,
        "real_floor_area_sqft": 22700.0, "real_column_depth_ft": None,
    },
    {
        "name": "TC Energy Center", "property_id": 233905, "floor_name": 24,
        "vexcel_file": "property_29.7607687_-95.366467.json", "query_lat": 29.7607687, "query_lon": -95.366467,
        "real_floor_area_sqft": 22875.0, "real_column_depth_ft": None,
    },
    {
        "name": "1415 Louisiana", "property_id": 233928, "floor_name": 60,
        "vexcel_file": "property_29.7547798_-95.3698082.json", "query_lat": 29.7547798, "query_lon": -95.3698082,
        "real_floor_area_sqft": 12000.0, "real_column_depth_ft": None,
    },
    {
        "name": "5555 San Felipe", "property_id": 233930, "floor_name": 34,
        "vexcel_file": "property_29.7498236_-95.4720879.json", "query_lat": 29.7498236, "query_lon": -95.4720879,
        "real_floor_area_sqft": 27000.0, "real_column_depth_ft": None,
    },
    {
        "name": "Belchase Bldg", "property_id": 240054, "floor_name": 27,
        "vexcel_file": "property_29.9416795_-95.4018971.json", "query_lat": 29.9416795, "query_lon": -95.4018971,
        "real_floor_area_sqft": 31864.0, "real_column_depth_ft": None,
    },
    {
        "name": "Houston Technology Center", "property_id": 240117, "floor_name": 24,
        "vexcel_file": "property_29.7132915_-95.5505272.json", "query_lat": 29.7132915, "query_lon": -95.5505272,
        "real_floor_area_sqft": 141833.0, "real_column_depth_ft": None,
    },
    # Found by deliberately searching for REAL high-vacancy Houston office
    # buildings (confirmed via location-occupancy.jsonl: real lease history
    # exists, every lease has ended, none current) rather than picking by
    # size/stories alone. These are the first buildings where a real
    # foregone-office-income bug fix (a genuinely vacant floor has $0 real
    # office income to give up, not a market-rate assumption) produces a
    # positive value impact - the strongest real result in the whole build.
    {
        "name": "Bridgewood I", "property_id": 239910, "floor_name": 24,
        "vexcel_file": "property_29.937547_-95.387156.json", "query_lat": 29.937547, "query_lon": -95.387156,
        "real_floor_area_sqft": 34298.0, "real_column_depth_ft": None,
    },
    {
        "name": "Halbouty Center", "property_id": 239835, "floor_name": 24,
        "vexcel_file": "property_29.7410658_-95.4648038.json", "query_lat": 29.7410658, "query_lon": -95.4648038,
        "real_floor_area_sqft": 21819.0, "real_column_depth_ft": None,
    },
    {
        "name": "CityNorth 1", "property_id": 235518, "floor_name": 24,
        "vexcel_file": "property_29.9435561_-95.4058157.json", "query_lat": 29.9435561, "query_lon": -95.4058157,
        "real_floor_area_sqft": 21130.0, "real_column_depth_ft": None,
    },
    # The top 5 by confidence-weighted rank_score (real value impact x real
    # geometry_confidence x real value_confidence) across a 35-building blind
    # scan - the strongest results by actual data quality, not hand-picked.
    # TC Energy Center already above; these 4 are new.
    {
        "name": "Woodway Center", "property_id": 239944, "floor_name": 1,
        "vexcel_file": "property_29.7635027_-95.4602233.json", "query_lat": 29.7635027, "query_lon": -95.4602233,
        "real_floor_area_sqft": 55146.0, "real_column_depth_ft": None,
    },
    {
        "name": "7900 Fannin", "property_id": 6550930, "floor_name": 1,
        "vexcel_file": "property_29.6915081_-95.4034524.json", "query_lat": 29.6915081, "query_lon": -95.4034524,
        "real_floor_area_sqft": 43980.0, "real_column_depth_ft": None,
    },
    {
        "name": "Sage Park Office Building", "property_id": 240038, "floor_name": 1,
        "vexcel_file": "property_29.745295_-95.4665119.json", "query_lat": 29.745295, "query_lon": -95.4665119,
        "real_floor_area_sqft": 36796.0, "real_column_depth_ft": None,
    },
    {
        "name": "1113 Vine St", "property_id": 235123, "floor_name": 1,
        "vexcel_file": "property_29.768557_-95.357148.json", "query_lat": 29.768557, "query_lon": -95.357148,
        "real_floor_area_sqft": 33000.0, "real_column_depth_ft": None,
    },
]

if __name__ == "__main__":
    base = os.path.join(os.path.dirname(__file__), "..", "output")
    for b in BUILDINGS:
        path = os.path.join(base, b["vexcel_file"])
        if not os.path.exists(path):
            print(f"Skipping {b['name']} - {path} not found")
            continue
        with open(path) as f:
            vexcel_data = json.load(f)
        safe_name = b["name"].lower().replace(" ", "_")
        render(
            property_id=b["property_id"], floor_name=b["floor_name"], vexcel_response=vexcel_data,
            query_lat=b["query_lat"], query_lon=b["query_lon"],
            real_floor_area_sqft=b["real_floor_area_sqft"], real_column_depth_ft=b["real_column_depth_ft"],
            out_path=os.path.join(base, f"{safe_name}_render.png"),
        )
