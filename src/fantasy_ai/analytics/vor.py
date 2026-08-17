"""Value over replacement.

    VOR = projected_points - replacement_level(position)

Methodology note (ANALYTICS.md asks for this explicitly):

The replacement level already accounts for flex contention, because
:func:`~fantasy_ai.analytics.replacement.simulate_starter_pool` fills flex slots
from the same points-ordered pool a real league drafts from.  So a flex-eligible
player's *primary* VOR is measured against their own position's replacement --
adding a separate flex adjustment on top would double-count the flex slot.

``flex_vor`` is still reported as a secondary view: it measures a player against
the best flex-eligible player who made no starting lineup at all, which answers
"how much better is this player than a streamable flex body?"  It is
informational and is not part of the draft score.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..config import LeagueConfig
from .common import ScoredPlayer
from .replacement import ReplacementLevels


@dataclass(slots=True)
class VORResult:
    player_id: str
    position: str
    points: float
    replacement_points: float
    vor: float
    flex_vor: float | None = None
    flex_eligible: bool = False

    def explain(self) -> str:
        base = (
            f"VOR = {self.points:.1f} projected - {self.replacement_points:.1f} "
            f"replacement ({self.position}) = {self.vor:+.1f}"
        )
        if self.flex_vor is not None:
            base += f"; vs flex-streamer baseline {self.flex_vor:+.1f}"
        return base


def flex_eligible_positions(league: LeagueConfig) -> set[str]:
    return {
        position
        for slot in league.flex_slots()
        for position in slot.eligible_positions
    }


def compute_vor(
    players: Sequence[ScoredPlayer],
    replacement: ReplacementLevels,
    league: LeagueConfig,
) -> dict[str, VORResult]:
    """VOR for every supplied player, keyed by player id."""
    flex_positions = flex_eligible_positions(league)
    results: dict[str, VORResult] = {}

    for player in players:
        replacement_points = replacement.points_for(player.position)
        is_flex = player.position in flex_positions
        flex_vor: float | None = None
        if is_flex and replacement.flex_points is not None:
            flex_vor = player.points - replacement.flex_points
        results[player.player_id] = VORResult(
            player_id=player.player_id,
            position=player.position,
            points=player.points,
            replacement_points=replacement_points,
            vor=player.points - replacement_points,
            flex_vor=flex_vor,
            flex_eligible=is_flex,
        )
    return results


def vor_rank_map(results: dict[str, VORResult]) -> dict[str, int]:
    """1-indexed overall rank by VOR descending -- the model's own board order."""
    ordered = sorted(results.values(), key=lambda item: item.vor, reverse=True)
    return {result.player_id: index for index, result in enumerate(ordered, start=1)}
