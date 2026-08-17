"""``recommend``, ``ask``, and ``llm`` commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel

from ...llm import LLMClient, Recommender, build_context
from ...llm.prompts import build_recommendation_prompt, build_system_prompt
from ...services import freshness
from ..context import CLIContext
from ..render import console, error, key_values, note, print_board, success, title, warn

llm_app = typer.Typer(help="Local model status and diagnostics.", no_args_is_help=True)


def _freshness_lines(cli: CLIContext) -> list[str]:
    report = freshness(cli.settings, cli.repos)
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


def recommend(
    ctx: typer.Context,
    position: str = typer.Option(None, "--position", "-p", help="Restrict to one position."),
    candidates: int = typer.Option(None, "--candidates", "-c", min=1, max=40),
    live: bool = typer.Option(False, "--live", help="Terse output for use on the clock."),
    iterations: int = typer.Option(None, "--iterations", min=1),
    show_context: bool = typer.Option(
        False, "--show-context", help="Print the JSON sent to the model."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build the prompt and print it without calling the model."
    ),
) -> None:
    """Ask the local model who to draft, using the deterministic analytics."""
    cli: CLIContext = ctx.obj
    settings = cli.settings
    service = cli.analysis()
    context = service.board(simulate=True, iterations=iterations)
    board = context.board

    top = board.top(candidates or settings.app.llm.max_candidates,
                    position=position.upper() if position else None)
    if not live:
        print_board(top, heading="Candidates sent to the model")

    if dry_run or show_context:
        payload = build_context(
            board,
            settings.league,
            status=context.status,
            simulation=context.simulation,
            max_candidates=candidates or settings.app.llm.max_candidates,
            position=position.upper() if position else None,
            freshness_lines=_freshness_lines(cli),
        )
        if show_context:
            title("Context sent to the model")
            console.print_json(json.dumps(payload.to_dict(), default=str))
        if dry_run:
            title("Prompt")
            console.print(build_system_prompt(live=live))
            console.print(build_recommendation_prompt(payload))
            note(f"Approximately {payload.approximate_tokens()} tokens.")
            return

    if not settings.app.llm.enabled:
        warn("The LLM is disabled (llm.enabled: false). Showing deterministic output only.")
        return

    with LLMClient(settings.app.llm) as client:
        recommender = Recommender(client, settings.league, settings.app.llm)
        result = recommender.recommend(
            board,
            status=context.status,
            simulation=context.simulation,
            position=position.upper() if position else None,
            live=live,
            max_candidates=candidates,
            freshness_lines=_freshness_lines(cli),
        )

    deterministic = (
        f"{top[0].name} ({top[0].position}), draft score {top[0].score:+.1f}" if top else None
    )
    body = result.render(deterministic_top=deterministic)

    if result.ok:
        console.print(
            Panel(body, title="Model recommendation", title_align="left", border_style="green")
        )
    else:
        console.print(
            Panel(body, title="Model output unusable", title_align="left", border_style="red")
        )

    if not live:
        note(
            f"model={result.model or settings.app.llm.model} "
            f"mode={result.structured_mode} attempts={result.attempts}"
            + (f" latency={result.latency_seconds:.1f}s" if result.latency_seconds else "")
        )
        if top:
            note(
                f"Deterministic top candidate: {top[0].name} "
                f"(score {top[0].score:+.1f}). The model interprets these numbers; "
                f"it does not compute them."
            )
    if result.truncated:
        warn(
            "The model's reply hit the token limit. Raise llm.max_tokens in "
            "config/sources.yaml if the answer looks cut off."
        )


def ask(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="Your question about the current analysis."),
    candidates: int = typer.Option(None, "--candidates", "-c", min=1, max=40),
    iterations: int = typer.Option(None, "--iterations", min=1),
) -> None:
    """Ask the local model a question about the current board and draft."""
    cli: CLIContext = ctx.obj
    settings = cli.settings
    if not settings.app.llm.enabled:
        error("The LLM is disabled (llm.enabled: false in config/sources.yaml).")
        raise typer.Exit(code=1)

    service = cli.analysis()
    context = service.board(simulate=True, iterations=iterations)

    with LLMClient(settings.app.llm) as client:
        recommender = Recommender(client, settings.league, settings.app.llm)
        result = recommender.ask(
            context.board,
            question,
            status=context.status,
            simulation=context.simulation,
            max_candidates=candidates,
            freshness_lines=_freshness_lines(cli),
        )

    console.print(
        Panel(
            result.render(),
            title=question,
            title_align="left",
            border_style="green" if result.ok else "red",
        )
    )
    note(f"model={result.model or settings.app.llm.model} attempts={result.attempts}")


@llm_app.command("status")
def llm_status(ctx: typer.Context) -> None:
    """Check that the local model server is reachable and the model is loaded."""
    cli: CLIContext = ctx.obj
    config = cli.settings.app.llm
    key_values(
        [
            ("Enabled", config.enabled),
            ("Base URL", config.base_url),
            ("Model", config.model),
            ("Structured output", config.structured_output),
            ("Temperature", config.temperature),
            ("Max tokens", config.max_tokens),
        ],
        heading="LLM configuration",
    )
    with LLMClient(config) as client:
        ok, message = client.health()
    if ok:
        success(message)
    else:
        error(message)
        raise typer.Exit(code=1)


@llm_app.command("models")
def llm_models(ctx: typer.Context) -> None:
    """List models the local server has available."""
    cli: CLIContext = ctx.obj
    with LLMClient(cli.settings.app.llm) as client:
        models = client.list_models()
    if not models:
        warn("The server reported no models. Load one in LM Studio first.")
        return
    title(f"Models at {cli.settings.app.llm.base_url}")
    for name in models:
        marker = "*" if name == cli.settings.app.llm.model else " "
        note(f"{marker} {name}")
    note("Set llm.model in config/sources.yaml to the one you want (marked * is current).")


@llm_app.command("context")
def llm_context(
    ctx: typer.Context,
    candidates: int = typer.Option(None, "--candidates", "-c", min=1, max=40),
    output: Path = typer.Option(None, "--output", "-o", help="Write the JSON to a file."),
) -> None:
    """Print the exact context object that would be sent to the model."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    context = service.board(simulate=True)
    payload = build_context(
        context.board,
        cli.settings.league,
        status=context.status,
        simulation=context.simulation,
        max_candidates=candidates or cli.settings.app.llm.max_candidates,
        freshness_lines=_freshness_lines(cli),
    )
    rendered = json.dumps(payload.to_dict(), indent=2, default=str)
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
        success(f"Wrote {len(rendered)} bytes to {output}")
    else:
        console.print_json(rendered)
    note(f"Approximately {payload.approximate_tokens()} tokens.")
