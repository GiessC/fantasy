"""``draft`` commands."""

from __future__ import annotations

import typer

from ...errors import DataMissingError, DraftStateError
from ...models import DraftRecord
from ...services import AnalysisService, SleeperDraftImporter
from ..context import CLIContext
from ..render import (
    key_values,
    note,
    print_board,
    print_json,
    success,
    title,
    warn,
)

draft_app = typer.Typer(help="Track a live draft.", no_args_is_help=True)


def _apply_keepers(service: AnalysisService, record: DraftRecord) -> None:
    """Record configured keepers, reporting any name that could not be resolved."""

    def resolve(name: str) -> str | None:
        try:
            return service.resolve_one(name).player_id
        except DataMissingError as exc:
            warn(f"Keeper {name!r}: {exc}")
            return None

    results = service.drafts.apply_keepers(record, resolve)
    if not results:
        return
    for keeper_name, round_number, player_id in results:
        if player_id is None:
            continue
        note(f"Keeper recorded: {keeper_name} (round {round_number})")
    unresolved = [name for name, _, player_id in results if player_id is None]
    if unresolved:
        warn(
            f"{len(unresolved)} keeper(s) could not be identified and were skipped. "
            f"Add them with 'draft pick <name> --at <overall pick>'."
        )
    note(
        "Only your own keepers are known from config. Record other teams' keepers "
        "with 'draft pick <name> --at <overall pick>'."
    )


@draft_app.command("start")
def draft_start(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help="Label for this draft."),
    position: int = typer.Option(None, "--position", "-p", min=1, help="Your draft slot."),
    teams: int = typer.Option(None, "--teams", min=2),
    rounds: int = typer.Option(None, "--rounds", min=1),
    draft_type: str = typer.Option(None, "--type", help="snake | linear | third_round_reversal"),
    replace: bool = typer.Option(False, "--replace", help="Abandon any active draft first."),
    keepers: bool = typer.Option(
        True, "--keepers/--no-keepers",
        help="Record the keepers declared in league.keepers.keepers.",
    ),
) -> None:
    """Begin tracking a draft."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    manager = service.drafts
    record = manager.start(
        name=name,
        user_slot=position,
        teams=teams,
        rounds=rounds,
        draft_type=draft_type,
        replace_active=replace,
    )
    success(f"Started draft {record.draft_id}: {record.name}")

    if keepers and cli.settings.league.keepers.enabled:
        _apply_keepers(service, record)
    key_values(
        [
            ("Teams", record.teams),
            ("Rounds", record.rounds),
            ("Type", record.draft_type),
            ("Your slot", record.user_slot),
            (
                "Your picks",
                ", ".join(str(pick) for pick in manager.user_picks(record)[:8]) + " ...",
            ),
        ]
    )
    note("Record picks with: fantasy-ai draft pick \"<player name>\"")


@draft_app.command("pick")
def draft_pick(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Player taken (yours or another team's)."),
    overall: int = typer.Option(
        None, "--at", min=1, help="Record at a specific overall pick number."
    ),
    keeper: bool = typer.Option(False, "--keeper"),
    price: float = typer.Option(None, "--price", help="Auction price, if applicable."),
) -> None:
    """Record a selection, by any team, at the next open pick."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    manager = service.drafts
    record = manager.active()
    player = service.resolve_one(name)

    pick = manager.record_pick(
        record, player.player_id, overall_pick=overall, keeper=keeper, auction_price=price
    )
    owner = "you" if pick.is_user else f"slot {pick.slot}"
    success(
        f"Pick {pick.overall_pick} (round {pick.round_number}, {owner}): "
        f"{player.full_name} ({player.position or '?'})"
    )

    status = manager.status(record, service.players_by_id())
    for line in status.describe():
        note(line)


@draft_app.command("skip")
def draft_skip(
    ctx: typer.Context,
    count: int = typer.Option(1, "--count", min=1, help="How many picks to mark unknown."),
) -> None:
    """Record picks whose player you do not know, to keep the pick clock aligned."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    manager = service.drafts
    record = manager.active()
    for _ in range(count):
        pick = manager.record_pick(record, None)
        note(f"Pick {pick.overall_pick} recorded as unknown (slot {pick.slot}).")
    success(f"Advanced {count} pick(s).")


@draft_app.command("undo")
def draft_undo(ctx: typer.Context) -> None:
    """Remove the most recent pick."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    manager = service.drafts
    record = manager.active()
    pick = manager.undo(record)
    player = cli.repos.players.get(pick.player_id) if pick.player_id else None
    success(
        f"Undid pick {pick.overall_pick}"
        + (f" ({player.full_name})" if player else " (unknown player)")
    )


@draft_app.command("status")
def draft_status(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Where the draft stands, and what your roster looks like."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    status = service.draft_status()
    if status is None:
        warn("No active draft. Start one with 'fantasy-ai draft start'.")
        raise typer.Exit(code=1)

    players = service.players_by_id()

    if as_json:
        print_json(
            {
                "draft_id": status.draft.draft_id,
                "current_pick": status.current_pick,
                "on_the_clock_slot": status.on_the_clock,
                "user_slot": status.user_slot,
                "user_next_pick": status.user_next_pick,
                "picks_until_user": status.picks_until_user,
                "is_complete": status.is_complete,
                "roster": [
                    {
                        "player_id": player_id,
                        "name": players[player_id].full_name if player_id in players else None,
                        "position": players[player_id].position if player_id in players else None,
                    }
                    for player_id in status.user_player_ids
                ],
                "drafted_count": len(status.drafted_ids),
            }
        )
        return

    title("Draft status")
    for line in status.describe():
        note(line)

    title("Your roster")
    if not status.user_player_ids:
        note("Empty.")
    else:
        rows = []
        for index, player_id in enumerate(status.user_player_ids, start=1):
            player = players.get(player_id)
            rows.append(
                (
                    f"{index}.",
                    f"{player.full_name} ({player.position or '?'} - {player.team or '?'})"
                    if player
                    else player_id,
                )
            )
        key_values(rows)
        counts = status.user_roster.positions
        note("By position: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    recent = status.picks[-10:]
    if recent:
        title("Recent picks")
        for pick in recent:
            player = players.get(pick.player_id) if pick.player_id else None
            marker = "*" if pick.is_user else " "
            note(
                f"{marker} {pick.overall_pick:>3} (R{pick.round_number}.{pick.slot:02d})  "
                + (player.full_name if player else "unknown")
                + (f"  [{player.position}]" if player and player.position else "")
            )


@draft_app.command("board")
def draft_board(
    ctx: typer.Context,
    limit: int = typer.Option(15, "--limit", "-n", min=1, max=200),
    position: str = typer.Option(None, "--position", "-p"),
    iterations: int = typer.Option(None, "--iterations", min=1),
) -> None:
    """The best available players, in the context of the live draft."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    context = service.board(simulate=True, iterations=iterations)

    if context.status is None:
        warn("No active draft; showing the pre-draft board.")
    else:
        for line in context.status.describe():
            note(line)

    print_board(
        context.board.top(limit, position=position.upper() if position else None),
        heading="Best available",
    )
    if context.board.needs is not None:
        note(context.board.needs.describe())
    if context.simulation is not None:
        note(context.simulation.summary())


@draft_app.command("list")
def draft_list(ctx: typer.Context) -> None:
    """List stored drafts."""
    cli: CLIContext = ctx.obj
    records = cli.repos.drafts.list_all()
    if not records:
        note("No drafts recorded.")
        return
    title("Drafts")
    for record in records:
        picks = len(cli.repos.drafts.picks(record.draft_id or 0))
        marker = "*" if record.status == "active" else " "
        note(
            f"{marker} {record.draft_id:>3}  {record.name:<28} {record.season}  "
            f"{record.teams}x{record.rounds} {record.draft_type:<10} "
            f"slot {record.user_slot}  {picks} pick(s)  [{record.status}]"
        )


@draft_app.command("complete")
def draft_complete(ctx: typer.Context) -> None:
    """Mark the active draft finished."""
    cli: CLIContext = ctx.obj
    manager = cli.analysis().drafts
    record = manager.active()
    manager.complete(record.draft_id or 0)
    success(f"Draft {record.draft_id} marked complete.")


@draft_app.command("delete")
def draft_delete(
    ctx: typer.Context,
    draft_id: int = typer.Argument(None, help="Draft to delete. Defaults to the active one."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Delete a draft and all of its picks."""
    cli: CLIContext = ctx.obj
    repos = cli.repos
    record = repos.drafts.get(draft_id) if draft_id else repos.drafts.active()
    if record is None:
        raise DraftStateError("No draft to delete.")
    if not yes:
        typer.confirm(
            f"Delete draft {record.draft_id} ('{record.name}') and its picks?", abort=True
        )
    repos.drafts.delete(record.draft_id or 0)
    success(f"Deleted draft {record.draft_id}.")


@draft_app.command("import")
def draft_import(
    ctx: typer.Context,
    draft_id: str = typer.Option(None, "--draft-id", help="Sleeper draft id."),
    position: int = typer.Option(None, "--position", "-p", help="Your slot, if not resolvable."),
    create: bool = typer.Option(
        True, "--create/--no-create", help="Start a local draft if needed."
    ),
) -> None:
    """Pull picks from a live Sleeper draft into local state (repeatable)."""
    cli: CLIContext = ctx.obj
    importer = SleeperDraftImporter(cli.settings, cli.repos)
    result = importer.import_draft(
        draft_id=draft_id, create_if_missing=create, user_slot=position
    )
    success(result.describe())
    for warning in result.warnings:
        warn(warning)
    if result.unresolved:
        warn(
            "Could not identify: "
            + ", ".join(result.unresolved[:10])
            + ". Those picks were recorded as unknown so the pick clock stays correct; "
            "run 'fantasy-ai sync players' and re-import to resolve them."
        )
