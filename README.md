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
found 20 with positive implied conversion value, almost entirely buildings
that are genuinely vacant right now.

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

## Known, stated simplifications

- Construction cost ($150-220/sf) is a flat, location-naive assumption.
- "Window-to-core depth" is shorthand: none of our sources record actual
  window positions - what's measured is exterior-wall-to-core distance,
  assuming standard curtain-wall glazing.
- Matterport was evaluated and **dropped as a data/geometry source** - its
  Property Intelligence room data returned the same placeholder graph across
  four different model IDs tested, confirmed not real per-building data for
  this credential.
- The real vacancy/opportunity-cost logic ignores the option value of
  eventually re-leasing a vacant floor as office space at market rate.

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
