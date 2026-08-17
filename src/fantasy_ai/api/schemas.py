"""Response models for the HTTP API.

These are the wire contract. They deliberately mirror the shapes the CLI's
``--json`` output already produces, so both surfaces describe the analysis the
same way and a change to one is visibly a change to the other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


class ScoreComponentOut(BaseModel):
    name: str
    raw: float
    weight: float
    contribution: float
    description: str


class PlayerOut(BaseModel):
    """One analysed player, as the board table renders it."""

    player_id: str
    name: str
    position: str
    team: str | None = None
    bye_week: int | None = None

    projected_points: float
    points_per_game: float
    our_rank: int
    position_rank: int
    replacement_points: float
    vor: float

    tier: int | None = None
    players_left_in_tier: int | None = None
    points_to_next_tier: float | None = None

    adp: float | None = None
    adp_value_picks: float | None = None
    consensus_label: str | None = None
    expert_stdev: float | None = None

    next_player_delta: float | None = None
    dropoff_5: float | None = None

    availability_next_pick: float | None = None
    availability_method: str | None = None

    roster_fit: str | None = None
    roster_fit_points: float | None = None
    risk: str | None = None
    risk_points: float | None = None
    injury_status: str | None = None

    draft_score: float | None = None
    components: list[ScoreComponentOut] = Field(default_factory=list)

    projection_source: str | None = None
    adp_source: str | None = None


class ReplacementLevelOut(BaseModel):
    position: str
    points: float
    rank: int
    demand: int
    method: str
    has_starting_demand: bool
    explanation: str


class PositionScarcityOut(BaseModel):
    position: str
    replacement_points: float
    above_replacement_count: int
    remaining_demand: int
    starter_supply_ratio: float
    average_decay: float
    scarcity_index: float


class LineupSlotOut(BaseModel):
    slot: str
    player_id: str | None = None
    name: str | None = None
    position: str | None = None
    points: float | None = None


class RosterOut(BaseModel):
    players: list[PlayerRefOut] = Field(default_factory=list)
    counts_by_position: dict[str, int] = Field(default_factory=dict)
    lineup: list[LineupSlotOut] = Field(default_factory=list)
    lineup_points: float = 0.0
    open_slots: dict[str, int] = Field(default_factory=dict)
    positions_needed: list[str] = Field(default_factory=list)
    picks_remaining: int = 0


class PlayerRefOut(BaseModel):
    player_id: str
    name: str
    position: str | None = None
    team: str | None = None


class BoardOut(BaseModel):
    season: int
    generated_at: str
    availability_method: Literal["monte-carlo", "analytic", "none"]
    next_pick: int | None = None
    current_pick: int | None = None
    total_available: int
    players: list[PlayerOut]
    replacement: list[ReplacementLevelOut] = Field(default_factory=list)
    scarcity: list[PositionScarcityOut] = Field(default_factory=list)
    roster: RosterOut = Field(default_factory=RosterOut)
    warnings: list[str] = Field(default_factory=list)
    simulation: SimulationOut | None = None
    #: Increments whenever the underlying draft state changes, so the UI can
    #: tell a genuinely new board from a cached one.
    state_version: str = ""


class SimulationOut(BaseModel):
    iterations: int
    seed: int | None = None
    target_pick: int
    picks_simulated: int
    expected_best_by_position: dict[str, float] = Field(default_factory=dict)
    expected_best_overall: float = 0.0


# ---------------------------------------------------------------------------
# Player detail
# ---------------------------------------------------------------------------


class ScoringLineOut(BaseModel):
    label: str
    units: float
    rate: float
    points: float
    note: str | None = None


class PlayerDetailOut(BaseModel):
    player: PlayerOut
    explanation: list[str]
    scoring_lines: list[ScoringLineOut]
    tier_players: list[PlayerRefOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


class PickOut(BaseModel):
    overall_pick: int
    round_number: int
    slot: int
    is_user: bool
    keeper: bool
    player_id: str | None = None
    name: str | None = None
    position: str | None = None
    team: str | None = None


class DraftStatusOut(BaseModel):
    active: bool
    draft_id: int | None = None
    name: str | None = None
    season: int | None = None
    teams: int | None = None
    rounds: int | None = None
    draft_type: str | None = None
    user_slot: int | None = None
    current_pick: int | None = None
    current_round: int | None = None
    on_the_clock_slot: int | None = None
    is_user_on_the_clock: bool = False
    user_next_pick: int | None = None
    picks_until_user: int = 0
    picks_remaining_for_user: int = 0
    total_picks: int = 0
    drafted_count: int = 0
    is_complete: bool = False
    user_picks: list[int] = Field(default_factory=list)
    recent_picks: list[PickOut] = Field(default_factory=list)
    roster: RosterOut = Field(default_factory=RosterOut)
    state_version: str = ""


class StartDraftIn(BaseModel):
    name: str | None = None
    position: int | None = Field(default=None, ge=1)
    teams: int | None = Field(default=None, ge=2)
    rounds: int | None = Field(default=None, ge=1)
    draft_type: str | None = None
    replace: bool = False
    apply_keepers: bool = True


class PickIn(BaseModel):
    player_id: str | None = None
    name: str | None = None
    overall_pick: int | None = Field(default=None, ge=1)
    keeper: bool = False


class PickResultOut(BaseModel):
    pick: PickOut
    status: DraftStatusOut


# ---------------------------------------------------------------------------
# League / data
# ---------------------------------------------------------------------------


class LeagueOut(BaseModel):
    name: str
    season: int
    type: str
    teams: int
    scoring_format: str
    starting_lineup: dict[str, int]
    flex_eligibility: dict[str, list[str]]
    bench: int
    roster_size: int
    rounds: int
    draft_type: str
    draft_position: int | None = None
    superflex: bool
    notes: list[str] = Field(default_factory=list)


class FreshnessOut(BaseModel):
    dataset: str
    source: str
    records: int
    age_hours: float | None = None
    described_age: str
    stale: bool


class DataStatusOut(BaseModel):
    season: int
    threshold_hours: float
    datasets: list[FreshnessOut]
    has_data: bool
    stale_count: int
    missing: list[str] = Field(default_factory=list)


class SearchResultOut(BaseModel):
    player_id: str
    name: str
    position: str | None = None
    team: str | None = None
    drafted: bool = False


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class RecommendIn(BaseModel):
    position: str | None = None
    candidates: int | None = Field(default=None, ge=1, le=40)
    live: bool = True


class AlternativeOut(BaseModel):
    player: str
    reason: str


class RecommendOut(BaseModel):
    ok: bool
    recommendation: str | None = None
    confidence: str | None = None
    reasoning: list[str] = Field(default_factory=list)
    alternatives: list[AlternativeOut] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    #: The deterministic top candidate, always present so the UI can show what
    #: the analytics say even when the model is unusable.
    deterministic_top: str | None = None
    model: str | None = None
    attempts: int = 0
    structured_mode: str | None = None
    latency_seconds: float | None = None
    raw_text: str | None = None
    failures: list[str] = Field(default_factory=list)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    candidates: int | None = Field(default=None, ge=1, le=40)


class AskOut(BaseModel):
    ok: bool
    answer: str | None = None
    caveats: list[str] = Field(default_factory=list)
    unknown_players: list[str] = Field(default_factory=list)
    model: str | None = None
    raw_text: str | None = None


class LLMStatusOut(BaseModel):
    enabled: bool
    reachable: bool
    message: str
    base_url: str
    model: str
    available_models: list[str] = Field(default_factory=list)


class ErrorOut(BaseModel):
    error: str
    detail: str | None = None
    hint: str | None = None


class ApiInfoOut(BaseModel):
    version: str
    league: LeagueOut
    has_data: bool
    llm_enabled: bool
    config_warnings: list[str] = Field(default_factory=list)
    settings_files: dict[str, str] = Field(default_factory=dict)


# Resolve forward references declared before their targets.
RosterOut.model_rebuild()
BoardOut.model_rebuild()
