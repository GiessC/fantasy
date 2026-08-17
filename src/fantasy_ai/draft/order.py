"""Draft order arithmetic.

Pure functions over ``(teams, rounds, draft_type)``.  No state, no database --
which makes the snake/reversal edge cases (they are where off-by-one bugs live)
directly testable.

Conventions: overall picks and rounds are 1-indexed, and so are draft slots.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import DraftStateError


@dataclass(frozen=True, slots=True)
class PickPosition:
    overall: int
    round_number: int
    slot: int

    @property
    def team_index(self) -> int:
        """0-indexed team, for array addressing."""
        return self.slot - 1

    def label(self) -> str:
        return f"{self.round_number}.{self.slot:02d} (#{self.overall})"


def slot_for_pick(
    overall_pick: int,
    teams: int,
    draft_type: str = "snake",
    *,
    reversal_round: int | None = None,
) -> PickPosition:
    """Which slot is on the clock at ``overall_pick``.

    ``snake``
        Odd rounds run 1..N, even rounds run N..1.

    ``linear``
        Every round runs 1..N.

    ``third_round_reversal``
        Snake, except the direction flips again at ``reversal_round`` (3 by
        convention), so that round repeats the previous round's order.  This
        gives the last slot back-to-back picks twice instead of once.
    """
    if teams < 1:
        raise DraftStateError("A draft needs at least one team.")
    if overall_pick < 1:
        raise DraftStateError(f"Overall pick must be >= 1, got {overall_pick}.")

    round_number = (overall_pick - 1) // teams + 1
    index_in_round = (overall_pick - 1) % teams  # 0-indexed

    if draft_type == "linear":
        slot = index_in_round + 1
    elif draft_type in {"snake", "auction", "third_round_reversal"}:
        reversed_round = round_number % 2 == 0
        if draft_type == "third_round_reversal" and reversal_round is not None:
            if round_number >= reversal_round:
                # From the reversal round onward the parity is inverted.
                reversed_round = not reversed_round
        slot = (teams - index_in_round) if reversed_round else (index_in_round + 1)
    else:
        raise DraftStateError(f"Unsupported draft type: {draft_type!r}.")

    return PickPosition(overall=overall_pick, round_number=round_number, slot=slot)


def picks_for_slot(
    slot: int,
    teams: int,
    rounds: int,
    draft_type: str = "snake",
    *,
    reversal_round: int | None = None,
) -> list[int]:
    """Every overall pick belonging to ``slot``, in order."""
    if slot < 1 or slot > teams:
        raise DraftStateError(f"Draft slot {slot} is outside 1..{teams}.")
    return [
        overall
        for overall in range(1, teams * rounds + 1)
        if slot_for_pick(
            overall, teams, draft_type, reversal_round=reversal_round
        ).slot == slot
    ]


def next_pick_for_slot(
    slot: int,
    current_overall: int,
    teams: int,
    rounds: int,
    draft_type: str = "snake",
    *,
    reversal_round: int | None = None,
) -> int | None:
    """The slot's next pick at or after ``current_overall``, or ``None`` if done."""
    for overall in picks_for_slot(
        slot, teams, rounds, draft_type, reversal_round=reversal_round
    ):
        if overall >= current_overall:
            return overall
    return None


def picks_between(
    slot: int,
    current_overall: int,
    teams: int,
    rounds: int,
    draft_type: str = "snake",
    *,
    reversal_round: int | None = None,
) -> int:
    """How many other teams pick between this slot's current and next pick.

    This is the number that actually matters for availability: a slot-1 drafter
    in a 10-team snake waits 18 picks between the first and second round, a
    slot-5 drafter waits 10.
    """
    slot_picks = picks_for_slot(
        slot, teams, rounds, draft_type, reversal_round=reversal_round
    )
    upcoming = [pick for pick in slot_picks if pick >= current_overall]
    if len(upcoming) < 2:
        return 0
    return upcoming[1] - upcoming[0] - 1


def build_order(
    teams: int,
    rounds: int,
    draft_type: str = "snake",
    *,
    reversal_round: int | None = None,
) -> list[PickPosition]:
    """The full draft order."""
    return [
        slot_for_pick(overall, teams, draft_type, reversal_round=reversal_round)
        for overall in range(1, teams * rounds + 1)
    ]


def round_and_pick(overall_pick: int, teams: int) -> tuple[int, int]:
    """``(round, index within round)``, both 1-indexed."""
    return (overall_pick - 1) // teams + 1, (overall_pick - 1) % teams + 1
