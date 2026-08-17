"""``analyze`` commands."""

from __future__ import annotations

import typer

from ...analytics import compare_scores
from ..context import CLIContext
from ..render import (
    note,
    print_board,
    print_json,
    print_player_detail,
    print_replacement_levels,
    print_scarcity,
    print_scoring_breakdown,
    print_tiers,
    title,
    warn,
)

analyze_app = typer.Typer(help="Deterministic board and player analysis.", no_args_is_help=True)


@analyze_app.command("board")
def analyze_board(
    ctx: typer.Context,
    limit: int = typer.Option(25, "--limit", "-n", min=1, max=500),
    position: str = typer.Option(None, "--position", "-p", help="Filter to one position."),
    simulate: bool = typer.Option(
        True, "--simulate/--no-simulate",
        help="Use Monte Carlo availability instead of the closed-form estimate.",
    ),
    iterations: int = typer.Option(None, "--iterations", min=1),
    detail: bool = typer.Option(False, "--detail", help="Also show scarcity and replacement."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """The full draft board, ranked by composite draft score."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    context = service.board(simulate=simulate, iterations=iterations)
    board = context.board

    players = board.top(limit, position=position.upper() if position else None)

    if as_json:
        print_json(
            {
                "season": board.season,
                "availability_method": context.availability_method(),
                "next_pick": board.next_pick,
                "current_pick": board.current_pick,
                "players": [player.to_dict() for player in players],
            }
        )
        return

    heading = f"Draft board -- {cli.settings.league.name} {board.season}"
    if position:
        heading += f" ({position.upper()})"
    print_board(players, heading=heading)

    if context.status is not None:
        for line in context.status.describe():
            note(line)
    if board.next_pick is not None:
        note(
            f"Availability measured at pick {board.next_pick} "
            f"({context.availability_method()})."
        )
    if context.simulation is not None:
        note(context.simulation.summary())
    for warning in context.warnings:
        warn(warning)
    if board.excluded:
        note(f"{len(board.excluded)} player(s) excluded (no projection, inactive, or zero points).")

    if detail:
        title("Replacement levels")
        print_replacement_levels(board)
        title("Positional scarcity")
        print_scarcity(board)


@analyze_app.command("players")
def analyze_players(
    ctx: typer.Context,
    position: str = typer.Option(None, "--position", "-p"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1000),
    sort: str = typer.Option(
        "score", "--sort", help="score | vor | points | adp | value"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List analysed players, sorted however you like."""
    cli: CLIContext = ctx.obj
    context = cli.analysis().board(simulate=False)
    players = list(context.board.players)
    if position:
        players = [p for p in players if p.position == position.upper()]

    keys = {
        "score": lambda p: p.score,
        "vor": lambda p: p.vor.vor,
        "points": lambda p: p.projected_points,
        "adp": lambda p: -(p.adp if p.adp is not None else 1e9),
        "value": lambda p: (
            p.adp_value.rank_difference
            if p.adp_value and p.adp_value.rank_difference is not None
            else -1e9
        ),
    }
    if sort not in keys:
        raise typer.BadParameter(f"Unknown sort {sort!r}. Choose from: {', '.join(keys)}.")
    players.sort(key=keys[sort], reverse=True)
    players = players[:limit]

    if as_json:
        print_json([player.to_dict() for player in players])
        return
    print_board(players, heading=f"Players by {sort}")


@analyze_app.command("player")
def analyze_player(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Player name (partial names are matched)."),
    breakdown: bool = typer.Option(
        False, "--breakdown", help="Show how the projection converts to points."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Everything the system knows about one player, fully decomposed."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    player = service.resolve_one(name)
    context = service.board(simulate=True)
    analysis = context.board.by_id(player.player_id)

    if analysis is None:
        warn(
            f"{player.full_name} is not on the current board "
            f"(already drafted, inactive, or without a projection)."
        )
        raise typer.Exit(code=1)

    if as_json:
        print_json(analysis.to_dict())
        return

    print_player_detail(analysis)
    if breakdown:
        print_scoring_breakdown(analysis)


@analyze_app.command("compare")
def analyze_compare(
    ctx: typer.Context,
    first: str = typer.Argument(...),
    second: str = typer.Argument(...),
) -> None:
    """Explain, component by component, why one player outranks another."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    left_player = service.resolve_one(first)
    right_player = service.resolve_one(second)
    context = service.board(simulate=True)

    left = context.board.by_id(left_player.player_id)
    right = context.board.by_id(right_player.player_id)
    if left is None or right is None:
        missing = left_player.full_name if left is None else right_player.full_name
        warn(f"{missing} is not on the current board.")
        raise typer.Exit(code=1)

    if left.score < right.score:
        left, right = right, left

    title(f"{left.name} vs {right.name}")
    if left.draft_score and right.draft_score:
        for line in compare_scores(left.draft_score, right.draft_score):
            note(line)
    note("")
    print_board([left, right], heading="Side by side")


@analyze_app.command("tiers")
def analyze_tiers(
    ctx: typer.Context,
    position: str = typer.Argument(..., help="Position to show tiers for."),
    limit: int = typer.Option(6, "--limit", min=1, max=30),
) -> None:
    """Show value tiers at a position, with the gap that opened each one."""
    cli: CLIContext = ctx.obj
    context = cli.analysis().board(simulate=False)
    title(f"{position.upper()} tiers")
    print_tiers(context.board, position.upper(), limit=limit)


@analyze_app.command("replacement")
def analyze_replacement(ctx: typer.Context) -> None:
    """Show how replacement level was derived for each position."""
    cli: CLIContext = ctx.obj
    context = cli.analysis().board(simulate=False)
    print_replacement_levels(context.board)
    title("Derivation")
    for line in context.board.replacement.explain():
        note(line)


@analyze_app.command("scarcity")
def analyze_scarcity(ctx: typer.Context) -> None:
    """Show how quickly value decays at each position."""
    cli: CLIContext = ctx.obj
    context = cli.analysis().board(simulate=False)
    print_scarcity(context.board)
