"""Configuration validation and scoring compilation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fantasy_ai.config import LeagueConfig, load_settings, validate_settings
from fantasy_ai.config.ranges import Range, lookup, parse_range, parse_range_table
from fantasy_ai.config.scoring import ScoringConfig
from fantasy_ai.errors import ConfigError


class TestRanges:
    @pytest.mark.parametrize(
        ("key", "lower", "upper"),
        [("7", 7, 7), ("0-39", 0, 39), ("50+", 50, float("inf")), ("<=6", -float("inf"), 6)],
    )
    def test_parse_forms(self, key, lower, upper):
        parsed = parse_range(key)
        assert parsed.lower == lower
        assert parsed.upper == upper

    def test_rejects_nonsense(self):
        with pytest.raises(ConfigError, match="Cannot parse range key"):
            parse_range("thirty to forty")

    def test_rejects_overlaps(self):
        with pytest.raises(ConfigError, match="overlap"):
            parse_range_table({"0-20": 5, "10-30": 3})

    def test_lookup(self):
        table = parse_range_table({"0": 10, "1-6": 7, "7-13": 4, "35+": -4})
        assert lookup(table, 0) == 10
        assert lookup(table, 3) == 7
        assert lookup(table, 13) == 4
        assert lookup(table, 99) == -4
        assert lookup(table, 20) == 0  # outside every configured range

    def test_overlap_arithmetic(self):
        assert Range(0, 9).overlap(Range(5, 14)) == 5
        assert Range(0, 9).overlap(Range(10, 20)) == 0


class TestScoringCompilation:
    def test_yards_per_point_inverts(self):
        compiled = ScoringConfig().compile()
        assert compiled.rates["pass_yd"] == pytest.approx(0.04)
        assert compiled.rates["rush_yd"] == pytest.approx(0.1)

    def test_points_per_yard_alternative(self):
        scoring = ScoringConfig.model_validate(
            {"passing": {"yards_per_point": None, "points_per_yard": 0.05}}
        )
        assert scoring.compile().rates["pass_yd"] == pytest.approx(0.05)

    def test_rejects_both_yardage_forms(self):
        with pytest.raises(ConfigError, match="not both"):
            ScoringConfig.model_validate(
                {"passing": {"yards_per_point": 25, "points_per_yard": 0.04}}
            ).compile()

    def test_ppr_variants(self):
        assert ScoringConfig().compile().describe_format() == "Standard"
        half = ScoringConfig.model_validate({"receiving": {"reception": 0.5}})
        assert half.compile().describe_format() == "Half PPR"
        full = ScoringConfig.model_validate({"receiving": {"reception": 1.0}})
        assert full.compile().is_ppr

    def test_te_premium_override(self):
        scoring = ScoringConfig.model_validate(
            {
                "receiving": {"reception": 0.5},
                "position_overrides": {"TE": {"receiving": {"reception": 1.5}}},
            }
        )
        compiled = scoring.compile()
        assert compiled.rate("rec", "WR") == 0.5
        assert compiled.rate("rec", "TE") == 1.5
        assert "TE 1.5" in compiled.describe_format()

    def test_field_goal_ranges_map_onto_buckets(self):
        scoring = ScoringConfig.model_validate(
            {"kicking": {"field_goal": {"ranges": {"0-39": 3, "40-49": 4, "50+": 5}}}}
        )
        compiled = scoring.compile()
        assert compiled.rates["kick_fgm_0_19"] == 3
        assert compiled.rates["kick_fgm_30_39"] == 3
        assert compiled.rates["kick_fgm_40_49"] == 4
        assert compiled.rates["kick_fgm_50_plus"] == 5
        # A total-FG rate is blended over the distance distribution.
        assert 3 < compiled.rates["kick_fgm"] < 5

    def test_field_goal_scalar_shorthand(self):
        scoring = ScoringConfig.model_validate({"kicking": {"field_goal": 3.5}})
        assert scoring.compile().rates["kick_fgm"] == 3.5

    def test_straddling_range_is_overlap_weighted(self):
        # "0-35" covers 6 of the 10 yards in our 30-39 bucket; "36-49" the rest.
        scoring = ScoringConfig.model_validate(
            {"kicking": {"field_goal": {"ranges": {"0-35": 3, "36-49": 5, "50+": 6}}}}
        )
        rate = scoring.compile().rates["kick_fgm_30_39"]
        assert 3 < rate < 5

    def test_custom_stat_keys_validated(self):
        with pytest.raises(ConfigError, match="unknown stat keys"):
            ScoringConfig.model_validate({"custom": {"not_a_real_stat": 1.0}})

    def test_bonus_validates_stat(self):
        with pytest.raises(ConfigError, match="unknown stat"):
            ScoringConfig.model_validate(
                {"bonuses": [{"name": "x", "stat": "nope", "threshold": 100, "points": 3}]}
            )

    def test_unknown_section_key_is_an_error(self):
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            ScoringConfig.model_validate({"passing": {"touchdwon": 4}})


class TestLeagueConfig:
    def test_derived_roster_sizes(self, league: LeagueConfig):
        assert league.starters_per_team == 9
        assert league.roster_size == 15
        assert league.dedicated_starters() == {
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1
        }
        assert league.flex_capacity() == {"RB": 1, "WR": 1, "TE": 1}

    def test_superflex_detected(self, superflex_league: LeagueConfig):
        assert superflex_league.is_superflex
        assert "QB" in superflex_league.flex_capacity()

    def test_custom_flex_slot_needs_eligibility(self):
        with pytest.raises(ConfigError, match="looks like a flex slot"):
            LeagueConfig(season=2026, teams=10, roster={"QB": 1, "WEIRDFLEX": 1}).roster_slots()

    def test_custom_flex_slot_from_yaml(self):
        config = LeagueConfig(
            season=2026,
            teams=10,
            roster={"WR": 2, "WRT": 1, "BENCH": 4},
            flex={"allowed_positions": ["RB", "WR", "TE"], "slots": {"WRT": ["WR", "TE"]}},
        )
        slots = {slot.name: slot.eligible_positions for slot in config.starting_slots}
        assert slots["WRT"] == ("WR", "TE")

    def test_unknown_position_rejected(self):
        with pytest.raises(ConfigError, match="not a known position"):
            LeagueConfig(season=2026, teams=10, roster={"CENTER": 1}).roster_slots()

    def test_draft_slot_must_fit(self):
        with pytest.raises(ConfigError, match="exceeds team count"):
            LeagueConfig(season=2026, teams=8, draft={"position": 12})

    def test_auction_needs_budget(self):
        with pytest.raises(ConfigError, match="budget is required"):
            LeagueConfig(season=2026, teams=10, draft={"type": "auction"})

    def test_keepers_conflict_with_redraft(self):
        with pytest.raises(ConfigError, match="redraft"):
            LeagueConfig(
                season=2026, teams=10, type="redraft",
                keepers={"enabled": True, "max_keepers": 2},
            )

    def test_slot_names_normalised(self):
        config = LeagueConfig(season=2026, teams=10, roster={"qb": 1, " rb ": 2, "BENCH": 3})
        assert set(config.roster) == {"QB", "RB", "BENCH"}

    def test_position_aliases(self):
        config = LeagueConfig(
            season=2026, teams=10,
            roster={"QB": 1, "FLEX": 1, "BENCH": 3},
            flex={"allowed_positions": ["rb", "wr", "D/ST"]},
        )
        eligibility = config.flex.eligibility_for("FLEX")
        assert eligibility == ("RB", "WR", "DST")


class TestLoader:
    def _write(self, root: Path, league_yaml: str, sources_yaml: str = "") -> None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "league.yaml").write_text(textwrap.dedent(league_yaml))
        (root / "config" / "sources.yaml").write_text(textwrap.dedent(sources_yaml))

    def test_loads_minimal_config(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 12
        """)
        settings = load_settings(root=tmp_path)
        assert settings.league.teams == 12
        assert settings.app.paths.database.is_absolute()

    def test_missing_league_block(self, tmp_path: Path):
        self._write(tmp_path, "teams: 10\n")
        with pytest.raises(ConfigError, match="no top-level 'league:' block"):
            load_settings(root=tmp_path)

    def test_unknown_top_level_block(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 10
            leauge:
              typo: true
        """)
        with pytest.raises(ConfigError, match="Unknown top-level"):
            load_settings(root=tmp_path)

    def test_duplicate_block_across_files(self, tmp_path: Path):
        self._write(
            tmp_path,
            """
            league:
              season: 2026
              teams: 10
            simulation:
              iterations: 50
            """,
            """
            simulation:
              iterations: 99
            """,
        )
        with pytest.raises(ConfigError, match="defined in both"):
            load_settings(root=tmp_path)

    def test_invalid_yaml_reports_file(self, tmp_path: Path):
        self._write(tmp_path, "league: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_settings(root=tmp_path)

    def test_validation_error_points_at_field(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 99
        """)
        with pytest.raises(ConfigError, match="teams"):
            load_settings(root=tmp_path)

    def test_env_overrides(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 10
        """)
        settings = load_settings(
            root=tmp_path,
            env={"FANTASY_AI_SIM_ITERATIONS": "42", "FANTASY_AI_LLM_MODEL": "other"},
        )
        assert settings.app.simulation.iterations == 42
        assert settings.app.llm.model == "other"

    def test_cli_overrides_win(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 10
            simulation:
              iterations: 10
        """)
        settings = load_settings(
            root=tmp_path, env={}, overrides={"simulation": {"iterations": 500}}
        )
        assert settings.app.simulation.iterations == 500

    def test_falls_back_to_example_files(self, tmp_path: Path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "league.example.yaml").write_text(
            "league:\n  season: 2026\n  teams: 10\n"
        )
        (tmp_path / "config" / "sources.example.yaml").write_text("log_level: debug\n")
        settings = load_settings(root=tmp_path)
        assert settings.app.log_level == "debug"
        assert settings.league_path.name == "league.example.yaml"

    def test_warnings_are_non_fatal(self, tmp_path: Path):
        self._write(tmp_path, """
            league:
              season: 2026
              teams: 10
              roster: {QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DST: 1, BENCH: 6}
              draft: {rounds: 16}
        """)
        settings = load_settings(root=tmp_path, env={})
        warnings = validate_settings(settings)
        assert any("exceeds draftable roster spots" in warning for warning in warnings)
        assert any("draft.position is not set" in warning for warning in warnings)

    def test_example_config_in_repo_is_valid(self):
        """The shipped example must always load -- it is the first-run path."""
        root = Path(__file__).resolve().parents[1]
        settings = load_settings(root=root, env={})
        assert settings.league.teams > 0
        assert settings.league.scoring.compile().rates
