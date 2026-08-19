"""Explicit risk representation.

ANALYTICS.md requires risk to be visible rather than folded into a projection.
So nothing here modifies projected points; the risk profile is computed
separately and enters the draft score as a *named, capped* discount that the
user can see and zero out.

Four observable components, each converted to points via a configurable weight:

``projection_uncertainty``
    From expert-ranking dispersion. A wide expert range is the best available
    proxy for "nobody knows what this player's role will be".

``injury``
    Current designation, mapped to a severity scale.

``age``
    Years past the position's typical decline age. RBs decline early, QBs late,
    so the curve is per-position and configurable.

``role``
    Rookies and players with no established usage carry extra variance. Note
    this is *uncertainty*, not badness -- it widens outcomes in both directions,
    which is why it is reported separately and weighted lightly.

``durability``
    Share of recent seasons missed, from completed-season games played. Every
    other component describes the player as he is *today*; this is the only one
    with a memory, and a currently-healthy player who has broken down twice is
    otherwise indistinguishable from one who never has. Needs at least two
    seasons, because one cannot separate a freak injury from a pattern, and is
    silent when no history is stored rather than assuming durability.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import RiskConfig
from ..models import InjuryRecord, Player, SeasonHistoryRecord
from .common import clamp
from .market import ConsensusProfile

#: Injury designations mapped to a 0-1 severity. Anything unrecognised is 0.
INJURY_SEVERITY: dict[str, float] = {
    "healthy": 0.0,
    "active": 0.0,
    "probable": 0.1,
    "questionable": 0.3,
    "doubtful": 0.6,
    "out": 0.75,
    "sus": 0.6,
    "suspended": 0.7,
    "pup": 0.8,
    "physically unable to perform": 0.8,
    "nfi": 0.8,
    "ir": 1.0,
    "injured reserve": 1.0,
    "covid": 0.3,
    "dtd": 0.25,
    "day-to-day": 0.25,
}

#: Player statuses that mean "will not play this season".
INACTIVE_STATUSES = frozenset({"inactive", "retired", "ir", "injured reserve", "pup", "nfi"})


@dataclass(slots=True)
class DurabilityProfile:
    """Recency-weighted share of recent seasons a player was available for."""

    #: 1.0 = never missed a game across the seasons considered.
    availability: float
    seasons_used: int
    #: (season, games played, games possible), newest first.
    seasons: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def missed_share(self) -> float:
        return clamp(1.0 - self.availability, 0.0, 1.0)

    def describe(self) -> str:
        games = ", ".join(
            f"{season}: {played}/{possible}" for season, played, possible in self.seasons
        )
        return f"{self.availability * 100:.0f}% of games played ({games})"


def compute_durability(
    history: Sequence[SeasonHistoryRecord],
    *,
    season_weights: Sequence[float] | None = None,
    min_seasons: int = 2,
) -> DurabilityProfile | None:
    """Weight recent seasons more heavily, or return ``None`` if too little is known.

    Returning ``None`` rather than a neutral score matters: absence of history
    is not evidence of durability, and a rookie must not be quietly credited
    with a clean bill of health he has not earned.
    """
    weights = list(season_weights or (0.5, 0.3, 0.2))
    usable = [
        record
        for record in sorted(history, key=lambda item: item.season, reverse=True)
        if record.availability is not None
    ][: len(weights)]
    if len(usable) < min_seasons:
        return None

    total_weight = 0.0
    weighted = 0.0
    seasons: list[tuple[int, int, int]] = []
    for record, weight in zip(usable, weights, strict=False):
        availability = record.availability
        assert availability is not None  # filtered above
        weighted += availability * weight
        total_weight += weight
        seasons.append(
            (record.season, int(record.games_played or 0), int(record.games_possible or 0))
        )
    if total_weight <= 0:
        return None
    return DurabilityProfile(
        availability=clamp(weighted / total_weight, 0.0, 1.0),
        seasons_used=len(usable),
        seasons=seasons,
    )


@dataclass(slots=True)
class RiskComponent:
    name: str
    raw: float
    weight: float
    points: float
    note: str = ""

    def describe(self) -> str:
        base = f"{self.name}: {self.raw:.2f} x {self.weight:g} = {self.points:.1f} pts"
        return f"{base} ({self.note})" if self.note else base


@dataclass(slots=True)
class RiskProfile:
    player_id: str
    components: list[RiskComponent] = field(default_factory=list)
    discount_points: float = 0.0
    capped: bool = False
    injury_status: str | None = None
    injury_note: str | None = None
    durability: DurabilityProfile | None = None

    @property
    def level(self) -> str:
        if self.discount_points >= 15:
            return "high"
        if self.discount_points >= 6:
            return "medium"
        if self.discount_points > 0:
            return "low"
        return "minimal"

    def component(self, name: str) -> RiskComponent | None:
        return next((item for item in self.components if item.name == name), None)

    def explain(self) -> str:
        if not self.components:
            return "Risk: minimal (no dispersion, injury, or age signal)."
        parts = "; ".join(item.describe() for item in self.components if item.points)
        cap = " (capped)" if self.capped else ""
        return f"Risk {self.level}: -{self.discount_points:.1f} pts{cap} -- {parts or 'no charges'}"


def injury_severity(status: str | None) -> float:
    if not status:
        return 0.0
    return INJURY_SEVERITY.get(status.strip().lower(), 0.0)


def is_inactive(player: Player, injury: InjuryRecord | None) -> bool:
    """Whether a player should be excluded from the draft board entirely."""
    for value in (player.status, player.injury_status, injury.status if injury else None):
        if value and value.strip().lower() in INACTIVE_STATUSES:
            return True
    return False


def compute_risk(
    player: Player,
    *,
    consensus: ConsensusProfile | None = None,
    injury: InjuryRecord | None = None,
    projected_points: float = 0.0,
    config: RiskConfig | None = None,
    history: Sequence[SeasonHistoryRecord] | None = None,
) -> RiskProfile:
    """Build a player's risk profile."""
    config = config or RiskConfig()
    components: list[RiskComponent] = []

    # 1. Projection uncertainty from expert dispersion.
    if consensus is not None and consensus.expert_stdev:
        raw = consensus.disagreement_index
        points = raw * config.consensus_weight * max(projected_points, 1.0) / 10.0
        components.append(
            RiskComponent(
                name="projection_uncertainty",
                raw=raw,
                weight=config.consensus_weight,
                points=points,
                note=(
                    f"expert sd {consensus.expert_stdev:.1f}"
                    + (
                        f", range {consensus.best:.0f}-{consensus.worst:.0f}"
                        if consensus.best is not None and consensus.worst is not None
                        else ""
                    )
                ),
            )
        )

    # 2. Injury designation.
    status = injury.status if injury and injury.status else player.injury_status
    severity = injury_severity(status)
    if severity > 0:
        components.append(
            RiskComponent(
                name="injury",
                raw=severity,
                weight=config.injury_weight,
                points=severity * config.injury_weight,
                note=str(status),
            )
        )

    # 3. Age relative to the position's decline curve.
    if player.age is not None and player.position:
        peak = config.age_curve.get(player.position)
        if peak is not None and player.age > peak:
            years_past = player.age - peak
            components.append(
                RiskComponent(
                    name="age",
                    raw=years_past,
                    weight=config.age_weight,
                    points=years_past * config.age_weight,
                    note=f"age {player.age:.1f} vs {player.position} decline age {peak:g}",
                )
            )

    # 4. Role uncertainty for players with no track record.
    if player.years_exp is not None and player.years_exp <= 0:
        components.append(
            RiskComponent(
                name="role_uncertainty",
                raw=1.0,
                weight=config.rookie_uncertainty,
                points=config.rookie_uncertainty,
                note="rookie / no established role",
            )
        )

    # 5. Durability, from completed seasons. The only component with a memory.
    durability = compute_durability(
        history or (),
        season_weights=config.durability_season_weights,
        min_seasons=config.durability_min_seasons,
    )
    if durability is not None and durability.missed_share > 0:
        components.append(
            RiskComponent(
                name="durability",
                raw=durability.missed_share,
                weight=config.durability_weight,
                points=durability.missed_share * config.durability_weight,
                note=durability.describe(),
            )
        )

    total = sum(item.points for item in components)
    capped = total > config.max_discount_points
    discount = clamp(total, 0.0, config.max_discount_points)

    return RiskProfile(
        player_id=player.player_id,
        components=components,
        durability=durability,
        discount_points=discount,
        capped=capped,
        injury_status=status,
        injury_note=injury.description if injury else None,
    )
