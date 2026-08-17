"""Mapping source stat dialects onto the canonical vocabulary.

Sources disagree about spelling (``pass_yds`` / ``pass_yd`` / ``PASS YDS`` /
``passing_yards``) but agree about meaning.  Rather than a table per source,
field names are reduced to a comparison key (lowercase, alphanumeric only) and
looked up in one broad alias table.  A source with a genuinely unusual name gets
an entry in :data:`SOURCE_OVERRIDES`.

Anything unrecognised is dropped and counted, so a source adding a field never
silently corrupts scoring -- ``fantasy-ai sync --verbose`` reports the misses.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from .. import stats as S
from ..logging_setup import get_logger

log = get_logger(__name__)

_KEY_CLEAN = re.compile(r"[^a-z0-9]")


def _key(name: str) -> str:
    return _KEY_CLEAN.sub("", name.strip().lower())


def _aliases(canonical: str, *names: str) -> dict[str, str]:
    return {_key(name): canonical for name in names}


#: Field-name aliases, keyed by cleaned name.
ALIASES: dict[str, str] = {}
for _canonical, _names in {
    S.PASS_ATT: ("pass_att", "passatt", "pass attempts", "passing attempts", "att", "patt"),
    S.PASS_CMP: ("pass_cmp", "cmp", "completions", "pass completions", "pass_comp"),
    S.PASS_INC: ("pass_inc", "incompletions", "pass_incomplete"),
    S.PASS_YD: ("pass_yd", "pass_yds", "passyds", "passing yards", "pass yards", "pyds", "py"),
    S.PASS_TD: ("pass_td", "pass_tds", "passtds", "passing tds", "passing touchdowns", "ptd"),
    S.PASS_INT: ("pass_int", "ints", "int", "interceptions", "passing interceptions", "pint"),
    S.PASS_2PT: ("pass_2pt", "pass2pt", "passing 2pt", "two point pass", "pass_two_pt"),
    S.PASS_SACKED: ("pass_sacked", "sacked", "times sacked", "sacks taken"),
    S.PASS_FIRST_DOWN: ("pass_fd", "pass_first_down", "passing first downs"),
    S.RUSH_ATT: ("rush_att", "rushatt", "rushing attempts", "carries", "ratt", "car"),
    S.RUSH_YD: ("rush_yd", "rush_yds", "rushyds", "rushing yards", "rush yards", "ryds", "ry"),
    S.RUSH_TD: ("rush_td", "rush_tds", "rushtds", "rushing tds", "rushing touchdowns", "rtd"),
    S.RUSH_2PT: ("rush_2pt", "rush2pt", "rushing 2pt", "rush_two_pt"),
    S.RUSH_FIRST_DOWN: ("rush_fd", "rush_first_down", "rushing first downs"),
    S.REC: ("rec", "receptions", "catches", "recs", "reception"),
    S.REC_TGT: ("rec_tgt", "targets", "tgt", "tgts", "rec_targets"),
    S.REC_YD: ("rec_yd", "rec_yds", "recyds", "receiving yards", "rec yards", "reyds"),
    S.REC_TD: ("rec_td", "rec_tds", "rectds", "receiving tds", "receiving touchdowns", "retd"),
    S.REC_2PT: ("rec_2pt", "rec2pt", "receiving 2pt", "rec_two_pt"),
    S.REC_FIRST_DOWN: ("rec_fd", "rec_first_down", "receiving first downs"),
    S.FUM: ("fum", "fumbles", "fmb", "total fumbles"),
    S.FUM_LOST: ("fum_lost", "fl", "fumbles lost", "fumbleslost", "fumlost", "lost fumbles"),
    S.FUM_TD: ("fum_td", "fumble return td", "fumble_rec_td"),
    S.MISC_RETURN_YD: ("return_yd", "return_yds", "kick return yards", "punt return yards",
                       "returnyards"),
    S.MISC_RETURN_TD: ("return_td", "return_tds", "kick return td", "punt return td", "krtd",
                       "prtd"),
    S.MISC_2PT: ("two_pt", "2pt", "two point conversions", "twoptconv", "2pc"),
    S.KICK_XPM: ("xpm", "xp_made", "extra points made", "extra point", "pat made", "patm"),
    S.KICK_XPA: ("xpa", "xp_att", "extra points attempted", "pat att"),
    S.KICK_XP_MISS: ("xp_miss", "extra points missed", "xpmiss"),
    S.KICK_FGM: ("fgm", "fg_made", "field goals made", "fg", "fgmade"),
    S.KICK_FGA: ("fga", "fg_att", "field goals attempted", "fgatt"),
    S.KICK_FG_MISS: ("fg_miss", "field goals missed", "fgmiss"),
    S.KICK_FGM_0_19: ("fgm019", "fg_0_19", "fg made 0 19", "fg119", "fgm119"),
    S.KICK_FGM_20_29: ("fgm2029", "fg_20_29", "fg made 20 29"),
    S.KICK_FGM_30_39: ("fgm3039", "fg_30_39", "fg made 30 39"),
    S.KICK_FGM_40_49: ("fgm4049", "fg_40_49", "fg made 40 49"),
    S.KICK_FGM_50_PLUS: ("fgm50", "fg_50_plus", "fg made 50", "fgm50plus", "fg50"),
    S.DST_SACK: ("dst_sack", "sack", "sacks", "def sacks", "sk"),
    S.DST_INT: ("dst_int", "def int", "defensive interceptions", "int_def"),
    S.DST_FUM_REC: ("dst_fum_rec", "fumble recovery", "fumbles recovered", "fr", "def fr"),
    S.DST_TD: ("dst_td", "def td", "defensive touchdowns", "dst touchdowns", "deftd"),
    S.DST_SAFETY: ("dst_safety", "safety", "safeties", "sfty"),
    S.DST_BLK: ("dst_blk", "blocked kicks", "blk", "blocks"),
    S.DST_PTS_ALLOWED: ("dst_pts_allowed", "points allowed", "pa", "pts allowed", "ptsallow"),
    S.DST_YDS_ALLOWED: ("dst_yds_allowed", "yards allowed", "yds allowed", "ydsagn", "ya"),
    S.IDP_TACKLE_SOLO: ("idp_tkl_solo", "solo tackles", "tackles solo", "tkl_solo", "solo"),
    S.IDP_TACKLE_AST: ("idp_tkl_ast", "assisted tackles", "tackles assist", "tkl_ast", "ast"),
    S.IDP_TACKLE_TOTAL: ("idp_tkl", "total tackles", "tackles", "tkl"),
    S.IDP_TACKLE_LOSS: ("idp_tkl_loss", "tackles for loss", "tfl"),
    S.IDP_SACK: ("idp_sack", "idp sacks"),
    S.IDP_INT: ("idp_int", "idp interceptions"),
    S.IDP_PASS_DEFENDED: ("idp_pass_def", "passes defended", "pd"),
    S.IDP_FORCED_FUM: ("idp_ff", "forced fumbles", "ff"),
    S.IDP_FUM_REC: ("idp_fum_rec", "idp fumble recoveries"),
    S.IDP_TD: ("idp_td", "idp touchdowns"),
    S.IDP_SAFETY: ("idp_safety",),
    S.META_GAMES: ("games", "g", "gp", "games played", "gms"),
}.items():
    ALIASES.update(_aliases(_canonical, *_names))

# Canonical keys always map to themselves.
ALIASES.update({_key(name): name for name in S.ALL_KEYS})

#: Per-source resolutions where a name is genuinely ambiguous across sources.
#: ``sacks`` means a defense's sacks for a DST row but sacks *taken* on a QB row,
#: so ambiguity that depends on position is handled in :func:`map_stats`.
SOURCE_OVERRIDES: dict[str, dict[str, str]] = {
    "fantasypros": {
        _key("fpts"): "",          # league-specific; we recompute from stats
        _key("fantasy points"): "",
        _key("rank"): "",
        _key("tier"): "",
    },
    "sleeper": {
        _key("pts_ppr"): "",
        _key("pts_half_ppr"): "",
        _key("pts_std"): "",
        _key("gp"): S.META_GAMES,
    },
}

#: Fields that mean different things depending on the row's position.
_POSITION_SENSITIVE: dict[str, dict[str, str]] = {
    _key("sacks"): {"DST": S.DST_SACK, "QB": S.PASS_SACKED, "_default": S.IDP_SACK},
    _key("sack"): {"DST": S.DST_SACK, "QB": S.PASS_SACKED, "_default": S.IDP_SACK},
    _key("int"): {"DST": S.DST_INT, "QB": S.PASS_INT, "_default": S.IDP_INT},
    _key("ints"): {"DST": S.DST_INT, "QB": S.PASS_INT, "_default": S.IDP_INT},
    _key("td"): {"DST": S.DST_TD, "_default": ""},
    _key("tds"): {"DST": S.DST_TD, "_default": ""},
    _key("ff"): {"_default": S.IDP_FORCED_FUM},
    _key("fr"): {"DST": S.DST_FUM_REC, "_default": S.IDP_FUM_REC},
}


def resolve_field(name: str, *, source: str | None = None, position: str | None = None) -> str | None:
    """Canonical key for a source field name, or ``None`` if unrecognised.

    An empty-string mapping means "known but deliberately ignored" (a source's
    own fantasy-point total, for instance -- we recompute those from the league
    config and never trust a source's scoring).
    """
    cleaned = _key(name)
    if not cleaned:
        return None

    if source and (override := SOURCE_OVERRIDES.get(source, {})).get(cleaned) is not None:
        mapped = override[cleaned]
        return mapped or None

    if cleaned in _POSITION_SENSITIVE:
        table = _POSITION_SENSITIVE[cleaned]
        mapped = table.get(position or "", table.get("_default", ""))
        return mapped or None

    return ALIASES.get(cleaned)


def map_stats(
    raw: Mapping[str, object],
    *,
    source: str | None = None,
    position: str | None = None,
    misses: Counter | None = None,
) -> S.StatLine:
    """Convert a source's raw stat mapping into a canonical :class:`StatLine`.

    Non-numeric values and unmapped fields are dropped.  When ``misses`` is
    supplied, unmapped field names are counted into it for reporting.
    """
    line = S.StatLine()
    for field_name, value in raw.items():
        canonical = resolve_field(field_name, source=source, position=position)
        if canonical is None:
            if misses is not None:
                misses[str(field_name)] += 1
            continue
        number = _to_float(value)
        if number is None:
            continue
        # Several aliases can land on the same canonical key (``rec_yd`` from
        # both "REC YDS" and "receiving yards"); last non-zero value wins.
        if canonical in line and number == 0:
            continue
        line[canonical] = number
    return _derive(line)


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "N/A", "NA", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _derive(line: S.StatLine) -> S.StatLine:
    """Fill in stats implied by others, so scoring rules always have an input."""
    # Incompletions from attempts and completions.
    if S.PASS_INC not in line and S.PASS_ATT in line and S.PASS_CMP in line:
        line[S.PASS_INC] = max(0.0, line[S.PASS_ATT] - line[S.PASS_CMP])
    # Missed kicks from attempts and makes.
    if S.KICK_FG_MISS not in line and S.KICK_FGA in line and S.KICK_FGM in line:
        line[S.KICK_FG_MISS] = max(0.0, line[S.KICK_FGA] - line[S.KICK_FGM])
    if S.KICK_XP_MISS not in line and S.KICK_XPA in line and S.KICK_XPM in line:
        line[S.KICK_XP_MISS] = max(0.0, line[S.KICK_XPA] - line[S.KICK_XPM])
    # Total tackles from the solo/assist split, and vice versa where possible.
    if S.IDP_TACKLE_TOTAL not in line and (
        S.IDP_TACKLE_SOLO in line or S.IDP_TACKLE_AST in line
    ):
        line[S.IDP_TACKLE_TOTAL] = line.get_stat(S.IDP_TACKLE_SOLO) + line.get_stat(
            S.IDP_TACKLE_AST
        )
    # If a source gave bucketed FGs but no total, sum them.
    if S.KICK_FGM not in line:
        buckets = [line[key] for key, _, _ in S.FG_DISTANCE_BUCKETS if key in line]
        if buckets:
            line[S.KICK_FGM] = sum(buckets)
    return line


def describe_misses(misses: Counter, limit: int = 15) -> str:
    """Readable summary of unmapped fields, for sync output."""
    if not misses:
        return "none"
    top = misses.most_common(limit)
    rendered = ", ".join(f"{name} (x{count})" for name, count in top)
    if len(misses) > limit:
        rendered += f", +{len(misses) - limit} more"
    return rendered
