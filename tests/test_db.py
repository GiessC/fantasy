"""Schema, snapshots, and repositories."""

from __future__ import annotations

import pytest

from fantasy_ai.db import Database, Repositories
from fantasy_ai.db.database import content_hash
from fantasy_ai.db.schema import LATEST_VERSION
from fantasy_ai.errors import DatabaseError, DraftStateError
from fantasy_ai.models import ADPRecord, DraftPick, DraftRecord, InjuryRecord, ProjectionRecord
from fantasy_ai.stats import StatLine

from .conftest import make_player


class TestMigrations:
    def test_fresh_database_reaches_latest(self, db: Database):
        assert db.version == LATEST_VERSION

    def test_migrate_is_idempotent(self, db: Database):
        assert db.migrate() == []

    def test_rejects_a_future_schema(self, db: Database):
        db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, 'future', datetime('now'))",
            (LATEST_VERSION + 5,),
        )
        with pytest.raises(DatabaseError, match="newer than this build"):
            db.ensure_ready()

    def test_foreign_keys_enforced(self, repos: Repositories):
        with pytest.raises(DatabaseError):
            repos.db.execute(
                "INSERT INTO projections (player_id, season, source, stats_json, "
                "content_hash, retrieved_at) VALUES ('ghost', 2026, 't', '{}', 'h', 'now')"
            )


class TestPlayerRepository:
    def test_upsert_and_read_back(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test Player", "RB")])
        player = repos.players.get("p1")
        assert player is not None
        assert player.full_name == "Test Player"
        assert player.source_ids == {"test": "p1"}

    def test_upsert_preserves_existing_fields(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test Player", "RB", age=27.0)])
        updated = make_player("p1", "Test Player", "RB")
        updated.age = None
        repos.players.upsert([updated])
        assert repos.players.get("p1").age == 27.0

    def test_source_id_resolution(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test Player", "RB")])
        assert repos.players.resolve_source_id("test", "p1") == "p1"
        repos.players.link_source_id("fantasypros", "9999", "p1")
        assert repos.players.resolve_source_id("fantasypros", "9999") == "p1"

    def test_search_and_normalized_lookup(self, repos: Repositories):
        repos.players.upsert(
            [
                make_player("p1", "Marvin Harrison Jr.", "WR"),
                make_player("p2", "Someone Else", "RB"),
            ]
        )
        assert [p.player_id for p in repos.players.search("harrison")] == ["p1"]
        assert repos.players.find_by_normalized_name("marvinharrison")[0].player_id == "p1"

    def test_bulk_source_ids(self, repos: Repositories):
        repos.players.upsert(
            [make_player("p1", "A B", "RB"), make_player("p2", "C D", "WR")]
        )
        mapping = repos.players.all_source_ids()
        assert mapping["p1"] == {"test": "p1"}


class TestSnapshots:
    def _projection(self, value: float) -> ProjectionRecord:
        return ProjectionRecord(
            player_id="p1", season=2026, source="test", stats=StatLine({"rush_yd": value})
        )

    def test_identical_snapshot_is_skipped(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        assert repos.projections.add_many([self._projection(1000)]) == 1
        assert repos.projections.add_many([self._projection(1000)]) == 0
        assert repos.projections.count() == 1

    def test_changed_snapshot_is_appended(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        repos.projections.add_many([self._projection(1000)])
        repos.projections.add_many([self._projection(1100)])
        assert repos.projections.count() == 2
        latest = repos.projections.latest(2026)["p1"][0]
        assert latest.stats["rush_yd"] == 1100
        assert len(repos.projections.history("p1", 2026)) == 2

    def test_reverting_to_an_earlier_value_is_recorded(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        repos.projections.add_many([self._projection(1000)])
        repos.projections.add_many([self._projection(1100)])
        repos.projections.add_many([self._projection(1000)])
        assert repos.projections.count() == 3

    def test_latest_view_picks_newest_per_source(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        repos.projections.add_many(
            [
                ProjectionRecord(player_id="p1", season=2026, source="a",
                                 stats=StatLine({"rush_yd": 900})),
                ProjectionRecord(player_id="p1", season=2026, source="b",
                                 stats=StatLine({"rush_yd": 950})),
            ]
        )
        sources = {record.source for record in repos.projections.latest(2026)["p1"]}
        assert sources == {"a", "b"}

    def test_prune_keeps_newest(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        for value in range(1000, 1050, 10):
            repos.projections.add_many([self._projection(value)])
        assert repos.projections.count() == 5
        repos.projections.prune_history(keep=2)
        assert repos.projections.count() == 2
        assert repos.projections.latest(2026)["p1"][0].stats["rush_yd"] == 1040

    def test_adp_and_injury_snapshots(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "Test", "RB")])
        repos.adp.add_many(
            [ADPRecord(player_id="p1", season=2026, source="t", adp=12.5, stdev=3.0)]
        )
        repos.injuries.add_many(
            [InjuryRecord(player_id="p1", season=2026, source="t", status="Questionable")]
        )
        assert repos.adp.latest(2026)["p1"][0].adp == 12.5
        assert repos.injuries.latest(2026)["p1"][0].status == "Questionable"

    def test_content_hash_is_order_independent(self):
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


class TestDraftRepository:
    def _draft(self, repos: Repositories) -> DraftRecord:
        return repos.drafts.create(
            DraftRecord(
                draft_id=None, name="Test", season=2026, teams=10, rounds=15,
                draft_type="snake", user_slot=7,
            )
        )

    def test_create_and_find_active(self, repos: Repositories):
        record = self._draft(repos)
        assert record.draft_id is not None
        assert repos.drafts.active().draft_id == record.draft_id

    def test_picks_round_trip(self, repos: Repositories):
        record = self._draft(repos)
        repos.players.upsert([make_player("p1", "Test", "RB")])
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"),
        )
        picks = repos.drafts.picks(record.draft_id)
        assert len(picks) == 1
        assert picks[0].player_id == "p1"

    def test_duplicate_player_rejected(self, repos: Repositories):
        record = self._draft(repos)
        repos.players.upsert([make_player("p1", "Test", "RB")])
        pick = DraftPick(
            overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"
        )
        repos.drafts.add_pick(record.draft_id, pick)
        with pytest.raises(DraftStateError, match="already been drafted"):
            repos.drafts.add_pick(
                record.draft_id,
                DraftPick(overall_pick=2, round_number=1, slot=2, team_index=1,
                          player_id="p1"),
            )

    def test_duplicate_pick_number_rejected(self, repos: Repositories):
        record = self._draft(repos)
        repos.players.upsert(
            [make_player("p1", "A", "RB"), make_player("p2", "B", "WR")]
        )
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"),
        )
        with pytest.raises(DraftStateError, match="already been recorded"):
            repos.drafts.add_pick(
                record.draft_id,
                DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0,
                          player_id="p2"),
            )

    def test_multiple_unknown_picks_allowed(self, repos: Repositories):
        """Unknown players must not collide on the partial unique index."""
        record = self._draft(repos)
        for overall in (1, 2, 3):
            repos.drafts.add_pick(
                record.draft_id,
                DraftPick(overall_pick=overall, round_number=1, slot=overall,
                          team_index=overall - 1, player_id=None),
            )
        assert len(repos.drafts.picks(record.draft_id)) == 3

    def test_undo_removes_the_last_pick(self, repos: Repositories):
        record = self._draft(repos)
        repos.players.upsert(
            [make_player("p1", "A", "RB"), make_player("p2", "B", "WR")]
        )
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"),
        )
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=2, round_number=1, slot=2, team_index=1, player_id="p2"),
        )
        removed = repos.drafts.remove_last_pick(record.draft_id)
        assert removed.overall_pick == 2
        assert len(repos.drafts.picks(record.draft_id)) == 1
        repos.drafts.remove_last_pick(record.draft_id)
        assert repos.drafts.remove_last_pick(record.draft_id) is None

    def test_deleting_a_draft_cascades(self, repos: Repositories):
        record = self._draft(repos)
        repos.players.upsert([make_player("p1", "A", "RB")])
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"),
        )
        repos.drafts.delete(record.draft_id)
        assert repos.drafts.picks(record.draft_id) == []

    def test_dropping_a_player_keeps_the_pick(self, repos: Repositories):
        """A pick's slot must survive its player being removed (ON DELETE SET NULL)."""
        record = self._draft(repos)
        repos.players.upsert([make_player("p1", "A", "RB")])
        repos.drafts.add_pick(
            record.draft_id,
            DraftPick(overall_pick=1, round_number=1, slot=1, team_index=0, player_id="p1"),
        )
        repos.db.execute("DELETE FROM players WHERE player_id = 'p1'")
        picks = repos.drafts.picks(record.draft_id)
        assert len(picks) == 1
        assert picks[0].player_id is None


class TestFreshness:
    def test_reports_per_dataset(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "A", "RB")])
        repos.projections.add_many(
            [ProjectionRecord(player_id="p1", season=2026, source="demo",
                              stats=StatLine({"rush_yd": 1000}))]
        )
        entries = {entry.dataset: entry for entry in repos.sync_runs.freshness(2026)}
        assert entries["projections"].record_count == 1
        assert entries["projections"].source == "demo"
        assert entries["rankings"].record_count == 0
        assert entries["rankings"].describe_age() == "never"

    def test_player_source_label_is_not_hardcoded(self, repos: Repositories):
        repos.players.upsert([make_player("p1", "A", "RB")])
        entries = {entry.dataset: entry for entry in repos.sync_runs.freshness(2026)}
        assert entries["players"].source == "test"

    def test_sync_run_lifecycle(self, repos: Repositories):
        run_id = repos.sync_runs.start("projections", "test", 2026)
        repos.sync_runs.finish(run_id, record_count=10, inserted_count=8)
        row = repos.sync_runs.recent(1)[0]
        assert row["status"] == "success"
        assert row["record_count"] == 10
