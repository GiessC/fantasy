"""Player identity resolution.

DATA_SOURCES.md is explicit: a display name is never the identity key.  So every
player gets a canonical id, and each source's own id is recorded alongside it.

Resolution order when ingesting a record from source *X*:

1. **Known source id** -- ``player_source_ids`` already links ``X:<id>``.
2. **Cross-source id** -- the payload carries another source's id we know
   (FantasyPros rows often include a Sleeper id).
3. **Name + position + team** -- an exact normalized-name match, disambiguated
   by position and then team.
4. **Name + position** -- accepted only when unambiguous.
5. **New canonical player** -- minted as ``<source>:<source_id>``, or as a
   name-derived id when the source has no id at all.

Steps 3-5 are recorded in the resolution result so ``sync`` can report how many
players were matched by name, and how many were ambiguous and skipped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from ..config.positions import normalize_position
from ..logging_setup import get_logger
from ..models import Player, utcnow

log = get_logger(__name__)

#: Suffixes stripped before comparing names.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

#: NFL team abbreviations that differ between sources.
_TEAM_ALIASES: dict[str, str] = {
    "JAC": "JAX", "JACK": "JAX",
    "WSH": "WAS", "WFT": "WAS",
    "LA": "LAR", "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV", "LVR": "LV", "RAI": "LV",
    "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB",
    "NNO": "NO",
}

#: Common first-name variants, so "Josh"/"Joshua" style mismatches still match.
_NAME_ALIASES: dict[str, str] = {
    "joshua": "josh", "michael": "mike", "christopher": "chris",
    "matthew": "matt", "nicholas": "nick", "benjamin": "ben",
    "zachary": "zach", "zack": "zach", "jonathan": "jon", "gabriel": "gabe",
    "kenneth": "ken", "nathaniel": "nate", "nathan": "nate", "samuel": "sam",
    "william": "will", "robert": "rob", "daniel": "dan", "anthony": "tony",
    "jeffery": "jeff", "jeffrey": "jeff", "steven": "steve", "stephen": "steve",
    "cameron": "cam", "dominique": "dom", "alexander": "alex",
}


def normalize_team(team: str | None) -> str | None:
    """Canonicalise an NFL team abbreviation."""
    if not team:
        return None
    cleaned = team.strip().upper()
    if not cleaned or cleaned in {"FA", "NONE", "-", "N/A"}:
        return None
    return _TEAM_ALIASES.get(cleaned, cleaned)


def normalize_name(name: str) -> str:
    """Reduce a display name to a comparison key.

    Strips accents, punctuation, suffixes, and whitespace, then applies
    first-name aliasing::

        "Ke'Shawn Vaughn"    -> "keshawnvaughn"
        "Marvin Harrison Jr." -> "marvinharrison"
        "Joshua Palmer"       -> "joshpalmer"
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    lowered = lowered.replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9\s]", "", lowered)
    tokens = [token for token in cleaned.split() if token]

    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    if tokens:
        tokens[0] = _NAME_ALIASES.get(tokens[0], tokens[0])
    return "".join(tokens)


def split_name(full_name: str) -> tuple[str | None, str | None]:
    """Best-effort first/last split of a display name."""
    parts = [part for part in full_name.replace(",", " ").split() if part]
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    last_index = len(parts) - 1
    while last_index > 0 and parts[last_index].lower().rstrip(".") in _SUFFIXES:
        last_index -= 1
    return parts[0], " ".join(parts[1 : last_index + 1])


def canonical_id(source: str, source_player_id: str | None, *, name: str = "",
                 position: str | None = None) -> str:
    """Mint a canonical id.

    Prefers ``<source>:<source_id>`` because it is stable.  Sources without ids
    (CSV exports) fall back to a name/position key, which is stable enough for a
    single season and is always superseded once a real id shows up.
    """
    if source_player_id:
        return f"{source}:{source_player_id}"
    key = normalize_name(name) or "unknown"
    suffix = f"-{position.lower()}" if position else ""
    return f"name:{key}{suffix}"


MatchMethod = Literal["source_id", "cross_source_id", "name_position_team", "name_position", "new"]


@dataclass(slots=True)
class ResolvedPlayer:
    player_id: str
    method: MatchMethod
    created: bool
    candidates: int = 1


@dataclass(slots=True)
class ResolutionStats:
    """Counts of how identity resolution went during one sync."""

    by_method: dict[str, int] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)
    created: int = 0

    def record(self, resolved: ResolvedPlayer, label: str) -> None:
        self.by_method[resolved.method] = self.by_method.get(resolved.method, 0) + 1
        if resolved.created:
            self.created += 1
        if resolved.candidates > 1:
            self.ambiguous.append(label)

    def summary(self) -> str:
        parts = [f"{method}={count}" for method, count in sorted(self.by_method.items())]
        if self.ambiguous:
            parts.append(f"ambiguous={len(self.ambiguous)}")
        return ", ".join(parts) or "no records"


class PlayerIndex:
    """In-memory identity index over the canonical player table.

    Built once per sync so resolution is dictionary lookups rather than a query
    per record.  New players are added to the index as they are minted, so a
    batch containing the same player twice resolves consistently.
    """

    def __init__(self, players: list[Player] | None = None) -> None:
        self._by_id: dict[str, Player] = {}
        self._by_source: dict[tuple[str, str], str] = {}
        self._by_name: dict[str, list[str]] = {}
        for player in players or []:
            self.add(player)

    # -- construction ------------------------------------------------------

    def add(self, player: Player) -> None:
        if not player.normalized_name:
            player.normalized_name = normalize_name(player.full_name)
        self._by_id[player.player_id] = player
        for source, source_id in player.source_ids.items():
            if source_id:
                self._by_source[(source, str(source_id))] = player.player_id
        bucket = self._by_name.setdefault(player.normalized_name, [])
        if player.player_id not in bucket:
            bucket.append(player.player_id)

    def link(self, source: str, source_id: str, player_id: str) -> None:
        self._by_source[(source, str(source_id))] = player_id
        player = self._by_id.get(player_id)
        if player is not None:
            player.source_ids[source] = str(source_id)

    # -- lookups -----------------------------------------------------------

    def get(self, player_id: str) -> Player | None:
        return self._by_id.get(player_id)

    def by_source(self, source: str, source_id: str) -> str | None:
        return self._by_source.get((source, str(source_id)))

    def by_name(self, name: str) -> list[Player]:
        key = normalize_name(name)
        return [self._by_id[pid] for pid in self._by_name.get(key, []) if pid in self._by_id]

    def __len__(self) -> int:
        return len(self._by_id)

    def players(self) -> list[Player]:
        return list(self._by_id.values())

    # -- resolution --------------------------------------------------------

    def resolve(
        self,
        *,
        source: str,
        source_player_id: str | None,
        name: str,
        position: str | None = None,
        team: str | None = None,
        cross_source_ids: dict[str, str] | None = None,
        create_missing: bool = True,
    ) -> ResolvedPlayer | None:
        """Find (or mint) the canonical player for an incoming source record.

        Returns ``None`` when the record cannot be resolved and
        ``create_missing`` is false, or when a name match is ambiguous.
        """
        position = normalize_position(position)
        team = normalize_team(team)

        # 1. this source's own id
        if source_player_id:
            existing = self.by_source(source, str(source_player_id))
            if existing:
                self._enrich(existing, position=position, team=team, name=name)
                return ResolvedPlayer(existing, "source_id", created=False)

        # 2. an id from another source carried in the payload
        for other_source, other_id in (cross_source_ids or {}).items():
            if not other_id:
                continue
            existing = self.by_source(other_source, str(other_id))
            if existing:
                if source_player_id:
                    self.link(source, str(source_player_id), existing)
                self._enrich(existing, position=position, team=team, name=name)
                return ResolvedPlayer(existing, "cross_source_id", created=False)

        # 3/4. name matching, narrowed by position then team
        matches = self.by_name(name)
        if matches:
            if position:
                narrowed = [p for p in matches if p.position == position]
                # DST rows sometimes carry no position on one side; keep those.
                matches = narrowed or [p for p in matches if p.position is None] or matches
            if len(matches) > 1 and team:
                by_team = [p for p in matches if p.team == team]
                if by_team:
                    return self._accept_name_match(
                        by_team, source, source_player_id, position, team, name,
                        "name_position_team",
                    )
            if len(matches) == 1:
                return self._accept_name_match(
                    matches, source, source_player_id, position, team, name, "name_position"
                )
            if len(matches) > 1:
                log.debug(
                    "Ambiguous name match for %r (%s/%s): %s",
                    name, position, team, [p.player_id for p in matches],
                )
                if not create_missing:
                    return ResolvedPlayer(
                        matches[0].player_id, "name_position", created=False,
                        candidates=len(matches),
                    )

        if not create_missing:
            return None

        # 5. mint a new canonical player
        new_id = canonical_id(source, source_player_id, name=name, position=position)
        if new_id in self._by_id:
            # Name-derived id collision: two different players, same name and
            # position, neither with a source id. Disambiguate with the team.
            new_id = f"{new_id}-{(team or 'unk').lower()}"
        first, last = split_name(name)
        player = Player(
            player_id=new_id,
            full_name=name.strip(),
            normalized_name=normalize_name(name),
            first_name=first,
            last_name=last,
            position=position,
            team=team,
            source_ids={source: str(source_player_id)} if source_player_id else {},
            updated_at=utcnow(),
        )
        self.add(player)
        return ResolvedPlayer(new_id, "new", created=True)

    def _accept_name_match(
        self,
        matches: list[Player],
        source: str,
        source_player_id: str | None,
        position: str | None,
        team: str | None,
        name: str,
        method: MatchMethod,
    ) -> ResolvedPlayer:
        player = matches[0]
        if source_player_id:
            self.link(source, str(source_player_id), player.player_id)
        self._enrich(player.player_id, position=position, team=team, name=name)
        return ResolvedPlayer(player.player_id, method, created=False, candidates=len(matches))

    def _enrich(
        self, player_id: str, *, position: str | None, team: str | None, name: str
    ) -> None:
        """Fill in fields the canonical record is missing, never overwriting."""
        player = self._by_id.get(player_id)
        if player is None:
            return
        if player.position is None and position:
            player.position = position
        if player.team is None and team:
            player.team = team
        if not player.full_name and name:
            player.full_name = name.strip()
