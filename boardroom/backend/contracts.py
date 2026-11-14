"""Single source of truth for every shape that crosses a module boundary."""
from typing import Annotated, Literal, Optional, Union
from pydantic import BaseModel, Field

Layer = Literal["geometry", "aerial", "costar", "zoning", "derived"]
SeatId = Literal["engineer", "planner", "analyst"]
Verdict = Literal["pass", "escalate", "fail"]
Outcome = Literal["approve", "approve_with_conditions", "reject", "no_consensus"]

class Anchor(BaseModel):                 # where the UI pins a fact
    kind: Literal["plan_point", "plan_region", "aerial_region", "costar_row", "map_region", "building"]
    floor: Optional[int] = None
    x: Optional[float] = None            # feet, plan coordinates
    y: Optional[float] = None
    ref: Optional[str] = None            # CoStar row, aerial region or map polygon id

class Evidence(BaseModel):               # produced by CODE, never by a model
    id: str                              # "E-GEO-03"
    layer: Layer
    label: str                           # "Deepest point from a window"
    value: float | str
    unit: Optional[str] = None
    anchor: Anchor

class Incentive(BaseModel):              # copied from the city's published program, with its source
    name: str
    benefit: str                         # plain words, as published
    obligation: Optional[str] = None     # for example an affordability set-aside, as published
    source_url: str

class Jurisdiction(BaseModel):           # output of the spatial joins plus the rules lookup
    jurisdiction_id: str                 # from the municipal boundary join
    name: str
    council_district: Optional[str] = None   # from the political boundary join
    zoning_district: str                 # from the zoning join
    conversion_pathway: Literal["by_right", "special_permit", "variance", "not_allowed"]
    approval_months: Optional[int] = None
    max_far: Optional[float] = None
    max_height_ft: Optional[float] = None
    set_aside_pct: Optional[float] = None
    incentives: list[Incentive] = []
    source_url: str
    as_of: str                           # ISO date the rule was checked

class Claim(BaseModel):
    id: str = ""                         # backend assigns: "ENG-0-1"
    text: str = Field(max_length=220)
    evidence_ids: list[str] = Field(min_length=1)
    kind: Literal["support", "risk", "rebuttal", "concession"]
    target_claim_id: Optional[str] = None   # required when kind == "rebuttal"

class Condition(BaseModel):
    text: str = Field(max_length=160)    # "Cut a lightwell at grid C4"
    evidence_ids: list[str] = []

class Statement(BaseModel):              # one seat, one round
    seat: SeatId
    round: Literal[0, 1, 2]
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    claims: list[Claim] = Field(min_length=1, max_length=4)
    conditions: list[Condition] = []
    would_change_my_mind: str = Field(max_length=160)   # the falsifier
    changed_mind: bool = False                           # round 2 only

class Ruling(BaseModel):                 # computed by CODE from the final statements
    building_id: str
    outcome: Outcome
    agreement: float                     # weighted share of seats on the modal verdict
    coverage: float                      # seats present / 3; drops when a seat is muted
    confidence: float                    # agreement * coverage
    votes: dict[SeatId, Verdict]
    conditions: list[Condition]
    dissent: list[Claim]
    muted: list[SeatId] = []

class PhasePlan(BaseModel):
    phase: int
    floors: list[int]
    earliest_start: str                  # ISO date: later of lease expiry and approval timeline
    rationale: str
    evidence_ids: list[str]

class Recommendation(BaseModel):         # output of the recommendation agent
    building_id: str
    decision: Outcome
    headline: str
    phases: list[PhasePlan]
    cost_usd: float                      # computed in code from geometry
    value_usd: float                     # computed in code from CoStar rents
    payback_years: Optional[float] = None
    risks: list[str]
    rank_score: float                    # feeds portfolio ranking
    memo_markdown: str


# ---------------------------------------------------------------------------
# ADDED in Phase 1 (not in the original doc): building input + SSE events.
# ---------------------------------------------------------------------------

class FloorRow(BaseModel):               # one row of the per-floor CoStar table
    floor: int
    rsf: float
    tenant: Optional[str] = None         # None = vacant
    lease_expiry: Optional[str] = None   # ISO date
    costar_row: str                      # CoStar row id, used by Anchor.ref

class AerialContext(BaseModel):          # read off the aerial imagery
    region_id: str
    daylight_limit_ft: float             # max useful depth from a window, given obstructions
    facing_building_height_ft: float
    street_width_ft: float

class CostarSummary(BaseModel):
    row_id: str
    column_depth_ft: float
    office_asking_rent_psf_yr: float
    mf_rent_psf_month: float             # multifamily comp rent
    cap_rate: float

class Building(BaseModel):
    building_id: str
    name: str
    lat: float
    lon: float
    viability: float = Field(ge=0, le=1) # triage score from the feasibility classifier
    data_as_of: str                      # ISO date all relative dates are measured from
    lot_area_sf: float
    floors: list[FloorRow]
    plan: dict                           # GeoJSON FeatureCollection in plan feet: floor_plate + windows
    aerial: AerialContext
    costar: CostarSummary

class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str
    building_id: str
    verdict: Verdict
    jurisdiction: Jurisdiction
    evidence: list[Evidence]

class StatementEvent(BaseModel):
    type: Literal["statement"] = "statement"
    statement: Statement
    fallback_used: bool = False

class RoundComplete(BaseModel):
    type: Literal["round_complete"] = "round_complete"
    round: Literal[0, 1, 2]
    votes: dict[SeatId, Verdict]
    consensus: bool

class RulingEvent(BaseModel):
    type: Literal["ruling"] = "ruling"
    ruling: Ruling

class RecommendationEvent(BaseModel):
    type: Literal["recommendation"] = "recommendation"
    recommendation: Recommendation
    fallback_used: bool = False

BoardEvent = Annotated[
    Union[RunStarted, StatementEvent, RoundComplete, RulingEvent, RecommendationEvent],
    Field(discriminator="type"),
]

class Run(BaseModel):                    # golden_run.json and runs/<run_id>/events.json
    run_id: str
    building_id: str
    events: list[BoardEvent]
