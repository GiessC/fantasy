"""League scoring configuration and its compiled form.

The YAML uses friendly, nested names (``passing.yards_per_point``) because that
is what a human writes.  The analytics engine wants a flat table of
``{canonical_stat_key: points_per_unit}`` plus a handful of non-linear rules.
:meth:`ScoringConfig.compile` bridges the two, and :class:`CompiledScoring` is
the only scoring artifact analytics ever sees.

Non-linear rules kept out of the flat table:

* **Distance-bucketed field goals** — a league scores ``0-39`` / ``40-49`` /
  ``50+``, but a projection often only gives total FGs made.
* **Points/yards allowed tiers** for team defenses.
* **Threshold bonuses** ("100+ rushing yards"), which are per-game events
  evaluated against a season-total projection.

Each of those is compiled into an explicit rule object so
:mod:`fantasy_ai.analytics.scoring` can show its work.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import stats as S
from ..errors import ConfigError
from .positions import KNOWN_POSITIONS, normalize_position
from .ranges import Range, parse_range_table


class _Section(BaseModel):
    """Base for scoring sections: unknown keys are an error, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")


def _yardage_rate(
    yards_per_point: float | None,
    points_per_yard: float | None,
    section: str,
) -> float:
    """Resolve the two equivalent ways to express yardage scoring into points/yard."""
    if yards_per_point is not None and points_per_yard is not None:
        raise ConfigError(
            f"scoring.{section}: set either 'yards_per_point' or 'points_per_yard', not both."
        )
    if points_per_yard is not None:
        return float(points_per_yard)
    if yards_per_point is not None:
        if yards_per_point == 0:
            raise ConfigError(f"scoring.{section}.yards_per_point cannot be 0.")
        return 1.0 / float(yards_per_point)
    return 0.0


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class PassingScoring(_Section):
    yards_per_point: float | None = 25.0
    points_per_yard: float | None = None
    touchdown: float = 4.0
    interception: float = -2.0
    two_point: float = 2.0
    completion: float = 0.0
    incompletion: float = 0.0
    attempt: float = 0.0
    first_down: float = 0.0
    sacked: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            S.PASS_YD: _yardage_rate(self.yards_per_point, self.points_per_yard, "passing"),
            S.PASS_TD: self.touchdown,
            S.PASS_INT: self.interception,
            S.PASS_2PT: self.two_point,
            S.PASS_CMP: self.completion,
            S.PASS_INC: self.incompletion,
            S.PASS_ATT: self.attempt,
            S.PASS_FIRST_DOWN: self.first_down,
            S.PASS_SACKED: self.sacked,
        }


class RushingScoring(_Section):
    yards_per_point: float | None = 10.0
    points_per_yard: float | None = None
    touchdown: float = 6.0
    two_point: float = 2.0
    attempt: float = 0.0
    first_down: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            S.RUSH_YD: _yardage_rate(self.yards_per_point, self.points_per_yard, "rushing"),
            S.RUSH_TD: self.touchdown,
            S.RUSH_2PT: self.two_point,
            S.RUSH_ATT: self.attempt,
            S.RUSH_FIRST_DOWN: self.first_down,
        }


class ReceivingScoring(_Section):
    yards_per_point: float | None = 10.0
    points_per_yard: float | None = None
    touchdown: float = 6.0
    reception: float = 0.0
    target: float = 0.0
    two_point: float = 2.0
    first_down: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            S.REC_YD: _yardage_rate(self.yards_per_point, self.points_per_yard, "receiving"),
            S.REC_TD: self.touchdown,
            S.REC: self.reception,
            S.REC_TGT: self.target,
            S.REC_2PT: self.two_point,
            S.REC_FIRST_DOWN: self.first_down,
        }


class FumbleScoring(_Section):
    lost: float = -2.0
    total: float = 0.0
    return_touchdown: float = 6.0

    def rates(self) -> dict[str, float]:
        return {
            S.FUM_LOST: self.lost,
            S.FUM: self.total,
            S.FUM_TD: self.return_touchdown,
        }


class MiscScoring(_Section):
    return_yards_per_point: float | None = None
    points_per_return_yard: float | None = None
    return_touchdown: float = 6.0
    two_point: float = 2.0

    def rates(self) -> dict[str, float]:
        return {
            S.MISC_RETURN_YD: _yardage_rate(
                self.return_yards_per_point, self.points_per_return_yard, "misc"
            ),
            S.MISC_RETURN_TD: self.return_touchdown,
            S.MISC_2PT: self.two_point,
        }


class FieldGoalScoring(_Section):
    """Field-goal scoring, flat or bucketed by distance."""

    flat: float | None = None
    ranges: dict[str, float] | None = None
    missed: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar(cls, value: Any) -> Any:
        """Allow ``field_goal: 3`` as shorthand for ``field_goal: {flat: 3}``."""
        if isinstance(value, int | float):
            return {"flat": float(value)}
        return value

    @model_validator(mode="after")
    def _require_one(self) -> FieldGoalScoring:
        if self.flat is None and not self.ranges:
            self.flat = 3.0
        if self.flat is not None and self.ranges:
            raise ConfigError(
                "scoring.kicking.field_goal: set either a flat value or 'ranges', not both."
            )
        return self

    def parsed_ranges(self) -> list[tuple[Range, float]]:
        return parse_range_table(dict(self.ranges)) if self.ranges else []


#: Share of made field goals falling in each canonical distance bucket.
#: Used only when a projection reports total FGs made without a distance split.
#: Values approximate recent league-wide NFL kicking distributions and are
#: overridable via ``scoring.kicking.distance_distribution``.
DEFAULT_FG_DISTRIBUTION: dict[str, float] = {
    S.KICK_FGM_0_19: 0.01,
    S.KICK_FGM_20_29: 0.21,
    S.KICK_FGM_30_39: 0.28,
    S.KICK_FGM_40_49: 0.29,
    S.KICK_FGM_50_PLUS: 0.21,
}


class KickingScoring(_Section):
    extra_point: float = 1.0
    extra_point_missed: float = 0.0
    field_goal: FieldGoalScoring = Field(default_factory=FieldGoalScoring)
    field_goal_attempt: float = 0.0
    distance_distribution: dict[str, float] | None = None

    @model_validator(mode="after")
    def _check_distribution(self) -> KickingScoring:
        if self.distance_distribution is None:
            return self
        unknown = set(self.distance_distribution) - set(DEFAULT_FG_DISTRIBUTION)
        if unknown:
            raise ConfigError(
                "scoring.kicking.distance_distribution has unknown buckets: "
                f"{sorted(unknown)}. Valid buckets: {sorted(DEFAULT_FG_DISTRIBUTION)}."
            )
        total = sum(self.distance_distribution.values())
        if total <= 0:
            raise ConfigError("scoring.kicking.distance_distribution must sum to a positive value.")
        return self

    def resolved_distribution(self) -> dict[str, float]:
        """The distance distribution, normalised to sum to 1."""
        raw = self.distance_distribution or DEFAULT_FG_DISTRIBUTION
        total = sum(raw.values())
        return {bucket: raw.get(bucket, 0.0) / total for bucket in DEFAULT_FG_DISTRIBUTION}

    def rates(self) -> dict[str, float]:
        rates = {
            S.KICK_XPM: self.extra_point,
            S.KICK_XP_MISS: self.extra_point_missed,
            S.KICK_FGA: self.field_goal_attempt,
            S.KICK_FG_MISS: self.field_goal.missed,
        }
        if self.field_goal.flat is not None:
            rates[S.KICK_FGM] = self.field_goal.flat
            for bucket, _, _ in S.FG_DISTANCE_BUCKETS:
                rates[bucket] = self.field_goal.flat
        return rates


class DefenseScoring(_Section):
    """Team defense / special teams."""

    sack: float = 1.0
    interception: float = 2.0
    fumble_recovery: float = 2.0
    touchdown: float = 6.0
    safety: float = 2.0
    blocked_kick: float = 2.0
    points_allowed: dict[str, float] | None = None
    yards_allowed: dict[str, float] | None = None
    points_allowed_per_game_cv: float = Field(default=0.45, gt=0.0, le=2.0)
    yards_allowed_per_game_cv: float = Field(default=0.25, gt=0.0, le=2.0)

    def rates(self) -> dict[str, float]:
        return {
            S.DST_SACK: self.sack,
            S.DST_INT: self.interception,
            S.DST_FUM_REC: self.fumble_recovery,
            S.DST_TD: self.touchdown,
            S.DST_SAFETY: self.safety,
            S.DST_BLK: self.blocked_kick,
        }


class IDPScoring(_Section):
    tackle_solo: float = 0.0
    tackle_assist: float = 0.0
    tackle_total: float = 0.0
    tackle_for_loss: float = 0.0
    sack: float = 0.0
    interception: float = 0.0
    pass_defended: float = 0.0
    forced_fumble: float = 0.0
    fumble_recovery: float = 0.0
    touchdown: float = 0.0
    safety: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            S.IDP_TACKLE_SOLO: self.tackle_solo,
            S.IDP_TACKLE_AST: self.tackle_assist,
            S.IDP_TACKLE_TOTAL: self.tackle_total,
            S.IDP_TACKLE_LOSS: self.tackle_for_loss,
            S.IDP_SACK: self.sack,
            S.IDP_INT: self.interception,
            S.IDP_PASS_DEFENDED: self.pass_defended,
            S.IDP_FORCED_FUM: self.forced_fumble,
            S.IDP_FUM_REC: self.fumble_recovery,
            S.IDP_TD: self.touchdown,
            S.IDP_SAFETY: self.safety,
        }


class BonusRule(_Section):
    """A threshold bonus, e.g. "+3 for a 100-yard rushing game"."""

    name: str
    stat: str
    threshold: float
    points: float
    positions: list[str] | None = None
    per_game: bool = True
    repeat: bool = False
    per_game_cv: float | None = Field(default=None, gt=0.0, le=3.0)

    @model_validator(mode="after")
    def _validate(self) -> BonusRule:
        if self.stat not in S.SCORING_KEYS:
            raise ConfigError(
                f"Bonus {self.name!r} references unknown stat {self.stat!r}. "
                f"Use a canonical stat key (see fantasy_ai/stats.py)."
            )
        if self.threshold <= 0:
            raise ConfigError(f"Bonus {self.name!r} needs a positive threshold.")
        if self.positions is not None:
            normalized = [normalize_position(p) for p in self.positions]
            self.positions = [p for p in normalized if p]
        return self

    def applies_to(self, position: str | None) -> bool:
        if self.positions is None:
            return True
        return position is not None and position in self.positions

    def cv(self) -> float:
        if self.per_game_cv is not None:
            return self.per_game_cv
        return S.DEFAULT_PER_GAME_CV.get(self.stat, S.FALLBACK_PER_GAME_CV)


class PositionOverride(_Section):
    """Per-position scoring overrides, e.g. TE premium receptions."""

    passing: PassingScoring | None = None
    rushing: RushingScoring | None = None
    receiving: ReceivingScoring | None = None
    fumbles: FumbleScoring | None = None
    misc: MiscScoring | None = None
    custom: dict[str, float] = Field(default_factory=dict)

    def rates(self, base: ScoringConfig) -> dict[str, float]:
        """Rates that differ from ``base``.

        Only the sections present in the override contribute, and within a
        section only values that actually differ from the base are emitted, so
        an override never resurrects a default the league turned off.
        """
        overrides: dict[str, float] = {}
        pairs = (
            (self.passing, base.passing),
            (self.rushing, base.rushing),
            (self.receiving, base.receiving),
            (self.fumbles, base.fumbles),
            (self.misc, base.misc),
        )
        for override_section, base_section in pairs:
            if override_section is None:
                continue
            base_rates = base_section.rates()
            for key, value in override_section.rates().items():
                if base_rates.get(key) != value:
                    overrides[key] = value
        overrides.update(self.custom)
        return overrides


class ScoringConfig(_Section):
    """The ``league.scoring`` block."""

    passing: PassingScoring = Field(default_factory=PassingScoring)
    rushing: RushingScoring = Field(default_factory=RushingScoring)
    receiving: ReceivingScoring = Field(default_factory=ReceivingScoring)
    fumbles: FumbleScoring = Field(default_factory=FumbleScoring)
    misc: MiscScoring = Field(default_factory=MiscScoring)
    kicking: KickingScoring = Field(default_factory=KickingScoring)
    defense: DefenseScoring = Field(default_factory=DefenseScoring)
    idp: IDPScoring = Field(default_factory=IDPScoring)

    #: Extra flat rates keyed directly by canonical stat key, for anything the
    #: named sections do not cover.
    custom: dict[str, float] = Field(default_factory=dict)

    #: Per-position deltas (TE premium, QB-specific rules, ...).
    position_overrides: dict[str, PositionOverride] = Field(default_factory=dict)

    bonuses: list[BonusRule] = Field(default_factory=list)

    #: Games a full-season projection is assumed to cover, used by per-game bonuses.
    season_games: int = Field(default=S.DEFAULT_SEASON_GAMES, ge=1, le=25)

    @model_validator(mode="after")
    def _validate(self) -> ScoringConfig:
        if unknown := S.unknown_keys(self.custom):
            raise ConfigError(
                f"scoring.custom has unknown stat keys: {unknown}. "
                "See fantasy_ai/stats.py for the canonical vocabulary."
            )
        fixed: dict[str, PositionOverride] = {}
        for raw_position, override in self.position_overrides.items():
            position = normalize_position(raw_position)
            if position is None:
                raise ConfigError("scoring.position_overrides has an empty position key.")
            if position not in KNOWN_POSITIONS:
                raise ConfigError(
                    f"scoring.position_overrides references unknown position {raw_position!r}."
                )
            if unknown := S.unknown_keys(override.custom):
                raise ConfigError(
                    f"scoring.position_overrides.{position}.custom has unknown "
                    f"stat keys: {unknown}."
                )
            fixed[position] = override
        self.position_overrides = fixed
        return self

    def compile(self) -> CompiledScoring:
        """Flatten into the form analytics consumes."""
        rates: dict[str, float] = {}
        for section in (
            self.passing,
            self.rushing,
            self.receiving,
            self.fumbles,
            self.misc,
            self.kicking,
            self.defense,
            self.idp,
        ):
            rates.update(section.rates())
        rates.update(self.custom)

        # Distance-bucketed field goals override the flat FG rate per bucket.
        fg_ranges = self.kicking.field_goal.parsed_ranges()
        bucket_points: dict[str, float] = {}
        if fg_ranges:
            for bucket, low, high in S.FG_DISTANCE_BUCKETS:
                bucket_range = Range(float(low), float(high) if high is not None else float("inf"))
                bucket_points[bucket] = _weighted_points(bucket_range, fg_ranges)
            rates.update(bucket_points)
            # A projection reporting only total FGs made is scored with the
            # league's distance distribution; see CompiledScoring.fg_blended_rate.
            distribution = self.kicking.resolved_distribution()
            rates[S.KICK_FGM] = sum(
                distribution[bucket] * bucket_points[bucket] for bucket in bucket_points
            )

        position_rates = {
            position: override.rates(self)
            for position, override in self.position_overrides.items()
        }

        return CompiledScoring(
            rates={key: float(value) for key, value in rates.items() if value},
            position_rates={
                position: overrides for position, overrides in position_rates.items() if overrides
            },
            bonuses=tuple(self.bonuses),
            points_allowed=tuple(parse_range_table(self.defense.points_allowed))
            if self.defense.points_allowed
            else (),
            yards_allowed=tuple(parse_range_table(self.defense.yards_allowed))
            if self.defense.yards_allowed
            else (),
            points_allowed_cv=self.defense.points_allowed_per_game_cv,
            yards_allowed_cv=self.defense.yards_allowed_per_game_cv,
            fg_distribution=self.kicking.resolved_distribution(),
            season_games=self.season_games,
        )


def _weighted_points(bucket: Range, table: list[tuple[Range, float]]) -> float:
    """Points for a canonical FG bucket given the league's (possibly coarser) ranges.

    When a league range straddles a bucket boundary (``0-35`` against our
    ``30-39`` bucket) the points are averaged by yard overlap rather than
    silently rounded to one side.
    """
    total_weight = 0.0
    total_points = 0.0
    for rng, points in table:
        overlap = bucket.overlap(rng)
        if overlap == 0:
            continue
        # Unbounded overlap (both open-ended) contributes with weight 1.
        weight = 1.0 if overlap == float("inf") else overlap
        total_weight += weight
        total_points += weight * points
    if total_weight == 0:
        return 0.0
    return total_points / total_weight


class CompiledScoring(BaseModel):
    """Immutable, flattened scoring rules.

    ``rates`` maps a canonical stat key to points per unit.  ``position_rates``
    layers per-position deltas on top.  The remaining fields carry the
    non-linear rules that cannot be expressed as a per-unit rate.
    """

    model_config = ConfigDict(frozen=True)

    rates: dict[str, float]
    position_rates: dict[str, dict[str, float]] = Field(default_factory=dict)
    bonuses: tuple[BonusRule, ...] = ()
    points_allowed: tuple[tuple[Range, float], ...] = ()
    yards_allowed: tuple[tuple[Range, float], ...] = ()
    points_allowed_cv: float = 0.45
    yards_allowed_cv: float = 0.25
    fg_distribution: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_FG_DISTRIBUTION))
    season_games: int = S.DEFAULT_SEASON_GAMES

    def rate(self, stat: str, position: str | None = None) -> float:
        """Points per unit for ``stat``, honouring any per-position override."""
        if position is not None:
            override = self.position_rates.get(position)
            if override is not None and stat in override:
                return override[stat]
        return self.rates.get(stat, 0.0)

    def active_stats(self, position: str | None = None) -> set[str]:
        """Stat keys with a non-zero rate for this position."""
        keys = {stat for stat, value in self.rates.items() if value}
        if position and (override := self.position_rates.get(position)):
            keys |= {stat for stat, value in override.items() if value}
        return keys

    @property
    def is_ppr(self) -> bool:
        return self.rates.get(S.REC, 0.0) > 0

    @property
    def ppr_value(self) -> float:
        return self.rates.get(S.REC, 0.0)

    def describe_format(self) -> str:
        """Short human label such as ``"Half PPR"`` or ``"0.5 PPR (TE 1.0)"``."""
        ppr = self.ppr_value
        base = {0.0: "Standard", 0.5: "Half PPR", 1.0: "Full PPR"}.get(ppr, f"{ppr:g} PPR")
        te_rate = self.position_rates.get("TE", {}).get(S.REC)
        if te_rate is not None and te_rate != ppr:
            return f"{base} (TE {te_rate:g})"
        return base


ScoringFormat = Annotated[
    Literal["standard", "half_ppr", "ppr"],
    Field(description="Coarse scoring bucket used when querying external sources."),
]
