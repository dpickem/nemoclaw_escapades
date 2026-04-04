"""Orchestrator — the M1 agent loop.

The orchestrator is the central component of the NemoClaw runtime.  It
owns the full request lifecycle: receive a platform-neutral request from
a connector, build prompt context, call the inference backend, apply
defensive output handling, check the approval gate, and return a
platform-neutral response.

**Isolation guarantees** — The orchestrator imports *no* platform SDK
(``slack_sdk``, ``slack_bolt``, etc.) and contains *no* provider-specific
logic.  It communicates with connectors through ``NormalizedRequest`` /
``RichResponse`` and with backends through ``InferenceRequest`` /
``InferenceResponse``.

**Multi-turn conversation** — Each Slack thread (keyed by ``thread_ts``)
maintains an in-memory message history.  The prompt sent to the model is
always: system prompt + full thread history + latest user message.
History is capped at a configurable maximum (default 50 messages) to
prevent unbounded memory growth.  History is lost on restart — persistent
conversation storage is deferred to M5.

**Transcript repair** — After every inference call the response passes
through a repair layer that handles empty replies, truncated output
(``finish_reason="length"`` triggers a continuation retry), and
content-filter blocks.  See ``transcript_repair.py`` for details.

**Approval gate** — Every response passes through an ``ApprovalGate``
before being returned to the user.  In M1 this is an ``AutoApproval``
stub that approves everything (no tools = no side effects).  The
interface is scaffolded so M2 can plug in a tiered classifier and
async Slack escalation without restructuring the loop.

**Error handling** — All failures are caught, categorised (auth, rate
limit, timeout, model error, unknown), logged with structured JSON, and
surfaced to the user as a human-readable Slack message.  The process
never crashes on a failed request.
"""

from __future__ import annotations

import time
from collections import defaultdict

from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import OrchestratorConfig, load_system_prompt
from nemoclaw_escapades.models.types import (
    ErrorCategory,
    InferenceError,
    InferenceRequest,
    NormalizedRequest,
    RichResponse,
    TextBlock,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.orchestrator.approval import ApprovalGate, AutoApproval
from nemoclaw_escapades.orchestrator.transcript_repair import (
    CONTINUATION_PROMPT,
    MAX_CONTINUATION_RETRIES,
    repair_response,
)

logger = get_logger("orchestrator")


class Orchestrator:
    """M1 orchestrator: multi-turn conversational pipeline with in-memory
    thread history, transcript repair, and a pluggable approval gate."""

    def __init__(
        self,
        backend: BackendBase,
        config: OrchestratorConfig,
        approval: ApprovalGate | None = None,
    ) -> None:
        """Wire together the backend, config, approval gate, and system prompt.

        Args:
            backend:  Inference backend to use for model calls.  The
                      orchestrator only depends on the ``BackendBase``
                      contract, never on a specific provider.
            config:   Orchestrator settings — system prompt path, max
                      thread history length, etc.
            approval: Approval gate for response/action gating.
                      Defaults to ``AutoApproval`` (approve everything)
                      when ``None``.
        """
        self._backend = backend
        self._config = config
        self._approval = approval or AutoApproval()
        self._system_prompt = load_system_prompt(config.system_prompt_path)

        # thread_ts → list of {"role": ..., "content": ...}
        self._thread_history: dict[str, list[dict[str, str]]] = defaultdict(list)

    async def handle(self, request: NormalizedRequest) -> RichResponse:
        """Process a normalised request through the full agent loop.

        Steps:

        1. Build prompt context (system prompt + thread history + user
           message).
        2. Call the inference backend (with transcript-repair retries
           for truncated output).
        3. Pass the result through the approval gate.
        4. Append the assistant reply to thread history.
        5. Shape and return a platform-neutral ``RichResponse``.

        Every step is logged with structured JSON.  On failure the user
        always receives a human-readable error message — the method
        never raises.

        Args:
            request: Platform-neutral request produced by a connector's
                     ``normalize()`` method.

        Returns:
            A ``RichResponse`` containing one or more ``ResponseBlock``
            objects that the connector will render into the platform's
            native format.
        """
        start = time.monotonic()
        thread_key = request.thread_ts or request.request_id

        try:
            messages = self._build_prompt(thread_key, request.text)

            logger.info(
                "Prompt built",
                extra={
                    "request_id": request.request_id,
                    "thread_ts": thread_key,
                    "history_length": len(self._thread_history[thread_key]),
                },
            )

            content = await self._inference_with_repair(
                messages, request.request_id
            )

            approval = await self._approval.check(
                "respond", {"content": content, "request_id": request.request_id}
            )
            if not approval.approved:
                logger.warning(
                    "Response not approved",
                    extra={
                        "request_id": request.request_id,
                        "reason": approval.reason,
                    },
                )
                content = (
                    "I generated a response but it was not approved. "
                    "Please try rephrasing your request."
                )

            self._thread_history[thread_key].append(
                {"role": "assistant", "content": content}
            )

            total_ms = (time.monotonic() - start) * 1000
            logger.info(
                "Request completed",
                extra={
                    "request_id": request.request_id,
                    "latency_ms": round(total_ms, 1),
                },
            )

            return self._shape_response(request, content)

        except InferenceError as exc:
            total_ms = (time.monotonic() - start) * 1000
            logger.error(
                "Inference failed",
                extra={
                    "request_id": request.request_id,
                    "error_category": exc.category.value,
                    "latency_ms": round(total_ms, 1),
                },
                exc_info=True,
            )
            return self._error_response(request, exc.category)

        except Exception:
            total_ms = (time.monotonic() - start) * 1000
            logger.error(
                "Unhandled error in orchestrator",
                extra={
                    "request_id": request.request_id,
                    "error_category": ErrorCategory.UNKNOWN.value,
                    "latency_ms": round(total_ms, 1),
                },
                exc_info=True,
            )
            return self._error_response(request, ErrorCategory.UNKNOWN)

    # ------------------------------------------------------------------
    # Inference + transcript repair
    # ------------------------------------------------------------------

    async def _inference_with_repair(
        self,
        messages: list[dict[str, str]],
        request_id: str,
    ) -> str:
        """Call the inference backend, then apply transcript repair.

        If the model's response is truncated (``finish_reason="length"``),
        the method appends the partial output as an assistant message
        followed by the ``CONTINUATION_PROMPT`` and retries — up to
        ``MAX_CONTINUATION_RETRIES`` additional calls.  Content from
        successive attempts is concatenated.

        If the response is empty or content-filtered, the repair layer
        substitutes a user-friendly fallback and no continuation is
        attempted.

        Args:
            messages:   The full OpenAI-format message list (system
                        prompt + thread history + latest user message).
            request_id: Correlation ID for structured logging.

        Returns:
            The final assistant content string — either the model's
            original output, a concatenation of continuation segments,
            or a repair-layer fallback.
        """
        accumulated_content = ""

        for attempt in range(1 + MAX_CONTINUATION_RETRIES):
            if attempt > 0:
                messages = messages + [
                    {"role": "assistant", "content": accumulated_content},
                    {"role": "user", "content": CONTINUATION_PROMPT},
                ]
                logger.info(
                    "Retrying with continuation prompt",
                    extra={
                        "request_id": request_id,
                        "continuation_attempt": attempt,
                    },
                )

            inference_request = InferenceRequest(
                messages=messages,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
            )
            result = await self._backend.complete(inference_request)

            logger.info(
                "Inference call completed",
                extra={
                    "request_id": request_id,
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "total_tokens": result.usage.total_tokens,
                    "model": result.model,
                    "finish_reason": result.finish_reason,
                },
            )

            repair = repair_response(result, request_id)

            if repair.was_repaired and not repair.needs_continuation:
                return repair.content

            accumulated_content += repair.content

            if not repair.needs_continuation:
                return accumulated_content

        logger.warning(
            "Exhausted continuation retries, returning partial content",
            extra={"request_id": request_id},
        )
        return accumulated_content

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self, thread_key: str, user_text: str
    ) -> list[dict[str, str]]:
        """Assemble the message list sent to the inference backend.

        Appends *user_text* to the thread history, enforces the maximum
        history length (dropping the oldest messages when exceeded), and
        prepends the static system prompt.

        Args:
            thread_key: Thread identifier (``thread_ts`` or the
                        message's own ``request_id`` for top-level
                        messages).
            user_text:  The user's message content.

        Returns:
            A list of ``{"role": ..., "content": ...}`` dicts in
            OpenAI message format, starting with the system prompt.
        """
        self._thread_history[thread_key].append(
            {"role": "user", "content": user_text}
        )

        history = self._thread_history[thread_key]
        max_len = self._config.max_thread_history
        if len(history) > max_len:
            self._thread_history[thread_key] = history[-max_len:]
            history = self._thread_history[thread_key]

        return [{"role": "system", "content": self._system_prompt}] + list(history)

    # ------------------------------------------------------------------
    # Response shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _shape_response(request: NormalizedRequest, content: str) -> RichResponse:
        """Wrap a plain-text assistant reply in a ``RichResponse``.

        Args:
            request: The originating request (used for channel/thread
                     routing).
            content: The assistant's text content.

        Returns:
            A ``RichResponse`` with a single ``TextBlock``.
        """
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[TextBlock(text=content)],
        )

    @staticmethod
    def _error_response(
        request: NormalizedRequest, category: ErrorCategory
    ) -> RichResponse:
        """Build a user-facing error message for a failed request.

        Each ``ErrorCategory`` maps to a distinct, non-technical message
        so the user understands what went wrong without seeing raw
        tracebacks.

        Args:
            request:  The originating request (used for channel/thread
                      routing).
            category: The classified error type.

        Returns:
            A ``RichResponse`` with a single ``TextBlock`` containing
            the error message.
        """
        messages = {
            ErrorCategory.AUTH_ERROR: (
                "I'm having a configuration issue and can't reach the model right now. "
                "Please let the admin know."
            ),
            ErrorCategory.RATE_LIMIT: (
                "I'm being rate-limited right now. Please try again in a moment."
            ),
            ErrorCategory.TIMEOUT: (
                "The model didn't respond in time. Please try again."
            ),
            ErrorCategory.MODEL_ERROR: (
                "Something went wrong with the model. Please try again shortly."
            ),
            ErrorCategory.CONNECTOR_ERROR: (
                "I had trouble communicating. Please try again."
            ),
            ErrorCategory.UNKNOWN: (
                "Something unexpected happened. Please try again, and if the issue "
                "persists, let the admin know."
            ),
        }
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[
                TextBlock(text=messages.get(category, messages[ErrorCategory.UNKNOWN]))
            ],
        )
