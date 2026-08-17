"""The composite draft score.

ANALYTICS.md forbids an opaque weighted score, so this one is built to a
different rule: **every component is already denominated in fantasy points**,
and the total is a sum, not a normalised blend.  A draft score of ``+38.4``
means "about 38 points of draft value over a replacement-level starter, after
accounting for what I would get if I waited, how the player fits my roster, what
the market charges, and how uncertain he is".

Components
----------

``value``
    VOR. The base: points above the replacement-level starter at the position.

``urgency``
    The cost of waiting::

        urgency = (1 - P_available_next_pick) x max(0, VOR - E[best same-position
                  alternative still available at my next pick])

    If a player is near-certain to come back, urgency is ~0 no matter how good
    he is -- you can take someone else now.  If he will certainly be gone, the
    full gap to your fallback is at stake.  ``E[best alternative]`` is the exact
    expectation of the maximum VOR over the position's remaining players under
    independent availability, not just "the next guy".

``roster_fit``
    A non-positive adjustment for redundancy, plus the end-of-draft penalty for
    leaving a starting slot unfilled. See :mod:`.roster_fit`.

``market``
    A **capped shrinkage toward market consensus**, not a bargain bonus::

        market = -weight x (our VOR - market-implied VOR),  clamped to +/- cap

    The direction is deliberate and worth being explicit about, because it is
    the opposite of what "ADP value" intuitively suggests.  The surplus from a
    player falling past his worth is realised by *waiting for him*, and waiting
    is exactly what ``urgency`` already prices -- a player certain to be
    available gets no urgency credit, so nothing is lost.  What remains, once
    that is stripped out, is information: when our projection and the whole
    market disagree, the market has aggregated depth charts, camp reports, and
    beat coverage that our projection source may not have. So we shade toward
    it.

    The cap (``max_market_points``, 8 by default) keeps this a tiebreaker.
    Without it, positions the market structurally ignores -- kickers and
    defenses, whose ADPs sit 150+ picks below where raw VOR puts them -- would
    swamp every other term.  Set the weight to 0 to ignore the market entirely.

``risk``
    The explicit, capped risk discount. See :mod:`.risk`.

``tier_cliff``
    A top-up for the part of a tier drop that ``urgency`` did not already
    capture, capped so the two never double-count::

        tier_cliff = (1 - P_available) x max(0, tier_drop - urgency_gap)

    Usually zero. It becomes non-zero when a player is the last of his tier and
    the fall-off is steeper than the same-position alternatives suggest.

Weights default to ``1.0`` (0.25 for market) so the composite reads in real
points.  Changing a weight expresses a preference, not a unit conversion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import DraftScoreWeights
from .common import ScoredPlayer, clamp
from .market import ADPValue
from .risk import RiskProfile
from .roster_fit import RosterFit
from .scarcity import PlayerScarcity
from .vor import VORResult


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    raw: float
    weight: float
    contribution: float
    description: str

    def describe(self) -> str:
        weight = "" if self.weight == 1.0 else f" x {self.weight:g}"
        return f"{self.name}: {self.raw:+.1f}{weight} = {self.contribution:+.1f} ({self.description})"


@dataclass(slots=True)
class DraftScore:
    player_id: str
    total: float
    components: list[ScoreComponent] = field(default_factory=list)
    expected_best_alternative: float = 0.0
    availability: float | None = None

    def component(self, name: str) -> ScoreComponent | None:
        return next((item for item in self.components if item.name == name), None)

    def contribution(self, name: str) -> float:
        component = self.component(name)
        return component.contribution if component else 0.0

    def explain(self) -> list[str]:
        return [component.describe() for component in self.components]

    def summary(self) -> str:
        parts = [
            f"{component.name} {component.contribution:+.1f}"
            for component in self.components
            if abs(component.contribution) >= 0.05
        ]
        return f"Draft score {self.total:+.1f} = " + " ".join(parts)


def expected_best_alternative_vor(
    alternatives: Sequence[tuple[str, float, float]],
) -> float:
    """Expected best VOR among alternatives still available at the next pick.

    ``alternatives`` is ``(player_id, vor, probability_available)``, and the
    result is the exact expectation of the maximum under independence::

        E[max] = sum_i  vor_i * P(i available) * prod_{j < i} (1 - P(j available))

    with the list ordered by VOR descending, so ``j < i`` are the strictly better
    options that would have been taken instead.  A player who is certain to be
    there truncates the sum, as he should.
    """
    ordered = sorted(alternatives, key=lambda item: item[1], reverse=True)
    expectation = 0.0
    survival = 1.0
    for _, vor, probability in ordered:
        probability = clamp(probability, 0.0, 1.0)
        expectation += vor * probability * survival
        survival *= 1.0 - probability
        if survival <= 1e-6:
            break
    return expectation


def compute_draft_score(
    player: ScoredPlayer,
    *,
    vor: VORResult,
    availability: float | None,
    alternatives: Sequence[tuple[str, float, float]],
    roster_fit: RosterFit | None = None,
    adp_value: ADPValue | None = None,
    risk: RiskProfile | None = None,
    scarcity: PlayerScarcity | None = None,
    weights: DraftScoreWeights | None = None,
    max_market_points: float = 8.0,
) -> DraftScore:
    """Build one player's decomposed draft score."""
    weights = weights or DraftScoreWeights()
    components: list[ScoreComponent] = []

    components.append(
        ScoreComponent(
            name="value",
            raw=vor.vor,
            weight=weights.value,
            contribution=vor.vor * weights.value,
            description=(
                f"{vor.points:.1f} projected - {vor.replacement_points:.1f} "
                f"replacement {vor.position}"
            ),
        )
    )

    probability = 1.0 if availability is None else clamp(availability, 0.0, 1.0)
    best_alternative = expected_best_alternative_vor(
        [item for item in alternatives if item[0] != player.player_id]
    )
    urgency_gap = max(0.0, vor.vor - best_alternative)
    urgency_raw = (1.0 - probability) * urgency_gap
    components.append(
        ScoreComponent(
            name="urgency",
            raw=urgency_raw,
            weight=weights.urgency,
            contribution=urgency_raw * weights.urgency,
            description=(
                f"{(1 - probability):.0%} chance he is gone; fallback at {vor.position} "
                f"worth {best_alternative:+.1f} VOR"
                if availability is not None
                else "no availability estimate; urgency treated as 0"
            ),
        )
    )

    if roster_fit is not None:
        components.append(
            ScoreComponent(
                name="roster_fit",
                raw=roster_fit.total,
                weight=weights.roster_fit,
                contribution=roster_fit.total * weights.roster_fit,
                description=roster_fit.explain(),
            )
        )

    if adp_value is not None and adp_value.adp is not None:
        shrinkage = clamp(
            -adp_value.points_value * weights.market_value,
            -max_market_points,
            max_market_points,
        )
        direction = "above" if adp_value.points_value > 0 else "below"
        components.append(
            ScoreComponent(
                name="market",
                raw=-adp_value.points_value,
                weight=weights.market_value,
                contribution=shrinkage,
                description=(
                    f"our rank {adp_value.our_rank} vs ADP {adp_value.adp:.1f} "
                    f"({adp_value.rank_difference:+.1f} picks); we are "
                    f"{abs(adp_value.points_value):.1f} pts {direction} the market, "
                    f"shading toward it"
                ),
            )
        )

    if risk is not None and risk.discount_points:
        components.append(
            ScoreComponent(
                name="risk",
                raw=-risk.discount_points,
                weight=weights.risk,
                contribution=-risk.discount_points * weights.risk,
                description=risk.explain(),
            )
        )

    if scarcity is not None and scarcity.tier_cliff > 0:
        residual = max(0.0, scarcity.tier_cliff - urgency_gap)
        cliff_raw = (1.0 - probability) * residual
        if cliff_raw > 0.05:
            components.append(
                ScoreComponent(
                    name="tier_cliff",
                    raw=cliff_raw,
                    weight=weights.tier_cliff,
                    contribution=cliff_raw * weights.tier_cliff,
                    description=(
                        f"{scarcity.tier_cliff:.1f}-pt drop to the next {player.position} tier, "
                        f"{scarcity.players_left_in_tier} left in this tier "
                        f"(urgency already covers {urgency_gap:.1f})"
                    ),
                )
            )

    total = sum(component.contribution for component in components)
    return DraftScore(
        player_id=player.player_id,
        total=total,
        components=components,
        expected_best_alternative=best_alternative,
        availability=availability,
    )


def compare_scores(left: DraftScore, right: DraftScore) -> list[str]:
    """Component-by-component explanation of why ``left`` outranks ``right``."""
    names: list[str] = []
    for score in (left, right):
        for component in score.components:
            if component.name not in names:
                names.append(component.name)

    lines = [f"Total: {left.total:+.1f} vs {right.total:+.1f} ({left.total - right.total:+.1f})"]
    for name in names:
        difference = left.contribution(name) - right.contribution(name)
        if abs(difference) < 0.05:
            continue
        lines.append(
            f"  {name}: {left.contribution(name):+.1f} vs {right.contribution(name):+.1f} "
            f"({difference:+.1f})"
        )
    return lines
