"""Sleeper API adapter.

Sleeper is used for what DATA_SOURCES.md assigns it: canonical player metadata
and identity, plus live league/draft state.  It needs no authentication.

Endpoints used (all under ``https://api.sleeper.app/v1``):

======================================  ==========================================
``/players/nfl``                        every player, keyed by Sleeper id (~5 MB)
``/state/nfl``                          current season and week
``/user/{username}``                    resolve a username to a user id
``/user/{user_id}/leagues/nfl/{season}``     a user's leagues
``/league/{league_id}``                 league settings and scoring
``/league/{league_id}/rosters``         rosters
``/league/{league_id}/drafts``          drafts belonging to a league
``/draft/{draft_id}``                   draft settings and slot mapping
``/draft/{draft_id}/picks``             picks made so far
======================================  ==========================================

The player payload is large and changes slowly, so it gets its own long cache
TTL (``sources.sleeper.player_cache_ttl_seconds``).  Draft picks are never
cached -- during a live draft, a stale pick list is worse than no pick list.

Sleeper is also the best available source of *identity*: its ids are stable and
widely cross-referenced, so syncing players first gives every later source a
canonical player to attach to.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import HTTPConfig, SleeperConfig
from ..config.positions import KNOWN_POSITIONS, normalize_position
from ..errors import SourceResponseError
from ..logging_setup import get_logger
from ..models import InjuryRecord, Player, utcnow
from ..normalization.identity import normalize_name, normalize_team, split_name
from .http import HTTPSource, Response

log = get_logger(__name__)

SOURCE = "sleeper"

#: Sleeper statuses that mean the player is not on an active roster.
_INACTIVE_STATUSES = {"Inactive", "Retired", "Non Football Injury", "Practice Squad"}


@dataclass(slots=True)
class SleeperDraft:
    """Draft metadata as Sleeper reports it."""

    draft_id: str
    league_id: str | None
    season: int
    status: str
    draft_type: str
    teams: int
    rounds: int
    #: ``{user_id: slot}`` -- Sleeper's ``draft_order``.
    draft_order: dict[str, int]
    #: ``{slot: roster_id}`` -- Sleeper's ``slot_to_roster_id``.
    slot_to_roster: dict[int, int]
    settings: dict[str, Any]

    def slot_for_user(self, user_id: str) -> int | None:
        return self.draft_order.get(user_id)


@dataclass(slots=True)
class SleeperPick:
    """One pick from a Sleeper draft."""

    pick_no: int
    round_number: int
    draft_slot: int
    sleeper_player_id: str | None
    picked_by: str | None
    roster_id: int | None
    is_keeper: bool = False
    metadata: dict[str, Any] | None = None

    @property
    def player_name(self) -> str | None:
        if not self.metadata:
            return None
        first = self.metadata.get("first_name") or ""
        last = self.metadata.get("last_name") or ""
        combined = f"{first} {last}".strip()
        return combined or None


class SleeperClient(HTTPSource):
    """Read-only client for the Sleeper API."""

    source_name = SOURCE

    def __init__(
        self,
        config: SleeperConfig | None = None,
        http: HTTPConfig | None = None,
        *,
        cache_dir: Path | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = config or SleeperConfig()
        super().__init__(
            base_url=self.settings.base_url,
            config=http,
            cache_dir=cache_dir,
            client=client,
            rate_limit_interval=self.settings.rate_limit_interval,
        )

    # -- players -----------------------------------------------------------

    def fetch_players(self, *, force_refresh: bool = False) -> Response:
        """The full NFL player dictionary (large; cached aggressively)."""
        return self.get_json(
            "players/nfl",
            cache_ttl_seconds=self.settings.player_cache_ttl_seconds,
            force_refresh=force_refresh,
        )

    def parse_players(
        self, payload: Any, *, retrieved_at: datetime | None = None
    ) -> tuple[list[Player], list[InjuryRecord]]:
        """Convert Sleeper's player dictionary into canonical players.

        Team defenses appear with a position of ``DEF`` and the team code as the
        id; they are normalised to ``DST`` with a readable name so they can be
        drafted like anyone else.
        """
        if not isinstance(payload, dict):
            raise SourceResponseError(
                "Sleeper players payload was not a JSON object. "
                "Expected {player_id: {...}}.",
                source=SOURCE,
            )
        stamp = retrieved_at or utcnow()
        players: list[Player] = []
        injuries: list[InjuryRecord] = []
        skipped = 0

        for sleeper_id, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            position = normalize_position(
                raw.get("position") or _first(raw.get("fantasy_positions"))
            )
            if position is None:
                continue
            # Sleeper ships every player under contract, so roughly half the
            # payload is offensive linemen, punters, and long snappers that no
            # fantasy format can start. Storing them bloats the player table and
            # the identity index for nothing.
            if self.settings.fantasy_positions_only and position not in KNOWN_POSITIONS:
                skipped += 1
                continue

            team = normalize_team(raw.get("team"))
            if position == "DST":
                name = f"{team or sleeper_id} Defense"
                first = last = None
            else:
                name = (
                    raw.get("full_name")
                    or " ".join(
                        part for part in (raw.get("first_name"), raw.get("last_name")) if part
                    )
                    or str(sleeper_id)
                )
                first, last = split_name(name)

            players.append(
                Player(
                    player_id=f"{SOURCE}:{sleeper_id}",
                    full_name=name,
                    normalized_name=normalize_name(name),
                    first_name=first,
                    last_name=last,
                    position=position,
                    team=team,
                    age=_as_float(raw.get("age")),
                    years_exp=_as_int(raw.get("years_exp")),
                    status=raw.get("status"),
                    injury_status=raw.get("injury_status"),
                    height=raw.get("height"),
                    weight=raw.get("weight"),
                    college=raw.get("college"),
                    birth_date=raw.get("birth_date"),
                    depth_chart_order=_as_int(raw.get("depth_chart_order")),
                    source_ids=_source_ids(sleeper_id, raw),
                    updated_at=stamp,
                )
            )

            if raw.get("injury_status"):
                injuries.append(
                    InjuryRecord(
                        player_id=f"{SOURCE}:{sleeper_id}",
                        season=0,  # filled in by the sync service
                        source=SOURCE,
                        status=raw.get("injury_status"),
                        description=raw.get("injury_notes"),
                        body_part=raw.get("injury_body_part"),
                        retrieved_at=stamp,
                    )
                )

        log.debug(
            "Parsed %d Sleeper players (%d skipped as non-fantasy positions)",
            len(players), skipped,
        )
        return players, injuries

    # -- league and draft --------------------------------------------------

    def fetch_state(self) -> Response:
        return self.get_json("state/nfl", cache_ttl_seconds=3600)

    def fetch_user(self, username: str) -> Response:
        return self.get_json(f"user/{username}", cache_ttl_seconds=86400)

    def fetch_user_leagues(self, user_id: str, season: int) -> Response:
        return self.get_json(f"user/{user_id}/leagues/nfl/{season}", cache_ttl_seconds=600)

    def fetch_league(self, league_id: str) -> Response:
        return self.get_json(f"league/{league_id}", cache_ttl_seconds=600)

    def fetch_league_drafts(self, league_id: str) -> Response:
        return self.get_json(f"league/{league_id}/drafts", cache_ttl_seconds=300)

    def fetch_rosters(self, league_id: str) -> Response:
        return self.get_json(f"league/{league_id}/rosters", cache_ttl_seconds=120)

    def fetch_draft(self, draft_id: str) -> Response:
        return self.get_json(f"draft/{draft_id}", cache_ttl_seconds=120)

    def fetch_draft_picks(self, draft_id: str) -> Response:
        """Picks made so far. Never cached -- staleness here loses drafts."""
        return self.get_json(f"draft/{draft_id}/picks", use_cache=False)

    @staticmethod
    def parse_draft(payload: Any) -> SleeperDraft:
        if not isinstance(payload, dict):
            raise SourceResponseError("Sleeper draft payload was not an object.", source=SOURCE)
        settings = payload.get("settings") or {}
        raw_order = payload.get("draft_order") or {}
        raw_slots = payload.get("slot_to_roster_id") or {}
        return SleeperDraft(
            draft_id=str(payload.get("draft_id", "")),
            league_id=payload.get("league_id"),
            season=_as_int(payload.get("season")) or 0,
            status=str(payload.get("status", "unknown")),
            draft_type=str(payload.get("type", "snake")),
            teams=_as_int(settings.get("teams")) or 0,
            rounds=_as_int(settings.get("rounds")) or 0,
            draft_order={str(k): int(v) for k, v in raw_order.items() if v is not None},
            slot_to_roster={
                int(k): int(v) for k, v in raw_slots.items() if v is not None
            },
            settings=settings,
        )

    @staticmethod
    def parse_picks(payload: Any) -> list[SleeperPick]:
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise SourceResponseError("Sleeper picks payload was not a list.", source=SOURCE)
        picks: list[SleeperPick] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            picks.append(
                SleeperPick(
                    pick_no=_as_int(raw.get("pick_no")) or 0,
                    round_number=_as_int(raw.get("round")) or 0,
                    draft_slot=_as_int(raw.get("draft_slot")) or 0,
                    sleeper_player_id=(
                        str(raw["player_id"]) if raw.get("player_id") is not None else None
                    ),
                    picked_by=raw.get("picked_by") or None,
                    roster_id=_as_int(raw.get("roster_id")),
                    is_keeper=bool(raw.get("is_keeper")),
                    metadata=raw.get("metadata"),
                )
            )
        picks.sort(key=lambda pick: pick.pick_no)
        return picks

    def resolve_draft_id(self, league_id: str) -> str | None:
        """The most recent draft for a league."""
        response = self.fetch_league_drafts(league_id)
        drafts = response.data
        if not isinstance(drafts, list) or not drafts:
            return None
        newest = max(
            drafts,
            key=lambda item: _as_int(item.get("start_time")) or _as_int(item.get("created")) or 0,
        )
        return str(newest.get("draft_id")) if newest.get("draft_id") else None


def _source_ids(sleeper_id: str, raw: dict[str, Any]) -> dict[str, str]:
    """Collect the cross-source ids Sleeper publishes alongside its own.

    These are what let a FantasyPros row attach to an existing canonical player
    without falling back to name matching.
    """
    ids = {SOURCE: str(sleeper_id).strip()}
    for field_name, source in (
        ("espn_id", "espn"),
        ("yahoo_id", "yahoo"),
        ("rotowire_id", "rotowire"),
        ("sportradar_id", "sportradar"),
        ("fantasy_data_id", "fantasydata"),
        ("stats_id", "stats"),
        ("gsis_id", "gsis"),
        ("swish_id", "swish"),
    ):
        value = raw.get(field_name)
        if value in (None, "", 0):
            continue
        # Sleeper ships some ids with surrounding whitespace -- gsis_id in
        # particular arrives as " 00-0035057". An unstripped id silently fails
        # to match the same id from another source, which is exactly the
        # name-matching fallback these ids exist to avoid.
        cleaned = str(value).strip()
        if cleaned:
            ids[source] = cleaned
    return ids


def _first(value: Any) -> Any:
    if isinstance(value, list | tuple) and value:
        return value[0]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iter_active(players: Iterable[Player]) -> list[Player]:
    """Drop players Sleeper marks as not on an active roster."""
    return [
        player
        for player in players
        if not (player.status and player.status in _INACTIVE_STATUSES)
    ]
