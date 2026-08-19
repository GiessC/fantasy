"""League-adjusted scoring."""

from __future__ import annotations

import pytest

from fantasy_ai.analytics.scoring import (
    Scorer,
    expected_games_over,
    expected_repeat_bonus,
    expected_tiered_points,
)
from fantasy_ai.config.ranges import parse_range_table
from fantasy_ai.config.scoring import ScoringConfig
from fantasy_ai.stats import StatLine


@pytest.fixture
def half_ppr() -> Scorer:
    return Scorer(ScoringConfig.model_validate({"receiving": {"reception": 0.5}}).compile())


class TestBasicScoring:
    def test_quarterback_line(self, half_ppr: Scorer):
        line = StatLine({
            "pass_yd": 4500, "pass_td": 32, "pass_int": 11,
            "rush_yd": 420, "rush_td": 5, "fum_lost": 3,
        })
        result = half_ppr.score(line, "QB")
        expected = 4500 * 0.04 + 32 * 4 + 11 * -2 + 420 * 0.1 + 5 * 6 + 3 * -2
        assert result.points == pytest.approx(expected)

    def test_receiver_line_uses_ppr_value(self, half_ppr: Scorer):
        line = StatLine({"rec": 100, "rec_yd": 1300, "rec_td": 9})
        assert half_ppr.score(line, "WR").points == pytest.approx(
            100 * 0.5 + 1300 * 0.1 + 9 * 6
        )

    def test_fractional_scoring_is_exact(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {"receiving": {"reception": 0.35, "points_per_yard": 0.085,
                               "yards_per_point": None}}
            ).compile()
        )
        result = scorer.score(StatLine({"rec": 73, "rec_yd": 942}), "WR")
        assert result.points == pytest.approx(73 * 0.35 + 942 * 0.085)

    def test_breakdown_sums_to_total(self, half_ppr: Scorer):
        line = StatLine({"rec": 80, "rec_yd": 1000, "rec_td": 7, "fum_lost": 2})
        result = half_ppr.score(line, "WR")
        assert sum(result.breakdown().values()) == pytest.approx(result.points)
        assert len(result.explain()) == len(result.lines)

    def test_unscored_categories_are_omitted(self, half_ppr: Scorer):
        result = half_ppr.score(StatLine({"rec_tgt": 150, "rec": 10}), "WR")
        assert "rec_tgt" not in result.breakdown()

    def test_points_per_game(self, half_ppr: Scorer):
        result = half_ppr.score(StatLine({"rec_yd": 1700, "meta_games": 17}), "WR")
        assert result.points_per_game == pytest.approx(10.0)

    def test_zero_stat_line_scores_zero(self, half_ppr: Scorer):
        assert half_ppr.score(StatLine(), "WR").points == 0.0


class TestPositionOverrides:
    def test_te_premium_applies_only_to_te(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {
                    "receiving": {"reception": 0.5},
                    "position_overrides": {"TE": {"receiving": {"reception": 1.5}}},
                }
            ).compile()
        )
        line = StatLine({"rec": 80, "rec_yd": 900})
        te = scorer.score(line, "TE").points
        wr = scorer.score(line, "WR").points
        assert te - wr == pytest.approx(80 * 1.0)

    def test_override_does_not_leak_other_categories(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {
                    "receiving": {"reception": 0.5, "touchdown": 6},
                    "position_overrides": {"TE": {"receiving": {"reception": 1.0}}},
                }
            ).compile()
        )
        assert scorer.scoring.rate("rec_td", "TE") == 6


class TestKicking:
    def test_bucketed_field_goals_scored_by_distance(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {"kicking": {"field_goal": {"ranges": {"0-39": 3, "40-49": 4, "50+": 5}},
                             "extra_point": 1}}
            ).compile()
        )
        line = StatLine({
            "kick_fgm_20_29": 8, "kick_fgm_30_39": 10,
            "kick_fgm_40_49": 9, "kick_fgm_50_plus": 4, "kick_xpm": 40,
        })
        result = scorer.score(line, "K")
        assert result.points == pytest.approx(8 * 3 + 10 * 3 + 9 * 4 + 4 * 5 + 40)

    def test_total_fgs_do_not_double_count_buckets(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {"kicking": {"field_goal": {"ranges": {"0-39": 3, "40-49": 4, "50+": 5}}}}
            ).compile()
        )
        line = StatLine({"kick_fgm": 31, "kick_fgm_30_39": 10, "kick_fgm_40_49": 9,
                         "kick_fgm_50_plus": 4, "kick_fgm_20_29": 8})
        result = scorer.score(line, "K")
        assert "kick_fgm" not in result.breakdown()

    def test_total_only_uses_blended_rate(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {"kicking": {"field_goal": {"ranges": {"0-39": 3, "40-49": 4, "50+": 5}}}}
            ).compile()
        )
        result = scorer.score(StatLine({"kick_fgm": 30}), "K")
        assert 30 * 3 < result.points < 30 * 5
        assert "blended" in (result.lines[0].note or "")


class TestDefenseTiers:
    def test_points_allowed_uses_expected_tier(self):
        scorer = Scorer(
            ScoringConfig.model_validate(
                {
                    "defense": {
                        "sack": 1,
                        "points_allowed": {
                            "0": 10, "1-6": 7, "7-13": 4, "14-20": 1,
                            "21-27": 0, "28-34": -1, "35+": -4,
                        },
                    }
                }
            ).compile()
        )
        # A great defense (14/game) should score more than a bad one (28/game).
        good = scorer.score(StatLine({"dst_pts_allowed": 14 * 17, "meta_games": 17}), "DST")
        bad = scorer.score(StatLine({"dst_pts_allowed": 28 * 17, "meta_games": 17}), "DST")
        assert good.points > bad.points
        assert "most likely tier" in (good.lines[0].note or "")

    def test_tier_expectation_is_bounded_by_the_table(self):
        table = tuple(parse_range_table({"0": 10, "1-6": 7, "7-13": 4, "35+": -4}))
        points, _ = expected_tiered_points(17 * 10, 17, table, 0.45)
        assert -4 * 17 <= points <= 10 * 17

    def test_no_table_means_no_points(self):
        points, note = expected_tiered_points(300, 17, (), 0.45)
        assert points == 0.0
        assert "no tier table" in note


class TestBonuses:
    def _scorer(self, **bonus) -> Scorer:
        payload = {"name": "test", "per_game": True, **bonus}
        return Scorer(
            ScoringConfig.model_validate(
                {"rushing": {"yards_per_point": 10}, "bonuses": [payload]}
            ).compile()
        )

    def test_per_game_bonus_scales_with_volume(self):
        scorer = self._scorer(stat="rush_yd", threshold=100, points=3)
        low = scorer.score(StatLine({"rush_yd": 700, "meta_games": 17}), "RB")
        high = scorer.score(StatLine({"rush_yd": 1700, "meta_games": 17}), "RB")
        low_bonus = low.points - 70
        high_bonus = high.points - 170
        assert 0 <= low_bonus < high_bonus
        assert high_bonus < 17 * 3  # cannot exceed every game clearing it

    def test_season_total_bonus_is_all_or_nothing(self):
        scorer = self._scorer(stat="rush_yd", threshold=1000, points=10, per_game=False)
        under = scorer.score(StatLine({"rush_yd": 900}), "RB")
        over = scorer.score(StatLine({"rush_yd": 1100}), "RB")
        assert under.points == pytest.approx(90)
        assert over.points == pytest.approx(110 + 10)

    def test_bonus_respects_position_filter(self):
        scorer = self._scorer(stat="rush_yd", threshold=100, points=3, positions=["RB"])
        line = StatLine({"rush_yd": 1400, "meta_games": 17})
        assert scorer.score(line, "RB").points > scorer.score(line, "QB").points

    def test_repeating_bonus_accumulates(self):
        scorer = self._scorer(stat="rush_yd", threshold=100, points=1, repeat=True)
        result = scorer.score(StatLine({"rush_yd": 1700, "meta_games": 17}), "RB")
        assert result.points > 170

    def test_expected_games_over_is_monotone_and_bounded(self):
        for total in (500, 1000, 1500, 2000):
            value = expected_games_over(total, 17, 100, 0.55)
            assert 0 <= value <= 17
        assert expected_games_over(1700, 17, 100, 0.55) > expected_games_over(
            900, 17, 100, 0.55
        )

    def test_expected_games_over_degenerate_inputs(self):
        assert expected_games_over(0, 17, 100, 0.5) == 0.0
        assert expected_games_over(1000, 0, 100, 0.5) == 0.0
        assert expected_games_over(1000, 17, 0, 0.5) == 0.0

    def test_repeat_bonus_terminates_for_tiny_thresholds(self):
        assert expected_repeat_bonus(1700, 17, 1, 0.5) > 0


class TestExplainability:
    def test_every_line_has_units_rate_and_points(self, half_ppr: Scorer):
        result = half_ppr.score(
            StatLine({"rec": 90, "rec_yd": 1200, "rec_td": 8, "fum_lost": 1}), "WR"
        )
        for line in result.lines:
            assert line.points == pytest.approx(line.units * line.rate)
            assert line.label
            assert line.describe()

    def test_top_contributors_ordered_by_magnitude(self, half_ppr: Scorer):
        result = half_ppr.score(
            StatLine({"rec": 90, "rec_yd": 1200, "rec_td": 8, "fum_lost": 4}), "WR"
        )
        top = result.top_contributors(3)
        magnitudes = [abs(line.points) for line in top]
        assert magnitudes == sorted(magnitudes, reverse=True)


class TestTeamDefensivePlays:
    """Team-level TFL, forced fumbles, and fourth-down stops.

    These default to 0 because most leagues do not score them, so the test
    that matters is that they contribute exactly what the league configures
    and nothing at all when it does not.
    """

    @staticmethod
    def _scorer(**defense: object) -> Scorer:
        return Scorer(ScoringConfig.model_validate({"defense": defense}).compile())

    def test_each_play_contributes_its_configured_points(self):
        scorer = self._scorer(fourth_down_stop=2, tackle_for_loss=0.5, forced_fumble=1)
        result = scorer.score(
            StatLine(
                {
                    "dst_fourth_down_stop": 6,
                    "dst_tackle_loss": 10,
                    "dst_forced_fum": 4,
                }
            ),
            "DST",
        )
        assert result.points == pytest.approx(6 * 2 + 10 * 0.5 + 4 * 1)

    def test_absent_from_scoring_means_absent_from_the_total(self):
        scorer = self._scorer(sack=1)
        result = scorer.score(
            StatLine({"dst_sack": 3, "dst_fourth_down_stop": 9, "dst_tackle_loss": 20}), "DST"
        )
        assert result.points == pytest.approx(3.0)

    def test_every_line_is_explainable(self):
        # The project's standing rule: components must sum to the total.
        scorer = self._scorer(fourth_down_stop=2, tackle_for_loss=0.5, forced_fumble=1)
        result = scorer.score(
            StatLine({"dst_fourth_down_stop": 3, "dst_tackle_loss": 8, "dst_forced_fum": 2}),
            "DST",
        )
        assert sum(line.points for line in result.lines) == pytest.approx(result.points)
        assert {line.label for line in result.lines} == {
            "DST fourth-down stops",
            "DST tackles for loss",
            "DST forced fumbles",
        }


class TestSpecialTeams:
    """Team-credited and player-credited special teams are separate lines.

    A league that scores both lists them as two rules, so one play must not
    be paid for twice by collapsing them onto a shared key.
    """

    @staticmethod
    def _scorer() -> Scorer:
        return Scorer(
            ScoringConfig.model_validate(
                {
                    "defense": {
                        "special_teams_touchdown": 6,
                        "special_teams_forced_fumble": 1,
                        "special_teams_fumble_recovery": 1,
                    },
                    "special_teams_player": {
                        "touchdown": 6,
                        "forced_fumble": 1,
                        "fumble_recovery": 1,
                    },
                }
            ).compile()
        )

    def test_team_credited_special_teams(self):
        result = self._scorer().score(
            StatLine({"dst_st_td": 2, "dst_st_forced_fum": 3, "dst_st_fum_rec": 1}), "DST"
        )
        assert result.points == pytest.approx(2 * 6 + 3 + 1)

    def test_player_credited_special_teams(self):
        result = self._scorer().score(
            StatLine({"st_player_td": 1, "st_player_forced_fum": 2, "st_player_fum_rec": 1}),
            "WR",
        )
        assert result.points == pytest.approx(6 + 2 + 1)

    def test_the_two_do_not_share_a_key(self):
        scorer = self._scorer()
        team = scorer.score(StatLine({"dst_st_td": 1}), "DST").points
        player = scorer.score(StatLine({"st_player_td": 1}), "WR").points
        both = scorer.score(StatLine({"dst_st_td": 1, "st_player_td": 1}), "DST").points
        assert team == pytest.approx(6)
        assert player == pytest.approx(6)
        assert both == pytest.approx(12)

    def test_fumble_recovery_touchdown_needs_no_new_configuration(self):
        # fumbles.return_touchdown already covers it, and defaults to 6.
        scorer = Scorer(ScoringConfig.model_validate({}).compile())
        assert scorer.score(StatLine({"fum_td": 1}), "RB").points == pytest.approx(6)
