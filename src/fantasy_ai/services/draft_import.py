"""Importing a live Sleeper draft into local draft state.

Lets the tool follow a real draft without retyping every pick: point it at a
Sleeper draft id and it pulls picks, maps Sleeper player ids onto canonical
players, and records anything new.

The import is incremental and idempotent -- it only records picks the local
draft does not already have -- so it can be run repeatedly while the draft is
underway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..db import Repositories
from ..draft import DraftStateManager
from ..errors import DraftStateError, SourceError
from ..logging_setup import get_logger
from ..models import DraftRecord
from ..normalization.identity import PlayerIndex
from ..sources.sleeper import SleeperClient, SleeperDraft, SleeperPick

log = get_logger(__name__)


@dataclass(slots=True)
class ImportResult:
    draft_id: int | None
    external_draft_id: str
    picks_seen: int
    picks_added: int
    unresolved: list[str] = field(default_factory=list)
    user_slot: int | None = None
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        base = (
            f"Sleeper draft {self.external_draft_id}: {self.picks_seen} pick(s) seen, "
            f"{self.picks_added} newly recorded"
        )
        if self.unresolved:
            base += f", {len(self.unresolved)} unresolved player(s)"
        return base


class SleeperDraftImporter:
    """Pulls a Sleeper draft into local state."""

    def __init__(
        self,
        settings: Settings,
        repos: Repositories,
        *,
        client: SleeperClient | None = None,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.manager = DraftStateManager(repos, settings.league)
        self._client = client

    @property
    def client(self) -> SleeperClient:
        if self._client is None:
            self._client = SleeperClient(
                self.settings.app.sources.sleeper,
                self.settings.app.sources.http,
                cache_dir=self.settings.app.paths.http_cache,
            )
        return self._client

    # -- discovery ---------------------------------------------------------

    def resolve_draft_id(self, draft_id: str | None = None) -> str:
        """Find the draft to follow, from an argument, config, or the league."""
        settings = self.settings.app.sources.sleeper
        candidate = draft_id or settings.draft_id
        if candidate:
            return candidate
        if settings.league_id:
            resolved = self.client.resolve_draft_id(settings.league_id)
            if resolved:
                return resolved
            raise DraftStateError(
                f"Sleeper league {settings.league_id} has no drafts."
            )
        raise DraftStateError(
            "No Sleeper draft to import. Pass --draft-id, or set "
            "sources.sleeper.draft_id / sources.sleeper.league_id in config/sources.yaml."
        )

    def resolve_user_slot(self, draft: SleeperDraft) -> int | None:
        """Which slot is the configured Sleeper user drafting from?"""
        username = self.settings.app.sources.sleeper.username
        if not username:
            return None
        try:
            user = self.client.fetch_user(username).data
        except SourceError as exc:
            log.warning("Could not resolve Sleeper username %r: %s", username, exc)
            return None
        user_id = str(user.get("user_id")) if isinstance(user, dict) else None
        return draft.slot_for_user(user_id) if user_id else None

    # -- import ------------------------------------------------------------

    def import_draft(
        self,
        *,
        draft_id: str | None = None,
        create_if_missing: bool = True,
        user_slot: int | None = None,
    ) -> ImportResult:
        """Fetch the draft and record any picks not already stored locally."""
        external_id = self.resolve_draft_id(draft_id)
        meta = self.client.parse_draft(self.client.fetch_draft(external_id).data)
        picks = self.client.parse_picks(self.client.fetch_draft_picks(external_id).data)

        result = ImportResult(
            draft_id=None, external_draft_id=external_id, picks_seen=len(picks), picks_added=0
        )

        local = self._local_draft(meta, external_id, create_if_missing, user_slot, result)
        result.draft_id = local.draft_id
        result.user_slot = local.user_slot

        existing = {
            pick.overall_pick for pick in self.repos.drafts.picks(local.draft_id or 0)
        }
        index = PlayerIndex(self.repos.players.all(with_source_ids=True))
        added = 0

        for pick in sorted(picks, key=lambda item: item.pick_no):
            if pick.pick_no in existing or pick.pick_no < 1:
                continue
            player_id = self._resolve_player(pick, index, result)
            try:
                self.manager.record_pick(
                    local,
                    player_id,
                    overall_pick=pick.pick_no,
                    source="sleeper",
                    keeper=pick.is_keeper,
                )
                added += 1
            except DraftStateError as exc:
                result.warnings.append(f"Pick {pick.pick_no}: {exc}")

        result.picks_added = added
        log.info("%s", result.describe())
        return result

    def _local_draft(
        self,
        meta: SleeperDraft,
        external_id: str,
        create_if_missing: bool,
        user_slot: int | None,
        result: ImportResult,
    ) -> DraftRecord:
        active = self.repos.drafts.active()
        if active is not None and active.external_id == external_id:
            return active
        if active is not None:
            result.warnings.append(
                f"Active local draft {active.draft_id} is not this Sleeper draft; "
                f"finish or delete it before importing, or it will be left alone."
            )
            raise DraftStateError(
                f"Active draft {active.draft_id} does not match Sleeper draft "
                f"{external_id}. Run 'fantasy-ai draft delete' or 'draft complete' first."
            )
        if not create_if_missing:
            raise DraftStateError(
                f"No local draft for Sleeper draft {external_id}. "
                f"Run with --create to start one."
            )

        slot = user_slot or self.resolve_user_slot(meta) or self.settings.league.draft.position
        if slot is None:
            raise DraftStateError(
                "Could not determine your draft slot. Pass --position, or set "
                "sources.sleeper.username so it can be resolved from the draft order."
            )
        if meta.teams and meta.teams != self.settings.league.teams:
            result.warnings.append(
                f"Sleeper reports {meta.teams} teams but your league config says "
                f"{self.settings.league.teams}; using Sleeper's value for this draft."
            )
        return self.manager.start(
            name=f"Sleeper {external_id}",
            user_slot=slot,
            teams=meta.teams or self.settings.league.teams,
            rounds=meta.rounds or self.settings.league.effective_rounds,
            draft_type=meta.draft_type or self.settings.league.draft.type,
            external_id=external_id,
            external_source="sleeper",
        )

    @staticmethod
    def _resolve_player(
        pick: SleeperPick, index: PlayerIndex, result: ImportResult
    ) -> str | None:
        """Map a Sleeper pick onto a canonical player id.

        Falls back to the pick's embedded name metadata, which is how a pick of a
        player we have not synced still lands on the right person.
        """
        if pick.sleeper_player_id:
            existing = index.by_source("sleeper", pick.sleeper_player_id)
            if existing:
                return existing
        name = pick.player_name
        if name:
            resolved = index.resolve(
                source="sleeper",
                source_player_id=pick.sleeper_player_id,
                name=name,
                position=(pick.metadata or {}).get("position"),
                team=(pick.metadata or {}).get("team"),
                create_missing=False,
            )
            if resolved is not None and resolved.candidates == 1:
                return resolved.player_id
        label = name or pick.sleeper_player_id or f"pick {pick.pick_no}"
        result.unresolved.append(label)
        # Record the pick anyway: knowing a slot is used matters for draft order
        # even when we cannot say who it was.
        return None
