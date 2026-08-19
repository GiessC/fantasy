"""Shared HTTP client for source adapters.

Provides the things DATA_SOURCES.md asks every client to have -- retries,
rate-limit awareness, timeouts, structured errors, caching, retrieval
timestamps -- once, so the FantasyPros and Sleeper adapters only contain
knowledge about *their* API.

The on-disk cache matters more than usual here: "do not make the entire
application dependent on live API availability after data has been successfully
cached" is an explicit requirement, and a draft is exactly when you do not want
to discover that an API is down.  Cached responses are keyed by method, URL, and
query, stored as JSON with their retrieval time, and served past their TTL when
the network fails (a stale hit is logged, never silent).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import HTTPConfig
from ..errors import SourceAuthError, SourceResponseError, SourceUnavailableError
from ..logging_setup import get_logger

log = get_logger(__name__)

#: Status codes worth retrying: transient server problems and rate limits.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class Response:
    """A fetched payload plus its provenance."""

    data: Any
    url: str
    retrieved_at: datetime
    from_cache: bool = False
    stale: bool = False
    status_code: int | None = None

    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.retrieved_at).total_seconds()


@dataclass(slots=True)
class HTTPCache:
    """A tiny JSON file cache."""

    directory: Path
    ttl_seconds: int = 6 * 3600

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    @staticmethod
    def key_for(method: str, url: str, params: dict[str, Any] | None) -> str:
        payload = json.dumps(
            {"method": method.upper(), "url": url, "params": params or {}}, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]

    def read(self, key: str) -> tuple[Any, datetime] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.debug("Discarding unreadable cache entry %s", path)
            return None
        stamp = payload.get("retrieved_at")
        try:
            retrieved = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return None
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=UTC)
        return payload.get("data"), retrieved

    def is_fresh(self, retrieved: datetime) -> bool:
        if self.ttl_seconds <= 0:
            return False
        age = (datetime.now(UTC) - retrieved).total_seconds()
        return age < self.ttl_seconds

    def write(self, key: str, data: Any) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(
                json.dumps(
                    {
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "data": data,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("Could not write HTTP cache entry: %s", exc)

    def clear(self) -> int:
        if not self.directory.exists():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                pass
        return removed


class HTTPSource:
    """Base class for HTTP-backed source adapters."""

    #: Name recorded on every database record this source produces.
    source_name: str = "http"

    def __init__(
        self,
        base_url: str,
        config: HTTPConfig | None = None,
        *,
        cache_dir: Path | None = None,
        client: httpx.Client | None = None,
        cache_ttl_seconds: int | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config or HTTPConfig()
        self.cache = (
            HTTPCache(
                directory=cache_dir,
                ttl_seconds=(
                    cache_ttl_seconds
                    if cache_ttl_seconds is not None
                    else self.config.cache_ttl_seconds
                ),
            )
            if cache_dir is not None
            else None
        )
        self._client = client
        self._owns_client = client is None
        self._headers = {"User-Agent": self.config.user_agent, **(default_headers or {})}
        self._last_request_at = 0.0

    # -- client lifecycle --------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.timeout_seconds,
                headers=self._headers,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> HTTPSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- requests ----------------------------------------------------------

    def _respect_rate_limit(self) -> None:
        interval = self.config.rate_limit_interval
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        force_refresh: bool = False,
        cache_ttl_seconds: int | None = None,
    ) -> Response:
        """GET a JSON payload, with caching, retries, and structured errors."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        cache_key = HTTPCache.key_for("GET", url, clean_params)
        cached = None
        if self.cache is not None and use_cache:
            cached = self.cache.read(cache_key)
            if cached is not None and not force_refresh:
                data, retrieved = cached
                ttl = (
                    cache_ttl_seconds
                    if cache_ttl_seconds is not None
                    else self.cache.ttl_seconds
                )
                age = (datetime.now(UTC) - retrieved).total_seconds()
                if ttl > 0 and age < ttl:
                    log.debug("Cache hit (%.0fs old): %s", age, url)
                    return Response(
                        data=data, url=url, retrieved_at=retrieved, from_cache=True
                    )

        try:
            payload = self._request_with_retries(url, clean_params, headers)
        except SourceUnavailableError:
            if cached is not None:
                data, retrieved = cached
                log.warning(
                    "%s unreachable; serving cached response from %s",
                    self.source_name, retrieved.isoformat(),
                )
                return Response(
                    data=data, url=url, retrieved_at=retrieved, from_cache=True, stale=True
                )
            raise

        if self.cache is not None and use_cache:
            self.cache.write(cache_key, payload)
        return Response(data=payload, url=url, retrieved_at=datetime.now(UTC))

    def _request_with_retries(
        self, url: str, params: dict[str, Any], headers: dict[str, str] | None
    ) -> Any:
        delay = self.config.backoff_seconds
        last_error: str = "unknown error"

        for attempt in range(self.config.max_retries + 1):
            self._respect_rate_limit()
            try:
                response = self.client.get(url, params=params, headers=headers)
                self._last_request_at = time.monotonic()
            except httpx.TimeoutException as exc:
                last_error = f"timed out after {self.config.timeout_seconds}s ({exc})"
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code in {401, 403}:
                    raise SourceAuthError(
                        f"{self.source_name} rejected the request "
                        f"({response.status_code}). Check the API key configured for "
                        f"this source in config/sources.yaml; 'fantasy-ai validate-config' "
                        f"reports which of api_key / api_key_file / the environment it "
                        f"is reading.",
                        source=self.source_name,
                    )
                if response.status_code == 404:
                    raise SourceResponseError(
                        f"{self.source_name} returned 404 for {url}. The endpoint path "
                        f"may have changed; endpoints are configurable under "
                        f"sources.{self.source_name}.endpoints.",
                        source=self.source_name,
                    )
                if response.status_code in RETRYABLE_STATUS:
                    retry_after = _retry_after_seconds(response)
                    last_error = f"HTTP {response.status_code}"
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                elif response.status_code >= 400:
                    raise SourceResponseError(
                        f"{self.source_name} returned HTTP {response.status_code} for "
                        f"{url}: {response.text[:300]}",
                        source=self.source_name,
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise SourceResponseError(
                            f"{self.source_name} returned a non-JSON body for {url}: "
                            f"{response.text[:300]}",
                            source=self.source_name,
                        ) from exc

            if attempt < self.config.max_retries:
                log.warning(
                    "%s request failed (%s); retrying in %.1fs (attempt %d/%d)",
                    self.source_name, last_error, delay, attempt + 1, self.config.max_retries,
                )
                time.sleep(delay)
                delay = min(
                    delay * self.config.backoff_multiplier, self.config.max_backoff_seconds
                )

        raise SourceUnavailableError(
            f"{self.source_name} did not respond successfully after "
            f"{self.config.max_retries + 1} attempt(s): {last_error}",
            source=self.source_name,
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(slots=True)
class SyncReport:
    """What a sync actually did, for CLI output and the sync_runs table."""

    dataset: str
    source: str
    season: int | None = None
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    from_cache: bool = False
    stale: bool = False
    retrieved_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)
    unmapped_fields: dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        origin = ""
        if self.stale:
            origin = " (STALE CACHE -- source unreachable)"
        elif self.from_cache:
            origin = " (cached)"
        base = (
            f"{self.dataset} from {self.source}: {self.fetched} fetched, "
            f"{self.written} new snapshot(s), {self.skipped} unchanged{origin}"
        )
        return base
