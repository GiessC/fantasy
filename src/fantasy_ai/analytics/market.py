"""Market comparison: ADP value and expert-consensus disagreement.

Two distinct questions, deliberately kept apart:

**ADP value** compares our board position to where the market drafts a player.
A positive ``rank_difference`` means the market lets him fall past where we rank
him.  Alongside the rank difference (what people quote) we compute the same
disagreement in points::

    market_implied_vor = VOR of whoever our board ranks at slot round(ADP)
    points_value       = our VOR - market_implied_vor

so a positive ``points_value`` means "we think he is worth this many more points
than the market is paying for him".

Note the rank difference is converted through **our own board's VOR curve**, not
through a local slope.  An earlier version multiplied the rank gap by the local
points-per-slot slope, which blows up for large gaps: a kicker whose ADP is 220
but whose VOR places him at board slot 60 produced a fictitious 150-point
"discount" and took over the board.  Reading the curve at both endpoints keeps
the number bounded by the actual span of board value.

How this feeds the draft score is a separate decision, documented in
:mod:`.draft_score`: the surplus from a falling player is realised by *waiting*,
which availability already prices, so the score uses this number as a capped
shrinkage toward market consensus rather than as a bonus.

**Consensus disagreement** describes the spread among the experts themselves
(best/worst/stdev from FantasyPros' expert panel).  A wide spread is not the
same thing as a market discount: it means the player is *polarising*, which is
information about risk and upside, not about value.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..models import ADPRecord, RankingRecord
from .common import ScoredPlayer, mean, median, stdev


@dataclass(slots=True)
class ADPValue:
    player_id: str
    our_rank: int
    adp: float | None
    adp_source: str | None
    #: ``adp - our_rank``; positive means the market drafts him later than we rank him.
    rank_difference: float | None
    #: ``our VOR - VOR of whoever our board ranks at slot round(ADP)``.
    #: Positive means we value him above what the market is paying.
    points_value: float
    #: The VOR our board assigns to the ADP slot itself.
    market_implied_vor: float = 0.0
    consensus_rank: float | None = None
    #: Difference between our rank and expert consensus rank.
    consensus_difference: float | None = None

    def label(self) -> str:
        if self.rank_difference is None:
            return "no ADP"
        if self.rank_difference >= 12:
            return "clear value"
        if self.rank_difference >= 5:
            return "slight value"
        if self.rank_difference <= -12:
            return "clear reach"
        if self.rank_difference <= -5:
            return "slight reach"
        return "at market"

    def explain(self) -> str:
        if self.adp is None:
            return f"Our rank {self.our_rank}; no ADP available."
        direction = "above" if self.points_value >= 0 else "below"
        return (
            f"Our rank {self.our_rank} vs ADP {self.adp:.1f} "
            f"({self.rank_difference:+.1f} picks) -- {self.label()}; "
            f"we are {abs(self.points_value):.1f} pts {direction} the market's price "
            f"for that slot"
        )


@dataclass(slots=True)
class ConsensusProfile:
    player_id: str
    expert_mean: float | None = None
    expert_median: float | None = None
    expert_stdev: float | None = None
    best: float | None = None
    worst: float | None = None
    expert_count: int | None = None
    our_rank: int | None = None
    #: ``expert_mean - our_rank``; positive means we like him more than the panel.
    model_vs_consensus: float | None = None
    #: This player's expert spread divided by the typical spread at his rank.
    #: 1.0 is exactly average disagreement; above 1 is unusually polarising.
    disagreement_index: float = 1.0
    #: The typical spread the index was measured against, for explanations.
    typical_stdev: float | None = None

    def label(self) -> str:
        if self.expert_stdev is None:
            return "unknown"
        if self.disagreement_index >= 1.5:
            return "polarising"
        if self.disagreement_index >= 1.15:
            return "some disagreement"
        if self.disagreement_index <= 0.7:
            return "strong consensus"
        return "consensus"

    def explain(self) -> str:
        if self.expert_mean is None:
            return "No expert ranking data."
        parts = [
            f"Experts: mean {self.expert_mean:.1f}",
        ]
        if self.expert_median is not None:
            parts.append(f"median {self.expert_median:.1f}")
        if self.expert_stdev is not None:
            parts.append(f"sd {self.expert_stdev:.1f}")
        if self.best is not None and self.worst is not None:
            parts.append(f"range {self.best:.0f}-{self.worst:.0f}")
        if self.typical_stdev is not None:
            parts.append(f"typical sd at this rank {self.typical_stdev:.1f}")
        if self.model_vs_consensus is not None:
            direction = "higher" if self.model_vs_consensus > 0 else "lower"
            parts.append(f"we are {abs(self.model_vs_consensus):.1f} ranks {direction}")
        return ", ".join(parts) + f" -- {self.label()}"


class BoardValueCurve:
    """Our board's VOR as a function of board slot.

    Lets any draft slot be priced in points: "what is a pick at slot 31 worth on
    our board?"  Values are read from the sorted VOR list with linear
    interpolation, and clamped at both ends so an ADP deeper than our pool
    prices at the pool's floor rather than running off the end.
    """

    def __init__(self, vor_by_rank: Sequence[float]) -> None:
        self.values = list(vor_by_rank)

    @classmethod
    def from_board(
        cls, our_ranks: dict[str, int], vor_by_id: dict[str, float]
    ) -> BoardValueCurve:
        ordered = sorted(
            (rank, vor_by_id.get(player_id, 0.0)) for player_id, rank in our_ranks.items()
        )
        return cls([vor for _, vor in ordered])

    def at(self, slot: float) -> float:
        if not self.values:
            return 0.0
        position = max(1.0, min(float(slot), float(len(self.values))))
        low_index = int(position) - 1
        high_index = min(low_index + 1, len(self.values) - 1)
        fraction = position - int(position)
        return (
            self.values[low_index] * (1 - fraction) + self.values[high_index] * fraction
        )


def compute_adp_values(
    players: Sequence[ScoredPlayer],
    our_ranks: dict[str, int],
    adp_records: dict[str, ADPRecord],
    vor_by_id: dict[str, float],
    rankings: dict[str, RankingRecord] | None = None,
) -> dict[str, ADPValue]:
    """ADP value for each player, using our own board order as the reference."""
    curve = BoardValueCurve.from_board(our_ranks, vor_by_id)
    rankings = rankings or {}

    results: dict[str, ADPValue] = {}
    for player in players:
        our_rank = our_ranks.get(player.player_id, 0)
        record = adp_records.get(player.player_id)
        adp = record.adp if record else None
        difference = (adp - our_rank) if adp is not None else None
        our_vor = vor_by_id.get(player.player_id, 0.0)
        market_implied = curve.at(adp) if adp is not None else our_vor
        points_value = our_vor - market_implied if adp is not None else 0.0

        ranking = rankings.get(player.player_id)
        consensus_rank = ranking.ecr if ranking else None
        results[player.player_id] = ADPValue(
            player_id=player.player_id,
            our_rank=our_rank,
            adp=adp,
            adp_source=record.source if record else None,
            rank_difference=difference,
            points_value=points_value,
            market_implied_vor=market_implied,
            consensus_rank=consensus_rank,
            consensus_difference=(
                consensus_rank - our_rank if consensus_rank is not None else None
            ),
        )
    return results


def compute_consensus(
    rankings: dict[str, RankingRecord],
    our_ranks: dict[str, int],
    *,
    extra_rankings: dict[str, list[RankingRecord]] | None = None,
) -> dict[str, ConsensusProfile]:
    """Consensus profile per player.

    Primary input is a source's own expert dispersion (FantasyPros publishes
    best/worst/average/stdev over its expert panel).  When several *sources*
    ranked the player, their disagreement is folded in too, since cross-source
    disagreement is the same kind of signal.

    Dispersion is then measured **relative to what is normal at that rank**,
    using the median spread among nearby-ranked players in this very dataset.
    Expert spread grows with rank -- everyone agrees about the top pick, nobody
    agrees about the 150th -- so a raw standard deviation says almost nothing on
    its own, and any fixed formula for normalising it (``sd / sqrt(rank)`` and
    friends) is a magic constant that mislabels one end of the board or the
    other.  Calibrating against the data makes the index self-adjusting: 1.0 is
    ordinary disagreement for that part of the board, 2.0 is genuinely
    polarising.
    """
    profiles: dict[str, ConsensusProfile] = {}
    extra_rankings = extra_rankings or {}
    typical = _typical_stdev_by_rank(rankings)

    for player_id, ranking in rankings.items():
        our_rank = our_ranks.get(player_id)
        others = [
            record.ecr
            for record in extra_rankings.get(player_id, [])
            if record.ecr is not None
        ]

        expert_mean = ranking.average if ranking.average is not None else ranking.ecr
        expert_stdev = ranking.stdev
        expert_median = ranking.ecr

        if len(others) > 1:
            cross_mean = mean(others)
            cross_stdev = stdev(others)
            expert_mean = expert_mean if expert_mean is not None else cross_mean
            expert_median = median(others)
            # Combine within-panel and across-source dispersion in quadrature.
            if expert_stdev is None:
                expert_stdev = cross_stdev
            else:
                expert_stdev = (expert_stdev**2 + cross_stdev**2) ** 0.5

        reference = expert_mean if expert_mean else (ranking.ecr or 0.0)
        baseline = typical(reference) if reference else None
        disagreement = 1.0
        if expert_stdev is not None and baseline:
            disagreement = expert_stdev / baseline

        profiles[player_id] = ConsensusProfile(
            typical_stdev=baseline,
            player_id=player_id,
            expert_mean=expert_mean,
            expert_median=expert_median,
            expert_stdev=expert_stdev,
            best=ranking.best,
            worst=ranking.worst,
            expert_count=ranking.expert_count,
            our_rank=our_rank,
            model_vs_consensus=(
                expert_mean - our_rank
                if expert_mean is not None and our_rank is not None
                else None
            ),
            disagreement_index=disagreement,
        )
    return profiles


def _typical_stdev_by_rank(
    rankings: dict[str, RankingRecord],
) -> Callable[[float], float | None]:
    """Build a lookup for "normal" expert spread at a given rank.

    Uses the median spread of the nearest-ranked players in this dataset, so it
    adapts to whatever source and season are loaded.  Returns a callable so the
    sorted arrays are built once rather than per player.
    """
    points = sorted(
        (
            (record.average if record.average is not None else record.ecr, record.stdev)
            for record in rankings.values()
        ),
        key=lambda item: (item[0] is None, item[0] or 0.0),
    )
    ranks = [rank for rank, spread in points if rank is not None and spread is not None]
    spreads = [spread for rank, spread in points if rank is not None and spread is not None]

    if len(ranks) < 5:
        return lambda rank: None

    import bisect

    window = max(5, len(ranks) // 12)

    def typical(rank: float) -> float | None:
        index = bisect.bisect_left(ranks, rank)
        low = max(0, index - window)
        high = min(len(spreads), index + window)
        neighbourhood = spreads[low:high]
        if not neighbourhood:
            return None
        value = median(neighbourhood)
        # Guard against a degenerate all-zero neighbourhood.
        return value if value > 1e-6 else None

    return typical
