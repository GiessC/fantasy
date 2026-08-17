"""``fantasy-ai`` command-line entry point.

The CLI is presentation only: it parses arguments, calls a service, and renders
the result.  No analytical logic lives here, which is what lets a future web API
expose the same behaviour by calling the same services.

Expected errors (:class:`~fantasy_ai.errors.FantasyAIError`) are caught centrally
and printed as a single clean message with a meaningful exit code, so a missing
config file or an unreachable model never produces a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import __version__
from ..errors import FantasyAIError
from ..logging_setup import configure_logging
from .commands import (
    analyze_app,
    ask,
    data_app,
    db_app,
    draft_app,
    llm_app,
    recommend,
    simulate_app,
    sync_app,
    validate_config,
)
from .context import CLIContext
from .render import error, note

app = typer.Typer(
    name="fantasy-ai",
    help=(
        "Local fantasy football draft analysis.\n\n"
        "Deterministic analytics from your own league's YAML, plus a local LLM "
        "(via LM Studio) that interprets them. The model never computes a number.\n\n"
        "First run:\n"
        "  fantasy-ai validate-config\n"
        "  fantasy-ai db init\n"
        "  fantasy-ai sync demo        # or: sync all, with an API key configured\n"
        "  fantasy-ai analyze board"
    ),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
)

app.add_typer(db_app, name="db")
app.add_typer(data_app, name="data")
app.add_typer(sync_app, name="sync")
app.add_typer(analyze_app, name="analyze")
app.add_typer(draft_app, name="draft")
app.add_typer(simulate_app, name="simulate")
app.add_typer(llm_app, name="llm")

app.command("validate-config")(validate_config)
app.command("recommend")(recommend)
app.command("ask")(ask)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fantasy-ai {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    league: Path = typer.Option(
        None, "--league", envvar="FANTASY_AI_LEAGUE_CONFIG",
        help="League YAML (default: config/league.yaml).",
    ),
    sources: Path = typer.Option(
        None, "--sources", envvar="FANTASY_AI_SOURCES_CONFIG",
        help="Sources YAML (default: config/sources.yaml).",
    ),
    log_level: str = typer.Option(
        None, "--log-level", help="critical | error | warning | info | debug"
    ),
    iterations: int = typer.Option(
        None, "--sim-iterations", help="Override simulation.iterations for this run.",
    ),
    seed: int = typer.Option(
        None, "--sim-seed", help="Override simulation.seed for this run.",
    ),
    version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Global options, applied before any command runs."""
    overrides: dict = {}
    if iterations is not None:
        overrides.setdefault("simulation", {})["iterations"] = iterations
    if seed is not None:
        overrides.setdefault("simulation", {})["seed"] = seed

    ctx.obj = CLIContext(
        league_path=league,
        sources_path=sources,
        log_level=log_level,
        overrides=overrides,
    )
    ctx.call_on_close(ctx.obj.close)


def _click_exception_type() -> type[BaseException]:
    """Click's ``ClickException``, wherever it lives.

    Typer 0.27 vendors Click as ``typer._click`` and no longer installs the
    ``click`` package, while earlier versions depend on the real one.  Walking
    the MRO of a public Typer export finds the right class either way, without
    importing a private module or adding a dependency we do not otherwise need.
    """
    for base in typer.BadParameter.__mro__:
        if base.__name__ == "ClickException":
            return base
    return Exception


_CLICK_EXCEPTION = _click_exception_type()


def run() -> int:
    """Entry point that turns expected errors into clean messages."""
    try:
        # With standalone_mode=False, Click *returns* an Exit's code rather than
        # raising it, so a command's `raise typer.Exit(code=N)` arrives here as a
        # return value. Ignoring it would silently report success on failure.
        result = app(standalone_mode=False)
        return result if isinstance(result, int) else 0
    except FantasyAIError as exc:
        error(str(exc))
        return exc.exit_code
    except typer.Exit as exc:
        return int(getattr(exc, "exit_code", 0) or 0)
    except typer.Abort:
        note("Aborted.")
        return 130
    except _CLICK_EXCEPTION as exc:  # type: ignore[misc]
        message = getattr(exc, "format_message", None)
        error(message() if callable(message) else str(exc))
        return int(getattr(exc, "exit_code", 1) or 1)
    except KeyboardInterrupt:
        note("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    configure_logging()
    sys.exit(run())
