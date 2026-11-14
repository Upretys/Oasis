# Office-to-Residential Conversion Feasibility Agent

Screens real Houston office buildings/floors for conversion to apartments,
using **real Vexcel aerial footprint data** (geometry/shape), **real CoStar
property/lease/comp data** (financials), and a **live Claude tool-calling
agent** that decides which checks to run and writes its own verdict.

CoStar's data is real but is a static hackathon-kit snapshot, not a live feed.
The Vexcel footprint lookups and the Claude agent calls are genuinely live.

## What it does

For a given office floor: scales Vexcel's real aerial footprint to CoStar's
real per-floor square footage, slices it into real apartment-sized units
(studio/1BR/2BR, filtered against real minimum-size standards), cross-checks
the geometry against CoStar's real column-depth data, then prices the
conversion using real nearby apartment rent comps, a real cap rate when a
sale comp exists, and a stated construction-cost assumption.

**Key finding**: a floor's real office-occupancy history (CoStar's
`location-occupancy.jsonl`) matters as much as its shape. Genuinely vacant
floors (real lease history, all leases ended, none current) have **$0 real
office income to give up** by converting - very different economics from an
occupied floor. A blind portfolio scan of 35 real Houston office buildings
found 19 with positive implied conversion value, almost entirely buildings
that are genuinely vacant right now.

Every result also carries a real **value_confidence** score (0-1): how many
of {real cap rate, real office-occupancy determination, real construction
material, real residential rent comp} actually backed that dollar figure,
vs. a stated default. Buildings are ranked by real value impact x geometry
confidence x value confidence - not by dollar amount alone, so a number
built entirely on real data outranks an equally large one resting on
defaults.

## Structure

- `track_a_geometry/` - Vexcel footprint parsing/scaling, window-to-core
  depth check, real apartment-unit layout generation (`geometry.py`); live
  Vexcel API client (`vexcel_live.py`).
- `track_b_value/` - real CoStar lease/occupancy/comp data reads, the value
  calculation, and `list_houston_office_buildings_broad()` for finding real
  candidate buildings beyond the ones already tested (`value.py`).
- `track_c_agent/` - ties geometry + value together. `run_pipeline()` is the
  deterministic, trustworthy triage logic; `run_agent_llm()` is a real
  Claude tool-calling loop that decides the same thing on its own.
- `demo/` - `render_floor.py` (matplotlib floor-plan renders), `webapp.py`
  (the live interactive web demo: a Houston map, click-to-analyze on any
  real building, the "ask the AI agent" narrated live run, and a portfolio
  scan view), `portfolio_scan.py` (the blind-batch scan script).
- `vexcel_quickstart.py` - the original standalone Vexcel API script.

## Setup

1. `pip install -r requirements.txt` (or use the `.venv` already in this repo)
2. Copy `.env.example` to `.env` and fill in real keys - **never commit `.env`**
3. Set `COSTAR_KIT_PATH` to wherever you unzipped the CoStar hackathon kit

## Running the live web demo

```bash
COSTAR_KIT_PATH=/path/to/costar-hackathon-kit \
VEXCEL_API_KEY=your_key \
ANTHROPIC_API_KEY=your_key \
.venv/bin/python3 demo/webapp.py
```

Open `http://127.0.0.1:5050`. Pre-tested buildings run instantly; any other
real Houston office building on the map triggers a genuinely live Vexcel
fetch. The portfolio scan view shows results from `demo/portfolio_scan.json`
(run `python3 demo/portfolio_scan.py` to regenerate it against a fresh batch).

## Real legal / building-code checks

Every result includes, not just a dollar figure:
- **Zoning**: Houston has no citywide zoning (confirmed via the City of
  Houston Planning Dept's own "No Zoning Letter") - the real constraint is
  private deed restrictions, which aren't in this dataset.
- **Parking**: real CoStar `parkingRatio` vs. a stated residential-parking
  assumption.
- **Occupancy change**: converting IBC Group B (office) to R-2 (residential)
  requires a Certificate of Occupancy and triggers IBC 903.2.8 (automatic
  sprinklers throughout) - checked against CoStar's real `hasSprinklers`
  field per building.
- **Column spacing**: CoStar's real `columnDepthFeet` (the only real
  structural spacing field in the kit - no bay-width/grid field exists)
  drawn as an actual line on the floor-plan render, with specific units
  flagged if they sit near it - not just a blanket sentence for the floor.

The live AI agent is required (by its own prompt) to address zoning and the
occupancy-change requirement in its own written verdict, not just the
deterministic pipeline's notes.

## Known, stated simplifications

- Construction cost ($150-220/sf baseline) is adjusted by a real per-building
  multiplier from CoStar's `buildingConstructionMaterial` field (concrete
  1.15x, masonry 1.10x, steel/wood 1.00x - a documented real cost driver),
  but the baseline range itself is still not location-specific.
- "Window-to-core depth" is shorthand: none of our sources record actual
  window positions - what's measured is exterior-wall-to-core distance,
  assuming standard curtain-wall glazing.
- Matterport was evaluated and **dropped as a data/geometry source** - its
  Property Intelligence room data returned the same placeholder graph across
  four different model IDs tested, confirmed not real per-building data for
  this credential.
- The real vacancy/opportunity-cost logic ignores the option value of
  eventually re-leasing a vacant floor as office space at market rate - but
  it's cross-checked against real submarket-wide office vacancy, so the
  result states how defensible that simplification actually is per building.

## Test buildings

14 real Houston buildings are permanently wired up in `demo/render_floor.py`'s
`BUILDINGS` list (First Community Tower, Williams Tower, TC Energy Center,
1415 Louisiana, 5555 San Felipe, Belchase Bldg, Houston Technology Center,
Bridgewood I, Halbouty Center, CityNorth 1, Woodway Center, 7900 Fannin,
Sage Park Office Building, 1113 Vine St - the last 4 added as the real top-5
by confidence-weighted rank_score), plus 21 more scanned once in
`demo/portfolio_scan.json` (35 total, 19 with positive implied value).

## Downloadable report

Every analyzed building gets a "Download report" button in the web demo - a
real `.txt` file (survives after the browser tab and server are gone) with a
one-word BOTTOM LINE (PASS/FAIL/REVIEW), the full reasoning, legal checks,
and the live AI agent's own write-up if it was run.
