"""The analytics engine: orchestration, not new math.

Every number here comes from one of the focused modules in this package.  The
engine's job is to run them in the right order, with the right inputs, and to
package the results so that a CLI table, an LLM context object, and a test can
all read the same structure.

Order matters, and it is a straight line:

    score -> replacement -> VOR -> our board order -> tiers -> scarcity
          -> market -> risk -> availability -> roster fit -> draft score

Draft state is optional.  Without it, the engine analyses the whole board as if
nothing has been drafted; with it, drafted players are removed, roster fit is
measured against the user's actual roster, and availability is measured against
the user's actual next pick.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import AnalyticsConfig, LeagueConfig, SimulationConfig
from ..logging_setup import get_logger
from ..models import PlayerData
from .availability import AvailabilityEstimate, AvailabilityModel
from .common import ScoredPlayer
from .dataset import Dataset
from .draft_score import DraftScore, compute_draft_score
from .lineup import Lineup, optimal_lineup
from .market import ADPValue, ConsensusProfile, compute_adp_values, compute_consensus
from .replacement import ReplacementLevels, compute_replacement_levels
from .risk import RiskProfile, compute_risk, is_inactive
from .roster_fit import RosterFit, RosterNeeds, compute_needs, compute_roster_fit
from .scarcity import PlayerScarcity, ScarcityResult, compute_scarcity
from .scoring import Scorer, ScoringResult
from .tiers import PlayerTier, TierResult, compute_tiers
from .vor import VORResult, compute_vor

log = get_logger(__name__)


@dataclass(slots=True)
class PlayerAnalysis:
    """Every deterministic number the system knows about one player."""

    player_id: str
    name: str
    position: str
    team: str | None
    bye_week: int | None

    projected_points: float
    points_per_game: float
    scoring: ScoringResult

    overall_rank: int
    position_rank: int
    vor: VORResult

    tier: PlayerTier | None = None
    scarcity: PlayerScarcity | None = None
    adp_value: ADPValue | None = None
    consensus: ConsensusProfile | None = None
    risk: RiskProfile | None = None
    availability: AvailabilityEstimate | None = None
    roster_fit: RosterFit | None = None
    draft_score: DraftScore | None = None

    projection_source: str | None = None
    adp_source: str | None = None

    @property
    def score(self) -> float:
        return self.draft_score.total if self.draft_score else self.vor.vor

    @property
    def adp(self) -> float | None:
        return self.adp_value.adp if self.adp_value else None

    def explain(self) -> list[str]:
        """Full decomposition, in the order a human reads it."""
        lines = [
            f"{self.name} ({self.position}{f' - {self.team}' if self.team else ''})",
            f"Projected points: {self.projected_points:.1f} "
            f"({self.points_per_game:.1f}/game, source: {self.projection_source or 'n/a'})",
            f"Our rank: {self.overall_rank} overall, {self.position}{self.position_rank}",
            self.vor.explain(),
        ]
        if self.tier:
            lines.append(
                f"Tier {self.tier.tier} of {self.position} "
                f"({self.tier.players_left_in_tier} left in tier, "
                f"{self.tier.points_to_next_tier:.1f} pts to next tier)"
            )
        if self.adp_value:
            lines.append(self.adp_value.explain())
        if self.consensus:
            lines.append(self.consensus.explain())
        if self.scarcity:
            lines.append(self.scarcity.explain())
        if self.availability:
            lines.append(self.availability.explain())
        if self.roster_fit:
            lines.append(self.roster_fit.explain())
        if self.risk:
            lines.append(self.risk.explain())
        if self.draft_score:
            lines.append(self.draft_score.summary())
            lines.extend(f"  {line}" for line in self.draft_score.explain())
        return lines

    def to_dict(self) -> dict[str, object]:
        """Flat, JSON-safe view -- the shape sent to the LLM and to exports."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "position": self.position,
            "team": self.team,
            "bye_week": self.bye_week,
            "projected_points": round(self.projected_points, 2),
            "points_per_game": round(self.points_per_game, 2),
            "our_rank": self.overall_rank,
            "position_rank": self.position_rank,
            "replacement_points": round(self.vor.replacement_points, 2),
            "vor": round(self.vor.vor, 2),
            "tier": self.tier.tier if self.tier else None,
            "players_left_in_tier": self.tier.players_left_in_tier if self.tier else None,
            "points_to_next_tier": (
                round(self.tier.points_to_next_tier, 2) if self.tier else None
            ),
            "adp": round(self.adp, 1) if self.adp is not None else None,
            "adp_value_picks": (
                round(self.adp_value.rank_difference, 1)
                if self.adp_value and self.adp_value.rank_difference is not None
                else None
            ),
            "adp_value_points": (
                round(self.adp_value.points_value, 2) if self.adp_value else None
            ),
            "expert_stdev": (
                round(self.consensus.expert_stdev, 2)
                if self.consensus and self.consensus.expert_stdev is not None
                else None
            ),
            "consensus_label": self.consensus.label() if self.consensus else None,
            "next_player_delta": (
                round(self.scarcity.next_player_delta, 2) if self.scarcity else None
            ),
            "dropoff_5": round(self.scarcity.dropoff_5, 2) if self.scarcity else None,
            "availability_next_pick": (
                round(self.availability.probability, 4) if self.availability else None
            ),
            "roster_fit": self.roster_fit.label if self.roster_fit else None,
            "roster_fit_points": (
                round(self.roster_fit.total, 2) if self.roster_fit else None
            ),
            "risk": self.risk.level if self.risk else None,
            "risk_points": round(self.risk.discount_points, 2) if self.risk else None,
            "injury_status": self.risk.injury_status if self.risk else None,
            "draft_score": round(self.draft_score.total, 2) if self.draft_score else None,
            "draft_score_components": (
                {
                    component.name: round(component.contribution, 2)
                    for component in self.draft_score.components
                }
                if self.draft_score
                else None
            ),
            "projection_source": self.projection_source,
            "adp_source": self.adp_source,
        }


@dataclass(slots=True)
class BoardAnalysis:
    """The analysed board, plus the league-level context behind it."""

    season: int
    players: list[PlayerAnalysis]
    replacement: ReplacementLevels
    tiers: TierResult
    scarcity: ScarcityResult
    lineup: Lineup | None = None
    needs: RosterNeeds | None = None
    next_pick: int | None = None
    current_pick: int | None = None
    drafted_count: int = 0
    excluded: dict[str, str] = field(default_factory=dict)
    source_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    def by_id(self, player_id: str) -> PlayerAnalysis | None:
        return next((p for p in self.players if p.player_id == player_id), None)

    def top(self, limit: int = 20, position: str | None = None) -> list[PlayerAnalysis]:
        pool = [p for p in self.players if position is None or p.position == position]
        return sorted(pool, key=lambda p: p.score, reverse=True)[:limit]

    def by_position(self) -> dict[str, list[PlayerAnalysis]]:
        grouped: dict[str, list[PlayerAnalysis]] = {}
        for player in self.players:
            grouped.setdefault(player.position, []).append(player)
        for group in grouped.values():
            group.sort(key=lambda p: p.score, reverse=True)
        return grouped


class AnalyticsEngine:
    """Runs the deterministic pipeline over a :class:`Dataset`."""

    def __init__(
        self,
        league: LeagueConfig,
        config: AnalyticsConfig | None = None,
        simulation: SimulationConfig | None = None,
    ) -> None:
        self.league = league
        self.config = config or AnalyticsConfig()
        self.simulation = simulation or SimulationConfig()
        self.scorer = Scorer(league.scoring.compile())
        self.availability_model = AvailabilityModel(self.simulation)

    # -- step 1: projections to points -------------------------------------

    def score_players(
        self, dataset: Dataset
    ) -> tuple[list[ScoredPlayer], dict[str, ScoringResult], dict[str, str]]:
        """Project league-adjusted points for every player with a projection."""
        scored: list[ScoredPlayer] = []
        results: dict[str, ScoringResult] = {}
        excluded: dict[str, str] = {}

        for data in dataset.players.values():
            if data.projection is None:
                excluded[data.player_id] = "no projection"
                continue
            if data.position is None:
                excluded[data.player_id] = "unknown position"
                continue
            if is_inactive(data.player, data.injury):
                status = data.player.status or data.player.injury_status
                excluded[data.player_id] = f"inactive ({status})"
                continue

            result = self.scorer.score(data.stats, data.position)
            if result.points <= 0:
                excluded[data.player_id] = "non-positive projection"
                continue
            results[data.player_id] = result
            scored.append(
                ScoredPlayer(
                    player_id=data.player_id,
                    position=data.position,
                    points=result.points,
                    name=data.name,
                )
            )

        scored.sort(key=lambda player: player.points, reverse=True)
        return scored, results, excluded

    # -- the full pipeline -------------------------------------------------

    def analyze(
        self,
        dataset: Dataset,
        *,
        drafted_ids: Sequence[str] | None = None,
        roster_ids: Sequence[str] | None = None,
        next_pick: int | None = None,
        current_pick: int | None = None,
        picks_remaining: int | None = None,
        availability_override: dict[str, float] | None = None,
        limit: int | None = None,
    ) -> BoardAnalysis:
        """Analyse the board, optionally in the context of a live draft.

        ``availability_override`` lets the Monte Carlo simulator supply
        probabilities in place of the closed-form model; the resulting
        :class:`AvailabilityEstimate` records which one was used.
        """
        drafted = set(drafted_ids or ())
        roster = list(roster_ids or ())

        scored, scoring_results, excluded = self.score_players(dataset)
        by_id = {player.player_id: player for player in scored}

        # Replacement level is a property of the *league*, computed over the
        # full player pool, not over who happens to be left. Recomputing it as
        # players come off the board would make VOR drift during a draft.
        replacement = compute_replacement_levels(scored, self.league, self.config.replacement)

        available = [player for player in scored if player.player_id not in drafted]
        if limit is not None:
            available = available[: max(limit, 1)]

        vor_results = compute_vor(scored, replacement, self.league)
        our_ranks = self._board_ranks(scored, vor_results)

        tiers = compute_tiers(available, self.config.tiers)

        remaining_demand = self._remaining_demand(replacement, dataset, drafted)
        scarcity = compute_scarcity(
            available, replacement, tiers, remaining_demand=remaining_demand
        )

        adp_records = dataset.adp_records()
        vor_by_id = {player_id: result.vor for player_id, result in vor_results.items()}
        adp_values = compute_adp_values(
            available, our_ranks, adp_records, vor_by_id, dataset.ranking_records()
        )
        consensus = compute_consensus(
            dataset.ranking_records(), our_ranks, extra_rankings=dataset.all_rankings
        )

        availability = self._availability(
            available, adp_records, next_pick, availability_override, current_pick
        )

        roster_players = [by_id[pid] for pid in roster if pid in by_id]
        remaining_picks = (
            picks_remaining
            if picks_remaining is not None
            else max(0, self.league.effective_rounds - len(roster_players))
        )
        best_available = self._best_available_by_position(available)
        roster_fits = compute_roster_fit(
            available,
            roster_players,
            self.league,
            picks_remaining=remaining_picks,
            bench_start_share=self.config.bench_start_share,
            best_available=best_available,
        )

        alternatives_by_position = self._alternatives(available, vor_results, availability)

        analyses: list[PlayerAnalysis] = []
        for player in available:
            data: PlayerData = dataset.players[player.player_id]
            vor = vor_results[player.player_id]
            estimate = availability.get(player.player_id)
            risk = compute_risk(
                data.player,
                consensus=consensus.get(player.player_id),
                injury=data.injury,
                projected_points=player.points,
                config=self.config.risk,
            )
            scarcity_entry = scarcity.by_player.get(player.player_id)
            score = compute_draft_score(
                player,
                vor=vor,
                availability=estimate.probability if estimate else None,
                alternatives=alternatives_by_position.get(player.position, []),
                roster_fit=roster_fits.get(player.player_id),
                adp_value=adp_values.get(player.player_id),
                risk=risk,
                scarcity=scarcity_entry,
                weights=self.config.weights,
                max_market_points=self.config.weights.max_market_points,
            )
            scoring_result = scoring_results[player.player_id]
            analyses.append(
                PlayerAnalysis(
                    player_id=player.player_id,
                    name=player.name,
                    position=player.position,
                    team=data.player.team,
                    bye_week=data.player.bye_week,
                    projected_points=player.points,
                    points_per_game=scoring_result.points_per_game,
                    scoring=scoring_result,
                    overall_rank=our_ranks.get(player.player_id, 0),
                    position_rank=self._position_rank(player, scored, our_ranks),
                    vor=vor,
                    tier=tiers.player_tiers.get(player.player_id),
                    scarcity=scarcity_entry,
                    adp_value=adp_values.get(player.player_id),
                    consensus=consensus.get(player.player_id),
                    risk=risk,
                    availability=estimate,
                    roster_fit=roster_fits.get(player.player_id),
                    draft_score=score,
                    projection_source=data.projection.source if data.projection else None,
                    adp_source=data.adp.source if data.adp else None,
                )
            )

        analyses.sort(key=lambda item: item.score, reverse=True)

        lineup = optimal_lineup(roster_players, self.league) if roster_players else None
        needs = compute_needs(roster_players, self.league, picks_remaining=remaining_picks)

        return BoardAnalysis(
            season=dataset.season,
            players=analyses,
            replacement=replacement,
            tiers=tiers,
            scarcity=scarcity,
            lineup=lineup,
            needs=needs,
            next_pick=next_pick,
            current_pick=current_pick,
            drafted_count=len(drafted),
            excluded=excluded,
            source_usage=dataset.source_usage,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _board_ranks(
        scored: Sequence[ScoredPlayer], vor_results: dict[str, VORResult]
    ) -> dict[str, int]:
        """Our board order: VOR descending, points as the tiebreak.

        VOR rather than raw points, because raw points would rank every QB above
        every RB in a one-QB league -- the exact mistake replacement level exists
        to correct.
        """
        ordered = sorted(
            scored,
            key=lambda player: (
                vor_results[player.player_id].vor if player.player_id in vor_results else 0.0,
                player.points,
            ),
            reverse=True,
        )
        return {player.player_id: index for index, player in enumerate(ordered, start=1)}

    @staticmethod
    def _position_rank(
        player: ScoredPlayer, scored: Sequence[ScoredPlayer], our_ranks: dict[str, int]
    ) -> int:
        same = sorted(
            (item for item in scored if item.position == player.position),
            key=lambda item: our_ranks.get(item.player_id, 10**6),
        )
        for index, item in enumerate(same, start=1):
            if item.player_id == player.player_id:
                return index
        return 0

    def _remaining_demand(
        self, replacement: ReplacementLevels, dataset: Dataset, drafted: set[str]
    ) -> dict[str, int]:
        """League-wide starting demand still unfilled, for scarcity."""
        remaining = dict(replacement.starter_demand)
        for player_id in drafted:
            data = dataset.players.get(player_id)
            position = data.position if data else None
            if position and remaining.get(position, 0) > 0:
                remaining[position] -= 1
        return remaining

    def _availability(
        self,
        available: Sequence[ScoredPlayer],
        adp_records: dict,
        next_pick: int | None,
        override: dict[str, float] | None,
        current_pick: int | None = None,
    ) -> dict[str, AvailabilityEstimate]:
        if next_pick is None:
            return {}
        if current_pick is not None and next_pick <= current_pick:
            # You are on the clock: every player still on the board is
            # available to you right now, whatever their ADP says. The ADP
            # model would otherwise report a player you can draft this second
            # as only 65% likely to be there.
            return {
                player.player_id: AvailabilityEstimate(
                    player_id=player.player_id,
                    target_pick=next_pick,
                    probability=1.0,
                    adp=None,
                    sigma=None,
                    method="on-the-clock",
                )
                for player in available
            }
        estimates = self.availability_model.estimate_many(
            [player.player_id for player in available], next_pick, adp_records
        )
        if override:
            for player_id, probability in override.items():
                existing = estimates.get(player_id)
                estimates[player_id] = AvailabilityEstimate(
                    player_id=player_id,
                    target_pick=next_pick,
                    probability=probability,
                    adp=existing.adp if existing else None,
                    sigma=existing.sigma if existing else None,
                    method="monte-carlo",
                )
        return estimates

    @staticmethod
    def _best_available_by_position(available: Sequence[ScoredPlayer]) -> dict[str, float]:
        best: dict[str, float] = {}
        for player in available:
            if player.points > best.get(player.position, 0.0):
                best[player.position] = player.points
        return best

    @staticmethod
    def _alternatives(
        available: Sequence[ScoredPlayer],
        vor_results: dict[str, VORResult],
        availability: dict[str, AvailabilityEstimate],
    ) -> dict[str, list[tuple[str, float, float]]]:
        """Per-position ``(player_id, vor, p_available)`` for the urgency term."""
        grouped: dict[str, list[tuple[str, float, float]]] = {}
        for player in available:
            vor = vor_results[player.player_id].vor if player.player_id in vor_results else 0.0
            estimate = availability.get(player.player_id)
            probability = estimate.probability if estimate else 1.0
            grouped.setdefault(player.position, []).append(
                (player.player_id, vor, probability)
            )
        for group in grouped.values():
            group.sort(key=lambda item: item[1], reverse=True)
        return grouped
