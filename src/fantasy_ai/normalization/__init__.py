"""Converting source payloads into canonical models."""

from __future__ import annotations

from .identity import (
    PlayerIndex,
    ResolutionStats,
    ResolvedPlayer,
    canonical_id,
    normalize_name,
    normalize_team,
    split_name,
)
from .stat_mapping import ALIASES, describe_misses, map_stats, resolve_field

__all__ = [
    "ALIASES",
    "PlayerIndex",
    "ResolutionStats",
    "ResolvedPlayer",
    "canonical_id",
    "describe_misses",
    "map_stats",
    "normalize_name",
    "normalize_team",
    "resolve_field",
    "split_name",
]
