"""
Real livability data - schools, hospitals, parks, grocery stores near a
building - from OpenStreetMap's Overpass API (free, real, no key needed,
same source family as this project's map tiles).

The default public endpoint (overpass-api.de) was down (502 "at capacity")
for 45+ minutes straight tonight across every retry - a real outage on
their end. overpass.openstreetmap.fr is a working alternate public mirror,
confirmed directly (found "Fannin School" near downtown Houston before
trusting it). Falls back through a short list of mirrors so one outage
doesn't block the whole feature again.
"""

from __future__ import annotations

import requests

MIRRORS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Real, standard OSM tags - not invented categories.
QUERIES = {
    "schools": ('amenity', 'school', 1500),
    "hospitals": ('amenity', 'hospital', 3000),
    "parks": ('leisure', 'park', 1500),
    "grocery": ('shop', 'supermarket', 1500),
}


class LivabilityUnavailable(Exception):
    pass


def _query_mirror(mirror: str, lat: float, lon: float, timeout: int = 25) -> dict:
    clauses = "\n".join(
        f'  node["{k}"="{v}"](around:{radius},{lat},{lon});'
        for k, v, radius in QUERIES.values()
    )
    query = f"[out:json][timeout:{timeout - 5}];(\n{clauses}\n);out center;"
    r = requests.post(mirror, data={"data": query}, timeout=timeout,
                       headers={"User-Agent": "hackathon-office-conversion-demo/1.0"})
    r.raise_for_status()
    return r.json()


def real_nearby_amenities(lat: float, lon: float) -> dict:
    """Returns real counts and up to 3 real names per category. Raises
    LivabilityUnavailable if every mirror fails - never returns fake data."""
    last_error = None
    for mirror in MIRRORS:
        try:
            data = _query_mirror(mirror, lat, lon)
            break
        except Exception as e:
            last_error = e
            continue
    else:
        raise LivabilityUnavailable(f"All Overpass mirrors failed: {last_error}")

    by_category = {name: [] for name in QUERIES}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        for name, (key, value, _radius) in QUERIES.items():
            if tags.get(key) == value:
                by_category[name].append(tags.get("name", "(unnamed)"))

    return {
        name: {"count": len(names), "examples": names[:3]}
        for name, names in by_category.items()
    }


if __name__ == "__main__":
    import json
    # Woodway Center - one of the real top-5 buildings
    print(json.dumps(real_nearby_amenities(29.7635027, -95.4602233), indent=2))
