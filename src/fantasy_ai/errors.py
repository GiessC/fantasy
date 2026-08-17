"""Exception hierarchy.

Every error the user can plausibly cause (bad config, missing data, unreachable
service) is a :class:`FantasyAIError` so the CLI can print a clean message
instead of a traceback.
"""

from __future__ import annotations


class FantasyAIError(Exception):
    """Base class for all expected application errors."""

    exit_code: int = 1


class ConfigError(FantasyAIError):
    """Raised when configuration is missing, malformed, or internally inconsistent."""

    exit_code = 2


class DatabaseError(FantasyAIError):
    """Raised for schema/migration problems and integrity violations."""

    exit_code = 3


class SourceError(FantasyAIError):
    """Base class for external data-source failures."""

    exit_code = 4

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.source = source


class SourceUnavailableError(SourceError):
    """The source could not be reached, or returned a retryable failure."""


class SourceAuthError(SourceError):
    """The source rejected our credentials."""


class SourceResponseError(SourceError):
    """The source responded, but the payload was not in a shape we understand."""


class DataMissingError(FantasyAIError):
    """A command needs data that has not been synced yet."""

    exit_code = 5


class DraftStateError(FantasyAIError):
    """An illegal draft operation (duplicate pick, undo with no picks, ...)."""

    exit_code = 6


class LLMError(FantasyAIError):
    """The local model was unreachable or produced unusable output."""

    exit_code = 7
