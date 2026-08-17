"""Shared CLI state.

Configuration and the database are opened once per invocation and lazily, so
``fantasy-ai --help`` and ``validate-config`` never touch the filesystem more
than they need to, and a command that only reads config does not create a
database file as a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, load_settings
from ..db import Database, Repositories
from ..logging_setup import configure_logging
from ..services import AnalysisService, SyncService


@dataclass(slots=True)
class CLIContext:
    """Everything a command needs, built on demand."""

    league_path: Path | None = None
    sources_path: Path | None = None
    log_level: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    _settings: Settings | None = None
    _database: Database | None = None
    _repos: Repositories | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(
                league_path=self.league_path,
                sources_path=self.sources_path,
                overrides=self.overrides or None,
            )
            configure_logging(self.log_level or self._settings.app.log_level)
        return self._settings

    @property
    def database(self) -> Database:
        if self._database is None:
            self._database = Database(self.settings.app.paths.database)
            self._database.ensure_ready()
        return self._database

    @property
    def repos(self) -> Repositories:
        if self._repos is None:
            self._repos = Repositories(self.database)
        return self._repos

    def analysis(self) -> AnalysisService:
        return AnalysisService(self.settings, self.repos)

    def sync(self) -> SyncService:
        return SyncService(self.settings, self.repos)

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None
            self._repos = None
