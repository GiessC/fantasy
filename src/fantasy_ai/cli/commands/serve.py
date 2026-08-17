"""``serve`` command: the local web UI."""

from __future__ import annotations

import typer

from ...api import WEB_DIST
from ...errors import FantasyAIError
from ..context import CLIContext
from ..render import key_values, note, success, warn


def serve(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Defaults to localhost -- there is no authentication.",
    ),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the UI in your default browser."
    ),
) -> None:
    """Serve the web UI and its API.

    The UI is a live-draft board: best available with the full analytics, your
    roster and open slots, availability at your next pick, and the local model's
    recommendation. It consumes the same services the CLI does.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise FantasyAIError(
            "The web UI needs FastAPI and uvicorn. Install them with:\n"
            "  pip install 'fantasy-ai[web]'"
        ) from exc

    cli: CLIContext = ctx.obj
    settings = cli.settings

    if not WEB_DIST.is_dir():
        warn(
            f"No built frontend at {WEB_DIST}. The API will serve, but the UI will not.\n"
            f"    Build it with:  cd web && npm install && npm run build"
        )

    key_values(
        [
            ("URL", f"http://{host}:{port}"),
            ("League", f"{settings.league.name} ({settings.league.season})"),
            ("Database", settings.app.paths.database),
            ("Local model", f"{settings.app.llm.model} at {settings.app.llm.base_url}"),
        ],
        heading="Serving",
    )
    if host not in {"127.0.0.1", "localhost", "::1"}:
        warn(
            f"Binding to {host} exposes your draft data on the network. "
            f"There is no authentication -- use 127.0.0.1 unless you mean it."
        )
    note("Press Ctrl+C to stop.")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    # Passing the factory by import string is what lets --reload work; the
    # app then loads configuration from the same defaults the CLI resolved.
    uvicorn.run(
        "fantasy_ai.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=settings.app.log_level.lower(),
    )
    success("Server stopped.")
