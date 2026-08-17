"""Logging configuration.

A single ``fantasy_ai`` logger tree.  The CLI installs a Rich handler; library
consumers get the standard library default (no handler) so importing the package
never hijacks their logging.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_configured = False


def resolve_level(level: str | int | None) -> int:
    """Turn a user-supplied level (``"debug"``, ``10``, ``None``) into a level int."""
    if level is None:
        env = os.environ.get("FANTASY_AI_LOG_LEVEL")
        return resolve_level(env) if env else logging.INFO
    if isinstance(level, int):
        return level
    key = level.strip().lower()
    if key not in _LEVELS:
        raise ValueError(f"Unknown log level: {level!r}. Expected one of {sorted(_LEVELS)}.")
    return _LEVELS[key]


def configure_logging(level: str | int | None = None, *, rich: bool = True) -> logging.Logger:
    """Attach a handler to the ``fantasy_ai`` logger. Idempotent."""
    global _configured
    logger = logging.getLogger("fantasy_ai")
    logger.setLevel(resolve_level(level))

    if _configured:
        return logger

    handler: logging.Handler
    if rich:
        try:
            from rich.logging import RichHandler

            handler = RichHandler(rich_tracebacks=True, show_path=False, show_time=False)
            handler.setFormatter(logging.Formatter("%(message)s"))
        except ImportError:  # pragma: no cover - rich is a hard dependency
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    logger.addHandler(handler)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``fantasy_ai`` logger."""
    if name.startswith("fantasy_ai"):
        return logging.getLogger(name)
    return logging.getLogger(f"fantasy_ai.{name}")


def log_context(**fields: Any) -> str:
    """Render key/value pairs for log messages in a stable order."""
    return " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
