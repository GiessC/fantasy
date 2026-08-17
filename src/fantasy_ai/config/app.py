"""Runtime configuration: data sources, the local LLM, analytics tuning, paths.

Kept separate from :mod:`fantasy_ai.config.league` because these are *tool*
settings, not league rules.  Secrets never live here -- API keys are named by
environment variable and resolved at request time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ConfigError
from .positions import normalize_position


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Model):
    """Where local state lives. Relative paths resolve against the project root."""

    data_dir: Path = Path("data")
    database: Path = Path("data/fantasy.db")
    http_cache: Path = Path("data/http-cache")

    def resolve(self, root: Path) -> PathsConfig:
        def _abs(path: Path) -> Path:
            return path if path.is_absolute() else (root / path)

        return PathsConfig(
            data_dir=_abs(self.data_dir),
            database=_abs(self.database),
            http_cache=_abs(self.http_cache),
        )


class HTTPConfig(_Model):
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=1.0, ge=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_backoff_seconds: float = Field(default=30.0, gt=0)
    #: Minimum seconds between requests to the same host.
    rate_limit_interval: float = Field(default=0.0, ge=0)
    user_agent: str = "fantasy-ai/0.1 (+local draft assistant)"
    #: Seconds a cached response stays fresh. ``0`` disables the cache.
    cache_ttl_seconds: int = Field(default=6 * 3600, ge=0)


class FantasyProsConfig(_Model):
    """FantasyPros API access.

    The public API requires a key (``x-api-key``).  Endpoint paths are config,
    not code, so a shape change on their side is a YAML edit.
    """

    enabled: bool = True
    base_url: str = "https://api.fantasypros.com/public/v2/json/nfl"
    api_key_env: str = "FANTASYPROS_API_KEY"
    #: Scoring bucket to request. ``auto`` derives it from league scoring.
    scoring: Literal["auto", "STD", "HALF", "PPR"] = "auto"
    #: Positions to request rankings/projections for.
    positions: list[str] = Field(default_factory=lambda: ["QB", "RB", "WR", "TE", "K", "DST"])
    #: Ranking types to sync.
    ranking_types: list[str] = Field(default_factory=lambda: ["ADP", "ROS", "DRAFT"])
    #: Week ``0`` means full-season projections.
    projection_week: int = Field(default=0, ge=0, le=18)
    #: Optional override for a self-hosted proxy or a recorded fixture server.
    endpoints: dict[str, str] = Field(
        default_factory=lambda: {
            "consensus_rankings": "{season}/consensus-rankings",
            "projections": "{season}/projections",
            "players": "players",
        }
    )

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class SleeperConfig(_Model):
    """Sleeper API access. No authentication required."""

    enabled: bool = True
    base_url: str = "https://api.sleeper.app/v1"
    #: Optional Sleeper league to pull live draft state from.
    league_id: str | None = None
    draft_id: str | None = None
    username: str | None = None
    #: Player metadata is ~5 MB; refresh at most this often.
    player_cache_ttl_seconds: int = Field(default=24 * 3600, ge=0)


class CSVImportConfig(_Model):
    """Fallback ingestion from FantasyPros CSV exports.

    Useful when the API key is not available, or when the API shape drifts:
    every FantasyPros ranking/projection page has a "Download CSV" button.
    """

    enabled: bool = True
    directory: Path = Path("data/imports")

    def resolve(self, root: Path) -> CSVImportConfig:
        directory = self.directory if self.directory.is_absolute() else root / self.directory
        return CSVImportConfig(enabled=self.enabled, directory=directory)


class SourcesConfig(_Model):
    fantasypros: FantasyProsConfig = Field(default_factory=FantasyProsConfig)
    sleeper: SleeperConfig = Field(default_factory=SleeperConfig)
    csv_import: CSVImportConfig = Field(default_factory=CSVImportConfig)
    http: HTTPConfig = Field(default_factory=HTTPConfig)
    #: Warn when the newest snapshot for a dataset is older than this.
    staleness_warning_hours: float = Field(default=24.0, gt=0)


class LLMConfig(_Model):
    """LM Studio (or any OpenAI-compatible local server)."""

    enabled: bool = True
    base_url: str = "http://localhost:1234/v1"
    model: str = "muse-glimmer"
    api_key_env: str = "LM_STUDIO_API_KEY"
    #: LM Studio ignores the key but the OpenAI client shape requires one.
    api_key_default: str = "lm-studio"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1200, gt=0)
    timeout_seconds: float = Field(default=120.0, gt=0)
    #: Ask for a JSON schema first, then a bare JSON object, then plain text.
    structured_output: Literal["json_schema", "json_object", "prompt_only", "off"] = "json_schema"
    #: Extra attempts with a correction prompt when parsing fails.
    max_parse_retries: int = Field(default=2, ge=0, le=5)
    #: Candidates included in a recommendation request.
    max_candidates: int = Field(default=8, ge=1, le=40)
    #: Rostered players and recent picks included for context.
    max_context_picks: int = Field(default=20, ge=0, le=200)
    #: Extra request body fields (``top_p``, ``repeat_penalty``, ...).
    extra_body: dict[str, object] = Field(default_factory=dict)
    system_prompt_override: str | None = None

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env) or self.api_key_default


class ReplacementConfig(_Model):
    """How replacement level is derived. See ANALYTICS.md and analytics/replacement.py."""

    method: Literal["starter_demand", "fixed_rank", "blended"] = "starter_demand"
    #: Under ``starter_demand``, how far past the last starter the replacement
    #: player sits.  ``0`` = the first player outside the starter pool.
    offset: int = Field(default=0, ge=0)
    #: Average replacement over this many players to damp projection noise.
    window: int = Field(default=3, ge=1, le=25)
    #: Under ``fixed_rank``, explicit per-position replacement ranks.
    fixed_ranks: dict[str, int] = Field(default_factory=dict)
    #: Under ``blended``, weight on the starter-demand estimate (rest on fixed).
    blend_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Positions with no starting slot get replacement 0 and VOR == points.
    include_kickers_and_dst: bool = True

    @model_validator(mode="after")
    def _normalize(self) -> ReplacementConfig:
        self.fixed_ranks = {
            position: rank
            for raw, rank in self.fixed_ranks.items()
            if (position := normalize_position(raw)) is not None
        }
        if self.method == "fixed_rank" and not self.fixed_ranks:
            raise ConfigError(
                "analytics.replacement.method is 'fixed_rank' but no fixed_ranks were given."
            )
        return self


class TierConfig(_Model):
    """Gap-based tier detection."""

    #: A gap is a tier break when it exceeds ``sensitivity`` standard deviations
    #: above the mean consecutive gap at that position.
    sensitivity: float = Field(default=1.0, ge=0.0, le=5.0)
    #: Never break a tier below this size, to avoid one-player tiers from noise.
    min_tier_size: int = Field(default=1, ge=1)
    #: Force a break once a tier reaches this size.
    max_tier_size: int = Field(default=8, ge=1)
    #: Only consider the top N players per position (tiers below matter little).
    max_players_per_position: int = Field(default=60, ge=5)
    #: Absolute floor on a break, in fantasy points, to suppress noise breaks.
    min_gap_points: float = Field(default=1.0, ge=0.0)


class RiskConfig(_Model):
    """Weights for the explicit risk profile (never folded into projections)."""

    #: Points of risk charged per unit of expert-rank standard deviation.
    consensus_weight: float = Field(default=0.15, ge=0.0)
    #: Points charged per injury-severity unit (see analytics/risk.py).
    injury_weight: float = Field(default=6.0, ge=0.0)
    #: Points charged per year of age above the position's typical peak.
    age_weight: float = Field(default=1.5, ge=0.0)
    #: Age past which a decline charge applies, per position.
    age_curve: dict[str, float] = Field(
        default_factory=lambda: {"QB": 34.0, "RB": 27.0, "WR": 30.0, "TE": 31.0}
    )
    #: Rookies and players with no track record carry uncertainty both ways.
    rookie_uncertainty: float = Field(default=0.5, ge=0.0)
    #: Cap on the total risk discount, in points, so risk never dominates value.
    max_discount_points: float = Field(default=25.0, ge=0.0)


class DraftScoreWeights(_Model):
    """Multipliers on the draft-score components.

    Every component is already denominated in fantasy points (see
    analytics/draft_score.py), so the defaults are all ``1.0`` and the composite
    reads as "expected points of draft value".  Change a weight to express a
    preference (chase upside, ignore market value), not to make units work.
    """

    value: float = Field(default=1.0, ge=0.0)
    urgency: float = Field(default=1.0, ge=0.0)
    roster_fit: float = Field(default=1.0, ge=0.0)
    market_value: float = Field(default=0.25, ge=0.0)
    risk: float = Field(default=1.0, ge=0.0)
    tier_cliff: float = Field(default=1.0, ge=0.0)


class AnalyticsConfig(_Model):
    replacement: ReplacementConfig = Field(default_factory=ReplacementConfig)
    tiers: TierConfig = Field(default_factory=TierConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    weights: DraftScoreWeights = Field(default_factory=DraftScoreWeights)
    #: Preferred projection source order; the first with data for a player wins.
    projection_source_priority: list[str] = Field(
        default_factory=lambda: ["fantasypros", "csv", "demo"]
    )
    ranking_source_priority: list[str] = Field(
        default_factory=lambda: ["fantasypros", "csv", "demo"]
    )
    adp_source_priority: list[str] = Field(
        default_factory=lambda: ["fantasypros", "sleeper", "csv", "demo"]
    )
    #: Players outside this ADP/rank depth are ignored by board analytics.
    max_player_pool: int = Field(default=400, ge=50, le=2000)
    #: Points-per-game basis when converting season projections for display.
    display_games: int = Field(default=17, ge=1, le=25)


class SimulationConfig(_Model):
    iterations: int = Field(default=1000, ge=1, le=200_000)
    seed: int | None = 20260817
    #: Standard deviation of a player's simulated draft slot, as a fraction of
    #: their ADP, when the source gives no per-player stdev.
    adp_noise_fraction: float = Field(default=0.22, ge=0.0, le=2.0)
    #: Floor on that standard deviation, in picks, so early ADPs still move.
    adp_noise_floor: float = Field(default=3.0, ge=0.0)
    #: Ceiling, so deep sleepers do not become uniformly random.
    adp_noise_ceiling: float = Field(default=45.0, gt=0.0)
    #: Weight on positional need when simulating other teams (0 = pure ADP).
    need_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Players with no ADP are assumed to go this far past the last known ADP.
    undrafted_adp_padding: float = Field(default=40.0, ge=0.0)
    #: Cap on how many available players a simulated pick considers.
    candidate_pool: int = Field(default=60, ge=5, le=500)


class AppConfig(_Model):
    """Everything that is not league rules."""

    paths: PathsConfig = Field(default_factory=PathsConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    log_level: str = "info"

    def resolve_paths(self, root: Path) -> AppConfig:
        """Return a copy with every filesystem path made absolute."""
        clone = self.model_copy(deep=True)
        clone.paths = self.paths.resolve(root)
        clone.sources.csv_import = self.sources.csv_import.resolve(root)
        return clone
