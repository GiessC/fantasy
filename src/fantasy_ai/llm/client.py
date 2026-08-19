"""OpenAI-compatible client for a locally served model.

LM Studio exposes an OpenAI-compatible ``/v1`` API, so this speaks that protocol
rather than any model-specific SDK.  The same code therefore works with LM
Studio, Ollama's OpenAI shim, llama.cpp's server, or vLLM -- and swapping the
model behind it is a YAML edit, exactly as LLM_INTEGRATION.md requires.

Structured output is attempted in descending order of strictness, because local
runtimes vary in what they support:

1. ``json_schema`` -- the server constrains generation to the schema.
2. ``json_object`` -- the server guarantees syntactically valid JSON.
3. prompt-only -- we ask for JSON and parse defensively.

A server that rejects a mode with a 4xx is automatically retried one step down,
and the working mode is remembered for the rest of the session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig
from ..errors import LLMError
from ..logging_setup import get_logger

log = get_logger(__name__)

_MODE_ORDER = ["json_schema", "json_object", "prompt_only"]


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class ChatResponse:
    """A model reply plus the metadata worth surfacing to the user."""

    content: str
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    structured_mode: str = "prompt_only"
    latency_seconds: float | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class LLMClient:
    """Minimal chat-completions client."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or LLMConfig()
        self._client = client
        self._owns_client = client is None
        self._working_mode: str | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.config.resolved_api_key()}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- discovery ---------------------------------------------------------

    def list_models(self) -> list[str]:
        """Model ids the server reports, for ``fantasy-ai llm models``."""
        url = f"{self.config.base_url.rstrip('/')}/models"
        try:
            response = self.client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(self._connection_hint(exc)) from exc
        except ValueError as exc:
            raise LLMError(f"{url} did not return JSON.") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]

    def health(self) -> tuple[bool, str]:
        """Whether the server is reachable and the configured model is loaded."""
        try:
            models = self.list_models()
        except LLMError as exc:
            return False, str(exc)
        if not models:
            return True, f"{self.config.base_url} is reachable but reports no models."
        if self.config.model in models:
            return True, f"{self.config.base_url}: model '{self.config.model}' is available."
        return False, (
            f"{self.config.base_url} is reachable, but '{self.config.model}' is not loaded. "
            f"Available: {', '.join(models[:10])}. "
            f"Set llm.model in config/sources.yaml to one of these."
        )

    # -- completion --------------------------------------------------------

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Run a chat completion, degrading structured-output mode as needed."""
        modes = self._modes_to_try(schema)
        last_error: Exception | None = None

        for mode in modes:
            body = self._build_body(
                messages, mode, schema, schema_name, temperature, max_tokens
            )
            try:
                response = self._post(body)
            except LLMError as exc:
                # A 4xx here usually means "this server does not support that
                # response_format"; fall back rather than failing the command.
                if getattr(exc, "retryable_mode", False) and mode != modes[-1]:
                    log.info("Structured mode %r rejected; trying the next mode.", mode)
                    last_error = exc
                    continue
                raise
            self._working_mode = mode
            response.structured_mode = mode
            return response

        raise LLMError(
            f"Every structured-output mode failed ({', '.join(modes)}). Last error: {last_error}"
        )

    def _modes_to_try(self, schema: dict[str, Any] | None) -> list[str]:
        configured = self.config.structured_output
        if schema is None or configured == "off":
            return ["prompt_only"]
        start = self._working_mode or configured
        if start not in _MODE_ORDER:
            start = "json_schema"
        return _MODE_ORDER[_MODE_ORDER.index(start):]

    def _build_body(
        self,
        messages: list[ChatMessage],
        mode: str,
        schema: dict[str, Any] | None,
        schema_name: str,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [message.to_dict() for message in messages],
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if schema is not None and mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif schema is not None and mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        if self.config.extra_body:
            body.update(self.config.extra_body)
        return body

    def _post(self, body: dict[str, Any]) -> ChatResponse:
        import time

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        started = time.monotonic()
        try:
            response = self.client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(self._connection_hint(exc)) from exc
        latency = time.monotonic() - started

        if response.status_code >= 400:
            error = LLMError(
                f"{url} returned HTTP {response.status_code}: {response.text[:400]}"
            )
            # Mark 4xx as worth retrying with a simpler response_format.
            error.retryable_mode = 400 <= response.status_code < 500  # type: ignore[attr-defined]
            raise error

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError(f"{url} returned a non-JSON body: {response.text[:300]}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"{url} returned no choices: {json.dumps(payload)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            raise LLMError("The model returned an empty message.")

        return ChatResponse(
            content=str(content),
            model=payload.get("model"),
            finish_reason=choices[0].get("finish_reason"),
            usage=payload.get("usage") or {},
            latency_seconds=latency,
        )

    def _connection_hint(self, exc: Exception) -> str:
        return (
            f"Could not reach the local model at {self.config.base_url} ({exc}). "
            f"Check that LM Studio is running with its server started "
            f"(Developer tab -> Start Server), that the port matches "
            f"llm.base_url in config/sources.yaml, and that a model is loaded. "
            f"'fantasy-ai llm status' re-runs this check."
        )
