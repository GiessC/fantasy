"""SQLite connection management and migrations.

Deliberately no ORM.  The schema is small, the queries are analytical, and
``sqlite3`` with row factories keeps the data layer transparent -- which matters
because a stated goal is that every number is explainable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import DatabaseError
from ..logging_setup import get_logger
from .schema import LATEST_VERSION, MIGRATIONS

log = get_logger(__name__)

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def content_hash(payload: Any) -> str:
    """Stable hash of a record's meaningful content.

    Used to make syncing idempotent: a snapshot identical to the newest one for
    the same key is skipped rather than appended, so re-running ``sync`` does
    not inflate history.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


class Database:
    """A connection to the local SQLite database."""

    def __init__(
        self,
        path: Path | str,
        *,
        create_parents: bool = True,
        allow_cross_thread: bool = False,
    ) -> None:
        """Open a database handle.

        ``allow_cross_thread`` relaxes sqlite3's same-thread check. It exists for
        the HTTP API, where FastAPI runs sync handlers in a threadpool, and it is
        only safe because that layer serialises every database access behind a
        single lock. The CLI keeps the strict default.
        """
        self.path = Path(path)
        if self.path.name != ":memory:" and create_parents:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.allow_cross_thread = allow_cross_thread
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect(self) -> sqlite3.Connection:
        target = ":memory:" if self.path.name == ":memory:" else str(self.path)
        try:
            connection = sqlite3.connect(
                target,
                isolation_level=None,
                check_same_thread=not self.allow_cross_thread,
            )
        except sqlite3.Error as exc:  # pragma: no cover - depends on filesystem
            raise DatabaseError(f"Cannot open database {target}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside a single transaction, rolling back on error."""
        connection = self.connection
        connection.execute("BEGIN")
        try:
            yield connection
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    # -- queries -----------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        try:
            return self.connection.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Query failed: {exc}\nSQL: {sql.strip()[:400]}") from exc

    def executemany(self, sql: str, seq: list[tuple] | list[dict]) -> sqlite3.Cursor:
        try:
            return self.connection.executemany(sql, seq)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Bulk query failed: {exc}\nSQL: {sql.strip()[:400]}") from exc

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # -- migrations --------------------------------------------------------

    @property
    def version(self) -> int:
        self.connection.execute(_MIGRATION_TABLE)
        value = self.scalar("SELECT MAX(version) FROM schema_migrations")
        return int(value) if value is not None else 0

    def migrate(self) -> list[int]:
        """Apply pending migrations. Returns the versions applied."""
        self.connection.execute(_MIGRATION_TABLE)
        current = self.version
        applied: list[int] = []

        for migration in sorted(MIGRATIONS, key=lambda m: m.version):
            if migration.version <= current:
                continue
            log.debug("Applying migration %s (%s)", migration.version, migration.name)
            with self.transaction() as connection:
                for statement in migration.statements:
                    try:
                        connection.execute(statement)
                    except sqlite3.Error as exc:
                        raise DatabaseError(
                            f"Migration {migration.version} ({migration.name}) failed: {exc}\n"
                            f"Statement: {statement.strip()[:400]}"
                        ) from exc
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (migration.version, migration.name),
                )
            applied.append(migration.version)

        if applied:
            log.info("Applied %d migration(s): %s", len(applied), applied)
        return applied

    def ensure_ready(self) -> None:
        """Migrate if needed; raise if the database is newer than this code."""
        current = self.version
        if current > LATEST_VERSION:
            raise DatabaseError(
                f"Database schema version {current} is newer than this build supports "
                f"({LATEST_VERSION}). Upgrade fantasy-ai or point at a different database."
            )
        self.migrate()

    # -- introspection -----------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        counts: dict[str, int] = {}
        for row in rows:
            table = row["name"]
            counts[table] = int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0)  # noqa: S608
        return counts

    def vacuum(self) -> None:
        self.connection.execute("VACUUM")


def open_database(path: Path | str, *, migrate: bool = True) -> Database:
    """Open (and by default migrate) a database."""
    database = Database(path)
    if migrate:
        database.ensure_ready()
    return database


def json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
