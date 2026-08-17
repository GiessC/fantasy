"""Building the analytical context sent to the model.

LLM_INTEGRATION.md: "Only send the relevant subset to the model. Do not dump the
entire database into context."  So this assembles a compact, explicitly-labelled
JSON object -- league rules, draft position, roster, a handful of candidates with
their full analytics, positional scarcity, and simulation output -- and nothing
else.

Every number in it was computed deterministically before the model saw it.  The
model's job is to interpret, compare, and explain; the context is framed to make
that division obvious, including an explicit statement that these figures are
authoritative and must not be recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..analytics import BoardAnalysis, PlayerAnalysis
from ..config import LeagueConfig
from ..draft import DraftStatus, SimulationResult
from ..models import utcnow


@dataclass(slots=True)
class DraftContext:
    """The structured payload sent to the model."""

    league: dict[str, Any]
    draft: dict[str, Any]
    roster: dict[str, Any]
    candidates: list[dict[str, Any]]
    positional_scarcity: list[dict[str, Any]]
    replacement_levels: dict[str, float]
    simulation: dict[str, Any] | None = None
    data_freshness: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def candidate_names(self) -> list[str]:
        return [candidate["name"] for candidate in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "league": self.league,
            "draft": self.draft,
            "my_roster": self.roster,
            "candidates": self.candidates,
            "positional_scarcity": self.positional_scarcity,
            "replacement_levels": self.replacement_levels,
        }
        if self.simulation:
            payload["simulation"] = self.simulation
        if self.data_freshness:
            payload["data_freshness"] = self.data_freshness
        if self.notes:
            payload["league_notes"] = self.notes
        return payload

    def approximate_tokens(self) -> int:
        """Rough size estimate, for warning before a huge request."""
        import json

        return len(json.dumps(self.to_dict())) // 4


def _candidate_payload(analysis: PlayerAnalysis) -> dict[str, Any]:
    """One candidate's analytics, trimmed to what a recommendation needs."""
    payload = analysis.to_dict()
    # Drop identifiers and provenance the model has no use for; they only cost
    # tokens and invite the model to quote internal ids back at the user.
    for key in ("player_id", "projection_source", "adp_source", "draft_score_components"):
        payload.pop(key, None)
    if analysis.draft_score:
        payload["draft_score_explained"] = {
            component.name: round(component.contribution, 1)
            for component in analysis.draft_score.components
        }
    if analysis.risk and analysis.risk.components:
        payload["risk_factors"] = [
            f"{component.name}: {component.note or f'{component.raw:.2f}'} "
            f"(-{component.points:.1f} pts)"
            for component in analysis.risk.components
            if component.points
        ]
    if analysis.scarcity:
        payload["position_run_cost"] = round(analysis.scarcity.dropoff_5, 1)
    return {key: value for key, value in payload.items() if value is not None}


def build_context(
    board: BoardAnalysis,
    league: LeagueConfig,
    *,
    status: DraftStatus | None = None,
    simulation: SimulationResult | None = None,
    max_candidates: int = 8,
    position: str | None = None,
    freshness_lines: list[str] | None = None,
) -> DraftContext:
    """Assemble the model's context from an analysed board."""
    candidates = board.top(max_candidates, position=position)

    league_payload = league.summary()
    league_payload["scoring_note"] = (
        "Projections below are already converted to THIS league's scoring. "
        "Do not re-derive them."
    )

    draft_payload: dict[str, Any] = {"season": board.season}
    roster_payload: dict[str, Any] = {}

    if status is not None:
        draft_payload.update(
            {
                "current_pick": status.current_pick,
                "round": status.current_position.round_number if status.current_position else None,
                "my_slot": status.user_slot,
                "on_the_clock_slot": status.on_the_clock,
                "it_is_my_pick": status.is_user_on_the_clock,
                "my_next_pick": status.user_next_pick,
                "picks_until_my_next_turn": status.picks_until_user,
                "picks_i_have_left": status.picks_remaining_for_user,
                "players_drafted_so_far": len(status.drafted_ids),
            }
        )
        roster_payload = {
            "players": [
                {"name": name, "position": position_name}
                for name, position_name in _roster_names(board, status)
            ],
            "counts_by_position": dict(status.user_roster.positions),
        }
    else:
        draft_payload.update(
            {
                "current_pick": board.current_pick,
                "my_slot": league.draft.position,
                "my_next_pick": board.next_pick,
                "note": "No draft has been started; this is pre-draft board analysis.",
            }
        )
        roster_payload = {"players": [], "counts_by_position": {}}

    if board.needs is not None:
        roster_payload["open_starting_slots"] = board.needs.open_slots
        roster_payload["positions_still_needed"] = sorted(board.needs.positions_needed)
        roster_payload["picks_remaining"] = board.needs.picks_remaining
    if board.lineup is not None:
        roster_payload["current_starting_lineup"] = board.lineup.describe()
        roster_payload["current_lineup_projected_points"] = round(board.lineup.points, 1)

    scarcity_payload = [
        {
            "position": entry.position,
            "players_above_replacement": entry.above_replacement_count,
            "unfilled_starting_slots_league_wide": entry.remaining_demand,
            "supply_ratio": round(entry.starter_supply_ratio, 2),
            "points_lost_per_player": round(entry.average_decay, 1),
        }
        for entry in sorted(
            board.scarcity.by_position.values(),
            key=lambda item: item.scarcity_index,
            reverse=True,
        )
    ]

    simulation_payload = None
    if simulation is not None:
        simulation_payload = {
            "iterations": simulation.iterations,
            "seed": simulation.seed,
            "target_pick": simulation.target_pick,
            "picks_simulated": simulation.simulated_picks,
            "expected_best_available_vor_by_position": {
                position_name: round(value, 1)
                for position_name, value in sorted(
                    simulation.expected_best_by_position.items()
                )
            },
            "method": (
                "Monte Carlo over ADP distributions with positional need; these are "
                "estimates of other drafters' behaviour, not predictions."
            ),
        }

    return DraftContext(
        league=league_payload,
        draft=draft_payload,
        roster=roster_payload,
        candidates=[_candidate_payload(analysis) for analysis in candidates],
        positional_scarcity=scarcity_payload,
        replacement_levels={
            position_name: round(level.points, 1)
            for position_name, level in sorted(board.replacement.by_position.items())
        },
        simulation=simulation_payload,
        data_freshness=freshness_lines or [],
        notes=list(league.notes),
    )


def _roster_names(board: BoardAnalysis, status: DraftStatus) -> list[tuple[str, str | None]]:
    """Names for the user's rostered players.

    Drafted players are no longer on the board, so their names come from the
    board's lineup where available and fall back to the player id.
    """
    by_id: dict[str, tuple[str, str | None]] = {}
    if board.lineup is not None:
        for player in board.lineup.assignments.values():
            by_id[player.player_id] = (player.name, player.position)
        for player in board.lineup.bench:
            by_id[player.player_id] = (player.name, player.position)
    return [by_id.get(player_id, (player_id, None)) for player_id in status.user_player_ids]


def context_timestamp() -> str:
    return utcnow().isoformat(timespec="seconds")
