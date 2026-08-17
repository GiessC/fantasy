"""Small shared types and statistics helpers for the analytics package."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class ScoredPlayer:
    """The minimum an analytics routine needs: who, where, and how many points.

    Keeping this separate from :class:`fantasy_ai.models.PlayerData` lets the
    numeric routines be unit-tested with three-line fixtures instead of a
    database.
    """

    player_id: str
    position: str
    points: float
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.player_id


def mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def median(values: Iterable[float]) -> float:
    items = sorted(values)
    if not items:
        return 0.0
    middle = len(items) // 2
    if len(items) % 2:
        return items[middle]
    return (items[middle - 1] + items[middle]) / 2.0


def stdev(values: Iterable[float]) -> float:
    """Sample standard deviation; ``0.0`` for fewer than two values."""
    items = list(values)
    if len(items) < 2:
        return 0.0
    average = mean(items)
    variance = sum((value - average) ** 2 for value in items) / (len(items) - 1)
    return math.sqrt(variance)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """P(X <= x) for a normal distribution."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def normal_sf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """P(X > x)."""
    return 1.0 - normal_cdf(x, mu, sigma)


def group_by_position(players: Sequence[ScoredPlayer]) -> dict[str, list[ScoredPlayer]]:
    """Group players by position, each group sorted by points descending."""
    groups: dict[str, list[ScoredPlayer]] = {}
    for player in players:
        groups.setdefault(player.position, []).append(player)
    for group in groups.values():
        group.sort(key=lambda item: item.points, reverse=True)
    return groups


def rank_map(players: Sequence[ScoredPlayer]) -> dict[str, int]:
    """1-indexed overall rank by points descending."""
    ordered = sorted(players, key=lambda item: item.points, reverse=True)
    return {player.player_id: index for index, player in enumerate(ordered, start=1)}


def position_rank_map(players: Sequence[ScoredPlayer]) -> dict[str, int]:
    """1-indexed rank within each position."""
    ranks: dict[str, int] = {}
    for group in group_by_position(players).values():
        for index, player in enumerate(group, start=1):
            ranks[player.player_id] = index
    return ranks
