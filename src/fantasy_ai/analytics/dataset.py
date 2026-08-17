"""Assembling the analysis dataset from stored snapshots.

Several sources may have projected, ranked, or priced the same player.  This
module resolves each dataset down to one record per player using the configured
source priority, and reports which source won -- so a number on screen can
always be traced back to who published it and when.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import AnalyticsConfig, LeagueConfig
from ..db import Repositories
from ..errors import DataMissingError
from ..logging_setup import get_logger
from ..models import ADPRecord, InjuryRecord, PlayerData, ProjectionRecord, RankingRecord

log = get_logger(__name__)


def _pick_by_priority(
    records: Sequence[ProjectionRecord | RankingRecord | ADPRecord | InjuryRecord],
    priority: Sequence[str],
):
    """Choose the record whose source ranks highest; ties break on recency."""
    if not records:
        return None
    order = {source: index for index, source in enumerate(priority)}
    return min(
        records,
        key=lambda record: (
            order.get(record.source, len(order)),
            -(record.retrieved_at.timestamp() if record.retrieved_at else 0.0),
        ),
    )


@dataclass(slots=True)
class Dataset:
    """Everything analytics needs for one season, already source-resolved."""

    season: int
    players: dict[str, PlayerData] = field(default_factory=dict)
    #: All ranking records per player, so cross-source disagreement is visible.
    all_rankings: dict[str, list[RankingRecord]] = field(default_factory=dict)
    source_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.players)

    def with_projections(self) -> list[PlayerData]:
        return [data for data in self.players.values() if data.projection is not None]

    def adp_records(self) -> dict[str, ADPRecord]:
        return {
            player_id: data.adp
            for player_id, data in self.players.items()
            if data.adp is not None
        }

    def ranking_records(self) -> dict[str, RankingRecord]:
        return {
            player_id: data.ranking
            for player_id, data in self.players.items()
            if data.ranking is not None
        }

    def describe_sources(self) -> list[str]:
        lines = []
        for dataset, counts in sorted(self.source_usage.items()):
            rendered = ", ".join(
                f"{source}: {count}" for source, count in sorted(counts.items())
            )
            lines.append(f"{dataset}: {rendered}")
        return lines


def load_dataset(
    repos: Repositories,
    league: LeagueConfig,
    config: AnalyticsConfig | None = None,
    *,
    season: int | None = None,
    require_projections: bool = True,
) -> Dataset:
    """Load and source-resolve everything for a season."""
    config = config or AnalyticsConfig()
    target_season = season if season is not None else league.season

    players = repos.players.all()
    if not players:
        raise DataMissingError(
            "No players in the database. Run 'fantasy-ai sync players' "
            "(or 'fantasy-ai sync demo' to generate a synthetic dataset)."
        )

    projections = repos.projections.latest(target_season)
    rankings = repos.rankings.latest(target_season)
    adp = repos.adp.latest(target_season)
    injuries = repos.injuries.latest(target_season)

    if require_projections and not projections:
        raise DataMissingError(
            f"No projections stored for season {target_season}. Run "
            f"'fantasy-ai sync projections' (or 'fantasy-ai sync demo')."
        )

    dataset = Dataset(season=target_season, all_rankings=rankings)
    usage: dict[str, dict[str, int]] = {
        "projections": {}, "rankings": {}, "adp": {}, "injuries": {}
    }

    for player in players:
        projection = _pick_by_priority(
            projections.get(player.player_id, []), config.projection_source_priority
        )
        ranking = _pick_by_priority(
            rankings.get(player.player_id, []), config.ranking_source_priority
        )
        adp_record = _pick_by_priority(
            adp.get(player.player_id, []), config.adp_source_priority
        )
        injury = _pick_by_priority(injuries.get(player.player_id, []), ["sleeper", "fantasypros"])

        for name, record in (
            ("projections", projection),
            ("rankings", ranking),
            ("adp", adp_record),
            ("injuries", injury),
        ):
            if record is not None:
                usage[name][record.source] = usage[name].get(record.source, 0) + 1

        dataset.players[player.player_id] = PlayerData(
            player=player,
            projection=projection,
            ranking=ranking,
            adp=adp_record,
            injury=injury,
        )

    dataset.source_usage = usage
    log.debug(
        "Loaded dataset for %s: %d players, %d with projections",
        target_season, len(dataset.players), len(dataset.with_projections()),
    )
    return dataset
