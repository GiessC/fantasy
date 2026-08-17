"""CSV ingestion.

Every FantasyPros rankings and projections page has a "Download CSV" button, and
that export format is far more stable than the API.  This adapter makes those
files a first-class ingestion path, which means the system is fully usable
without an API key and stays usable if an API response shape drifts.

The reader is format-agnostic on purpose: it finds the header row (FantasyPros
exports sometimes carry a title line above it), reads two-row headers where the
group label sits above the field name (``PASSING | YDS``), and routes every
column through the shared stat-mapping table.  Columns it cannot map are
reported rather than dropped silently.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.positions import normalize_position
from ..errors import SourceResponseError
from ..logging_setup import get_logger
from ..normalization.stat_mapping import map_stats, resolve_field
from ..stats import StatLine

log = get_logger(__name__)

SOURCE = "csv"

_NAME_COLUMNS = ("player", "player name", "name", "playername")
_TEAM_COLUMNS = ("team", "tm", "player team", "team id")
_POSITION_COLUMNS = ("pos", "position", "player position")
_RANK_COLUMNS = ("rk", "rank", "ecr", "overall rank", "rank ecr")
_ADP_COLUMNS = ("adp", "avg", "average pick", "avg pick")
_BEST_COLUMNS = ("best", "best rank", "min")
_WORST_COLUMNS = ("worst", "worst rank", "max")
_AVG_COLUMNS = ("avg rank", "average rank", "avg.")
_STDEV_COLUMNS = ("std dev", "stdev", "std", "std. dev")
_TIER_COLUMNS = ("tier", "tiers")
_BYE_COLUMNS = ("bye", "bye week")


@dataclass(slots=True)
class CSVRow:
    name: str
    position: str | None
    team: str | None
    bye_week: int | None = None
    rank: float | None = None
    adp: float | None = None
    best: float | None = None
    worst: float | None = None
    average: float | None = None
    stdev: float | None = None
    tier: int | None = None
    stats: StatLine = field(default_factory=StatLine)
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CSVImportReport:
    path: Path
    rows: int = 0
    header_row_index: int = 0
    mapped_columns: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    skipped_rows: int = 0

    def describe(self) -> list[str]:
        lines = [f"{self.path.name}: {self.rows} row(s), header on line {self.header_row_index + 1}"]
        if self.mapped_columns:
            lines.append(
                "stat columns: "
                + ", ".join(f"{k} -> {v}" for k, v in sorted(self.mapped_columns.items()))
            )
        if self.unmapped_columns:
            lines.append("ignored columns: " + ", ".join(sorted(set(self.unmapped_columns))))
        if self.skipped_rows:
            lines.append(f"skipped {self.skipped_rows} row(s) with no player name")
        return lines


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _looks_like_header(cells: Sequence[str]) -> bool:
    """A header row names a player column and has several non-empty cells."""
    lowered = [_clean(cell).lower() for cell in cells]
    if sum(1 for cell in lowered if cell) < 2:
        return False
    return any(cell in _NAME_COLUMNS for cell in lowered)


def _merge_two_row_header(top: Sequence[str], bottom: Sequence[str]) -> list[str]:
    """Combine a grouped header (``PASSING`` over ``YDS``) into ``passing yds``."""
    merged: list[str] = []
    current_group = ""
    for index, cell in enumerate(bottom):
        group = _clean(top[index]) if index < len(top) else ""
        if group:
            current_group = group
        name = _clean(cell)
        merged.append(f"{current_group} {name}".strip() if current_group else name)
    return merged


def read_rows(
    path: Path, *, position_hint: str | None = None
) -> tuple[list[CSVRow], CSVImportReport]:
    """Read a CSV export into normalised rows."""
    if not path.exists():
        raise SourceResponseError(f"CSV file not found: {path}", source=SOURCE)

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SourceResponseError(f"Cannot read {path}: {exc}", source=SOURCE) from exc

    raw_rows = list(csv.reader(text.splitlines()))
    if not raw_rows:
        raise SourceResponseError(f"{path} is empty.", source=SOURCE)

    header_index = next(
        (index for index, row in enumerate(raw_rows[:10]) if _looks_like_header(row)), None
    )
    if header_index is None:
        raise SourceResponseError(
            f"Could not find a header row in {path}. Expected a column named one of "
            f"{list(_NAME_COLUMNS)} within the first 10 lines.",
            source=SOURCE,
        )

    header = [_clean(cell) for cell in raw_rows[header_index]]
    body_start = header_index + 1
    # A grouped header has a mostly-empty row above it whose labels span groups.
    if header_index > 0:
        above = [_clean(cell) for cell in raw_rows[header_index - 1]]
        if above and sum(1 for cell in above if cell) < len(header) / 2:
            header = _merge_two_row_header(above, header)

    report = CSVImportReport(path=path, header_row_index=header_index)
    lowered_header = [cell.lower() for cell in header]

    rows: list[CSVRow] = []
    for raw in raw_rows[body_start:]:
        if not any(_clean(cell) for cell in raw):
            continue
        record = {
            header[index]: _clean(value)
            for index, value in enumerate(raw)
            if index < len(header) and header[index]
        }
        name = _lookup(record, lowered_header, _NAME_COLUMNS)
        if not name:
            report.skipped_rows += 1
            continue

        position = normalize_position(
            _lookup(record, lowered_header, _POSITION_COLUMNS) or position_hint
        )
        # Rankings exports encode position rank in the POS column ("RB4").
        if position is None and position_hint:
            position = normalize_position(position_hint)

        misses: Counter = Counter()
        stats = map_stats(record, source=SOURCE, position=position, misses=misses)
        report.unmapped_columns.extend(misses)
        for column in record:
            mapped = resolve_field(column, source=SOURCE, position=position)
            if mapped:
                report.mapped_columns[column] = mapped

        rows.append(
            CSVRow(
                name=_strip_team_suffix(name),
                position=position,
                team=_lookup(record, lowered_header, _TEAM_COLUMNS) or _team_from_name(name),
                bye_week=_as_int(_lookup(record, lowered_header, _BYE_COLUMNS)),
                rank=_as_float(_lookup(record, lowered_header, _RANK_COLUMNS)),
                adp=_as_float(_lookup(record, lowered_header, _ADP_COLUMNS)),
                best=_as_float(_lookup(record, lowered_header, _BEST_COLUMNS)),
                worst=_as_float(_lookup(record, lowered_header, _WORST_COLUMNS)),
                average=_as_float(_lookup(record, lowered_header, _AVG_COLUMNS)),
                stdev=_as_float(_lookup(record, lowered_header, _STDEV_COLUMNS)),
                tier=_as_int(_lookup(record, lowered_header, _TIER_COLUMNS)),
                stats=stats,
                raw=record,
            )
        )

    report.rows = len(rows)
    if not rows:
        raise SourceResponseError(f"No usable rows found in {path}.", source=SOURCE)
    log.debug("Read %d rows from %s", len(rows), path)
    return rows, report


def _lookup(
    record: dict[str, str], lowered_header: Sequence[str], candidates: Sequence[str]
) -> str | None:
    """Case-insensitive column lookup, trying exact then suffix matches."""
    lowered = {key.lower(): value for key, value in record.items()}
    for candidate in candidates:
        value = lowered.get(candidate)
        if value:
            return value
    for candidate in candidates:
        for key, value in lowered.items():
            if value and (key.endswith(f" {candidate}") or key == candidate):
                return value
    return None


def _strip_team_suffix(name: str) -> str:
    """FantasyPros CSVs render names as ``Player Name TEAM``; drop the team."""
    parts = name.split()
    if len(parts) > 1 and parts[-1].isupper() and 2 <= len(parts[-1]) <= 3:
        return " ".join(parts[:-1])
    return name


def _team_from_name(name: str) -> str | None:
    parts = name.split()
    if len(parts) > 1 and parts[-1].isupper() and 2 <= len(parts[-1]) <= 3:
        return parts[-1]
    return None


def _as_float(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def discover(directory: Path, pattern: str = "*.csv") -> list[Path]:
    """Every CSV in an import directory, sorted for deterministic ingestion."""
    if not directory.exists():
        return []
    return sorted(directory.glob(pattern))


def rows_with_stats(rows: Iterable[CSVRow]) -> list[CSVRow]:
    return [row for row in rows if row.stats]
