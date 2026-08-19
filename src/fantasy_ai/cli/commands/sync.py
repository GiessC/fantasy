"""``sync`` commands."""

from __future__ import annotations

from pathlib import Path

import typer

from ...sources.http import SyncReport
from ..context import CLIContext
from ..render import note, success, title, warn

sync_app = typer.Typer(help="Fetch and store data from configured sources.", no_args_is_help=True)


def _report(reports: list[SyncReport]) -> None:
    for report in reports:
        if any(warning.startswith("FAILED") for warning in report.warnings):
            warn(report.describe())
        else:
            success(report.describe())
        for warning in report.warnings:
            note(f"    {warning}")
        if report.unmapped_fields:
            top = ", ".join(
                f"{name} (x{count})"
                for name, count in sorted(
                    report.unmapped_fields.items(), key=lambda item: -item[1]
                )[:8]
            )
            note(f"    unmapped source fields: {top}")


@sync_app.command("players")
def sync_players(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Ignore the HTTP cache."),
) -> None:
    """Canonical player metadata and identity, from Sleeper."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_players(force_refresh=force)])
    finally:
        service.close()


@sync_app.command("rankings")
def sync_rankings(
    ctx: typer.Context,
    ranking_type: str = typer.Option(
        "DRAFT", "--type", help="FantasyPros ranking type (DRAFT, ROS, WEEKLY, ...)."
    ),
    force: bool = typer.Option(False, "--force"),
    verbose: bool = typer.Option(False, "--verbose", help="Show field-mapping details."),
) -> None:
    """Expert consensus rankings from FantasyPros."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_rankings(
            ranking_type=ranking_type, force_refresh=force, verbose=verbose
        )])
    finally:
        service.close()


@sync_app.command("adp")
def sync_adp(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Average draft position from FantasyPros."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_adp(force_refresh=force, verbose=verbose)])
    finally:
        service.close()


@sync_app.command("projections")
def sync_projections(
    ctx: typer.Context,
    position: list[str] = typer.Option(
        None, "--position", "-p", help="Limit to these positions (repeatable)."
    ),
    from_csv: Path = typer.Option(
        None, "--from-csv", help="Import a FantasyPros CSV export instead of calling the API."
    ),
    force: bool = typer.Option(False, "--force"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Statistical projections from FantasyPros (or a CSV export)."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        if from_csv is not None:
            _report([service.sync_from_csv(
                from_csv,
                dataset="projections",
                position_hint=position[0] if position else None,
                verbose=verbose,
            )])
        else:
            _report([service.sync_projections(
                positions=position or None, force_refresh=force, verbose=verbose
            )])
    finally:
        service.close()


@sync_app.command("injuries")
def sync_injuries(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Injury designations, from Sleeper's player payload."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_injuries(force_refresh=force)])
    finally:
        service.close()


@sync_app.command("csv")
def sync_csv(
    ctx: typer.Context,
    path: Path = typer.Argument(
        None, help="A CSV file. Omit to import every CSV in the configured directory."
    ),
    dataset: str = typer.Option(
        "auto", "--dataset",
        help="auto | projections | rankings | adp -- what to take from the file.",
    ),
    position: str = typer.Option(
        None, "--position", help="Position hint for files without a POS column."
    ),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Import FantasyPros CSV exports (no API key needed)."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        if path is not None:
            _report([service.sync_from_csv(
                path, dataset=dataset, position_hint=position, verbose=verbose
            )])
        else:
            reports = service.sync_csv_directory(verbose=verbose)
            if not reports:
                warn(
                    f"No CSV files in {cli.settings.app.sources.csv_import.directory}. "
                    f"Download exports from FantasyPros and drop them there."
                )
            _report(reports)
    finally:
        service.close()


@sync_app.command("demo")
def sync_demo(
    ctx: typer.Context,
    seed: int = typer.Option(None, "--seed", help="Seed for reproducible synthetic data."),
) -> None:
    """Generate a synthetic season so the tool runs with no network or API key."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_demo(seed=seed)])
        note("Try: fantasy-ai analyze board")
    finally:
        service.close()


@sync_app.command("history")
def sync_history(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="A FantasyPros cheat-sheet CSV export."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show what was parsed."),
    no_adp: bool = typer.Option(
        False, "--no-adp", help="Store only the past seasons, not the sheet's current ADP."
    ),
) -> None:
    """Import completed seasons (games played, points, ADP) from a cheat sheet.

    History feeds durability and trajectory. It is never used as a projection:
    what a player scored last year is not a forecast of this year.
    """
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        _report([service.sync_history(path, verbose=verbose, import_adp=not no_adp)])
        note("Try: fantasy-ai analyze player \"<name>\"  to see the durability profile")
    finally:
        service.close()


@sync_app.command("all")
def sync_all(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Ignore the HTTP cache."),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Sync every enabled source, in dependency order."""
    cli: CLIContext = ctx.obj
    service = cli.sync()
    try:
        title("Syncing all sources")
        reports = service.sync_all(force_refresh=force, verbose=verbose)
        _report(reports)
        if not reports:
            warn("No sources are enabled. Check config/sources.yaml.")
    finally:
        service.close()
