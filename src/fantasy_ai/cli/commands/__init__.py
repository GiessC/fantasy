"""CLI command groups."""

from __future__ import annotations

from .analyze import analyze_app
from .config_db import data_app, db_app, validate_config
from .draft import draft_app
from .llm import ask, llm_app, recommend
from .simulate import simulate_app
from .sync import sync_app

__all__ = [
    "analyze_app",
    "ask",
    "data_app",
    "db_app",
    "draft_app",
    "llm_app",
    "recommend",
    "simulate_app",
    "sync_app",
    "validate_config",
]
