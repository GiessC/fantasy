"""External data sources.

Each adapter knows only how to *retrieve and shape* its own provider's data.
Nothing here writes to the database (that is
:mod:`fantasy_ai.services.sync`), computes analytics, or knows about league
scoring.
"""

from __future__ import annotations

from . import csv_import, demo
from .fantasypros import FantasyProsClient, FantasyProsRow, ParseReport
from .http import HTTPCache, HTTPSource, Response, SyncReport
from .sleeper import SleeperClient, SleeperDraft, SleeperPick

__all__ = [
    "FantasyProsClient",
    "FantasyProsRow",
    "HTTPCache",
    "HTTPSource",
    "ParseReport",
    "Response",
    "SleeperClient",
    "SleeperDraft",
    "SleeperPick",
    "SyncReport",
    "csv_import",
    "demo",
]
