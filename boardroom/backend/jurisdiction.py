"""Place a building: three point-in-polygon joins, then a rules lookup. No rule is ever invented."""
import json
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape

from backend.contracts import Building, Jurisdiction

FIXTURES = Path(__file__).parent / "fixtures"
RULE_FIELDS = ["conversion_pathway", "approval_months", "max_far", "max_height_ft",
               "set_aside_pct", "source_url", "as_of"]


def load_building(building_id: str) -> Building:
    return Building.model_validate_json((FIXTURES / f"{building_id}.json").read_text(encoding="utf-8"))


@lru_cache
def _boundaries() -> list[tuple[str, dict, object]]:
    fc = json.loads((FIXTURES / "boundaries.geojson").read_text(encoding="utf-8"))
    return [(f["properties"]["layer"], f["properties"], shape(f["geometry"])) for f in fc["features"]]


@lru_cache
def _rules() -> dict[tuple[str, str], dict]:
    data = json.loads((FIXTURES / "jurisdiction_rules.json").read_text(encoding="utf-8"))
    return {(r["jurisdiction_id"], r["zoning_district"]): r for r in data["rules"]}


def _join(point: Point, layer: str) -> dict | None:
    for lyr, props, poly in _boundaries():
        if lyr == layer and poly.contains(point):
            return props
    return None


def place(building: Building) -> Jurisdiction:
    pt = Point(building.lon, building.lat)
    muni, council, zone = (_join(pt, layer) for layer in ("municipality", "council", "zoning"))
    if muni is None or zone is None:
        raise ValueError(f"{building.building_id}: no municipality or zoning polygon contains the site")
    rule = _rules().get((muni["id"], zone["district"]))
    if rule is None:
        raise KeyError(f"no published rule for {muni['id']} / {zone['district']}")
    return Jurisdiction(
        jurisdiction_id=muni["id"],
        name=muni["name"],
        council_district=council["id"] if council else None,
        zoning_district=zone["district"],
        incentives=rule.get("incentives", []),
        **{k: rule.get(k) for k in RULE_FIELDS},   # unknown fields stay None
    )
