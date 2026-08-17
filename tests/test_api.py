"""HTTP API tests.

The API is what the web UI depends on, so these pin the wire contract: field
names, error shapes, and the cache/invalidation behaviour a polling UI relies
on. No network and no model server -- the LLM routes are driven by a fake
client, exactly as in ``test_llm.py``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fantasy_ai.api import AppState, create_app
from fantasy_ai.services import SyncService


@pytest.fixture
def state(settings) -> AppState:
    """App state on an in-memory database that may cross threads.

    FastAPI runs sync handlers in a threadpool, so the connection needs the same
    relaxation the real server uses; without it every request fails with
    sqlite3's same-thread check.
    """
    from fantasy_ai.db import Database

    database = Database(":memory:", allow_cross_thread=True)
    app_state = AppState(settings=settings, database=database)
    yield app_state
    app_state.close()


@pytest.fixture
def seeded(state: AppState) -> AppState:
    SyncService(state.settings, state.repos).sync_demo(seed=11)
    state.invalidate()
    return state


@pytest.fixture
def client(seeded: AppState) -> TestClient:
    with TestClient(create_app(state=seeded, serve_frontend=False)) as test_client:
        yield test_client


@pytest.fixture
def empty_client(state: AppState) -> TestClient:
    with TestClient(create_app(state=state, serve_frontend=False)) as test_client:
        yield test_client


class TestMeta:
    def test_info(self, client: TestClient):
        body = client.get("/api/info").json()
        assert body["has_data"] is True
        assert body["league"]["teams"] == 10
        assert body["league"]["scoring_format"]
        assert "league_config" in body["settings_files"]

    def test_info_without_data(self, empty_client: TestClient):
        assert empty_client.get("/api/info").json()["has_data"] is False

    def test_data_status(self, client: TestClient):
        body = client.get("/api/data/status").json()
        assert body["has_data"] is True
        datasets = {entry["dataset"] for entry in body["datasets"]}
        assert {"players", "projections", "rankings", "adp"} <= datasets

    def test_openapi_is_served(self, client: TestClient):
        schema = client.get("/openapi.json").json()
        assert "/api/board" in schema["paths"]


class TestBoard:
    def test_board_shape(self, client: TestClient):
        body = client.get("/api/board?limit=5").json()
        assert len(body["players"]) == 5
        assert body["availability_method"] in {"monte-carlo", "analytic", "none"}
        assert body["total_available"] > 50
        assert body["replacement"] and body["scarcity"]

        player = body["players"][0]
        for field in (
            "player_id", "name", "position", "projected_points", "vor",
            "draft_score", "components", "our_rank",
        ):
            assert field in player, field
        assert player["components"], "draft score must arrive decomposed"

    def test_components_sum_to_the_score(self, client: TestClient):
        """The API must not lose the invariant the analytics guarantee."""
        for player in client.get("/api/board?limit=10").json()["players"]:
            total = sum(component["contribution"] for component in player["components"])
            assert total == pytest.approx(player["draft_score"], abs=0.02)

    def test_position_filter(self, client: TestClient):
        body = client.get("/api/board?position=TE&limit=8").json()
        assert body["players"]
        assert {player["position"] for player in body["players"]} == {"TE"}

    def test_limit_is_bounded(self, client: TestClient):
        assert client.get("/api/board?limit=0").status_code == 422
        assert client.get("/api/board?limit=9999").status_code == 422

    def test_board_without_data_is_a_clean_409(self, empty_client: TestClient):
        response = empty_client.get("/api/board")
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "DataMissingError"
        assert "sync" in body["hint"].lower()

    def test_simulation_can_be_disabled(self, client: TestClient):
        body = client.get("/api/board?limit=3&simulate=false").json()
        assert body["simulation"] is None
        assert body["availability_method"] in {"analytic", "none"}


class TestCaching:
    def test_repeated_requests_reuse_the_cached_board(self, client: TestClient):
        first = client.get("/api/board?limit=3").json()
        second = client.get("/api/board?limit=3").json()
        # Same state version and the same generation timestamp means it was not
        # recomputed -- which is what makes a polling UI cheap.
        assert first["state_version"] == second["state_version"]
        assert first["generated_at"] == second["generated_at"]

    def test_a_pick_invalidates_the_cache(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        before = client.get("/api/board?limit=3").json()
        target = before["players"][0]
        client.post("/api/draft/pick", json={"player_id": target["player_id"]})
        after = client.get("/api/board?limit=3").json()

        assert after["state_version"] != before["state_version"]
        assert target["player_id"] not in {p["player_id"] for p in after["players"]}


class TestPlayerDetail:
    def test_detail_includes_the_derivation(self, client: TestClient):
        player_id = client.get("/api/board?limit=1").json()["players"][0]["player_id"]
        body = client.get(f"/api/players/{player_id}").json()
        assert body["explanation"]
        assert body["scoring_lines"]
        total = sum(line["points"] for line in body["scoring_lines"])
        assert total == pytest.approx(body["player"]["projected_points"], abs=0.05)

    def test_unknown_player_is_404(self, client: TestClient):
        assert client.get("/api/players/does-not-exist").status_code == 404


class TestSearch:
    def test_search_finds_players(self, client: TestClient):
        results = client.get("/api/search?q=pemberton&limit=5").json()
        assert results
        assert all("pemberton" in item["name"].lower() for item in results)

    def test_search_flags_drafted_players(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        target = client.get("/api/board?limit=1").json()["players"][0]
        client.post("/api/draft/pick", json={"player_id": target["player_id"]})

        results = client.get(f"/api/search?q={target['name']}&limit=5").json()
        match = next(item for item in results if item["player_id"] == target["player_id"])
        assert match["drafted"] is True

    def test_empty_query_rejected(self, client: TestClient):
        assert client.get("/api/search?q=").status_code == 422


class TestDraft:
    def test_status_before_starting(self, client: TestClient):
        assert client.get("/api/draft/status").json()["active"] is False

    def test_start_pick_undo_cycle(self, client: TestClient):
        started = client.post("/api/draft/start", json={"position": 7}).json()
        assert started["active"] is True
        assert started["user_slot"] == 7
        assert started["user_picks"][:2] == [7, 14]

        target = client.get("/api/board?limit=1").json()["players"][0]
        result = client.post(
            "/api/draft/pick", json={"player_id": target["player_id"]}
        ).json()
        assert result["pick"]["overall_pick"] == 1
        assert result["pick"]["name"] == target["name"]
        assert result["status"]["drafted_count"] == 1

        undone = client.post("/api/draft/undo").json()
        assert undone["pick"]["overall_pick"] == 1
        assert undone["status"]["drafted_count"] == 0

    def test_pick_by_name(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        target = client.get("/api/board?limit=1").json()["players"][0]
        result = client.post("/api/draft/pick", json={"name": target["name"]})
        assert result.status_code == 200
        assert result.json()["pick"]["player_id"] == target["player_id"]

    def test_duplicate_pick_is_a_clean_409(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        target = client.get("/api/board?limit=1").json()["players"][0]
        client.post("/api/draft/pick", json={"player_id": target["player_id"]})
        again = client.post("/api/draft/pick", json={"player_id": target["player_id"]})
        assert again.status_code == 409
        assert "already been drafted" in again.json()["detail"]

    def test_pick_without_a_draft_is_a_clean_409(self, client: TestClient):
        target = client.get("/api/board?limit=1").json()["players"][0]
        response = client.post("/api/draft/pick", json={"player_id": target["player_id"]})
        assert response.status_code == 409
        assert response.json()["error"] == "DraftStateError"

    def test_pick_requires_an_identifier(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        assert client.post("/api/draft/pick", json={}).status_code == 422

    def test_skip_advances_the_clock(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        result = client.post("/api/draft/skip").json()
        assert result["pick"]["player_id"] is None
        assert result["status"]["current_pick"] == 2

    def test_roster_reflects_the_users_picks(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 1})
        target = client.get("/api/board?limit=1").json()["players"][0]
        result = client.post(
            "/api/draft/pick", json={"player_id": target["player_id"]}
        ).json()
        roster = result["status"]["roster"]
        assert [entry["name"] for entry in roster["players"]] == [target["name"]]
        assert roster["lineup"], "the lineup must be resolved for the UI"
        assert roster["picks_remaining"] >= 0

    def test_second_start_requires_replace(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        blocked = client.post("/api/draft/start", json={"position": 7})
        assert blocked.status_code == 409
        allowed = client.post(
            "/api/draft/start", json={"position": 7, "replace": True}
        )
        assert allowed.status_code == 200

    def test_complete_marks_the_draft_done(self, client: TestClient):
        client.post("/api/draft/start", json={"position": 7})
        client.post("/api/draft/complete")
        assert client.get("/api/draft/status").json()["active"] is False


class TestLLMRoutes:
    def test_status_when_disabled(self, client: TestClient, seeded: AppState):
        seeded.settings.app.llm.enabled = False
        body = client.get("/api/llm/status").json()
        assert body["enabled"] is False
        assert body["reachable"] is False

    def test_recommend_when_disabled_is_503(self, client: TestClient, seeded: AppState):
        seeded.settings.app.llm.enabled = False
        response = client.post("/api/recommend", json={})
        assert response.status_code == 503
        assert response.json()["error"] == "LLMError"
        assert "board works without" in response.json()["hint"]

    def test_ask_when_disabled_is_503(self, client: TestClient, seeded: AppState):
        seeded.settings.app.llm.enabled = False
        assert client.post("/api/ask", json={"question": "who?"}).status_code == 503

    def test_recommend_with_a_fake_model(
        self, client: TestClient, seeded: AppState, monkeypatch
    ):
        """The happy path, without a model server."""
        from fantasy_ai.api import app as app_module

        board = seeded.board().context.board
        names = [player.name for player in board.top(8)]
        reply = json.dumps(
            {
                "recommendation": names[0],
                "confidence": "high",
                "reasoning": ["Highest VOR", "Unlikely to return"],
                "alternatives": [{"player": names[1], "reason": "safer floor"}],
                "risks": ["projection uncertainty"],
            }
        )

        from .conftest import FakeLLMClient

        class Fake(FakeLLMClient):
            def __init__(self, config=None, **_):
                super().__init__([reply], config)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        monkeypatch.setattr(app_module, "LLMClient", Fake)
        body = client.post("/api/recommend", json={"live": True}).json()
        assert body["ok"] is True
        assert body["recommendation"] == names[0]
        assert body["reasoning"]
        assert body["alternatives"][0]["player"] == names[1]
        assert body["deterministic_top"] is not None

    def test_recommend_reports_an_unusable_model_without_failing(
        self, client: TestClient, seeded: AppState, monkeypatch
    ):
        """A model that cannot produce JSON must not cost the user their pick."""
        from fantasy_ai.api import app as app_module

        from .conftest import FakeLLMClient

        class Fake(FakeLLMClient):
            def __init__(self, config=None, **_):
                super().__init__(["not json at all", "still not", "nope"], config)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        monkeypatch.setattr(app_module, "LLMClient", Fake)
        body = client.post("/api/recommend", json={}).json()
        assert body["ok"] is False
        assert body["failures"]
        # The deterministic answer is still there, which is the point.
        assert body["deterministic_top"]


class TestFrontendServing:
    def test_missing_build_explains_itself(self, seeded: AppState):
        from fantasy_ai.api import app as app_module

        original = app_module.WEB_DIST
        app_module.WEB_DIST = original.parent / "definitely-not-built"
        try:
            with TestClient(
                create_app(state=seeded, serve_frontend=True)
            ) as test_client:
                response = test_client.get("/")
                assert response.status_code == 503
                assert "npm run build" in response.json()["hint"]
        finally:
            app_module.WEB_DIST = original
