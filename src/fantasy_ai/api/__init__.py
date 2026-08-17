"""HTTP API for the local web UI.

Consumes ``fantasy_ai.services`` -- the same entry points the CLI uses -- so the
two surfaces cannot drift apart analytically.
"""

from __future__ import annotations

from .app import WEB_DIST, create_app
from .state import AppState, CachedBoard

__all__ = ["WEB_DIST", "AppState", "CachedBoard", "create_app"]
