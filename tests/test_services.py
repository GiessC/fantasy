"""Sync orchestration and Sleeper draft import, driven by mocked transports.

These paths are the ones a user cannot exercise without an API key and a live
draft, so they are covered here against recorded-shape payloads rather than left
to be discovered on draft night.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fantasy_ai.errors import DraftStateError, SourceAuthError, SourceUnavailableError
from fantasy_ai.services import SleeperDraftImporter, SyncService, freshness
from fantasy_ai.sources.fantasypros import FantasyProsClient
from fantasy_ai.sources.sleeper import SleeperClient

from .test_sources import FP_PROJECTIONS, FP_RANKINGS, SLEEPER_PLAYERS


def sleeper_client(routes: dict[str, object], *, config=None) -> SleeperClient:
    """A Sleeper client whose transport answers from a path -> payload map."""
    from fantasy_ai.config import HTTPConfig, SleeperConfig

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": request.url.path})

    return SleeperClient(
        config or SleeperConfig(),
        HTTPConfig(max_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def fantasypros_client(payloads: dict[str, object]) -> FantasyProsClient:
    """A FantasyPros client that answers by endpoint keyword."""
    from fantasy_ai.config import FantasyProsConfig, HTTPConfig

    def handler(request: httpx.Request) -> httpx.Response:
        for keyword, payload in payloads.items():
            if keyword in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": str(request.url)})

    return FantasyProsClient(
        FantasyProsConfig(),
        HTTPConfig(max_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_key="test-key",
    )


@pytest.fixture
def service(settings, repos) -> SyncService:
    return SyncService(
        settings,
        repos,
        sleeper=sleeper_client({"players/nfl": SLEEPER_PLAYERS}),
        fantasypros=fantasypros_client(
            {"consensus-rankings": FP_RANKINGS, "projections": FP_PROJECTIONS}
        ),
    )


class TestSyncPlayers:
    def test_writes_players_and_injuries(self, service, repos):
        report = service.sync_players()
        assert report.written == 3         # the row without a position is skipped
        assert repos.players.count() == 3
        assert repos.players.resolve_source_id("sleeper", "4034") is not None
        assert repos.injuries.count() == 1

    def test_is_idempotent(self, service, repos):
        service.sync_players()
        service.sync_players()
        assert repos.players.count() == 3
        assert repos.injuries.count() == 1

    def test_records_a_sync_run(self, service, repos):
        service.sync_players()
        row = repos.sync_runs.recent(1)[0]
        assert row["dataset"] == "players"
        assert row["status"] == "success"

    def test_failure_is_recorded_and_raised(self, settings, repos):
        broken = SleeperClient(
            settings.app.sources.sleeper,
            settings.app.sources.http.model_copy(update={"max_retries": 0}),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(503))
            ),
        )
        service = SyncService(settings, repos, sleeper=broken)
        with pytest.raises(SourceUnavailableError):
            service.sync_players()
        assert repos.sync_runs.recent(1)[0]["status"] == "failed"


class TestSyncFantasyPros:
    def test_rankings_resolve_onto_sleeper_players(self, service, repos):
        service.sync_players()
        report = service.sync_rankings()
        assert report.written == 2
        # "Alvin Player" exists in the Sleeper fixture, so it must attach to the
        # existing canonical id rather than minting a new player.
        assert repos.players.count() == 3
        alvin = repos.players.resolve_source_id("sleeper", "4034")
        assert alvin in repos.rankings.latest(2026)

    def test_rankings_carry_dispersion(self, service, repos):
        service.sync_players()
        service.sync_rankings()
        record = next(iter(repos.rankings.latest(2026).values()))[0]
        assert record.stdev is not None
        assert record.best is not None
        assert record.scoring_format in {"STD", "HALF", "PPR"}

    def test_bye_weeks_are_backfilled(self, service, repos):
        service.sync_players()
        assert repos.players.get("sleeper:4034").bye_week is None
        service.sync_rankings()
        assert repos.players.get("sleeper:4034").bye_week == 9

    def test_adp_sync(self, service, repos):
        service.sync_players()
        report = service.sync_adp()
        assert report.written == 2
        records = repos.adp.latest(2026)
        assert all(record[0].adp > 0 for record in records.values())

    def test_projections_sync(self, service, repos):
        service.sync_players()
        report = service.sync_projections(positions=["RB"])
        assert report.written == 1
        projection = next(iter(repos.projections.latest(2026).values()))[0]
        assert projection.stats["rush_yd"] == 1150

    def test_projections_are_scored_under_our_league(self, service, repos, settings):
        """A source's own FPTS column must never leak into our numbers."""
        service.sync_players()
        service.sync_projections(positions=["RB"])
        projection = next(iter(repos.projections.latest(2026).values()))[0]
        assert "fpts" not in projection.stats

    def test_missing_api_key_is_reported_not_raised(self, settings, repos):
        from fantasy_ai.config import FantasyProsConfig, HTTPConfig

        keyless = FantasyProsClient(
            FantasyProsConfig(), HTTPConfig(),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            ),
            api_key=None,
        )
        service = SyncService(
            settings, repos,
            sleeper=sleeper_client({"players/nfl": SLEEPER_PLAYERS}),
            fantasypros=keyless,
        )
        reports = service.sync_all()
        messages = " ".join(w for report in reports for w in report.warnings)
        assert "FANTASYPROS_API_KEY" in messages
        with pytest.raises(SourceAuthError):
            service.sync_rankings()

    def test_sync_all_continues_past_one_failure(self, settings, repos):
        def handler(request: httpx.Request) -> httpx.Response:
            if "projections" in str(request.url):
                return httpx.Response(500)
            if "consensus-rankings" in str(request.url):
                return httpx.Response(200, json=FP_RANKINGS)
            return httpx.Response(404)

        from fantasy_ai.config import FantasyProsConfig, HTTPConfig

        flaky = FantasyProsClient(
            FantasyProsConfig(), HTTPConfig(max_retries=0),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            api_key="k",
        )
        service = SyncService(
            settings, repos,
            sleeper=sleeper_client({"players/nfl": SLEEPER_PLAYERS}),
            fantasypros=flaky,
        )
        reports = service.sync_all()
        datasets = {report.dataset: report for report in reports}
        assert any("FAILED" in w for w in datasets["projections"].warnings)
        assert datasets["rankings[DRAFT]"].written > 0


class TestSyncCSV:
    def _csv(self, tmp_path: Path) -> Path:
        path = tmp_path / "imports" / "rb.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '"Player","Team","POS","ATT","YDS","TDS","REC","REC YDS","REC TDS","FL"\n'
            '"Alvin Player","NO","RB1","250","1150","9","60","480","3","2"\n'
            '"New Guy","SEA","RB2","120","560","4","20","150","1","1"\n'
        )
        return path

    def test_import_creates_projections(self, service, repos, tmp_path):
        service.sync_players()
        report = service.sync_from_csv(self._csv(tmp_path), dataset="projections")
        assert report.written == 2
        assert repos.projections.count() == 2

    def test_import_matches_existing_players_by_name(self, service, repos, tmp_path):
        service.sync_players()
        before = repos.players.count()
        service.sync_from_csv(self._csv(tmp_path), dataset="projections")
        # "Alvin Player" already exists; only "New Guy" is minted.
        assert repos.players.count() == before + 1

    def test_directory_import(self, service, repos, tmp_path, settings):
        self._csv(tmp_path)
        settings.app.sources.csv_import.directory = tmp_path / "imports"
        service.sync_players()
        reports = service.sync_csv_directory()
        assert len(reports) == 1
        assert reports[0].written > 0

    def test_empty_directory_is_not_an_error(self, service, settings, tmp_path):
        settings.app.sources.csv_import.directory = tmp_path / "nothing"
        assert service.sync_csv_directory() == []

    def test_import_is_idempotent(self, service, repos, tmp_path):
        service.sync_players()
        path = self._csv(tmp_path)
        service.sync_from_csv(path, dataset="projections")
        count = repos.projections.count()
        service.sync_from_csv(path, dataset="projections")
        assert repos.projections.count() == count


class TestFreshnessReporting:
    def test_stale_threshold(self, service, settings, repos):
        service.sync_players()
        settings.app.sources.staleness_warning_hours = 0.0001
        report = freshness(settings, repos)
        # Nothing is old enough to be stale yet; the report must still be valid.
        assert report.describe()
        assert [entry.dataset for entry in report.missing()]


# ---------------------------------------------------------------------------
# Sleeper draft import
# ---------------------------------------------------------------------------


SLEEPER_DRAFT = {
    "draft_id": "900", "league_id": "100", "season": "2026", "status": "drafting",
    "type": "snake", "settings": {"teams": 10, "rounds": 15},
    "draft_order": {"user-me": 3},
    "slot_to_roster_id": {"3": 1},
}

SLEEPER_PICKS = [
    {"pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "4034",
     "metadata": {"first_name": "Alvin", "last_name": "Player", "position": "RB"}},
    {"pick_no": 2, "round": 1, "draft_slot": 2, "player_id": "6794",
     "metadata": {"first_name": "Justin", "last_name": "Thrower", "position": "QB"}},
]


class TestSleeperDraftImport:
    def _importer(self, settings, repos, *, picks=None, config=None) -> SleeperDraftImporter:
        from fantasy_ai.config import SleeperConfig

        settings.app.sources.sleeper = config or SleeperConfig(draft_id="900")
        client = sleeper_client(
            {
                "draft/900/picks": picks if picks is not None else SLEEPER_PICKS,
                "draft/900": SLEEPER_DRAFT,
                "league/100/drafts": [{"draft_id": "900", "start_time": 1}],
                "user/me": {"user_id": "user-me"},
                "players/nfl": SLEEPER_PLAYERS,
            },
            config=settings.app.sources.sleeper,
        )
        return SleeperDraftImporter(settings, repos, client=client)

    def _stock_players(self, settings, repos) -> None:
        SyncService(
            settings, repos,
            sleeper=sleeper_client({"players/nfl": SLEEPER_PLAYERS}),
        ).sync_players()

    def test_creates_a_local_draft_and_imports_picks(self, settings, repos):
        self._stock_players(settings, repos)
        result = self._importer(settings, repos).import_draft(user_slot=3)
        assert result.picks_seen == 2
        assert result.picks_added == 2
        assert not result.unresolved

        draft = repos.drafts.active()
        assert draft.external_id == "900"
        assert draft.user_slot == 3
        picks = repos.drafts.picks(draft.draft_id)
        assert [pick.overall_pick for pick in picks] == [1, 2]
        assert picks[0].player_id == "sleeper:4034"
        assert picks[0].source == "sleeper"

    def test_import_is_incremental(self, settings, repos):
        self._stock_players(settings, repos)
        importer = self._importer(settings, repos)
        importer.import_draft(user_slot=3)
        again = importer.import_draft(user_slot=3)
        assert again.picks_added == 0
        assert len(repos.drafts.picks(repos.drafts.active().draft_id)) == 2

    def test_new_picks_are_added_on_a_later_run(self, settings, repos):
        self._stock_players(settings, repos)
        self._importer(settings, repos).import_draft(user_slot=3)
        extended = [*SLEEPER_PICKS, {
            "pick_no": 3, "round": 1, "draft_slot": 3, "player_id": "KC",
            "metadata": {"first_name": "KC", "last_name": "Defense", "position": "DEF"},
        }]
        result = self._importer(settings, repos, picks=extended).import_draft(user_slot=3)
        assert result.picks_added == 1
        assert len(repos.drafts.picks(repos.drafts.active().draft_id)) == 3

    def test_unknown_player_still_consumes_its_pick(self, settings, repos):
        """The pick clock must stay aligned even when identity fails."""
        self._stock_players(settings, repos)
        picks = [{
            "pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "99999",
            "metadata": {"first_name": "Totally", "last_name": "Unknown"},
        }]
        result = self._importer(settings, repos, picks=picks).import_draft(user_slot=3)
        assert result.picks_added == 1
        assert result.unresolved == ["Totally Unknown"]
        stored = repos.drafts.picks(repos.drafts.active().draft_id)
        assert stored[0].player_id is None
        assert stored[0].overall_pick == 1

    def test_resolves_the_slot_from_the_username(self, settings, repos):
        from fantasy_ai.config import SleeperConfig

        self._stock_players(settings, repos)
        importer = self._importer(
            settings, repos, config=SleeperConfig(draft_id="900", username="me")
        )
        result = importer.import_draft()
        assert result.user_slot == 3

    def test_resolves_the_draft_from_the_league(self, settings, repos):
        from fantasy_ai.config import SleeperConfig

        self._stock_players(settings, repos)
        importer = self._importer(
            settings, repos, config=SleeperConfig(league_id="100")
        )
        assert importer.resolve_draft_id() == "900"

    def test_no_draft_configured(self, settings, repos):
        from fantasy_ai.config import SleeperConfig

        importer = self._importer(settings, repos, config=SleeperConfig())
        with pytest.raises(DraftStateError, match="No Sleeper draft"):
            importer.import_draft()

    def test_refuses_to_clash_with_an_unrelated_active_draft(self, settings, repos):
        from fantasy_ai.draft import DraftStateManager

        self._stock_players(settings, repos)
        DraftStateManager(repos, settings.league).start(name="Local", user_slot=1)
        with pytest.raises(DraftStateError, match="does not match"):
            self._importer(settings, repos).import_draft(user_slot=3)

    def test_team_count_mismatch_is_warned_not_fatal(self, settings, repos):
        self._stock_players(settings, repos)
        settings.league.teams = 12
        result = self._importer(settings, repos).import_draft(user_slot=3)
        assert any("teams" in warning for warning in result.warnings)
        assert repos.drafts.active().teams == 10

    def test_imported_draft_feeds_the_board(self, settings, repos):
        """The end-to-end point of importing: drafted players leave the board."""
        from fantasy_ai.services import AnalysisService

        SyncService(settings, repos).sync_demo(seed=11)
        self._stock_players(settings, repos)
        self._importer(settings, repos).import_draft(user_slot=3)

        service = AnalysisService(settings, repos)
        context = service.board()
        assert context.status is not None
        assert context.status.current_pick == 3
        remaining = {player.player_id for player in context.board.players}
        assert "sleeper:4034" not in remaining
