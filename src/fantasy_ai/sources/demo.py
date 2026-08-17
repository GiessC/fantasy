"""Synthetic data source.

Generates a complete, self-consistent season of players, projections, expert
rankings, ADP, and injury designations without touching the network.  It exists
for three reasons:

* the pipeline can be exercised end to end before any API key is in place;
* tests get realistic-shaped data without fixtures for every table;
* a live-draft rehearsal is possible out of season.

Everything it produces is labelled ``source="demo"`` in the database, and every
name is synthetic -- no real player's statistics are simulated or implied.  The
generator is fully deterministic given a seed.

Stat curves are shaped to resemble real positional distributions (a steep top
end at RB/WR, a long flat tail at QB, a cliff after the top few TEs) so that
replacement level, tiers, and scarcity produce plausible output.  They are *not*
projections of anything real.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..logging_setup import get_logger
from ..models import (
    ADPRecord,
    InjuryRecord,
    Player,
    ProjectionRecord,
    RankingRecord,
    utcnow,
)
from ..normalization.identity import normalize_name, split_name
from ..stats import StatLine

log = get_logger(__name__)

SOURCE = "demo"

_FIRST_NAMES = [
    "Avery", "Brennan", "Carson", "Dashiell", "Emory", "Finnian", "Grayson",
    "Harlan", "Ignatius", "Jarek", "Kellan", "Lucian", "Maddox", "Nolan",
    "Orrin", "Peyton", "Quill", "Rowan", "Sylas", "Tobin", "Ulric", "Vance",
    "Wilder", "Xander", "Yorick", "Zephyr", "Ambrose", "Bodhi", "Cassian",
    "Dorian", "Elian", "Ferris", "Gideon", "Hollis", "Isaiah", "Jonas",
]
_LAST_NAMES = [
    "Ashford", "Blackwood", "Carrow", "Deveraux", "Ellery", "Fairbanks",
    "Grimsby", "Hollingsworth", "Ironwood", "Jessup", "Kingsley", "Larkin",
    "Mercer", "Northcott", "Oakhurst", "Pemberton", "Quimby", "Ravenscroft",
    "Sinclair", "Thackeray", "Underhill", "Vandermeer", "Whitlock", "Yardley",
    "Ziegler", "Abernathy", "Bellweather", "Calloway", "Drummond", "Eastwood",
    "Fenwick", "Galloway", "Hawthorne", "Inglewood", "Jamison", "Kirkland",
]
_TEAMS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
]


@dataclass(frozen=True, slots=True)
class PositionSpec:
    """How many players to generate at a position, and the shape of the curve."""

    position: str
    count: int
    #: Multiplier applied to the top player's stat line at rank 1.
    top_scale: float
    #: Exponential decay rate across the position's ranks.
    decay: float
    #: Rank at which the curve flattens into the replacement tail.
    tail_start: int


DEFAULT_SPECS: tuple[PositionSpec, ...] = (
    PositionSpec("QB", 34, 1.00, 0.030, 18),
    PositionSpec("RB", 70, 1.00, 0.055, 36),
    PositionSpec("WR", 90, 1.00, 0.045, 48),
    PositionSpec("TE", 40, 1.00, 0.085, 14),
    PositionSpec("K", 32, 1.00, 0.020, 20),
    PositionSpec("DST", 32, 1.00, 0.028, 20),
)


def _curve(rank: int, spec: PositionSpec) -> float:
    """Value multiplier at a given position rank, in ``(0, 1]``.

    Exponential decay with a flattened tail, which is what real positional value
    curves look like: a steep early drop, then a long stretch of interchangeable
    players.
    """
    effective_rank = min(rank, spec.tail_start) + max(0, rank - spec.tail_start) * 0.35
    import math

    return spec.top_scale * math.exp(-spec.decay * (effective_rank - 1))


def _base_stats(position: str, factor: float, rng: random.Random) -> StatLine:
    """A stat line for a player at ``factor`` of the position's top output."""
    jitter = lambda spread: 1.0 + rng.uniform(-spread, spread)  # noqa: E731

    if position == "QB":
        return StatLine({
            "pass_att": round(600 * factor * jitter(0.10)),
            "pass_cmp": round(400 * factor * jitter(0.10)),
            "pass_yd": round(4800 * factor * jitter(0.08)),
            "pass_td": round(38 * factor * jitter(0.15), 1),
            "pass_int": round(9 * (1.6 - factor) * jitter(0.25), 1),
            "rush_yd": round(420 * factor * jitter(0.85)),
            "rush_td": round(4.5 * factor * jitter(0.8), 1),
            "fum_lost": round(3 * jitter(0.4), 1),
            "meta_games": 17,
        })
    if position == "RB":
        receiving_share = rng.uniform(0.15, 0.75)
        return StatLine({
            "rush_att": round(310 * factor * jitter(0.15)),
            "rush_yd": round(1450 * factor * jitter(0.12)),
            "rush_td": round(13 * factor * jitter(0.30), 1),
            "rec": round(75 * factor * receiving_share * jitter(0.25), 1),
            "rec_tgt": round(95 * factor * receiving_share * jitter(0.25)),
            "rec_yd": round(620 * factor * receiving_share * jitter(0.25)),
            "rec_td": round(3.5 * factor * receiving_share * jitter(0.5), 1),
            "fum_lost": round(1.8 * jitter(0.5), 1),
            "meta_games": 17,
        })
    if position == "WR":
        return StatLine({
            "rec": round(112 * factor * jitter(0.12), 1),
            "rec_tgt": round(160 * factor * jitter(0.12)),
            "rec_yd": round(1520 * factor * jitter(0.12)),
            "rec_td": round(11 * factor * jitter(0.35), 1),
            "rush_yd": round(45 * factor * jitter(1.0)),
            "rush_td": round(0.4 * factor * jitter(1.0), 1),
            "fum_lost": round(0.9 * jitter(0.6), 1),
            "meta_games": 17,
        })
    if position == "TE":
        return StatLine({
            "rec": round(95 * factor * jitter(0.15), 1),
            "rec_tgt": round(130 * factor * jitter(0.15)),
            "rec_yd": round(1100 * factor * jitter(0.15)),
            "rec_td": round(9 * factor * jitter(0.40), 1),
            "fum_lost": round(0.6 * jitter(0.6), 1),
            "meta_games": 17,
        })
    if position == "K":
        return StatLine({
            "kick_fgm": round(36 * factor * jitter(0.12), 1),
            "kick_fga": round(42 * factor * jitter(0.12), 1),
            "kick_fgm_0_19": round(1 * factor * jitter(0.5), 1),
            "kick_fgm_20_29": round(8 * factor * jitter(0.25), 1),
            "kick_fgm_30_39": round(11 * factor * jitter(0.25), 1),
            "kick_fgm_40_49": round(10 * factor * jitter(0.30), 1),
            "kick_fgm_50_plus": round(6 * factor * jitter(0.45), 1),
            "kick_xpm": round(44 * factor * jitter(0.18), 1),
            "kick_xpa": round(46 * factor * jitter(0.18), 1),
            "meta_games": 17,
        })
    # DST
    return StatLine({
        "dst_sack": round(52 * factor * jitter(0.18), 1),
        "dst_int": round(18 * factor * jitter(0.30), 1),
        "dst_fum_rec": round(12 * factor * jitter(0.35), 1),
        "dst_td": round(4.5 * factor * jitter(0.6), 1),
        "dst_safety": round(0.8 * factor * jitter(0.9), 1),
        "dst_blk": round(1.4 * factor * jitter(0.8), 1),
        "dst_pts_allowed": round(290 / max(factor, 0.35) * jitter(0.10)),
        "dst_yds_allowed": round(5100 / max(factor, 0.35) * jitter(0.08)),
        "meta_games": 17,
    })


@dataclass(slots=True)
class DemoDataset:
    players: list[Player]
    projections: list[ProjectionRecord]
    rankings: list[RankingRecord]
    adp: list[ADPRecord]
    injuries: list[InjuryRecord]

    def counts(self) -> dict[str, int]:
        return {
            "players": len(self.players),
            "projections": len(self.projections),
            "rankings": len(self.rankings),
            "adp": len(self.adp),
            "injuries": len(self.injuries),
        }


def generate(
    season: int,
    *,
    seed: int = 20260817,
    teams: int = 10,
    specs: tuple[PositionSpec, ...] = DEFAULT_SPECS,
    scoring_format: str = "HALF",
) -> DemoDataset:
    """Generate a complete synthetic season."""
    rng = random.Random(seed)
    retrieved = utcnow()

    players: list[Player] = []
    projections: list[ProjectionRecord] = []
    used_names: set[str] = set()

    #: (player_id, position, quality) triples used to build market data.
    quality: list[tuple[str, str, float]] = []

    for spec in specs:
        for rank in range(1, spec.count + 1):
            name = _unique_name(rng, used_names)
            player_id = f"{SOURCE}:{spec.position.lower()}{rank:03d}"
            first, last = split_name(name)
            is_dst = spec.position == "DST"

            player = Player(
                player_id=player_id,
                full_name=f"{_TEAMS[rank % len(_TEAMS)]} Defense" if is_dst else name,
                normalized_name=normalize_name(
                    f"{_TEAMS[rank % len(_TEAMS)]} Defense" if is_dst else name
                ),
                first_name=None if is_dst else first,
                last_name=None if is_dst else last,
                position=spec.position,
                team=_TEAMS[rank % len(_TEAMS)],
                age=None if is_dst else round(rng.uniform(21.5, 33.5), 1),
                years_exp=None if is_dst else max(0, int(rng.triangular(0, 12, 3))),
                status="Active",
                bye_week=rng.randint(5, 14),
                source_ids={SOURCE: f"{spec.position.lower()}{rank:03d}"},
                updated_at=retrieved,
            )
            players.append(player)

            factor = _curve(rank, spec)
            stats = _base_stats(spec.position, factor, rng)
            projections.append(
                ProjectionRecord(
                    player_id=player_id,
                    season=season,
                    source=SOURCE,
                    stats=stats,
                    scoring_context="synthetic",
                    retrieved_at=retrieved,
                )
            )
            quality.append((player_id, spec.position, factor))

    rankings, adp = _market_data(quality, season, teams, rng, retrieved, scoring_format)
    injuries = _injuries(players, season, rng, retrieved)

    log.debug("Generated demo dataset: %d players", len(players))
    return DemoDataset(
        players=players,
        projections=projections,
        rankings=rankings,
        adp=adp,
        injuries=injuries,
    )


def _unique_name(rng: random.Random, used: set[str]) -> str:
    for _ in range(200):
        name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
        if name not in used:
            used.add(name)
            return name
    # Exhausted the pool; fall back to a numbered variant.
    name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)} {len(used)}"
    used.add(name)
    return name


#: Rough per-position value weighting used to build a plausible market board.
#: Deliberately *not* the same shape the analytics engine derives, so ADP and our
#: rank disagree the way they do in reality.
_MARKET_WEIGHT = {"QB": 0.72, "RB": 1.00, "WR": 0.95, "TE": 0.80, "K": 0.10, "DST": 0.14}


def _market_data(
    quality: list[tuple[str, str, float]],
    season: int,
    teams: int,
    rng: random.Random,
    retrieved,
    scoring_format: str,
) -> tuple[list[RankingRecord], list[ADPRecord]]:
    """Build expert rankings and ADP from player quality, with market noise."""
    ordered = sorted(
        quality,
        key=lambda item: _MARKET_WEIGHT.get(item[1], 0.5) * item[2] * rng.uniform(0.88, 1.12),
        reverse=True,
    )

    rankings: list[RankingRecord] = []
    adp_records: list[ADPRecord] = []
    position_counts: dict[str, int] = {}

    for index, (player_id, position, _factor) in enumerate(ordered, start=1):
        position_counts[position] = position_counts.get(position, 0) + 1

        # Expert dispersion grows with rank; sqrt keeps it from exploding.
        spread = max(1.2, 0.55 * index**0.5) * rng.uniform(0.7, 1.4)
        rankings.append(
            RankingRecord(
                player_id=player_id,
                season=season,
                source=SOURCE,
                ecr=float(index),
                position_rank=position_counts[position],
                best=max(1.0, round(index - spread * rng.uniform(1.0, 2.0))),
                worst=round(index + spread * rng.uniform(1.0, 2.2)),
                average=round(index + rng.uniform(-1.5, 1.5), 2),
                stdev=round(spread, 2),
                expert_count=rng.randint(8, 24),
                ranking_type="DRAFT",
                scoring_format=scoring_format,
                retrieved_at=retrieved,
            )
        )

        # ADP drifts from consensus rank: the market reaches for some players
        # and lets others fall, which is exactly the signal ADP value measures.
        drift = rng.gauss(0, max(1.5, index * 0.10))
        adp_value = max(1.0, round(index + drift, 1))
        adp_records.append(
            ADPRecord(
                player_id=player_id,
                season=season,
                source=SOURCE,
                adp=adp_value,
                stdev=round(max(1.0, spread * 1.3), 2),
                best=max(1.0, round(adp_value - spread * 1.5)),
                worst=round(adp_value + spread * 1.8),
                sample_size=rng.randint(150, 900),
                teams=teams,
                scoring_format=scoring_format,
                retrieved_at=retrieved,
            )
        )

    return rankings, adp_records


#: Designation pool with roughly realistic preseason frequencies.
_INJURY_POOL = [
    (None, 0.86), ("Questionable", 0.06), ("Doubtful", 0.02),
    ("Out", 0.02), ("PUP", 0.02), ("IR", 0.02),
]

_INJURY_DESCRIPTIONS = [
    "Hamstring", "Ankle", "Knee", "Shoulder", "Foot", "Hip", "Groin", "Concussion",
]


def _injuries(
    players: list[Player], season: int, rng: random.Random, retrieved
) -> list[InjuryRecord]:
    records: list[InjuryRecord] = []
    statuses = [status for status, _ in _INJURY_POOL]
    weights = [weight for _, weight in _INJURY_POOL]

    for player in players:
        if player.position == "DST":
            continue
        status = rng.choices(statuses, weights=weights, k=1)[0]
        if status is None:
            continue
        player.injury_status = status
        records.append(
            InjuryRecord(
                player_id=player.player_id,
                season=season,
                source=SOURCE,
                status=status,
                description=f"{rng.choice(_INJURY_DESCRIPTIONS)} - synthetic demo data",
                retrieved_at=retrieved,
            )
        )
    return records
