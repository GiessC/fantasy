"""``simulate`` commands."""

from __future__ import annotations

import typer
from rich.table import Table

from ..context import CLIContext
from ..render import console, note, position_text, print_json, title, warn

simulate_app = typer.Typer(
    help="Monte Carlo draft simulation.", no_args_is_help=True
)


@simulate_app.command("availability")
def simulate_availability(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
    iterations: int = typer.Option(None, "--iterations", min=1),
    seed: int = typer.Option(None, "--seed"),
    at_pick: int = typer.Option(None, "--at-pick", help="Target pick to measure against."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Probability each top candidate survives to your next pick."""
    cli: CLIContext = ctx.obj
    context = cli.analysis().board(
        simulate=True, iterations=iterations, seed=seed, next_pick=at_pick
    )
    if context.simulation is None:
        if at_pick is not None:
            warn(
                f"No simulation ran: pick {at_pick} leaves nothing to simulate. "
                f"Pass a later --at-pick, or check 'fantasy-ai draft status' if a "
                f"draft is under way."
            )
        else:
            warn(
                "No simulation ran -- there are no intervening picks, and no draft "
                "position is configured. Set league.draft.position, or pass --at-pick."
            )
        raise typer.Exit(code=1)

    players = context.board.top(limit)
    simulation = context.simulation

    if as_json:
        print_json(
            {
                "target_pick": simulation.target_pick,
                "iterations": simulation.iterations,
                "seed": simulation.seed,
                "players": [
                    {
                        "name": player.name,
                        "position": player.position,
                        "adp": player.adp,
                        "monte_carlo": simulation.probability(player.player_id),
                        "analytic": (
                            player.availability.probability if player.availability else None
                        ),
                    }
                    for player in players
                ],
            }
        )
        return

    table = Table(title=f"Availability at pick {simulation.target_pick}")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("ADP", justify="right")
    table.add_column("Monte Carlo", justify="right")
    table.add_column("Mean pick taken", justify="right")
    table.add_column("Score", justify="right")

    for player in players:
        entry = simulation.availability.get(player.player_id)
        probability = entry.probability if entry else None
        table.add_row(
            player.name,
            position_text(player.position),
            f"{player.adp:.1f}" if player.adp is not None else "-",
            f"{probability:.0%}" if probability is not None else "-",
            (
                f"{entry.mean_taken_pick:.1f}"
                if entry and entry.mean_taken_pick is not None
                else "survives"
            ),
            f"{player.score:+.1f}",
        )
    console.print(table)
    note(simulation.summary())
    note(
        "Expected best VOR still available by position: "
        + ", ".join(
            f"{position} {value:.0f}"
            for position, value in sorted(simulation.expected_best_by_position.items())
        )
    )


@simulate_app.command("draft")
def simulate_draft(
    ctx: typer.Context,
    candidates: int = typer.Option(5, "--candidates", "-c", min=2, max=15),
    iterations: int = typer.Option(
        200, "--iterations", "-i", min=10, max=20_000,
        help="Full-draft simulations per candidate (slower than availability).",
    ),
    seed: int = typer.Option(None, "--seed"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare strategies: take each candidate now, simulate the rest, value the roster."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    context = service.board(simulate=True)
    board = context.board

    if board.next_pick is None or board.current_pick is None:
        warn(
            "Strategy simulation needs a draft position. Run 'fantasy-ai draft start' "
            "or set league.draft.position."
        )
        raise typer.Exit(code=1)

    simulator = service.simulator()
    rows = [
        (p.player_id, p.position, p.name, p.projected_points, p.vor.vor) for p in board.players
    ]
    sim_players = simulator.build_players(rows, context.dataset.adp_records())
    top = board.top(candidates)

    status = context.status
    teams = status.draft.teams if status else cli.settings.league.teams
    rounds = status.draft.rounds if status else cli.settings.league.effective_rounds
    user_slot = status.user_slot if status else (cli.settings.league.draft.position or 1)
    draft_type = status.draft.draft_type if status else cli.settings.league.draft.type

    roster_ids = set(status.user_player_ids) if status else set()
    user_roster = [player for player in sim_players if player.player_id in roster_ids]

    title(f"Simulating {candidates} candidates x {iterations} full drafts")
    note("This models the rest of the draft, so it takes longer than availability.")

    outcomes = simulator.compare_strategies(
        sim_players,
        [player.player_id for player in top],
        current_pick=board.current_pick,
        teams=teams,
        rounds=rounds,
        user_slot=user_slot,
        draft_type=draft_type,
        user_roster=user_roster,
        iterations=iterations,
        seed=seed,
    )

    if as_json:
        print_json(
            [
                {
                    "player": outcome.name,
                    "position": outcome.position,
                    "mean_starting_lineup_points": outcome.mean_starter_points,
                    "stdev": outcome.stdev_roster_points,
                    "iterations": outcome.iterations,
                }
                for outcome in outcomes
            ]
        )
        return

    if not outcomes:
        warn("No candidates could be simulated.")
        raise typer.Exit(code=1)

    best = outcomes[0].mean_starter_points
    table = Table(title="Expected starting lineup if you take this player now")
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("Mean lineup pts", justify="right")
    table.add_column("vs best", justify="right")
    table.add_column("Std dev", justify="right")

    for outcome in outcomes:
        table.add_row(
            outcome.name,
            position_text(outcome.position),
            f"{outcome.mean_starter_points:.1f}",
            f"{outcome.mean_starter_points - best:+.1f}",
            f"{outcome.stdev_roster_points:.1f}",
        )
    console.print(table)
    note(
        "Later picks are made by a simple best-available-that-starts policy, so this "
        "isolates the effect of THIS pick. Differences smaller than the standard "
        "deviation are noise."
    )


@simulate_app.command("player")
def simulate_player(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Player to evaluate."),
    iterations: int = typer.Option(None, "--iterations", min=1),
    seed: int = typer.Option(None, "--seed"),
) -> None:
    """Availability and fallback analysis for a single player."""
    cli: CLIContext = ctx.obj
    service = cli.analysis()
    player = service.resolve_one(name)
    context = service.board(simulate=True, iterations=iterations, seed=seed)
    analysis = context.board.by_id(player.player_id)

    if analysis is None:
        warn(f"{player.full_name} is not on the current board.")
        raise typer.Exit(code=1)

    title(f"{analysis.name} ({analysis.position})")
    for line in analysis.explain():
        note(line)

    if context.simulation is None:
        note("No simulation ran; availability is the closed-form estimate.")
        return

    entry = context.simulation.availability.get(player.player_id)
    if entry is not None:
        note("")
        note(entry.explain(context.simulation.target_pick))
    expected = context.simulation.expected_best_by_position.get(analysis.position)
    if expected is not None:
        note(
            f"If he is gone, the best {analysis.position} expected to survive to pick "
            f"{context.simulation.target_pick} is worth about {expected:.0f} VOR "
            f"(vs his {analysis.vor.vor:+.0f})."
        )
