"""Replacement level, VOR, lineups, tiers, scarcity, market, risk, draft score."""

from __future__ import annotations

import pytest

from fantasy_ai.analytics.availability import AvailabilityModel
from fantasy_ai.analytics.common import ScoredPlayer
from fantasy_ai.analytics.draft_score import (
    compare_scores,
    compute_draft_score,
    expected_best_alternative_vor,
)
from fantasy_ai.analytics.lineup import expand_slots, optimal_lineup
from fantasy_ai.analytics.market import BoardValueCurve, compute_adp_values, compute_consensus
from fantasy_ai.analytics.replacement import (
    compute_replacement_levels,
    simulate_starter_pool,
)
from fantasy_ai.analytics.risk import (
    compute_durability,
    compute_risk,
    injury_severity,
    is_inactive,
)
from fantasy_ai.analytics.roster_fit import compute_needs, compute_roster_fit
from fantasy_ai.analytics.scarcity import compute_scarcity
from fantasy_ai.analytics.tiers import compute_tiers
from fantasy_ai.analytics.vor import compute_vor
from fantasy_ai.config import LeagueConfig, ReplacementConfig, SimulationConfig, TierConfig
from fantasy_ai.models import ADPRecord, RankingRecord, SeasonHistoryRecord

from .conftest import make_player, scored


class TestStarterPool:
    def test_demand_exceeds_dedicated_slots_because_of_flex(self, league, board_players):
        _, demand, _ = simulate_starter_pool(board_players, league)
        # 10 teams x 2 RB = 20 dedicated, plus a share of the 10 flex slots.
        assert demand["RB"] > 20
        assert demand["WR"] > 20
        assert demand["RB"] + demand["WR"] + demand["TE"] == 20 + 20 + 10 + 10

    def test_one_qb_league_demands_exactly_one_qb_per_team(self, league, board_players):
        _, demand, _ = simulate_starter_pool(board_players, league)
        assert demand["QB"] == league.teams

    def test_superflex_deepens_qb_demand(self, superflex_league, board_players):
        _, demand, _ = simulate_starter_pool(board_players, superflex_league)
        assert demand["QB"] > superflex_league.teams

    def test_every_starting_slot_gets_filled(self, league, board_players):
        pool, demand, _ = simulate_starter_pool(board_players, league)
        assert sum(demand.values()) == league.starters_per_team * league.teams
        assert sum(len(ids) for ids in pool.values()) == sum(demand.values())

    def test_scoring_changes_flex_allocation(self, board_players):
        """PPR should pull more receivers into flex than a standard league would."""
        standard = LeagueConfig(
            season=2026, teams=10,
            roster={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BENCH": 6},
        )
        # Simulate a PPR world by boosting every receiver's projection.
        ppr_players = [
            ScoredPlayer(p.player_id, p.position, p.points + (60 if p.position == "WR" else 0),
                         p.name)
            for p in board_players
        ]
        _, base_demand, _ = simulate_starter_pool(board_players, standard)
        _, ppr_demand, _ = simulate_starter_pool(ppr_players, standard)
        assert ppr_demand["WR"] > base_demand["WR"]


class TestReplacementLevels:
    def test_replacement_sits_just_outside_the_starter_pool(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        rb = levels.by_position["RB"]
        assert rb.rank == rb.demand + 1
        assert rb.points > 0
        assert len(rb.window_players) == ReplacementConfig().window

    def test_window_averages_rather_than_picking_one_player(self, league, board_players):
        wide = compute_replacement_levels(
            board_players, league, ReplacementConfig(window=5)
        )
        narrow = compute_replacement_levels(
            board_players, league, ReplacementConfig(window=1)
        )
        assert wide.by_position["RB"].points != narrow.by_position["RB"].points
        assert len(wide.by_position["RB"].window_players) == 5

    def test_fixed_rank_method(self, league, board_players):
        levels = compute_replacement_levels(
            board_players, league,
            ReplacementConfig(method="fixed_rank", fixed_ranks={"RB": 25, "WR": 30},
                              window=1),
        )
        assert levels.by_position["RB"].rank == 25
        assert levels.by_position["RB"].method == "fixed_rank"

    def test_blended_method_between_the_two(self, league, board_players):
        simulated = compute_replacement_levels(board_players, league)
        fixed_rank = 40
        blended = compute_replacement_levels(
            board_players, league,
            ReplacementConfig(method="blended", fixed_ranks={"RB": fixed_rank},
                             blend_weight=0.5, window=1),
        )
        low = min(simulated.by_position["RB"].rank, fixed_rank)
        high = max(simulated.by_position["RB"].rank, fixed_rank)
        assert low <= blended.by_position["RB"].rank <= high

    def test_position_without_a_slot_gets_no_demand(self, board_players):
        no_kicker = LeagueConfig(
            season=2026, teams=10,
            roster={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BENCH": 6},
        )
        levels = compute_replacement_levels(board_players, no_kicker)
        kicker = levels.by_position["K"]
        assert not kicker.has_starting_demand
        assert kicker.points == max(p.points for p in board_players if p.position == "K")

    def test_explanations_mention_the_derivation(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        text = " ".join(levels.explain())
        assert "league-wide starters" in text
        assert "replacement" in text


class TestVOR:
    def test_vor_is_points_minus_replacement(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        results = compute_vor(board_players, levels, league)
        for player in board_players:
            result = results[player.player_id]
            assert result.vor == pytest.approx(
                player.points - levels.points_for(player.position)
            )

    def test_vor_corrects_the_raw_points_ordering(self, league, board_players):
        """Top QB outscores top RB in raw points, but not in VOR in a 1-QB league."""
        levels = compute_replacement_levels(board_players, league)
        results = compute_vor(board_players, levels, league)
        assert results["qb1"].points > results["rb1"].points
        assert results["rb1"].vor > results["qb1"].vor

    def test_flex_vor_only_for_flex_eligible(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        results = compute_vor(board_players, levels, league)
        assert results["rb1"].flex_eligible
        assert results["rb1"].flex_vor is not None
        assert not results["qb1"].flex_eligible
        assert results["qb1"].flex_vor is None


class TestOptimalLineup:
    def test_fills_flex_with_best_leftover(self, league):
        roster = [
            ScoredPlayer("q", "QB", 330), ScoredPlayer("r1", "RB", 250),
            ScoredPlayer("r2", "RB", 220), ScoredPlayer("r3", "RB", 200),
            ScoredPlayer("w1", "WR", 240), ScoredPlayer("w2", "WR", 180),
            ScoredPlayer("w3", "WR", 175), ScoredPlayer("t", "TE", 150),
            ScoredPlayer("k", "K", 140), ScoredPlayer("d", "DST", 120),
        ]
        lineup = optimal_lineup(roster, league)
        assert lineup.points == pytest.approx(330 + 250 + 220 + 240 + 180 + 150 + 200 + 140 + 120)
        assert [p.player_id for p in lineup.bench] == ["w3"]

    def test_handles_crossing_eligibility_exactly(self):
        league = LeagueConfig(
            season=2026, teams=10,
            roster={"WR": 1, "FLEX": 1, "WRT": 1, "BENCH": 3},
            flex={"allowed_positions": ["RB", "WR", "TE"], "slots": {"WRT": ["WR", "TE"]}},
        )
        players = [
            ScoredPlayer("w1", "WR", 100), ScoredPlayer("r1", "RB", 95),
            ScoredPlayer("t1", "TE", 90), ScoredPlayer("r2", "RB", 85),
        ]
        # The only feasible maximum keeps RB out of WRT: WR->WR, RB->FLEX, TE->WRT.
        assert optimal_lineup(players, league).points == pytest.approx(285)

    def test_superflex_starts_a_second_quarterback(self, superflex_league):
        players = [
            ScoredPlayer("q1", "QB", 380), ScoredPlayer("q2", "QB", 350),
            ScoredPlayer("r1", "RB", 250), ScoredPlayer("r2", "RB", 240),
            ScoredPlayer("w1", "WR", 230), ScoredPlayer("w2", "WR", 220),
            ScoredPlayer("w3", "WR", 210), ScoredPlayer("t1", "TE", 180),
            ScoredPlayer("r3", "RB", 200),
        ]
        lineup = optimal_lineup(players, superflex_league)
        starters = {p.player_id for p in lineup.assignments.values()}
        assert {"q1", "q2"} <= starters

    def test_partial_roster_leaves_slots_open(self, league):
        lineup = optimal_lineup([ScoredPlayer("q", "QB", 300)], league)
        assert lineup.filled == 1
        assert len(lineup.open_slots) == league.starters_per_team - 1
        assert "RB" in lineup.positions_needed()

    def test_empty_roster(self, league):
        lineup = optimal_lineup([], league)
        assert lineup.points == 0
        assert lineup.open_slot_counts()["RB"] == 2

    def test_negative_projection_never_starts(self, league):
        lineup = optimal_lineup([ScoredPlayer("q", "QB", -5)], league)
        assert lineup.points == 0
        assert lineup.filled == 0

    def test_slots_expand_by_count(self, league):
        slots = expand_slots(league)
        assert len(slots) == league.starters_per_team
        assert sum(1 for slot in slots if slot.name == "RB") == 2


class TestTiers:
    def test_break_at_a_large_gap(self):
        players = scored("te", "TE", [230, 195, 175, 160, 152, 146, 140, 135])
        result = compute_tiers(players, TierConfig(sensitivity=0.8, min_gap_points=1.0))
        assert result.tier_of("te1") == 1
        assert result.tier_of("te1") < result.tier_of("te8")

    def test_flat_position_stays_in_few_tiers(self):
        players = scored("k", "K", [150, 149, 148, 147, 146, 145, 144, 143])
        result = compute_tiers(players, TierConfig(min_gap_points=5.0, max_tier_size=20))
        assert len(result.tiers_by_position["K"]) == 1

    def test_max_tier_size_forces_a_break(self):
        players = scored("wr", "WR", [200 - index for index in range(20)])
        result = compute_tiers(players, TierConfig(max_tier_size=5, min_gap_points=50))
        assert all(tier.size <= 5 for tier in result.tiers_by_position["WR"])

    def test_player_tier_carries_context(self):
        players = scored("te", "TE", [230, 195, 175, 160, 152, 146])
        result = compute_tiers(players, TierConfig(sensitivity=0.8))
        entry = result.player_tiers["te1"]
        assert entry.players_left_in_tier >= 1
        assert entry.next_player_delta == pytest.approx(35)

    def test_break_gap_is_explainable(self):
        players = scored("te", "TE", [230, 195, 175, 160, 152, 146])
        result = compute_tiers(players, TierConfig(sensitivity=0.8))
        tiers = result.tiers_by_position["TE"]
        assert tiers[0].explain()
        if len(tiers) > 1:
            assert tiers[1].break_gap > 0
            assert "drop" in tiers[1].explain()

    def test_single_player_position(self):
        result = compute_tiers(scored("te", "TE", [200]))
        assert result.tier_of("te1") == 1


class TestScarcity:
    def test_supply_ratio_and_decay(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        tiers = compute_tiers(board_players)
        result = compute_scarcity(board_players, levels, tiers)
        rb = result.by_position["RB"]
        assert rb.above_replacement_count > 0
        assert rb.average_decay > 0
        assert 0 <= result.by_player["rb1"].position_scarcity_index <= 1

    def test_dropoff_increases_with_distance(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        tiers = compute_tiers(board_players)
        result = compute_scarcity(board_players, levels, tiers)
        entry = result.by_player["rb1"]
        assert entry.next_player_delta <= entry.dropoff_3 <= entry.dropoff_5 <= entry.dropoff_10

    def test_scarcity_reflects_remaining_demand(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        tiers = compute_tiers(board_players)
        plenty = compute_scarcity(
            board_players, levels, tiers, remaining_demand={"RB": 1, "WR": 1}
        )
        tight = compute_scarcity(
            board_players, levels, tiers, remaining_demand={"RB": 40, "WR": 1}
        )
        assert (
            tight.by_position["RB"].starter_supply_ratio
            < plenty.by_position["RB"].starter_supply_ratio
        )


class TestMarketValue:
    def test_board_curve_interpolates_and_clamps(self):
        curve = BoardValueCurve([100.0, 80.0, 60.0, 40.0])
        assert curve.at(1) == 100
        assert curve.at(2.5) == pytest.approx(70)
        assert curve.at(0) == 100      # clamped low
        assert curve.at(99) == 40      # clamped high

    def test_adp_value_is_bounded_by_board_span(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        vor = compute_vor(board_players, levels, league)
        vor_by_id = {pid: result.vor for pid, result in vor.items()}
        ranks = {
            player.player_id: index
            for index, player in enumerate(
                sorted(board_players, key=lambda p: -vor_by_id[p.player_id]), start=1
            )
        }
        # A kicker whose ADP is far below his VOR rank must not produce a
        # fictitious enormous discount.
        adp_records = {
            "k1": ADPRecord(player_id="k1", season=2026, source="test", adp=200.0)
        }
        values = compute_adp_values(board_players, ranks, adp_records, vor_by_id)
        span = max(vor_by_id.values()) - min(vor_by_id.values())
        assert abs(values["k1"].points_value) <= span

    def test_positive_rank_difference_means_falling(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        vor = compute_vor(board_players, levels, league)
        vor_by_id = {pid: result.vor for pid, result in vor.items()}
        # Our board order is VOR descending, which puts rb1 near the very top.
        ranks = {
            player.player_id: index
            for index, player in enumerate(
                sorted(board_players, key=lambda p: -vor_by_id[p.player_id]), start=1
            )
        }
        assert ranks["rb1"] == 1
        records = {"rb1": ADPRecord(player_id="rb1", season=2026, source="t", adp=14.0)}
        value = compute_adp_values(board_players, ranks, records, vor_by_id)["rb1"]
        assert value.rank_difference == pytest.approx(13.0)
        assert value.label() == "clear value"

    def test_consensus_index_is_relative_to_rank(self):
        rankings = {
            f"p{index}": RankingRecord(
                player_id=f"p{index}", season=2026, source="t",
                ecr=float(index), average=float(index), stdev=index * 0.4,
            )
            for index in range(1, 60)
        }
        # One player with double the usual spread for his rank.
        rankings["p30"].stdev = 30 * 0.4 * 2.5
        profiles = compute_consensus(rankings, {f"p{i}": i for i in range(1, 60)})
        assert profiles["p30"].disagreement_index > 2.0
        assert profiles["p30"].label() == "polarising"
        # A typical player is near 1.0, not automatically "polarising".
        assert 0.6 < profiles["p20"].disagreement_index < 1.6

    def test_consensus_without_data(self):
        assert compute_consensus({}, {}) == {}


class TestAvailability:
    def test_probability_falls_with_distance(self):
        model = AvailabilityModel(SimulationConfig())
        assert model.probability(10, 5) > 0.9
        assert model.probability(10, 10) == pytest.approx(0.5, abs=0.1)
        assert model.probability(10, 30) < 0.05

    def test_published_stdev_is_used(self):
        model = AvailabilityModel(SimulationConfig())
        tight = model.probability(20, 25, 1.0)
        loose = model.probability(20, 25, 20.0)
        assert loose > tight

    def test_sigma_respects_floor_and_ceiling(self):
        model = AvailabilityModel(SimulationConfig(adp_noise_floor=3, adp_noise_ceiling=20))
        assert model.sigma_for(1) == 3
        assert model.sigma_for(500) == 20

    def test_missing_adp_is_imputed_not_guessed(self):
        model = AvailabilityModel(SimulationConfig())
        estimate = model.estimate("x", 50, None, max_known_adp=200)
        assert estimate.probability > 0.9
        assert "imputed" in estimate.method


class TestRosterFit:
    def test_open_slot_gives_zero_adjustment(self, league):
        candidates = [ScoredPlayer("r1", "RB", 250)]
        fits = compute_roster_fit(candidates, [], league, picks_remaining=15)
        assert fits["r1"].adjustment == pytest.approx(0.0)
        assert fits["r1"].label == "high"

    def test_redundant_player_is_penalised(self, league):
        roster = [
            ScoredPlayer("q1", "QB", 340), ScoredPlayer("r1", "RB", 250),
            ScoredPlayer("r2", "RB", 240), ScoredPlayer("r3", "RB", 230),
            ScoredPlayer("w1", "WR", 240), ScoredPlayer("w2", "WR", 230),
            ScoredPlayer("t1", "TE", 180),
        ]
        candidates = [ScoredPlayer("q2", "QB", 300), ScoredPlayer("k1", "K", 150)]
        fits = compute_roster_fit(candidates, roster, league, picks_remaining=8)
        assert fits["q2"].adjustment < -100     # second QB mostly sits
        assert fits["k1"].adjustment == pytest.approx(0.0)   # fills an open slot

    def test_bench_share_controls_depth_value(self, league):
        roster = [ScoredPlayer("q1", "QB", 340)]
        candidates = [ScoredPlayer("q2", "QB", 300)]
        generous = compute_roster_fit(
            candidates, roster, league, picks_remaining=14, bench_start_share=0.5
        )
        stingy = compute_roster_fit(
            candidates, roster, league, picks_remaining=14, bench_start_share=0.0
        )
        assert generous["q2"].adjustment > stingy["q2"].adjustment

    def test_forced_fill_penalty_at_the_end_of_the_draft(self, league):
        roster = [
            ScoredPlayer("q1", "QB", 340), ScoredPlayer("r1", "RB", 250),
            ScoredPlayer("r2", "RB", 240), ScoredPlayer("w1", "WR", 240),
            ScoredPlayer("w2", "WR", 230), ScoredPlayer("t1", "TE", 180),
            ScoredPlayer("f1", "RB", 200),
        ]
        # Two picks left; K and DST are both unfilled starting slots. w3 is worse
        # than the RB already occupying FLEX, so he cannot improve the lineup.
        candidates = [ScoredPlayer("w3", "WR", 190), ScoredPlayer("k1", "K", 150)]
        fits = compute_roster_fit(
            candidates, roster, league, picks_remaining=2,
            best_available={"K": 150, "DST": 130, "WR": 190},
        )
        assert not fits["w3"].fills_open_slot
        assert fits["w3"].forced_fill_penalty < 0
        assert fits["k1"].forced_fill_penalty == 0

    def test_needs_reports_open_slots(self, league):
        needs = compute_needs(
            [ScoredPlayer("q1", "QB", 340)], league, picks_remaining=10
        )
        assert needs.open_slots["RB"] == 2
        assert "QB" not in needs.open_slots
        assert needs.slack == 10 - needs.starting_slots_open


class TestRisk:
    def test_injury_designation_charges_points(self):
        healthy = compute_risk(make_player("a", "A", "RB"), projected_points=200)
        hurt = compute_risk(
            make_player("b", "B", "RB", injury_status="Doubtful"), projected_points=200
        )
        assert hurt.discount_points > healthy.discount_points
        assert hurt.component("injury") is not None

    def test_age_charge_is_position_specific(self):
        old_rb = compute_risk(make_player("a", "A", "RB", age=31), projected_points=200)
        old_qb = compute_risk(make_player("b", "B", "QB", age=31), projected_points=200)
        assert old_rb.component("age") is not None
        assert old_qb.component("age") is None

    def test_rookie_uncertainty(self):
        rookie = compute_risk(
            make_player("a", "A", "WR", years_exp=0, age=22), projected_points=180
        )
        assert rookie.component("role_uncertainty") is not None

    def test_discount_is_capped(self):
        from fantasy_ai.config import RiskConfig

        profile = compute_risk(
            make_player("a", "A", "RB", age=40, injury_status="IR", years_exp=0),
            projected_points=300,
            config=RiskConfig(max_discount_points=10),
        )
        assert profile.discount_points == 10
        assert profile.capped

    def test_severity_lookup(self):
        assert injury_severity("Questionable") == 0.3
        assert injury_severity(None) == 0.0
        assert injury_severity("Totally Made Up") == 0.0

    def test_inactive_players_detected(self):
        assert is_inactive(make_player("a", "A", "RB", injury_status="IR"), None)
        assert not is_inactive(make_player("b", "B", "RB", injury_status="Questionable"), None)

    def test_risk_never_alters_projection(self):
        """Risk is a separate discount; the projection object is untouched."""
        player = make_player("a", "A", "RB", age=33)
        profile = compute_risk(player, projected_points=200)
        assert profile.discount_points > 0
        assert player.age == 33  # nothing mutated


class TestDraftScore:
    def _vor(self, league, board_players):
        levels = compute_replacement_levels(board_players, league)
        return compute_vor(board_players, levels, league)

    def test_expected_best_alternative_exact_expectation(self):
        # A certain option truncates the sum at its own value.
        assert expected_best_alternative_vor([("a", 50.0, 1.0), ("b", 40.0, 1.0)]) == 50.0
        # Independent 50/50s: 50*0.5 + 40*0.5*0.5 = 35
        assert expected_best_alternative_vor(
            [("a", 50.0, 0.5), ("b", 40.0, 0.5)]
        ) == pytest.approx(35.0)
        assert expected_best_alternative_vor([]) == 0.0

    def test_urgency_vanishes_when_certain_to_return(self, league, board_players):
        vor = self._vor(league, board_players)
        player = next(p for p in board_players if p.player_id == "rb1")
        alternatives = [("rb2", vor["rb2"].vor, 1.0), ("rb3", vor["rb3"].vor, 1.0)]
        certain = compute_draft_score(
            player, vor=vor["rb1"], availability=1.0, alternatives=alternatives
        )
        gone = compute_draft_score(
            player, vor=vor["rb1"], availability=0.0, alternatives=alternatives
        )
        assert certain.contribution("urgency") == pytest.approx(0.0)
        assert gone.contribution("urgency") > 0

    def test_urgency_is_capped_by_the_gap_to_the_fallback(self, league, board_players):
        vor = self._vor(league, board_players)
        player = next(p for p in board_players if p.player_id == "rb1")
        alternatives = [("rb2", vor["rb2"].vor, 1.0)]
        score = compute_draft_score(
            player, vor=vor["rb1"], availability=0.0, alternatives=alternatives
        )
        assert score.contribution("urgency") == pytest.approx(
            vor["rb1"].vor - vor["rb2"].vor
        )

    def test_components_sum_to_total(self, league, board_players):
        vor = self._vor(league, board_players)
        player = next(p for p in board_players if p.player_id == "wr1")
        score = compute_draft_score(
            player, vor=vor["wr1"], availability=0.4,
            alternatives=[("wr2", vor["wr2"].vor, 0.6)],
            risk=compute_risk(make_player("wr1", "WR1", "WR"), projected_points=280),
        )
        assert score.total == pytest.approx(
            sum(component.contribution for component in score.components)
        )
        assert all(component.description for component in score.components)

    def test_market_shrinkage_is_capped(self, league, board_players):
        from fantasy_ai.analytics.market import ADPValue

        vor = self._vor(league, board_players)
        player = next(p for p in board_players if p.player_id == "k1")
        huge = ADPValue(
            player_id="k1", our_rank=40, adp=220.0, adp_source="t",
            rank_difference=180.0, points_value=400.0,
        )
        score = compute_draft_score(
            player, vor=vor["k1"], availability=1.0, alternatives=[],
            adp_value=huge, max_market_points=8.0,
        )
        assert score.contribution("market") == pytest.approx(-8.0)

    def test_market_shades_toward_consensus(self, league, board_players):
        from fantasy_ai.analytics.market import ADPValue

        vor = self._vor(league, board_players)
        player = next(p for p in board_players if p.player_id == "rb5")
        above = ADPValue("rb5", 20, 60.0, "t", 40.0, points_value=20.0)
        below = ADPValue("rb5", 20, 5.0, "t", -15.0, points_value=-20.0)
        high = compute_draft_score(player, vor=vor["rb5"], availability=1.0,
                                   alternatives=[], adp_value=above)
        low = compute_draft_score(player, vor=vor["rb5"], availability=1.0,
                                  alternatives=[], adp_value=below)
        # We are above the market on `above`, so we shade down.
        assert high.contribution("market") < 0 < low.contribution("market")

    def test_comparison_explains_the_difference(self, league, board_players):
        vor = self._vor(league, board_players)
        left = compute_draft_score(
            next(p for p in board_players if p.player_id == "rb1"),
            vor=vor["rb1"], availability=0.2, alternatives=[("rb2", vor["rb2"].vor, 0.8)],
        )
        right = compute_draft_score(
            next(p for p in board_players if p.player_id == "wr1"),
            vor=vor["wr1"], availability=0.9, alternatives=[("wr2", vor["wr2"].vor, 0.9)],
        )
        lines = compare_scores(left, right)
        assert lines[0].startswith("Total:")
        assert any("value" in line for line in lines)


class TestDurability:
    """Recency-weighted availability from completed seasons.

    The only risk component with a memory: everything else describes the player
    as he is today, so a currently-healthy player who has broken down twice is
    otherwise indistinguishable from one who never has.
    """

    @staticmethod
    def _history(*seasons: tuple[int, int]) -> list[SeasonHistoryRecord]:
        return [
            SeasonHistoryRecord(
                player_id="p1", season=year, source="cheatsheet",
                games_played=games, games_possible=17,
            )
            for year, games in seasons
        ]

    def test_a_full_slate_is_nearly_free(self):
        profile = compute_durability(self._history((2025, 17), (2024, 17), (2023, 17)))
        assert profile is not None
        assert profile.availability == pytest.approx(1.0)
        assert profile.missed_share == pytest.approx(0.0)

    def test_recent_injuries_cost_more_than_old_ones(self):
        recent = compute_durability(self._history((2025, 9), (2024, 17), (2023, 17)))
        old = compute_durability(self._history((2025, 17), (2024, 17), (2023, 9)))
        assert recent is not None and old is not None
        # Same games missed, different years. A three-year-old injury says less
        # about this season than last year's does.
        assert recent.missed_share > old.missed_share

    def test_chronic_beats_one_bad_year(self):
        chronic = compute_durability(self._history((2025, 9), (2024, 12), (2023, 11)))
        one_off = compute_durability(self._history((2025, 16), (2024, 17), (2023, 9)))
        assert chronic is not None and one_off is not None
        assert chronic.missed_share > one_off.missed_share

    def test_too_little_history_yields_no_opinion(self):
        # Absence of history is not evidence of durability. A rookie must not be
        # credited with a clean bill of health he has not earned, and one season
        # cannot separate a freak injury from a pattern.
        assert compute_durability([]) is None
        assert compute_durability(self._history((2025, 17))) is None
        assert compute_durability(self._history((2025, 17)), min_seasons=1) is not None

    def test_only_the_weighted_seasons_are_considered(self):
        # Three weights means three seasons, however many are supplied.
        profile = compute_durability(
            self._history((2025, 17), (2024, 17), (2023, 17), (2022, 1), (2021, 1))
        )
        assert profile is not None
        assert profile.seasons_used == 3
        assert profile.availability == pytest.approx(1.0)

    def test_risk_charges_for_it_and_explains_itself(self):
        fragile = compute_risk(
            make_player("p1", "Fragile Guy", "RB", age=25.0),
            history=self._history((2025, 9), (2024, 12), (2023, 11)),
        )
        durable = compute_risk(
            make_player("p2", "Iron Guy", "RB", age=25.0),
            history=self._history((2025, 17), (2024, 17), (2023, 17)),
        )
        assert fragile.discount_points > durable.discount_points
        component = fragile.component("durability")
        assert component is not None
        assert "9/17" in component.note

    def test_no_history_means_no_durability_charge(self):
        profile = compute_risk(make_player("p3", "Unknown Guy", "RB", age=25.0))
        assert profile.component("durability") is None
