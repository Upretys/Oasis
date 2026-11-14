"""
Live Vexcel fetch for the interactive demo.

Same real /property/extract call as vexcel_quickstart.py, pulled out as a
function so the web demo can hit the real API live, in front of a judge,
for a building we haven't pre-cached - not just replay a saved JSON file.
Returns the exact same response shape load_scaled_footprint() already
expects, so nothing downstream needs to change.
"""

from __future__ import annotations

import os

import requests

API = "https://api.vexcelgroup.com/v2"


class VexcelKeyMissing(Exception):
    pass


class VexcelNoPropertyData(Exception):
    pass


def fetch_vexcel_property(lat: float, lon: float, timeout: int = 120) -> dict:
    """Real, live call - not cached, not replayed. Raises a clear error
    instead of silently falling back to anything fake."""
    key = "".join(os.environ.get("VEXCEL_API_KEY", "").split())
    if not key:
        raise VexcelKeyMissing(
            "VEXCEL_API_KEY is not set in the terminal running this server - "
            "live lookups for buildings outside the 7 pre-tested ones need it."
        )

    point = f"POINT({lon} {lat})"
    r = requests.get(
        f"{API}/property/extract",
        headers={"X-API-KEY": key},
        timeout=timeout,
        params={
            "wkt": point, "srid": 4326,
            "layer": "urban,urban-r,wide-area,urban-g",
            "latest": "true", "include": "precomputed", "unit-system": "imperial",
        },
    )
    if r.status_code == 401:
        raise VexcelKeyMissing("Vexcel rejected VEXCEL_API_KEY (401 Unauthorized).")
    if r.status_code == 404:
        raise VexcelNoPropertyData(f"No real Vexcel structure found near ({lat}, {lon}).")
    r.raise_for_status()
    return r.json()
