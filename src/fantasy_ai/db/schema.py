"""SQLite schema, expressed as an ordered list of migrations.

Each migration is applied once, in order, inside a transaction, and recorded in
``schema_migrations``.  To evolve the schema, append a new :class:`Migration`;
never edit an existing one, because databases in the wild have already applied
it.

Snapshot philosophy: the fact tables (``projections``, ``rankings``, ``adp``,
``injuries``) are append-only.  Rows are never updated, so ADP movement and
projection revisions stay queryable.  "Current" values come from
``latest_*`` views that pick the newest ``retrieved_at`` per key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


_V1 = (
    # -- identity ----------------------------------------------------------
    """
    CREATE TABLE players (
        player_id          TEXT PRIMARY KEY,
        full_name          TEXT NOT NULL,
        normalized_name    TEXT NOT NULL,
        first_name         TEXT,
        last_name          TEXT,
        position           TEXT,
        team               TEXT,
        age                REAL,
        years_exp          INTEGER,
        status             TEXT,
        injury_status      TEXT,
        bye_week           INTEGER,
        height             TEXT,
        weight             TEXT,
        college            TEXT,
        birth_date         TEXT,
        depth_chart_order  INTEGER,
        updated_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_players_normalized ON players (normalized_name)",
    "CREATE INDEX idx_players_position ON players (position)",
    "CREATE INDEX idx_players_team ON players (team)",
    """
    CREATE TABLE player_source_ids (
        source            TEXT NOT NULL,
        source_player_id  TEXT NOT NULL,
        player_id         TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        PRIMARY KEY (source, source_player_id)
    )
    """,
    "CREATE INDEX idx_source_ids_player ON player_source_ids (player_id)",
    # -- sync bookkeeping --------------------------------------------------
    """
    CREATE TABLE sync_runs (
        sync_run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset       TEXT NOT NULL,
        source        TEXT NOT NULL,
        season        INTEGER,
        started_at    TEXT NOT NULL,
        completed_at  TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        record_count  INTEGER NOT NULL DEFAULT 0,
        inserted_count INTEGER NOT NULL DEFAULT 0,
        detail        TEXT
    )
    """,
    "CREATE INDEX idx_sync_runs_dataset ON sync_runs (dataset, source, season, started_at DESC)",
    # -- facts (append-only snapshots) -------------------------------------
    """
    CREATE TABLE projections (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id     TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        season        INTEGER NOT NULL,
        week          INTEGER,
        source        TEXT NOT NULL,
        scoring_context TEXT,
        stats_json    TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        raw_json      TEXT,
        sync_run_id   INTEGER REFERENCES sync_runs(sync_run_id),
        retrieved_at  TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_projections_key
        ON projections (player_id, season, week, source, retrieved_at DESC)
    """,
    """
    CREATE TABLE rankings (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id      TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        season         INTEGER NOT NULL,
        week           INTEGER,
        source         TEXT NOT NULL,
        ranking_type   TEXT NOT NULL DEFAULT 'DRAFT',
        scoring_format TEXT,
        ecr            REAL,
        position_rank  INTEGER,
        best           REAL,
        worst          REAL,
        average        REAL,
        stdev          REAL,
        tier           INTEGER,
        expert_count   INTEGER,
        content_hash   TEXT NOT NULL,
        raw_json       TEXT,
        sync_run_id    INTEGER REFERENCES sync_runs(sync_run_id),
        retrieved_at   TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_rankings_key
        ON rankings (player_id, season, source, ranking_type, scoring_format, retrieved_at DESC)
    """,
    """
    CREATE TABLE adp (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id      TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        season         INTEGER NOT NULL,
        source         TEXT NOT NULL,
        scoring_format TEXT,
        teams          INTEGER,
        adp            REAL NOT NULL,
        stdev          REAL,
        best           REAL,
        worst          REAL,
        sample_size    INTEGER,
        content_hash   TEXT NOT NULL,
        raw_json       TEXT,
        sync_run_id    INTEGER REFERENCES sync_runs(sync_run_id),
        retrieved_at   TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_adp_key
        ON adp (player_id, season, source, scoring_format, retrieved_at DESC)
    """,
    """
    CREATE TABLE injuries (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id     TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        season        INTEGER NOT NULL,
        week          INTEGER,
        source        TEXT NOT NULL,
        status        TEXT,
        description   TEXT,
        body_part     TEXT,
        content_hash  TEXT NOT NULL,
        raw_json      TEXT,
        sync_run_id   INTEGER REFERENCES sync_runs(sync_run_id),
        retrieved_at  TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_injuries_key
        ON injuries (player_id, season, source, retrieved_at DESC)
    """,
    # -- draft state -------------------------------------------------------
    """
    CREATE TABLE drafts (
        draft_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        league_name     TEXT,
        season          INTEGER NOT NULL,
        teams           INTEGER NOT NULL,
        rounds          INTEGER NOT NULL,
        draft_type      TEXT NOT NULL,
        user_slot       INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active',
        external_id     TEXT,
        external_source TEXT,
        settings_json   TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE draft_picks (
        pick_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id      INTEGER NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
        overall_pick  INTEGER NOT NULL,
        round_number  INTEGER NOT NULL,
        slot          INTEGER NOT NULL,
        team_index    INTEGER NOT NULL,
        player_id     TEXT REFERENCES players(player_id) ON DELETE SET NULL,
        is_user       INTEGER NOT NULL DEFAULT 0,
        keeper        INTEGER NOT NULL DEFAULT 0,
        auction_price REAL,
        source        TEXT NOT NULL DEFAULT 'manual',
        created_at    TEXT NOT NULL,
        UNIQUE (draft_id, overall_pick)
    )
    """,
    """
    CREATE UNIQUE INDEX idx_draft_picks_player
        ON draft_picks (draft_id, player_id) WHERE player_id IS NOT NULL
    """,
    "CREATE INDEX idx_draft_picks_draft ON draft_picks (draft_id, overall_pick)",
    # -- convenience views -------------------------------------------------
    """
    CREATE VIEW latest_projections AS
    SELECT p.*
    FROM projections p
    JOIN (
        SELECT player_id, season, IFNULL(week, -1) AS wk, source,
               MAX(retrieved_at) AS newest
        FROM projections
        GROUP BY player_id, season, IFNULL(week, -1), source
    ) newest
      ON p.player_id = newest.player_id
     AND p.season = newest.season
     AND IFNULL(p.week, -1) = newest.wk
     AND p.source = newest.source
     AND p.retrieved_at = newest.newest
    """,
    """
    CREATE VIEW latest_rankings AS
    SELECT r.*
    FROM rankings r
    JOIN (
        SELECT player_id, season, source, ranking_type,
               IFNULL(scoring_format, '') AS fmt, MAX(retrieved_at) AS newest
        FROM rankings
        GROUP BY player_id, season, source, ranking_type, IFNULL(scoring_format, '')
    ) newest
      ON r.player_id = newest.player_id
     AND r.season = newest.season
     AND r.source = newest.source
     AND r.ranking_type = newest.ranking_type
     AND IFNULL(r.scoring_format, '') = newest.fmt
     AND r.retrieved_at = newest.newest
    """,
    """
    CREATE VIEW latest_adp AS
    SELECT a.*
    FROM adp a
    JOIN (
        SELECT player_id, season, source, IFNULL(scoring_format, '') AS fmt,
               MAX(retrieved_at) AS newest
        FROM adp
        GROUP BY player_id, season, source, IFNULL(scoring_format, '')
    ) newest
      ON a.player_id = newest.player_id
     AND a.season = newest.season
     AND a.source = newest.source
     AND IFNULL(a.scoring_format, '') = newest.fmt
     AND a.retrieved_at = newest.newest
    """,
    """
    CREATE VIEW latest_injuries AS
    SELECT i.*
    FROM injuries i
    JOIN (
        SELECT player_id, season, source, MAX(retrieved_at) AS newest
        FROM injuries
        GROUP BY player_id, season, source
    ) newest
      ON i.player_id = newest.player_id
     AND i.season = newest.season
     AND i.source = newest.source
     AND i.retrieved_at = newest.newest
    """,
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial_schema", statements=_V1),
)

#: The schema version a fresh database is created at.
LATEST_VERSION: int = max(migration.version for migration in MIGRATIONS)
