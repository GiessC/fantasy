"""Loading, merging, and validating YAML configuration.

Two files, discovered relative to the project root:

``config/league.yaml``
    ``league:`` (required) plus optional ``analytics:`` and ``simulation:``
    blocks, which sit here because they are tuned alongside league rules.

``config/sources.yaml``
    ``sources:``, ``llm:``, ``paths:``, ``log_level:`` -- tool settings.

Either file may be absent, in which case the matching ``*.example.yaml`` is used
so a fresh checkout runs without setup.  Blocks may also appear in either file;
the loader merges them and reports conflicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from ..logging_setup import get_logger
from .app import AppConfig
from .league import LeagueConfig

log = get_logger(__name__)

DEFAULT_LEAGUE_FILE = "config/league.yaml"
DEFAULT_SOURCES_FILE = "config/sources.yaml"

#: Blocks that belong to :class:`AppConfig` regardless of which file they are in.
_APP_BLOCKS = ("paths", "sources", "llm", "analytics", "simulation", "log_level")

#: ``FANTASY_AI_`` environment overrides, mapped to a dotted path in AppConfig.
_ENV_OVERRIDES: dict[str, str] = {
    "FANTASY_AI_DB": "paths.database",
    "FANTASY_AI_DATA_DIR": "paths.data_dir",
    "FANTASY_AI_LOG_LEVEL": "log_level",
    "FANTASY_AI_LLM_BASE_URL": "llm.base_url",
    "FANTASY_AI_LLM_MODEL": "llm.model",
    "FANTASY_AI_LLM_ENABLED": "llm.enabled",
    "FANTASY_AI_SIM_ITERATIONS": "simulation.iterations",
    "FANTASY_AI_SIM_SEED": "simulation.seed",
}


@dataclass(slots=True)
class Settings:
    """Fully-resolved configuration plus provenance for error messages."""

    league: LeagueConfig
    app: AppConfig
    root: Path
    league_path: Path
    sources_path: Path

    @property
    def database_path(self) -> Path:
        return self.app.paths.database

    def describe_sources(self) -> dict[str, str]:
        return {
            "league_config": str(self.league_path),
            "sources_config": str(self.sources_path),
            "database": str(self.app.paths.database),
        }


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward for a directory containing ``pyproject.toml`` or ``config/``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / "config").is_dir():
            return candidate
    return current


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def _resolve_file(root: Path, explicit: Path | None, default_rel: str) -> Path:
    """Pick the config file: explicit > real file > bundled example."""
    if explicit is not None:
        path = explicit if explicit.is_absolute() else root / explicit
        if not path.exists():
            raise ConfigError(f"Configuration file not found: {path}")
        return path

    primary = root / default_rel
    if primary.exists():
        return primary

    example = primary.with_name(primary.stem + ".example" + primary.suffix)
    if example.exists():
        log.debug("Using bundled example config %s", example)
        return example

    raise ConfigError(
        f"No configuration found. Expected {primary} or {example}. "
        f"Copy the example file and edit it."
    )


def _merge_blocks(
    league_doc: dict[str, Any],
    sources_doc: dict[str, Any],
    league_path: Path,
    sources_path: Path,
) -> dict[str, Any]:
    """Collect app-level blocks from both documents, rejecting duplicates."""
    merged: dict[str, Any] = {}
    for block in _APP_BLOCKS:
        in_league = block in league_doc
        in_sources = block in sources_doc
        if in_league and in_sources:
            raise ConfigError(
                f"Block '{block}:' is defined in both {league_path.name} and "
                f"{sources_path.name}. Keep it in exactly one file."
            )
        if in_league:
            merged[block] = league_doc[block]
        elif in_sources:
            merged[block] = sources_doc[block]
    return merged


def _coerce_env_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _apply_env_overrides(payload: dict[str, Any], env: dict[str, str]) -> list[str]:
    """Apply ``FANTASY_AI_*`` overrides in place. Returns what was applied."""
    applied: list[str] = []
    for var, dotted in _ENV_OVERRIDES.items():
        if var not in env:
            continue
        value = _coerce_env_value(env[var])
        target = payload
        parts = dotted.split(".")
        for part in parts[:-1]:
            existing = target.get(part)
            if not isinstance(existing, dict):
                existing = {}
                target[part] = existing
            target = existing
        target[parts[-1]] = value
        applied.append(f"{var} -> {dotted}")
    return applied


def format_validation_error(exc: ValidationError, *, path: Path) -> str:
    """Turn a pydantic error into a message that points at the YAML."""
    lines = [f"{path} failed validation ({exc.error_count()} problem(s)):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = error["msg"]
        detail = ""
        if "input" in error and error["input"] is not None:
            rendered = repr(error["input"])
            if len(rendered) <= 80:
                detail = f" (got {rendered})"
        lines.append(f"  - {location}: {message}{detail}")
    return "\n".join(lines)


def load_settings(
    *,
    league_path: Path | None = None,
    sources_path: Path | None = None,
    root: Path | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load, merge, and validate configuration.

    ``overrides`` is a nested mapping applied on top of the merged app payload,
    used by CLI flags such as ``--iterations``.
    """
    project_root = (root or find_project_root()).resolve()
    resolved_league = _resolve_file(project_root, league_path, DEFAULT_LEAGUE_FILE)
    resolved_sources = _resolve_file(project_root, sources_path, DEFAULT_SOURCES_FILE)

    league_doc = _read_yaml(resolved_league)
    sources_doc = _read_yaml(resolved_sources) if resolved_sources != resolved_league else {}

    if "league" not in league_doc:
        raise ConfigError(
            f"{resolved_league} has no top-level 'league:' block. "
            f"See config/league.example.yaml for the expected shape."
        )
    if "league" in sources_doc:
        raise ConfigError(
            f"{resolved_sources} must not define a 'league:' block; that belongs in "
            f"{resolved_league.name}."
        )

    unknown_top_level = (
        set(league_doc) | set(sources_doc)
    ) - ({"league"} | set(_APP_BLOCKS))
    if unknown_top_level:
        raise ConfigError(
            f"Unknown top-level configuration block(s): {sorted(unknown_top_level)}. "
            f"Expected any of: league, {', '.join(_APP_BLOCKS)}."
        )

    app_payload = _merge_blocks(league_doc, sources_doc, resolved_league, resolved_sources)
    applied = _apply_env_overrides(app_payload, dict(env if env is not None else os.environ))
    if applied:
        log.debug("Applied environment overrides: %s", ", ".join(applied))
    if overrides:
        _deep_update(app_payload, overrides)

    try:
        league = LeagueConfig.model_validate(league_doc["league"])
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, path=resolved_league)) from exc

    try:
        app = AppConfig.model_validate(app_payload)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, path=resolved_sources)) from exc

    return Settings(
        league=league,
        app=app.resolve_paths(project_root),
        root=project_root,
        league_path=resolved_league,
        sources_path=resolved_sources,
    )


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def validate_settings(settings: Settings) -> list[str]:
    """Non-fatal consistency checks, returned as human-readable warnings."""
    warnings: list[str] = []
    league = settings.league
    app = settings.app

    if league.draft.position is None:
        warnings.append(
            "league.draft.position is not set; draft-order analytics will assume slot 1 "
            "until you pass --position or run 'fantasy-ai draft start --position N'."
        )
    if league.draft.rounds is not None and league.draft.rounds > league.roster_size:
        warnings.append(
            f"league.draft.rounds ({league.draft.rounds}) exceeds draftable roster spots "
            f"({league.roster_size} = {league.starters_per_team} starters + "
            f"{league.roster.get('BENCH', 0)} bench). Later rounds will have nowhere to go "
            f"unless you cut players; check BENCH."
        )
    if league.draft.type == "auction":
        warnings.append(
            "Auction drafts are configured but the simulator currently models snake "
            "and linear orders only; auction values are not simulated."
        )
    if league.roster.get("K", 0) == 0 and league.roster.get("DST", 0) == 0:
        warnings.append("No K or DST starting slots; those positions will be excluded from VOR.")
    if league.is_superflex:
        warnings.append(
            "Superflex detected: QB replacement level is computed from the full "
            "QB starter demand, which is much deeper than a 1-QB league."
        )
    if app.sources.fantasypros.enabled and app.sources.fantasypros.api_key() is None:
        warnings.append(
            f"FantasyPros is enabled but ${app.sources.fantasypros.api_key_env} is not set. "
            f"Set it, or use 'fantasy-ai sync projections --from-csv' / 'sync demo'."
        )
    if league.scoring.compile().rates.get("rec", 0.0) == 0 and league.roster.get("TE", 0) > 0:
        warnings.append(
            "Scoring is non-PPR; TE and pass-catching RB values shift substantially "
            "relative to the PPR consensus rankings most sources publish."
        )
    idp_slots = [
        slot for slot in league.starting_slots
        if any(p in {"DL", "LB", "DB", "DE", "DT", "CB", "S", "IDP"} for p in slot.eligible_positions)
    ]
    if idp_slots and not any(
        value for key, value in league.scoring.idp.rates().items()
    ):
        warnings.append(
            "IDP starting slots are configured but league.scoring.idp is all zeroes."
        )
    return warnings
