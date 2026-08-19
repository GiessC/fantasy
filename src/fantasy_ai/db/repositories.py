"""Repositories: the only place that knows SQL for a given table.

Snapshot repositories are append-only with content-hash de-duplication: writing
a record identical to the newest one for the same key is a no-op, so ``sync``
is idempotent while genuine changes still accumulate history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from ..errors import DatabaseError, DraftStateError
from ..logging_setup import get_logger
from ..models import (
    ADPRecord,
    DataFreshness,
    DraftPick,
    DraftRecord,
    InjuryRecord,
    Player,
    ProjectionRecord,
    RankingRecord,
    SeasonHistoryRecord,
    from_iso,
    to_iso,
    utcnow,
)
from ..stats import StatLine
from .database import Database, content_hash, json_dumps, json_loads

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


class PlayerRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, players: Iterable[Player]) -> int:
        """Insert or update players and their source ids. Returns rows written."""
        rows = list(players)
        if not rows:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO players (
                    player_id, full_name, normalized_name, first_name, last_name,
                    position, team, age, years_exp, status, injury_status, bye_week,
                    height, weight, college, birth_date, depth_chart_order, updated_at
                ) VALUES (
                    :player_id, :full_name, :normalized_name, :first_name, :last_name,
                    :position, :team, :age, :years_exp, :status, :injury_status, :bye_week,
                    :height, :weight, :college, :birth_date, :depth_chart_order, :updated_at
                )
                ON CONFLICT(player_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    normalized_name = excluded.normalized_name,
                    first_name = COALESCE(excluded.first_name, players.first_name),
                    last_name = COALESCE(excluded.last_name, players.last_name),
                    position = COALESCE(excluded.position, players.position),
                    team = COALESCE(excluded.team, players.team),
                    age = COALESCE(excluded.age, players.age),
                    years_exp = COALESCE(excluded.years_exp, players.years_exp),
                    status = COALESCE(excluded.status, players.status),
                    injury_status = excluded.injury_status,
                    bye_week = COALESCE(excluded.bye_week, players.bye_week),
                    height = COALESCE(excluded.height, players.height),
                    weight = COALESCE(excluded.weight, players.weight),
                    college = COALESCE(excluded.college, players.college),
                    birth_date = COALESCE(excluded.birth_date, players.birth_date),
                    depth_chart_order = excluded.depth_chart_order,
                    updated_at = excluded.updated_at
                """,
                [self._to_params(player) for player in rows],
            )
            source_links = [
                (source, str(source_id), player.player_id)
                for player in rows
                for source, source_id in player.source_ids.items()
                if source_id
            ]
            if source_links:
                connection.executemany(
                    """
                    INSERT INTO player_source_ids (source, source_player_id, player_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source, source_player_id)
                    DO UPDATE SET player_id = excluded.player_id
                    """,
                    source_links,
                )
        return len(rows)

    @staticmethod
    def _to_params(player: Player) -> dict[str, Any]:
        return {
            "player_id": player.player_id,
            "full_name": player.full_name,
            "normalized_name": player.normalized_name,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "position": player.position,
            "team": player.team,
            "age": player.age,
            "years_exp": player.years_exp,
            "status": player.status,
            "injury_status": player.injury_status,
            "bye_week": player.bye_week,
            "height": player.height,
            "weight": player.weight,
            "college": player.college,
            "birth_date": player.birth_date,
            "depth_chart_order": player.depth_chart_order,
            "updated_at": to_iso(player.updated_at),
        }

    def get(self, player_id: str) -> Player | None:
        row = self.db.query_one("SELECT * FROM players WHERE player_id = ?", (player_id,))
        if row is None:
            return None
        player = self._from_row(row)
        player.source_ids = self.source_ids_for(player_id)
        return player

    def all(
        self, *, positions: Sequence[str] | None = None, with_source_ids: bool = False
    ) -> list[Player]:
        if positions:
            placeholders = ",".join("?" * len(positions))
            rows = self.db.query(
                f"SELECT * FROM players WHERE position IN ({placeholders}) ORDER BY full_name",  # noqa: S608
                tuple(positions),
            )
        else:
            rows = self.db.query("SELECT * FROM players ORDER BY full_name")
        players = [self._from_row(row) for row in rows]
        if with_source_ids:
            mapping = self.all_source_ids()
            for player in players:
                player.source_ids = mapping.get(player.player_id, {})
        return players

    def all_source_ids(self) -> dict[str, dict[str, str]]:
        """Every source id, grouped by canonical player id (one query)."""
        rows = self.db.query("SELECT player_id, source, source_player_id FROM player_source_ids")
        mapping: dict[str, dict[str, str]] = {}
        for row in rows:
            mapping.setdefault(row["player_id"], {})[row["source"]] = row["source_player_id"]
        return mapping

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM players") or 0)

    def resolve_source_id(self, source: str, source_player_id: str) -> str | None:
        return self.db.scalar(
            "SELECT player_id FROM player_source_ids WHERE source = ? AND source_player_id = ?",
            (source, str(source_player_id)),
        )

    def link_source_id(self, source: str, source_player_id: str, player_id: str) -> None:
        self.db.execute(
            """
            INSERT INTO player_source_ids (source, source_player_id, player_id)
            VALUES (?, ?, ?)
            ON CONFLICT(source, source_player_id) DO UPDATE SET player_id = excluded.player_id
            """,
            (source, str(source_player_id), player_id),
        )

    def source_ids_for(self, player_id: str) -> dict[str, str]:
        rows = self.db.query(
            "SELECT source, source_player_id FROM player_source_ids WHERE player_id = ?",
            (player_id,),
        )
        return {row["source"]: row["source_player_id"] for row in rows}

    def find_by_normalized_name(
        self, normalized: str, *, position: str | None = None
    ) -> list[Player]:
        if position:
            rows = self.db.query(
                "SELECT * FROM players WHERE normalized_name = ? AND position = ?",
                (normalized, position),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM players WHERE normalized_name = ?", (normalized,)
            )
        return [self._from_row(row) for row in rows]

    def search(self, fragment: str, *, limit: int = 25) -> list[Player]:
        """Case-insensitive substring search over display and normalized names."""
        pattern = f"%{fragment.strip().lower()}%"
        rows = self.db.query(
            """
            SELECT * FROM players
            WHERE LOWER(full_name) LIKE ? OR normalized_name LIKE ?
            ORDER BY LENGTH(full_name), full_name
            LIMIT ?
            """,
            (pattern, pattern, limit),
        )
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Player:
        return Player(
            player_id=row["player_id"],
            full_name=row["full_name"],
            normalized_name=row["normalized_name"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            position=row["position"],
            team=row["team"],
            age=row["age"],
            years_exp=row["years_exp"],
            status=row["status"],
            injury_status=row["injury_status"],
            bye_week=row["bye_week"],
            height=row["height"],
            weight=row["weight"],
            college=row["college"],
            birth_date=row["birth_date"],
            depth_chart_order=row["depth_chart_order"],
            updated_at=from_iso(row["updated_at"]) or utcnow(),
        )


# ---------------------------------------------------------------------------
# Sync bookkeeping
# ---------------------------------------------------------------------------


class SyncRunRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, dataset: str, source: str, season: int | None) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO sync_runs (dataset, source, season, started_at, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (dataset, source, season, to_iso(utcnow())),
        )
        return int(cursor.lastrowid or 0)

    def finish(
        self,
        sync_run_id: int,
        *,
        status: str = "success",
        record_count: int = 0,
        inserted_count: int = 0,
        detail: str | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE sync_runs
               SET completed_at = ?, status = ?, record_count = ?,
                   inserted_count = ?, detail = ?
             WHERE sync_run_id = ?
            """,
            (to_iso(utcnow()), status, record_count, inserted_count, detail, sync_run_id),
        )

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )

    def freshness(self, season: int | None = None) -> list[DataFreshness]:
        """Newest snapshot time per (dataset, source), for the staleness report."""
        results: list[DataFreshness] = []
        specs = (
            ("players", "players", "updated_at", None),
            ("projections", "projections", "retrieved_at", "season"),
            ("rankings", "rankings", "retrieved_at", "season"),
            ("adp", "adp", "retrieved_at", "season"),
            ("injuries", "injuries", "retrieved_at", "season"),
        )
        for dataset, table, time_column, season_column in specs:
            # players has no source column; label each row by whichever source
            # supplied its id, so a demo-populated database does not claim Sleeper.
            source_expr = (
                "COALESCE((SELECT s.source FROM player_source_ids s "
                "WHERE s.player_id = players.player_id LIMIT 1), 'unknown')"
                if table == "players"
                else "source"
            )
            where = ""
            params: tuple = ()
            if season_column and season is not None:
                where = f"WHERE {season_column} = ?"
                params = (season,)
            rows = self.db.query(
                f"""
                SELECT {source_expr} AS source, MAX({time_column}) AS newest, COUNT(*) AS n
                FROM {table} {where}
                GROUP BY {source_expr}
                """,  # noqa: S608
                params,
            )
            if not rows:
                results.append(
                    DataFreshness(
                        dataset=dataset, source="-", season=season,
                        retrieved_at=None, record_count=0,
                    )
                )
            for row in rows:
                results.append(
                    DataFreshness(
                        dataset=dataset,
                        source=row["source"],
                        season=season,
                        retrieved_at=from_iso(row["newest"]),
                        record_count=int(row["n"]),
                    )
                )
        return results


# ---------------------------------------------------------------------------
# Snapshot repositories
# ---------------------------------------------------------------------------


class _SnapshotRepository:
    """Shared append-with-dedupe behaviour for the fact tables."""

    table: str = ""
    #: Columns that identify a logical series (a key whose newest row is "current").
    key_columns: tuple[str, ...] = ()

    def __init__(self, db: Database) -> None:
        self.db = db

    def _newest_hash_map(self) -> dict[tuple, str]:
        """Map of key tuple -> newest content hash for the whole table."""
        columns = ", ".join(self.key_columns)
        rows = self.db.query(
            f"""
            SELECT {columns}, content_hash
            FROM {self.table}
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY {", ".join(f"IFNULL({c}, '')" for c in self.key_columns)}
                        ORDER BY retrieved_at DESC, id DESC
                    ) AS rn
                    FROM {self.table}
                ) WHERE rn = 1
            )
            """  # noqa: S608
        )
        return {
            tuple("" if row[column] is None else str(row[column]) for column in self.key_columns):
            row["content_hash"]
            for row in rows
        }

    def prune_history(self, keep: int = 10) -> int:
        """Keep only the newest ``keep`` snapshots per key. Returns rows deleted."""
        if keep < 1:
            raise DatabaseError("prune_history requires keep >= 1")
        cursor = self.db.execute(
            f"""
            DELETE FROM {self.table}
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY {", ".join(f"IFNULL({c}, '')" for c in self.key_columns)}
                        ORDER BY retrieved_at DESC, id DESC
                    ) AS rn
                    FROM {self.table}
                ) WHERE rn > ?
            )
            """,  # noqa: S608
            (keep,),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def delete_source(self, source: str) -> int:
        """Delete every snapshot contributed by ``source``. Returns rows deleted."""
        cursor = self.db.execute(
            f"DELETE FROM {self.table} WHERE source = ?",  # noqa: S608
            (source,),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT COUNT(*) FROM {self.table}") or 0)  # noqa: S608


class ProjectionRepository(_SnapshotRepository):
    table = "projections"
    key_columns = ("player_id", "season", "week", "source")

    def add_many(
        self, records: Iterable[ProjectionRecord], *, sync_run_id: int | None = None
    ) -> int:
        rows = list(records)
        if not rows:
            return 0
        existing = self._newest_hash_map()
        payload: list[tuple] = []
        for record in rows:
            digest = content_hash(dict(record.stats))
            key = (
                record.player_id,
                str(record.season),
                "" if record.week is None else str(record.week),
                record.source,
            )
            if existing.get(key) == digest:
                continue
            payload.append(
                (
                    record.player_id,
                    record.season,
                    record.week,
                    record.source,
                    record.scoring_context,
                    json_dumps(dict(record.stats)),
                    digest,
                    json_dumps(record.raw),
                    sync_run_id,
                    to_iso(record.retrieved_at),
                )
            )
        if not payload:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO projections (
                    player_id, season, week, source, scoring_context,
                    stats_json, content_hash, raw_json, sync_run_id, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def latest(
        self, season: int, *, week: int | None = None, sources: Sequence[str] | None = None
    ) -> dict[str, list[ProjectionRecord]]:
        """Newest projection per (player, source), grouped by player id."""
        clauses = ["season = ?"]
        params: list[Any] = [season]
        if week is None:
            clauses.append("week IS NULL")
        else:
            clauses.append("week = ?")
            params.append(week)
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        rows = self.db.query(
            f"SELECT * FROM latest_projections WHERE {' AND '.join(clauses)}",  # noqa: S608
            tuple(params),
        )
        grouped: dict[str, list[ProjectionRecord]] = {}
        for row in rows:
            record = ProjectionRecord(
                player_id=row["player_id"],
                season=row["season"],
                week=row["week"],
                source=row["source"],
                scoring_context=row["scoring_context"],
                stats=StatLine.from_mapping(json_loads(row["stats_json"]) or {}),
                retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
                raw=json_loads(row["raw_json"]),
            )
            grouped.setdefault(record.player_id, []).append(record)
        return grouped

    def history(self, player_id: str, season: int, source: str | None = None) -> list[
        ProjectionRecord
    ]:
        clauses = ["player_id = ?", "season = ?"]
        params: list[Any] = [player_id, season]
        if source:
            clauses.append("source = ?")
            params.append(source)
        rows = self.db.query(
            f"SELECT * FROM projections WHERE {' AND '.join(clauses)} "  # noqa: S608
            "ORDER BY retrieved_at",
            tuple(params),
        )
        return [
            ProjectionRecord(
                player_id=row["player_id"],
                season=row["season"],
                week=row["week"],
                source=row["source"],
                scoring_context=row["scoring_context"],
                stats=StatLine.from_mapping(json_loads(row["stats_json"]) or {}),
                retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
            )
            for row in rows
        ]


class RankingRepository(_SnapshotRepository):
    table = "rankings"
    key_columns = ("player_id", "season", "source", "ranking_type", "scoring_format")

    def add_many(self, records: Iterable[RankingRecord], *, sync_run_id: int | None = None) -> int:
        rows = list(records)
        if not rows:
            return 0
        existing = self._newest_hash_map()
        payload: list[tuple] = []
        for record in rows:
            digest = content_hash(
                {
                    "ecr": record.ecr,
                    "position_rank": record.position_rank,
                    "best": record.best,
                    "worst": record.worst,
                    "average": record.average,
                    "stdev": record.stdev,
                    "tier": record.tier,
                    "expert_count": record.expert_count,
                }
            )
            key = (
                record.player_id,
                str(record.season),
                record.source,
                record.ranking_type,
                record.scoring_format or "",
            )
            if existing.get(key) == digest:
                continue
            payload.append(
                (
                    record.player_id, record.season, record.week, record.source,
                    record.ranking_type, record.scoring_format, record.ecr,
                    record.position_rank, record.best, record.worst, record.average,
                    record.stdev, record.tier, record.expert_count, digest,
                    json_dumps(record.raw), sync_run_id, to_iso(record.retrieved_at),
                )
            )
        if not payload:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO rankings (
                    player_id, season, week, source, ranking_type, scoring_format,
                    ecr, position_rank, best, worst, average, stdev, tier,
                    expert_count, content_hash, raw_json, sync_run_id, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def latest(
        self,
        season: int,
        *,
        ranking_type: str | None = None,
        sources: Sequence[str] | None = None,
    ) -> dict[str, list[RankingRecord]]:
        clauses = ["season = ?"]
        params: list[Any] = [season]
        if ranking_type:
            clauses.append("ranking_type = ?")
            params.append(ranking_type)
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        rows = self.db.query(
            f"SELECT * FROM latest_rankings WHERE {' AND '.join(clauses)}",  # noqa: S608
            tuple(params),
        )
        grouped: dict[str, list[RankingRecord]] = {}
        for row in rows:
            record = RankingRecord(
                player_id=row["player_id"],
                season=row["season"],
                source=row["source"],
                ecr=row["ecr"],
                position_rank=row["position_rank"],
                best=row["best"],
                worst=row["worst"],
                average=row["average"],
                stdev=row["stdev"],
                tier=row["tier"],
                ranking_type=row["ranking_type"],
                scoring_format=row["scoring_format"],
                week=row["week"],
                expert_count=row["expert_count"],
                retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
            )
            grouped.setdefault(record.player_id, []).append(record)
        return grouped


class ADPRepository(_SnapshotRepository):
    table = "adp"
    key_columns = ("player_id", "season", "source", "scoring_format")

    def add_many(self, records: Iterable[ADPRecord], *, sync_run_id: int | None = None) -> int:
        rows = list(records)
        if not rows:
            return 0
        existing = self._newest_hash_map()
        payload: list[tuple] = []
        for record in rows:
            digest = content_hash(
                {
                    "adp": record.adp,
                    "stdev": record.stdev,
                    "best": record.best,
                    "worst": record.worst,
                    "sample_size": record.sample_size,
                }
            )
            key = (
                record.player_id, str(record.season), record.source, record.scoring_format or ""
            )
            if existing.get(key) == digest:
                continue
            payload.append(
                (
                    record.player_id, record.season, record.source, record.scoring_format,
                    record.teams, record.adp, record.stdev, record.best, record.worst,
                    record.sample_size, digest, json_dumps(record.raw), sync_run_id,
                    to_iso(record.retrieved_at),
                )
            )
        if not payload:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO adp (
                    player_id, season, source, scoring_format, teams, adp, stdev,
                    best, worst, sample_size, content_hash, raw_json, sync_run_id, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def latest(self, season: int, *, sources: Sequence[str] | None = None) -> dict[
        str, list[ADPRecord]
    ]:
        clauses = ["season = ?"]
        params: list[Any] = [season]
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        rows = self.db.query(
            f"SELECT * FROM latest_adp WHERE {' AND '.join(clauses)}",  # noqa: S608
            tuple(params),
        )
        grouped: dict[str, list[ADPRecord]] = {}
        for row in rows:
            record = ADPRecord(
                player_id=row["player_id"],
                season=row["season"],
                source=row["source"],
                adp=row["adp"],
                stdev=row["stdev"],
                best=row["best"],
                worst=row["worst"],
                sample_size=row["sample_size"],
                teams=row["teams"],
                scoring_format=row["scoring_format"],
                retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
            )
            grouped.setdefault(record.player_id, []).append(record)
        return grouped

    def history(self, player_id: str, season: int) -> list[tuple[datetime | None, float]]:
        rows = self.db.query(
            "SELECT retrieved_at, adp FROM adp WHERE player_id = ? AND season = ? "
            "ORDER BY retrieved_at",
            (player_id, season),
        )
        return [(from_iso(row["retrieved_at"]), row["adp"]) for row in rows]


class InjuryRepository(_SnapshotRepository):
    table = "injuries"
    key_columns = ("player_id", "season", "source")

    def add_many(self, records: Iterable[InjuryRecord], *, sync_run_id: int | None = None) -> int:
        rows = list(records)
        if not rows:
            return 0
        existing = self._newest_hash_map()
        payload: list[tuple] = []
        for record in rows:
            digest = content_hash(
                {
                    "status": record.status,
                    "description": record.description,
                    "body_part": record.body_part,
                }
            )
            key = (record.player_id, str(record.season), record.source)
            if existing.get(key) == digest:
                continue
            payload.append(
                (
                    record.player_id, record.season, record.week, record.source,
                    record.status, record.description, record.body_part, digest,
                    json_dumps(record.raw), sync_run_id, to_iso(record.retrieved_at),
                )
            )
        if not payload:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO injuries (
                    player_id, season, week, source, status, description, body_part,
                    content_hash, raw_json, sync_run_id, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def latest(self, season: int, *, sources: Sequence[str] | None = None) -> dict[
        str, list[InjuryRecord]
    ]:
        clauses = ["season = ?"]
        params: list[Any] = [season]
        if sources:
            clauses.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        rows = self.db.query(
            f"SELECT * FROM latest_injuries WHERE {' AND '.join(clauses)}",  # noqa: S608
            tuple(params),
        )
        grouped: dict[str, list[InjuryRecord]] = {}
        for row in rows:
            record = InjuryRecord(
                player_id=row["player_id"],
                season=row["season"],
                source=row["source"],
                status=row["status"],
                description=row["description"],
                body_part=row["body_part"],
                week=row["week"],
                retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
            )
            grouped.setdefault(record.player_id, []).append(record)
        return grouped


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------


class PlayerHistoryRepository(_SnapshotRepository):
    """Completed-season results, append-only like every other fact table."""

    table = "player_history"
    key_columns = ("player_id", "season", "source")

    def add_many(
        self, records: Iterable[SeasonHistoryRecord], *, sync_run_id: int | None = None
    ) -> int:
        rows = list(records)
        if not rows:
            return 0
        existing = self._newest_hash_map()
        payload: list[tuple] = []
        for record in rows:
            digest = content_hash(
                {
                    "games_played": record.games_played,
                    "games_possible": record.games_possible,
                    "fantasy_points": record.fantasy_points,
                    "points_per_game": record.points_per_game,
                    "adp": record.adp,
                }
            )
            key = (record.player_id, str(record.season), record.source)
            if existing.get(key) == digest:
                continue
            payload.append(
                (
                    record.player_id, record.season, record.source,
                    record.games_played, record.games_possible, record.fantasy_points,
                    record.points_per_game, record.adp, record.scoring_format,
                    digest, json_dumps(record.raw), sync_run_id,
                    to_iso(record.retrieved_at),
                )
            )
        if not payload:
            return 0
        with self.db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO player_history (
                    player_id, season, source, games_played, games_possible,
                    fantasy_points, points_per_game, adp, scoring_format,
                    content_hash, raw_json, sync_run_id, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def by_player(self, *, source: str | None = None) -> dict[str, list[SeasonHistoryRecord]]:
        """Newest season first, per player, from the latest snapshot of each."""
        sql = "SELECT * FROM latest_player_history"
        params: tuple = ()
        if source is not None:
            sql += " WHERE source = ?"
            params = (source,)
        sql += " ORDER BY player_id, season DESC"
        grouped: dict[str, list[SeasonHistoryRecord]] = {}
        for row in self.db.query(sql, params):
            grouped.setdefault(str(row["player_id"]), []).append(
                SeasonHistoryRecord(
                    player_id=str(row["player_id"]),
                    season=int(row["season"]),
                    source=str(row["source"]),
                    games_played=row["games_played"],
                    games_possible=row["games_possible"],
                    fantasy_points=row["fantasy_points"],
                    points_per_game=row["points_per_game"],
                    adp=row["adp"],
                    scoring_format=row["scoring_format"],
                    retrieved_at=from_iso(row["retrieved_at"]) or utcnow(),
                )
            )
        return grouped


class DraftRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, draft: DraftRecord) -> DraftRecord:
        cursor = self.db.execute(
            """
            INSERT INTO drafts (
                name, league_name, season, teams, rounds, draft_type, user_slot,
                status, external_id, external_source, settings_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.name, draft.league_name, draft.season, draft.teams, draft.rounds,
                draft.draft_type, draft.user_slot, draft.status, draft.external_id,
                draft.external_source, json_dumps(draft.settings),
                to_iso(draft.created_at), to_iso(draft.updated_at),
            ),
        )
        draft.draft_id = int(cursor.lastrowid or 0)
        return draft

    def get(self, draft_id: int) -> DraftRecord | None:
        row = self.db.query_one("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))
        return self._from_row(row) if row else None

    def active(self) -> DraftRecord | None:
        row = self.db.query_one(
            "SELECT * FROM drafts WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
        )
        return self._from_row(row) if row else None

    def list_all(self, limit: int = 25) -> list[DraftRecord]:
        rows = self.db.query(
            "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [self._from_row(row) for row in rows]

    def set_status(self, draft_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE draft_id = ?",
            (status, to_iso(utcnow()), draft_id),
        )

    def delete(self, draft_id: int) -> None:
        self.db.execute("DELETE FROM drafts WHERE draft_id = ?", (draft_id,))

    def add_pick(self, draft_id: int, pick: DraftPick) -> DraftPick:
        try:
            cursor = self.db.execute(
                """
                INSERT INTO draft_picks (
                    draft_id, overall_pick, round_number, slot, team_index,
                    player_id, is_user, keeper, auction_price, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id, pick.overall_pick, pick.round_number, pick.slot,
                    pick.team_index, pick.player_id, int(pick.is_user), int(pick.keeper),
                    pick.auction_price, pick.source, to_iso(pick.created_at),
                ),
            )
        except DatabaseError as exc:
            message = str(exc)
            if "idx_draft_picks_player" in message or "draft_picks.player_id" in message:
                raise DraftStateError(
                    f"Player {pick.player_id} has already been drafted in this draft."
                ) from exc
            if "draft_picks.overall_pick" in message or "overall_pick" in message:
                raise DraftStateError(
                    f"Pick {pick.overall_pick} has already been recorded."
                ) from exc
            raise
        pick.pick_id = int(cursor.lastrowid or 0)
        self.db.execute(
            "UPDATE drafts SET updated_at = ? WHERE draft_id = ?", (to_iso(utcnow()), draft_id)
        )
        return pick

    def picks(self, draft_id: int) -> list[DraftPick]:
        rows = self.db.query(
            "SELECT * FROM draft_picks WHERE draft_id = ? ORDER BY overall_pick", (draft_id,)
        )
        return [
            DraftPick(
                pick_id=row["pick_id"],
                overall_pick=row["overall_pick"],
                round_number=row["round_number"],
                slot=row["slot"],
                team_index=row["team_index"],
                player_id=row["player_id"],
                is_user=bool(row["is_user"]),
                keeper=bool(row["keeper"]),
                auction_price=row["auction_price"],
                source=row["source"],
                created_at=from_iso(row["created_at"]) or utcnow(),
            )
            for row in rows
        ]

    def remove_last_pick(self, draft_id: int) -> DraftPick | None:
        row = self.db.query_one(
            "SELECT * FROM draft_picks WHERE draft_id = ? ORDER BY overall_pick DESC LIMIT 1",
            (draft_id,),
        )
        if row is None:
            return None
        self.db.execute("DELETE FROM draft_picks WHERE pick_id = ?", (row["pick_id"],))
        self.db.execute(
            "UPDATE drafts SET updated_at = ? WHERE draft_id = ?", (to_iso(utcnow()), draft_id)
        )
        return DraftPick(
            pick_id=row["pick_id"],
            overall_pick=row["overall_pick"],
            round_number=row["round_number"],
            slot=row["slot"],
            team_index=row["team_index"],
            player_id=row["player_id"],
            is_user=bool(row["is_user"]),
            keeper=bool(row["keeper"]),
            auction_price=row["auction_price"],
            source=row["source"],
            created_at=from_iso(row["created_at"]) or utcnow(),
        )

    def clear_picks(self, draft_id: int) -> int:
        cursor = self.db.execute("DELETE FROM draft_picks WHERE draft_id = ?", (draft_id,))
        return cursor.rowcount or 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DraftRecord:
        return DraftRecord(
            draft_id=row["draft_id"],
            name=row["name"],
            league_name=row["league_name"],
            season=row["season"],
            teams=row["teams"],
            rounds=row["rounds"],
            draft_type=row["draft_type"],
            user_slot=row["user_slot"],
            status=row["status"],
            external_id=row["external_id"],
            external_source=row["external_source"],
            settings=json_loads(row["settings_json"]) or {},
            created_at=from_iso(row["created_at"]) or utcnow(),
            updated_at=from_iso(row["updated_at"]) or utcnow(),
        )


class Repositories:
    """Convenience bundle so callers pass one object around."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.players = PlayerRepository(db)
        self.projections = ProjectionRepository(db)
        self.rankings = RankingRepository(db)
        self.adp = ADPRepository(db)
        self.injuries = InjuryRepository(db)
        self.history = PlayerHistoryRepository(db)
        self.sync_runs = SyncRunRepository(db)
        self.drafts = DraftRepository(db)

    def sources_present(self) -> dict[str, int]:
        """Every source with stored facts, and how many rows each contributed."""
        counts: dict[str, int] = {}
        for table in ("projections", "rankings", "adp", "injuries", "player_history"):
            rows = self.db.query(
                f"SELECT source, COUNT(*) AS n FROM {table} GROUP BY source"  # noqa: S608
            )
            for row in rows:
                counts[str(row["source"])] = counts.get(str(row["source"]), 0) + int(row["n"])
        return counts

    def delete_source(self, source: str) -> dict[str, int]:
        """Remove everything a source contributed, including players only it knew.

        Demo data is the motivating case: it mints its own players, so deleting
        its projections alone would leave invented names sitting on the board
        with no numbers behind them. A player is removed only when no source id
        and no fact row remains, so a real player who happened to be described
        by this source as well survives.
        """
        removed: dict[str, int] = {}
        for name in ("projections", "rankings", "adp", "injuries", "history"):
            count = getattr(self, name).delete_source(source)
            if count:
                removed[name] = count

        cursor = self.db.execute("DELETE FROM player_source_ids WHERE source = ?", (source,))
        if cursor.rowcount and cursor.rowcount > 0:
            removed["player_source_ids"] = cursor.rowcount

        cursor = self.db.execute(
            """
            DELETE FROM players
            WHERE player_id NOT IN (SELECT player_id FROM player_source_ids)
              AND player_id NOT IN (SELECT player_id FROM projections)
              AND player_id NOT IN (SELECT player_id FROM rankings)
              AND player_id NOT IN (SELECT player_id FROM adp)
              AND player_id NOT IN (SELECT player_id FROM injuries)
              AND player_id NOT IN (SELECT player_id FROM player_history)
            """
        )
        if cursor.rowcount and cursor.rowcount > 0:
            removed["players"] = cursor.rowcount

        cursor = self.db.execute("DELETE FROM sync_runs WHERE source = ?", (source,))
        if cursor.rowcount and cursor.rowcount > 0:
            removed["sync_runs"] = cursor.rowcount
        return removed
