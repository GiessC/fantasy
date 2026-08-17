"""Positional scarcity: how fast value decays at each position.

Scarcity is what makes a 210-point TE more valuable than a 215-point WR in some
leagues.  Rather than one opaque index, four separate observable quantities are
reported, each in fantasy points so they compose with VOR:

``next_player_delta``
    Points lost by taking the next player at this position instead.

``dropoff_n``
    Points lost by waiting until ``n`` more players at this position are gone --
    the practical cost of skipping a run.

``tier_cliff``
    Points from this player to the top of the next tier: the cost of missing
    this tier entirely.

``starter_supply``
    Players left at the position who still project above replacement, versus
    league-wide starting demand still unfilled.  Below 1.0 means the position is
    already short of startable bodies.

The aggregate ``scarcity_index`` is a normalised 0-1 convenience for sorting and
display only; every decision that consumes scarcity uses the point-denominated
values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .common import ScoredPlayer, group_by_position, mean
from .replacement import ReplacementLevels
from .tiers import TierResult


@dataclass(slots=True)
class PositionScarcity:
    position: str
    replacement_points: float
    above_replacement_count: int
    remaining_demand: int
    starter_supply_ratio: float
    #: Mean points lost per subsequent player, over the players above replacement.
    average_decay: float
    #: Points from the best available to the replacement level.
    span_to_replacement: float
    scarcity_index: float = 0.0

    def explain(self) -> str:
        return (
            f"{self.position}: {self.above_replacement_count} players left above "
            f"replacement ({self.replacement_points:.1f}) against {self.remaining_demand} "
            f"unfilled starting slots (supply ratio {self.starter_supply_ratio:.2f}); "
            f"value decays {self.average_decay:.1f} pts per player"
        )


@dataclass(slots=True)
class PlayerScarcity:
    player_id: str
    position: str
    next_player_delta: float
    dropoff_3: float
    dropoff_5: float
    dropoff_10: float
    tier_cliff: float
    players_left_in_tier: int
    position_scarcity_index: float

    def dropoff(self, n: int) -> float:
        return {3: self.dropoff_3, 5: self.dropoff_5, 10: self.dropoff_10}.get(n, 0.0)

    def explain(self) -> str:
        return (
            f"Next {self.position} costs {self.next_player_delta:.1f} pts; "
            f"waiting for 5 more {self.position}s off the board costs {self.dropoff_5:.1f}; "
            f"{self.players_left_in_tier} left in tier, {self.tier_cliff:.1f} pts to the next tier"
        )


@dataclass(slots=True)
class ScarcityResult:
    by_position: dict[str, PositionScarcity]
    by_player: dict[str, PlayerScarcity]

    def explain(self) -> list[str]:
        return [entry.explain() for entry in self.by_position.values()]


def compute_scarcity(
    available: Sequence[ScoredPlayer],
    replacement: ReplacementLevels,
    tiers: TierResult,
    *,
    remaining_demand: dict[str, int] | None = None,
) -> ScarcityResult:
    """Scarcity over the currently available player pool.

    ``remaining_demand`` is the league-wide count of unfilled starting slots per
    position; during a live draft the draft state supplies it, and before a
    draft it defaults to the full starter demand.
    """
    groups = group_by_position(available)
    demand = remaining_demand if remaining_demand is not None else replacement.starter_demand

    position_results: dict[str, PositionScarcity] = {}
    player_results: dict[str, PlayerScarcity] = {}

    raw_indices: dict[str, float] = {}

    for position, group in groups.items():
        replacement_points = replacement.points_for(position)
        above = [player for player in group if player.points > replacement_points]
        above_count = len(above)
        position_demand = max(0, demand.get(position, 0))

        supply_ratio = (
            above_count / position_demand if position_demand > 0 else float("inf")
        )
        decay_gaps = [
            above[index].points - above[index + 1].points for index in range(len(above) - 1)
        ]
        average_decay = mean(decay_gaps) if decay_gaps else 0.0
        span = (group[0].points - replacement_points) if group else 0.0

        # Raw scarcity: steep decay and thin supply both raise it. Using the
        # decay per *unfilled slot* keeps positions with large demand (WR in
        # most leagues) from looking artificially scarce.
        raw = average_decay * (1.0 / supply_ratio if supply_ratio > 0 else 1.0)
        raw_indices[position] = raw if supply_ratio != float("inf") else 0.0

        position_results[position] = PositionScarcity(
            position=position,
            replacement_points=replacement_points,
            above_replacement_count=above_count,
            remaining_demand=position_demand,
            starter_supply_ratio=supply_ratio if supply_ratio != float("inf") else 999.0,
            average_decay=average_decay,
            span_to_replacement=span,
        )

    highest = max(raw_indices.values(), default=0.0)
    for position, raw in raw_indices.items():
        position_results[position].scarcity_index = raw / highest if highest > 0 else 0.0

    for position, group in groups.items():
        index_value = position_results[position].scarcity_index
        for offset, player in enumerate(group):
            tier_entry = tiers.player_tiers.get(player.player_id)
            player_results[player.player_id] = PlayerScarcity(
                player_id=player.player_id,
                position=position,
                next_player_delta=player.points - _points_after(group, offset, 1),
                dropoff_3=player.points - _points_after(group, offset, 3),
                dropoff_5=player.points - _points_after(group, offset, 5),
                dropoff_10=player.points - _points_after(group, offset, 10),
                tier_cliff=tier_entry.points_to_next_tier if tier_entry else 0.0,
                players_left_in_tier=tier_entry.players_left_in_tier if tier_entry else 0,
                position_scarcity_index=index_value,
            )

    return ScarcityResult(by_position=position_results, by_player=player_results)


def _points_after(group: Sequence[ScoredPlayer], offset: int, step: int) -> float:
    """Points of the player ``step`` slots further down the position's board.

    Past the end of the pool, the last player's projection is used, so a dropoff
    over a thin position reports the real remaining decline rather than zero.
    """
    if not group:
        return 0.0
    target = offset + step
    return group[target].points if target < len(group) else group[-1].points
