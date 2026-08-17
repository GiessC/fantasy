"""End-to-end tests: config -> database -> sync -> analytics -> draft -> CLI.

These are the tests that would catch a break in the wiring between layers, which
unit tests by construction cannot.  Still no network and no model server.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from fantasy_ai.analytics import AnalyticsEngine, load_dataset
from fantasy_ai.cli.main import run
from fantasy_ai.errors import DataMissingError
from fantasy_ai.services import AnalysisService, SyncService, freshness

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_demo_sync_then_analyse(self, settings, repos):
        report = SyncService(settings, repos).sync_demo(seed=11)
        assert report.written > 0
        assert any("Synthetic" in warning for warning in report.warnings)

        context = AnalysisService(settings, repos).board(use_draft=False, simulate=False)
        board = context.board
        assert len(board.players) > 100
        assert board.replacement.by_position["RB"].points > 0

        top = board.top(20)
        # A well-formed board is led by flex-eligible skill positions, not by
        # kickers and defenses -- the failure mode the market term guards against.
        assert {player.position for player in top[:10]} <= {"RB", "WR", "TE", "QB"}

    def test_sync_is_idempotent(self, settings, repos):
        service = SyncService(settings, repos)
        service.sync_demo(seed=11)
        data_tables = ("players", "projections", "rankings", "adp", "injuries")
        before = {name: repos.db.table_counts()[name] for name in data_tables}
        service.sync_demo(seed=11)
        after = {name: repos.db.table_counts()[name] for name in data_tables}
        # sync_runs grows by design (it is the audit log); the data must not.
        assert after == before

    def test_every_number_is_explainable(self, settings, repos):
        SyncService(settings, repos).sync_demo(seed=11)
        context = AnalysisService(settings, repos).board(use_draft=False, simulate=False)
        player = context.board.top(1)[0]

        lines = player.explain()
        assert any("Projected points" in line for line in lines)
        assert any("VOR =" in line for line in lines)
        assert any("Draft score" in line for line in lines)

        # The projection decomposes exactly into its scoring lines.
        assert sum(player.scoring.breakdown().values()) == pytest.approx(
            player.projected_points
        )
        # The draft score decomposes exactly into its components.
        assert player.draft_score.total == pytest.approx(
            sum(c.contribution for c in player.draft_score.components)
        )

    def test_analysis_is_reproducible(self, settings, repos):
        SyncService(settings, repos).sync_demo(seed=11)
        service = AnalysisService(settings, repos)
        first = service.board(use_draft=False, simulate=True, seed=5)
        second = service.board(use_draft=False, simulate=True, seed=5)
        assert [p.player_id for p in first.board.top(20)] == [
            p.player_id for p in second.board.top(20)
        ]
        assert first.board.top(1)[0].score == pytest.approx(second.board.top(1)[0].score)

    def test_simulation_feeds_back_into_the_score(self, settings, repos):
        SyncService(settings, repos).sync_demo(seed=11)
        service = AnalysisService(settings, repos)
        with_sim = service.board(use_draft=False, simulate=True, iterations=200, seed=3)
        without = service.board(use_draft=False, simulate=False)
        assert with_sim.availability_method() == "monte-carlo"
        assert without.availability_method() == "analytic"
        assert with_sim.board.top(1)[0].availability.method == "monte-carlo"

    def test_missing_data_raises_a_helpful_error(self, settings, repos):
        with pytest.raises(DataMissingError, match="sync players"):
            AnalysisService(settings, repos).board()

    def test_projections_without_players_error(self, settings, repos, league):
        from .conftest import make_player

        repos.players.upsert([make_player("p1", "A", "RB")])
        with pytest.raises(DataMissingError, match="sync projections"):
            load_dataset(repos, league, settings.app.analytics)

    def test_freshness_reflects_the_sync(self, settings, repos):
        SyncService(settings, repos).sync_demo(seed=11)
        report = freshness(settings, repos)
        assert not report.missing()
        assert not report.stale()


class TestDraftIntegration:
    def _service(self, settings, repos) -> AnalysisService:
        SyncService(settings, repos).sync_demo(seed=11)
        return AnalysisService(settings, repos)

    def test_drafted_players_leave_the_board(self, settings, repos):
        service = self._service(settings, repos)
        draft = service.drafts.start()
        before = service.board().board
        taken = before.top(3)
        for player in taken:
            service.drafts.record_pick(draft, player.player_id)
        after = service.board().board
        remaining = {player.player_id for player in after.players}
        assert not any(player.player_id in remaining for player in taken)

    def test_roster_shapes_the_board(self, settings, repos):
        service = self._service(settings, repos)
        draft = service.drafts.start()
        # Fill the user's QB slot, then check a second QB is penalised.
        board = service.board().board
        qb = next(p for p in board.players if p.position == "QB")
        service.drafts.record_pick(draft, qb.player_id, overall_pick=7)

        updated = service.board().board
        next_qb = next(p for p in updated.players if p.position == "QB")
        assert next_qb.roster_fit is not None
        assert next_qb.roster_fit.total < 0
        assert not next_qb.roster_fit.fills_open_slot

    def test_availability_targets_the_users_next_turn(self, settings, repos):
        service = self._service(settings, repos)
        draft = service.drafts.start()
        board = service.board().board
        for index, player in enumerate(board.top(6), start=1):
            service.drafts.record_pick(draft, player.player_id, overall_pick=index)
        context = service.board()
        # On the clock at 7 in a 10-team snake, the next turn is pick 14.
        assert context.board.next_pick == 14

    def test_undo_restores_the_board(self, settings, repos):
        service = self._service(settings, repos)
        draft = service.drafts.start()
        before = {player.player_id for player in service.board().board.players}
        target = service.board().board.top(1)[0]
        service.drafts.record_pick(draft, target.player_id)
        service.drafts.undo(draft)
        after = {player.player_id for player in service.board().board.players}
        assert before == after

    def test_end_of_draft_forces_unfilled_starters(self, settings, repos):
        """With picks running out, the board must insist on K and DST."""
        service = self._service(settings, repos)
        draft = service.drafts.start(rounds=9)
        board = service.board().board

        # Fill every starting slot except K and DST using the user's own picks.
        wanted = ["QB", "RB", "RB", "WR", "WR", "TE", "RB"]
        user_picks = service.drafts.user_picks(draft)
        used: set[str] = set()
        for index, position in enumerate(wanted):
            candidate = next(
                p for p in board.players
                if p.position == position and p.player_id not in used
            )
            used.add(candidate.player_id)
            service.drafts.record_pick(
                draft, candidate.player_id, overall_pick=user_picks[index]
            )

        context = service.board()
        assert context.status.picks_remaining_for_user == 2
        top_positions = [player.position for player in context.board.top(4)]
        assert {"K", "DST"} <= set(top_positions)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project directory with a valid config."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "league.yaml").write_text(
        textwrap.dedent(
            """
            league:
              name: CLI Test League
              season: 2026
              teams: 10
              scoring:
                receiving:
                  reception: 0.5
              roster:
                QB: 1
                RB: 2
                WR: 2
                TE: 1
                FLEX: 1
                K: 1
                DST: 1
                BENCH: 6
              draft:
                type: snake
                position: 4
                rounds: 15
            simulation:
              iterations: 60
              seed: 3
            """
        )
    )
    (tmp_path / "config" / "sources.yaml").write_text(
        textwrap.dedent(
            f"""
            paths:
              data_dir: {tmp_path / "data"}
              database: {tmp_path / "data" / "test.db"}
              http_cache: {tmp_path / "data" / "cache"}
            llm:
              enabled: false
            log_level: warning
            """
        )
    )
    return tmp_path


@dataclass(slots=True)
class CLIResult:
    exit_code: int
    output: str


def invoke(project: Path, *args: str) -> CLIResult:
    """Run the CLI through its real entry point.

    Deliberately not Typer's ``CliRunner``: that invokes the Typer app directly
    and therefore skips :func:`fantasy_ai.cli.main.run`, which is where expected
    errors are turned into clean messages and meaningful exit codes.  Testing
    around it would leave the exit-code contract unverified.
    """
    argv = [
        "fantasy-ai",
        "--league", str(project / "config" / "league.yaml"),
        "--sources", str(project / "config" / "sources.yaml"),
        *args,
    ]
    buffer = io.StringIO()
    with (
        mock.patch.object(sys, "argv", argv),
        contextlib.redirect_stdout(buffer),
        contextlib.redirect_stderr(buffer),
    ):
        code = run()
    return CLIResult(exit_code=code, output=buffer.getvalue())


class TestCLI:
    def test_validate_config(self, project: Path):
        result = invoke(project, "validate-config")
        assert result.exit_code == 0
        assert "CLI Test League" in result.output
        assert "Half PPR" in result.output

    def test_validate_config_json(self, project: Path):
        result = invoke(project, "validate-config", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["valid"] is True
        assert payload["league"]["teams"] == 10

    def test_db_init_then_status(self, project: Path):
        assert invoke(project, "db", "init").exit_code == 0
        result = invoke(project, "db", "status")
        assert result.exit_code == 0
        assert "players" in result.output

    def test_full_workflow(self, project: Path):
        assert invoke(project, "db", "init").exit_code == 0
        assert invoke(project, "sync", "demo").exit_code == 0

        board = invoke(project, "analyze", "board", "-n", "5")
        assert board.exit_code == 0
        assert "Draft board" in board.output

        status = invoke(project, "data", "status")
        assert status.exit_code == 0
        assert "projections" in status.output

    def test_analyze_board_json(self, project: Path):
        invoke(project, "sync", "demo")
        result = invoke(project, "analyze", "board", "-n", "3", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload["players"]) == 3
        first = payload["players"][0]
        for field in ("name", "position", "projected_points", "vor", "draft_score"):
            assert field in first

    def test_draft_lifecycle(self, project: Path):
        invoke(project, "sync", "demo")
        assert invoke(project, "draft", "start").exit_code == 0

        board = invoke(project, "analyze", "board", "-n", "3", "--json")
        names = [player["name"] for player in json.loads(board.output)["players"]]

        pick = invoke(project, "draft", "pick", names[0])
        assert pick.exit_code == 0
        assert names[0] in pick.output

        status = invoke(project, "draft", "status")
        assert status.exit_code == 0
        assert names[0] in status.output

        undo = invoke(project, "draft", "undo")
        assert undo.exit_code == 0
        assert names[0] in undo.output

    def test_duplicate_pick_is_a_clean_error(self, project: Path):
        invoke(project, "sync", "demo")
        invoke(project, "draft", "start")
        board = invoke(project, "analyze", "board", "-n", "1", "--json")
        name = json.loads(board.output)["players"][0]["name"]
        invoke(project, "draft", "pick", name)
        second = invoke(project, "draft", "pick", name)
        assert second.exit_code != 0
        assert "already been drafted" in second.output

    def test_unknown_player_is_a_clean_error(self, project: Path):
        invoke(project, "sync", "demo")
        result = invoke(project, "analyze", "player", "Nobody At All")
        assert result.exit_code == 5
        assert "No player matches" in result.output

    def test_analyze_player_detail(self, project: Path):
        invoke(project, "sync", "demo")
        board = invoke(project, "analyze", "board", "-n", "1", "--json")
        name = json.loads(board.output)["players"][0]["name"]
        result = invoke(project, "analyze", "player", name, "--breakdown")
        assert result.exit_code == 0
        assert "VOR" in result.output
        assert "computed" in result.output

    def test_simulate_availability(self, project: Path):
        invoke(project, "sync", "demo")
        invoke(project, "draft", "start")
        result = invoke(project, "simulate", "availability", "-n", "5", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["iterations"] > 0
        assert all(
            0.0 <= player["monte_carlo"] <= 1.0 for player in payload["players"]
        )

    def test_analysis_before_sync_is_a_clean_error(self, project: Path):
        invoke(project, "db", "init")
        result = invoke(project, "analyze", "board")
        assert result.exit_code == 5
        assert "sync" in result.output

    def test_llm_commands_respect_the_disabled_flag(self, project: Path):
        invoke(project, "sync", "demo")
        result = invoke(project, "ask", "Who should I take?")
        assert result.exit_code == 7
        assert "disabled" in result.output

    def test_recommend_dry_run_needs_no_model(self, project: Path):
        invoke(project, "sync", "demo")
        result = invoke(project, "recommend", "--dry-run", "-c", "3")
        assert result.exit_code == 0
        assert "RECOMMENDATION" not in result.output
        assert "candidates list" in result.output

    def test_sim_overrides_from_the_command_line(self, project: Path):
        invoke(project, "sync", "demo")
        invoke(project, "draft", "start")
        result = invoke(
            project, "--sim-iterations", "25", "simulate", "availability", "--json"
        )
        assert json.loads(result.output)["iterations"] == 25

    def test_version(self, project: Path):
        result = invoke(project, "--version")
        assert result.exit_code == 0
        assert "fantasy-ai" in result.output


class TestCrossLeagueBehaviour:
    """The same data must produce different boards under different rules."""

    def _board(self, settings, repos, league):
        settings.league = league
        dataset = load_dataset(repos, league, settings.app.analytics)
        engine = AnalyticsEngine(league, settings.app.analytics, settings.app.simulation)
        return engine.analyze(dataset)

    def test_superflex_lifts_quarterbacks(self, settings, repos, superflex_league):
        """The only difference is the SUPERFLEX slot, so QBs must move up."""
        from fantasy_ai.config import LeagueConfig

        SyncService(settings, repos).sync_demo(seed=11)
        single_qb_roster = dict(superflex_league.roster)
        single_qb_roster.pop("SUPERFLEX")
        single_qb_roster["BENCH"] = single_qb_roster["BENCH"] + 1
        single_qb = LeagueConfig(
            name="Single QB",
            season=superflex_league.season,
            teams=superflex_league.teams,
            roster=single_qb_roster,
            flex=superflex_league.flex.model_dump(),
            scoring=superflex_league.scoring.model_dump(),
        )

        standard = self._board(settings, repos, single_qb)
        superflex = self._board(settings, repos, superflex_league)

        def best_qb_rank(board) -> int:
            return next(
                index
                for index, player in enumerate(board.top(300), start=1)
                if player.position == "QB"
            )

        assert (
            superflex.replacement.by_position["QB"].demand
            > standard.replacement.by_position["QB"].demand
        )
        assert best_qb_rank(superflex) < best_qb_rank(standard)

    def test_ppr_lifts_receivers(self, settings, repos, league):
        from fantasy_ai.config import LeagueConfig

        SyncService(settings, repos).sync_demo(seed=11)
        standard = LeagueConfig(
            name="Standard", season=league.season, teams=10,
            roster=dict(league.roster),
            scoring={"receiving": {"reception": 0.0}},
        )
        ppr = LeagueConfig(
            name="PPR", season=league.season, teams=10,
            roster=dict(league.roster),
            scoring={"receiving": {"reception": 1.0}},
        )
        standard_board = self._board(settings, repos, standard)
        ppr_board = self._board(settings, repos, ppr)

        def receiver_share(board) -> float:
            top = board.top(24)
            return sum(1 for player in top if player.position in {"WR", "TE"}) / len(top)

        assert receiver_share(ppr_board) >= receiver_share(standard_board)

    def test_no_kicker_slot_removes_kickers_from_contention(self, settings, repos, league):
        from fantasy_ai.config import LeagueConfig

        SyncService(settings, repos).sync_demo(seed=11)
        no_kicker = LeagueConfig(
            name="No K", season=league.season, teams=10,
            roster={"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "BENCH": 7},
        )
        board = self._board(settings, repos, no_kicker)
        kickers = [player for player in board.players if player.position == "K"]
        assert kickers
        assert all(player.vor.vor <= 0 for player in kickers)
        assert not board.replacement.by_position["K"].has_starting_demand
