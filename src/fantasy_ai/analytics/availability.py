"""Closed-form availability probability.

    P(player is still on the board at overall pick N)

Model: a player's realised draft slot ``D`` is normally distributed around their
ADP.  Availability at pick ``N`` is then ``P(D >= N)``.

Sigma comes from the source's own ADP standard deviation when published.
Otherwise it is ``clamp(fraction * adp, floor, ceiling)`` -- dispersion grows
roughly proportionally with ADP (nobody is unsure about pick 1; everyone is
unsure about pick 140), with a floor so early picks are not treated as certain
and a ceiling so deep sleepers do not become uniformly random.

Two known limitations, which is why the Monte Carlo simulator
(:mod:`fantasy_ai.draft.simulator`) is the authoritative estimate and this
module is the fast fallback:

* It treats players independently. In reality exactly one player goes per pick,
  so their availabilities are negatively correlated.
* It ignores roster construction. A run on tight ends is a real, correlated
  event this model cannot see.

Both errors push the same direction (this model is slightly optimistic about
mid-round players surviving), and the CLI labels which estimator produced a
number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..config import SimulationConfig
from ..models import ADPRecord
from .common import clamp, normal_sf


@dataclass(slots=True)
class AvailabilityEstimate:
    player_id: str
    target_pick: int
    probability: float
    adp: float | None
    sigma: float | None
    method: str = "analytic"

    def explain(self) -> str:
        if self.adp is None:
            return f"No ADP; availability at pick {self.target_pick} unknown."
        return (
            f"P(available at pick {self.target_pick}) = {self.probability:.0%} "
            f"(ADP {self.adp:.1f}, sd {self.sigma:.1f}, {self.method})"
        )


class AvailabilityModel:
    """Closed-form availability estimator driven by :class:`SimulationConfig`."""

    def __init__(self, config: SimulationConfig | None = None) -> None:
        self.config = config or SimulationConfig()

    def sigma_for(self, adp: float, published_stdev: float | None = None) -> float:
        """Standard deviation of a player's draft slot, in picks."""
        if published_stdev is not None and published_stdev > 0:
            return float(published_stdev)
        return clamp(
            self.config.adp_noise_fraction * adp,
            self.config.adp_noise_floor,
            self.config.adp_noise_ceiling,
        )

    def probability(
        self, adp: float, target_pick: int, published_stdev: float | None = None
    ) -> float:
        """P(a player with this ADP survives to ``target_pick``).

        A half-pick continuity correction is applied because draft slots are
        integers while the model is continuous.
        """
        sigma = self.sigma_for(adp, published_stdev)
        return clamp(normal_sf(target_pick - 0.5, mu=adp, sigma=sigma), 0.0, 1.0)

    def estimate(
        self,
        player_id: str,
        target_pick: int,
        adp_record: ADPRecord | None,
        *,
        max_known_adp: float | None = None,
    ) -> AvailabilityEstimate:
        """Availability for one player.

        Players with no ADP are assumed to go ``undrafted_adp_padding`` picks
        past the deepest ADP we do know, which makes them very likely available
        rather than unknown.
        """
        if adp_record is None:
            if max_known_adp is None:
                return AvailabilityEstimate(
                    player_id=player_id,
                    target_pick=target_pick,
                    probability=1.0,
                    adp=None,
                    sigma=None,
                    method="no-adp (assumed available)",
                )
            assumed = max_known_adp + self.config.undrafted_adp_padding
            sigma = self.sigma_for(assumed)
            return AvailabilityEstimate(
                player_id=player_id,
                target_pick=target_pick,
                probability=self.probability(assumed, target_pick),
                adp=assumed,
                sigma=sigma,
                method="analytic (imputed ADP)",
            )

        sigma = self.sigma_for(adp_record.adp, adp_record.stdev)
        return AvailabilityEstimate(
            player_id=player_id,
            target_pick=target_pick,
            probability=self.probability(adp_record.adp, target_pick, adp_record.stdev),
            adp=adp_record.adp,
            sigma=sigma,
            method="analytic",
        )

    def estimate_many(
        self,
        player_ids: Sequence[str],
        target_pick: int,
        adp_records: dict[str, ADPRecord],
    ) -> dict[str, AvailabilityEstimate]:
        known = [record.adp for record in adp_records.values()]
        max_known = max(known) if known else None
        return {
            player_id: self.estimate(
                player_id, target_pick, adp_records.get(player_id), max_known_adp=max_known
            )
            for player_id in player_ids
        }
