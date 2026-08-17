"""Draft order, state management, and the simulator."""

from __future__ import annotations

import pytest

from fantasy_ai.draft.order import (
    build_order,
    next_pick_for_slot,
    picks_between,
    picks_for_slot,
    slot_for_pick,
)
from fantasy_ai.draft.simulator import DrafterProfile, DraftSimulator, SimPlayer
from fantasy_ai.draft.state import DraftStateManager
from fantasy_ai.errors import DraftStateError
from fantasy_ai.models import ADPRecord

from .conftest import make_player


class TestDraftOrder:
    def test_snake_alternates(self):
        assert [slot_for_pick(n, 10).slot for n in range(1, 11)] == list(range(1, 11))
        assert [slot_for_pick(n, 10).slot for n in range(11, 21)] == list(range(10, 0, -1))
        assert [slot_for_pick(n, 10).slot for n in range(21, 31)] == list(range(1, 11))

    def test_linear_repeats(self):
        assert [slot_for_pick(n, 10, "linear").slot for n in range(11, 21)] == list(
            range(1, 11)
        )

    def test_third_round_reversal_repeats_round_two_order(self):
        second = [
            slot_for_pick(n, 10, "third_round_reversal", reversal_round=3).slot
            for n in range(11, 21)
        ]
        third = [
            slot_for_pick(n, 10, "third_round_reversal", reversal_round=3).slot
            for n in range(21, 31)
        ]
        assert second == third == list(range(10, 0, -1))

    def test_round_numbers(self):
        assert slot_for_pick(1, 10).round_number == 1
        assert slot_for_pick(10, 10).round_number == 1
        assert slot_for_pick(11, 10).round_number == 2
        assert slot_for_pick(30, 10).round_number == 3

    def test_turn_ends_are_back_to_back(self):
        assert picks_for_slot(1, 10, 4) == [1, 20, 21, 40]
        assert picks_for_slot(10, 10, 4) == [10, 11, 30, 31]

    def test_wait_length_depends_on_slot(self):
        # Slot 1 waits the longest between rounds 1 and 2; slot 10 waits not at all.
        assert picks_between(1, 1, 10, 16) == 18
        assert picks_between(10, 10, 10, 16) == 0
        assert picks_between(7, 7, 10, 16) == 6

    def test_next_pick_lookup(self):
        assert next_pick_for_slot(7, 8, 10, 16) == 14
        assert next_pick_for_slot(7, 7, 10, 16) == 7
        assert next_pick_for_slot(7, 200, 10, 16) is None

    def test_full_order_covers_every_pick_once(self):
        order = build_order(10, 5)
        assert len(order) == 50
        assert sorted(position.overall for position in order) == list(range(1, 51))
        for slot in range(1, 11):
            assert sum(1 for position in order if position.slot == slot) == 5

    def test_invalid_inputs(self):
        with pytest.raises(DraftStateError):
            slot_for_pick(0, 10)
        with pytest.raises(DraftStateError):
            picks_for_slot(11, 10, 5)
        with pytest.raises(DraftStateError, match="Unsupported draft type"):
            slot_for_pick(1, 10, "lottery")


class TestDraftState:
    @pytest.fixture
    def manager(self, repos, league) -> DraftStateManager:
        repos.players.upsert(
            [make_player(f"p{index}", f"Player {index}", "RB") for index in range(1, 40)]
        )
        return DraftStateManager(repos, league)

    def test_start_uses_league_defaults(self, manager, league):
        record = manager.start()
        assert record.teams == league.teams
        assert record.user_slot == league.draft.position
        assert record.rounds == league.effective_rounds

    def test_second_draft_needs_replace(self, manager):
        manager.start()
        with pytest.raises(DraftStateError, match="still active"):
            manager.start()
        assert manager.start(replace_active=True) is not None

    def test_slot_must_be_valid(self, manager):
        with pytest.raises(DraftStateError, match="outside"):
            manager.start(user_slot=99)

    def test_picks_advance_in_order(self, manager, repos, league):
        record = manager.start()
        for index in range(1, 8):
            pick = manager.record_pick(record, f"p{index}")
            assert pick.overall_pick == index
        status = manager.status(record, {p.player_id: p for p in repos.players.all()})
        assert status.current_pick == 8
        assert status.on_the_clock == 8
        assert not status.is_user_on_the_clock

    def test_user_pick_is_flagged_and_rostered(self, manager, repos):
        record = manager.start()
        for index in range(1, 8):
            manager.record_pick(record, f"p{index}")
        status = manager.status(record, {p.player_id: p for p in repos.players.all()})
        assert status.user_player_ids == ["p7"]
        assert status.user_roster.positions == {"RB": 1}

    def test_availability_target_is_the_next_turn(self, manager, repos):
        record = manager.start()
        for index in range(1, 7):
            manager.record_pick(record, f"p{index}")
        status = manager.status(record, {p.player_id: p for p in repos.players.all()})
        assert status.is_user_on_the_clock
        # On the clock at 7, the question is whether a player returns at 14.
        assert manager.target_pick_for_availability(status) == 14

    def test_undo(self, manager, repos):
        record = manager.start()
        manager.record_pick(record, "p1")
        manager.record_pick(record, "p2")
        assert manager.undo(record).player_id == "p2"
        with pytest.raises(DraftStateError):
            manager.undo(record)
            manager.undo(record)

    def test_unknown_picks_keep_the_clock_aligned(self, manager, repos):
        record = manager.start()
        for _ in range(6):
            manager.record_pick(record, None)
        status = manager.status(record, {})
        assert status.current_pick == 7
        assert status.is_user_on_the_clock

    def test_explicit_pick_number(self, manager, repos):
        record = manager.start()
        pick = manager.record_pick(record, "p1", overall_pick=25)
        assert pick.round_number == 3
        assert pick.slot == 5

    def test_pick_past_the_end_rejected(self, manager):
        record = manager.start()
        with pytest.raises(DraftStateError, match="past the end"):
            manager.record_pick(record, "p1", overall_pick=10_000)

    def test_complete_draft_reports_done(self, manager, repos, league):
        record = manager.start(rounds=1, teams=3, user_slot=2)
        for index in range(1, 4):
            manager.record_pick(record, f"p{index}")
        status = manager.status(record, {})
        assert status.is_complete
        assert manager.target_pick_for_availability(status) is None


class TestSimulator:
    def _players(self, count: int = 60) -> list[SimPlayer]:
        positions = ["RB", "WR", "QB", "TE", "WR", "RB"]
        return [
            SimPlayer(
                player_id=f"p{index}",
                position=positions[index % len(positions)],
                name=f"Player {index}",
                points=300 - index * 2,
                vor=200 - index * 2,
                adp=float(index),
                sigma=4.0,
            )
            for index in range(1, count + 1)
        ]

    def test_seeded_runs_are_reproducible(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        players = self._players()
        first = simulator.simulate_to_pick(
            players, current_pick=1, target_pick=13, teams=10, seed=99
        )
        second = simulator.simulate_to_pick(
            players, current_pick=1, target_pick=13, teams=10, seed=99
        )
        assert first.availability["p20"].probability == second.availability["p20"].probability

    def test_different_seeds_differ(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        players = self._players()
        a = simulator.simulate_to_pick(
            players, current_pick=1, target_pick=13, teams=10, seed=1
        )
        b = simulator.simulate_to_pick(
            players, current_pick=1, target_pick=13, teams=10, seed=2
        )
        assert [r.probability for r in a.availability.values()] != [
            r.probability for r in b.availability.values()
        ]

    def test_availability_decreases_with_adp(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        result = simulator.simulate_to_pick(
            self._players(), current_pick=1, target_pick=13, teams=10,
            iterations=400, seed=5,
        )
        early = result.availability["p2"].probability
        late = result.availability["p40"].probability
        assert early < 0.2
        assert late > 0.9
        assert early < late

    def test_availability_is_a_probability(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        result = simulator.simulate_to_pick(
            self._players(), current_pick=1, target_pick=13, teams=10,
            iterations=200, seed=5,
        )
        assert all(0.0 <= entry.probability <= 1.0 for entry in result.availability.values())

    def test_no_intervening_picks_means_certain(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        result = simulator.simulate_to_pick(
            self._players(), current_pick=7, target_pick=7, teams=10,
            iterations=20, seed=1,
        )
        assert all(entry.probability == 1.0 for entry in result.availability.values())

    def test_adp_signal_survives_the_need_adjustment(self, league, settings):
        """A regression guard: multiplicative need shifts once swamped ADP."""
        settings.app.simulation.need_weight = 1.0
        simulator = DraftSimulator(league, settings.app.simulation)
        result = simulator.simulate_to_pick(
            self._players(), current_pick=1, target_pick=11, teams=10,
            iterations=400, seed=3,
        )
        assert result.availability["p1"].probability < 0.25

    def test_positional_caps_are_respected(self, league, settings):
        """No simulated team should take a third QB in a one-QB league."""
        simulator = DraftSimulator(league, settings.app.simulation)
        qbs = [
            SimPlayer(f"qb{i}", "QB", f"QB{i}", 300 - i, 100 - i, float(i), 2.0)
            for i in range(1, 25)
        ]
        # Only quarterbacks available: teams must stop at their cap.
        result = simulator.simulate_to_pick(
            qbs, current_pick=1, target_pick=40, teams=10, iterations=50, seed=4
        )
        cap = simulator._position_caps()["QB"]
        assert cap <= 2
        # With 10 teams capped at `cap` QBs, at most 10*cap can be drafted.
        taken = sum(1 for entry in result.availability.values() if entry.probability < 0.5)
        assert taken <= 10 * cap

    def test_need_weight_zero_is_pure_adp(self, league, settings):
        settings.app.simulation.need_weight = 0.0
        simulator = DraftSimulator(league, settings.app.simulation)
        result = simulator.simulate_to_pick(
            self._players(), current_pick=1, target_pick=11, teams=10,
            iterations=300, seed=8,
        )
        assert result.availability["p1"].probability < 0.2

    def test_drafter_profile_shifts_a_position(self, league, settings):
        simulator = DraftSimulator(
            league,
            settings.app.simulation,
            profiles=[DrafterProfile(slot=2, position_bias={"TE": -50.0})],
        )
        neutral = DraftSimulator(league, settings.app.simulation)
        players = self._players()
        with_bias = simulator.simulate_to_pick(
            players, current_pick=1, target_pick=11, teams=10, iterations=300, seed=11
        )
        without = neutral.simulate_to_pick(
            players, current_pick=1, target_pick=11, teams=10, iterations=300, seed=11
        )
        te_ids = [p.player_id for p in players if p.position == "TE"][:3]
        biased_survival = sum(with_bias.availability[pid].probability for pid in te_ids)
        neutral_survival = sum(without.availability[pid].probability for pid in te_ids)
        assert biased_survival < neutral_survival

    def test_build_players_imputes_missing_adp(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        records = {"a": ADPRecord(player_id="a", season=2026, source="t", adp=10.0)}
        built = simulator.build_players(
            [("a", "RB", "A", 250.0, 100.0), ("b", "RB", "B", 200.0, 50.0)], records
        )
        by_id = {player.player_id: player for player in built}
        assert by_id["a"].adp == 10.0
        assert by_id["b"].adp > 10.0  # imputed past the deepest known ADP

    def test_published_stdev_wins_over_the_default(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        records = {
            "a": ADPRecord(player_id="a", season=2026, source="t", adp=50.0, stdev=1.5)
        }
        built = simulator.build_players([("a", "RB", "A", 250.0, 100.0)], records)
        assert built[0].sigma == 1.5

    def test_strategy_comparison_ranks_candidates(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        players = self._players()
        outcomes = simulator.compare_strategies(
            players, ["p1", "p30"], current_pick=1, teams=10, rounds=6,
            user_slot=1, iterations=25, seed=3,
        )
        assert len(outcomes) == 2
        # Taking the best player available should not be worse on average.
        by_id = {outcome.player_id: outcome for outcome in outcomes}
        assert by_id["p1"].mean_starter_points >= by_id["p30"].mean_starter_points
        assert all(outcome.iterations == 25 for outcome in outcomes)

    def test_strategy_comparison_is_reproducible(self, league, settings):
        simulator = DraftSimulator(league, settings.app.simulation)
        players = self._players()
        kwargs = {
            "current_pick": 1, "teams": 10, "rounds": 5,
            "user_slot": 1, "iterations": 15, "seed": 42,
        }
        first = simulator.compare_strategies(players, ["p1"], **kwargs)
        second = simulator.compare_strategies(players, ["p1"], **kwargs)
        assert first[0].mean_starter_points == second[0].mean_starter_points


class TestKeepers:
    """Keepers declared in YAML must land on the board as already-gone players."""

    @pytest.fixture
    def keeper_league(self):
        from fantasy_ai.config import LeagueConfig

        return LeagueConfig(
            name="Keeper League",
            season=2026,
            teams=10,
            type="keeper",
            roster={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BENCH": 6},
            draft={"type": "snake", "position": 4, "rounds": 13},
            keepers={
                "enabled": True,
                "max_keepers": 2,
                "cost_rule": "round_penalty",
                "keepers": {"Kept Back": 3, "Kept Wideout": 7},
            },
        )

    @pytest.fixture
    def manager(self, repos, keeper_league) -> DraftStateManager:
        repos.players.upsert(
            [
                make_player("k1", "Kept Back", "RB"),
                make_player("k2", "Kept Wideout", "WR"),
                *[make_player(f"p{i}", f"Player {i}", "RB") for i in range(1, 30)],
            ]
        )
        return DraftStateManager(repos, keeper_league)

    def _resolver(self, repos):
        def resolve(name: str) -> str | None:
            from fantasy_ai.normalization.identity import normalize_name

            matches = repos.players.find_by_normalized_name(normalize_name(name))
            return matches[0].player_id if len(matches) == 1 else None

        return resolve

    def test_keepers_land_on_the_users_picks(self, manager, repos):
        record = manager.start()
        results = manager.apply_keepers(record, self._resolver(repos))
        assert [(name, rnd) for name, rnd, _ in results] == [
            ("Kept Back", 3), ("Kept Wideout", 7)
        ]

        picks = {pick.overall_pick: pick for pick in repos.drafts.picks(record.draft_id)}
        slot_picks = manager.user_picks(record)
        assert slot_picks[2] in picks       # round 3
        assert slot_picks[6] in picks       # round 7
        assert all(pick.keeper for pick in picks.values())
        assert all(pick.is_user for pick in picks.values())
        assert all(pick.source == "keeper" for pick in picks.values())

    def test_kept_players_are_on_the_user_roster(self, manager, repos):
        record = manager.start()
        manager.apply_keepers(record, self._resolver(repos))
        status = manager.status(record, {p.player_id: p for p in repos.players.all()})
        assert set(status.user_player_ids) == {"k1", "k2"}
        assert status.user_roster.positions == {"RB": 1, "WR": 1}

    def test_unresolvable_keeper_is_reported_not_fatal(self, manager, repos, keeper_league):
        keeper_league.keepers.keepers = {"Kept Back": 3, "Nobody At All": 5}
        record = manager.start()
        results = manager.apply_keepers(record, self._resolver(repos))
        assert [player_id for _, _, player_id in results] == ["k1", None]
        assert len(repos.drafts.picks(record.draft_id)) == 1

    def test_keeper_round_outside_the_draft_is_an_error(self, manager, repos, keeper_league):
        keeper_league.keepers.keepers = {"Kept Back": 99}
        record = manager.start()
        with pytest.raises(DraftStateError, match="outside"):
            manager.apply_keepers(record, self._resolver(repos))

    def test_no_keepers_configured_is_a_no_op(self, repos, league):
        repos.players.upsert([make_player("p1", "A", "RB")])
        manager = DraftStateManager(repos, league)
        record = manager.start()
        assert manager.apply_keepers(record, lambda name: None) == []

    def test_the_draft_clock_still_starts_at_pick_one(self, manager, repos):
        """Keepers occupy later rounds; the draft has not begun."""
        record = manager.start()
        manager.apply_keepers(record, self._resolver(repos))
        status = manager.status(record, {})
        assert status.current_pick == 1
