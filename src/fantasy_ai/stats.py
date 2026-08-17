"""Canonical statistic vocabulary.

Every source (FantasyPros, Sleeper, a CSV export) speaks its own dialect for
projected statistics.  Normalization converts each dialect into the flat
``{canonical_key: float}`` mapping defined here, and the scoring engine is
written *only* against these keys.  That is what keeps provider quirks out of
analytics.

Keys are lowercase ``snake_case`` and grouped by prefix:

======  ===============================================
prefix  meaning
======  ===============================================
pass_   quarterback passing
rush_   rushing (any position)
rec_    receiving (any position)
fum_    fumbles
misc_   returns and other offensive miscellany
kick_   place kicking
dst_    team defense / special teams
idp_    individual defensive players
meta_   not a scoring stat (games played, snap share, ...)
======  ===============================================

All counting stats are **season totals** unless the key ends in ``_pg``
(per game).  ``meta_games`` carries the number of games the projection covers,
which per-game bonus math depends on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

# --------------------------------------------------------------------------
# Passing
# --------------------------------------------------------------------------
PASS_ATT: Final = "pass_att"
PASS_CMP: Final = "pass_cmp"
PASS_INC: Final = "pass_inc"
PASS_YD: Final = "pass_yd"
PASS_TD: Final = "pass_td"
PASS_INT: Final = "pass_int"
PASS_2PT: Final = "pass_2pt"
PASS_SACKED: Final = "pass_sacked"
PASS_FIRST_DOWN: Final = "pass_first_down"

# --------------------------------------------------------------------------
# Rushing
# --------------------------------------------------------------------------
RUSH_ATT: Final = "rush_att"
RUSH_YD: Final = "rush_yd"
RUSH_TD: Final = "rush_td"
RUSH_2PT: Final = "rush_2pt"
RUSH_FIRST_DOWN: Final = "rush_first_down"

# --------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------
REC: Final = "rec"
REC_TGT: Final = "rec_tgt"
REC_YD: Final = "rec_yd"
REC_TD: Final = "rec_td"
REC_2PT: Final = "rec_2pt"
REC_FIRST_DOWN: Final = "rec_first_down"

# --------------------------------------------------------------------------
# Fumbles / misc
# --------------------------------------------------------------------------
FUM: Final = "fum"
FUM_LOST: Final = "fum_lost"
FUM_TD: Final = "fum_td"
MISC_RETURN_YD: Final = "misc_return_yd"
MISC_RETURN_TD: Final = "misc_return_td"
MISC_2PT: Final = "misc_2pt"

# --------------------------------------------------------------------------
# Kicking
# --------------------------------------------------------------------------
KICK_XPM: Final = "kick_xpm"
KICK_XPA: Final = "kick_xpa"
KICK_XP_MISS: Final = "kick_xp_miss"
KICK_FGM: Final = "kick_fgm"
KICK_FGA: Final = "kick_fga"
KICK_FG_MISS: Final = "kick_fg_miss"
KICK_FGM_0_19: Final = "kick_fgm_0_19"
KICK_FGM_20_29: Final = "kick_fgm_20_29"
KICK_FGM_30_39: Final = "kick_fgm_30_39"
KICK_FGM_40_49: Final = "kick_fgm_40_49"
KICK_FGM_50_PLUS: Final = "kick_fgm_50_plus"

#: Field-goal-made buckets in ascending distance order, with the yard span each
#: one covers.  ``None`` as an upper bound means "no upper limit".
FG_DISTANCE_BUCKETS: Final[tuple[tuple[str, int, int | None], ...]] = (
    (KICK_FGM_0_19, 0, 19),
    (KICK_FGM_20_29, 20, 29),
    (KICK_FGM_30_39, 30, 39),
    (KICK_FGM_40_49, 40, 49),
    (KICK_FGM_50_PLUS, 50, None),
)

# --------------------------------------------------------------------------
# Team defense / special teams
# --------------------------------------------------------------------------
DST_SACK: Final = "dst_sack"
DST_INT: Final = "dst_int"
DST_FUM_REC: Final = "dst_fum_rec"
DST_TD: Final = "dst_td"
DST_SAFETY: Final = "dst_safety"
DST_BLK: Final = "dst_blk"
DST_PTS_ALLOWED: Final = "dst_pts_allowed"
DST_YDS_ALLOWED: Final = "dst_yds_allowed"

# --------------------------------------------------------------------------
# IDP
# --------------------------------------------------------------------------
IDP_TACKLE_SOLO: Final = "idp_tackle_solo"
IDP_TACKLE_AST: Final = "idp_tackle_ast"
IDP_TACKLE_TOTAL: Final = "idp_tackle_total"
IDP_TACKLE_LOSS: Final = "idp_tackle_loss"
IDP_SACK: Final = "idp_sack"
IDP_INT: Final = "idp_int"
IDP_PASS_DEFENDED: Final = "idp_pass_defended"
IDP_FORCED_FUM: Final = "idp_forced_fum"
IDP_FUM_REC: Final = "idp_fum_rec"
IDP_TD: Final = "idp_td"
IDP_SAFETY: Final = "idp_safety"

# --------------------------------------------------------------------------
# Metadata (never scored directly)
# --------------------------------------------------------------------------
META_GAMES: Final = "meta_games"

#: Stats that describe the projection rather than contribute to it.
META_KEYS: Final[frozenset[str]] = frozenset({META_GAMES})

#: Every canonical scoring stat.  Used to validate custom scoring rules.
SCORING_KEYS: Final[frozenset[str]] = frozenset(
    {
        PASS_ATT, PASS_CMP, PASS_INC, PASS_YD, PASS_TD, PASS_INT, PASS_2PT,
        PASS_SACKED, PASS_FIRST_DOWN,
        RUSH_ATT, RUSH_YD, RUSH_TD, RUSH_2PT, RUSH_FIRST_DOWN,
        REC, REC_TGT, REC_YD, REC_TD, REC_2PT, REC_FIRST_DOWN,
        FUM, FUM_LOST, FUM_TD, MISC_RETURN_YD, MISC_RETURN_TD, MISC_2PT,
        KICK_XPM, KICK_XPA, KICK_XP_MISS, KICK_FGM, KICK_FGA, KICK_FG_MISS,
        KICK_FGM_0_19, KICK_FGM_20_29, KICK_FGM_30_39, KICK_FGM_40_49,
        KICK_FGM_50_PLUS,
        DST_SACK, DST_INT, DST_FUM_REC, DST_TD, DST_SAFETY, DST_BLK,
        DST_PTS_ALLOWED, DST_YDS_ALLOWED,
        IDP_TACKLE_SOLO, IDP_TACKLE_AST, IDP_TACKLE_TOTAL, IDP_TACKLE_LOSS,
        IDP_SACK, IDP_INT, IDP_PASS_DEFENDED, IDP_FORCED_FUM, IDP_FUM_REC,
        IDP_TD, IDP_SAFETY,
    }
)

#: Every key the system understands.
ALL_KEYS: Final[frozenset[str]] = SCORING_KEYS | META_KEYS

#: Human labels, used in explanations and CLI tables.
STAT_LABELS: Final[Mapping[str, str]] = {
    PASS_ATT: "Pass attempts",
    PASS_CMP: "Completions",
    PASS_INC: "Incompletions",
    PASS_YD: "Passing yards",
    PASS_TD: "Passing TD",
    PASS_INT: "Interceptions thrown",
    PASS_2PT: "Passing 2PT",
    PASS_SACKED: "Sacks taken",
    PASS_FIRST_DOWN: "Passing first downs",
    RUSH_ATT: "Rush attempts",
    RUSH_YD: "Rushing yards",
    RUSH_TD: "Rushing TD",
    RUSH_2PT: "Rushing 2PT",
    RUSH_FIRST_DOWN: "Rushing first downs",
    REC: "Receptions",
    REC_TGT: "Targets",
    REC_YD: "Receiving yards",
    REC_TD: "Receiving TD",
    REC_2PT: "Receiving 2PT",
    REC_FIRST_DOWN: "Receiving first downs",
    FUM: "Fumbles",
    FUM_LOST: "Fumbles lost",
    FUM_TD: "Fumble return TD",
    MISC_RETURN_YD: "Return yards",
    MISC_RETURN_TD: "Return TD",
    MISC_2PT: "Two-point conversions",
    KICK_XPM: "Extra points made",
    KICK_XPA: "Extra points attempted",
    KICK_XP_MISS: "Extra points missed",
    KICK_FGM: "Field goals made",
    KICK_FGA: "Field goals attempted",
    KICK_FG_MISS: "Field goals missed",
    KICK_FGM_0_19: "FG made 0-19",
    KICK_FGM_20_29: "FG made 20-29",
    KICK_FGM_30_39: "FG made 30-39",
    KICK_FGM_40_49: "FG made 40-49",
    KICK_FGM_50_PLUS: "FG made 50+",
    DST_SACK: "DST sacks",
    DST_INT: "DST interceptions",
    DST_FUM_REC: "DST fumble recoveries",
    DST_TD: "DST touchdowns",
    DST_SAFETY: "DST safeties",
    DST_BLK: "DST blocked kicks",
    DST_PTS_ALLOWED: "Points allowed",
    DST_YDS_ALLOWED: "Yards allowed",
    IDP_TACKLE_SOLO: "Solo tackles",
    IDP_TACKLE_AST: "Assisted tackles",
    IDP_TACKLE_TOTAL: "Total tackles",
    IDP_TACKLE_LOSS: "Tackles for loss",
    IDP_SACK: "IDP sacks",
    IDP_INT: "IDP interceptions",
    IDP_PASS_DEFENDED: "Passes defended",
    IDP_FORCED_FUM: "Forced fumbles",
    IDP_FUM_REC: "IDP fumble recoveries",
    IDP_TD: "IDP touchdowns",
    IDP_SAFETY: "IDP safeties",
    META_GAMES: "Games",
}

#: Typical game-to-game coefficient of variation for stats that per-game
#: bonuses key off.  Used by :mod:`fantasy_ai.analytics.scoring` to estimate how
#: many games a player clears a bonus threshold, given only a season total.
#:
#: These are rough empirical values (weekly volatility is high; passing volume
#: is the most stable, receiving TDs the least).  They only matter for leagues
#: that actually configure per-game bonuses, and they are overridable in YAML.
DEFAULT_PER_GAME_CV: Final[Mapping[str, float]] = {
    PASS_YD: 0.35,
    PASS_TD: 0.75,
    PASS_ATT: 0.25,
    PASS_CMP: 0.28,
    RUSH_YD: 0.55,
    RUSH_ATT: 0.40,
    RUSH_TD: 1.10,
    REC_YD: 0.65,
    REC: 0.45,
    REC_TGT: 0.40,
    REC_TD: 1.20,
}

#: Fallback coefficient of variation for stats missing from the table above.
FALLBACK_PER_GAME_CV: Final[float] = 0.60

#: Default number of games a full-season projection is assumed to cover.
DEFAULT_SEASON_GAMES: Final[int] = 17


class StatLine(dict):
    """A ``{canonical_key: value}`` mapping with convenient defaults.

    Subclasses ``dict`` so it serialises and compares like one, but missing
    stats read as ``0.0`` via :meth:`get_stat` instead of raising.
    """

    def get_stat(self, key: str, default: float = 0.0) -> float:
        value = self.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @property
    def games(self) -> float:
        """Games this projection covers (defaults to a full season)."""
        value = self.get_stat(META_GAMES, 0.0)
        return value if value > 0 else float(DEFAULT_SEASON_GAMES)

    def scoring_stats(self) -> dict[str, float]:
        """Just the stats that can contribute points."""
        return {k: float(v) for k, v in self.items() if k in SCORING_KEYS}

    def merged(self, other: Mapping[str, float]) -> StatLine:
        """Return a copy with ``other`` layered on top."""
        combined = StatLine(self)
        combined.update(other)
        return combined

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> StatLine:
        """Build a stat line, dropping unknown keys and non-numeric values."""
        line = cls()
        for key, value in mapping.items():
            if key not in ALL_KEYS:
                continue
            try:
                line[key] = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        return line


def unknown_keys(keys: Iterable[str]) -> list[str]:
    """Return the subset of ``keys`` that is not part of the canonical vocabulary."""
    return sorted(key for key in keys if key not in ALL_KEYS)


def label(key: str) -> str:
    """Human-readable label for a stat key, falling back to the key itself."""
    return STAT_LABELS.get(key, key)
