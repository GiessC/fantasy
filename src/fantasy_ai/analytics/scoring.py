"""League-adjusted fantasy point projection.

Given a projected stat line and a league's :class:`CompiledScoring`, produce a
point total *and* the line-by-line breakdown that produced it.  Nothing here
consults a source's own fantasy-point column -- those are computed under someone
else's scoring settings and are exactly the kind of hidden assumption
CONFIGURATION.md warns about.

Three pieces of the calculation are approximations, because a *season-total*
projection cannot exactly answer a *per-game* scoring question.  Each is
documented at its implementation and is configurable:

1. :func:`expected_games_over` -- how often a player clears a per-game bonus.
2. :func:`expected_tiered_points` -- DST points/yards-allowed tiers.
3. Field goals, when a source reports only a total (handled at compile time in
   :meth:`ScoringConfig.compile`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .. import stats as S
from ..config.ranges import Range
from ..config.scoring import BonusRule, CompiledScoring


@dataclass(frozen=True, slots=True)
class ScoringLine:
    """One contribution to a player's projected points."""

    key: str
    label: str
    units: float
    rate: float
    points: float
    note: str | None = None

    def describe(self) -> str:
        base = f"{self.label}: {self.units:g} x {self.rate:g} = {self.points:+.2f}"
        return f"{base} ({self.note})" if self.note else base


@dataclass(slots=True)
class ScoringResult:
    """Projected points plus a full audit trail."""

    points: float
    lines: list[ScoringLine] = field(default_factory=list)
    games: float = float(S.DEFAULT_SEASON_GAMES)

    @property
    def points_per_game(self) -> float:
        return self.points / self.games if self.games else 0.0

    def breakdown(self) -> dict[str, float]:
        """``{stat_key: points}``, summing duplicate keys."""
        totals: dict[str, float] = {}
        for line in self.lines:
            totals[line.key] = totals.get(line.key, 0.0) + line.points
        return totals

    def top_contributors(self, limit: int = 5) -> list[ScoringLine]:
        return sorted(self.lines, key=lambda line: abs(line.points), reverse=True)[:limit]

    def explain(self) -> list[str]:
        return [line.describe() for line in sorted(
            self.lines, key=lambda line: abs(line.points), reverse=True
        )]


# ---------------------------------------------------------------------------
# Per-game approximations
# ---------------------------------------------------------------------------


def _normal_sf(x: float) -> float:
    """P(Z > x) for a standard normal, via the error function."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def expected_games_over(
    season_total: float, games: float, threshold: float, cv: float
) -> float:
    """Expected number of games in which a stat clears ``threshold``.

    A season total says nothing directly about game-to-game distribution, so we
    assume per-game values are normal with mean ``season_total / games`` and
    standard deviation ``cv * mean``, and count::

        E[games over] = games * P(X > threshold)

    The normal assumption understates the right tail for spiky stats
    (touchdowns), which is why :data:`fantasy_ai.stats.DEFAULT_PER_GAME_CV`
    uses much larger coefficients of variation for those.  ``per_game_cv`` on a
    bonus rule overrides it per league.

    This only matters for leagues that configure per-game bonuses; with no
    bonuses the function is never called.
    """
    if games <= 0 or season_total <= 0 or threshold <= 0:
        return 0.0
    mean = season_total / games
    sigma = max(cv * mean, 1e-9)
    probability = _normal_sf((threshold - mean) / sigma)
    return games * probability


def expected_repeat_bonus(
    season_total: float, games: float, threshold: float, cv: float
) -> float:
    """Expected count of threshold multiples across the season.

    For a repeating bonus ("+1 per 100 receiving yards, every game"), the count
    in a game is ``floor(X / threshold)``.  We sum the tail probabilities for
    each multiple, capping at a sensible number of multiples so the loop
    terminates for tiny thresholds.
    """
    if games <= 0 or season_total <= 0 or threshold <= 0:
        return 0.0
    mean = season_total / games
    sigma = max(cv * mean, 1e-9)
    total = 0.0
    max_multiples = max(1, int((mean + 6 * sigma) / threshold) + 1)
    for multiple in range(1, min(max_multiples, 50) + 1):
        total += _normal_sf((multiple * threshold - mean) / sigma)
    return games * total


def expected_tiered_points(
    season_total: float, games: float, table: tuple[tuple[Range, float], ...], cv: float
) -> tuple[float, str]:
    """Expected points from a per-game tier table given a season total.

    Team-defense scoring ("0 points allowed = 10, 1-6 = 7, ...") is evaluated
    weekly, but projections give a season total.  We model per-game values as
    normal with mean ``season_total / games`` and standard deviation
    ``cv * mean``, integrate the normal mass falling in each tier, and take the
    probability-weighted points.

    Returns the expected season points and a short note naming the modal tier,
    so the breakdown can show which tier drives the number.
    """
    if not table or games <= 0:
        return 0.0, "no tier table"
    mean = season_total / games if games else 0.0
    sigma = max(cv * max(mean, 1e-6), 1e-6)

    expected_per_game = 0.0
    best_probability = 0.0
    best_label = ""
    for rng, points in table:
        lower = rng.lower
        upper = rng.upper
        # P(lower <= X <= upper) with a continuity correction of half a unit,
        # since the underlying quantity is integer-valued.
        probability = _normal_sf((lower - 0.5 - mean) / sigma) - _normal_sf(
            (upper + 0.5 - mean) / sigma
        )
        probability = max(0.0, probability)
        expected_per_game += probability * points
        if probability > best_probability:
            best_probability = probability
            best_label = str(rng)
    note = f"{mean:.1f}/game, most likely tier {best_label} ({best_probability:.0%})"
    return expected_per_game * games, note


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


class Scorer:
    """Turns stat lines into league-adjusted fantasy points."""

    def __init__(self, scoring: CompiledScoring) -> None:
        self.scoring = scoring

    def score(self, line: S.StatLine, position: str | None = None) -> ScoringResult:
        """Score one projected stat line."""
        games = line.get_stat(S.META_GAMES) or float(self.scoring.season_games)
        lines: list[ScoringLine] = []
        total = 0.0

        handled_by_tier = set()
        if self.scoring.points_allowed:
            handled_by_tier.add(S.DST_PTS_ALLOWED)
        if self.scoring.yards_allowed:
            handled_by_tier.add(S.DST_YDS_ALLOWED)

        # If a source gave bucketed field goals, the total is redundant; scoring
        # both would double count.
        has_buckets = any(key in line for key, _, _ in S.FG_DISTANCE_BUCKETS)

        for key, units in sorted(line.items()):
            if key in S.META_KEYS or key in handled_by_tier:
                continue
            if key == S.KICK_FGM and has_buckets:
                continue
            rate = self.scoring.rate(key, position)
            if not rate or not units:
                continue
            points = units * rate
            total += points
            note = None
            if key == S.KICK_FGM and not has_buckets and self.scoring.rates.get(S.KICK_FGM):
                note = "blended over the configured distance distribution"
            lines.append(
                ScoringLine(
                    key=key, label=S.label(key), units=units, rate=rate, points=points, note=note
                )
            )

        total += self._add_tier_lines(line, games, lines)
        total += self._add_bonus_lines(line, games, position, lines)

        return ScoringResult(points=total, lines=lines, games=games)

    def _add_tier_lines(
        self, line: S.StatLine, games: float, lines: list[ScoringLine]
    ) -> float:
        added = 0.0
        for key, table, cv in (
            (S.DST_PTS_ALLOWED, self.scoring.points_allowed, self.scoring.points_allowed_cv),
            (S.DST_YDS_ALLOWED, self.scoring.yards_allowed, self.scoring.yards_allowed_cv),
        ):
            if not table or key not in line:
                continue
            season_total = line.get_stat(key)
            points, note = expected_tiered_points(season_total, games, table, cv)
            if points == 0.0:
                continue
            added += points
            lines.append(
                ScoringLine(
                    key=key,
                    label=S.label(key),
                    units=season_total,
                    rate=points / season_total if season_total else 0.0,
                    points=points,
                    note=note,
                )
            )
        return added

    def _add_bonus_lines(
        self,
        line: S.StatLine,
        games: float,
        position: str | None,
        lines: list[ScoringLine],
    ) -> float:
        added = 0.0
        for bonus in self.scoring.bonuses:
            if not bonus.applies_to(position):
                continue
            units, note = self._bonus_units(bonus, line, games)
            if units <= 0:
                continue
            points = units * bonus.points
            added += points
            lines.append(
                ScoringLine(
                    key=f"bonus:{bonus.stat}",
                    label=bonus.name,
                    units=round(units, 3),
                    rate=bonus.points,
                    points=points,
                    note=note,
                )
            )
        return added

    @staticmethod
    def _bonus_units(bonus: BonusRule, line: S.StatLine, games: float) -> tuple[float, str | None]:
        season_total = line.get_stat(bonus.stat)
        if season_total <= 0:
            return 0.0, None
        if not bonus.per_game:
            if bonus.repeat:
                return float(int(season_total // bonus.threshold)), "season total, repeating"
            return (1.0, "season total") if season_total >= bonus.threshold else (0.0, None)
        cv = bonus.cv()
        if bonus.repeat:
            return (
                expected_repeat_bonus(season_total, games, bonus.threshold, cv),
                f"expected repeats over {games:g} games (cv {cv:g})",
            )
        return (
            expected_games_over(season_total, games, bonus.threshold, cv),
            f"expected games over {bonus.threshold:g} (cv {cv:g})",
        )

    def score_many(
        self, lines: dict[str, S.StatLine], positions: dict[str, str | None]
    ) -> dict[str, ScoringResult]:
        return {
            player_id: self.score(line, positions.get(player_id))
            for player_id, line in lines.items()
        }
