"""Turning analytics into a validated recommendation.

The full loop: build context -> prompt the local model -> extract JSON ->
validate the shape -> verify every player it named exists in the context ->
retry with a correction prompt if any of that fails -> fall back to plain text.

The fallback matters.  A local model that cannot produce clean JSON should not
cost the user their pick, so a failed structured parse degrades to showing the
model's prose alongside the deterministic top candidate, clearly labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analytics import BoardAnalysis
from ..config import LLMConfig, LeagueConfig
from ..draft import DraftStatus, SimulationResult
from ..errors import LLMError
from ..logging_setup import get_logger
from .client import ChatMessage, ChatResponse, LLMClient
from .context import DraftContext, build_context
from .parsing import ParseFailure, check_player_names, parse_model
from .prompts import (
    build_correction_prompt,
    build_question_prompt,
    build_recommendation_prompt,
    build_system_prompt,
)
from .schemas import ANSWER_SCHEMA, RECOMMENDATION_SCHEMA, Answer, Recommendation

log = get_logger(__name__)


@dataclass(slots=True)
class RecommendationResult:
    """A validated recommendation, or the reason there is not one."""

    recommendation: Recommendation | None
    raw_text: str
    context: DraftContext
    attempts: int = 1
    structured_mode: str = "prompt_only"
    failures: list[ParseFailure] = field(default_factory=list)
    corrections: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    latency_seconds: float | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.recommendation is not None

    def render(self, *, deterministic_top: str | None = None) -> str:
        if self.recommendation is not None:
            body = self.recommendation.render()
            if self.corrections:
                fixes = ", ".join(
                    f"{wrote!r} -> {canonical!r}" for wrote, canonical in self.corrections.items()
                )
                body += f"\n\n(Player names normalised: {fixes})"
            return body

        lines = [
            "The model did not return usable structured output "
            f"after {self.attempts} attempt(s).",
        ]
        for failure in self.failures:
            lines.append(f"  - {failure.problem}")
        if deterministic_top:
            lines.append("")
            lines.append(
                f"Deterministic top candidate (no model involved): {deterministic_top}"
            )
        if self.raw_text.strip():
            lines.append("")
            lines.append("Raw model output:")
            lines.append(self.raw_text.strip())
        return "\n".join(lines)


@dataclass(slots=True)
class AnswerResult:
    answer: Answer | None
    raw_text: str
    context: DraftContext
    attempts: int = 1
    failures: list[ParseFailure] = field(default_factory=list)
    unknown_players: list[str] = field(default_factory=list)
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.answer is not None

    def render(self) -> str:
        if self.answer is None:
            body = "The model did not return usable structured output."
            if self.raw_text.strip():
                body += "\n\nRaw model output:\n" + self.raw_text.strip()
            return body
        text = self.answer.render()
        if self.unknown_players:
            text += (
                "\n\nWarning: the model referenced players that are not in the "
                f"supplied context: {', '.join(self.unknown_players)}. "
                "Treat those statements as unverified."
            )
        return text


class Recommender:
    """Asks the local model to interpret an analysed board."""

    def __init__(
        self,
        client: LLMClient,
        league: LeagueConfig,
        config: LLMConfig | None = None,
    ) -> None:
        self.client = client
        self.league = league
        self.config = config or client.config

    # -- public API --------------------------------------------------------

    def recommend(
        self,
        board: BoardAnalysis,
        *,
        status: DraftStatus | None = None,
        simulation: SimulationResult | None = None,
        position: str | None = None,
        live: bool = False,
        max_candidates: int | None = None,
        freshness_lines: list[str] | None = None,
    ) -> RecommendationResult:
        """Produce a validated recommendation."""
        context = build_context(
            board,
            self.league,
            status=status,
            simulation=simulation,
            max_candidates=max_candidates or self.config.max_candidates,
            position=position,
            freshness_lines=freshness_lines,
        )
        messages = [
            ChatMessage("system", build_system_prompt(
                live=live, override=self.config.system_prompt_override
            )),
            ChatMessage("user", build_recommendation_prompt(context)),
        ]
        result = RecommendationResult(
            recommendation=None, raw_text="", context=context
        )
        allowed = context.candidate_names()

        for attempt in range(self.config.max_parse_retries + 1):
            result.attempts = attempt + 1
            response = self._call(messages, RECOMMENDATION_SCHEMA, "recommendation")
            result.raw_text = response.content
            result.structured_mode = response.structured_mode
            result.model = response.model
            result.latency_seconds = response.latency_seconds
            result.truncated = response.truncated

            problem = self._validate_recommendation(response, allowed, result)
            if problem is None:
                return result

            result.failures.append(ParseFailure(problem=problem, raw=response.content))
            if attempt == self.config.max_parse_retries:
                break
            log.info("Retrying with a correction prompt: %s", problem)
            messages.extend(
                [
                    ChatMessage("assistant", response.content),
                    ChatMessage("user", build_correction_prompt(problem)),
                ]
            )

        return result

    def ask(
        self,
        board: BoardAnalysis,
        question: str,
        *,
        status: DraftStatus | None = None,
        simulation: SimulationResult | None = None,
        max_candidates: int | None = None,
        freshness_lines: list[str] | None = None,
    ) -> AnswerResult:
        """Answer a free-form question about the analysis."""
        context = build_context(
            board,
            self.league,
            status=status,
            simulation=simulation,
            max_candidates=max_candidates or self.config.max_candidates,
            freshness_lines=freshness_lines,
        )
        messages = [
            ChatMessage("system", build_system_prompt(
                override=self.config.system_prompt_override
            )),
            ChatMessage("user", build_question_prompt(context, question)),
        ]
        result = AnswerResult(answer=None, raw_text="", context=context)
        allowed = context.candidate_names()

        for attempt in range(self.config.max_parse_retries + 1):
            result.attempts = attempt + 1
            response = self._call(messages, ANSWER_SCHEMA, "answer")
            result.raw_text = response.content
            result.model = response.model
            try:
                answer = parse_model(response.content, Answer)
            except ValueError as exc:
                problem = str(exc)
                result.failures.append(ParseFailure(problem=problem, raw=response.content))
                if attempt == self.config.max_parse_retries:
                    break
                messages.extend(
                    [
                        ChatMessage("assistant", response.content),
                        ChatMessage("user", build_correction_prompt(problem)),
                    ]
                )
                continue

            unknown, corrections = check_player_names(answer.players_referenced, allowed)
            answer.players_referenced = [
                corrections.get(name, name) for name in answer.players_referenced
            ]
            # An unknown name in a free-form answer is a warning rather than a
            # rejection: the user may have asked about a player not in the top
            # candidates, and the answer can still be useful with a caveat.
            result.unknown_players = unknown
            result.answer = answer
            return result

        return result

    # -- internals ---------------------------------------------------------

    def _call(self, messages: list[ChatMessage], schema: dict, name: str) -> ChatResponse:
        try:
            return self.client.complete(messages, schema=schema, schema_name=name)
        except LLMError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise LLMError(f"Unexpected failure calling the local model: {exc}") from exc

    @staticmethod
    def _validate_recommendation(
        response: ChatResponse, allowed: list[str], result: RecommendationResult
    ) -> str | None:
        """Return a problem description, or ``None`` when the reply is usable."""
        try:
            recommendation = parse_model(response.content, Recommendation)
        except ValueError as exc:
            return str(exc)

        unknown, corrections = check_player_names(recommendation.named_players(), allowed)
        if unknown:
            return (
                f"you named {', '.join(repr(name) for name in unknown)}, which "
                f"{'is' if len(unknown) == 1 else 'are'} not in the candidates list. "
                f"Use only these names: {', '.join(allowed)}"
            )

        recommendation.recommendation = corrections.get(
            recommendation.recommendation, recommendation.recommendation
        )
        for alternative in recommendation.alternatives:
            alternative.player = corrections.get(alternative.player, alternative.player)

        if not recommendation.reasoning:
            return "the 'reasoning' array was empty; give at least one bullet"

        result.recommendation = recommendation
        result.corrections = corrections
        return None
