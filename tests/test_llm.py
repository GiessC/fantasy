"""LLM context, parsing, validation, and the recommendation loop.

No test here contacts a model server: transport-level tests use an httpx mock,
and loop-level tests use the scripted fake client from conftest.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fantasy_ai.analytics import AnalyticsEngine, load_dataset
from fantasy_ai.config import LLMConfig
from fantasy_ai.errors import LLMError
from fantasy_ai.llm.client import ChatMessage, LLMClient
from fantasy_ai.llm.context import build_context
from fantasy_ai.llm.parsing import check_player_names, extract_json, parse_model
from fantasy_ai.llm.prompts import build_recommendation_prompt, build_system_prompt
from fantasy_ai.llm.recommender import Recommender
from fantasy_ai.llm.schemas import Answer, Recommendation


@pytest.fixture
def board(stocked_repos, league, settings):
    dataset = load_dataset(stocked_repos, league, settings.app.analytics)
    engine = AnalyticsEngine(league, settings.app.analytics, settings.app.simulation)
    return engine.analyze(dataset, next_pick=14, current_pick=7)


class TestJSONExtraction:
    def test_bare_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_object(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_with_surrounding_prose(self):
        text = 'Sure! Here you go:\n{"a": 1, "b": [2, 3]}\nHope that helps.'
        assert extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        text = '{"note": "use {curly} braces", "n": 1}'
        assert extract_json(text)["n"] == 1

    def test_escaped_quotes(self):
        assert extract_json(r'{"note": "he said \"hi\""}')["note"] == 'he said "hi"'

    def test_empty_reply(self):
        with pytest.raises(ValueError, match="empty"):
            extract_json("   ")

    def test_no_json_at_all(self):
        with pytest.raises(ValueError, match="no JSON object"):
            extract_json("I think you should draft the running back.")

    def test_malformed_json_reports_position(self):
        with pytest.raises(ValueError, match="malformed"):
            extract_json('{"a": 1,,}')


class TestSchemas:
    def test_valid_recommendation(self):
        payload = {
            "recommendation": "Player A", "confidence": "high",
            "reasoning": ["one", "two"],
            "alternatives": [{"player": "Player B", "reason": "safer"}],
            "risks": ["injury"],
        }
        rec = parse_model(json.dumps(payload), Recommendation)
        assert rec.recommendation == "Player A"
        assert rec.named_players() == ["Player A", "Player B"]

    def test_string_coerced_to_list(self):
        rec = parse_model(
            '{"recommendation": "A", "confidence": "low", "reasoning": "just one"}',
            Recommendation,
        )
        assert rec.reasoning == ["just one"]

    def test_extra_fields_ignored(self):
        rec = parse_model(
            '{"recommendation": "A", "confidence": "low", "reasoning": ["x"],'
            ' "made_up_field": 42}',
            Recommendation,
        )
        assert rec.recommendation == "A"

    def test_bad_confidence_rejected(self):
        with pytest.raises(ValueError, match="did not match the required shape"):
            parse_model(
                '{"recommendation": "A", "confidence": "extremely", "reasoning": []}',
                Recommendation,
            )

    def test_missing_required_field(self):
        with pytest.raises(ValueError, match="did not match"):
            parse_model('{"confidence": "high"}', Recommendation)

    def test_array_at_top_level_rejected(self):
        with pytest.raises(ValueError, match="an object was required"):
            parse_model("[1, 2, 3]", Recommendation)

    def test_render_matches_the_live_draft_format(self):
        rec = Recommendation(
            recommendation="Player A", confidence="medium",
            reasoning=["Best VOR available"],
            alternatives=[{"player": "Player B", "reason": "71% to make it back"}],
            risks=["projection uncertainty"],
        )
        rendered = rec.render()
        assert rendered.startswith("RECOMMENDATION: Player A")
        assert "Why:" in rendered
        assert "- Best VOR available" in rendered
        assert "Player B" in rendered

    def test_answer_schema(self):
        answer = parse_model(
            '{"answer": "Take the RB.", "players_referenced": ["A"], "caveats": "none"}',
            Answer,
        )
        assert answer.caveats == ["none"]


class TestPlayerNameChecking:
    def test_known_names_pass(self):
        unknown, corrections = check_player_names(["Player A"], ["Player A", "Player B"])
        assert unknown == []
        assert corrections == {}

    def test_invented_player_is_caught(self):
        unknown, _ = check_player_names(["Patrick Mahomes"], ["Player A", "Player B"])
        assert unknown == ["Patrick Mahomes"]

    def test_near_miss_is_corrected_not_rejected(self):
        unknown, corrections = check_player_names(["Joshua Allen"], ["Josh Allen"])
        assert unknown == []
        assert corrections == {"Joshua Allen": "Josh Allen"}

    def test_suffix_difference_is_corrected(self):
        unknown, corrections = check_player_names(
            ["Marvin Harrison"], ["Marvin Harrison Jr."]
        )
        assert unknown == []
        assert corrections["Marvin Harrison"] == "Marvin Harrison Jr."

    def test_empty_names_ignored(self):
        unknown, _ = check_player_names(["", None], ["Player A"])  # type: ignore[list-item]
        assert unknown == []


class TestContextBuilder:
    def test_contains_league_draft_and_candidates(self, board, league):
        context = build_context(board, league, max_candidates=5)
        payload = context.to_dict()
        assert payload["league"]["teams"] == league.teams
        assert payload["league"]["scoring_format"]
        assert len(payload["candidates"]) == 5
        assert "replacement_levels" in payload
        assert payload["positional_scarcity"]

    def test_respects_the_candidate_limit(self, board, league):
        assert len(build_context(board, league, max_candidates=3).candidates) == 3

    def test_does_not_dump_the_database(self, board, league):
        """The stated requirement: send a relevant subset, not everything."""
        context = build_context(board, league, max_candidates=8)
        assert len(context.candidates) == 8
        assert len(board.players) > 50
        assert context.approximate_tokens() < 6000

    def test_internal_ids_are_not_sent(self, board, league):
        context = build_context(board, league, max_candidates=4)
        assert all("player_id" not in candidate for candidate in context.candidates)

    def test_candidates_carry_the_decomposition(self, board, league):
        candidate = build_context(board, league, max_candidates=1).candidates[0]
        assert "vor" in candidate
        assert "draft_score_explained" in candidate
        assert "projected_points" in candidate

    def test_position_filter(self, board, league):
        context = build_context(board, league, max_candidates=5, position="TE")
        assert all(candidate["position"] == "TE" for candidate in context.candidates)

    def test_candidate_names_match_the_payload(self, board, league):
        context = build_context(board, league, max_candidates=5)
        assert context.candidate_names() == [
            candidate["name"] for candidate in context.candidates
        ]

    def test_json_serialisable(self, board, league):
        json.dumps(build_context(board, league, max_candidates=5).to_dict(), default=str)


class TestPrompts:
    def test_system_prompt_states_the_ground_rules(self):
        prompt = build_system_prompt()
        assert "authoritative" in prompt
        assert "Never invent players" in prompt
        assert "THIS league" in prompt
        assert "why the best alternative was NOT chosen" in prompt

    def test_live_mode_asks_for_brevity(self):
        assert "LIVE DRAFT" in build_system_prompt(live=True)
        assert "LIVE DRAFT" not in build_system_prompt(live=False)

    def test_override_replaces_the_prompt(self):
        assert build_system_prompt(override="custom") == "custom"

    def test_recommendation_prompt_embeds_the_context(self, board, league):
        context = build_context(board, league, max_candidates=3)
        prompt = build_recommendation_prompt(context)
        assert "```json" in prompt
        assert context.candidate_names()[0] in prompt
        assert "must appear in the candidates list" in prompt


class TestClientTransport:
    def _client(self, handler, config: LLMConfig | None = None) -> LLMClient:
        return LLMClient(
            config or LLMConfig(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def _completion(self, content: str) -> dict:
        return {
            "model": "test-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        }

    def test_successful_completion(self):
        client = self._client(
            lambda request: httpx.Response(200, json=self._completion('{"ok": 1}'))
        )
        response = client.complete([ChatMessage("user", "hi")])
        assert response.content == '{"ok": 1}'
        assert response.model == "test-model"

    def test_json_schema_rejection_falls_back(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            mode = body.get("response_format", {}).get("type", "none")
            seen.append(mode)
            if mode == "json_schema":
                return httpx.Response(400, json={"error": "unsupported"})
            return httpx.Response(200, json=self._completion('{"ok": 1}'))

        client = self._client(handler)
        response = client.complete([ChatMessage("user", "hi")], schema={"type": "object"})
        assert seen == ["json_schema", "json_object"]
        assert response.structured_mode == "json_object"

    def test_working_mode_is_remembered(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            mode = body.get("response_format", {}).get("type", "none")
            calls.append(mode)
            if mode == "json_schema":
                return httpx.Response(400, json={"error": "unsupported"})
            return httpx.Response(200, json=self._completion("{}"))

        client = self._client(handler)
        client.complete([ChatMessage("user", "a")], schema={"type": "object"})
        client.complete([ChatMessage("user", "b")], schema={"type": "object"})
        # The second request must not retry the mode already known to fail.
        assert calls == ["json_schema", "json_object", "json_object"]

    def test_connection_failure_explains_lm_studio(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = self._client(handler)
        with pytest.raises(LLMError, match="LM Studio"):
            client.complete([ChatMessage("user", "hi")])

    def test_server_error_is_reported(self):
        client = self._client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(LLMError, match="HTTP 500"):
            client.complete([ChatMessage("user", "hi")])

    def test_empty_choices(self):
        client = self._client(lambda request: httpx.Response(200, json={"choices": []}))
        with pytest.raises(LLMError, match="no choices"):
            client.complete([ChatMessage("user", "hi")])

    def test_health_reports_a_missing_model(self):
        client = self._client(
            lambda request: httpx.Response(200, json={"data": [{"id": "other"}]}),
            LLMConfig(model="muse-glimmer"),
        )
        ok, message = client.health()
        assert not ok
        assert "not loaded" in message
        assert "other" in message

    def test_health_success(self):
        client = self._client(
            lambda request: httpx.Response(200, json={"data": [{"id": "muse-glimmer"}]}),
            LLMConfig(model="muse-glimmer"),
        )
        ok, _ = client.health()
        assert ok

    def test_truncation_is_surfaced(self):
        payload = {
            "choices": [{"message": {"content": "{"}, "finish_reason": "length"}],
        }
        client = self._client(lambda request: httpx.Response(200, json=payload))
        assert client.complete([ChatMessage("user", "hi")]).truncated


class TestRecommenderLoop:
    def _reply(self, names: list[str]) -> str:
        return json.dumps(
            {
                "recommendation": names[0],
                "confidence": "high",
                "reasoning": ["Highest VOR", "Unlikely to return"],
                "alternatives": [{"player": names[1], "reason": "safer floor"}],
                "risks": ["projection uncertainty"],
            }
        )

    def _names(self, board, league, count: int = 6) -> list[str]:
        return build_context(board, league, max_candidates=count).candidate_names()

    def test_happy_path(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        client = fake_llm_factory([self._reply(names)])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        assert result.ok
        assert result.recommendation.recommendation == names[0]
        assert result.attempts == 1

    def test_invented_player_triggers_a_correction(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        bad = json.dumps({
            "recommendation": "Totally Invented", "confidence": "high",
            "reasoning": ["vibes"], "alternatives": [], "risks": [],
        })
        client = fake_llm_factory([bad, self._reply(names)])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        assert result.ok
        assert result.attempts == 2
        assert "Totally Invented" in result.failures[0].problem
        # The correction prompt must list the legal names.
        correction = client.requests[1][-1].content
        assert names[0] in correction

    def test_unparseable_reply_triggers_a_correction(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        client = fake_llm_factory(["I think you should take the running back.",
                                   self._reply(names)])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        assert result.ok
        assert result.attempts == 2

    def test_gives_up_after_the_retry_budget(self, board, league, fake_llm_factory):
        client = fake_llm_factory(["nope", "still nope", "nope again"])
        result = Recommender(
            client, league, LLMConfig(max_candidates=6, max_parse_retries=2)
        ).recommend(board)
        assert not result.ok
        assert result.attempts == 3
        rendered = result.render(deterministic_top="Fallback Player")
        assert "Fallback Player" in rendered
        assert "Raw model output" in rendered

    def test_name_near_miss_is_repaired(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        # Drop a suffix / alter spacing the way a model plausibly would.
        mangled = names[0].replace(" ", "  ")
        reply = json.dumps({
            "recommendation": mangled, "confidence": "medium",
            "reasoning": ["fine"], "alternatives": [], "risks": [],
        })
        client = fake_llm_factory([reply])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        assert result.ok
        assert result.recommendation.recommendation == names[0]
        assert result.corrections

    def test_empty_reasoning_is_rejected(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        empty = json.dumps({
            "recommendation": names[0], "confidence": "high",
            "reasoning": [], "alternatives": [], "risks": [],
        })
        client = fake_llm_factory([empty, self._reply(names)])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        assert result.attempts == 2
        assert "reasoning" in result.failures[0].problem

    def test_ask_warns_about_unknown_players(self, board, league, fake_llm_factory):
        reply = json.dumps({
            "answer": "Take the back.",
            "players_referenced": ["Someone Not In Context"],
            "caveats": [],
        })
        client = fake_llm_factory([reply])
        result = Recommender(client, league, LLMConfig(max_candidates=6)).ask(
            board, "Who should I take?"
        )
        assert result.ok
        assert result.unknown_players == ["Someone Not In Context"]
        assert "unverified" in result.render()

    def test_context_is_sent_in_the_user_message(self, board, league, fake_llm_factory):
        names = self._names(board, league)
        client = fake_llm_factory([self._reply(names)])
        Recommender(client, league, LLMConfig(max_candidates=6)).recommend(board)
        messages = client.requests[0]
        assert messages[0].role == "system"
        assert "authoritative" in messages[0].content
        assert '"candidates"' in messages[1].content
