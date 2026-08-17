"""Shared fixtures.

No test in this suite touches the network or a running LLM.  Source clients are
driven by fixture payloads through an injected transport, and the local model is
replaced by a fake client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fantasy_ai.analytics.common import ScoredPlayer
from fantasy_ai.config import AppConfig, LeagueConfig, Settings
from fantasy_ai.db import Database, Repositories
from fantasy_ai.models import ADPRecord, Player, ProjectionRecord, RankingRecord
from fantasy_ai.stats import StatLine

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def league() -> LeagueConfig:
    """A standard 10-team half-PPR league with one flex."""
    return LeagueConfig(
        name="Test League",
        season=2026,
        teams=10,
        roster={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "BENCH": 6},
        draft={"type": "snake", "position": 7, "rounds": 15},
    )


@pytest.fixture
def superflex_league() -> LeagueConfig:
    """A 12-team superflex league, for testing that QB demand adapts."""
    return LeagueConfig(
        name="Superflex League",
        season=2026,
        teams=12,
        roster={
            "QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SUPERFLEX": 1, "BENCH": 7
        },
        flex={"allowed_positions": ["RB", "WR", "TE"],
              "slots": {"SUPERFLEX": ["QB", "RB", "WR", "TE"]}},
        draft={"type": "snake", "position": 3, "rounds": 16},
        scoring={"receiving": {"reception": 1.0}},
    )


@pytest.fixture
def settings(tmp_path: Path, league: LeagueConfig) -> Settings:
    app = AppConfig()
    app.paths.database = tmp_path / "test.db"
    app.paths.data_dir = tmp_path
    app.paths.http_cache = tmp_path / "http-cache"
    app.sources.csv_import.directory = tmp_path / "imports"
    app.simulation.iterations = 100
    app.simulation.seed = 7
    return Settings(
        league=league,
        app=app,
        root=tmp_path,
        league_path=tmp_path / "league.yaml",
        sources_path=tmp_path / "sources.yaml",
    )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repos(db: Database) -> Repositories:
    return Repositories(db)


# ---------------------------------------------------------------------------
# Player data
# ---------------------------------------------------------------------------


def make_player(
    player_id: str,
    name: str,
    position: str,
    *,
    team: str = "KC",
    age: float | None = 26.0,
    years_exp: int | None = 4,
    injury_status: str | None = None,
) -> Player:
    from fantasy_ai.normalization.identity import normalize_name

    return Player(
        player_id=player_id,
        full_name=name,
        normalized_name=normalize_name(name),
        position=position,
        team=team,
        age=age,
        years_exp=years_exp,
        injury_status=injury_status,
        status="Active",
        source_ids={"test": player_id},
    )


def scored(prefix: str, position: str, points: list[float]) -> list[ScoredPlayer]:
    """A position's board: ``prefix1..prefixN`` with the given projections."""
    return [
        ScoredPlayer(
            player_id=f"{prefix}{index}",
            position=position,
            points=value,
            name=f"{prefix.upper()}{index}",
        )
        for index, value in enumerate(points, start=1)
    ]


@pytest.fixture
def board_players() -> list[ScoredPlayer]:
    """A small but realistically-shaped board across every position."""
    players: list[ScoredPlayer] = []
    players += scored("qb", "QB", [380, 360, 350, 330, 322, 316, 310, 305, 300, 296,
                                   292, 288, 284, 280, 276, 272])
    players += scored("rb", "RB", [300, 285, 270, 250, 235, 228, 220, 214, 208, 202,
                                   196, 190, 185, 180, 176, 172, 168, 164, 160, 156,
                                   152, 148, 144, 140, 136, 132, 128, 124, 120, 116])
    players += scored("wr", "WR", [295, 280, 272, 262, 250, 242, 236, 230, 224, 219,
                                   214, 209, 204, 200, 196, 192, 188, 184, 180, 176,
                                   172, 168, 164, 160, 156, 152, 148, 144, 140, 136,
                                   132, 128, 124, 120, 116, 112])
    players += scored("te", "TE", [230, 195, 175, 160, 152, 146, 140, 135, 130, 126,
                                   122, 118, 114, 110, 106, 102])
    players += scored("k", "K", [150, 146, 143, 140, 138, 136, 134, 132, 130, 128,
                                 126, 124, 122, 120])
    players += scored("dst", "DST", [140, 132, 126, 121, 117, 113, 110, 107, 104, 101,
                                     98, 95, 92, 89])
    return players


@pytest.fixture
def stocked_repos(repos: Repositories, league: LeagueConfig) -> Repositories:
    """A database populated with a small, deterministic dataset."""
    specs = [
        ("rb", "RB", [300, 285, 270, 250, 235, 228, 220, 214, 208, 202, 196, 190,
                      185, 180, 176, 172, 168, 164, 160, 156, 152, 148, 144, 140, 136]),
        ("wr", "WR", [295, 280, 272, 262, 250, 242, 236, 230, 224, 219, 214, 209,
                      204, 200, 196, 192, 188, 184, 180, 176, 172, 168, 164, 160, 156,
                      152, 148, 144, 140, 136]),
        ("qb", "QB", [380, 360, 350, 330, 322, 316, 310, 305, 300, 296, 292, 288]),
        ("te", "TE", [230, 195, 175, 160, 152, 146, 140, 135, 130, 126, 122, 118]),
        ("k", "K", [150, 146, 143, 140, 138, 136, 134, 132, 130, 128]),
        ("dst", "DST", [140, 132, 126, 121, 117, 113, 110, 107, 104, 101]),
    ]
    players: list[Player] = []
    projections: list[ProjectionRecord] = []
    rankings: list[RankingRecord] = []
    adp_records: list[ADPRecord] = []

    # ADP is assigned by overall projected points, with a deterministic wobble so
    # our board and the market disagree the way they do in reality.
    flat = [
        (f"{prefix}{index}", position, value)
        for prefix, position, values in specs
        for index, value in enumerate(values, start=1)
    ]
    ordered = sorted(flat, key=lambda item: -item[2])
    adp_by_id = {
        player_id: float(rank + ((hash(player_id) % 7) - 3))
        for rank, (player_id, _, _) in enumerate(ordered, start=1)
    }

    for prefix, position, values in specs:
        for index, value in enumerate(values, start=1):
            player_id = f"{prefix}{index}"
            players.append(make_player(player_id, f"{prefix.upper()} Player{index}", position))
            projections.append(
                ProjectionRecord(
                    player_id=player_id,
                    season=league.season,
                    source="test",
                    # Scoring these stats under the fixture league reproduces
                    # `value` exactly: 0.1 pts/yard with no other categories.
                    stats=StatLine({"rec_yd" if position != "QB" else "rush_yd": value * 10}),
                )
            )
            rankings.append(
                RankingRecord(
                    player_id=player_id,
                    season=league.season,
                    source="test",
                    ecr=adp_by_id[player_id],
                    stdev=1.0 + index * 0.3,
                    best=max(1.0, adp_by_id[player_id] - 4),
                    worst=adp_by_id[player_id] + 5,
                    average=adp_by_id[player_id],
                    expert_count=12,
                )
            )
            adp_records.append(
                ADPRecord(
                    player_id=player_id,
                    season=league.season,
                    source="test",
                    adp=max(1.0, adp_by_id[player_id]),
                    stdev=2.0 + index * 0.2,
                    teams=league.teams,
                )
            )

    repos.players.upsert(players)
    repos.projections.add_many(projections)
    repos.rankings.add_many(rankings)
    repos.adp.add_many(adp_records)
    return repos


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Stands in for :class:`fantasy_ai.llm.LLMClient` with scripted replies."""

    def __init__(self, replies: list[str], config: Any = None) -> None:
        from fantasy_ai.config import LLMConfig

        self.replies = list(replies)
        self.config = config or LLMConfig()
        self.requests: list[list[Any]] = []

    def complete(self, messages, *, schema=None, schema_name="response", **_: Any):
        from fantasy_ai.llm.client import ChatResponse

        self.requests.append(list(messages))
        content = self.replies.pop(0) if self.replies else "{}"
        return ChatResponse(content=content, model="fake-model", finish_reason="stop")

    def close(self) -> None:
        return None


@pytest.fixture
def fake_llm_factory():
    def build(replies: list[str]) -> FakeLLMClient:
        return FakeLLMClient(replies)

    return build
