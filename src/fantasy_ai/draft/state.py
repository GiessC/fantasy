"""Draft state: who has been taken, whose turn it is, what the user's roster is.

State lives in SQLite so a draft survives closing the terminal.  Picks are
recorded in overall-pick order; the pick's round, slot, and team are derived
from :mod:`.order` rather than stored redundantly, so the two can never drift.

``undo`` removes the most recent pick, which is the operation you actually want
mid-draft ("I typed the wrong name").
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..config import LeagueConfig
from ..db import Repositories
from ..errors import DraftStateError
from ..logging_setup import get_logger
from ..models import DraftPick, DraftRecord, Player, utcnow
from .order import PickPosition, next_pick_for_slot, picks_between, picks_for_slot, slot_for_pick

log = get_logger(__name__)


@dataclass(slots=True)
class TeamRoster:
    """One team's picks so far."""

    team_index: int
    slot: int
    is_user: bool
    player_ids: list[str] = field(default_factory=list)
    positions: dict[str, int] = field(default_factory=dict)

    def add(self, player_id: str, position: str | None) -> None:
        self.player_ids.append(player_id)
        if position:
            self.positions[position] = self.positions.get(position, 0) + 1

    @property
    def size(self) -> int:
        return len(self.player_ids)


@dataclass(slots=True)
class DraftStatus:
    """A complete snapshot of a draft in progress."""

    draft: DraftRecord
    picks: list[DraftPick]
    rosters: dict[int, TeamRoster]
    current_pick: int
    current_position: PickPosition | None
    user_slot: int
    user_next_pick: int | None
    picks_until_user: int
    picks_between_user_turns: int
    total_picks: int
    is_complete: bool

    @property
    def drafted_ids(self) -> set[str]:
        return {pick.player_id for pick in self.picks if pick.player_id}

    @property
    def user_roster(self) -> TeamRoster:
        return self.rosters[self.user_slot - 1]

    @property
    def user_player_ids(self) -> list[str]:
        return self.user_roster.player_ids

    @property
    def picks_remaining_for_user(self) -> int:
        return max(0, self.draft.rounds - self.user_roster.size)

    @property
    def on_the_clock(self) -> int | None:
        return self.current_position.slot if self.current_position else None

    @property
    def is_user_on_the_clock(self) -> bool:
        return self.on_the_clock == self.user_slot

    def describe(self) -> list[str]:
        lines = [
            f"Draft: {self.draft.name} (season {self.draft.season}, "
            f"{self.draft.teams} teams, {self.draft.rounds} rounds, {self.draft.draft_type})",
            f"Your slot: {self.user_slot}",
        ]
        if self.is_complete:
            lines.append(f"Draft complete: all {self.total_picks} picks recorded.")
            return lines
        position = self.current_position
        lines.append(
            f"On the clock: pick {self.current_pick} "
            f"({position.label() if position else '?'}), slot {self.on_the_clock}"
            + (" -- that's you" if self.is_user_on_the_clock else "")
        )
        if self.user_next_pick is not None:
            lines.append(
                f"Your next pick: {self.user_next_pick} "
                f"({self.picks_until_user} pick(s) away); "
                f"{self.picks_between_user_turns} pick(s) until the turn after that"
            )
        lines.append(
            f"Your roster: {self.user_roster.size} player(s), "
            f"{self.picks_remaining_for_user} pick(s) remaining"
        )
        return lines


class DraftStateManager:
    """Create, advance, and inspect drafts."""

    def __init__(self, repos: Repositories, league: LeagueConfig) -> None:
        self.repos = repos
        self.league = league

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        name: str | None = None,
        user_slot: int | None = None,
        teams: int | None = None,
        rounds: int | None = None,
        draft_type: str | None = None,
        external_id: str | None = None,
        external_source: str | None = None,
        replace_active: bool = False,
    ) -> DraftRecord:
        """Begin a new draft, deriving defaults from the league configuration."""
        existing = self.repos.drafts.active()
        if existing is not None:
            if not replace_active:
                raise DraftStateError(
                    f"Draft {existing.draft_id} ('{existing.name}') is still active. "
                    f"Finish it with 'draft complete', delete it with 'draft delete', "
                    f"or pass --replace."
                )
            self.repos.drafts.set_status(existing.draft_id or 0, "abandoned")

        resolved_teams = teams or self.league.teams
        resolved_slot = user_slot or self.league.draft.position
        if resolved_slot is None:
            raise DraftStateError(
                "No draft slot known. Set league.draft.position in your YAML or pass "
                "--position."
            )
        if not 1 <= resolved_slot <= resolved_teams:
            raise DraftStateError(
                f"Draft slot {resolved_slot} is outside 1..{resolved_teams}."
            )

        record = DraftRecord(
            draft_id=None,
            name=name or f"{self.league.name} {self.league.season}",
            league_name=self.league.name,
            season=self.league.season,
            teams=resolved_teams,
            rounds=rounds or self.league.effective_rounds,
            draft_type=draft_type or self.league.draft.type,
            user_slot=resolved_slot,
            external_id=external_id,
            external_source=external_source,
            settings={
                "reversal_round": self.league.draft.reversal_round,
                "roster": self.league.roster,
            },
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        created = self.repos.drafts.create(record)
        log.info(
            "Started draft %s: %s teams, %s rounds, slot %s",
            created.draft_id, created.teams, created.rounds, created.user_slot,
        )
        return created

    def active(self) -> DraftRecord:
        record = self.repos.drafts.active()
        if record is None:
            raise DraftStateError(
                "No active draft. Run 'fantasy-ai draft start' first."
            )
        return record

    def complete(self, draft_id: int) -> None:
        self.repos.drafts.set_status(draft_id, "complete")

    # -- picks -------------------------------------------------------------

    def record_pick(
        self,
        draft: DraftRecord,
        player_id: str | None,
        *,
        overall_pick: int | None = None,
        source: str = "manual",
        keeper: bool = False,
        auction_price: float | None = None,
    ) -> DraftPick:
        """Record a selection at the next open pick (or an explicit one)."""
        picks = self.repos.drafts.picks(draft.draft_id or 0)
        used = {pick.overall_pick for pick in picks}
        target = overall_pick if overall_pick is not None else self._next_open(used, draft)

        if target in used:
            raise DraftStateError(f"Pick {target} is already recorded.")
        total = draft.teams * draft.rounds
        if target > total:
            raise DraftStateError(
                f"Pick {target} is past the end of the draft ({total} total picks)."
            )
        if player_id and any(pick.player_id == player_id for pick in picks):
            raise DraftStateError(f"Player {player_id} has already been drafted.")

        position = slot_for_pick(
            target,
            draft.teams,
            draft.draft_type,
            reversal_round=draft.settings.get("reversal_round"),
        )
        pick = DraftPick(
            overall_pick=target,
            round_number=position.round_number,
            slot=position.slot,
            team_index=position.team_index,
            player_id=player_id,
            is_user=position.slot == draft.user_slot,
            source=source,
            keeper=keeper,
            auction_price=auction_price,
            created_at=utcnow(),
        )
        return self.repos.drafts.add_pick(draft.draft_id or 0, pick)

    def record_picks(
        self, draft: DraftRecord, player_ids: Sequence[str | None], *, source: str = "manual"
    ) -> list[DraftPick]:
        """Record several picks in order (used when importing an external draft)."""
        return [
            self.record_pick(draft, player_id, source=source) for player_id in player_ids
        ]

    def apply_keepers(
        self, draft: DraftRecord, resolve: Callable[[str], str | None]
    ) -> list[tuple[str, int, str | None]]:
        """Record the keepers declared in ``league.keepers.keepers``.

        The config maps a player name to the round they cost, which is the
        common house format.  Each keeper is recorded at *your* pick in that
        round, flagged ``keeper=True``, so the board treats them as already
        gone and your roster reflects them from the first pick.

        Other teams' keepers are not knowable from your own config; record them
        with ``draft pick <name> --at <overall>`` as you learn them.

        ``resolve`` maps a name to a canonical player id (or ``None``). Returns
        ``(name, round, player_id_or_None)`` per keeper, so the caller can
        report which ones could not be identified.
        """
        results: list[tuple[str, int, str | None]] = []
        if not self.league.keepers.enabled or not self.league.keepers.keepers:
            return results

        slot_picks = picks_for_slot(
            draft.user_slot, draft.teams, draft.rounds, draft.draft_type,
            reversal_round=draft.settings.get("reversal_round"),
        )
        for name, round_number in sorted(
            self.league.keepers.keepers.items(), key=lambda item: item[1]
        ):
            if not 1 <= round_number <= len(slot_picks):
                raise DraftStateError(
                    f"Keeper {name!r} costs round {round_number}, which is outside "
                    f"1..{len(slot_picks)} for this draft."
                )
            player_id = resolve(name)
            results.append((name, round_number, player_id))
            if player_id is None:
                continue
            self.record_pick(
                draft, player_id, overall_pick=slot_picks[round_number - 1],
                source="keeper", keeper=True,
            )
        return results

    def undo(self, draft: DraftRecord) -> DraftPick:
        pick = self.repos.drafts.remove_last_pick(draft.draft_id or 0)
        if pick is None:
            raise DraftStateError("No picks to undo.")
        return pick

    @staticmethod
    def _next_open(used: set[int], draft: DraftRecord) -> int:
        total = draft.teams * draft.rounds
        for overall in range(1, total + 1):
            if overall not in used:
                return overall
        raise DraftStateError("The draft is already complete.")

    # -- inspection --------------------------------------------------------

    def status(self, draft: DraftRecord, players: dict[str, Player] | None = None) -> DraftStatus:
        """Build a full status snapshot."""
        picks = self.repos.drafts.picks(draft.draft_id or 0)
        total = draft.teams * draft.rounds
        reversal = draft.settings.get("reversal_round")

        rosters = {
            index: TeamRoster(
                team_index=index, slot=index + 1, is_user=index + 1 == draft.user_slot
            )
            for index in range(draft.teams)
        }
        for pick in picks:
            if pick.player_id is None:
                continue
            player = (players or {}).get(pick.player_id)
            rosters[pick.team_index].add(pick.player_id, player.position if player else None)

        used = {pick.overall_pick for pick in picks}
        current = next((overall for overall in range(1, total + 1) if overall not in used), None)
        is_complete = current is None
        current_pick = current if current is not None else total
        position = (
            slot_for_pick(current_pick, draft.teams, draft.draft_type, reversal_round=reversal)
            if not is_complete
            else None
        )

        user_next = (
            next_pick_for_slot(
                draft.user_slot, current_pick, draft.teams, draft.rounds,
                draft.draft_type, reversal_round=reversal,
            )
            if not is_complete
            else None
        )
        until_user = max(0, user_next - current_pick) if user_next is not None else 0
        between = (
            picks_between(
                draft.user_slot, current_pick, draft.teams, draft.rounds,
                draft.draft_type, reversal_round=reversal,
            )
            if not is_complete
            else 0
        )

        return DraftStatus(
            draft=draft,
            picks=picks,
            rosters=rosters,
            current_pick=current_pick,
            current_position=position,
            user_slot=draft.user_slot,
            user_next_pick=user_next,
            picks_until_user=until_user,
            picks_between_user_turns=between,
            total_picks=total,
            is_complete=is_complete,
        )

    def user_picks(self, draft: DraftRecord) -> list[int]:
        return picks_for_slot(
            draft.user_slot, draft.teams, draft.rounds, draft.draft_type,
            reversal_round=draft.settings.get("reversal_round"),
        )

    def target_pick_for_availability(self, status: DraftStatus) -> int | None:
        """The pick availability should be measured against.

        If the user is on the clock, that is their *next* turn -- the question
        is "will he come back to me?".  Otherwise it is their upcoming turn.
        """
        draft = status.draft
        reversal = draft.settings.get("reversal_round")
        if status.is_complete:
            return None
        if status.is_user_on_the_clock:
            return next_pick_for_slot(
                draft.user_slot, status.current_pick + 1, draft.teams, draft.rounds,
                draft.draft_type, reversal_round=reversal,
            )
        return status.user_next_pick
