"""FantasyPros "cheat sheet" exports: current ADP plus several past seasons.

This is a different shape from the per-dataset exports :mod:`csv_import` reads,
and different enough to deserve its own reader:

* a **title row above the header**, with merged group labels
  (``ADP 2025-2024 (and rank)``) spread across blank cells,
* **repeated column names** -- ``Rk`` appears nine times, once per ranked
  metric per season, so a name-keyed dict silently keeps only the last,
* **season-suffixed columns** (``FPT-25``, ``Gms-24``) that carry the actual
  history,
* **ADP written as round.pick** (``3.08`` is round 3, pick 8), which is not a
  number you can compare or average,
* ``#N/A`` filler rows where the source spreadsheet had no match.

Nothing here is scored or projected. The file reports what already happened,
and history enters analytics only as durability and trajectory -- never as a
stand-in for a projection of the coming season.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config.positions import normalize_position
from ..errors import SourceResponseError

SOURCE = "cheatsheet"

#: Games in a modern NFL regular season, the denominator for availability.
GAMES_IN_SEASON = 17

#: ``FPT-25`` -> metric "FPT", season 2025.
_SEASON_COLUMN = re.compile(r"^(ADP|FPT|Pt/W|Gms)-(\d{2})$", re.IGNORECASE)

#: ``3.08`` -- round 3, pick 8. Two digits after the point, always.
_ROUND_PICK = re.compile(r"^(\d{1,2})\.(\d{2})$")

_METRIC_KEYS = {
    "adp": "adp",
    "fpt": "fantasy_points",
    "pt/w": "points_per_game",
    "gms": "games_played",
}


@dataclass(slots=True)
class CheatSheetSeason:
    """One completed season for one player."""

    season: int
    adp: float | None = None
    fantasy_points: float | None = None
    points_per_game: float | None = None
    games_played: int | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (self.adp, self.fantasy_points, self.points_per_game, self.games_played)
        )


@dataclass(slots=True)
class CheatSheetRow:
    name: str
    position: str | None = None
    team: str | None = None
    age: float | None = None
    #: Current-season ADP as an overall pick number, not round.pick.
    adp: float | None = None
    seasons: list[CheatSheetSeason] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CheatSheetReport:
    path: Path
    rows: int = 0
    header_row_index: int = 0
    round_size: int = 0
    seasons: list[int] = field(default_factory=list)
    skipped_rows: int = 0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        lines = [
            f"{self.path.name}: {self.rows} player(s), "
            f"{self.skipped_rows} row(s) skipped",
            f"History seasons: {', '.join(str(s) for s in self.seasons) or 'none'}",
            f"ADP read as round.pick over {self.round_size} teams",
        ]
        lines.extend(self.notes)
        return lines


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _number(value: str | None) -> float | None:
    text = _clean(value).replace(",", "")
    # '-' is this export's placeholder for "not applicable" (a DST has no age).
    if not text or text in {"-", "--", "N/A", "#N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick_number(value: str | None, round_size: int) -> float | None:
    """``3.08`` over 12 teams -> overall pick 32. A plain number passes through."""
    text = _clean(value)
    if not text:
        return None
    match = _ROUND_PICK.match(text)
    if match:
        rnd, pick = int(match.group(1)), int(match.group(2))
        if pick < 1:  # '31.00' and friends are filler, not a real slot
            return None
        return float((rnd - 1) * round_size + pick)
    return _number(text)


def _detect_round_size(rows: list[list[str]], columns: list[int]) -> int:
    """Largest pick suffix seen, which is the draft size the sheet assumes."""
    largest = 0
    for row in rows:
        for index in columns:
            if index >= len(row):
                continue
            match = _ROUND_PICK.match(_clean(row[index]))
            if match:
                largest = max(largest, int(match.group(2)))
    # A sheet that never shows a late pick would understate the size; 12 is the
    # near-universal convention and a safer floor than guessing smaller.
    return largest if largest >= 8 else 12


def _find_header(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows[:8]):
        if row and _clean(row[0]).lower() == "player":
            return index
    raise SourceResponseError(
        "Could not find the header row: no row in the first 8 begins with 'Player'. "
        "Is this a FantasyPros cheat-sheet export?",
        source=SOURCE,
    )


def read_cheat_sheet(
    path: str | Path, *, league_teams: int | None = None
) -> tuple[list[CheatSheetRow], CheatSheetReport]:
    """Parse a cheat-sheet export into rows plus a report of what was read.

    ``league_teams`` rescales pick numbers when your league is not the size the
    sheet assumes: the *order* is the market's, but pick 32 of a 12-team draft
    lands around pick 27 of a 10-team one, and availability is measured in
    picks.
    """
    path = Path(path)
    if not path.exists():
        raise SourceResponseError(f"No such file: {path}", source=SOURCE)

    with path.open(newline="", encoding="utf-8-sig") as handle:
        raw_rows = [row for row in csv.reader(handle) if any(_clean(cell) for cell in row)]
    if not raw_rows:
        raise SourceResponseError(f"{path} is empty.", source=SOURCE)

    header_index = _find_header(raw_rows)
    header = [_clean(cell) for cell in raw_rows[header_index]]
    body = raw_rows[header_index + 1 :]

    # Indexed by position, never by name: 'Rk' repeats nine times and a dict
    # keyed on the header would keep only the last of them.
    plain: dict[str, int] = {}
    season_columns: dict[int, dict[str, int]] = {}
    for index, name in enumerate(header):
        match = _SEASON_COLUMN.match(name)
        if match:
            metric = _METRIC_KEYS[match.group(1).lower()]
            season = 2000 + int(match.group(2))
            season_columns.setdefault(season, {})[metric] = index
        elif name and name not in plain:
            plain[name.lower()] = index

    if "player" not in plain:
        raise SourceResponseError("Header has no 'Player' column.", source=SOURCE)

    adp_columns = [plain[key] for key in ("adp",) if key in plain]
    adp_columns += [cols["adp"] for cols in season_columns.values() if "adp" in cols]
    round_size = _detect_round_size(body, adp_columns)
    scale = (league_teams / round_size) if league_teams and league_teams != round_size else 1.0

    report = CheatSheetReport(
        path=path,
        header_row_index=header_index,
        round_size=round_size,
        seasons=sorted(season_columns, reverse=True),
    )
    if scale != 1.0:
        report.notes.append(
            f"Pick numbers scaled by {scale:.2f} for a {league_teams}-team league "
            f"(the sheet is {round_size}-team)."
        )

    def cell(row: list[str], index: int | None) -> str:
        return row[index] if index is not None and index < len(row) else ""

    rows: list[CheatSheetRow] = []
    for raw in body:
        name = _clean(cell(raw, plain.get("player")))
        # '#N/A' rows are spreadsheet lookup failures, not players.
        if not name or name.startswith("#"):
            report.skipped_rows += 1
            continue

        position = normalize_position(_clean(cell(raw, plain.get("pos"))))
        current_adp = _pick_number(cell(raw, plain.get("adp")), round_size)
        entry = CheatSheetRow(
            name=name,
            position=position,
            team=_clean(cell(raw, plain.get("tm"))) or None,
            age=_number(cell(raw, plain.get("age"))),
            adp=current_adp * scale if current_adp is not None else None,
            raw={key: _clean(cell(raw, index)) for key, index in plain.items()},
        )

        for season in sorted(season_columns, reverse=True):
            columns = season_columns[season]
            adp = _pick_number(cell(raw, columns.get("adp")), round_size)
            games = _number(cell(raw, columns.get("games_played")))
            history = CheatSheetSeason(
                season=season,
                adp=adp * scale if adp is not None else None,
                fantasy_points=_number(cell(raw, columns.get("fantasy_points"))),
                points_per_game=_number(cell(raw, columns.get("points_per_game"))),
                games_played=int(games) if games is not None else None,
            )
            # A season the player did not exist for carries no information, and
            # storing it as zeros would read as "played, scored nothing".
            if not history.is_empty:
                entry.seasons.append(history)

        rows.append(entry)

    report.rows = len(rows)
    if not rows:
        raise SourceResponseError(
            f"{path} has a header but no player rows.", source=SOURCE
        )
    return rows, report
