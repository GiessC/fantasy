"""Converting analytics objects into wire models.

Kept apart from the route handlers so the routes stay about HTTP and these stay
about shape. Nothing here computes anything.
"""

from __future__ import annotations

from ..analytics import BoardAnalysis, PlayerAnalysis
from ..config import LeagueConfig
from ..draft import DraftStatus, SimulationResult
from ..models import DraftPick, Player
from ..services import BoardContext
from ..services.sync import FreshnessReport
from .schemas import (
    DataStatusOut,
    DraftStatusOut,
    FreshnessOut,
    LeagueOut,
    LineupSlotOut,
    PickOut,
    PlayerDetailOut,
    PlayerOut,
    PlayerRefOut,
    PositionScarcityOut,
    ReplacementLevelOut,
    RosterOut,
    ScoreComponentOut,
    ScoringLineOut,
    SimulationOut,
)


def player_out(analysis: PlayerAnalysis) -> PlayerOut:
    return PlayerOut(
        player_id=analysis.player_id,
        name=analysis.name,
        position=analysis.position,
        team=analysis.team,
        bye_week=analysis.bye_week,
        projected_points=round(analysis.projected_points, 2),
        points_per_game=round(analysis.points_per_game, 2),
        our_rank=analysis.overall_rank,
        position_rank=analysis.position_rank,
        replacement_points=round(analysis.vor.replacement_points, 2),
        vor=round(analysis.vor.vor, 2),
        tier=analysis.tier.tier if analysis.tier else None,
        players_left_in_tier=(
            analysis.tier.players_left_in_tier if analysis.tier else None
        ),
        points_to_next_tier=(
            round(analysis.tier.points_to_next_tier, 2) if analysis.tier else None
        ),
        adp=round(analysis.adp, 1) if analysis.adp is not None else None,
        adp_value_picks=(
            round(analysis.adp_value.rank_difference, 1)
            if analysis.adp_value and analysis.adp_value.rank_difference is not None
            else None
        ),
        consensus_label=analysis.consensus.label() if analysis.consensus else None,
        expert_stdev=(
            round(analysis.consensus.expert_stdev, 2)
            if analysis.consensus and analysis.consensus.expert_stdev is not None
            else None
        ),
        next_player_delta=(
            round(analysis.scarcity.next_player_delta, 2) if analysis.scarcity else None
        ),
        dropoff_5=round(analysis.scarcity.dropoff_5, 2) if analysis.scarcity else None,
        availability_next_pick=(
            round(analysis.availability.probability, 4) if analysis.availability else None
        ),
        availability_method=(
            analysis.availability.method if analysis.availability else None
        ),
        roster_fit=analysis.roster_fit.label if analysis.roster_fit else None,
        roster_fit_points=(
            round(analysis.roster_fit.total, 2) if analysis.roster_fit else None
        ),
        risk=analysis.risk.level if analysis.risk else None,
        risk_points=round(analysis.risk.discount_points, 2) if analysis.risk else None,
        injury_status=analysis.risk.injury_status if analysis.risk else None,
        draft_score=round(analysis.score, 2),
        components=[
            ScoreComponentOut(
                name=component.name,
                raw=round(component.raw, 2),
                weight=component.weight,
                contribution=round(component.contribution, 2),
                description=component.description,
            )
            for component in (analysis.draft_score.components if analysis.draft_score else [])
        ],
        projection_source=analysis.projection_source,
        adp_source=analysis.adp_source,
    )


def player_detail_out(analysis: PlayerAnalysis, board: BoardAnalysis) -> PlayerDetailOut:
    tier_players: list[PlayerRefOut] = []
    if analysis.tier:
        tiers = board.tiers.tiers_by_position.get(analysis.position, [])
        match = next((tier for tier in tiers if tier.number == analysis.tier.tier), None)
        if match is not None:
            tier_players = [
                PlayerRefOut(
                    player_id=player.player_id,
                    name=player.name,
                    position=analysis.position,
                )
                for player in match.players
            ]
    return PlayerDetailOut(
        player=player_out(analysis),
        explanation=analysis.explain(),
        scoring_lines=[
            ScoringLineOut(
                label=line.label,
                units=round(line.units, 2),
                rate=line.rate,
                points=round(line.points, 2),
                note=line.note,
            )
            for line in analysis.scoring.top_contributors(15)
        ],
        tier_players=tier_players,
    )


def replacement_out(board: BoardAnalysis) -> list[ReplacementLevelOut]:
    return [
        ReplacementLevelOut(
            position=level.position,
            points=round(level.points, 2),
            rank=level.rank,
            demand=level.demand,
            method=level.method,
            has_starting_demand=level.has_starting_demand,
            explanation=level.explain(),
        )
        for level in sorted(
            board.replacement.by_position.values(), key=lambda item: -item.points
        )
    ]


def scarcity_out(board: BoardAnalysis) -> list[PositionScarcityOut]:
    return [
        PositionScarcityOut(
            position=entry.position,
            replacement_points=round(entry.replacement_points, 2),
            above_replacement_count=entry.above_replacement_count,
            remaining_demand=entry.remaining_demand,
            starter_supply_ratio=round(min(entry.starter_supply_ratio, 999.0), 2),
            average_decay=round(entry.average_decay, 2),
            scarcity_index=round(entry.scarcity_index, 3),
        )
        for entry in sorted(
            board.scarcity.by_position.values(),
            key=lambda item: item.scarcity_index,
            reverse=True,
        )
    ]


def roster_out(
    context: BoardContext, players_by_id: dict[str, Player]
) -> RosterOut:
    board = context.board
    status = context.status
    lineup_slots: list[LineupSlotOut] = []
    if board.lineup is not None:
        for slot in board.lineup.slots:
            assigned = board.lineup.assignments.get(slot.index)
            lineup_slots.append(
                LineupSlotOut(
                    slot=slot.name,
                    player_id=assigned.player_id if assigned else None,
                    name=assigned.name if assigned else None,
                    position=assigned.position if assigned else None,
                    points=round(assigned.points, 1) if assigned else None,
                )
            )

    refs: list[PlayerRefOut] = []
    if status is not None:
        for player_id in status.user_player_ids:
            player = players_by_id.get(player_id)
            refs.append(
                PlayerRefOut(
                    player_id=player_id,
                    name=player.full_name if player else player_id,
                    position=player.position if player else None,
                    team=player.team if player else None,
                )
            )

    return RosterOut(
        players=refs,
        counts_by_position=dict(status.user_roster.positions) if status else {},
        lineup=lineup_slots,
        lineup_points=round(board.lineup.points, 1) if board.lineup else 0.0,
        open_slots=board.needs.open_slots if board.needs else {},
        positions_needed=sorted(board.needs.positions_needed) if board.needs else [],
        picks_remaining=board.needs.picks_remaining if board.needs else 0,
    )


def simulation_out(simulation: SimulationResult | None) -> SimulationOut | None:
    if simulation is None:
        return None
    return SimulationOut(
        iterations=simulation.iterations,
        seed=simulation.seed,
        target_pick=simulation.target_pick,
        picks_simulated=simulation.simulated_picks,
        expected_best_by_position={
            position: round(value, 1)
            for position, value in sorted(simulation.expected_best_by_position.items())
        },
        expected_best_overall=round(simulation.expected_best_overall, 1),
    )


def pick_out(pick: DraftPick, players_by_id: dict[str, Player]) -> PickOut:
    player = players_by_id.get(pick.player_id) if pick.player_id else None
    return PickOut(
        overall_pick=pick.overall_pick,
        round_number=pick.round_number,
        slot=pick.slot,
        is_user=pick.is_user,
        keeper=pick.keeper,
        player_id=pick.player_id,
        name=player.full_name if player else None,
        position=player.position if player else None,
        team=player.team if player else None,
    )


def league_out(league: LeagueConfig) -> LeagueOut:
    """Built from the config's typed fields rather than from ``summary()``.

    ``summary()`` returns ``dict[str, object]`` for the CLI's display purposes;
    reading it here would mean a cast per field and would lose the guarantee
    that this response matches the configuration.
    """
    return LeagueOut(
        name=league.name,
        season=league.season,
        type=league.type,
        teams=league.teams,
        scoring_format=league.scoring.compile().describe_format(),
        starting_lineup={slot.name: slot.count for slot in league.starting_slots},
        flex_eligibility={
            slot.name: list(slot.eligible_positions) for slot in league.flex_slots()
        },
        bench=league.roster.get("BENCH", 0),
        roster_size=league.roster_size,
        rounds=league.effective_rounds,
        draft_type=league.draft.type,
        draft_position=league.draft.position,
        superflex=league.is_superflex,
        notes=list(league.notes),
    )


def data_status_out(report: FreshnessReport, season: int) -> DataStatusOut:
    stale_keys = {(entry.dataset, entry.source) for entry in report.stale()}
    datasets = [
        FreshnessOut(
            dataset=entry.dataset,
            source=entry.source,
            records=entry.record_count,
            age_hours=(
                round(entry.age_hours(report.now), 2)
                if entry.age_hours(report.now) is not None
                else None
            ),
            described_age=entry.describe_age(report.now),
            stale=(entry.dataset, entry.source) in stale_keys,
        )
        for entry in report.entries
    ]
    return DataStatusOut(
        season=season,
        threshold_hours=report.threshold_hours,
        datasets=datasets,
        has_data=any(entry.records for entry in datasets),
        stale_count=len(stale_keys),
        missing=[entry.dataset for entry in report.missing()],
    )


def draft_status_payload(
    status: DraftStatus | None,
    players_by_id: dict[str, Player],
    user_picks: list[int],
    roster: RosterOut,
    state_version: str,
) -> DraftStatusOut:
    """Build the draft-status wire model (or an inactive one)."""
    if status is None:
        return DraftStatusOut(active=False, state_version=state_version, roster=roster)

    draft = status.draft
    return DraftStatusOut(
        active=True,
        draft_id=draft.draft_id,
        name=draft.name,
        season=draft.season,
        teams=draft.teams,
        rounds=draft.rounds,
        draft_type=draft.draft_type,
        user_slot=status.user_slot,
        current_pick=status.current_pick,
        current_round=(
            status.current_position.round_number if status.current_position else None
        ),
        on_the_clock_slot=status.on_the_clock,
        is_user_on_the_clock=status.is_user_on_the_clock,
        user_next_pick=status.user_next_pick,
        picks_until_user=status.picks_until_user,
        picks_remaining_for_user=status.picks_remaining_for_user,
        total_picks=status.total_picks,
        drafted_count=len(status.drafted_ids),
        is_complete=status.is_complete,
        user_picks=user_picks,
        recent_picks=[pick_out(pick, players_by_id) for pick in status.picks[-15:]][::-1],
        roster=roster,
        state_version=state_version,
    )
