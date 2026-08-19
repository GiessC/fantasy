"""``validate-config``, ``db``, and ``data`` commands."""

from __future__ import annotations

from typing import Any

import typer

from ...config import FantasyProsConfig, load_settings, validate_settings
from ...db import LATEST_VERSION
from ...services import freshness
from ..context import CLIContext
from ..render import key_values, note, print_json, success, title, warn

db_app = typer.Typer(help="Database initialisation and inspection.", no_args_is_help=True)
data_app = typer.Typer(help="Stored-data inspection and freshness.", no_args_is_help=True)


def validate_config(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Load and validate configuration, reporting problems and warnings."""
    cli: CLIContext = ctx.obj
    settings = load_settings(
        league_path=cli.league_path, sources_path=cli.sources_path
    )
    warnings = validate_settings(settings)

    if as_json:
        print_json(
            {
                "valid": True,
                "league": settings.league.summary(),
                "files": settings.describe_sources(),
                "warnings": warnings,
            }
        )
        return

    success(f"Configuration is valid ({settings.league_path.name}, {settings.sources_path.name})")

    summary: dict[str, Any] = settings.league.summary()
    key_values(
        [
            ("League", summary["name"]),
            ("Season", summary["season"]),
            ("Type", summary["type"]),
            ("Teams", summary["teams"]),
            ("Scoring", summary["scoring_format"]),
            ("Starting lineup", ", ".join(
                f"{slot} x{count}" for slot, count in summary["starting_lineup"].items()
            )),
            ("Flex eligibility", ", ".join(
                f"{slot}: {'/'.join(positions)}"
                for slot, positions in summary["flex_eligibility"].items()
            ) or "none"),
            ("Bench", summary["bench"]),
            ("Roster size", summary["roster_size"]),
            ("Draft", f"{summary['draft_type']}, {summary['rounds']} rounds, "
                      f"slot {summary['draft_position'] or 'unset'}"),
            ("Superflex", "yes" if summary["superflex"] else "no"),
        ],
        heading="League",
    )

    compiled = settings.league.scoring.compile()
    key_values(
        [
            ("Active scoring rules", len(compiled.rates)),
            ("Per-position overrides", ", ".join(compiled.position_rates) or "none"),
            ("Bonuses", len(compiled.bonuses)),
            ("DST points-allowed tiers", len(compiled.points_allowed)),
        ],
        heading="Scoring",
    )

    key_values(
        [
            ("Database", settings.app.paths.database),
            ("HTTP cache", settings.app.paths.http_cache),
            ("FantasyPros", _fantasypros_status(settings.app.sources.fantasypros)),
            ("Sleeper", "enabled" if settings.app.sources.sleeper.enabled else "disabled"),
            ("Local model", f"{settings.app.llm.model} at {settings.app.llm.base_url}"),
        ],
        heading="Runtime",
    )

    if warnings:
        title("Warnings")
        for message in warnings:
            warn(message)
    else:
        note("No warnings.")


def _fantasypros_status(config: FantasyProsConfig) -> str:
    """Enabled/disabled plus where the key comes from -- never the key itself."""
    if not config.enabled:
        return "disabled"
    source = config.key_source()
    return f"enabled, key from {source}" if source else "enabled, no key found"


@db_app.command("init")
def db_init(ctx: typer.Context) -> None:
    """Create the database and apply any pending migrations."""
    from ...db import Database

    cli: CLIContext = ctx.obj
    # Open without the context's auto-migration, so this command can report what
    # it actually did rather than finding the work already done for it.
    database = Database(cli.settings.app.paths.database)
    before = database.version
    applied = database.migrate()
    if applied:
        success(
            f"{'Created database and applied' if before == 0 else 'Applied'} "
            f"migration(s): {applied}"
        )
    else:
        success("Database is already up to date.")
    key_values(
        [("Path", database.path), ("Schema version", database.version)],
        heading="Database",
    )
    database.close()


@db_app.command("status")
def db_status(ctx: typer.Context) -> None:
    """Show schema version and row counts."""
    cli: CLIContext = ctx.obj
    database = cli.database
    key_values(
        [
            ("Path", database.path),
            ("Schema version", f"{database.version} (latest {LATEST_VERSION})"),
        ],
        heading="Database",
    )
    counts = database.table_counts()
    key_values(sorted(counts.items()), heading="Row counts")


@db_app.command("vacuum")
def db_vacuum(ctx: typer.Context) -> None:
    """Compact the database file."""
    cli: CLIContext = ctx.obj
    cli.database.vacuum()
    success("Database compacted.")


@db_app.command("prune")
def db_prune(
    ctx: typer.Context,
    keep: int = typer.Option(10, "--keep", min=1, help="Snapshots to keep per series."),
) -> None:
    """Drop old snapshots, keeping the most recent ones per player and source."""
    cli: CLIContext = ctx.obj
    repos = cli.repos
    removed = {
        "projections": repos.projections.prune_history(keep),
        "rankings": repos.rankings.prune_history(keep),
        "adp": repos.adp.prune_history(keep),
        "injuries": repos.injuries.prune_history(keep),
    }
    total = sum(removed.values())
    success(f"Removed {total} historical snapshot(s), keeping {keep} per series.")
    key_values(sorted(removed.items()))


@data_app.command("status")
def data_status(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Report how fresh each dataset is, and warn about stale data."""
    cli: CLIContext = ctx.obj
    report = freshness(cli.settings, cli.repos)

    if as_json:
        print_json(
            {
                "season": cli.settings.league.season,
                "threshold_hours": report.threshold_hours,
                "datasets": [
                    {
                        "dataset": entry.dataset,
                        "source": entry.source,
                        "records": entry.record_count,
                        "retrieved_at": entry.retrieved_at,
                        "age_hours": entry.age_hours(report.now),
                    }
                    for entry in report.entries
                ],
            }
        )
        return

    title(f"Data freshness (season {cli.settings.league.season})")
    for line in report.describe():
        note(line)

    stale = report.stale()
    if stale:
        warn(
            f"{len(stale)} dataset(s) older than {report.threshold_hours:g}h: "
            + ", ".join(f"{entry.dataset}/{entry.source}" for entry in stale)
            + ". Run 'fantasy-ai sync all' before drafting."
        )
    missing = report.missing()
    if missing:
        warn(
            "No data for: "
            + ", ".join(entry.dataset for entry in missing)
            + ". Run 'fantasy-ai sync all', or 'fantasy-ai sync demo' to try the tool offline."
        )
    if not stale and not missing:
        success("All datasets are present and fresh.")


@data_app.command("sync-log")
def sync_log(
    ctx: typer.Context,
    limit: int = typer.Option(15, "--limit", min=1, max=200),
) -> None:
    """Show recent sync runs."""
    cli: CLIContext = ctx.obj
    rows = cli.repos.sync_runs.recent(limit)
    if not rows:
        note("No syncs recorded yet.")
        return
    title("Recent syncs")
    for row in rows:
        status = row["status"]
        marker = "OK " if status == "success" else ("RUN" if status == "running" else "ERR")
        note(
            f"{marker}  {row['started_at']}  {row['dataset']:<18} {row['source']:<12} "
            f"{row['record_count']:>5} fetched, {row['inserted_count']:>5} written"
            + (f"  -- {row['detail']}" if row["detail"] else "")
        )
