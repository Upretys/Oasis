"""Codifies the geometry audit that's been run manually, by hand, all
night: no unit overlaps, no unit rendered outside its own building's real
footprint, and the summed unit square footage matches the reported
viable_leasable_sqft exactly. These are the three real bugs that were
actually found this way (a unit rendered 100% outside its building, from
GEOS floating-point drift) - this suite is what should have caught that
automatically, the first time, instead of a human noticing it in a
screenshot."""
from shapely.geometry import Polygon

from geometry import run_feasibility, load_scaled_footprint, SIMPLIFY_TOLERANCE_M, METERS_TO_FEET


def _run(building):
    return run_feasibility(
        floor_id="test", vexcel_response=building["vexcel_data"],
        query_lat=building["query_lat"], query_lon=building["query_lon"],
        real_floor_area_sqft=building["real_floor_area_sqft"],
        real_column_depth_ft=building["real_column_depth_ft"],
    )


def test_no_unit_overlaps(building):
    geo = _run(building)
    units = geo.unit_layout_polygons
    for i in range(len(units)):
        pi = Polygon(units[i]["polygon_ft"])
        for j in range(i + 1, len(units)):
            pj = Polygon(units[j]["polygon_ft"])
            overlap_sqft = pi.intersection(pj).area if pi.intersects(pj) else 0.0
            assert overlap_sqft < 0.5, (
                f"{building['name']}: units {i} and {j} overlap by {overlap_sqft:.1f} sqft"
            )


def test_no_unit_outside_footprint(building):
    geo = _run(building)
    footprint = load_scaled_footprint(
        building["vexcel_data"], building["query_lat"], building["query_lon"], building["real_floor_area_sqft"]
    ).simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)

    for i, u in enumerate(geo.unit_layout_polygons):
        poly_m = Polygon([(x / METERS_TO_FEET, y / METERS_TO_FEET) for x, y in u["polygon_ft"]])
        outside_sqft = poly_m.difference(footprint).area * (METERS_TO_FEET ** 2)
        assert outside_sqft < 1.0, (
            f"{building['name']}: unit {i} is {outside_sqft:.1f} sqft outside the real footprint "
            f"(the exact bug class once found by hand on 5555 San Felipe - GEOS numerical drift "
            f"after repeated polygon .difference() calls)"
        )


def test_unit_sqft_sums_to_viable_leasable_sqft(building):
    geo = _run(building)
    unit_total = sum(u["area_sqft"] for u in geo.unit_layout_polygons)
    assert abs(unit_total - geo.viable_leasable_sqft) < 1.0, (
        f"{building['name']}: unit polygons sum to {unit_total:.1f} sqft but "
        f"viable_leasable_sqft says {geo.viable_leasable_sqft:.1f}"
    )


def test_viable_leasable_sqft_never_exceeds_real_floor_area(building):
    geo = _run(building)
    assert geo.viable_leasable_sqft <= building["real_floor_area_sqft"], (
        f"{building['name']}: viable_leasable_sqft ({geo.viable_leasable_sqft:.0f}) exceeds "
        f"the real total floor area ({building['real_floor_area_sqft']:.0f}) - impossible"
    )
