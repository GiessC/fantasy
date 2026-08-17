"""Data synchronisation.

The one place where sources, normalization, and persistence meet.  Each ``sync_*``
method follows the same shape:

    fetch -> parse -> resolve identity -> persist snapshot -> record the run

Every method is **idempotent**: repository writes are content-hash de-duplicated,
so running ``sync`` twice in a row produces the same database, while a genuine
change (ADP moved, a projection was revised) appends a new snapshot and keeps
the old one.

Identity resolution happens here rather than in the adapters, because it needs
the canonical player table.  Sleeper is synced first when possible: its ids are
the backbone every other source attaches to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import Settings
from ..db import Repositories
from ..errors import SourceError
from ..logging_setup import get_logger
from ..models import (
    ADPRecord,
    ProjectionRecord,
    RankingRecord,
    utcnow,
)
from ..normalization.identity import PlayerIndex, ResolutionStats
from ..sources import csv_import, demo
from ..sources.fantasypros import FantasyProsClient, FantasyProsRow, ParseReport
from ..sources.http import SyncReport
from ..sources.sleeper import SleeperClient

log = get_logger(__name__)


@dataclass(slots=True)
class SyncContext:
    """Shared state for a batch of syncs, so the player index is built once."""

    index: PlayerIndex
    resolution: ResolutionStats

    def flush(self, repos: Repositories) -> int:
        """Persist any players minted during resolution."""
        return repos.players.upsert(self.index.players())


class SyncService:
    """Fetches from configured sources and persists canonical snapshots."""

    def __init__(
        self,
        settings: Settings,
        repos: Repositories,
        *,
        sleeper: SleeperClient | None = None,
        fantasypros: FantasyProsClient | None = None,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self._sleeper = sleeper
        self._fantasypros = fantasypros

    # -- clients -----------------------------------------------------------

    @property
    def sleeper(self) -> SleeperClient:
        if self._sleeper is None:
            self._sleeper = SleeperClient(
                self.settings.app.sources.sleeper,
                self.settings.app.sources.http,
                cache_dir=self.settings.app.paths.http_cache,
            )
        return self._sleeper

    @property
    def fantasypros(self) -> FantasyProsClient:
        if self._fantasypros is None:
            self._fantasypros = FantasyProsClient(
                self.settings.app.sources.fantasypros,
                self.settings.app.sources.http,
                cache_dir=self.settings.app.paths.http_cache,
            )
        return self._fantasypros

    def close(self) -> None:
        for client in (self._sleeper, self._fantasypros):
            if client is not None:
                client.close()

    # -- shared helpers ----------------------------------------------------

    def context(self) -> SyncContext:
        """Load the canonical player table into an identity index."""
        players = self.repos.players.all(with_source_ids=True)
        return SyncContext(index=PlayerIndex(players), resolution=ResolutionStats())

    @property
    def scoring_bucket(self) -> str:
        compiled = self.settings.league.scoring.compile()
        return self.fantasypros.scoring_parameter(compiled.ppr_value)

    def _run(self, dataset: str, source: str, season: int | None) -> int:
        return self.repos.sync_runs.start(dataset, source, season)

    # -- players -----------------------------------------------------------

    def sync_players(self, *, force_refresh: bool = False) -> SyncReport:
        """Canonical player metadata and identity from Sleeper."""
        season = self.settings.league.season
        run_id = self._run("players", "sleeper", season)
        report = SyncReport(dataset="players", source="sleeper", season=season)
        try:
            response = self.sleeper.fetch_players(force_refresh=force_refresh)
            players, injuries = self.sleeper.parse_players(
                response.data, retrieved_at=response.retrieved_at
            )
            report.fetched = len(players)
            report.from_cache = response.from_cache
            report.stale = response.stale
            report.retrieved_at = response.retrieved_at

            report.written = self.repos.players.upsert(players)

            for injury in injuries:
                injury.season = season
            report.skipped = len(injuries) - self.repos.injuries.add_many(
                injuries, sync_run_id=run_id
            )
            self.repos.sync_runs.finish(
                run_id,
                record_count=report.fetched,
                inserted_count=report.written,
                detail=f"{len(injuries)} injury designation(s)",
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    # -- FantasyPros datasets ---------------------------------------------

    def sync_rankings(
        self,
        *,
        ranking_type: str = "DRAFT",
        force_refresh: bool = False,
        context: SyncContext | None = None,
        verbose: bool = False,
    ) -> SyncReport:
        """Expert consensus rankings, including the dispersion fields."""
        season = self.settings.league.season
        scoring = self.scoring_bucket
        run_id = self._run("rankings", "fantasypros", season)
        report = SyncReport(dataset=f"rankings[{ranking_type}]", source="fantasypros",
                            season=season)
        ctx = context or self.context()

        try:
            response = self.fantasypros.fetch_rankings(
                season, scoring=scoring, ranking_type=ranking_type, force_refresh=force_refresh
            )
            rows, parse_report = self.fantasypros.parse_rows(response.data)
            report.fetched = len(rows)
            report.from_cache = response.from_cache
            report.stale = response.stale
            report.retrieved_at = response.retrieved_at
            if verbose:
                report.warnings.extend(parse_report.describe())

            records = [
                RankingRecord(
                    player_id=player_id,
                    season=season,
                    source="fantasypros",
                    ecr=row.ecr,
                    position_rank=row.position_rank,
                    best=row.best,
                    worst=row.worst,
                    average=row.average,
                    stdev=row.stdev,
                    tier=row.tier,
                    ranking_type=ranking_type,
                    scoring_format=scoring,
                    expert_count=row.expert_count,
                    retrieved_at=response.retrieved_at,
                )
                for row, player_id in self._resolve_rows(rows, ctx, "fantasypros")
            ]
            # Bye weeks must be applied to the index *before* it is flushed,
            # or they are only ever set in memory and never persisted.
            self._apply_bye_weeks(rows, ctx)
            ctx.flush(self.repos)
            report.written = self.repos.rankings.add_many(records, sync_run_id=run_id)
            report.skipped = len(records) - report.written

            self.repos.sync_runs.finish(
                run_id, record_count=report.fetched, inserted_count=report.written,
                detail=ctx.resolution.summary(),
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    def sync_adp(
        self,
        *,
        force_refresh: bool = False,
        context: SyncContext | None = None,
        verbose: bool = False,
    ) -> SyncReport:
        """Average draft position from FantasyPros' ADP consensus."""
        season = self.settings.league.season
        scoring = self.scoring_bucket
        run_id = self._run("adp", "fantasypros", season)
        report = SyncReport(dataset="adp", source="fantasypros", season=season)
        ctx = context or self.context()

        try:
            response = self.fantasypros.fetch_rankings(
                season, scoring=scoring, ranking_type="ADP", force_refresh=force_refresh
            )
            rows, parse_report = self.fantasypros.parse_rows(response.data)
            report.fetched = len(rows)
            report.from_cache = response.from_cache
            report.stale = response.stale
            report.retrieved_at = response.retrieved_at
            if verbose:
                report.warnings.extend(parse_report.describe())

            records = []
            for row, player_id in self._resolve_rows(rows, ctx, "fantasypros"):
                value = row.adp if row.adp is not None else row.ecr
                if value is None:
                    continue
                records.append(
                    ADPRecord(
                        player_id=player_id,
                        season=season,
                        source="fantasypros",
                        adp=value,
                        stdev=row.stdev,
                        best=row.best,
                        worst=row.worst,
                        sample_size=row.sample_size,
                        teams=self.settings.league.teams,
                        scoring_format=scoring,
                        retrieved_at=response.retrieved_at,
                    )
                )
            ctx.flush(self.repos)
            report.written = self.repos.adp.add_many(records, sync_run_id=run_id)
            report.skipped = len(records) - report.written
            self.repos.sync_runs.finish(
                run_id, record_count=report.fetched, inserted_count=report.written,
                detail=ctx.resolution.summary(),
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    def sync_projections(
        self,
        *,
        positions: Sequence[str] | None = None,
        force_refresh: bool = False,
        context: SyncContext | None = None,
        verbose: bool = False,
    ) -> SyncReport:
        """Statistical projections, requested per position."""
        season = self.settings.league.season
        scoring = self.scoring_bucket
        week = self.settings.app.sources.fantasypros.projection_week
        target_positions = list(positions or self.settings.app.sources.fantasypros.positions)
        run_id = self._run("projections", "fantasypros", season)
        report = SyncReport(dataset="projections", source="fantasypros", season=season)
        ctx = context or self.context()

        try:
            all_records: list[ProjectionRecord] = []
            merged_report = ParseReport()
            for position in target_positions:
                response = self.fantasypros.fetch_projections(
                    season, position=position, scoring=scoring, week=week,
                    force_refresh=force_refresh,
                )
                rows, parse_report = self.fantasypros.parse_rows(
                    response.data, report=merged_report
                )
                report.fetched += len(rows)
                report.from_cache = report.from_cache or response.from_cache
                report.stale = report.stale or response.stale
                report.retrieved_at = response.retrieved_at

                for row, player_id in self._resolve_rows(
                    rows, ctx, "fantasypros", default_position=position
                ):
                    if not row.stats:
                        continue
                    all_records.append(
                        ProjectionRecord(
                            player_id=player_id,
                            season=season,
                            source="fantasypros",
                            stats=row.stats,
                            week=week or None,
                            scoring_context=scoring,
                            retrieved_at=response.retrieved_at,
                        )
                    )
                _ = parse_report

            if verbose:
                report.warnings.extend(merged_report.describe())
            report.unmapped_fields = dict(merged_report.unmapped_stat_fields)

            ctx.flush(self.repos)
            report.written = self.repos.projections.add_many(all_records, sync_run_id=run_id)
            report.skipped = len(all_records) - report.written
            self.repos.sync_runs.finish(
                run_id, record_count=report.fetched, inserted_count=report.written,
                detail=ctx.resolution.summary(),
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    def sync_injuries(self, *, force_refresh: bool = False) -> SyncReport:
        """Injury designations.

        Sleeper carries a current designation on every player, so this reuses
        the player payload rather than calling a separate endpoint.
        """
        season = self.settings.league.season
        run_id = self._run("injuries", "sleeper", season)
        report = SyncReport(dataset="injuries", source="sleeper", season=season)
        try:
            response = self.sleeper.fetch_players(force_refresh=force_refresh)
            _, injuries = self.sleeper.parse_players(
                response.data, retrieved_at=response.retrieved_at
            )
            for injury in injuries:
                injury.season = season
            known = {player.player_id for player in self.repos.players.all()}
            relevant = [injury for injury in injuries if injury.player_id in known]
            report.fetched = len(relevant)
            report.from_cache = response.from_cache
            report.stale = response.stale
            report.retrieved_at = response.retrieved_at
            report.written = self.repos.injuries.add_many(relevant, sync_run_id=run_id)
            report.skipped = report.fetched - report.written
            self.repos.sync_runs.finish(
                run_id, record_count=report.fetched, inserted_count=report.written
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    # -- CSV ---------------------------------------------------------------

    def sync_from_csv(
        self,
        path: Path,
        *,
        dataset: str = "auto",
        position_hint: str | None = None,
        context: SyncContext | None = None,
        verbose: bool = False,
    ) -> SyncReport:
        """Ingest a FantasyPros CSV export.

        ``dataset="auto"`` writes whichever of projections / rankings / ADP the
        file actually contains, which is usually all a user wants.
        """
        season = self.settings.league.season
        run_id = self._run(f"csv[{dataset}]", "csv", season)
        report = SyncReport(dataset=f"csv[{dataset}]", source="csv", season=season)
        ctx = context or self.context()
        retrieved = utcnow()

        try:
            rows, csv_report = csv_import.read_rows(path, position_hint=position_hint)
            report.fetched = len(rows)
            report.retrieved_at = retrieved
            if verbose:
                report.warnings.extend(csv_report.describe())

            projections: list[ProjectionRecord] = []
            rankings: list[RankingRecord] = []
            adp_records: list[ADPRecord] = []

            for row in rows:
                resolved = ctx.index.resolve(
                    source="csv",
                    source_player_id=None,
                    name=row.name,
                    position=row.position,
                    team=row.team,
                )
                if resolved is None:
                    continue
                ctx.resolution.record(resolved, row.name)
                player_id = resolved.player_id

                if row.stats and dataset in {"auto", "projections"}:
                    projections.append(
                        ProjectionRecord(
                            player_id=player_id, season=season, source="csv",
                            stats=row.stats, scoring_context="csv-import",
                            retrieved_at=retrieved,
                        )
                    )
                if dataset in {"auto", "rankings"} and row.rank is not None:
                    rankings.append(
                        RankingRecord(
                            player_id=player_id, season=season, source="csv",
                            ecr=row.rank, best=row.best, worst=row.worst,
                            average=row.average, stdev=row.stdev, tier=row.tier,
                            ranking_type="DRAFT", retrieved_at=retrieved,
                        )
                    )
                if dataset in {"auto", "adp"} and row.adp is not None:
                    adp_records.append(
                        ADPRecord(
                            player_id=player_id, season=season, source="csv",
                            adp=row.adp, stdev=row.stdev, best=row.best, worst=row.worst,
                            teams=self.settings.league.teams, retrieved_at=retrieved,
                        )
                    )
                if row.bye_week is not None:
                    player = ctx.index.get(player_id)
                    if player is not None and player.bye_week is None:
                        player.bye_week = row.bye_week

            ctx.flush(self.repos)
            written = (
                self.repos.projections.add_many(projections, sync_run_id=run_id)
                + self.repos.rankings.add_many(rankings, sync_run_id=run_id)
                + self.repos.adp.add_many(adp_records, sync_run_id=run_id)
            )
            total = len(projections) + len(rankings) + len(adp_records)
            report.written = written
            report.skipped = total - written
            detail = (
                f"{len(projections)} projection(s), {len(rankings)} ranking(s), "
                f"{len(adp_records)} adp; {ctx.resolution.summary()}"
            )
            report.warnings.append(detail)
            self.repos.sync_runs.finish(
                run_id, record_count=report.fetched, inserted_count=written, detail=detail
            )
        except SourceError as exc:
            self.repos.sync_runs.finish(run_id, status="failed", detail=str(exc))
            raise
        return report

    def sync_csv_directory(
        self, directory: Path | None = None, *, verbose: bool = False
    ) -> list[SyncReport]:
        target = directory or self.settings.app.sources.csv_import.directory
        files = csv_import.discover(target)
        if not files:
            log.warning("No CSV files found in %s", target)
            return []
        ctx = self.context()
        return [self.sync_from_csv(path, context=ctx, verbose=verbose) for path in files]

    # -- demo --------------------------------------------------------------

    def sync_demo(self, *, seed: int | None = None) -> SyncReport:
        """Populate a complete synthetic season (no network, no API key)."""
        season = self.settings.league.season
        run_id = self._run("demo", demo.SOURCE, season)
        report = SyncReport(dataset="demo", source=demo.SOURCE, season=season)
        dataset = demo.generate(
            season,
            seed=seed if seed is not None else (self.settings.app.simulation.seed or 20260817),
            teams=self.settings.league.teams,
            scoring_format=self.settings.league.scoring.compile().describe_format(),
        )
        report.fetched = sum(dataset.counts().values())
        report.retrieved_at = utcnow()

        self.repos.players.upsert(dataset.players)
        written = (
            self.repos.projections.add_many(dataset.projections, sync_run_id=run_id)
            + self.repos.rankings.add_many(dataset.rankings, sync_run_id=run_id)
            + self.repos.adp.add_many(dataset.adp, sync_run_id=run_id)
            + self.repos.injuries.add_many(dataset.injuries, sync_run_id=run_id)
        )
        report.written = written
        report.skipped = max(0, report.fetched - len(dataset.players) - written)
        report.warnings.append(
            "Synthetic data: names and statistics are generated, not real. "
            "Use it to exercise the pipeline, never to make real draft decisions."
        )
        self.repos.sync_runs.finish(
            run_id, record_count=report.fetched, inserted_count=written,
            detail=str(dataset.counts()),
        )
        return report

    # -- orchestration -----------------------------------------------------

    def sync_all(
        self, *, force_refresh: bool = False, verbose: bool = False
    ) -> list[SyncReport]:
        """Sync every enabled source, in dependency order.

        Players first so later sources have canonical ids to attach to.  A
        failure in one dataset is reported and the rest continue -- during draft
        prep, partial data beats no data.
        """
        reports: list[SyncReport] = []
        sources = self.settings.app.sources

        if sources.sleeper.enabled:
            reports.append(self._guarded(lambda: self.sync_players(force_refresh=force_refresh),
                                         "players", "sleeper"))

        if sources.fantasypros.enabled and self.fantasypros.has_key:
            ctx = self.context()
            reports.append(
                self._guarded(
                    lambda: self.sync_projections(
                        force_refresh=force_refresh, context=ctx, verbose=verbose
                    ),
                    "projections", "fantasypros",
                )
            )
            reports.append(
                self._guarded(
                    lambda: self.sync_rankings(
                        force_refresh=force_refresh, context=ctx, verbose=verbose
                    ),
                    "rankings", "fantasypros",
                )
            )
            reports.append(
                self._guarded(
                    lambda: self.sync_adp(
                        force_refresh=force_refresh, context=ctx, verbose=verbose
                    ),
                    "adp", "fantasypros",
                )
            )
        elif sources.fantasypros.enabled:
            report = SyncReport(dataset="fantasypros", source="fantasypros")
            report.warnings.append(
                f"Skipped: ${sources.fantasypros.api_key_env} is not set. "
                f"Use 'sync csv' with a FantasyPros CSV export, or 'sync demo'."
            )
            reports.append(report)

        if sources.csv_import.enabled and csv_import.discover(sources.csv_import.directory):
            reports.extend(self.sync_csv_directory(verbose=verbose))

        return reports

    def _guarded(
        self, action: Callable[[], SyncReport], dataset: str, source: str
    ) -> SyncReport:
        try:
            return action()
        except SourceError as exc:
            log.error("%s sync failed: %s", dataset, exc)
            report = SyncReport(dataset=dataset, source=source)
            report.warnings.append(f"FAILED: {exc}")
            return report

    # -- identity ----------------------------------------------------------

    def _resolve_rows(
        self,
        rows: Sequence[FantasyProsRow],
        ctx: SyncContext,
        source: str,
        *,
        default_position: str | None = None,
    ) -> Iterator[tuple[FantasyProsRow, str]]:
        """Yield ``(row, canonical_player_id)`` for each resolvable row."""
        for row in rows:
            resolved = ctx.index.resolve(
                source=source,
                source_player_id=row.source_player_id,
                name=row.name,
                position=row.position or default_position,
                team=row.team,
                cross_source_ids=row.cross_source_ids,
            )
            if resolved is None:
                continue
            ctx.resolution.record(resolved, row.name)
            yield row, resolved.player_id

    @staticmethod
    def _apply_bye_weeks(rows: Sequence[FantasyProsRow], ctx: SyncContext) -> None:
        """FantasyPros publishes bye weeks; Sleeper does not."""
        for row in rows:
            if row.bye_week is None or not row.source_player_id:
                continue
            player_id = ctx.index.by_source("fantasypros", row.source_player_id)
            if player_id is None:
                continue
            player = ctx.index.get(player_id)
            if player is not None:
                player.bye_week = row.bye_week


@dataclass(slots=True)
class FreshnessReport:
    """Data-freshness summary, with a stale-data warning."""

    entries: list
    threshold_hours: float
    now: datetime

    def stale(self) -> list:
        return [
            entry
            for entry in self.entries
            if entry.record_count
            and (entry.age_hours(self.now) or 0) > self.threshold_hours
        ]

    def missing(self) -> list:
        return [entry for entry in self.entries if not entry.record_count]

    def describe(self) -> list[str]:
        lines = []
        for entry in self.entries:
            marker = ""
            age = entry.age_hours(self.now)
            if entry.record_count and age is not None and age > self.threshold_hours:
                marker = "  [STALE]"
            lines.append(
                f"{entry.dataset:<14} {entry.source:<14} "
                f"{entry.record_count:>6} record(s)  {entry.describe_age(self.now)}{marker}"
            )
        return lines


def freshness(settings: Settings, repos: Repositories) -> FreshnessReport:
    """Data freshness across every dataset, as DATA_SOURCES.md requires."""
    return FreshnessReport(
        entries=repos.sync_runs.freshness(settings.league.season),
        threshold_hours=settings.app.sources.staleness_warning_hours,
        now=utcnow(),
    )
