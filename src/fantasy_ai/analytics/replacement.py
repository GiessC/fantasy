"""Positional replacement level.

ANALYTICS.md is emphatic that replacement level is not "rank #X".  In a 10-team
league starting 2 RB, 2 WR and 1 FLEX, the flex slot is contested, so real RB
demand is somewhere between 20 and 30 depending on how WR and TE value compares
-- and that comparison depends on the league's own scoring.

Method ``starter_demand`` (the default) resolves this by *simulating the league's
starting lineups*:

1. Sort every projected player by league-adjusted points, descending.
2. Walk the list. A player claims a dedicated slot at their position if one is
   left league-wide (``count x teams``); otherwise they claim any flex slot they
   are eligible for.
3. Stop when every starting slot in the league is filled.

Whoever a position runs out of slots for defines its starter demand, and
replacement level is the projection of the players just *outside* that pool.
Flex contention is settled by the same points comparison a real league settles
it by, so a pass-heavy scoring system automatically deepens WR demand and a
superflex automatically deepens QB demand -- with no position-specific code.

Two alternatives are configurable:

``fixed_rank``
    Explicit per-position replacement ranks, for users who prefer a convention
    (e.g. "QB12, RB30, WR36, TE12").

``blended``
    A weighted average of the two, for when the simulated demand looks
    aggressive but the convention looks stale.

To damp single-player projection noise, replacement is the **average of a small
window** of players at the replacement rank rather than one player's number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import LeagueConfig, ReplacementConfig
from ..logging_setup import get_logger
from .common import ScoredPlayer, group_by_position, mean

log = get_logger(__name__)


@dataclass(slots=True)
class ReplacementLevel:
    """Replacement level for one position, with its derivation."""

    position: str
    points: float
    rank: int
    demand: int
    method: str
    window_players: list[tuple[str, float]] = field(default_factory=list)
    has_starting_demand: bool = True
    note: str = ""

    def explain(self) -> str:
        if not self.has_starting_demand:
            return (
                f"{self.position}: no starting slot in this league, so replacement is "
                f"pinned to the best available player ({self.points:.1f}); VOR is <= 0."
            )
        names = ", ".join(f"{name} {points:.1f}" for name, points in self.window_players)
        return (
            f"{self.position}: {self.demand} league-wide starters "
            f"({self.method}), replacement = {self.position}{self.rank} "
            f"averaged over [{names}] = {self.points:.1f}"
        )


@dataclass(slots=True)
class ReplacementLevels:
    """Replacement levels for every position, plus the flex pool."""

    by_position: dict[str, ReplacementLevel]
    starter_demand: dict[str, int]
    starter_pool: dict[str, list[str]]
    flex_points: float | None = None
    flex_positions: tuple[str, ...] = ()
    method: str = "starter_demand"

    def points_for(self, position: str | None) -> float:
        if position is None:
            return 0.0
        level = self.by_position.get(position)
        return level.points if level else 0.0

    def level_for(self, position: str | None) -> ReplacementLevel | None:
        return self.by_position.get(position) if position else None

    def explain(self) -> list[str]:
        lines = [level.explain() for level in self.by_position.values()]
        if self.flex_points is not None:
            lines.append(
                f"FLEX pool ({'/'.join(self.flex_positions)}): best player left out of "
                f"every starting lineup projects {self.flex_points:.1f}"
            )
        return lines


def simulate_starter_pool(
    players: Sequence[ScoredPlayer], league: LeagueConfig
) -> tuple[dict[str, list[str]], dict[str, int], list[ScoredPlayer]]:
    """Greedily fill every team's starting lineup from the projection pool.

    Returns the starting pool by position, the resulting per-position demand,
    and the players who did *not* make any starting lineup (in points order).
    """
    dedicated_left: dict[str, int] = {
        position: count * league.teams
        for position, count in league.dedicated_starters().items()
    }
    flex_left: list[tuple[frozenset[str], int]] = [
        (frozenset(slot.eligible_positions), slot.count * league.teams)
        for slot in league.flex_slots()
    ]
    # Mutable counts alongside their eligibility sets.
    flex_counts = [count for _, count in flex_left]
    flex_eligibility = [eligibility for eligibility, _ in flex_left]

    pool: dict[str, list[str]] = {}
    leftovers: list[ScoredPlayer] = []

    ordered = sorted(players, key=lambda item: item.points, reverse=True)
    for player in ordered:
        position = player.position
        if dedicated_left.get(position, 0) > 0:
            dedicated_left[position] -= 1
            pool.setdefault(position, []).append(player.player_id)
            continue

        placed = False
        for index, eligibility in enumerate(flex_eligibility):
            if flex_counts[index] > 0 and position in eligibility:
                flex_counts[index] -= 1
                pool.setdefault(position, []).append(player.player_id)
                placed = True
                break
        if not placed:
            leftovers.append(player)

    demand = {position: len(ids) for position, ids in pool.items()}
    return pool, demand, leftovers


def _window_average(
    group: Sequence[ScoredPlayer], rank: int, window: int
) -> tuple[float, int, list[tuple[str, float]]]:
    """Average points over ``window`` players starting at 1-indexed ``rank``.

    Clamps to the available pool.  When the pool is exhausted the last player's
    projection is used, which keeps replacement finite for thin positions.
    """
    if not group:
        return 0.0, rank, []
    start = max(0, min(rank - 1, len(group) - 1))
    end = min(len(group), start + max(1, window))
    selected = list(group[start:end])
    if not selected:
        selected = [group[-1]]
    points = mean(player.points for player in selected)
    return points, start + 1, [(player.name, player.points) for player in selected]


def compute_replacement_levels(
    players: Sequence[ScoredPlayer],
    league: LeagueConfig,
    config: ReplacementConfig | None = None,
) -> ReplacementLevels:
    """Compute replacement level for every position the league starts."""
    config = config or ReplacementConfig()
    groups = group_by_position(players)
    pool, demand, leftovers = simulate_starter_pool(players, league)

    fixed_demand = {
        position: max(0, rank - 1) for position, rank in config.fixed_ranks.items()
    }

    levels: dict[str, ReplacementLevel] = {}
    startable = set(league.drafted_positions)

    for position, group in groups.items():
        if position not in startable:
            # No starting slot for this position in this league. Pin replacement
            # to the top projection so VOR is <= 0 and the board de-emphasises it
            # rather than pretending the position has starting value.
            levels[position] = ReplacementLevel(
                position=position,
                points=group[0].points if group else 0.0,
                rank=1,
                demand=0,
                method="no_demand",
                has_starting_demand=False,
            )
            continue

        simulated = demand.get(position, 0)
        fixed = fixed_demand.get(position)

        if config.method == "fixed_rank":
            if fixed is None:
                log.debug(
                    "No fixed replacement rank for %s; falling back to simulated demand.",
                    position,
                )
                effective = simulated
                method = "starter_demand (no fixed rank configured)"
            else:
                effective = fixed
                method = "fixed_rank"
        elif config.method == "blended" and fixed is not None:
            weight = config.blend_weight
            effective = int(round(weight * simulated + (1 - weight) * fixed))
            method = f"blended({weight:g} simulated / {1 - weight:g} fixed)"
        else:
            effective = simulated
            method = "starter_demand"

        rank = max(1, effective + 1 + config.offset)
        points, actual_rank, window = _window_average(group, rank, config.window)
        levels[position] = ReplacementLevel(
            position=position,
            points=points,
            rank=actual_rank,
            demand=effective,
            method=method,
            window_players=window,
        )

    flex_positions = tuple(
        sorted({p for slot in league.flex_slots() for p in slot.eligible_positions})
    )
    flex_points: float | None = None
    if flex_positions:
        flex_leftovers = [p for p in leftovers if p.position in flex_positions]
        if flex_leftovers:
            flex_points = mean(
                player.points for player in flex_leftovers[: max(1, config.window)]
            )

    return ReplacementLevels(
        by_position=levels,
        starter_demand=demand,
        starter_pool=pool,
        flex_points=flex_points,
        flex_positions=flex_positions,
        method=config.method,
    )
