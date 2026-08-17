"""Analysis orchestration.

Ties configuration, stored data, draft state, and the simulator into the single
call the CLI (and the LLM context builder, and eventually a web API) makes:

    board = AnalysisService(settings, repos).board()

Keeping this here rather than in the CLI means the presentation layer holds no
analytical logic, and a future FastAPI layer reuses it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analytics import AnalyticsEngine, BoardAnalysis, Dataset, load_dataset
from ..config import Settings
from ..db import Repositories
from ..draft import DraftSimulator, DraftStateManager, DraftStatus, SimulationResult
from ..errors import DataMissingError
from ..logging_setup import get_logger
from ..models import DraftRecord, Player

log = get_logger(__name__)


@dataclass(slots=True)
class BoardContext:
    """A board plus everything that shaped it."""

    board: BoardAnalysis
    dataset: Dataset
    status: DraftStatus | None = None
    simulation: SimulationResult | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_draft(self) -> bool:
        return self.status is not None

    def availability_method(self) -> str:
        return "monte-carlo" if self.simulation is not None else "analytic"


class AnalysisService:
    """Builds analysed boards from stored data and draft state."""

    def __init__(self, settings: Settings, repos: Repositories) -> None:
        self.settings = settings
        self.repos = repos
        self.engine = AnalyticsEngine(
            settings.league, settings.app.analytics, settings.app.simulation
        )
        self.drafts = DraftStateManager(repos, settings.league)

    # -- data --------------------------------------------------------------

    def dataset(self, *, season: int | None = None) -> Dataset:
        return load_dataset(
            self.repos, self.settings.league, self.settings.app.analytics, season=season
        )

    def players_by_id(self) -> dict[str, Player]:
        return {player.player_id: player for player in self.repos.players.all()}

    # -- draft state -------------------------------------------------------

    def draft_status(self, draft: DraftRecord | None = None) -> DraftStatus | None:
        record = draft or self.repos.drafts.active()
        if record is None:
            return None
        return self.drafts.status(record, self.players_by_id())

    # -- the board ---------------------------------------------------------

    def board(
        self,
        *,
        use_draft: bool = True,
        simulate: bool = True,
        iterations: int | None = None,
        seed: int | None = None,
        next_pick: int | None = None,
        limit: int | None = None,
        season: int | None = None,
    ) -> BoardContext:
        """Analyse the board, optionally against the active draft and simulator.

        The simulator runs *after* a first analytics pass, because it needs each
        player's VOR to report expected fallbacks -- then its availability
        probabilities are fed back into a second pass, which is what makes
        urgency and the draft score reflect the Monte Carlo estimate rather than
        the closed-form approximation.
        """
        dataset = self.dataset(season=season)
        warnings: list[str] = []

        status = self.draft_status() if use_draft else None
        drafted_ids: list[str] = []
        roster_ids: list[str] = []
        current_pick: int | None = None
        target_pick = next_pick
        picks_remaining: int | None = None

        if status is not None:
            drafted_ids = sorted(status.drafted_ids)
            roster_ids = list(status.user_player_ids)
            current_pick = status.current_pick
            picks_remaining = status.picks_remaining_for_user
            if target_pick is None:
                target_pick = self.drafts.target_pick_for_availability(status)
            if status.is_complete:
                warnings.append("This draft is complete; the board shows leftover players.")
        elif target_pick is None and self.settings.league.draft.position is not None:
            # No draft started: assume the user's first pick.
            target_pick = self.settings.league.draft.position
            current_pick = 1

        first_pass = self.engine.analyze(
            dataset,
            drafted_ids=drafted_ids,
            roster_ids=roster_ids,
            next_pick=target_pick,
            current_pick=current_pick,
            picks_remaining=picks_remaining,
            limit=limit,
        )

        simulation: SimulationResult | None = None
        if simulate and target_pick is not None and current_pick is not None:
            if target_pick > current_pick:
                simulation = self._simulate(
                    first_pass, dataset, status, current_pick, target_pick,
                    iterations=iterations, seed=seed,
                )
            else:
                warnings.append(
                    "You are on the clock with no intervening picks; availability is 100%."
                )

        board = first_pass
        if simulation is not None:
            board = self.engine.analyze(
                dataset,
                drafted_ids=drafted_ids,
                roster_ids=roster_ids,
                next_pick=target_pick,
                current_pick=current_pick,
                picks_remaining=picks_remaining,
                availability_override={
                    player_id: result.probability
                    for player_id, result in simulation.availability.items()
                },
                limit=limit,
            )

        if not board.players:
            raise DataMissingError(
                "No players could be analysed. Check that projections are synced for "
                f"season {dataset.season} ('fantasy-ai data status')."
            )

        return BoardContext(
            board=board,
            dataset=dataset,
            status=status,
            simulation=simulation,
            warnings=warnings,
        )

    def _simulate(
        self,
        board: BoardAnalysis,
        dataset: Dataset,
        status: DraftStatus | None,
        current_pick: int,
        target_pick: int,
        *,
        iterations: int | None,
        seed: int | None,
    ) -> SimulationResult:
        simulator = self.simulator()
        rows = [
            (p.player_id, p.position, p.name, p.projected_points, p.vor.vor)
            for p in board.players
        ]
        players = simulator.build_players(rows, dataset.adp_records())
        teams = status.draft.teams if status else self.settings.league.teams
        draft_type = status.draft.draft_type if status else self.settings.league.draft.type
        reversal = (
            status.draft.settings.get("reversal_round")
            if status
            else self.settings.league.draft.reversal_round
        )
        existing = self._existing_rosters(status, dataset)

        return simulator.simulate_to_pick(
            players,
            current_pick=current_pick,
            target_pick=target_pick,
            teams=teams,
            draft_type=draft_type,
            reversal_round=reversal,
            existing_rosters=existing,
            iterations=iterations,
            seed=seed,
        )

    def simulator(self) -> DraftSimulator:
        return DraftSimulator(self.settings.league, self.settings.app.simulation)

    @staticmethod
    def _existing_rosters(
        status: DraftStatus | None, dataset: Dataset
    ) -> dict[int, dict[str, int]] | None:
        """Positional counts per team so far, so the simulator knows real needs."""
        if status is None:
            return None
        rosters: dict[int, dict[str, int]] = {}
        for index, roster in status.rosters.items():
            counts: dict[str, int] = {}
            for player_id in roster.player_ids:
                data = dataset.players.get(player_id)
                position = data.position if data else None
                if position:
                    counts[position] = counts.get(position, 0) + 1
            rosters[index] = counts
        return rosters

    # -- lookup ------------------------------------------------------------

    def find_players(self, query: str, *, limit: int = 10) -> list[Player]:
        """Resolve a user-typed name to candidate players.

        Exact normalized-name matches win outright; otherwise a substring search
        is returned so the CLI can ask which one was meant.
        """
        from ..normalization.identity import normalize_name

        normalized = normalize_name(query)
        exact = self.repos.players.find_by_normalized_name(normalized)
        if exact:
            return exact
        return self.repos.players.search(query, limit=limit)

    def resolve_one(self, query: str) -> Player:
        """Resolve to exactly one player, or raise with the alternatives listed."""
        matches = self.find_players(query)
        if not matches:
            raise DataMissingError(
                f"No player matches {query!r}. Try 'fantasy-ai analyze players' to see "
                f"what is in the database, or sync data first."
            )
        if len(matches) > 1:
            rendered = ", ".join(
                f"{player.full_name} ({player.position or '?'}/{player.team or '?'})"
                for player in matches[:8]
            )
            raise DataMissingError(
                f"{query!r} matches {len(matches)} players: {rendered}. "
                f"Be more specific."
            )
        return matches[0]
