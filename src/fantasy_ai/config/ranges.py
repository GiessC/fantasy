"""Parsing for the numeric range keys used in scoring YAML.

Leagues express field-goal distance and points-allowed scoring as ranges::

    "0-39": 3
    "40-49": 4
    "50+": 5

This module turns those keys into closed/half-open intervals and provides the
overlap arithmetic the scoring compiler needs when a league's ranges do not line
up exactly with our canonical stat buckets.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigError

_SINGLE = re.compile(r"^(-?\d+(?:\.\d+)?)$")
_CLOSED = re.compile(r"^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$")
_OPEN_UP = re.compile(r"^(-?\d+(?:\.\d+)?)\s*\+$")
_OPEN_DOWN = re.compile(r"^<=?\s*(-?\d+(?:\.\d+)?)$")


@dataclass(frozen=True, slots=True)
class Range:
    """An inclusive numeric interval. ``upper`` of ``inf`` means unbounded."""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        if self.upper < self.lower:
            raise ConfigError(f"Range upper bound {self.upper} is below lower bound {self.lower}.")

    def contains(self, value: float) -> bool:
        return self.lower <= value <= self.upper

    @property
    def width(self) -> float:
        """Inclusive integer width; unbounded ranges report ``inf``."""
        if math.isinf(self.upper):
            return math.inf
        return self.upper - self.lower + 1

    def overlap(self, other: Range) -> float:
        """Inclusive integer overlap with another range."""
        lower = max(self.lower, other.lower)
        upper = min(self.upper, other.upper)
        if upper < lower:
            return 0.0
        if math.isinf(upper):
            return math.inf
        return upper - lower + 1

    def __str__(self) -> str:
        if math.isinf(self.upper):
            return f"{self.lower:g}+"
        if self.lower == self.upper:
            return f"{self.lower:g}"
        return f"{self.lower:g}-{self.upper:g}"


def parse_range(key: str | int | float) -> Range:
    """Parse ``"0-39"``, ``"50+"``, ``"7"``, or ``"<=6"`` into a :class:`Range`."""
    text = str(key).strip()
    if not text:
        raise ConfigError("Empty range key in scoring configuration.")

    if match := _SINGLE.match(text):
        value = float(match.group(1))
        return Range(value, value)
    if match := _CLOSED.match(text):
        return Range(float(match.group(1)), float(match.group(2)))
    if match := _OPEN_UP.match(text):
        return Range(float(match.group(1)), math.inf)
    if match := _OPEN_DOWN.match(text):
        return Range(-math.inf, float(match.group(1)))

    raise ConfigError(
        f"Cannot parse range key {key!r}. Use forms like '7', '0-39', '50+', or '<=6'."
    )


def parse_range_table(table: Mapping[Any, float]) -> list[tuple[Range, float]]:
    """Parse a whole ``{range_key: points}`` table, sorted by lower bound.

    Raises when two ranges overlap, since that makes scoring ambiguous.
    """
    parsed = [(parse_range(key), float(points)) for key, points in table.items()]
    parsed.sort(key=lambda item: item[0].lower)

    for (left, _), (right, _) in zip(parsed, parsed[1:], strict=False):
        if left.overlap(right) > 0:
            raise ConfigError(
                f"Scoring ranges {left} and {right} overlap. Ranges must be disjoint."
            )
    return parsed


def lookup(table: list[tuple[Range, float]], value: float) -> float:
    """Points for ``value``, or ``0.0`` when it falls outside every range."""
    for rng, points in table:
        if rng.contains(value):
            return points
    return 0.0
