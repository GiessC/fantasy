"""Local LLM integration.

The model interprets analytics; it never produces them.  Nothing in this package
computes a fantasy number, and everything it returns is validated against the
context it was given before the user sees it.
"""

from __future__ import annotations

from .client import ChatMessage, ChatResponse, LLMClient
from .context import DraftContext, build_context
from .parsing import ParseFailure, check_player_names, extract_json, parse_model
from .prompts import SYSTEM_PROMPT, build_recommendation_prompt, build_system_prompt
from .recommender import AnswerResult, RecommendationResult, Recommender
from .schemas import (
    ANSWER_SCHEMA,
    RECOMMENDATION_SCHEMA,
    Alternative,
    Answer,
    Recommendation,
)

__all__ = [
    "ANSWER_SCHEMA",
    "RECOMMENDATION_SCHEMA",
    "SYSTEM_PROMPT",
    "Alternative",
    "Answer",
    "AnswerResult",
    "ChatMessage",
    "ChatResponse",
    "DraftContext",
    "LLMClient",
    "ParseFailure",
    "Recommendation",
    "RecommendationResult",
    "Recommender",
    "build_context",
    "build_recommendation_prompt",
    "build_system_prompt",
    "check_player_names",
    "extract_json",
    "parse_model",
]
