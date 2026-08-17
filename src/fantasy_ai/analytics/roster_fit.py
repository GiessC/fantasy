"""Roster fit.

ANALYTICS.md: roster fit should influence recommendations without overwhelming
raw player value.  It is therefore expressed as a **points adjustment relative
to a perfectly-fitting player**, so it is zero for a player who slots straight
into an open starting spot and negative for a player who would mostly sit.  It
can never inflate a player above their own projection.

    lineup_gain  = best_lineup(roster + player) - best_lineup(roster)
    depth_gain   = bench_start_share * (points - lineup_gain)
    effective    = lineup_gain + depth_gain
    roster_fit   = effective - points          # <= 0

``bench_start_share`` is the fraction of the fantasy season a rostered
non-starter actually starts.  Default 0.25: across a ~14-week fantasy regular
season, the player ahead of them misses roughly one bye plus two to four games
to injury or matchup, and there are usually two plausible fill-ins.  It is
configurable, and set to 0 it makes the model treat bench players as worthless.

One extra term handles the end of the draft.  When picks remaining no longer
cover the starting slots still open, taking a player who fills none of them
means a starting slot goes empty on opening day.  ``forced_fill_penalty``
charges the value of the best player who could have filled the most valuable
open slot, so the board correctly insists on a kicker at the right moment
without a hard-coded "draft K in round 15" rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import LeagueConfig
from .common import ScoredPlayer
from .lineup import Lineup, LineupSlot, expand_slots, optimal_lineup


@dataclass(slots=True)
class RosterNeeds:
    """Snapshot of what a roster still has to fill."""

    open_slots: dict[str, int] = field(default_factory=dict)
    positions_needed: set[str] = field(default_factory=set)
    counts_by_position: dict[str, int] = field(default_factory=dict)
    picks_remaining: int = 0
    starting_slots_open: int = 0

    @property
    def slack(self) -> int:
        """Picks beyond those needed to fill every open starting slot."""
        return self.picks_remaining - self.starting_slots_open

    def describe(self) -> str:
        if not self.open_slots:
            return "Starting lineup is full."
        parts = ", ".join(f"{name} x{count}" for name, count in sorted(self.open_slots.items()))
        return f"Open starting slots: {parts} ({self.picks_remaining} picks left, slack {self.slack})"


@dataclass(slots=True)
class RosterFit:
    player_id: str
    lineup_gain: float
    depth_gain: float
    effective_value: float
    points: float
    adjustment: float
    fills_open_slot: bool
    forced_fill_penalty: float = 0.0
    note: str = ""

    @property
    def total(self) -> float:
        """The points adjustment the draft score applies."""
        return self.adjustment + self.forced_fill_penalty

    @property
    def label(self) -> str:
        if self.points <= 0:
            return "n/a"
        ratio = self.effective_value / self.points
        if self.forced_fill_penalty < -1:
            return "poor"
        if ratio >= 0.97:
            return "high"
        if ratio >= 0.7:
            return "medium"
        if ratio >= 0.4:
            return "low"
        return "poor"

    def explain(self) -> str:
        parts = [
            f"Roster fit {self.label}: starting-lineup gain {self.lineup_gain:.1f} pts"
        ]
        if self.depth_gain > 0.05:
            parts.append(f"bench value {self.depth_gain:.1f}")
        if self.adjustment < -0.05:
            parts.append(f"redundancy {self.adjustment:.1f}")
        if self.forced_fill_penalty < -0.05:
            parts.append(f"leaves a starting slot unfilled {self.forced_fill_penalty:.1f}")
        if self.note:
            parts.append(self.note)
        return "; ".join(parts)


def compute_needs(
    roster: Sequence[ScoredPlayer],
    league: LeagueConfig,
    *,
    picks_remaining: int,
    slots: list[LineupSlot] | None = None,
) -> RosterNeeds:
    """What the roster still needs, from the optimal current lineup."""
    lineup = optimal_lineup(roster, league, slots=slots)
    counts: dict[str, int] = {}
    for player in roster:
        counts[player.position] = counts.get(player.position, 0) + 1
    return RosterNeeds(
        open_slots=lineup.open_slot_counts(),
        positions_needed=lineup.positions_needed(),
        counts_by_position=counts,
        picks_remaining=picks_remaining,
        starting_slots_open=len(lineup.open_slots),
    )


def _best_open_slot_value(
    lineup: Lineup, best_available: dict[str, float]
) -> tuple[float, str | None]:
    """Value of the most valuable open starting slot, and its name."""
    best_value = 0.0
    best_name: str | None = None
    for slot in lineup.open_slots:
        value = max((best_available.get(position, 0.0) for position in slot.eligible), default=0.0)
        if value > best_value:
            best_value = value
            best_name = slot.name
    return best_value, best_name


def compute_roster_fit(
    candidates: Sequence[ScoredPlayer],
    roster: Sequence[ScoredPlayer],
    league: LeagueConfig,
    *,
    picks_remaining: int,
    bench_start_share: float = 0.25,
    best_available: dict[str, float] | None = None,
) -> dict[str, RosterFit]:
    """Roster fit for each candidate against the current roster."""
    slots = expand_slots(league)
    current = optimal_lineup(roster, league, slots=slots)
    baseline = current.points
    roster_list = list(roster)

    needs = compute_needs(roster_list, league, picks_remaining=picks_remaining, slots=slots)
    open_slot_value, open_slot_name = _best_open_slot_value(current, best_available or {})
    must_fill = needs.slack <= 0 and needs.starting_slots_open > 0

    results: dict[str, RosterFit] = {}
    for candidate in candidates:
        with_candidate = optimal_lineup([*roster_list, candidate], league, slots=slots)
        lineup_gain = max(0.0, with_candidate.points - baseline)
        surplus = max(0.0, candidate.points - lineup_gain)
        depth_gain = bench_start_share * surplus
        effective = lineup_gain + depth_gain
        adjustment = effective - candidate.points
        fills = lineup_gain > 1e-9

        penalty = 0.0
        note = ""
        if must_fill and not fills:
            penalty = -open_slot_value
            note = (
                f"only {needs.picks_remaining} pick(s) left for "
                f"{needs.starting_slots_open} open starting slot(s)"
                + (f"; {open_slot_name} would go unfilled" if open_slot_name else "")
            )

        results[candidate.player_id] = RosterFit(
            player_id=candidate.player_id,
            lineup_gain=lineup_gain,
            depth_gain=depth_gain,
            effective_value=effective,
            points=candidate.points,
            adjustment=adjustment,
            fills_open_slot=fills,
            forced_fill_penalty=penalty,
            note=note,
        )
    return results
