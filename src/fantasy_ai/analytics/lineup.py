"""Optimal starting-lineup assignment.

Given a roster and a league's slots: who starts, and what does the lineup
project?  Used by roster fit ("how much would this player improve my starting
lineup?") and by the simulator when valuing a finished roster.

The algorithm is exact, not a heuristic.  The families of player-sets that can
be simultaneously matched into slots form a *transversal matroid*, and greedy by
weight is optimal over a matroid.  So:

1. Sort players by projected points, descending.
2. Try to add each to the matching via an augmenting path (Kuhn's algorithm).
3. Keep them if a matching including them exists.

That yields the maximum-point set of starters for any slot structure -- nested
(``QB`` inside ``SUPERFLEX``) or crossing (``WRT`` alongside ``FLEX``) -- with no
special cases.  Roster-sized inputs make the cost irrelevant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import LeagueConfig
from .common import ScoredPlayer


@dataclass(frozen=True, slots=True)
class LineupSlot:
    """One concrete starting slot instance (``FLEX: 2`` expands to two of these)."""

    name: str
    eligible: frozenset[str]
    index: int

    def accepts(self, position: str | None) -> bool:
        return position is not None and position in self.eligible


@dataclass(slots=True)
class Lineup:
    """A filled (or partly filled) starting lineup."""

    slots: list[LineupSlot] = field(default_factory=list)
    assignments: dict[int, ScoredPlayer] = field(default_factory=dict)
    bench: list[ScoredPlayer] = field(default_factory=list)

    @property
    def points(self) -> float:
        return sum(player.points for player in self.assignments.values())

    @property
    def filled(self) -> int:
        return len(self.assignments)

    @property
    def starters(self) -> list[ScoredPlayer]:
        return [
            self.assignments[slot.index]
            for slot in self.slots
            if slot.index in self.assignments
        ]

    @property
    def open_slots(self) -> list[LineupSlot]:
        return [slot for slot in self.slots if slot.index not in self.assignments]

    def open_slot_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for slot in self.open_slots:
            counts[slot.name] = counts.get(slot.name, 0) + 1
        return counts

    def positions_needed(self) -> set[str]:
        """Positions that could fill at least one open slot."""
        return {position for slot in self.open_slots for position in slot.eligible}

    def describe(self) -> list[str]:
        rows = []
        for slot in self.slots:
            player = self.assignments.get(slot.index)
            rows.append(
                f"{slot.name}: {player.name} ({player.points:.1f})"
                if player
                else f"{slot.name}: --"
            )
        return rows


def expand_slots(league: LeagueConfig) -> list[LineupSlot]:
    """Expand the league's starting slots into individual slot instances."""
    slots: list[LineupSlot] = []
    for roster_slot in league.starting_slots:
        for _ in range(roster_slot.count):
            slots.append(
                LineupSlot(
                    name=roster_slot.name,
                    eligible=frozenset(roster_slot.eligible_positions),
                    index=len(slots),
                )
            )
    return slots


def optimal_lineup(
    players: Sequence[ScoredPlayer],
    league: LeagueConfig,
    *,
    slots: list[LineupSlot] | None = None,
) -> Lineup:
    """The maximum-point starting lineup obtainable from ``players``."""
    slot_list = slots if slots is not None else expand_slots(league)
    lineup = Lineup(slots=list(slot_list))

    ordered = sorted(players, key=lambda player: player.points, reverse=True)
    if not slot_list:
        lineup.bench = ordered
        return lineup

    eligible_slots: dict[str, list[int]] = {}
    for player in ordered:
        eligible_slots[player.player_id] = [
            slot.index for slot in slot_list if slot.accepts(player.position)
        ]

    # slot index -> player id currently occupying it
    occupant: dict[int, str] = {}
    by_id = {player.player_id: player for player in ordered}
    started: list[str] = []

    for player in ordered:
        # A player with non-positive points never improves a lineup; starting
        # an empty slot scores zero, which is no worse.
        if player.points <= 0:
            continue
        if _augment(player.player_id, eligible_slots, occupant, set()):
            started.append(player.player_id)

    lineup.assignments = {
        index: by_id[player_id] for index, player_id in occupant.items()
    }
    starting = set(started)
    lineup.bench = [player for player in ordered if player.player_id not in starting]
    return lineup


def _augment(
    player_id: str,
    eligible_slots: dict[str, list[int]],
    occupant: dict[int, str],
    visited: set[int],
) -> bool:
    """Kuhn's augmenting-path step: seat ``player_id``, displacing others if possible."""
    for slot_index in eligible_slots.get(player_id, ()):
        if slot_index in visited:
            continue
        visited.add(slot_index)
        current = occupant.get(slot_index)
        if current is None or _augment(current, eligible_slots, occupant, visited):
            occupant[slot_index] = player_id
            return True
    return False


def lineup_points(players: Sequence[ScoredPlayer], league: LeagueConfig) -> float:
    """Projected points of the best starting lineup from ``players``."""
    return optimal_lineup(players, league).points
