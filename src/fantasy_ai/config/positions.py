"""Positions and roster-slot semantics.

Nothing in this module hard-codes *a* league; it defines the vocabulary that a
league's YAML draws from.  A slot is either

* a **direct** slot that accepts exactly one position (``QB``, ``RB``, ...),
* a **flex** slot that accepts several (``FLEX``, ``SUPERFLEX``, ``WRT``, ...), or
* a **reserve** slot that never starts (``BENCH``, ``IR``, ``TAXI``).

Flex eligibility is defaulted here and overridable per league, so an unusual
slot (``WRT``, ``REC_FLEX``, an IDP flex) needs YAML, not code.
"""

from __future__ import annotations

from typing import Final

QB: Final = "QB"
RB: Final = "RB"
WR: Final = "WR"
TE: Final = "TE"
K: Final = "K"
DST: Final = "DST"

OFFENSE_POSITIONS: Final[tuple[str, ...]] = (QB, RB, WR, TE)
IDP_POSITIONS: Final[tuple[str, ...]] = ("DL", "LB", "DB", "DE", "DT", "CB", "S", "IDP")

#: Positions the system knows how to score and rank out of the box.
KNOWN_POSITIONS: Final[frozenset[str]] = frozenset(
    OFFENSE_POSITIONS + (K, DST) + IDP_POSITIONS
)

#: Slots that hold players but never contribute to the starting lineup.
RESERVE_SLOTS: Final[tuple[str, ...]] = ("BENCH", "IR", "TAXI")

#: Reserve slots whose players count against the drafted-roster size.
#: ``IR`` and ``TAXI`` are typically filled after the draft, so by default only
#: ``BENCH`` extends draft-relevant roster capacity.
DRAFTABLE_RESERVE_SLOTS: Final[tuple[str, ...]] = ("BENCH",)

#: Default eligibility for well-known flex slot names.  A league may override
#: any of these (or introduce its own name) through ``league.flex.slots``.
DEFAULT_FLEX_ELIGIBILITY: Final[dict[str, tuple[str, ...]]] = {
    "FLEX": (RB, WR, TE),
    "WRT": (WR, TE),
    "RBWR": (RB, WR),
    "WRRB": (RB, WR),
    "REC_FLEX": (WR, TE),
    "SUPERFLEX": (QB, RB, WR, TE),
    "SFLEX": (QB, RB, WR, TE),
    "OP": (QB, RB, WR, TE),
    "QBFLEX": (QB, RB, WR, TE),
    "IDP_FLEX": ("DL", "LB", "DB"),
    "DP": ("DL", "LB", "DB"),
}

#: Source-specific spellings that normalize onto a canonical position.
_POSITION_ALIASES: Final[dict[str, str]] = {
    "PK": K, "KICKER": K, "K": K,
    "D/ST": DST, "DEF": DST, "D": DST, "DST": DST, "TEAM_DEF": DST, "TMDEF": DST,
    "FB": RB, "HB": RB, "RB/FB": RB,
    "WR/RB": WR, "SE": WR, "FL": WR,
    "QB": QB, "RB": RB, "WR": WR, "TE": TE,
    "OLB": "LB", "ILB": "LB", "MLB": "LB",
    "FS": "S", "SS": "S", "NB": "CB",
    "DE": "DE", "DT": "DT", "CB": "CB", "S": "S", "DL": "DL", "LB": "LB", "DB": "DB",
}


def normalize_position(raw: str | None) -> str | None:
    """Map a source's position string onto the canonical vocabulary.

    Returns ``None`` for empty input, and passes unknown values through
    upper-cased so a new position type does not silently vanish.
    """
    if raw is None:
        return None
    cleaned = raw.strip().upper().replace(" ", "")
    if not cleaned:
        return None
    return _POSITION_ALIASES.get(cleaned, cleaned)


def normalize_slot(raw: str) -> str:
    """Canonicalise a roster slot name (upper-case, underscores, no spaces)."""
    return raw.strip().upper().replace(" ", "_").replace("/", "").replace("-", "_")


def is_reserve_slot(slot: str) -> bool:
    return normalize_slot(slot) in RESERVE_SLOTS


def default_eligibility(slot: str) -> tuple[str, ...] | None:
    """Built-in eligibility for a flex slot name, or ``None`` if it is not a known flex."""
    return DEFAULT_FLEX_ELIGIBILITY.get(normalize_slot(slot))


def looks_like_flex(slot: str) -> bool:
    """True when the slot name is a known flex or contains ``FLEX``."""
    name = normalize_slot(slot)
    return name in DEFAULT_FLEX_ELIGIBILITY or "FLEX" in name
