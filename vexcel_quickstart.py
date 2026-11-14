"""
Vexcel API 2.0 - Hackathon Quickstart
-------------------------------------
From a single latitude/longitude:
  1. Get Elements property attributes (parcel + structures)
  2. Get ortho + oblique imagery clipped to the parcel polygon
  3. Detect custom elements (any plain-language label) on the property:
     bounding boxes AND object masks, drawn on the ortho and an oblique
  4. Run an embeddings (semantic) search over a large area around it

Setup:
    pip install requests shapely pillow

    The script reads your key from an environment variable named VEXCEL_API_KEY.
    IMPORTANT: an environment variable only exists in the terminal where you set
    it. Set it and run the script in the SAME terminal window:

        macOS / Linux:       export VEXCEL_API_KEY="your-long-life-key"
        Windows PowerShell:  $env:VEXCEL_API_KEY = "your-long-life-key"
        Windows cmd:         set VEXCEL_API_KEY=your-long-life-key
        then:                python vexcel_quickstart.py

    A different terminal tab or window will NOT see it. That includes VS Code's
    Run / Debug buttons, which can start their own terminal. If you set the
    variable in one terminal and the script says it is missing, run the script
    from that same terminal instead.

    To set it permanently (every new terminal sees it):
        Windows:             setx VEXCEL_API_KEY "your-long-life-key"
                             (then close and reopen the terminal, or restart VS Code)
        macOS / Linux:       add the export line to ~/.zshrc or ~/.bashrc,
                             then open a new terminal
"""
import base64
import io
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import requests
from PIL import Image                      # decodes the mask images
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union
from shapely import wkt as shapely_wkt

API = "https://api.vexcelgroup.com/v2"
KEY = os.environ.get("VEXCEL_API_KEY", "")
KEY = "".join(KEY.split())  # strip any stray newline/space a terminal paste introduced
if not KEY:
    sys.exit("VEXCEL_API_KEY is not set in this terminal.\n"
             "Set it in the SAME terminal you run this script from (see the Setup notes\n"
             "at the top of this file), or set it permanently, then run the script again.")
HEADERS = {"X-API-KEY": KEY}

# ====================== CHANGE THESE (one place) ======================
# Houston Technology Center, Houston - CoStar PropertyId 240117. 4 stories,
# real per-floor sizes 116,686-155,541 sqft (massive, squat floor plates).
# Strongest real candidate yet for an actual geometric FAIL.
# (Tested so far: First Community Tower 9008561, Williams Tower 233903,
#  TC Energy Center 233905, 1415 Louisiana 233928, 5555 San Felipe 233930,
#  Belchase Bldg 240054)
LAT, LON = 29.7132915, -95.5505272
SEARCH_RADIUS_M = 3000
NO_PARCEL_HALF_M = 40
# =====================================================================

POINT = f"POINT({LON} {LAT})"
LAYERS = "urban,urban-r,wide-area,urban-g"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_RETRIES = 5


def send(method, path, **kwargs):
    for attempt in range(MAX_RETRIES + 1):
        r = requests.request(method, f"{API}/{path}", headers=HEADERS, **kwargs)
        if r.status_code != 429 or attempt == MAX_RETRIES:
            return r
        wait = min(2 ** attempt, 30) + random.random()
        print(f"  429 rate limited - retrying in {wait:.1f}s "
              f"(retry {attempt + 1}/{MAX_RETRIES})")
        time.sleep(wait)


def get(path, **params):
    r = send("GET", path, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def post(path, body, expect_json=True):
    r = send("POST", path, json=body, timeout=300)
    if not r.ok:
        raise RuntimeError(f"/{path} failed ({r.status_code}): {r.text[:500]}")
    return r.json() if expect_json else r.content


def to_wkt(geometry):
    if isinstance(geometry, str):
        return shapely_wkt.loads(geometry).wkt
    return shape(geometry).wkt


def square_around(lat, lon, half_size_m):
    dlat = half_size_m / 111_320
    dlon = half_size_m / (111_320 * math.cos(math.radians(lat)))
    w, e, s, n = lon - dlon, lon + dlon, lat - dlat, lat + dlat
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def save(name, data):
    with open(OUTPUT_DIR / name, "wb") as f:
        f.write(data)
    print(f"  saved {name} ({len(data)//1024} KB)")


print(f"Output files will be saved in: {OUTPUT_DIR}")

print("1. Elements property attributes")
r = send("GET", "property/extract", timeout=120, params={
    "wkt": POINT, "srid": 4326,
    "layer": LAYERS,
    "latest": "true",
    "include": "precomputed",
    "unit-system": "imperial"})
if r.status_code == 401:
    sys.exit("401 Unauthorized: the API rejected your key. Check VEXCEL_API_KEY.")

if r.status_code == 404:
    print("  No property data at this point (HTTP 404).")
    prop, attrs = None, {}
    parcel_wkt = square_around(LAT, LON, NO_PARCEL_HALF_M)
    area_name = f"{2 * NO_PARCEL_HALF_M} m square around your point (NO PARCEL)"
else:
    r.raise_for_status()
    prop = r.json()
    parcel = prop["features"][0]
    attrs = parcel["properties"]
    parcel_wkt = to_wkt(parcel["geometry"])
    area_name = "parcel"

    print("  collection      :", attrs.get("metadata.collection"))
    print("  structures      :", attrs.get("structure.count"))
    print("  parcel tree %   :", attrs.get("parcel-tree-cover.percent"))
    print("  pool detected   :", attrs.get("pool.detected"))
    for s in attrs.get("structures", []):
        if s.get("primary-structure"):
            print("  primary roof    :", s.get("roof-characteristics.material.class"),
                  s.get("roof-characteristics.shape.value"),
                  "| condition", s.get("roof-characteristics.condition"))
            print("  footprint sq ft :", round(s.get("footprint.area", 0)))

    out_name = f"property_{LAT}_{LON}.json"  # unique per location - never overwrites a previous real building's saved response
    with open(OUTPUT_DIR / out_name, "w") as f:
        json.dump(prop, f, indent=2)
    print(f"  saved {out_name} (the full response, open it to browse every attribute)")

print(f"Done with Step 1. Output in: {OUTPUT_DIR}")
