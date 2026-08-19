"""FantasyPros API adapter.

FantasyPros is the primary fantasy source: expert consensus rankings, ADP,
projections, and the expert dispersion (best / worst / average / standard
deviation) that the consensus-disagreement and risk analytics depend on.

**A deliberate design choice about robustness.**  This adapter cannot be
verified against the live API without a key, and FantasyPros has changed
response shapes before.  So rather than hard-coding one field layout, it:

* takes endpoint paths from configuration (``sources.fantasypros.endpoints``),
  so a path change is a YAML edit, not a code change;
* extracts each field by trying a list of known aliases, and reports which
  aliases actually matched (``fantasy-ai sync rankings --verbose``);
* routes statistics through the same
  :mod:`fantasy_ai.normalization.stat_mapping` table every source uses, so both
  flat rows and nested ``stats`` objects work;
* raises :class:`SourceResponseError` with the observed keys when a payload
  cannot be understood, so the fix is obvious.

If the API shape has drifted past what the aliases cover, ``sync ... --from-csv``
ingests the CSV export from any FantasyPros rankings or projections page, which
is a stable format and needs no key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import FantasyProsConfig, HTTPConfig
from ..config.positions import normalize_position
from ..errors import SourceAuthError, SourceResponseError
from ..logging_setup import get_logger
from ..models import utcnow
from ..normalization.stat_mapping import map_stats
from ..stats import StatLine
from .http import HTTPSource, Response

log = get_logger(__name__)

SOURCE = "fantasypros"

#: Field aliases, tried in order. First present, non-empty value wins.
_NAME_KEYS = ("player_name", "name", "player", "fantasypros_player_name")
_ID_KEYS = ("player_id", "fantasypros_player_id", "id", "player_filter_id")
_TEAM_KEYS = ("player_team_id", "team_id", "player_team", "team")
_POSITION_KEYS = ("player_position_id", "position_id", "player_position", "position", "pos")
_BYE_KEYS = ("player_bye_week", "bye_week", "bye")
_ECR_KEYS = ("rank_ecr", "ecr", "rank", "avg_rank", "overall_rank")
_POS_RANK_KEYS = ("pos_rank", "position_rank", "rank_pos")
_BEST_KEYS = ("rank_min", "best", "best_rank", "min")
_WORST_KEYS = ("rank_max", "worst", "worst_rank", "max")
_AVERAGE_KEYS = ("rank_ave", "rank_avg", "average", "avg")
_STDEV_KEYS = ("rank_std", "std_dev", "stdev", "std")
_TIER_KEYS = ("tier", "rank_tier")
_ADP_KEYS = ("adp", "rank_ecr", "average_pick", "avg_pick", "player_adp")
_EXPERTS_KEYS = ("expert_count", "num_experts", "experts")
_SAMPLE_KEYS = ("sample_size", "num_drafts", "drafts")

#: Cross-source ids FantasyPros sometimes publishes, which let a row attach to an
#: existing canonical player without name matching.
_CROSS_SOURCE_KEYS = {
    "player_yahoo_id": "yahoo",
    "yahoo_player_id": "yahoo",
    "cbs_player_id": "cbs",
    "player_espn_id": "espn",
    "espn_player_id": "espn",
    "sportradar_id": "sportradar",
    "player_sleeper_id": "sleeper",
    "sleeper_player_id": "sleeper",
}


@dataclass(slots=True)
class FantasyProsRow:
    """One normalised row from any FantasyPros endpoint."""

    source_player_id: str | None
    name: str
    position: str | None
    team: str | None
    bye_week: int | None = None
    ecr: float | None = None
    position_rank: int | None = None
    best: float | None = None
    worst: float | None = None
    average: float | None = None
    stdev: float | None = None
    tier: int | None = None
    adp: float | None = None
    expert_count: int | None = None
    sample_size: int | None = None
    stats: StatLine = field(default_factory=StatLine)
    cross_source_ids: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParseReport:
    """What the tolerant parser actually found, for --verbose output."""

    rows: int = 0
    matched_keys: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    unmapped_stat_fields: Counter = field(default_factory=Counter)
    sample_keys: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        lines = [f"parsed {self.rows} row(s)"]
        if self.matched_keys:
            lines.append(
                "field mapping: "
                + ", ".join(f"{k} <- {v}" for k, v in sorted(self.matched_keys.items()))
            )
        if self.missing_fields:
            lines.append("not found in payload: " + ", ".join(sorted(set(self.missing_fields))))
        if self.unmapped_stat_fields:
            top = ", ".join(
                f"{name} (x{count})" for name, count in self.unmapped_stat_fields.most_common(10)
            )
            lines.append(f"unmapped stat fields: {top}")
        return lines


class FantasyProsClient(HTTPSource):
    """Client for the FantasyPros public API."""

    source_name = SOURCE

    def __init__(
        self,
        config: FantasyProsConfig | None = None,
        http: HTTPConfig | None = None,
        *,
        cache_dir: Path | None = None,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = config or FantasyProsConfig()
        self._api_key = api_key if api_key is not None else self.settings.resolved_api_key()
        super().__init__(
            base_url=self.settings.base_url,
            config=http,
            cache_dir=cache_dir,
            client=client,
        )

    # -- auth --------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise SourceAuthError(
                f"No FantasyPros API key. In config/sources.yaml, set either\n"
                f"    sources.fantasypros.api_key:      <the key>\n"
                f"    sources.fantasypros.api_key_file: path/to/keyfile\n"
                f"or export ${self.settings.api_key_env}. You can also disable the "
                f"source and ingest a CSV export with "
                f"'fantasy-ai sync projections --from-csv <file>'.",
                source=SOURCE,
            )
        return {"x-api-key": self._api_key}

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def scoring_parameter(self, ppr_value: float) -> str:
        """Map the league's reception scoring onto FantasyPros' scoring buckets."""
        if self.settings.scoring != "auto":
            return self.settings.scoring
        if ppr_value >= 0.75:
            return "PPR"
        if ppr_value >= 0.25:
            return "HALF"
        return "STD"

    def _endpoint(self, name: str, **params: Any) -> str:
        template = self.settings.endpoints.get(name)
        if template is None:
            raise SourceResponseError(
                f"No endpoint configured for {name!r}. Add it under "
                f"sources.fantasypros.endpoints in config/sources.yaml.",
                source=SOURCE,
            )
        return template.format(**params)

    # -- fetching ----------------------------------------------------------

    def fetch_rankings(
        self,
        season: int,
        *,
        scoring: str,
        ranking_type: str = "DRAFT",
        position: str = "ALL",
        week: int = 0,
        force_refresh: bool = False,
    ) -> Response:
        """Consensus rankings (also the source of ADP when ``type=ADP``)."""
        return self.get_json(
            self._endpoint("consensus_rankings", season=season),
            params={
                "position": position,
                "type": ranking_type,
                "scoring": scoring,
                "week": week,
                "experts": "available",
            },
            headers=self._auth_headers(),
            force_refresh=force_refresh,
        )

    def fetch_projections(
        self,
        season: int,
        *,
        position: str,
        scoring: str,
        week: int = 0,
        force_refresh: bool = False,
    ) -> Response:
        return self.get_json(
            self._endpoint("projections", season=season),
            params={"position": position, "scoring": scoring, "week": week},
            headers=self._auth_headers(),
            force_refresh=force_refresh,
        )

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def extract_rows(payload: Any) -> list[dict[str, Any]]:
        """Find the list of player rows in a payload, whatever it is wrapped in."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            raise SourceResponseError(
                f"Unexpected FantasyPros payload type: {type(payload).__name__}.",
                source=SOURCE,
            )
        for key in ("players", "items", "data", "results", "rankings", "projections"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = FantasyProsClient.extract_rows(value)
                if nested:
                    return nested
        raise SourceResponseError(
            "Could not find a list of players in the FantasyPros payload. "
            f"Top-level keys were: {sorted(payload)[:20]}. "
            "If the API shape has changed, use 'sync ... --from-csv' or adjust "
            "sources.fantasypros.endpoints.",
            source=SOURCE,
        )

    def parse_rows(
        self, payload: Any, *, report: ParseReport | None = None
    ) -> tuple[list[FantasyProsRow], ParseReport]:
        """Parse any FantasyPros payload into normalised rows."""
        raw_rows = self.extract_rows(payload)
        report = report or ParseReport()
        if raw_rows:
            report.sample_keys = sorted(raw_rows[0])[:40]

        parsed: list[FantasyProsRow] = []
        for raw in raw_rows:
            name = _pick_str(raw, _NAME_KEYS, report, "name")
            if not name:
                continue
            position = normalize_position(_pick_str(raw, _POSITION_KEYS, report, "position"))
            stats = _extract_stats(raw, position, report)

            parsed.append(
                FantasyProsRow(
                    source_player_id=_pick_str(raw, _ID_KEYS, report, "player_id"),
                    name=name,
                    position=position,
                    team=_pick_str(raw, _TEAM_KEYS, report, "team"),
                    bye_week=_pick_int(raw, _BYE_KEYS, report, "bye_week"),
                    ecr=_pick_float(raw, _ECR_KEYS, report, "ecr"),
                    position_rank=_position_rank(raw, report),
                    best=_pick_float(raw, _BEST_KEYS, report, "best"),
                    worst=_pick_float(raw, _WORST_KEYS, report, "worst"),
                    average=_pick_float(raw, _AVERAGE_KEYS, report, "average"),
                    stdev=_pick_float(raw, _STDEV_KEYS, report, "stdev"),
                    tier=_pick_int(raw, _TIER_KEYS, report, "tier"),
                    adp=_pick_float(raw, _ADP_KEYS, report, "adp"),
                    expert_count=_pick_int(raw, _EXPERTS_KEYS, report, "expert_count"),
                    sample_size=_pick_int(raw, _SAMPLE_KEYS, report, "sample_size"),
                    stats=stats,
                    cross_source_ids=_cross_source_ids(raw),
                    raw=raw,
                )
            )

        report.rows = len(parsed)
        for label, keys in (
            ("ecr", _ECR_KEYS), ("stdev", _STDEV_KEYS), ("player_id", _ID_KEYS)
        ):
            if label not in report.matched_keys and raw_rows:
                report.missing_fields.append(f"{label} (tried {', '.join(keys)})")

        if not parsed:
            raise SourceResponseError(
                "FantasyPros payload contained no recognisable player rows. "
                f"First row keys: {report.sample_keys}",
                source=SOURCE,
            )
        return parsed, report


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _record(report: ParseReport | None, label: str, key: str) -> None:
    if report is not None and label not in report.matched_keys:
        report.matched_keys[label] = key


def _pick_raw(row: dict[str, Any], keys: Sequence[str]) -> tuple[str, Any] | None:
    for key in keys:
        if key in row and row[key] not in (None, "", "-", "--"):
            return key, row[key]
    return None


def _pick_str(
    row: dict[str, Any], keys: Sequence[str], report: ParseReport | None, label: str
) -> str | None:
    found = _pick_raw(row, keys)
    if found is None:
        return None
    key, value = found
    _record(report, label, key)
    text = str(value).strip()
    return text or None


def _pick_float(
    row: dict[str, Any], keys: Sequence[str], report: ParseReport | None, label: str
) -> float | None:
    found = _pick_raw(row, keys)
    if found is None:
        return None
    key, value = found
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    _record(report, label, key)
    return number


def _pick_int(
    row: dict[str, Any], keys: Sequence[str], report: ParseReport | None, label: str
) -> int | None:
    number = _pick_float(row, keys, report, label)
    return int(number) if number is not None else None


def _position_rank(row: dict[str, Any], report: ParseReport | None) -> int | None:
    """Position rank, from either an int or a label like ``"RB4"``."""
    found = _pick_raw(row, _POS_RANK_KEYS)
    if found is None:
        return None
    key, value = found
    _record(report, "position_rank", key)
    digits = "".join(char for char in str(value) if char.isdigit())
    return int(digits) if digits else None


def _extract_stats(
    row: dict[str, Any], position: str | None, report: ParseReport | None
) -> StatLine:
    """Pull statistics from a row, whether flat or nested under ``stats``."""
    misses: Counter = Counter()
    nested = row.get("stats")
    source_mapping = nested if isinstance(nested, dict) else row
    stats = map_stats(source_mapping, source=SOURCE, position=position, misses=misses)
    if report is not None and misses:
        report.unmapped_stat_fields.update(misses)
    return stats


def _cross_source_ids(row: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, source in _CROSS_SOURCE_KEYS.items():
        value = row.get(key)
        if value not in (None, "", 0, "0"):
            ids[source] = str(value)
    return ids


def rows_with_stats(rows: Iterable[FantasyProsRow]) -> list[FantasyProsRow]:
    return [row for row in rows if row.stats]


def default_retrieved_at() -> datetime:
    return utcnow()
