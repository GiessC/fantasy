"""League configuration: roster construction, draft format, keepers, scoring.

The design rule from CONFIGURATION.md is that a user changes their league by
editing YAML, never Python.  So every quantity analytics needs -- team count,
starting slots, flex eligibility, bench depth, draft slot, superflex -- is a
field here, and derived quantities (positional demand, roster capacity) are
computed from those fields rather than assumed.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ConfigError
from .positions import (
    DRAFTABLE_RESERVE_SLOTS,
    KNOWN_POSITIONS,
    RESERVE_SLOTS,
    default_eligibility,
    looks_like_flex,
    normalize_position,
    normalize_slot,
)
from .scoring import ScoringConfig

LeagueType = Literal["redraft", "keeper", "dynasty"]
DraftType = Literal["snake", "linear", "auction", "third_round_reversal"]


class RosterSlot(BaseModel):
    """One resolved starting or reserve slot type."""

    model_config = ConfigDict(frozen=True)

    name: str
    count: int
    eligible_positions: tuple[str, ...]
    is_flex: bool
    is_reserve: bool

    @property
    def is_starter(self) -> bool:
        return not self.is_reserve


class FlexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Eligibility for slots literally named ``FLEX``.
    allowed_positions: list[str] = Field(default_factory=lambda: ["RB", "WR", "TE"])
    #: Per-slot overrides, e.g. ``{SUPERFLEX: [QB, RB, WR, TE], WRT: [WR, TE]}``.
    slots: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize(self) -> FlexConfig:
        self.allowed_positions = _normalize_positions(self.allowed_positions, "league.flex")
        self.slots = {
            normalize_slot(slot): _normalize_positions(
                positions, f"league.flex.slots.{slot}"
            )
            for slot, positions in self.slots.items()
        }
        return self

    def eligibility_for(self, slot: str) -> tuple[str, ...] | None:
        name = normalize_slot(slot)
        if name in self.slots:
            return tuple(self.slots[name])
        if name == "FLEX":
            return tuple(self.allowed_positions)
        return default_eligibility(name)


def _normalize_positions(positions: list[str], context: str) -> list[str]:
    resolved: list[str] = []
    for raw in positions:
        position = normalize_position(raw)
        if position is None:
            raise ConfigError(f"{context}: empty position entry.")
        if position not in KNOWN_POSITIONS:
            raise ConfigError(
                f"{context}: unknown position {raw!r}. Known positions: {sorted(KNOWN_POSITIONS)}."
            )
        if position not in resolved:
            resolved.append(position)
    if not resolved:
        raise ConfigError(f"{context}: needs at least one eligible position.")
    return resolved


class DraftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: DraftType = "snake"
    #: 1-indexed draft slot. ``None`` means "not decided yet".
    position: int | None = Field(default=None, ge=1)
    rounds: int | None = Field(default=None, ge=1, le=60)
    #: Auction budget per team; only meaningful when ``type == "auction"``.
    budget: float | None = Field(default=None, gt=0)
    #: Seconds per pick, purely informational for the LLM's "be concise" cue.
    seconds_per_pick: int | None = Field(default=None, ge=1)
    #: Rounds whose order is reversed relative to plain snake, for exotic formats.
    reversal_round: int | None = Field(default=None, ge=2)

    @model_validator(mode="after")
    def _validate(self) -> DraftConfig:
        if self.type == "auction" and self.budget is None:
            raise ConfigError("league.draft.budget is required for auction drafts.")
        if self.type == "third_round_reversal" and self.reversal_round is None:
            self.reversal_round = 3
        return self


class KeeperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_keepers: int = Field(default=0, ge=0)
    #: How a kept player's draft cost is determined.
    cost_rule: Literal["none", "previous_round", "round_penalty", "adp_round", "auction_value"] = (
        "none"
    )
    #: Rounds gained/lost under ``round_penalty`` (negative = earlier pick cost).
    round_penalty: int = 0
    #: Maximum seasons a player may be kept; ``None`` = unlimited.
    max_seasons: int | None = Field(default=None, ge=1)
    #: Explicit keeper declarations: player name/id -> round cost.
    keepers: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> KeeperConfig:
        if self.enabled and self.max_keepers == 0 and not self.keepers:
            raise ConfigError(
                "league.keepers.enabled is true but max_keepers is 0 and no keepers are listed."
            )
        return self


class WaiverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["rolling", "faab", "reverse_standings", "none"] = "rolling"
    faab_budget: float | None = Field(default=None, gt=0)


class PlayoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teams: int | None = Field(default=None, ge=2)
    weeks: list[int] = Field(default_factory=list)
    start_week: int | None = Field(default=None, ge=1, le=18)


class LeagueConfig(BaseModel):
    """A complete league definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = "My Fantasy League"
    season: int = Field(ge=1990, le=2100)
    type: LeagueType = "redraft"
    teams: int = Field(ge=2, le=32)

    scoring: ScoringConfig = Field(default_factory=ScoringConfig)

    #: Slot name -> count. Includes reserve slots (``BENCH``, ``IR``, ``TAXI``).
    roster: dict[str, int] = Field(
        default_factory=lambda: {
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "BENCH": 6
        }
    )
    flex: FlexConfig = Field(default_factory=FlexConfig)
    draft: DraftConfig = Field(default_factory=DraftConfig)
    keepers: KeeperConfig = Field(default_factory=KeeperConfig)
    waivers: WaiverConfig = Field(default_factory=WaiverConfig)
    playoff: PlayoffConfig = Field(default_factory=PlayoffConfig)

    #: Free-form notes surfaced to the LLM (house rules, trade norms, ...).
    notes: list[str] = Field(default_factory=list)

    # -- validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _validate(self) -> LeagueConfig:
        if not self.roster:
            raise ConfigError("league.roster cannot be empty.")

        normalized: dict[str, int] = {}
        for raw_slot, count in self.roster.items():
            slot = normalize_slot(raw_slot)
            if count < 0:
                raise ConfigError(f"league.roster.{raw_slot} cannot be negative.")
            if slot in normalized:
                raise ConfigError(f"league.roster has duplicate slot {slot!r}.")
            normalized[slot] = int(count)
        self.roster = normalized

        slots = self.roster_slots()
        if not any(slot.is_starter and slot.count for slot in slots):
            raise ConfigError("league.roster defines no starting slots.")

        if self.draft.position is not None and self.draft.position > self.teams:
            raise ConfigError(
                f"league.draft.position ({self.draft.position}) exceeds team count ({self.teams})."
            )
        # Note: rounds may legitimately exceed roster_size (leagues that draft
        # deep and cut down, or that fill IR/taxi from the draft), so that is a
        # warning from validate_settings rather than an error here.
        if (
            self.draft.reversal_round is not None
            and self.draft.rounds is not None
            and self.draft.reversal_round > self.draft.rounds
        ):
            raise ConfigError("league.draft.reversal_round is beyond the final round.")
        if self.type == "redraft" and self.keepers.enabled:
            raise ConfigError(
                "league.keepers.enabled is true but league.type is 'redraft'. "
                "Use type 'keeper' or 'dynasty'."
            )
        return self

    # -- derived views ------------------------------------------------------

    def roster_slots(self) -> list[RosterSlot]:
        """Resolve the raw ``roster`` mapping into typed slots with eligibility."""
        resolved: list[RosterSlot] = []
        for slot, count in self.roster.items():
            if slot in RESERVE_SLOTS:
                resolved.append(
                    RosterSlot(
                        name=slot,
                        count=count,
                        eligible_positions=(),
                        is_flex=False,
                        is_reserve=True,
                    )
                )
                continue

            eligibility = self.flex.eligibility_for(slot)
            if eligibility is not None:
                resolved.append(
                    RosterSlot(
                        name=slot,
                        count=count,
                        eligible_positions=tuple(eligibility),
                        is_flex=len(eligibility) > 1,
                        is_reserve=False,
                    )
                )
                continue

            if looks_like_flex(slot):
                raise ConfigError(
                    f"league.roster.{slot} looks like a flex slot but has no eligibility. "
                    f"Add it under league.flex.slots, e.g. 'flex: {{slots: {{{slot}: [RB, WR]}}}}'."
                )
            if slot not in KNOWN_POSITIONS:
                raise ConfigError(
                    f"league.roster.{slot} is not a known position or flex slot. "
                    f"Known positions: {sorted(KNOWN_POSITIONS)}; "
                    f"define custom flex slots under league.flex.slots."
                )
            resolved.append(
                RosterSlot(
                    name=slot,
                    count=count,
                    eligible_positions=(slot,),
                    is_flex=False,
                    is_reserve=False,
                )
            )
        return resolved

    @property
    def starting_slots(self) -> list[RosterSlot]:
        return [slot for slot in self.roster_slots() if slot.is_starter and slot.count > 0]

    @property
    def starters_per_team(self) -> int:
        return sum(slot.count for slot in self.starting_slots)

    @property
    def roster_size(self) -> int:
        """Starters plus draft-relevant reserve spots (bench, not IR/taxi)."""
        reserve = sum(
            count for slot, count in self.roster.items() if slot in DRAFTABLE_RESERVE_SLOTS
        )
        return self.starters_per_team + reserve

    @property
    def total_roster_capacity(self) -> int:
        """Every spot including IR and taxi."""
        return sum(self.roster.values())

    @property
    def effective_rounds(self) -> int:
        """Draft rounds, defaulting to a full roster when unspecified."""
        return self.draft.rounds if self.draft.rounds is not None else self.roster_size

    @property
    def is_superflex(self) -> bool:
        return any(
            slot.is_flex and "QB" in slot.eligible_positions
            for slot in self.starting_slots
        )

    @property
    def drafted_positions(self) -> list[str]:
        """Positions that can occupy a starting slot in this league."""
        seen: list[str] = []
        for slot in self.starting_slots:
            for position in slot.eligible_positions:
                if position not in seen:
                    seen.append(position)
        return seen

    def dedicated_starters(self) -> dict[str, int]:
        """Per-team count of single-position starting slots, by position."""
        counts: Counter[str] = Counter()
        for slot in self.starting_slots:
            if not slot.is_flex:
                counts[slot.eligible_positions[0]] += slot.count
        return dict(counts)

    def flex_slots(self) -> list[RosterSlot]:
        return [slot for slot in self.starting_slots if slot.is_flex]

    def flex_capacity(self) -> dict[str, int]:
        """Per-team flex slots each position is eligible for.

        A position can be eligible for several flex types, so these counts
        overlap; replacement-level math resolves the contention by simulation
        rather than by splitting these numbers.
        """
        counts: Counter[str] = Counter()
        for slot in self.flex_slots():
            for position in slot.eligible_positions:
                counts[position] += slot.count
        return dict(counts)

    def eligible_slots_for(self, position: str) -> list[RosterSlot]:
        return [slot for slot in self.starting_slots if position in slot.eligible_positions]

    def summary(self) -> dict[str, object]:
        """Compact description used in CLI output and LLM context."""
        return {
            "name": self.name,
            "season": self.season,
            "type": self.type,
            "teams": self.teams,
            "scoring_format": self.scoring.compile().describe_format(),
            "starting_lineup": {
                slot.name: slot.count for slot in self.starting_slots
            },
            "flex_eligibility": {
                slot.name: list(slot.eligible_positions) for slot in self.flex_slots()
            },
            "bench": self.roster.get("BENCH", 0),
            "roster_size": self.roster_size,
            "rounds": self.effective_rounds,
            "draft_type": self.draft.type,
            "draft_position": self.draft.position,
            "superflex": self.is_superflex,
            "notes": self.notes,
        }
