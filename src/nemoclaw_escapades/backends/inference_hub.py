"""Inference Hub backend.

Calls an OpenAI-compatible ``/chat/completions`` endpoint (configured via
``INFERENCE_HUB_BASE_URL``).  This is the first — and in M1 the only —
concrete ``BackendBase`` implementation.

Key behaviours:

- **HTTP client** — ``httpx.AsyncClient`` with a configurable base URL
  and Bearer-token auth.  Inside an OpenShell sandbox the base URL
  resolves to ``inference.local``; the gateway proxy transparently
  routes to the real endpoint and attaches the API key.
- **Retry via tenacity** — 429 (rate limit) and 5xx (server error)
  responses are retried up to ``max_retries`` times with exponential
  backoff + jitter.  The ``Retry-After`` header is honoured when
  present.  Auth failures (401/403) are never retried.
- **Timeout enforcement** — a configurable overall timeout (default 60 s)
  and a 10 s connect timeout.
- **Structured error reporting** — every failure path raises
  ``InferenceError`` with an ``ErrorCategory`` so the orchestrator can
  map it to the right user-facing message.
- **finish_reason capture** — the response includes the model's
  ``finish_reason`` so the transcript-repair layer can detect
  truncation (``"length"``) and trigger continuation retries.
"""

from __future__ import annotations

import time

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import InferenceConfig
from nemoclaw_escapades.models.types import (
    ErrorCategory,
    InferenceError,
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
    ToolCall,
)
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("inference_hub")


class _RetryableError(Exception):
    """Internal sentinel — tells tenacity this call should be retried.

    Raised by ``_send_request`` for transient HTTP failures (429, 5xx)
    and timeouts.  Never escapes the class boundary; ``complete``
    converts it to ``InferenceError`` if all retries are exhausted.

    Args:
        message:     Human-readable description of the failure.
        category:    ``ErrorCategory`` for structured logging and
                     user-facing error mapping.
        raw:         Raw response body or exception string, preserved
                     for debugging.
        retry_after: Seconds to wait before the next attempt, parsed
                     from the ``Retry-After`` header.  ``None`` means
                     fall back to exponential backoff.
    """

    def __init__(
        self,
        message: str,
        category: ErrorCategory,
        raw: object = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.raw = raw
        self.retry_after = retry_after


class InferenceHubBackend(BackendBase):
    """Calls NVIDIA Inference Hub via its OpenAI-compatible API.

    Retry logic is delegated to tenacity.  A single HTTP attempt lives in
    ``_send_request``; ``complete`` wraps it in an ``AsyncRetrying`` loop.
    """

    def __init__(self, config: InferenceConfig) -> None:
        """Initialise the HTTP client and retry strategy.

        Args:
            config: Inference settings (base URL, API key, model name,
                    timeout, max retries).  In an OpenShell sandbox the
                    base URL typically resolves to ``inference.local``.
        """
        self._config = config

        # In the OpenShell sandbox the app talks to inference.local, which
        # is a proxy that adds the real API key before forwarding upstream.
        # The app never sees the credential — api_key is empty and the
        # Authorization header is omitted.  Locally (no proxy) the app
        # sends the key from .env directly.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=10.0),
        )
        self._default_wait = wait_exponential_jitter(initial=2, max=30, jitter=2)

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Send a chat-completion request with automatic retries.

        Builds the OpenAI-format payload from *request*, then delegates
        to ``_send_request`` inside a tenacity ``AsyncRetrying`` loop.

        Args:
            request: The inference request containing the message list,
                     model name, temperature, and max token limit.

        Returns:
            An ``InferenceResponse`` with the assistant's content,
            token usage, latency, finish reason, and raw API response.

        Raises:
            InferenceError: If the call fails after all retries, or if
                a non-retryable error (e.g. 401 auth failure) occurs on
                the first attempt.
        """
        payload: dict[str, object] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = [t.to_dict() for t in request.tools]

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RetryableError),
                wait=self._wait_for_retry,
                stop=stop_after_attempt(self._config.max_retries),
                before_sleep=self._log_retry,
                reraise=True,
            ):
                with attempt:
                    return await self._send_request(payload, request.model, request.request_id)
        except _RetryableError as exc:
            raise InferenceError(str(exc), category=exc.category, raw=exc.raw) from exc

        raise InferenceError("All retries exhausted", category=ErrorCategory.UNKNOWN)

    async def close(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and release its
        connection pool."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Single-attempt HTTP call
    # ------------------------------------------------------------------

    async def _send_request(
        self, payload: dict[str, object], model: str, request_id: str = ""
    ) -> InferenceResponse:
        """Execute one HTTP POST to ``/chat/completions``.

        This method represents a *single* network attempt.  Tenacity
        calls it repeatedly until it succeeds or the retry budget is
        exhausted.

        Args:
            payload:    JSON body for the OpenAI-compatible completions
                        endpoint (model, messages, temperature, max_tokens).
            model:      Model identifier, included in log lines for
                        traceability.
            request_id: Correlation ID for structured logging.

        Returns:
            A parsed ``InferenceResponse`` on HTTP 200.

        Raises:
            _RetryableError: On 429, 5xx, or ``httpx.TimeoutException``
                — tenacity will schedule another attempt.
            InferenceError: On 401/403 (auth), unexpected status codes,
                or unrecognised exceptions — tenacity will **not** retry.
        """
        start = time.monotonic()
        try:
            logger.info(
                "Inference call starting",
                extra={"request_id": request_id, "model": model},
            )

            response = await self._client.post("/chat/completions", json=payload)
            latency_ms = (time.monotonic() - start) * 1000

            if response.status_code == 200:
                return self._parse_response(response, latency_ms, request_id)

            category = self._categorize_status(response.status_code)

            if category == ErrorCategory.AUTH_ERROR:
                raise InferenceError(
                    f"Authentication failed ({response.status_code})",
                    category=category,
                    raw=response.text,
                )

            if category in (ErrorCategory.RATE_LIMIT, ErrorCategory.MODEL_ERROR):
                raise _RetryableError(
                    f"Server returned {response.status_code}",
                    category=category,
                    raw=response.text,
                    retry_after=self._parse_retry_after(response),
                )

            raise InferenceError(
                f"Unexpected status {response.status_code}",
                category=ErrorCategory.UNKNOWN,
                raw=response.text,
            )

        except httpx.TimeoutException as exc:
            raise _RetryableError(
                "Request timed out",
                category=ErrorCategory.TIMEOUT,
                raw=str(exc),
            ) from exc

        except (InferenceError, _RetryableError):
            raise

        except Exception as exc:
            logger.error(
                "Unexpected error during inference call",
                extra={"error_category": ErrorCategory.UNKNOWN.value},
                exc_info=True,
            )
            raise InferenceError(str(exc), category=ErrorCategory.UNKNOWN, raw=str(exc)) from exc

    # ------------------------------------------------------------------
    # Tenacity callbacks
    # ------------------------------------------------------------------

    def _wait_for_retry(self, retry_state: RetryCallState) -> float:
        """Compute the delay before the next retry attempt.

        If the failed request received a ``Retry-After`` header (stored
        on the ``_RetryableError``), that value is used verbatim.
        Otherwise falls back to exponential backoff with jitter
        (initial=2 s, max=30 s, jitter=2 s).

        Args:
            retry_state: Tenacity state object carrying the outcome of
                         the most recent attempt.

        Returns:
            Seconds to sleep before the next attempt.
        """
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _RetryableError) and exc.retry_after is not None:
            return exc.retry_after
        return self._default_wait(retry_state)

    @staticmethod
    def _log_retry(retry_state: RetryCallState) -> None:
        """Tenacity ``before_sleep`` callback — log each retry attempt.

        Args:
            retry_state: Tenacity state object.  ``attempt_number`` is
                         the 1-based index of the attempt that just
                         failed, and ``outcome`` carries the exception.
        """
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        extra: dict[str, object] = {"attempt": retry_state.attempt_number}
        if isinstance(exc, _RetryableError):
            extra["error_category"] = exc.category.value
        logger.warning("Retrying inference call", extra=extra)

    # ------------------------------------------------------------------
    # Response parsing and helpers
    # ------------------------------------------------------------------

    def _parse_response(
        self, response: httpx.Response, latency_ms: float, request_id: str = ""
    ) -> InferenceResponse:
        """Deserialise a successful API response into an ``InferenceResponse``.

        Extracts the assistant message content, token usage counters,
        and ``finish_reason`` from the OpenAI-format JSON body.  Logs
        latency and token counts for observability.

        Args:
            response:   The raw ``httpx.Response`` with status 200.
            latency_ms: Wall-clock time of this HTTP round-trip in
                        milliseconds.
            request_id: Correlation ID for structured logging.

        Returns:
            A fully populated ``InferenceResponse``.

        Raises:
            InferenceError: If the JSON body is missing expected fields
                (``choices[0].message.content``).
        """
        data = response.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason", "stop")
        except (KeyError, IndexError) as exc:
            raise InferenceError(
                "Malformed response from model",
                category=ErrorCategory.MODEL_ERROR,
                raw=data,
            ) from exc

        tool_calls: list[ToolCall] | None = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments", "{}"),
                )
                for tc in raw_tool_calls
            ]

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        logger.info(
            "Inference call completed",
            extra={
                "request_id": request_id,
                "latency_ms": round(latency_ms, 1),
                "model": data.get("model", "unknown"),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "tool_calls_count": len(tool_calls) if tool_calls else 0,
            },
        )

        return InferenceResponse(
            content=content,
            model=data.get("model", "unknown"),
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason or "stop",
            tool_calls=tool_calls,
            raw_response=data,
        )

    @staticmethod
    def _categorize_status(status_code: int) -> ErrorCategory:
        """Map an HTTP status code to an ``ErrorCategory``.

        Args:
            status_code: The HTTP response status code.

        Returns:
            ``AUTH_ERROR`` for 401/403, ``RATE_LIMIT`` for 429,
            ``MODEL_ERROR`` for 5xx, ``UNKNOWN`` for everything else.
        """
        if status_code in (401, 403):
            return ErrorCategory.AUTH_ERROR
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT
        if status_code >= 500:
            return ErrorCategory.MODEL_ERROR
        return ErrorCategory.UNKNOWN

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Extract the ``Retry-After`` header as a float, if present.

        Args:
            response: The HTTP response that triggered a retryable error.

        Returns:
            Seconds to wait as a float, or ``None`` if the header is
            absent or not parseable as a number.
        """
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return None
