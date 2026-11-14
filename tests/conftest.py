"""Shared pytest fixtures. Needs COSTAR_KIT_PATH set (see README) and the
real cached Vexcel responses already in output/ - no live network calls,
so this suite runs offline and deterministically."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("track_a_geometry", "track_b_value", "track_c_agent", "demo"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import pytest  # noqa: E402

from render_floor import BUILDINGS  # noqa: E402


@pytest.fixture(scope="session", params=BUILDINGS, ids=lambda b: b["name"])
def building(request):
    """Each of the 5 real featured buildings, with its real cached Vexcel
    response already loaded - parametrized so every test using this
    fixture runs once per building automatically."""
    b = request.param
    with open(os.path.join(ROOT, "output", b["vexcel_file"])) as f:
        vexcel_data = json.load(f)
    return {**b, "vexcel_data": vexcel_data}
