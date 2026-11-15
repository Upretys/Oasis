# Office-to-Residential Conversion Feasibility Agent

Screens real Houston office buildings/floors for conversion to apartments,
using **real Vexcel aerial footprint data** (geometry/shape), **real CoStar
property/lease/comp data** (financials), and a rule-based feasibility pipeline. Claude drafts contractor emails.

CoStar's data is real but is a static hackathon-kit snapshot, not a live feed.
Oasis can fetch Vexcel footprints and generate email drafts live.

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
found 21 with positive implied conversion value, almost entirely buildings
that are genuinely vacant right now.

Every result also carries a real **value_confidence** score (0-1): how many
of {real cap rate, real office-occupancy determination, real construction
material, real residential rent comp} actually backed that dollar figure,
vs. a stated default. Buildings are ranked by real value impact x geometry
confidence x value confidence - not by dollar amount alone, so a number
built entirely on real data outranks an equally large one resting on
defaults. (This wasn't always true: the ranking code originally computed
both confidence scores and then silently sorted by dollar amount alone,
contradicting this exact claim - fixed once found.)

## Automated anomaly detection

`track_b_value/anomaly_detection.py` is a real, trained unsupervised model,
not a hand-picked threshold: it computes robust statistics (median +/- 3*IQR)
from the full real CoStar population - 3,217 real cap rates and 18,680 real
apartment rent data points citywide, not just the featured buildings - and
flags any computed cap rate or residential rent that falls outside the real
population's normal range. It formalizes the exact manual process used to
catch several of the bugs below (a value "looked wrong," so the raw data got
checked) into an automatic check that runs on every result.

## Cap rate sensitivity analysis

Every priced result also re-runs the same real value math at 9 other real
cap rate points (5%-9%) and states the real breakeven cap rate range - the
rate at which implied value crosses $0, using the real construction-cost
range's own low/high ends. The cap rate is the most commonly-defaulted real
input (no real sale comp exists for most buildings), so this shows how much
a verdict actually depends on that one assumption instead of presenting a
single, fragile number. Shown in the result's collapsed technical detail and
in the downloadable report, not the main card.

## Data & Methodology view

A dedicated page in the web demo (button next to Portfolio Scan), backed by
`/api/data_methodology`, computed live from the actual kit files - nothing
hardcoded: real record counts across the 4 source datasets (85,000+ total
real CoStar records), the anomaly detector's real population stats, and a
specific, honest list of real bugs found by checking raw data against this
project's own assumptions, each with its real root cause and fix.

## Structure

- `track_a_geometry/` - Vexcel footprint parsing/scaling, window-to-core
  depth check, real apartment-unit layout generation (`geometry.py`); live
  Vexcel API client (`vexcel_live.py`).
- `track_b_value/` - real CoStar lease/occupancy/comp data reads, the value
  calculation, and `list_houston_office_buildings_broad()` for finding real
  candidate buildings beyond the ones already tested (`value.py`).
- `track_c_agent/` - ties geometry + value together. `run_pipeline()` is the
  deterministic triage logic.
- `demo/` - the Oasis web app. `webapp.py` (Flask routes), `oasis.py`
  (checks, floors, floor plans, nearby places, boardroom agents, quote
  emails), `static/` (the single-page front end), `render_floor.py`
  (matplotlib floor-plan renders), `portfolio_scan.py` (the blind-batch scan
  script), `quote_mail.py` (Gmail and the demo contractor list), `places_houston.json` (OpenStreetMap cache).
- `vexcel_quickstart.py` - the original standalone Vexcel API script.

## Setup

1. `pip install -r requirements.txt` (or use the `.venv` already in this repo)
2. Copy `.env.example` to `.env` and fill in real keys - **never commit `.env`**
3. Set `COSTAR_KIT_PATH` to wherever you unzipped the CoStar hackathon kit

## Tests

```bash
COSTAR_KIT_PATH=/path/to/costar-hackathon-kit .venv/bin/python3 -m pytest tests/ -v
```

52 tests, all offline (no live network calls - runs against the real
cached Vexcel responses in `output/`), parametrized across the 5 real
featured buildings: zero unit overlaps, zero units outside their real
footprint, unit square footage sums match exactly, the anomaly detector
catches the real cap-rate-off-by-100x bug found tonight, cap rate
sensitivity is financially monotonic, and the real FHA accessibility
check behaves correctly on ground vs. non-ground floors.

## Running the Oasis web app

```bash
.venv/bin/python3 demo/webapp.py
```

Open `http://127.0.0.1:5050`. Keys and `COSTAR_KIT_PATH` are read from `.env`.
If port 5050 is busy, run `PORT=5051 .venv/bin/python3 demo/webapp.py` and
open `http://127.0.0.1:5051`.

MapLibre renders the OpenFreeMap city basemap. When Oasis has a Vexcel
footprint, it uses that outline for the selected building and its ground
highlight. Extrusion height comes from the matching basemap building when
available, or is estimated from the floor count. CoStar supplies property
and lease data when configured.

### Connecting another backend

The frontend lives in `demo/static/` and calls same-origin JSON endpoints
through `api()` in `app.js`. When integrating a separately hosted backend,
route `/api/*` through the same origin using a proxy. Use the Flask handlers
in `demo/webapp.py` as the reference for request and response shapes.

- The portfolio starts at `GET /api/overview`, which supplies buildings and
  their stable `property_id` values, source availability, and boardroom agents.
- `POST /api/analyze` takes `property_id` and optional `floor_name`. Its
  response identifies the result source as `live`, `saved`, or `scan`.
- `GET /api/floors` and `GET /api/area` take `property_id` in the query.
  `POST /api/floorplan` takes `property_id` and an optional unit `mix`.
- Analysis errors return JSON with `error` and `message`. Plan for missing
  source data and preserve the result source flags so the UI can describe it.

Before deploying for multiple users, replace the in-process analysis cache
with shared storage if analyses need to persist across workers, and add
authentication and authorization around contractor email sending.

The screens: a 3D map of Houston with search and "near a hospital / school /
grocery store / highway" filters; a building view with tabs for the verdict
and checks, a clickable floor stack, a generated tentative floor plan, the
area (market, census, places nearby) and a boardroom where a structural
engineer, a city planner and a market analyst debate the conversion; then a
"proceed?" step that drafts a quote-request email to the selected contractors.

What runs depends on what is configured, and the app says which case applies
(profile button, top right):

| Setting | With it | Without it |
| --- | --- | --- |
| `COSTAR_KIT_PATH` | the pipeline runs live, for any floor | saved results from `demo/all_results.json`, tested floor only |
| `VEXCEL_API_KEY` | untested buildings fetch their footprint live | cached footprints in `output/` only |
| `ANTHROPIC_API_KEY` | quote emails are drafted by Claude (`OASIS_MODEL`, default `claude-opus-5-5`) | quote emails use the built-in template |
| `CENSUS_API_KEY` | census demographics and rent trend for the tract | that section is left empty |

Nearby places come from OpenStreetMap and are cached in
`demo/places_houston.json`; delete that file to re-fetch (takes several minutes).

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

10 real Houston buildings are permanently wired up in `demo/render_floor.py`'s
`BUILDINGS` list (First Community Tower, Williams Tower, TC Energy Center,
1415 Louisiana, 5555 San Felipe, Belchase Bldg, Houston Technology Center,
Bridgewood I, Halbouty Center, CityNorth 1), plus 25 more scanned once in
`demo/portfolio_scan.json`.

### Demo contractor email with Gmail

Open Oasis at `http://127.0.0.1:5050`. The profile and sender share the
address in `demo/quote_mail.py`. The contractor roster is a Python table
with first name, last name, and email columns. Each selected contractor gets
a separate message with a personalized greeting and the feasibility report.
Claude drafts from the project facts when `ANTHROPIC_API_KEY` is set. Otherwise
Oasis uses its built-in template.

1. Enable the Gmail API in Google Cloud. Set up the OAuth consent screen and
   add the profile Gmail address as a test user if the app is in Testing.
2. Create a Web application OAuth client with
   `https://developers.google.com/oauthplayground` as an authorized redirect URI.
   In [Google OAuth Playground](https://developers.google.com/oauthplayground),
   use your own OAuth credentials and offline access. Authorize
   `https://www.googleapis.com/auth/gmail.modify` as the profile Gmail account
   and exchange the authorization code for tokens.
3. Put the client ID, client secret, and refresh token in your local `.env`
   using the names in `.env.example`. Restart Oasis. **Save to Gmail Drafts**
   writes the edited messages to Gmail for review.
4. To demo live delivery, replace the sample rows with inboxes you control,
   set `OASIS_SEND_EMAIL=1`, and restart. Run an analysis, open the quote
   composer, and send the requests. Reply from a demo inbox with the subject
   intact. Oasis checks threads it sent about every 12 seconds while the page
   is open and shows a toast and a Gmail link in the profile.

Without live sending enabled, the primary action writes `.eml` files to
`output/outbox/`. Reply tracking is in memory for the current server run and
covers messages sent from Oasis. Keep this single-account demo on localhost.
5 real Houston buildings are permanently wired up in `demo/render_floor.py`'s
`BUILDINGS` list (Williams Tower, TC Energy Center, Woodway Center,
7900 Fannin, Two Bering Park) - deliberately trimmed down from 14 to this
smaller set, chosen for real classification variety (not_feasible, feasible
x3 at different confidence levels, and marginal/escalate), rather than
breadth. The other 9 originally-tested buildings are no longer in this
permanent list but remain in `demo/portfolio_scan.json`'s 35-building blind
batch (see below) along with 26 more never hand-picked - 21 of the 35 show
positive implied value, ranked by confidence-weighted `rank_score`, not
dollar amount alone.

## Downloadable report

Every analyzed building gets a "Download report" button in the web demo - a
real `.txt` file (survives after the browser tab and server are gone) with a
one-word BOTTOM LINE (PASS/FAIL/REVIEW), the full reasoning, legal checks,
and the live AI agent's own write-up if it was run.
