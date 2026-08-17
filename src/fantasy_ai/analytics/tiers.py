"""Position tiers from value gaps.

A tier break must be explainable ("a 14.2-point drop, versus a 3.1-point typical
gap here"), so the algorithm is deliberately simple and local rather than a
black-box clustering:

1. Sort a position's players by projected points, descending.
2. Compute the consecutive gaps.
3. Break where a gap exceeds ``mean(gap) + sensitivity * stdev(gap)``, subject
   to a floor of ``min_gap_points`` and to min/max tier sizes.

Gap statistics are computed over the *top* of the position only (long tails of
near-identical bench players otherwise flatten the mean and make every early gap
look enormous).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import TierConfig
from .common import ScoredPlayer, group_by_position, mean, stdev


@dataclass(slots=True)
class Tier:
    position: str
    number: int
    players: list[ScoredPlayer] = field(default_factory=list)
    #: The gap that opened this tier, i.e. the drop from the previous tier.
    break_gap: float = 0.0
    #: Typical consecutive gap at this position, for comparison.
    typical_gap: float = 0.0

    @property
    def top_points(self) -> float:
        return self.players[0].points if self.players else 0.0

    @property
    def bottom_points(self) -> float:
        return self.players[-1].points if self.players else 0.0

    @property
    def size(self) -> int:
        return len(self.players)

    def explain(self) -> str:
        if self.number == 1:
            return (
                f"{self.position} Tier 1: {self.size} player(s), "
                f"{self.top_points:.1f}-{self.bottom_points:.1f} pts"
            )
        return (
            f"{self.position} Tier {self.number}: {self.size} player(s), "
            f"{self.top_points:.1f}-{self.bottom_points:.1f} pts "
            f"(opened by a {self.break_gap:.1f}-pt drop vs a {self.typical_gap:.1f}-pt typical gap)"
        )


@dataclass(slots=True)
class PlayerTier:
    tier: int
    position: str
    tier_size: int
    #: Points between this player and the first player in the next tier.
    points_to_next_tier: float
    #: Players remaining in this player's tier, including them.
    players_left_in_tier: int
    is_last_in_tier: bool
    #: Points lost by dropping from this player to the next player at the position.
    next_player_delta: float


@dataclass(slots=True)
class TierResult:
    tiers_by_position: dict[str, list[Tier]]
    player_tiers: dict[str, PlayerTier]

    def tier_of(self, player_id: str) -> int | None:
        entry = self.player_tiers.get(player_id)
        return entry.tier if entry else None

    def explain(self, position: str) -> list[str]:
        return [tier.explain() for tier in self.tiers_by_position.get(position, [])]


def _tier_boundaries(points: list[float], config: TierConfig) -> list[int]:
    """Indices at which a new tier starts (never includes 0)."""
    if len(points) < 2:
        return []
    gaps = [points[index] - points[index + 1] for index in range(len(points) - 1)]
    average = mean(gaps)
    spread = stdev(gaps)
    threshold = max(average + config.sensitivity * spread, config.min_gap_points)

    boundaries: list[int] = []
    current_size = 0
    for index, gap in enumerate(gaps):
        current_size += 1
        forced = current_size >= config.max_tier_size
        allowed = current_size >= config.min_tier_size
        if (gap >= threshold and allowed) or forced:
            boundaries.append(index + 1)
            current_size = 0
    return boundaries


def compute_tiers(
    players: Sequence[ScoredPlayer], config: TierConfig | None = None
) -> TierResult:
    """Assign tiers within each position."""
    config = config or TierConfig()
    groups = group_by_position(players)

    tiers_by_position: dict[str, list[Tier]] = {}
    player_tiers: dict[str, PlayerTier] = {}

    for position, group in groups.items():
        considered = group[: config.max_players_per_position]
        points = [player.points for player in considered]
        gaps = [points[i] - points[i + 1] for i in range(len(points) - 1)]
        typical_gap = mean(gaps) if gaps else 0.0

        boundaries = set(_tier_boundaries(points, config))
        tiers: list[Tier] = []
        current = Tier(position=position, number=1, typical_gap=typical_gap)

        for index, player in enumerate(considered):
            if index in boundaries and current.players:
                tiers.append(current)
                current = Tier(
                    position=position,
                    number=len(tiers) + 1,
                    break_gap=points[index - 1] - points[index],
                    typical_gap=typical_gap,
                )
            current.players.append(player)
        if current.players:
            tiers.append(current)

        # Players beyond the considered depth land in a final catch-all tier.
        if len(group) > len(considered):
            overflow = Tier(
                position=position,
                number=len(tiers) + 1,
                typical_gap=typical_gap,
                players=list(group[len(considered):]),
            )
            tiers.append(overflow)

        tiers_by_position[position] = tiers
        position_index = {player.player_id: index for index, player in enumerate(group)}

        for tier_index, tier in enumerate(tiers):
            next_tier_top = (
                tiers[tier_index + 1].top_points if tier_index + 1 < len(tiers) else None
            )
            for position_in_tier, player in enumerate(tier.players):
                overall_index = position_index.get(player.player_id, 0)
                next_player_points = (
                    group[overall_index + 1].points if overall_index + 1 < len(group) else None
                )
                player_tiers[player.player_id] = PlayerTier(
                    tier=tier.number,
                    position=position,
                    tier_size=tier.size,
                    points_to_next_tier=(
                        player.points - next_tier_top if next_tier_top is not None else 0.0
                    ),
                    players_left_in_tier=tier.size - position_in_tier,
                    is_last_in_tier=position_in_tier == tier.size - 1,
                    next_player_delta=(
                        player.points - next_player_points
                        if next_player_points is not None
                        else 0.0
                    ),
                )

    return TierResult(tiers_by_position=tiers_by_position, player_tiers=player_tiers)
