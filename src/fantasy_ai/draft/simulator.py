"""Monte Carlo draft simulation.

DRAFT_SIMULATION.md's requirement: ADP is a *distribution*, not a schedule.
Selecting players in exact ADP order would make every availability probability 0
or 1, which is both wrong and useless.

Model for one simulated pick by another team:

1. Take the shallowest ``candidate_pool`` available players by ADP.
2. Give each a perturbed draft priority::

       priority = adp + need_shift + Normal(0, sigma)

   ``sigma`` is the source's published ADP standard deviation, or
   ``clamp(fraction * adp, floor, ceiling)`` when it is not published.
3. The lowest priority is selected.

``need_shift`` is measured **in picks** -- "this team will reach about three
picks early for a position it still has to start" -- and scales with
``need_weight * teams``.  It is deliberately additive: an earlier version scaled
ADP multiplicatively, which uniformly compressed the gaps between players and
let the noise term swamp the ADP signal, so obvious first-round picks survived
into the second round far too often.

Positional caps stop a simulated team from taking a third quarterback in a
one-QB league -- something raw ADP sampling does occasionally, and which visibly
distorts late-round availability.  A team at its cap for a position simply does
not consider it.

What this does *not* claim: it does not predict any specific opponent.  It is a
decision-support model over plausible draft flows.  Where real league history is
available, :class:`DrafterProfile` lets per-opponent tendencies replace the
generic model.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import LeagueConfig, SimulationConfig
from ..logging_setup import get_logger
from ..models import ADPRecord
from .order import slot_for_pick

log = get_logger(__name__)


@dataclass(slots=True)
class SimPlayer:
    """A player as the simulator sees them."""

    player_id: str
    position: str
    name: str
    points: float
    vor: float
    adp: float
    sigma: float


@dataclass(slots=True)
class DrafterProfile:
    """Optional per-opponent tendencies learned from real draft history.

    Both fields are measured in picks: ``position_bias`` shifts a position's
    effective ADP for this drafter (negative = reaches for it), and
    ``reach_tendency`` shifts every pick (negative = drafts ahead of ADP
    generally).
    """

    slot: int
    position_bias: dict[str, float] = field(default_factory=dict)
    reach_tendency: float = 0.0
    name: str | None = None


@dataclass(slots=True)
class AvailabilityResult:
    player_id: str
    name: str
    position: str
    probability: float
    #: Mean simulated pick at which the player came off the board (None if he
    #: usually survived the simulated window).
    mean_taken_pick: float | None
    times_available: int
    iterations: int

    def explain(self, target_pick: int) -> str:
        base = (
            f"{self.name}: available at pick {target_pick} in "
            f"{self.times_available}/{self.iterations} simulations ({self.probability:.0%})"
        )
        if self.mean_taken_pick is not None:
            base += f"; when taken, mean pick {self.mean_taken_pick:.1f}"
        return base


@dataclass(slots=True)
class SimulationResult:
    """Availability across the window between now and the user's next pick."""

    target_pick: int
    current_pick: int
    iterations: int
    seed: int | None
    availability: dict[str, AvailabilityResult]
    #: Expected VOR of the best player left at each position at ``target_pick``.
    expected_best_by_position: dict[str, float]
    #: Expected VOR of the single best player left overall.
    expected_best_overall: float
    simulated_picks: int

    def probability(self, player_id: str) -> float | None:
        entry = self.availability.get(player_id)
        return entry.probability if entry else None

    def top_available(self, limit: int = 10) -> list[AvailabilityResult]:
        return sorted(
            self.availability.values(), key=lambda item: item.probability, reverse=True
        )[:limit]

    def summary(self) -> str:
        return (
            f"{self.iterations} simulations over {self.simulated_picks} picks "
            f"(pick {self.current_pick} -> {self.target_pick}), seed {self.seed}"
        )


@dataclass(slots=True)
class StrategyOutcome:
    """Result of committing to one player now and simulating the rest."""

    player_id: str
    name: str
    position: str
    mean_roster_points: float
    stdev_roster_points: float
    mean_starter_points: float
    iterations: int
    #: Mean projected points of the rest of the roster acquired after this pick.
    mean_future_points: float

    def explain(self) -> str:
        return (
            f"{self.name} ({self.position}): mean starting lineup "
            f"{self.mean_starter_points:.1f} pts "
            f"(sd {self.stdev_roster_points:.1f}) over {self.iterations} simulations"
        )


class DraftSimulator:
    """Simulates the picks between now and a target pick."""

    def __init__(
        self,
        league: LeagueConfig,
        config: SimulationConfig | None = None,
        *,
        profiles: Sequence[DrafterProfile] | None = None,
    ) -> None:
        self.league = league
        self.config = config or SimulationConfig()
        self.profiles = {profile.slot: profile for profile in (profiles or ())}
        # Cached once: these walk the league's roster slots and rebuild typed
        # models, which is far too expensive to redo per candidate per pick.
        self._dedicated = league.dedicated_starters()
        self._flex = league.flex_capacity()
        self._caps = self._position_caps()
        #: Need shifts, in picks, precomputed per position and need tier.
        self._need_scale = self.config.need_weight * league.teams

    # -- setup -------------------------------------------------------------

    def _position_caps(self) -> dict[str, int]:
        """How many of each position a simulated team will ever draft.

        Derived from the league, not hard-coded: dedicated starting slots, plus
        flex capacity, plus bench depth that scales with how usable the position
        is off the bench.  A 1-QB league caps QBs at 2; a superflex league
        derives a deeper cap automatically because QB gains flex capacity.
        """
        dedicated = self._dedicated
        flex = self._flex
        caps: dict[str, int] = {}
        for position in self.league.drafted_positions:
            base = dedicated.get(position, 0) + flex.get(position, 0)
            if position in {"K", "DST"}:
                bench_depth = 0
            elif flex.get(position, 0) > 0:
                bench_depth = 3
            else:
                bench_depth = 1
            caps[position] = max(1, base + bench_depth)
        return caps

    def _need_shift(self, roster_counts: dict[str, int], position: str) -> float | None:
        """Need adjustment for a team, **in picks**.

        Negative pulls a player earlier (the team still has to start the
        position), positive pushes them later.  ``None`` means the team is at its
        cap for the position and will not consider the player at all.
        """
        have = roster_counts.get(position, 0)
        dedicated = self._dedicated.get(position, 0)
        flex = self._flex.get(position, 0)
        cap = self._caps.get(position, dedicated + flex + 1)

        if have >= cap:
            return None
        if self._need_scale <= 0:
            return 0.0
        if have < dedicated:
            return -self._need_scale
        if have < dedicated + flex:
            return -self._need_scale * 0.5
        return self._need_scale * 0.5

    def build_players(
        self,
        candidates: Sequence[tuple[str, str, str, float, float]],
        adp_records: dict[str, ADPRecord],
    ) -> list[SimPlayer]:
        """Build the simulator's view from ``(id, position, name, points, vor)`` rows."""
        known = [record.adp for record in adp_records.values()]
        fallback = (max(known) if known else 100.0) + self.config.undrafted_adp_padding

        players: list[SimPlayer] = []
        for player_id, position, name, points, vor in candidates:
            record = adp_records.get(player_id)
            adp = record.adp if record else fallback
            sigma = (
                float(record.stdev)
                if record and record.stdev and record.stdev > 0
                else min(
                    max(self.config.adp_noise_fraction * adp, self.config.adp_noise_floor),
                    self.config.adp_noise_ceiling,
                )
            )
            players.append(
                SimPlayer(
                    player_id=player_id,
                    position=position,
                    name=name,
                    points=points,
                    vor=vor,
                    adp=adp,
                    sigma=sigma,
                )
            )
        players.sort(key=lambda player: player.adp)
        return players

    # -- core simulation ---------------------------------------------------

    def simulate_to_pick(
        self,
        players: Sequence[SimPlayer],
        *,
        current_pick: int,
        target_pick: int,
        teams: int,
        draft_type: str = "snake",
        reversal_round: int | None = None,
        existing_rosters: dict[int, dict[str, int]] | None = None,
        iterations: int | None = None,
        seed: int | None = None,
        track: Sequence[str] | None = None,
    ) -> SimulationResult:
        """Estimate who survives from ``current_pick`` to ``target_pick``."""
        iterations = iterations or self.config.iterations
        seed = self.config.seed if seed is None else seed
        rng = random.Random(seed)

        picks_to_simulate = max(0, target_pick - current_pick)
        tracked = list(track) if track is not None else [p.player_id for p in players]
        tracked_set = set(tracked)

        survived = dict.fromkeys(tracked, 0)
        taken_pick_total = dict.fromkeys(tracked, 0.0)
        taken_count = dict.fromkeys(tracked, 0)

        best_by_position: dict[str, float] = {}
        best_overall_total = 0.0

        pool_size = self.config.candidate_pool
        by_id = {player.player_id: player for player in players}

        for _ in range(iterations):
            available = list(players)
            roster_counts = {
                index: dict(existing_rosters.get(index, {}) if existing_rosters else {})
                for index in range(teams)
            }

            for offset in range(picks_to_simulate):
                overall = current_pick + offset
                position_info = slot_for_pick(
                    overall, teams, draft_type, reversal_round=reversal_round
                )
                team_index = position_info.team_index
                chosen_index = self._sample_pick(
                    available, roster_counts[team_index], position_info.slot, rng, pool_size
                )
                if chosen_index is None:
                    break
                chosen = available.pop(chosen_index)
                counts = roster_counts[team_index]
                counts[chosen.position] = counts.get(chosen.position, 0) + 1
                if chosen.player_id in tracked_set:
                    taken_pick_total[chosen.player_id] += overall
                    taken_count[chosen.player_id] += 1

            remaining_ids = {player.player_id for player in available}
            for player_id in tracked:
                if player_id in remaining_ids:
                    survived[player_id] += 1

            iteration_best = 0.0
            seen_positions: set[str] = set()
            for player in available:
                if player.vor > iteration_best:
                    iteration_best = player.vor
                if player.position not in seen_positions:
                    seen_positions.add(player.position)
                    best_by_position[player.position] = (
                        best_by_position.get(player.position, 0.0) + player.vor
                    )
            best_overall_total += iteration_best

        availability = {
            player_id: AvailabilityResult(
                player_id=player_id,
                name=by_id[player_id].name if player_id in by_id else player_id,
                position=by_id[player_id].position if player_id in by_id else "",
                probability=survived[player_id] / iterations,
                mean_taken_pick=(
                    taken_pick_total[player_id] / taken_count[player_id]
                    if taken_count[player_id]
                    else None
                ),
                times_available=survived[player_id],
                iterations=iterations,
            )
            for player_id in tracked
        }

        return SimulationResult(
            target_pick=target_pick,
            current_pick=current_pick,
            iterations=iterations,
            seed=seed,
            availability=availability,
            expected_best_by_position={
                position: total / iterations for position, total in best_by_position.items()
            },
            expected_best_overall=best_overall_total / iterations,
            simulated_picks=picks_to_simulate,
        )

    def _sample_pick(
        self,
        available: list[SimPlayer],
        roster_counts: dict[str, int],
        slot: int,
        rng: random.Random,
        pool_size: int,
    ) -> int | None:
        """Index into ``available`` of the player this simulated team takes."""
        if not available:
            return None
        pool = min(pool_size, len(available))
        profile = self.profiles.get(slot)

        gauss = rng.gauss
        best_index: int | None = None
        best_priority = float("inf")
        for index in range(pool):
            player = available[index]
            shift = self._need_shift(roster_counts, player.position)
            if shift is None:
                continue
            if profile is not None:
                shift += profile.position_bias.get(player.position, 0.0)
                shift += profile.reach_tendency
            priority = player.adp + shift + gauss(0.0, player.sigma)
            if priority < best_priority:
                best_priority = priority
                best_index = index
        if best_index is None and pool < len(available):
            # Everything in the shallow pool is capped out for this team; widen.
            for index in range(pool, len(available)):
                player = available[index]
                if self._need_shift(roster_counts, player.position) is not None:
                    return index
        return best_index

    # -- strategy comparison ----------------------------------------------

    def compare_strategies(
        self,
        players: Sequence[SimPlayer],
        candidates: Sequence[str],
        *,
        current_pick: int,
        teams: int,
        rounds: int,
        user_slot: int,
        draft_type: str = "snake",
        reversal_round: int | None = None,
        user_roster: Sequence[SimPlayer] = (),
        existing_rosters: dict[int, dict[str, int]] | None = None,
        iterations: int | None = None,
        seed: int | None = None,
    ) -> list[StrategyOutcome]:
        """For each candidate: take them now, simulate the rest, value the roster.

        The user's later picks are made by a simple greedy policy -- best VOR
        that the roster can still start, else best VOR available -- so the
        comparison isolates the effect of *this* pick rather than of a
        sophisticated future policy.
        """
        from ..analytics.common import ScoredPlayer
        from ..analytics.lineup import expand_slots, optimal_lineup

        iterations = iterations or max(50, self.config.iterations // 5)
        base_seed = self.config.seed if seed is None else seed
        by_id = {player.player_id: player for player in players}
        slots = expand_slots(self.league)
        total_picks = teams * rounds

        outcomes: list[StrategyOutcome] = []
        for candidate_index, candidate_id in enumerate(candidates):
            candidate = by_id.get(candidate_id)
            if candidate is None:
                continue
            totals: list[float] = []
            starter_totals: list[float] = []
            future_totals: list[float] = []

            for iteration in range(iterations):
                # Vary the seed per candidate and iteration, but deterministically.
                rng = random.Random((base_seed or 0) + candidate_index * 100_003 + iteration)
                available = [p for p in players if p.player_id != candidate_id]
                roster_counts = {
                    index: dict(existing_rosters.get(index, {}) if existing_rosters else {})
                    for index in range(teams)
                }
                my_roster = [*user_roster, candidate]
                my_counts = roster_counts.setdefault(user_slot - 1, {})
                my_counts[candidate.position] = my_counts.get(candidate.position, 0) + 1
                future_points = 0.0

                for overall in range(current_pick + 1, total_picks + 1):
                    if not available:
                        break
                    position_info = slot_for_pick(
                        overall, teams, draft_type, reversal_round=reversal_round
                    )
                    team_index = position_info.team_index
                    if position_info.slot == user_slot:
                        pick_index = self._greedy_user_pick(available, my_roster, slots)
                    else:
                        pick_index = self._sample_pick(
                            available, roster_counts[team_index], position_info.slot,
                            rng, self.config.candidate_pool,
                        )
                    if pick_index is None:
                        break
                    chosen = available.pop(pick_index)
                    counts = roster_counts[team_index]
                    counts[chosen.position] = counts.get(chosen.position, 0) + 1
                    if position_info.slot == user_slot:
                        my_roster.append(chosen)
                        future_points += chosen.points

                scored = [
                    ScoredPlayer(p.player_id, p.position, p.points, p.name) for p in my_roster
                ]
                lineup = optimal_lineup(scored, self.league, slots=slots)
                starter_totals.append(lineup.points)
                totals.append(sum(p.points for p in my_roster))
                future_totals.append(future_points)

            if not totals:
                continue
            mean_total = sum(totals) / len(totals)
            variance = (
                sum((value - mean_total) ** 2 for value in totals) / (len(totals) - 1)
                if len(totals) > 1
                else 0.0
            )
            outcomes.append(
                StrategyOutcome(
                    player_id=candidate_id,
                    name=candidate.name,
                    position=candidate.position,
                    mean_roster_points=mean_total,
                    stdev_roster_points=variance**0.5,
                    mean_starter_points=sum(starter_totals) / len(starter_totals),
                    mean_future_points=sum(future_totals) / len(future_totals),
                    iterations=iterations,
                )
            )

        outcomes.sort(key=lambda outcome: outcome.mean_starter_points, reverse=True)
        return outcomes

    def _greedy_user_pick(
        self, available: list[SimPlayer], roster: Sequence[SimPlayer], slots: list
    ) -> int | None:
        """The user's autopilot: best VOR, preferring positions that still start.

        One lineup solve per pick, not one per candidate.  Evaluating each
        candidate with its own lineup solve was exact but dominated the whole
        simulation's runtime, and the ranking it produced was the same: the
        open-slot set is what actually decides between candidates.
        """
        from ..analytics.common import ScoredPlayer
        from ..analytics.lineup import optimal_lineup

        if not available:
            return None
        current = [ScoredPlayer(p.player_id, p.position, p.points, p.name) for p in roster]
        lineup = optimal_lineup(current, self.league, slots=slots)
        needed = lineup.positions_needed()
        at_cap = {
            position
            for position in self._caps
            if sum(1 for player in roster if player.position == position)
            >= self._caps[position]
        }

        best_index = None
        best_score = float("-inf")
        for index, player in enumerate(available):
            if player.position in at_cap:
                continue
            # Bench value keeps the policy from ignoring depth once the starting
            # lineup is full; the share matches roster_fit's bench_start_share.
            score = player.vor if player.position in needed else 0.25 * player.vor
            if score > best_score:
                best_score = score
                best_index = index
        return best_index
