"""Application state for the HTTP API.

Holds the settings, database, and a cached board.

**Why cache.** A full board analysis is ~0.5-1s: it scores every player, solves
a lineup per candidate for roster fit, and runs a Monte Carlo simulation. A live
draft UI polls, so recomputing per request would burn a core continuously and
add latency to every keystroke. The cache is keyed on a *state version* derived
from the draft's pick count and last-updated time, so it is invalidated exactly
when something that changes the answer changes -- never on a timer.

**Why a lock.** SQLite connections are not shareable across threads, and two
concurrent recomputes would be wasted work. One lock around analysis keeps the
single connection safe and collapses concurrent requests onto one computation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings, load_settings, validate_settings
from ..db import Database, Repositories
from ..logging_setup import get_logger
from ..models import to_iso
from ..services import AnalysisService, BoardContext

log = get_logger(__name__)


@dataclass(slots=True)
class CachedBoard:
    context: BoardContext
    state_version: str
    generated_at: datetime
    simulate: bool


class AppState:
    """Everything a request handler needs, built once at startup."""

    def __init__(
        self,
        *,
        league_path: Path | None = None,
        sources_path: Path | None = None,
        settings: Settings | None = None,
        database: Database | None = None,
    ) -> None:
        """Build application state.

        ``settings`` and ``database`` are injectable so tests can run against an
        in-memory database without touching disk. An injected database must
        allow cross-thread use for the same reason the default one does.
        """
        self.settings = settings or load_settings(
            league_path=league_path, sources_path=sources_path
        )
        self.config_warnings = validate_settings(self.settings)
        # FastAPI runs sync handlers in a threadpool, so the connection has to
        # cross threads. Every access below goes through ``self._lock``, which is
        # what makes that safe.
        self.database = database or Database(
            self.settings.app.paths.database, allow_cross_thread=True
        )
        self.database.ensure_ready()
        self.repos = Repositories(self.database)
        self._lock = threading.RLock()
        self._cache: CachedBoard | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.database.close()

    # -- services ----------------------------------------------------------

    def analysis(self) -> AnalysisService:
        return AnalysisService(self.settings, self.repos)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- state versioning --------------------------------------------------

    def state_version(self) -> str:
        """A token that changes exactly when the draft state changes.

        Derived from the active draft's id, its pick count, and its
        ``updated_at``. A board computed under one version is valid until the
        version moves.
        """
        with self._lock:
            draft = self.repos.drafts.active()
            if draft is None:
                return "no-draft"
            picks = len(self.repos.drafts.picks(draft.draft_id or 0))
            return f"{draft.draft_id}:{picks}:{to_iso(draft.updated_at)}"

    def invalidate(self) -> None:
        """Drop the cached board. Called after any state-changing operation."""
        with self._lock:
            self._cache = None

    # -- board -------------------------------------------------------------

    def board(self, *, simulate: bool = True, force: bool = False) -> CachedBoard:
        """The current analysed board, recomputing only when it has gone stale."""
        with self._lock:
            version = self.state_version()
            cached = self._cache
            if (
                not force
                and cached is not None
                and cached.state_version == version
                and cached.simulate == simulate
            ):
                return cached

            log.debug("Recomputing board (state=%s, simulate=%s)", version, simulate)
            context = self.analysis().board(simulate=simulate)
            entry = CachedBoard(
                context=context,
                state_version=version,
                generated_at=datetime.now(UTC),
                simulate=simulate,
            )
            self._cache = entry
            return entry

    def has_data(self) -> bool:
        with self._lock:
            return self.repos.players.count() > 0
