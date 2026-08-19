"""Source adapters and normalization. Fixture-driven -- no network."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from fantasy_ai.config import FantasyProsConfig, HTTPConfig, SleeperConfig
from fantasy_ai.errors import SourceAuthError, SourceResponseError, SourceUnavailableError
from fantasy_ai.normalization.identity import (
    PlayerIndex,
    normalize_name,
    normalize_team,
    split_name,
)
from fantasy_ai.normalization.stat_mapping import map_stats, resolve_field
from fantasy_ai.sources import csv_import, demo
from fantasy_ai.sources.fantasypros import FantasyProsClient
from fantasy_ai.sources.http import HTTPCache, HTTPSource
from fantasy_ai.sources.sleeper import SleeperClient

from .conftest import make_player


def transport(handler) -> httpx.Client:
    """An httpx client backed by a callable instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNameNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ke'Shawn Vaughn", "keshawnvaughn"),
            ("Marvin Harrison Jr.", "marvinharrison"),
            ("Amon-Ra St. Brown", "amonrastbrown"),
            ("D'Andre Swift", "dandreswift"),
            ("  Extra   Spaces  ", "extraspaces"),
            ("", ""),
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_first_name_aliases_collapse(self):
        assert normalize_name("Joshua Palmer") == normalize_name("Josh Palmer")
        assert normalize_name("Michael Pittman") == normalize_name("Mike Pittman")

    def test_different_people_stay_different(self):
        assert normalize_name("Josh Allen") != normalize_name("Josh Jacobs")

    def test_split_name_handles_suffixes(self):
        assert split_name("Marvin Harrison Jr.") == ("Marvin", "Harrison")
        assert split_name("Amon-Ra St. Brown") == ("Amon-Ra", "St. Brown")
        assert split_name("Cher") == (None, "Cher")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("JAC", "JAX"), ("WSH", "WAS"), ("OAK", "LV"), ("KC", "KC"), ("FA", None)],
    )
    def test_team_aliases(self, raw, expected):
        assert normalize_team(raw) == expected


class TestPlayerIndex:
    def test_source_id_wins(self):
        index = PlayerIndex([make_player("sleeper:1", "Josh Allen", "QB", team="BUF")])
        index.link("sleeper", "1", "sleeper:1")
        resolved = index.resolve(
            source="sleeper", source_player_id="1", name="Completely Different", position="QB"
        )
        assert resolved.player_id == "sleeper:1"
        assert resolved.method == "source_id"

    def test_cross_source_id_links_without_name_matching(self):
        player = make_player("sleeper:1", "Josh Allen", "QB", team="BUF")
        player.source_ids = {"sleeper": "1", "yahoo": "9999"}
        index = PlayerIndex([player])
        resolved = index.resolve(
            source="fantasypros", source_player_id="777", name="J. Allen",
            position="QB", cross_source_ids={"yahoo": "9999"},
        )
        assert resolved.method == "cross_source_id"
        assert index.by_source("fantasypros", "777") == "sleeper:1"

    def test_name_match_links_the_new_source_id(self):
        index = PlayerIndex([make_player("sleeper:1", "Josh Allen", "QB", team="BUF")])
        resolved = index.resolve(
            source="fantasypros", source_player_id="777", name="Joshua Allen",
            position="QB", team="BUF",
        )
        assert resolved.player_id == "sleeper:1"
        assert index.by_source("fantasypros", "777") == "sleeper:1"

    def test_position_disambiguates_same_name(self):
        index = PlayerIndex(
            [
                make_player("a", "Michael Thomas", "WR", team="NO"),
                make_player("b", "Michael Thomas", "DB", team="NYG"),
            ]
        )
        resolved = index.resolve(
            source="fp", source_player_id="1", name="Michael Thomas", position="WR"
        )
        assert resolved.player_id == "a"

    def test_team_disambiguates_same_name_and_position(self):
        index = PlayerIndex(
            [
                make_player("a", "Same Name", "WR", team="NO"),
                make_player("b", "Same Name", "WR", team="NYG"),
            ]
        )
        resolved = index.resolve(
            source="fp", source_player_id="1", name="Same Name", position="WR", team="NYG"
        )
        assert resolved.player_id == "b"
        assert resolved.method == "name_position_team"

    def test_ambiguous_match_is_flagged(self):
        index = PlayerIndex(
            [
                make_player("a", "Same Name", "WR", team="NO"),
                make_player("b", "Same Name", "WR", team="NYG"),
            ]
        )
        resolved = index.resolve(
            source="fp", source_player_id="1", name="Same Name", position="WR",
            create_missing=False,
        )
        assert resolved.candidates == 2

    def test_new_player_is_minted_and_indexed(self):
        index = PlayerIndex([])
        resolved = index.resolve(
            source="fp", source_player_id="42", name="Brand New", position="WR", team="SEA"
        )
        assert resolved.created
        assert index.get(resolved.player_id).full_name == "Brand New"
        again = index.resolve(
            source="fp", source_player_id="42", name="Brand New", position="WR"
        )
        assert again.player_id == resolved.player_id
        assert not again.created

    def test_enrichment_never_overwrites(self):
        index = PlayerIndex([make_player("a", "Player", "WR", team="NO")])
        index.resolve(source="fp", source_player_id="1", name="Player",
                      position="WR", team="NYG")
        assert index.get("a").team == "NO"

    def test_missing_source_id_uses_a_name_derived_key(self):
        index = PlayerIndex([])
        resolved = index.resolve(
            source="csv", source_player_id=None, name="No Id Guy", position="RB"
        )
        assert resolved.player_id.startswith("name:")


class TestStatMapping:
    def test_common_dialects_all_land_on_canonical_keys(self):
        for field in ("pass_yds", "PASS YDS", "passing yards", "pass_yd"):
            assert resolve_field(field) == "pass_yd"

    def test_source_fantasy_points_are_ignored(self):
        """A source's own point total is under someone else's scoring."""
        assert resolve_field("fpts", source="fantasypros") is None
        assert resolve_field("pts_ppr", source="sleeper") is None

    def test_position_disambiguates_shared_names(self):
        assert resolve_field("sacks", position="DST") == "dst_sack"

    def test_team_and_individual_defensive_columns_are_told_apart(self):
        # "FF"/"TFL" mean the team stat on a DST sheet and the player stat
        # anywhere else. Before these were position-sensitive both spellings
        # resolved to the IDP key, so a DST export silently scored as IDP.
        for name in ("ff", "forced fumbles"):
            assert resolve_field(name, position="DST") == "dst_forced_fum"
            assert resolve_field(name, position="LB") == "idp_forced_fum"
        for name in ("tfl", "tackles for loss"):
            assert resolve_field(name, position="DST") == "dst_tackle_loss"
            assert resolve_field(name, position="LB") == "idp_tackle_loss"

    def test_fourth_down_stops_are_recognised(self):
        for name in ("4th down stops", "fourth down stops", "dst_fourth_down_stop"):
            assert resolve_field(name, position="DST") == "dst_fourth_down_stop"
        assert resolve_field("sacks", position="QB") == "pass_sacked"
        assert resolve_field("int", position="QB") == "pass_int"
        assert resolve_field("int", position="DST") == "dst_int"

    def test_numeric_coercion_and_misses(self):
        misses: Counter = Counter()
        line = map_stats(
            {"PASS YDS": "4,100", "PASS TDS": 30, "FPTS": 320.5, "mystery": 1},
            source="fantasypros", position="QB", misses=misses,
        )
        assert line["pass_yd"] == 4100
        assert line["pass_td"] == 30
        assert "mystery" in misses

    def test_non_numeric_values_dropped(self):
        line = map_stats({"rec": "-", "rec_yd": "N/A", "rec_td": "6"})
        assert "rec" not in line
        assert line["rec_td"] == 6

    def test_derived_stats(self):
        line = map_stats({"pass_att": 500, "pass_cmp": 340})
        assert line["pass_inc"] == 160
        buckets = map_stats({"fgm_30_39": 10, "fgm_40_49": 8})
        assert buckets["kick_fgm"] == 18


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class TestHTTPSource:
    def test_retries_then_succeeds(self, tmp_path: Path):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(200, json={"ok": True})

        source = HTTPSource(
            "http://test",
            HTTPConfig(max_retries=3, backoff_seconds=0.0, max_backoff_seconds=0.01),
            client=transport(handler),
        )
        assert source.get_json("thing").data == {"ok": True}
        assert calls["n"] == 3

    def test_gives_up_after_max_retries(self):
        source = HTTPSource(
            "http://test",
            HTTPConfig(max_retries=1, backoff_seconds=0.0),
            client=transport(lambda request: httpx.Response(500)),
        )
        with pytest.raises(SourceUnavailableError, match="did not respond"):
            source.get_json("thing")

    def test_auth_failure_is_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401)

        source = HTTPSource(
            "http://test", HTTPConfig(max_retries=3, backoff_seconds=0.0),
            client=transport(handler),
        )
        with pytest.raises(SourceAuthError):
            source.get_json("thing")
        assert calls["n"] == 1

    def test_404_names_the_configurable_endpoint(self):
        source = HTTPSource(
            "http://test", HTTPConfig(max_retries=0),
            client=transport(lambda request: httpx.Response(404)),
        )
        with pytest.raises(SourceResponseError, match="endpoint path"):
            source.get_json("thing")

    def test_non_json_body(self):
        source = HTTPSource(
            "http://test", HTTPConfig(max_retries=0),
            client=transport(lambda request: httpx.Response(200, text="<html>")),
        )
        with pytest.raises(SourceResponseError, match="non-JSON"):
            source.get_json("thing")

    def test_cache_serves_fresh_responses(self, tmp_path: Path):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"n": calls["n"]})

        source = HTTPSource(
            "http://test", HTTPConfig(cache_ttl_seconds=3600),
            cache_dir=tmp_path / "cache", client=transport(handler),
        )
        first = source.get_json("thing")
        second = source.get_json("thing")
        assert calls["n"] == 1
        assert second.from_cache
        assert second.data == first.data

    def test_force_refresh_bypasses_the_cache(self, tmp_path: Path):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"n": calls["n"]})

        source = HTTPSource(
            "http://test", HTTPConfig(cache_ttl_seconds=3600),
            cache_dir=tmp_path / "cache", client=transport(handler),
        )
        source.get_json("thing")
        source.get_json("thing", force_refresh=True)
        assert calls["n"] == 2

    def test_stale_cache_rescues_an_outage(self, tmp_path: Path):
        """The stated requirement: cached data keeps working when a source is down."""
        state = {"up": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if state["up"]:
                return httpx.Response(200, json={"value": 1})
            return httpx.Response(503)

        source = HTTPSource(
            "http://test",
            HTTPConfig(cache_ttl_seconds=0, max_retries=0, backoff_seconds=0.0),
            cache_dir=tmp_path / "cache", client=transport(handler),
        )
        source.get_json("thing")     # populates the cache
        state["up"] = False
        response = source.get_json("thing")
        assert response.stale
        assert response.data == {"value": 1}

    def test_cache_key_distinguishes_params(self):
        a = HTTPCache.key_for("GET", "http://x/y", {"position": "RB"})
        b = HTTPCache.key_for("GET", "http://x/y", {"position": "WR"})
        assert a != b


# ---------------------------------------------------------------------------
# Sleeper
# ---------------------------------------------------------------------------


SLEEPER_PLAYERS = {
    "4034": {
        "player_id": "4034", "first_name": "Alvin", "last_name": "Player",
        "full_name": "Alvin Player", "position": "RB", "team": "NO", "age": 29,
        "years_exp": 7, "status": "Active", "injury_status": "Questionable",
        "espn_id": 3116385, "yahoo_id": 30977, "college": "Tennessee",
    },
    "6794": {
        "player_id": "6794", "first_name": "Justin", "last_name": "Thrower",
        "full_name": "Justin Thrower", "position": "QB", "team": "JAC",
        "age": 25, "years_exp": 3, "status": "Active",
    },
    "KC": {
        "player_id": "KC", "position": "DEF", "team": "KC", "status": "Active",
        "fantasy_positions": ["DEF"],
    },
    "junk": {"player_id": "junk", "team": "NE"},   # no position: skipped
}


class TestSleeper:
    def _client(self, payload) -> SleeperClient:
        return SleeperClient(
            SleeperConfig(), HTTPConfig(max_retries=0),
            client=transport(lambda request: httpx.Response(200, json=payload)),
        )

    def test_parses_players(self):
        client = self._client(SLEEPER_PLAYERS)
        players, injuries = client.parse_players(SLEEPER_PLAYERS)
        by_id = {player.player_id: player for player in players}
        assert "sleeper:junk" not in by_id
        assert by_id["sleeper:4034"].position == "RB"
        assert by_id["sleeper:4034"].source_ids["espn"] == "3116385"
        assert len(injuries) == 1
        assert injuries[0].status == "Questionable"

    def test_normalises_team_defense(self):
        client = self._client(SLEEPER_PLAYERS)
        players, _ = client.parse_players(SLEEPER_PLAYERS)
        dst = next(p for p in players if p.position == "DST")
        assert dst.full_name == "KC Defense"

    def test_normalises_team_aliases(self):
        client = self._client(SLEEPER_PLAYERS)
        players, _ = client.parse_players(SLEEPER_PLAYERS)
        qb = next(p for p in players if p.position == "QB")
        assert qb.team == "JAX"

    def test_rejects_wrong_payload_shape(self):
        client = self._client([])
        with pytest.raises(SourceResponseError, match="not a JSON object"):
            client.parse_players(["nope"])

    def test_parses_draft_and_picks(self):
        draft_payload = {
            "draft_id": "999", "league_id": "111", "season": "2026", "status": "drafting",
            "type": "snake", "settings": {"teams": 10, "rounds": 15},
            "draft_order": {"user-a": 1, "user-b": 7},
            "slot_to_roster_id": {"1": 3, "7": 5},
        }
        client = self._client(draft_payload)
        draft = client.parse_draft(draft_payload)
        assert draft.teams == 10
        assert draft.slot_for_user("user-b") == 7

        picks = client.parse_picks(
            [
                {"pick_no": 2, "round": 1, "draft_slot": 2, "player_id": "6794",
                 "metadata": {"first_name": "Justin", "last_name": "Thrower"}},
                {"pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "4034"},
            ]
        )
        assert [pick.pick_no for pick in picks] == [1, 2]
        assert picks[1].player_name == "Justin Thrower"

    def test_empty_picks(self):
        client = self._client([])
        assert client.parse_picks(None) == []
        assert client.parse_picks([]) == []


# ---------------------------------------------------------------------------
# FantasyPros
# ---------------------------------------------------------------------------


FP_RANKINGS = {
    "count": 2,
    "players": [
        {
            "player_id": 17246, "player_name": "Alvin Player",
            "player_team_id": "NO", "player_position_id": "RB",
            "rank_ecr": 3, "rank_min": 1, "rank_max": 8, "rank_ave": 3.4,
            "rank_std": 1.9, "pos_rank": "RB2", "tier": 1, "player_bye_week": "9",
            "player_yahoo_id": 30977,
        },
        {
            "player_id": 19231, "player_name": "Justin Thrower",
            "player_team_id": "JAC", "player_position_id": "QB",
            "rank_ecr": 41, "rank_min": 30, "rank_max": 55, "rank_ave": 41.7,
            "rank_std": 6.2, "pos_rank": "QB5", "tier": 4, "player_bye_week": "11",
        },
    ],
}

FP_PROJECTIONS = {
    "players": [
        {
            "player_id": 17246, "player_name": "Alvin Player",
            "player_team_id": "NO", "player_position_id": "RB",
            "stats": {"rush_att": 250, "rush_yds": 1150, "rush_tds": 9,
                      "rec": 60, "rec_yds": 480, "rec_tds": 3, "fl": 2},
        }
    ]
}


class TestFantasyPros:
    def _client(self, payload, *, status: int = 200) -> FantasyProsClient:
        return FantasyProsClient(
            FantasyProsConfig(), HTTPConfig(max_retries=0),
            client=transport(lambda request: httpx.Response(status, json=payload)),
            api_key="test-key",
        )

    def test_parses_rankings_with_dispersion(self):
        client = self._client(FP_RANKINGS)
        rows, report = client.parse_rows(FP_RANKINGS)
        assert len(rows) == 2
        assert rows[0].ecr == 3
        assert rows[0].stdev == 1.9
        assert rows[0].position_rank == 2
        assert rows[0].bye_week == 9
        assert rows[0].cross_source_ids == {"yahoo": "30977"}
        assert report.matched_keys["ecr"] == "rank_ecr"

    def test_parses_projections_into_canonical_stats(self):
        client = self._client(FP_PROJECTIONS)
        rows, _ = client.parse_rows(FP_PROJECTIONS)
        stats = rows[0].stats
        assert stats["rush_yd"] == 1150
        assert stats["rec_td"] == 3
        assert stats["fum_lost"] == 2

    def test_tolerates_alternative_field_names(self):
        """The API shape has drifted before; aliases keep the adapter working."""
        payload = {
            "data": [
                {"name": "Some Player", "pos": "WR", "team": "SF",
                 "ecr": 12, "std_dev": 3.1, "best": 8, "worst": 20}
            ]
        }
        client = self._client(payload)
        rows, report = client.parse_rows(payload)
        assert rows[0].name == "Some Player"
        assert rows[0].position == "WR"
        assert rows[0].stdev == 3.1
        assert report.matched_keys["ecr"] == "ecr"

    def test_accepts_a_bare_list(self):
        payload = [{"player_name": "X Y", "player_position_id": "TE", "rank_ecr": 5}]
        client = self._client(payload)
        rows, _ = client.parse_rows(payload)
        assert rows[0].position == "TE"

    def test_unrecognisable_payload_reports_the_keys(self):
        payload = {"meta": {"note": "hi"}}
        client = self._client(payload)
        with pytest.raises(SourceResponseError, match="Could not find a list of players"):
            client.parse_rows(payload)

    def test_missing_api_key_explains_the_alternatives(self):
        client = FantasyProsClient(
            FantasyProsConfig(), HTTPConfig(),
            client=transport(lambda request: httpx.Response(200, json={})),
            api_key=None,
        )
        assert not client.has_key
        with pytest.raises(SourceAuthError, match="from-csv"):
            client.fetch_rankings(2026, scoring="HALF")

    def test_scoring_bucket_follows_league_ppr(self):
        client = self._client(FP_RANKINGS)
        assert client.scoring_parameter(0.0) == "STD"
        assert client.scoring_parameter(0.5) == "HALF"
        assert client.scoring_parameter(1.0) == "PPR"

    def test_explicit_scoring_overrides_auto(self):
        client = FantasyProsClient(
            FantasyProsConfig(scoring="PPR"), HTTPConfig(),
            client=transport(lambda request: httpx.Response(200, json={})),
            api_key="k",
        )
        assert client.scoring_parameter(0.0) == "PPR"

    def test_request_sends_the_api_key(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["key"] = request.headers.get("x-api-key")
            seen["url"] = str(request.url)
            return httpx.Response(200, json=FP_RANKINGS)

        client = FantasyProsClient(
            FantasyProsConfig(), HTTPConfig(), client=transport(handler), api_key="secret"
        )
        client.fetch_rankings(2026, scoring="HALF", ranking_type="ADP")
        assert seen["key"] == "secret"
        assert "type=ADP" in seen["url"]
        assert "2026" in seen["url"]


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


class TestCSVImport:
    def test_reads_a_projections_export(self, tmp_path: Path):
        path = tmp_path / "proj.csv"
        path.write_text(
            '"Player","Team","POS","ATT","YDS","TDS","REC","REC YDS","REC TDS","FL"\n'
            '"Alvin Player","NO","RB1","250","1,150","9","60","480","3","2"\n'
            '"Justin Thrower","JAX","QB5","40","210","2","0","0","0","1"\n'
        )
        rows, report = csv_import.read_rows(path)
        assert len(rows) == 2
        assert rows[0].position == "RB"
        assert rows[0].stats["rush_yd"] == 1150
        assert report.rows == 2

    def test_reads_a_rankings_export(self, tmp_path: Path):
        path = tmp_path / "ranks.csv"
        path.write_text(
            '"RK","TIERS","PLAYER NAME","TEAM","POS","BYE WEEK","SOS SEASON",'
            '"ECR VS. ADP","BEST","WORST","AVG.","STD.DEV"\n'
            '"1","1","Alvin Player","NO","RB1","9","3 out of 5 stars","-1",'
            '"1","4","1.8","0.9"\n'
        )
        rows, _ = csv_import.read_rows(path)
        assert rows[0].name == "Alvin Player"
        assert rows[0].rank == 1
        assert rows[0].stdev == 0.9
        assert rows[0].bye_week == 9

    def test_handles_a_title_line_above_the_header(self, tmp_path: Path):
        path = tmp_path / "titled.csv"
        path.write_text(
            "FantasyPros Half PPR Rankings\n"
            '"RK","PLAYER NAME","TEAM","POS"\n'
            '"1","Alvin Player","NO","RB1"\n'
        )
        rows, report = csv_import.read_rows(path)
        assert report.header_row_index == 1
        assert rows[0].name == "Alvin Player"

    def test_strips_a_team_suffix_from_the_name(self, tmp_path: Path):
        path = tmp_path / "suffix.csv"
        path.write_text('"Player","POS"\n"Alvin Player NO","RB"\n')
        rows, _ = csv_import.read_rows(path)
        assert rows[0].name == "Alvin Player"
        assert rows[0].team == "NO"

    def test_position_hint_for_files_without_a_pos_column(self, tmp_path: Path):
        path = tmp_path / "rb.csv"
        path.write_text('"Player","ATT","YDS","TDS"\n"Alvin Player","250","1150","9"\n')
        rows, _ = csv_import.read_rows(path, position_hint="RB")
        assert rows[0].position == "RB"

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(SourceResponseError, match="not found"):
            csv_import.read_rows(tmp_path / "nope.csv")

    def test_headerless_file(self, tmp_path: Path):
        path = tmp_path / "bad.csv"
        path.write_text("a,b,c\n1,2,3\n")
        with pytest.raises(SourceResponseError, match="Could not find a header"):
            csv_import.read_rows(path)

    def test_discover_sorts(self, tmp_path: Path):
        for name in ("b.csv", "a.csv"):
            (tmp_path / name).write_text('"Player"\n"X"\n')
        assert [path.name for path in csv_import.discover(tmp_path)] == ["a.csv", "b.csv"]


# ---------------------------------------------------------------------------
# Demo generator
# ---------------------------------------------------------------------------


class TestDemoSource:
    def test_is_deterministic(self):
        first = demo.generate(2026, seed=5)
        second = demo.generate(2026, seed=5)
        assert [p.player_id for p in first.players] == [p.player_id for p in second.players]
        assert first.projections[0].stats == second.projections[0].stats

    def test_different_seeds_differ(self):
        a = demo.generate(2026, seed=1)
        b = demo.generate(2026, seed=2)
        assert a.projections[0].stats != b.projections[0].stats

    def test_covers_every_position(self):
        dataset = demo.generate(2026, seed=3)
        positions = {player.position for player in dataset.players}
        assert {"QB", "RB", "WR", "TE", "K", "DST"} <= positions

    def test_value_declines_with_rank(self):
        dataset = demo.generate(2026, seed=3)
        by_id = {p.player_id: p for p in dataset.projections}
        top = by_id["demo:rb001"].stats["rush_yd"]
        deep = by_id["demo:rb060"].stats["rush_yd"]
        assert top > deep

    def test_market_and_projection_disagree(self):
        """ADP must not be a perfect function of our projection, or ADP value is dead."""
        dataset = demo.generate(2026, seed=3)
        adp_by_id = {record.player_id: record.adp for record in dataset.adp}
        projected = sorted(
            dataset.projections,
            key=lambda record: -sum(record.stats.values()),
        )
        order = [adp_by_id[record.player_id] for record in projected[:40]]
        assert order != sorted(order)

    def test_labels_itself_as_synthetic(self):
        dataset = demo.generate(2026, seed=3)
        assert all(record.source == "demo" for record in dataset.projections)
        assert all(player.player_id.startswith("demo:") for player in dataset.players)

    def test_serialisable(self):
        dataset = demo.generate(2026, seed=3)
        json.dumps(dict(dataset.projections[0].stats))


#: A verbatim record from a live ``/players/nfl`` response, trimmed only of
#: fields we never read. Kept exact -- including the leading space Sleeper ships
#: in ``gsis_id`` -- because that quirk is precisely what this pins down.
REAL_SLEEPER_RECORD = {
    "6462": {
        "player_id": "6462", "full_name": "Ellis Richardson", "number": 45,
        "search_first_name": "ellis", "injury_start_date": None, "first_name": "Ellis",
        "active": True, "fantasy_positions": ["TE"], "stats_id": 887183,
        "injury_notes": None, "rotoworld_id": None, "yahoo_id": 32262,
        "status": "Active", "hashtag": "#ellisrichardson-NFL-FA-45",
        "search_rank": 9999999, "search_full_name": "ellisrichardson",
        "fantasy_data_id": 21427, "team": None, "years_exp": 3, "swish_id": None,
        "search_last_name": "richardson", "rotowire_id": 14134, "player_shard": "8",
        "injury_status": None, "high_school": "Douglas County", "espn_id": 3926590,
        "age": 26, "sport": "nfl",
        "sportradar_id": "efd6f3c3-b752-4bc2-a4f2-b776c15c3ec0",
        "team_abbr": None, "height": "75", "depth_chart_position": None,
        "last_name": "Richardson", "position": "TE", "depth_chart_order": None,
        "injury_body_part": None, "weight": "245", "birth_date": "1995-02-12",
        "gsis_id": " 00-0035057", "college": "Georgia Southern",
    },
    "11255": {
        "player_id": "11255", "full_name": "Nick Amoah", "number": 0,
        "first_name": "Nick", "active": True, "fantasy_positions": ["OL"],
        "status": "Active", "search_rank": 9999999, "search_full_name": "nickamoah",
        "team": None, "years_exp": 3, "swish_id": 1052592, "last_name": "Amoah",
        "position": "OL", "age": None, "weight": "300", "sport": "nfl",
    },
}


class TestRealSleeperPayload:
    """Parsed against a verbatim record from the live API."""

    def _client(self, **overrides) -> SleeperClient:
        return SleeperClient(
            SleeperConfig(**overrides),
            HTTPConfig(max_retries=0),
            client=transport(lambda request: httpx.Response(200, json={})),
        )

    def test_every_field_we_read_survives(self):
        players, _ = self._client().parse_players(REAL_SLEEPER_RECORD)
        player = next(p for p in players if p.player_id == "sleeper:6462")
        assert player.full_name == "Ellis Richardson"
        assert (player.first_name, player.last_name) == ("Ellis", "Richardson")
        assert player.position == "TE"
        assert player.team is None            # free agent: team is null, not ""
        assert player.age == 26.0
        assert player.years_exp == 3
        assert player.status == "Active"
        assert player.injury_status is None
        assert player.birth_date == "1995-02-12"
        assert player.college == "Georgia Southern"

    def test_our_normalization_matches_sleepers_own_search_key(self):
        """Independent confirmation that name normalization is right.

        Sleeper publishes ``search_full_name`` computed by its own code. Ours
        agreeing with it on real records means the identity layer keys players
        the same way the source does.
        """
        players, _ = self._client(fantasy_positions_only=False).parse_players(
            REAL_SLEEPER_RECORD
        )
        by_id = {player.player_id: player for player in players}
        for sleeper_id, raw in REAL_SLEEPER_RECORD.items():
            player = by_id[f"sleeper:{sleeper_id}"]
            assert player.normalized_name == raw["search_full_name"]

    def test_cross_source_ids_are_collected(self):
        players, _ = self._client().parse_players(REAL_SLEEPER_RECORD)
        ids = next(p for p in players if p.player_id == "sleeper:6462").source_ids
        assert ids["sleeper"] == "6462"
        assert ids["espn"] == "3926590"
        assert ids["yahoo"] == "32262"
        assert ids["rotowire"] == "14134"
        assert ids["fantasydata"] == "21427"
        assert "swish" not in ids            # null must not become the string "None"

    def test_gsis_id_whitespace_is_stripped(self):
        """Sleeper ships ``gsis_id`` as " 00-0035057".

        An unstripped id silently fails to equal the same id from another
        source, which defeats the entire point of storing cross-source ids.
        """
        players, _ = self._client().parse_players(REAL_SLEEPER_RECORD)
        ids = next(p for p in players if p.player_id == "sleeper:6462").source_ids
        assert ids["gsis"] == "00-0035057"
        assert not ids["gsis"].startswith(" ")

    def test_non_fantasy_positions_are_dropped_by_default(self):
        players, _ = self._client().parse_players(REAL_SLEEPER_RECORD)
        assert [p.position for p in players] == ["TE"]

    def test_non_fantasy_positions_can_be_kept(self):
        players, _ = self._client(fantasy_positions_only=False).parse_players(
            REAL_SLEEPER_RECORD
        )
        assert {p.position for p in players} == {"TE", "OL"}

    def test_nose_tackle_maps_into_idp(self):
        payload = {
            "999": {
                "player_id": "999", "full_name": "Interior Guy", "first_name": "Interior",
                "last_name": "Guy", "position": "NT", "fantasy_positions": ["DL"],
                "team": "KC", "status": "Active",
            }
        }
        players, _ = self._client().parse_players(payload)
        assert players[0].position == "DT"
