"""Structured output contracts for the local model.

LLM_INTEGRATION.md: prefer structured JSON, and validate before displaying.  So
the model's reply is parsed into a pydantic model, and validation includes a
check the schema itself cannot express -- **every player the model names must
appear in the candidate list we sent it**.  That is the concrete defence against
the model inventing a player, which is the failure mode that would matter most
during a live draft.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Confidence = Literal["low", "medium", "high"]


class Alternative(BaseModel):
    model_config = ConfigDict(extra="ignore")

    player: str
    reason: str


class Recommendation(BaseModel):
    """The model's draft recommendation."""

    model_config = ConfigDict(extra="ignore")

    recommendation: str = Field(description="Name of the recommended player")
    confidence: Confidence = "medium"
    reasoning: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @field_validator("reasoning", "risks", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        """Accept a single string where a list was asked for."""
        if isinstance(value, str):
            return [value]
        return value

    def named_players(self) -> list[str]:
        return [self.recommendation, *(item.player for item in self.alternatives)]

    def render(self) -> str:
        """The concise live-draft format from LLM_INTEGRATION.md."""
        lines = [f"RECOMMENDATION: {self.recommendation}"]
        if self.confidence:
            lines[0] += f"  (confidence: {self.confidence})"
        if self.reasoning:
            lines.append("")
            lines.append("Why:")
            lines.extend(f"- {reason}" for reason in self.reasoning)
        if self.alternatives:
            lines.append("")
            lines.append("Alternatives:")
            lines.extend(
                f"- {item.player} - {item.reason}" for item in self.alternatives
            )
        if self.risks:
            lines.append("")
            lines.append("Risks:")
            lines.extend(f"- {risk}" for risk in self.risks)
        return "\n".join(lines)


class Answer(BaseModel):
    """A free-form answer about the analysis, still structured enough to check."""

    model_config = ConfigDict(extra="ignore")

    answer: str
    players_referenced: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @field_validator("caveats", "players_referenced", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [value]
        return value

    def render(self) -> str:
        lines = [self.answer]
        if self.caveats:
            lines.append("")
            lines.append("Caveats:")
            lines.extend(f"- {caveat}" for caveat in self.caveats)
        return "\n".join(lines)


#: JSON Schema sent to the server when ``structured_output: json_schema``.
#: Written out rather than generated from the pydantic model because local
#: runtimes' grammar compilers are picky: no ``$ref``, no ``anyOf``, every
#: property required, and ``additionalProperties: false`` throughout.
RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recommendation", "confidence", "reasoning", "alternatives", "risks"],
    "properties": {
        "recommendation": {
            "type": "string",
            "description": "Exact name of the recommended player, copied from the candidate list",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasoning": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short bullet points citing the supplied analytics",
        },
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["player", "reason"],
                "properties": {
                    "player": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "players_referenced", "caveats"],
    "properties": {
        "answer": {"type": "string"},
        "players_referenced": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
}
