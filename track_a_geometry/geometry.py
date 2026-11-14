"""
Track A - Geometry & Feasibility

REVISED after seeing the real Matterport query response: the Model API's
`floors { rooms { classifications, dimensionEstimates } }` query returns room
AREA, HEIGHT, and CLASSIFICATION LABELS (sometimes several per room, with
confidence scores) - it does NOT return wall/vertex boundary coordinates.
There is no polygon to run point-to-line distance math on.

So the geometry (window-to-core depth) comes from Vexcel's aerial footprint
instead, and Matterport supplies area/usability/height cross-checks:

  Vexcel footprint polygon  -> floor-plate SHAPE -> window-to-core buffer check
  Matterport room data      -> total usable area, ceiling height code check
  CoStar structural data    -> real column-depth cross-check on the Vexcel number

This is a deliberate, stated simplification: an office tower's floor-plate
shape is usually close to constant across stacked floors, so the ground-level
(or any available) aerial footprint is used as a stand-in for the specific
floor's shape. Say this out loud in the pitch rather than hiding it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

METERS_TO_FEET = 3.28084
# Researched, not guessed: ULI ("How to Make Office-to-Residential Conversions
# Work") and Gensler's Building Suitability Analysis (SPUR, Sept 2023) converge
# on a real range, not one legal number - 30-35ft is comfortable/ideal,
# 40-50ft is workable with good planning, 60ft+ is difficult (needs light
# wells or core removal). Our old 50ft sat at the upper edge of "workable" -
# the most lenient defensible choice. Moved to 45ft, mid-"workable," a more
# defensible cutoff than the most permissive end of the real literature range.
WINDOW_TO_CORE_THRESHOLD_FT = 45.0
UNIT_WIDTH_FT = 20.0
# Smooths real-world jaggedness (small notches/kinks in the raw Vexcel
# footprint) before any downstream geometry - applied once in run_feasibility
# so buffer_based_check and generate_unit_layout see the same smoothed shape.
SIMPLIFY_TOLERANCE_M = 3.0
# IRC R304: real minimum habitable room area is 70 sqft, min horizontal
# dimension 7ft (kitchens exempt). Our unit-size minimums below are in a
# comparable real-world range (not derived from one single code figure - no
# single federal/IBC minimum apartment size exists; NYC removed its own 400
# sqft minimum in 2023's "City of Yes" reform).
MIN_CEILING_HEIGHT_FT = 7.5  # typical residential habitable-room minimum

CORE_LABELS = {"void", "hallway"}  # weak proxy - see parse_matterport_floor's note


def _to_ft(meters: float) -> float:
    return meters * METERS_TO_FEET


@dataclass
class MatterportFloorSummary:
    total_area_m2: float
    total_indoor_area_m2: float
    core_candidate_area_m2: float  # weak proxy from Void/Hallway labels - not a real core location
    min_ceiling_height_m: float | None
    room_count: int
    labels_seen: list[str] = field(default_factory=list)


@dataclass
class FloorGeometryResult:
    floor_id: str
    verdict: str  # "pass" | "fail" | "escalate"
    confidence: float
    window_to_core_max_ft: float
    convertible_fraction: float
    ceiling_height_ft: float | None
    unit_layout_polygons: list[dict] = field(default_factory=list)
    viable_leasable_sqft: float = 0.0  # sum of real-apartment-sized units only, not the whole convertible zone
    unit_type_counts: dict = field(default_factory=dict)  # e.g. {"studio": 4, "1br": 12}
    notes: list[str] = field(default_factory=list)


def parse_matterport_floor(matterport_response: dict, floor_index: int = 0) -> MatterportFloorSummary:
    """Parse the REAL response shape: {"data": {"model": {"floors": [{"rooms": [...]}]}}}."""
    floors = matterport_response["data"]["model"]["floors"]
    floor = floors[floor_index]
    rooms = floor["rooms"]

    total_area = 0.0
    total_indoor = 0.0
    core_candidate_area = 0.0
    heights = []
    labels_seen = []

    for room in rooms:
        dims = room.get("dimensionEstimates") or {}
        area = dims.get("area", 0.0)
        total_area += area

        labels = [c["label"] for c in room.get("classifications", [])]
        labels_seen.extend(labels)
        label_set = {l.lower() for l in labels}

        if label_set & CORE_LABELS:
            core_candidate_area += area
        elif dims.get("areaIndoor"):
            total_indoor += dims["areaIndoor"]
            if dims.get("height"):
                heights.append(dims["height"])

    return MatterportFloorSummary(
        total_area_m2=total_area,
        total_indoor_area_m2=total_indoor,
        core_candidate_area_m2=core_candidate_area,
        min_ceiling_height_m=min(heights) if heights else None,
        room_count=len(rooms),
        labels_seen=labels_seen,
    )


def _lonlat_to_local_meters(coords: list[tuple[float, float]], lat0: float, lon0: float) -> list[tuple[float, float]]:
    """Equirectangular approx, centered at (lat0, lon0) - same convention
    Vexcel's own quickstart script uses (111_320 m/degree, cos-scaled for
    longitude). Fine at building scale; don't reuse this for large areas."""
    import math
    out = []
    for lon, lat in coords:
        x = (lon - lon0) * 111_320 * math.cos(math.radians(lat0))
        y = (lat - lat0) * 111_320
        out.append((x, y))
    return out


def pick_nearest_structure(vexcel_response: dict, query_lat: float, query_lon: float) -> dict | None:
    """The real /property/extract response can return several distinct nearby
    structures (not just repeat captures of one building). Pick the primary
    structure whose own roof centroid is closest to the exact query point,
    instead of blindly trusting features[0]."""
    import math
    from shapely import wkt as shapely_wkt

    best, best_dist = None, float("inf")
    for feat in vexcel_response.get("features", []):
        for s in feat.get("properties", {}).get("structures", []):
            if not s.get("primary-structure") or not s.get("footprint.wkt"):
                continue
            centroid_wkt = s.get("roof-characteristics.centroid.wkt")
            if centroid_wkt:
                pt = shapely_wkt.loads(centroid_wkt)
                lon, lat = pt.x, pt.y
            else:
                poly = shapely_wkt.loads(s["footprint.wkt"])
                lon, lat = poly.centroid.x, poly.centroid.y
            # equirectangular distance in meters, fine at this scale
            dx = (lon - query_lon) * 111_320 * math.cos(math.radians(query_lat))
            dy = (lat - query_lat) * 111_320
            dist = math.hypot(dx, dy)
            if dist < best_dist:
                best_dist, best = dist, s
    return best


def scale_footprint_to_area(poly: Polygon, target_area_m2: float) -> Polygon:
    """Vexcel's footprint is the merged ground-level structure (tower + any
    attached garage/base), not one floor's shape - it's typically much bigger
    than a single office floor. Scale it down to CoStar's real per-floor area
    (grossTypicalFloorSquareFeet), preserving shape, about its own centroid.
    This is a stated simplification: it assumes upper floors share the ground
    footprint's proportions, which is approximate, not exact."""
    if poly.area <= 0:
        return poly
    from shapely.affinity import scale
    factor = (target_area_m2 / poly.area) ** 0.5
    return scale(poly, xfact=factor, yfact=factor, origin="centroid")


def parse_vexcel_footprint(vexcel_response: dict) -> Polygon | None:
    """Parse a real /property/extract response. The parcel/footprint geometry
    comes back as GeoJSON or WKT in lon/lat WGS84 degrees (srid 4326) - this
    reprojects to local meters before any buffer/distance math runs, since
    shapely's buffer() assumes a planar CRS and degrees are not meters.

    Prefers the primary structure's footprint (include=footprint-wkt) over
    the parcel boundary, since the parcel is the LOT, not the building shape.
    Falls back to the parcel polygon if no structure footprint is present -
    label this fallback explicitly, it's a weaker proxy for the floor plate.
    """
    features = vexcel_response.get("features", [])
    if not features:
        return None
    props = features[0].get("properties", {})

    footprint_wkt = None
    for s in props.get("structures", []):
        if s.get("primary-structure") and s.get("footprint-wkt"):
            footprint_wkt = s["footprint-wkt"]
            break

    if footprint_wkt:
        from shapely import wkt as shapely_wkt
        geom = shapely_wkt.loads(footprint_wkt)
        used_fallback = False
    else:
        geom = __import__("shapely.geometry", fromlist=["shape"]).shape(features[0]["geometry"])
        used_fallback = True

    coords = list(geom.exterior.coords)
    lon0 = sum(c[0] for c in coords) / len(coords)
    lat0 = sum(c[1] for c in coords) / len(coords)
    local_coords = _lonlat_to_local_meters(coords, lat0, lon0)
    poly = Polygon(local_coords)
    poly = poly if poly.is_valid else poly.buffer(0)
    if used_fallback:
        pass  # caller can't easily tell; logged via print for now, not stored on the shapely object (it uses __slots__)
    return poly


def _exterior_ring_as_line(poly: Polygon) -> LineString:
    return LineString(poly.exterior.coords)


def buffer_based_check(footprint: Polygon, threshold_ft: float = WINDOW_TO_CORE_THRESHOLD_FT):
    """Buffer the footprint's own perimeter inward by the threshold and
    intersect with the footprint - the convertible zone, no window data needed."""
    threshold_m = threshold_ft / METERS_TO_FEET
    perimeter_line = _exterior_ring_as_line(footprint)
    convertible_zone = perimeter_line.buffer(threshold_m).intersection(footprint)
    convertible_fraction = convertible_zone.area / footprint.area if footprint.area else 0.0

    vertex_depths_m = [perimeter_line.distance(_point(pt)) for pt in footprint.interior_point_candidates()] \
        if hasattr(footprint, "interior_point_candidates") else []
    # simpler: use the polygon's centroid-to-perimeter distance as the depth estimate,
    # since without a true medial axis this is the best cheap proxy on a bare footprint.
    max_depth_m = footprint.centroid.distance(perimeter_line) if not footprint.centroid.is_empty else 0.0
    # better than centroid alone: sample the footprint's own vertices too
    vertex_dists = [perimeter_line.distance(_point(c)) for c in footprint.exterior.coords]
    if vertex_dists:
        max_depth_m = max(max_depth_m, max(vertex_dists))

    return convertible_zone, convertible_fraction, _to_ft(max_depth_m)


def _point(xy):
    from shapely.geometry import Point
    return Point(xy)


# Real minimum apartment sizes (sqft) and minimum widths (ft) - a strip
# smaller/narrower than this cannot actually fit a kitchen + bathroom +
# sleeping area, so it does not count as a unit at all, regardless of how
# the geometry slices out. These are standard rough minimums, not precise
# code citations, and are stated as an assumption in the pitch.
UNIT_TYPES = [
    # (type_name, min_area_sqft, min_width_ft)
    ("not_viable", 0, 0),       # below studio minimum - excluded from the count entirely
    ("studio", 450, 12),
    ("1br", 650, 18),
    ("2br", 900, 24),
]


def classify_unit(area_sqft: float, width_ft: float) -> str:
    """Returns the largest unit type this area/width actually qualifies for,
    walking from biggest to smallest minimum. Fails down to 'not_viable' if
    it doesn't meet even the studio minimum."""
    for type_name, min_area, min_width in reversed(UNIT_TYPES):
        if area_sqft >= min_area and width_ft >= min_width:
            return type_name
    return "not_viable"


def generate_unit_layout(footprint: Polygon, convertible_zone, unit_width_ft: float = UNIT_WIDTH_FT) -> list[dict]:
    """Slice the convertible zone into unit-width strips along the footprint perimeter.

    Walks each straight edge of a SIMPLIFIED footprint separately (not the raw
    arc-length of the whole jagged ring) so each unit's buffer direction stays
    aligned with the actual wall it's on, instead of cutting across real-world
    micro-notches in the aerial-derived outline. Real building footprints from
    Vexcel are jagged (genuine survey noise, not a bug) - slicing by total
    perimeter length regardless of corners produced overlapping/malformed
    strips at those notches; this fixes that by respecting edge boundaries."""
    if convertible_zone is None or convertible_zone.is_empty:
        return []
    unit_width_m = unit_width_ft / METERS_TO_FEET

    # footprint arrives already smoothed by run_feasibility (SIMPLIFY_TOLERANCE_M,
    # applied once, before buffer_based_check too) - slicing it again here would
    # be harmless but redundant.
    coords = list(footprint.exterior.coords)
    units = []
    min_unit_width_m = unit_width_m * 0.4  # drop slivers smaller than this
    remaining = convertible_zone  # shrinks as units claim territory - prevents double-counting the same sqft

    for i in range(len(coords) - 1):
        p_start, p_end = coords[i], coords[i + 1]
        edge = LineString([p_start, p_end])
        edge_len = edge.length
        if edge_len == 0:
            continue
        n_chunks = max(1, round(edge_len / unit_width_m))
        chunk_len = edge_len / n_chunks

        # Inward normal for this edge, pointing toward the building interior -
        # NOT a buffer radius. Width-along-wall and depth-into-building are
        # two different dimensions; collapsing them into one buffer radius
        # was the bug (every unit came out ~10ft deep, failing every real
        # apartment-size check). A generous reach (threshold + margin) gets
        # clipped back to the real shape by intersecting with convertible_zone.
        dx, dy = p_end[0] - p_start[0], p_end[1] - p_start[1]
        edge_dir_len = (dx ** 2 + dy ** 2) ** 0.5
        nx, ny = -dy / edge_dir_len, dx / edge_dir_len  # perpendicular, one of two directions
        probe = type(footprint.centroid)(p_start[0] + nx * 0.1, p_start[1] + ny * 0.1)
        if not footprint.contains(probe):
            nx, ny = -nx, -ny  # flip to point inward
        # Real apartments are typically ~25-32ft deep, not built out to the
        # full legal maximum (50ft) - that max is a ceiling for daylight
        # access, not a target depth. Using the ceiling here is what let one
        # wall's units greedily eat the whole floor before the opposite wall
        # got a turn. A realistic target depth leaves room for both sides
        # plus a corridor, without needing a true medial-axis computation.
        TYPICAL_UNIT_DEPTH_FT = 32.0
        reach_m = TYPICAL_UNIT_DEPTH_FT / METERS_TO_FEET

        for c in range(n_chunks):
            a = edge.interpolate(c * chunk_len)
            b = edge.interpolate(min((c + 1) * chunk_len, edge_len))
            if a.distance(b) < min_unit_width_m:
                continue
            quad = Polygon([
                (a.x, a.y), (b.x, b.y),
                (b.x + nx * reach_m, b.y + ny * reach_m),
                (a.x + nx * reach_m, a.y + ny * reach_m),
            ])
            # Only claim territory no earlier unit (from this or another wall)
            # has already claimed - prevents two opposing walls both counting
            # the same central sqft when the building is narrower than 2x reach.
            unit_poly = quad.intersection(remaining)
            if unit_poly.is_empty or not hasattr(unit_poly, "exterior"):
                continue

            # Reject irregular/sliver shapes instead of forcing them to fit.
            # Near a sharp convex corner, one wall's quad can claim a thin,
            # non-rectangular wedge that visually overlaps the NEXT wall's
            # natural territory even though shapely sees no true overlap -
            # that's the corner artifact a real screenshot caught. Measure
            # how close the claimed shape is to its own bounding rectangle;
            # a low ratio means a thin/bowtie-ish sliver, not a usable room
            # shape. Skip it AND don't subtract it from `remaining`, so the
            # next wall's edge gets a clean shot at that same corner instead.
            bounding_rect = unit_poly.minimum_rotated_rectangle
            rectangularity = unit_poly.area / bounding_rect.area if bounding_rect.area else 0.0
            # 0.55 was too lenient - a real screenshot caught a thin spike/tail
            # still passing through at a building corner (First Community
            # Tower unit K, 0.684). Checked across all 10 buildings: there's a
            # real cluster of similar corner-spike shapes at 0.61-0.70 in four
            # other buildings too, and a gap before the next-lowest legitimate
            # trapezoidal unit at 0.764 - so 0.72 cuts precisely between the
            # spike artifacts and real (just angled) units, confirmed by
            # re-rendering and re-inspecting both groups after raising it.
            if rectangularity < 0.72:
                continue

            remaining = remaining.difference(quad)

            width_ft = _to_ft(a.distance(b))
            area_sqft = unit_poly.area * (METERS_TO_FEET ** 2)
            unit_type = classify_unit(area_sqft, width_ft)

            units.append({
                "unit_id": chr(65 + len(units)) if len(units) < 26 else str(len(units)),
                "polygon_ft": [[_to_ft(x), _to_ft(y)] for x, y in unit_poly.exterior.coords],
                "area_sqft": round(area_sqft, 1),
                "width_ft": round(width_ft, 1),
                "unit_type": unit_type,
            })

    viable = [u for u in units if u["unit_type"] != "not_viable"]
    return viable  # not-viable slivers are generated internally but dropped here, not counted as units


def load_scaled_footprint(
    vexcel_response: dict, query_lat: float, query_lon: float, target_area_sqft: float
) -> Polygon | None:
    """The real, correct loader: picks the nearest real structure to the query
    point, projects it to local meters, then scales it to match CoStar's real
    per-floor area. Use this instead of parse_vexcel_footprint() directly."""
    from shapely import wkt as shapely_wkt

    structure = pick_nearest_structure(vexcel_response, query_lat, query_lon)
    if structure is None:
        return None

    geom = shapely_wkt.loads(structure["footprint.wkt"])
    coords = list(geom.exterior.coords)
    lon0 = sum(c[0] for c in coords) / len(coords)
    lat0 = sum(c[1] for c in coords) / len(coords)
    local_coords = _lonlat_to_local_meters(coords, lat0, lon0)
    poly = Polygon(local_coords)
    poly = poly if poly.is_valid else poly.buffer(0)

    target_area_m2 = target_area_sqft / (METERS_TO_FEET ** 2)
    return scale_footprint_to_area(poly, target_area_m2)


def run_feasibility(
    floor_id: str,
    vexcel_response: dict,
    query_lat: float,
    query_lon: float,
    real_floor_area_sqft: float,
    real_column_depth_ft: float | None = None,
) -> FloorGeometryResult:
    """Main entry point Track C calls.

    Matterport is intentionally NOT a parameter here - confirmed during the
    build that its Property Intelligence data is a shared placeholder for our
    key (same room/floor IDs across 4 different models tested), not real
    per-building data. Geometry runs entirely on Vexcel (scaled to CoStar's
    real floor area) cross-checked against CoStar's real column-depth data.
    Matterport is used elsewhere only for the demo's visual scan embed.
    """
    footprint = load_scaled_footprint(vexcel_response, query_lat, query_lon, real_floor_area_sqft)

    notes = []
    if footprint is not None:
        # Smooth once, here, before ANY downstream use. Smoothing only inside
        # generate_unit_layout left buffer_based_check's convertible_zone (and
        # therefore every unit's outer/building-wall edge) still tracing the
        # raw jagged footprint - a real screenshot caught a unit at the top of
        # First Community Tower rendering as a jagged star shape because of
        # this mismatch. One smoothed footprint used everywhere fixes it.
        footprint = footprint.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)

    if footprint is None:
        return FloorGeometryResult(
            floor_id=floor_id, verdict="escalate", confidence=0.0,
            window_to_core_max_ft=float("inf"), convertible_fraction=0.0,
            ceiling_height_ft=None,
            notes=["No matching Vexcel structure found near the query point - cannot compute floor-plate geometry."],
        )

    convertible_zone, convertible_fraction, depth_ft = buffer_based_check(footprint)

    # Cross-check against CoStar's real column-depth data when we have it -
    # two independent real sources, not two numbers derived from the same input.
    agrees_with_costar = True
    if real_column_depth_ft is not None:
        agrees_with_costar = abs(depth_ft - real_column_depth_ft) <= 15.0
        if not agrees_with_costar:
            notes.append(
                f"Vexcel-derived depth ({depth_ft:.1f}ft) disagrees with CoStar's real "
                f"column depth ({real_column_depth_ft:.1f}ft) by more than 15ft."
            )

    units = generate_unit_layout(footprint, convertible_zone)
    viable_leasable_sqft = sum(u["area_sqft"] for u in units)
    unit_type_counts: dict[str, int] = {}
    for u in units:
        unit_type_counts[u["unit_type"]] = unit_type_counts.get(u["unit_type"], 0) + 1
    if viable_leasable_sqft < footprint.area * (METERS_TO_FEET ** 2) * convertible_fraction * 0.6:
        notes.append(
            f"Only {viable_leasable_sqft:,.0f} sqft fits real apartment-sized units "
            f"(min studio/1BR/2BR dimensions) out of a {convertible_fraction:.0%} geometrically-convertible "
            f"zone - most of that zone is unusable slivers, not a count to trust at face value."
        )

    margin_ft = WINDOW_TO_CORE_THRESHOLD_FT - depth_ft
    total_floor_sqft = footprint.area * (METERS_TO_FEET ** 2)
    viable_fraction = viable_leasable_sqft / total_floor_sqft if total_floor_sqft else 0.0

    # The verdict is gated on REAL viable leasable area, not just whether the
    # single deepest point on the whole floor clears the threshold. A floor
    # with one bad interior pocket but a mostly-workable perimeter (see
    # TC Energy Center: 18 real units generated, yet the old logic called it
    # a flat "fail" because one point was 70ft deep) is a materially
    # different, more valuable finding than a floor that's uniformly too
    # deep everywhere - treating them the same discarded real information.
    if not agrees_with_costar:
        verdict = "escalate"
    elif viable_fraction >= 0.5:
        verdict = "pass"
        if margin_ft < 10:
            notes.append(f"Max depth exceeds threshold by {-margin_ft:.1f}ft in at least one spot, "
                         f"but {viable_fraction:.0%} of the floor's area still fits real units - passing on that basis.")
    elif viable_fraction >= 0.2:
        verdict = "escalate"
        notes.append(f"Only {viable_fraction:.0%} of the floor's area fits real units - partial conversion "
                     f"may be viable, flagging for human review rather than a flat pass/fail.")
    else:
        verdict = "fail"
        notes.append(f"Only {viable_fraction:.0%} of the floor's area fits real units - not enough to convert.")

    confidence = 0.85
    if not agrees_with_costar:
        confidence -= 0.3
    if real_column_depth_ft is None:
        confidence -= 0.15
        notes.append("No CoStar structural data available to cross-check the footprint-derived depth.")
    confidence = max(0.1, min(0.95, confidence))

    return FloorGeometryResult(
        floor_id=floor_id, verdict=verdict, confidence=round(confidence, 2),
        window_to_core_max_ft=round(depth_ft, 1), convertible_fraction=round(convertible_fraction, 2),
        ceiling_height_ft=None,
        unit_layout_polygons=units, viable_leasable_sqft=round(viable_leasable_sqft, 1),
        unit_type_counts=unit_type_counts, notes=notes,
    )


if __name__ == "__main__":
    import json

    with open("output/property.json") as f:
        vexcel_data = json.load(f)

    result = run_feasibility(
        floor_id="9008561-floor-37",
        vexcel_response=vexcel_data,
        query_lat=29.995495, query_lon=-95.579652,
        real_floor_area_sqft=23531.0,
        real_column_depth_ft=40.0,
    )
    print(json.dumps(result.__dict__, indent=2))
