"""Canonical internal models.

Sources speak their own schemas; normalization converts them into these.  Every
record carries ``source``, the source's own player id where relevant, and a
retrieval timestamp, so nothing in the database is anonymous and stale data can
always be identified.

Player identity is a canonical ``player_id`` (see
:mod:`fantasy_ai.normalization.identity`).  Display names are never identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .stats import StatLine


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class Player:
    """A canonical player (or team defense)."""

    player_id: str
    full_name: str
    position: str | None = None
    team: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    normalized_name: str = ""
    age: float | None = None
    years_exp: int | None = None
    status: str | None = None
    injury_status: str | None = None
    bye_week: int | None = None
    height: str | None = None
    weight: str | None = None
    college: str | None = None
    birth_date: str | None = None
    depth_chart_order: int | None = None
    #: ``{source: source_player_id}``
    source_ids: dict[str, str] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_rookie(self) -> bool:
        return self.years_exp == 0

    @property
    def display(self) -> str:
        parts = [self.full_name]
        if self.position:
            parts.append(self.position)
        if self.team:
            parts.append(self.team)
        return " ".join(parts[:1]) + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")


@dataclass(slots=True)
class SourceRecord:
    """Fields shared by every externally-sourced record."""

    source: str
    retrieved_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class ProjectionRecord:
    """A statistical projection. ``week=None`` means full season."""

    player_id: str
    season: int
    source: str
    stats: StatLine
    week: int | None = None
    scoring_context: str | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class RankingRecord:
    """Expert consensus ranking with its dispersion."""

    player_id: str
    season: int
    source: str
    ecr: float | None = None
    position_rank: int | None = None
    best: float | None = None
    worst: float | None = None
    average: float | None = None
    stdev: float | None = None
    tier: int | None = None
    ranking_type: str = "DRAFT"
    scoring_format: str | None = None
    week: int | None = None
    expert_count: int | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class ADPRecord:
    """Average draft position from a market source."""

    player_id: str
    season: int
    source: str
    adp: float
    stdev: float | None = None
    best: float | None = None
    worst: float | None = None
    sample_size: int | None = None
    teams: int | None = None
    scoring_format: str | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class InjuryRecord:
    player_id: str
    season: int
    source: str
    status: str | None = None
    description: str | None = None
    body_part: str | None = None
    week: int | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class DraftPick:
    """One selection in a draft."""

    overall_pick: int
    round_number: int
    slot: int
    player_id: str | None
    team_index: int
    is_user: bool = False
    source: str = "manual"
    keeper: bool = False
    auction_price: float | None = None
    created_at: datetime = field(default_factory=utcnow)
    pick_id: int | None = None


@dataclass(slots=True)
class DraftRecord:
    """A draft in progress or completed."""

    draft_id: int | None
    name: str
    season: int
    teams: int
    rounds: int
    draft_type: str
    user_slot: int
    status: str = "active"
    league_name: str | None = None
    external_id: str | None = None
    external_source: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DataFreshness:
    """When a dataset was last synced, for the staleness report."""

    dataset: str
    source: str
    season: int | None
    retrieved_at: datetime | None
    record_count: int

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.retrieved_at is None:
            return None
        reference = now or utcnow()
        return (reference - self.retrieved_at).total_seconds() / 3600.0

    def describe_age(self, now: datetime | None = None) -> str:
        hours = self.age_hours(now)
        if hours is None:
            return "never"
        if hours < 1 / 60:
            return "just now"
        if hours < 1:
            minutes = round(hours * 60)
            return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
        if hours < 48:
            return f"{hours:.1f} hours ago"
        return f"{hours / 24:.1f} days ago"


@dataclass(slots=True)
class PlayerData:
    """Everything analytics needs about one player, already source-resolved.

    Assembled by :mod:`fantasy_ai.analytics.dataset` from the latest snapshot of
    each dataset, honouring the configured source priority.
    """

    player: Player
    projection: ProjectionRecord | None = None
    ranking: RankingRecord | None = None
    adp: ADPRecord | None = None
    injury: InjuryRecord | None = None

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def position(self) -> str | None:
        return self.player.position

    @property
    def name(self) -> str:
        return self.player.full_name

    @property
    def stats(self) -> StatLine:
        return self.projection.stats if self.projection else StatLine()

    @property
    def adp_value(self) -> float | None:
        return self.adp.adp if self.adp else None

    @property
    def ecr(self) -> float | None:
        return self.ranking.ecr if self.ranking else None
