"""Rendering helpers.

All Rich usage lives here, so command modules stay about *what* to show and this
module owns *how*.  Every table is also available as plain rows, which is what
``--json`` output and future non-terminal consumers use.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analytics import BoardAnalysis, PlayerAnalysis

console = Console()
error_console = Console(stderr=True)

#: Consistent colour per position, so the board is scannable at a glance.
POSITION_STYLES: dict[str, str] = {
    "QB": "bright_magenta",
    "RB": "green",
    "WR": "cyan",
    "TE": "yellow",
    "K": "bright_black",
    "DST": "blue",
}


def position_text(position: str) -> Text:
    return Text(position, style=POSITION_STYLES.get(position, "white"))


def availability_text(probability: float | None) -> Text:
    if probability is None:
        return Text("-", style="bright_black")
    rendered = f"{probability:.0%}"
    if probability < 0.25:
        return Text(rendered, style="bold red")
    if probability < 0.6:
        return Text(rendered, style="yellow")
    return Text(rendered, style="green")


def risk_text(level: str | None) -> Text:
    styles = {"minimal": "green", "low": "green", "medium": "yellow", "high": "red"}
    return Text(level or "-", style=styles.get(level or "", "white"))


def title(text: str) -> None:
    console.print()
    console.rule(f"[bold]{text}[/bold]", style="bright_black")


def note(text: str) -> None:
    console.print(f"[bright_black]{text}[/bright_black]")


def warn(text: str) -> None:
    console.print(f"[yellow]![/yellow] {text}")


def error(text: str) -> None:
    error_console.print(f"[bold red]Error:[/bold red] {text}")


def success(text: str) -> None:
    console.print(f"[green]OK[/green] {text}")


def key_values(rows: Iterable[tuple[str, Any]], *, heading: str | None = None) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bright_black")
    table.add_column()
    for key, value in rows:
        table.add_row(str(key), "" if value is None else str(value))
    if heading:
        console.print(Panel(table, title=heading, title_align="left", border_style="bright_black"))
    else:
        console.print(table)


# ---------------------------------------------------------------------------
# Board tables
# ---------------------------------------------------------------------------

#: Full column set, widest first to drop. The board is dense, so on a narrow
#: terminal columns are removed rather than wrapped -- a wrapped table of
#: numbers is unreadable, and during a live draft legibility beats completeness.
BOARD_COLUMNS: tuple[str, ...] = (
    "#", "Player", "Pos", "Team", "Bye", "Proj", "VOR", "Tier",
    "ADP", "Val", "Avail", "Fit", "Risk", "Score",
)

#: Columns to drop, in the order they are given up as the terminal narrows.
_DROP_ORDER: tuple[str, ...] = ("Bye", "Fit", "Team", "Val", "Risk", "Tier", "ADP", "VOR")

#: Roughly how many characters each column needs, including padding.
_COLUMN_WIDTHS: dict[str, int] = {
    "#": 4, "Player": 24, "Pos": 5, "Team": 6, "Bye": 5, "Proj": 8, "VOR": 8,
    "Tier": 6, "ADP": 7, "Val": 6, "Avail": 7, "Fit": 6, "Risk": 8, "Score": 8,
}


def visible_columns(width: int | None = None) -> list[str]:
    """The columns that fit in the current terminal."""
    available = width if width is not None else console.width
    columns = list(BOARD_COLUMNS)
    for candidate in _DROP_ORDER:
        if sum(_COLUMN_WIDTHS[column] for column in columns) <= available:
            break
        if candidate in columns:
            columns.remove(candidate)
    return columns


def board_row(index: int, player: PlayerAnalysis) -> list[Any]:
    adp_value = (
        f"{player.adp_value.rank_difference:+.0f}"
        if player.adp_value and player.adp_value.rank_difference is not None
        else "-"
    )
    return [
        str(index),
        player.name,
        position_text(player.position),
        player.team or "-",
        str(player.bye_week) if player.bye_week else "-",
        f"{player.projected_points:.1f}",
        f"{player.vor.vor:+.1f}",
        str(player.tier.tier) if player.tier else "-",
        f"{player.adp:.1f}" if player.adp is not None else "-",
        adp_value,
        availability_text(player.availability.probability if player.availability else None),
        f"{player.roster_fit.total:+.0f}" if player.roster_fit else "-",
        risk_text(player.risk.level if player.risk else None),
        f"{player.score:+.1f}",
    ]


#: Legend text per column, so the footnote only explains what is on screen.
_LEGEND: dict[str, str] = {
    "Proj": "Proj = league-adjusted season points",
    "VOR": "VOR = points over replacement",
    "Val": "Val = ADP minus our rank (picks)",
    "Avail": "Avail = P(available at your next pick)",
    "Fit": "Fit = roster-fit points",
    "Score": "Score = composite draft score",
}


def board_table(
    players: Sequence[PlayerAnalysis],
    *,
    heading: str = "Draft board",
    columns: Sequence[str] | None = None,
) -> Table:
    selected = list(columns) if columns is not None else visible_columns()
    table = Table(title=heading, header_style="bold", expand=False)
    for column in selected:
        justify = "left" if column in {"Player", "Pos", "Team"} else "right"
        table.add_column(column, justify=justify, no_wrap=True)

    keep = [BOARD_COLUMNS.index(column) for column in selected]
    for index, player in enumerate(players, start=1):
        row = board_row(index, player)
        table.add_row(*[row[position] for position in keep])
    return table


def print_board(
    players: Sequence[PlayerAnalysis], *, heading: str = "Draft board"
) -> None:
    columns = visible_columns()
    console.print(board_table(players, heading=heading, columns=columns))
    legend = [_LEGEND[column] for column in columns if column in _LEGEND]
    note("  |  ".join(legend))
    dropped = [column for column in BOARD_COLUMNS if column not in columns]
    if dropped:
        note(
            f"Hidden (terminal too narrow): {', '.join(dropped)}. "
            f"Widen the window, or use --json for every field."
        )


def print_player_detail(player: PlayerAnalysis) -> None:
    console.print(
        Panel(
            "\n".join(player.explain()),
            title=f"{player.name} ({player.position})",
            title_align="left",
            border_style=POSITION_STYLES.get(player.position, "white"),
        )
    )


def print_scoring_breakdown(player: PlayerAnalysis, limit: int = 12) -> None:
    table = Table(title=f"How {player.name}'s {player.projected_points:.1f} points are computed")
    table.add_column("Stat")
    table.add_column("Units", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Points", justify="right")
    table.add_column("Note", style="bright_black")
    for line in player.scoring.top_contributors(limit):
        table.add_row(
            line.label,
            f"{line.units:g}",
            f"{line.rate:g}",
            f"{line.points:+.2f}",
            line.note or "",
        )
    table.add_row("", "", "", f"[bold]{player.projected_points:+.2f}[/bold]", "total")
    console.print(table)


def print_replacement_levels(board: BoardAnalysis) -> None:
    table = Table(title="Replacement levels")
    table.add_column("Pos")
    table.add_column("League starters", justify="right")
    table.add_column("Replacement rank", justify="right")
    table.add_column("Replacement pts", justify="right")
    table.add_column("Method", style="bright_black")
    for position, level in sorted(
        board.replacement.by_position.items(), key=lambda item: -item[1].points
    ):
        table.add_row(
            str(position_text(position)),
            str(level.demand),
            f"{position}{level.rank}",
            f"{level.points:.1f}",
            level.method,
        )
    console.print(table)
    if board.replacement.flex_points is not None:
        note(
            f"FLEX pool ({'/'.join(board.replacement.flex_positions)}): best player left "
            f"out of every starting lineup projects {board.replacement.flex_points:.1f}"
        )


def print_tiers(board: BoardAnalysis, position: str, limit: int = 6) -> None:
    tiers = board.tiers.tiers_by_position.get(position, [])
    if not tiers:
        warn(f"No tiers computed for {position}.")
        return
    for tier in tiers[:limit]:
        console.print(f"[bold]{tier.explain()}[/bold]")
        for player in tier.players:
            console.print(f"    {player.name:<28} {player.points:>7.1f}")
        console.print()


def print_scarcity(board: BoardAnalysis) -> None:
    table = Table(title="Positional scarcity")
    table.add_column("Pos")
    table.add_column("Above replacement", justify="right")
    table.add_column("Unfilled starters", justify="right")
    table.add_column("Supply ratio", justify="right")
    table.add_column("Pts lost per player", justify="right")
    for entry in sorted(
        board.scarcity.by_position.values(),
        key=lambda item: item.scarcity_index,
        reverse=True,
    ):
        ratio = "inf" if entry.starter_supply_ratio >= 999 else f"{entry.starter_supply_ratio:.2f}"
        table.add_row(
            str(position_text(entry.position)),
            str(entry.above_replacement_count),
            str(entry.remaining_demand),
            ratio,
            f"{entry.average_decay:.1f}",
        )
    console.print(table)
    note("Supply ratio below 1.0 means fewer startable players remain than starting slots.")


def print_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))
