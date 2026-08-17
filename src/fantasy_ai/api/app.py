"""FastAPI application.

The web UI consumes exactly the services the CLI does -- no analytical logic
lives here, the same way none lives in ``cli/``. A route resolves arguments,
calls a service, and serialises the result.

Bind address defaults to ``127.0.0.1``: this holds your league's data and drives
a local model, and there is no authentication because it is not meant to leave
your machine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..errors import DataMissingError, DraftStateError, FantasyAIError, LLMError
from ..llm import LLMClient, Recommender
from ..logging_setup import get_logger
from ..normalization.identity import normalize_name
from ..services import AnalysisService, freshness
from . import serializers as ser
from .schemas import (
    AlternativeOut,
    ApiInfoOut,
    AskIn,
    AskOut,
    BoardOut,
    DataStatusOut,
    DraftStatusOut,
    ErrorOut,
    LLMStatusOut,
    PickIn,
    PickResultOut,
    PlayerDetailOut,
    RecommendIn,
    RecommendOut,
    SearchResultOut,
    StartDraftIn,
)
from .state import AppState

log = get_logger(__name__)

#: Where the built frontend lands. Absent in a source checkout until `npm run build`.
WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


def get_state(request: Request) -> AppState:
    state: AppState | None = getattr(request.app.state, "app_state", None)
    if state is None:  # pragma: no cover - only if wired wrong
        raise HTTPException(status_code=500, detail="Application state is not initialised.")
    return state


def create_app(
    *,
    league_path: Path | None = None,
    sources_path: Path | None = None,
    state: AppState | None = None,
    serve_frontend: bool = True,
) -> FastAPI:
    """Build the application. ``state`` is injectable so tests skip disk config."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app_state = state or AppState(league_path=league_path, sources_path=sources_path)
        app.state.app_state = app_state
        log.info(
            "Serving %s (%s teams, %s) from %s",
            app_state.settings.league.name,
            app_state.settings.league.teams,
            app_state.settings.league.scoring.compile().describe_format(),
            app_state.settings.app.paths.database,
        )
        try:
            yield
        finally:
            if state is None:
                app_state.close()

    app = FastAPI(
        title="Fantasy AI",
        version=__version__,
        description=(
            "Local fantasy football draft analysis. Deterministic analytics plus a "
            "local LLM that interprets them."
        ),
        lifespan=lifespan,
    )

    # -- error handling ----------------------------------------------------

    @app.exception_handler(FantasyAIError)
    async def handle_known_error(request: Request, exc: FantasyAIError) -> JSONResponse:
        """Expected errors become structured JSON, not a 500 and a traceback."""
        status = {
            DataMissingError: 409,
            DraftStateError: 409,
            LLMError: 503,
        }.get(type(exc), 400)
        return JSONResponse(
            status_code=status,
            content=ErrorOut(
                error=type(exc).__name__,
                detail=str(exc),
                hint=_hint_for(exc),
            ).model_dump(),
        )

    # -- meta --------------------------------------------------------------

    @app.get("/api/info", response_model=ApiInfoOut, tags=["meta"])
    def info(state: AppState = Depends(get_state)) -> ApiInfoOut:
        return ApiInfoOut(
            version=__version__,
            league=ser.league_out(state.settings.league),
            has_data=state.has_data(),
            llm_enabled=state.settings.app.llm.enabled,
            config_warnings=state.config_warnings,
            settings_files=state.settings.describe_sources(),
        )

    @app.get("/api/data/status", response_model=DataStatusOut, tags=["meta"])
    def data_status(state: AppState = Depends(get_state)) -> DataStatusOut:
        with state.lock:
            report = freshness(state.settings, state.repos)
        return ser.data_status_out(report, state.settings.league.season)

    # -- board -------------------------------------------------------------

    @app.get("/api/board", response_model=BoardOut, tags=["analysis"])
    def board(
        limit: int = Query(60, ge=1, le=500),
        position: str | None = Query(None),
        simulate: bool = Query(True),
        state: AppState = Depends(get_state),
    ) -> BoardOut:
        entry = state.board(simulate=simulate)
        context = entry.context
        analysis = context.board
        filtered = analysis.top(limit, position=position.upper() if position else None)
        method: Literal["monte-carlo", "analytic", "none"] = (
            "none"
            if analysis.next_pick is None
            else ("monte-carlo" if context.simulation is not None else "analytic")
        )

        with state.lock:
            players_by_id = {
                player.player_id: player for player in state.repos.players.all()
            }

        return BoardOut(
            season=analysis.season,
            generated_at=entry.generated_at.isoformat(timespec="seconds"),
            availability_method=method,
            next_pick=analysis.next_pick,
            current_pick=analysis.current_pick,
            total_available=len(analysis.players),
            players=[ser.player_out(item) for item in filtered],
            replacement=ser.replacement_out(analysis),
            scarcity=ser.scarcity_out(analysis),
            roster=ser.roster_out(context, players_by_id),
            warnings=list(context.warnings),
            simulation=ser.simulation_out(context.simulation),
            state_version=entry.state_version,
        )

    @app.get(
        "/api/players/{player_id}", response_model=PlayerDetailOut, tags=["analysis"]
    )
    def player_detail(
        player_id: str, state: AppState = Depends(get_state)
    ) -> PlayerDetailOut:
        entry = state.board()
        analysis = entry.context.board.by_id(player_id)
        if analysis is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{player_id} is not on the current board "
                    f"(already drafted, inactive, or without a projection)."
                ),
            )
        return ser.player_detail_out(analysis, entry.context.board)

    @app.get("/api/search", response_model=list[SearchResultOut], tags=["analysis"])
    def search(
        q: str = Query(min_length=1, max_length=80),
        limit: int = Query(12, ge=1, le=50),
        state: AppState = Depends(get_state),
    ) -> list[SearchResultOut]:
        """Player lookup for the pick entry box.

        Exact normalized-name matches are returned first so typing a full name
        and pressing enter is unambiguous; otherwise it is a substring search.
        """
        with state.lock:
            exact = state.repos.players.find_by_normalized_name(normalize_name(q))
            fuzzy = state.repos.players.search(q, limit=limit)
            drafted: set[str] = set()
            draft = state.repos.drafts.active()
            if draft is not None:
                drafted = {
                    pick.player_id
                    for pick in state.repos.drafts.picks(draft.draft_id or 0)
                    if pick.player_id
                }

        seen: set[str] = set()
        results: list[SearchResultOut] = []
        for player in [*exact, *fuzzy]:
            if player.player_id in seen:
                continue
            seen.add(player.player_id)
            results.append(
                SearchResultOut(
                    player_id=player.player_id,
                    name=player.full_name,
                    position=player.position,
                    team=player.team,
                    drafted=player.player_id in drafted,
                )
            )
            if len(results) >= limit:
                break
        return results

    # -- draft -------------------------------------------------------------

    def _status_payload(state: AppState) -> DraftStatusOut:
        with state.lock:
            service = state.analysis()
            status = service.draft_status()
            players_by_id = service.players_by_id()
            user_picks = (
                service.drafts.user_picks(status.draft) if status is not None else []
            )
        roster = ser.roster_out(state.board().context, players_by_id)
        return ser.draft_status_payload(
            status, players_by_id, user_picks, roster, state.state_version()
        )

    @app.get("/api/draft/status", response_model=DraftStatusOut, tags=["draft"])
    def draft_status(state: AppState = Depends(get_state)) -> DraftStatusOut:
        return _status_payload(state)

    @app.post("/api/draft/start", response_model=DraftStatusOut, tags=["draft"])
    def draft_start(
        payload: StartDraftIn, state: AppState = Depends(get_state)
    ) -> DraftStatusOut:
        with state.lock:
            service = state.analysis()
            record = service.drafts.start(
                name=payload.name,
                user_slot=payload.position,
                teams=payload.teams,
                rounds=payload.rounds,
                draft_type=payload.draft_type,
                replace_active=payload.replace,
            )
            if payload.apply_keepers and state.settings.league.keepers.enabled:
                service.drafts.apply_keepers(record, _keeper_resolver(service))
        state.invalidate()
        return _status_payload(state)

    @app.post("/api/draft/pick", response_model=PickResultOut, tags=["draft"])
    def draft_pick(
        payload: PickIn, state: AppState = Depends(get_state)
    ) -> PickResultOut:
        with state.lock:
            service = state.analysis()
            record = service.drafts.active()
            player_id = payload.player_id
            if player_id is None and payload.name:
                player_id = service.resolve_one(payload.name).player_id
            if player_id is None:
                raise HTTPException(
                    status_code=422, detail="Provide either player_id or name."
                )
            pick = service.drafts.record_pick(
                record,
                player_id,
                overall_pick=payload.overall_pick,
                keeper=payload.keeper,
            )
            players_by_id = service.players_by_id()
        state.invalidate()
        return PickResultOut(
            pick=ser.pick_out(pick, players_by_id), status=_status_payload(state)
        )

    @app.post("/api/draft/skip", response_model=PickResultOut, tags=["draft"])
    def draft_skip(state: AppState = Depends(get_state)) -> PickResultOut:
        """Record a pick whose player is unknown, keeping the clock aligned."""
        with state.lock:
            service = state.analysis()
            pick = service.drafts.record_pick(service.drafts.active(), None)
            players_by_id = service.players_by_id()
        state.invalidate()
        return PickResultOut(
            pick=ser.pick_out(pick, players_by_id), status=_status_payload(state)
        )

    @app.post("/api/draft/undo", response_model=PickResultOut, tags=["draft"])
    def draft_undo(state: AppState = Depends(get_state)) -> PickResultOut:
        with state.lock:
            service = state.analysis()
            pick = service.drafts.undo(service.drafts.active())
            players_by_id = service.players_by_id()
        state.invalidate()
        return PickResultOut(
            pick=ser.pick_out(pick, players_by_id), status=_status_payload(state)
        )

    @app.post("/api/draft/complete", response_model=DraftStatusOut, tags=["draft"])
    def draft_complete(state: AppState = Depends(get_state)) -> DraftStatusOut:
        with state.lock:
            service = state.analysis()
            record = service.drafts.active()
            service.drafts.complete(record.draft_id or 0)
        state.invalidate()
        return _status_payload(state)

    # -- LLM ---------------------------------------------------------------

    @app.get("/api/llm/status", response_model=LLMStatusOut, tags=["llm"])
    def llm_status(state: AppState = Depends(get_state)) -> LLMStatusOut:
        config = state.settings.app.llm
        if not config.enabled:
            return LLMStatusOut(
                enabled=False,
                reachable=False,
                message="The LLM is disabled (llm.enabled: false).",
                base_url=config.base_url,
                model=config.model,
            )
        with LLMClient(config) as client:
            ok, message = client.health()
            models: list[str] = []
            if ok:
                try:
                    models = client.list_models()
                except LLMError:  # pragma: no cover - health already succeeded
                    models = []
        return LLMStatusOut(
            enabled=True,
            reachable=ok,
            message=message,
            base_url=config.base_url,
            model=config.model,
            available_models=models,
        )

    @app.post("/api/recommend", response_model=RecommendOut, tags=["llm"])
    def recommend(
        payload: RecommendIn, state: AppState = Depends(get_state)
    ) -> RecommendOut:
        entry = state.board()
        context = entry.context
        settings = state.settings
        top = context.board.top(
            payload.candidates or settings.app.llm.max_candidates,
            position=payload.position.upper() if payload.position else None,
        )
        deterministic = (
            f"{top[0].name} ({top[0].position}), draft score {top[0].score:+.1f}"
            if top
            else None
        )
        if not settings.app.llm.enabled:
            raise LLMError(
                "The LLM is disabled (llm.enabled: false in config/sources.yaml)."
            )

        with state.lock:
            lines = _freshness_lines(state)
        with LLMClient(settings.app.llm) as client:
            result = Recommender(client, settings.league, settings.app.llm).recommend(
                context.board,
                status=context.status,
                simulation=context.simulation,
                position=payload.position.upper() if payload.position else None,
                live=payload.live,
                max_candidates=payload.candidates,
                freshness_lines=lines,
            )

        recommendation = result.recommendation
        return RecommendOut(
            ok=result.ok,
            recommendation=recommendation.recommendation if recommendation else None,
            confidence=recommendation.confidence if recommendation else None,
            reasoning=list(recommendation.reasoning) if recommendation else [],
            alternatives=[
                AlternativeOut(player=alt.player, reason=alt.reason)
                for alt in (recommendation.alternatives if recommendation else [])
            ],
            risks=list(recommendation.risks) if recommendation else [],
            deterministic_top=deterministic,
            model=result.model,
            attempts=result.attempts,
            structured_mode=result.structured_mode,
            latency_seconds=(
                round(result.latency_seconds, 2) if result.latency_seconds else None
            ),
            raw_text=None if result.ok else result.raw_text,
            failures=[failure.problem for failure in result.failures],
        )

    @app.post("/api/ask", response_model=AskOut, tags=["llm"])
    def ask(payload: AskIn, state: AppState = Depends(get_state)) -> AskOut:
        settings = state.settings
        if not settings.app.llm.enabled:
            raise LLMError(
                "The LLM is disabled (llm.enabled: false in config/sources.yaml)."
            )
        entry = state.board()
        context = entry.context
        with state.lock:
            lines = _freshness_lines(state)
        with LLMClient(settings.app.llm) as client:
            result = Recommender(client, settings.league, settings.app.llm).ask(
                context.board,
                payload.question,
                status=context.status,
                simulation=context.simulation,
                max_candidates=payload.candidates,
                freshness_lines=lines,
            )
        return AskOut(
            ok=result.ok,
            answer=result.answer.answer if result.answer else None,
            caveats=list(result.answer.caveats) if result.answer else [],
            unknown_players=list(result.unknown_players),
            model=result.model,
            raw_text=None if result.ok else result.raw_text,
        )

    # -- frontend ----------------------------------------------------------

    if serve_frontend and WEB_DIST.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=WEB_DIST / "assets"),
            name="assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> Any:
            """Serve the single-page app, letting the client router own paths."""
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Unknown API route.")
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    elif serve_frontend:

        @app.get("/", include_in_schema=False)
        def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "FrontendNotBuilt",
                    "detail": f"No built frontend at {WEB_DIST}.",
                    "hint": "Run 'npm install && npm run build' in web/, or use "
                            "'fantasy-ai serve --dev' with the Vite dev server.",
                },
            )

    return app


def _freshness_lines(state: AppState) -> list[str]:
    report = freshness(state.settings, state.repos)
    lines = [
        f"{entry.dataset} ({entry.source}): {entry.describe_age(report.now)}"
        for entry in report.entries
        if entry.record_count
    ]
    stale = report.stale()
    if stale:
        lines.append(
            "WARNING: stale data -- "
            + ", ".join(f"{entry.dataset}/{entry.source}" for entry in stale)
        )
    return lines


def _keeper_resolver(service: AnalysisService) -> Callable[[str], str | None]:
    def resolve(name: str) -> str | None:
        try:
            return service.resolve_one(name).player_id
        except DataMissingError:
            log.warning("Keeper %r could not be resolved; skipping.", name)
            return None

    return resolve


def _hint_for(exc: FantasyAIError) -> str | None:
    if isinstance(exc, DataMissingError):
        return "Sync data first: 'fantasy-ai sync all', or 'fantasy-ai sync demo' offline."
    if isinstance(exc, LLMError):
        return "Check 'fantasy-ai llm status'. The board works without the model."
    if isinstance(exc, DraftStateError):
        return "Start a draft, or check the draft panel for its current state."
    return None
