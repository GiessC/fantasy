"""Extracting and validating JSON from a model reply.

Local models are less reliable than hosted ones at emitting bare JSON, even with
a response-format constraint, so parsing is defensive: strip markdown fences,
find the outermost balanced object, then validate.  Every failure produces a
specific message that is fed back to the model as a correction prompt, because
"your JSON was invalid" corrects far worse than "you used a player name that was
not in the candidate list".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..normalization.identity import normalize_name

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(slots=True)
class ParseFailure:
    """Why a reply could not be used, phrased for the correction prompt."""

    problem: str
    raw: str

    def __str__(self) -> str:
        return self.problem


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model reply, or raise ``ValueError``."""
    candidate = text.strip()
    if not candidate:
        raise ValueError("the reply was empty")

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    balanced = _balanced_object(candidate)
    if balanced is None:
        raise ValueError("no JSON object was found in the reply")
    try:
        return json.loads(balanced)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the JSON object was malformed ({exc.msg} at position {exc.pos})") from exc


def _balanced_object(text: str) -> str | None:
    """The first balanced ``{...}`` span, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_model(text: str, model: type[ModelT]) -> ModelT:
    """Parse a reply into ``model``, raising ``ValueError`` with a usable message."""
    payload = extract_json(text)
    if not isinstance(payload, dict):
        raise ValueError(
            f"the reply was a JSON {type(payload).__name__}, but an object was required"
        )
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'root'}: {error['msg']}"
            for error in exc.errors()[:4]
        )
        raise ValueError(f"the JSON did not match the required shape ({problems})") from exc


def check_player_names(
    named: list[str], allowed: list[str], *, label: str = "candidate list"
) -> tuple[list[str], dict[str, str]]:
    """Verify the model only named players we sent it.

    Returns ``(unknown_names, corrections)``.  Corrections map a name the model
    wrote to the canonical spelling from the context, so near-misses ("Josh
    Allen" for "Joshua Allen") are repaired rather than rejected -- while an
    outright invented player is still caught.
    """
    lookup = {normalize_name(name): name for name in allowed}
    unknown: list[str] = []
    corrections: dict[str, str] = {}

    for name in named:
        if not name:
            continue
        if name in allowed:
            continue
        canonical = lookup.get(normalize_name(name))
        if canonical is not None:
            corrections[name] = canonical
        else:
            unknown.append(name)

    _ = label
    return unknown, corrections


def apply_corrections(value: str, corrections: dict[str, str]) -> str:
    return corrections.get(value, value)
