"""Deterministic analytics.

Nothing in this package touches the network, the LLM, or a source client.  Every
routine is a pure function of a league configuration and stored data, which is
what makes the numbers reproducible and testable.
"""

from __future__ import annotations

from .availability import AvailabilityEstimate, AvailabilityModel
from .common import ScoredPlayer, group_by_position, mean, median, stdev
from .dataset import Dataset, load_dataset
from .draft_score import (
    DraftScore,
    ScoreComponent,
    compare_scores,
    compute_draft_score,
    expected_best_alternative_vor,
)
from .engine import AnalyticsEngine, BoardAnalysis, PlayerAnalysis
from .lineup import Lineup, LineupSlot, expand_slots, lineup_points, optimal_lineup
from .market import ADPValue, ConsensusProfile, compute_adp_values, compute_consensus
from .replacement import (
    ReplacementLevel,
    ReplacementLevels,
    compute_replacement_levels,
    simulate_starter_pool,
)
from .risk import RiskProfile, compute_risk, injury_severity, is_inactive
from .roster_fit import RosterFit, RosterNeeds, compute_needs, compute_roster_fit
from .scarcity import PlayerScarcity, PositionScarcity, ScarcityResult, compute_scarcity
from .scoring import Scorer, ScoringLine, ScoringResult, expected_games_over
from .tiers import PlayerTier, Tier, TierResult, compute_tiers
from .vor import VORResult, compute_vor, vor_rank_map

__all__ = [
    "ADPValue",
    "AnalyticsEngine",
    "AvailabilityEstimate",
    "AvailabilityModel",
    "BoardAnalysis",
    "ConsensusProfile",
    "Dataset",
    "DraftScore",
    "Lineup",
    "LineupSlot",
    "PlayerAnalysis",
    "PlayerScarcity",
    "PlayerTier",
    "PositionScarcity",
    "ReplacementLevel",
    "ReplacementLevels",
    "RiskProfile",
    "RosterFit",
    "RosterNeeds",
    "ScarcityResult",
    "ScoreComponent",
    "ScoredPlayer",
    "Scorer",
    "ScoringLine",
    "ScoringResult",
    "Tier",
    "TierResult",
    "VORResult",
    "compare_scores",
    "compute_adp_values",
    "compute_consensus",
    "compute_draft_score",
    "compute_needs",
    "compute_replacement_levels",
    "compute_risk",
    "compute_roster_fit",
    "compute_scarcity",
    "compute_tiers",
    "compute_vor",
    "expand_slots",
    "expected_best_alternative_vor",
    "expected_games_over",
    "group_by_position",
    "injury_severity",
    "is_inactive",
    "lineup_points",
    "load_dataset",
    "mean",
    "median",
    "optimal_lineup",
    "simulate_starter_pool",
    "stdev",
    "vor_rank_map",
]
