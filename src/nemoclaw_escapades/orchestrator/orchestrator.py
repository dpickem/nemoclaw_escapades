"""Orchestrator — connector-facing request handler with conversation management.

The orchestrator is the central component of the NemoClaw runtime.  It
owns the full request lifecycle: receive a platform-neutral request from
a connector, build prompt context, delegate inference + tool execution
to ``AgentLoop``, and return a platform-neutral response.

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

**Tool-use delegation** — When tools are registered, the orchestrator
creates an ``AgentLoop`` and delegates the multi-turn inference + tool
execution cycle to it.  The ``AgentLoop`` is stateless per call — all
connector concerns (history, approval UI, error responses) remain here.

**Approval gate** — Write tool calls are gated before execution.  The
``AgentLoop`` raises ``WriteApprovalError`` which the orchestrator
catches, saves the conversation state, and presents Approve / Deny
buttons via the connector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nemoclaw_escapades.agent.approval import ApprovalGate, WriteApproval
from nemoclaw_escapades.agent.loop import AgentLoop, WriteApprovalError
from nemoclaw_escapades.agent.prompt_builder import LayeredPromptBuilder, SourceType
from nemoclaw_escapades.agent.scratchpad import Scratchpad
from nemoclaw_escapades.agent.types import ToolStartCallback
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import AgentLoopConfig, OrchestratorConfig, load_system_prompt
from nemoclaw_escapades.connectors.base import StatusCallback
from nemoclaw_escapades.models.types import (
    ActionBlock,
    ActionButton,
    ErrorCategory,
    InferenceError,
    InferenceRequest,
    Message,
    MessageRole,
    NormalizedRequest,
    PendingApproval,
    RichResponse,
    TextBlock,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.observability.timer import Timer
from nemoclaw_escapades.orchestrator.transcript_repair import (
    CONTINUATION_PROMPT,
    MAX_CONTINUATION_RETRIES,
    repair_response,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB

logger = get_logger("orchestrator")

# Slack ``action_id`` values attached to the Approve / Deny buttons
# rendered by ``_build_approval_response``.  The connector routes
# button clicks back to ``handle()`` keyed on these IDs.
APPROVAL_ACTION_APPROVE = "approve_write"
APPROVAL_ACTION_DENY = "deny_write"


class Orchestrator:
    """Connector-facing request handler with conversation management.

    Wraps ``AgentLoop`` to add orchestrator-specific concerns:

    1. **Prompt assembly** — ``LayeredPromptBuilder`` prepends the system prompt
       to the per-thread conversation history and the latest user message.
    2. **Tool-use delegation** — when tools are registered, an
       ``AgentLoop`` handles the multi-turn inference + tool cycle.
    3. **Approval gating** — write tool calls surface Approve / Deny
       buttons; the conversation state is saved and resumed on click.
    4. **Non-tool fallback** — without tools, a simpler inference path
       with transcript repair is used directly.
    5. **Transcript repair** — empty replies, truncated output, and
       content-filter blocks are handled transparently.
    6. **Response delivery** — the final text is wrapped in a
       platform-neutral ``RichResponse`` and returned to the connector.

    Thread history is kept in memory (keyed by ``thread_ts``) and lost
    on restart.  Persistent storage is planned for a future milestone.
    """

    def __init__(
        self,
        backend: BackendBase,
        config: OrchestratorConfig,
        approval: ApprovalGate | None = None,
        tools: ToolRegistry | None = None,
        audit: AuditDB | None = None,
        scratchpad: Scratchpad | None = None,
        agent_id: str = "",
    ) -> None:
        """Initialise the orchestrator.

        Args:
            backend: Inference backend used for chat-completions calls.
            config: Orchestrator-level settings (model, temperature,
                max tokens, system prompt path, history cap).
            approval: Gate consulted before executing write tools.
                Defaults to ``WriteApproval`` (writes require user
                confirmation via Approve / Deny buttons).
            tools: Optional tool registry.  When provided, an
                ``AgentLoop`` is created and used for multi-turn
                tool calling.
            audit: Optional audit database.  When provided, every tool
                invocation is logged (service, args, latency, success).
            scratchpad: Optional scratchpad.  When provided, its contents
                are injected into the system prompt and returned in
                ``AgentLoopResult``; the scratchpad tools are expected
                to already be registered in *tools*.
            agent_id: Identifier surfaced in the runtime-metadata prompt
                layer (useful for multi-agent traceability once M2b
                lands).  Empty string omits the line.
        """
        self._backend = backend
        self._config = config
        # Default to WriteApproval — write tool calls are blocked and
        # surfaced to the user with Approve/Deny buttons.  Callers can
        # override with AutoApproval() for testing or trusted contexts.
        self._approval = approval or WriteApproval()
        self._tools = tools
        self._audit = audit
        self._scratchpad = scratchpad
        self._agent_id = agent_id
        # LayeredPromptBuilder owns per-thread conversation histories,
        # the 5-layer system prompt (identity, task context, cache
        # boundary, runtime metadata, channel hint), and the message
        # assembly for inference calls.
        self._prompt = LayeredPromptBuilder(
            identity=load_system_prompt(config.system_prompt_path),
            max_thread_history=config.max_thread_history,
        )
        # Cache a comma-separated list of available tool names for the
        # runtime-metadata layer.  Kept empty when no tools are
        # registered so the prompt doesn't advertise nothing.
        self._tools_summary = ", ".join(sorted(tools.names)) if tools else ""
        # Keyed by thread_ts — stores the full conversation snapshot
        # when a write tool is blocked.  Popped on Approve (resume
        # execution) or Deny (discard), or when a new user message
        # arrives in the same thread (stale cleanup).
        self._pending_approvals: dict[str, PendingApproval] = {}

        # The AgentLoop is created once and reused for every request.
        # It's only instantiated when tools are registered — without
        # tools, the orchestrator uses the simpler _inference_with_repair
        # path which doesn't support multi-turn tool calling.
        self._agent_loop: AgentLoop | None = None
        if tools and len(tools) > 0:
            self._agent_loop = AgentLoop(
                backend=backend,
                tools=tools,
                config=AgentLoopConfig(
                    model=config.model,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                ),
                audit=audit,
                # Share the same approval gate so both the orchestrator's
                # response-level check and the loop's tool-level check
                # use the same policy.
                approval=self._approval,
                scratchpad=scratchpad,
            )

    async def handle(
        self,
        request: NormalizedRequest,
        on_status: StatusCallback | None = None,
    ) -> RichResponse:
        """Process a normalised request through the full agent loop.

        When tools are registered, the loop supports multi-turn tool
        calling: the model can emit tool_calls, the orchestrator
        executes them, feeds results back, and the model continues
        until it produces a final text response.

        Write tool calls are gated: the loop pauses and returns an
        approval request with Approve / Deny buttons.  Clicking
        Approve resumes execution; Deny discards the pending action.

        Args:
            request: Platform-neutral request from the connector.
                May carry an ``ActionPayload`` for button clicks.
            on_status: Optional async callback invoked with a
                human-readable status string before each tool call
                (e.g. "Searching Jira...").  Connectors use this to
                update a thinking indicator in real time.

        Returns:
            A ``RichResponse`` containing the assistant's reply (text,
            action buttons, or an error message) addressed to the
            originating channel and thread.
        """
        timer = Timer()

        # Each Slack thread gets its own conversation history.  If the
        # request has no thread_ts (e.g. a top-level DM), fall back to
        # the request_id so every message still gets a unique key.
        thread_key = request.thread_ts or request.request_id

        # ── 1. Button-click dispatch ──────────────────────────────────
        # If the request carries an action payload, it's a button click
        # from a previous approval prompt — route it directly to the
        # approval/denial handler instead of running inference.
        if request.action:
            if request.action.action_id == APPROVAL_ACTION_APPROVE:
                return await self._handle_write_approval(request, thread_key, on_status)
            if request.action.action_id == APPROVAL_ACTION_DENY:
                return self._handle_write_denial(request, thread_key)

        # ── 2. Stale approval cleanup ─────────────────────────────────
        # If the user sends a new text message in a thread that has a
        # pending write approval, discard it.  The user is moving on;
        # executing a stale write after a new conversation turn would
        # be confusing and potentially dangerous.
        if thread_key in self._pending_approvals:
            logger.info(
                "Clearing stale pending approval (new message in thread)",
                extra={"thread_key": thread_key},
            )
            self._pending_approvals.pop(thread_key)

        try:
            # ── 3. Prompt assembly ────────────────────────────────────
            # Build the full message list: system prompt + per-thread
            # conversation history + the new user message.  History is
            # capped at max_thread_history to bound context window usage.
            messages = self._prompt.messages_for_inference(
                thread_key,
                request.text,
                agent_id=self._agent_id,
                source_type=self._resolve_source_type(request.source),
                scratchpad=(self._scratchpad.read() if self._scratchpad else ""),
                tools_summary=self._tools_summary,
            )

            logger.info(
                "Prompt built",
                extra={
                    "request_id": request.request_id,
                    "thread_ts": thread_key,
                    "history_length": len(messages) - 1,
                },
            )

            # ── 4. Inference ──────────────────────────────────────────
            # Two paths: with tools (AgentLoop handles multi-turn
            # tool calling) or without (single inference call with
            # transcript repair for truncation/empty responses).
            if self._agent_loop:
                callback = self._make_tool_start_callback(on_status)
                result = await self._agent_loop.run(
                    messages,
                    request.request_id,
                    thread_ts=thread_key,
                    on_tool_start=callback,
                )
                content = result.content
            else:
                # No tools registered — fall back to single-shot
                # inference with continuation retries for truncation.
                content = await self._inference_with_repair(messages, request.request_id)

            # ── 5. Response-level approval ────────────────────────────
            # A second approval check on the *text content* itself.
            # This is separate from the write-tool gate — it catches
            # cases where the model's final answer should be filtered
            # (e.g. policy violations in the response text).
            approval = await self._approval.check(
                "respond",
                {"content": content, "request_id": request.request_id},
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

            # ── 6. Commit to history ──────────────────────────────────
            # Only commit after a successful round — failed inference or
            # approval-blocked writes must not pollute the thread history,
            # otherwise the model would see phantom turns on the next request.
            self._prompt.commit_turn(thread_key, request.text, content)

            logger.info(
                "Request completed",
                extra={
                    "request_id": request.request_id,
                    "latency_ms": round(timer.ms, 1),
                },
            )

            return self._shape_response(request, content)

        # ── Error handling ────────────────────────────────────────────
        # Errors are caught in specificity order.  Each produces a
        # user-friendly message without leaking internal details.

        except WriteApprovalError as exc:
            # The AgentLoop encountered a write tool and the approval
            # gate denied it.  Save the full conversation state so we
            # can resume exactly where we left off when the user clicks
            # Approve.  The pending state includes the working messages,
            # the assistant's tool-call message, and the original user
            # text (needed to commit the turn on resume).
            exc.pending.original_user_text = request.text
            self._pending_approvals[thread_key] = exc.pending
            logger.info(
                "Write approval requested",
                extra={
                    "request_id": request.request_id,
                    "thread_key": thread_key,
                    "latency_ms": round(timer.ms, 1),
                },
            )
            return self._build_approval_response(request, exc.pending)

        except InferenceError as exc:
            # Classified backend failure (auth, rate limit, timeout,
            # model error).  Each category maps to a specific user-facing
            # message so the user knows whether to retry or escalate.
            logger.error(
                "Inference failed",
                extra={
                    "request_id": request.request_id,
                    "error_category": exc.category.value,
                    "latency_ms": round(timer.ms, 1),
                },
                exc_info=True,
            )
            return self._error_response(request, exc.category)

        except Exception:
            # Catch-all for truly unexpected failures (bugs, network
            # issues, corrupted state).  Logged at ERROR with full
            # traceback for debugging.
            logger.error(
                "Unhandled error in orchestrator",
                extra={
                    "request_id": request.request_id,
                    "error_category": ErrorCategory.UNKNOWN.value,
                    "latency_ms": round(timer.ms, 1),
                },
                exc_info=True,
            )
            return self._error_response(request, ErrorCategory.UNKNOWN)

    # ------------------------------------------------------------------
    # AgentLoop wiring
    # ------------------------------------------------------------------

    @staticmethod
    def _make_tool_start_callback(
        on_status: StatusCallback | None,
    ) -> ToolStartCallback | None:
        """Build a per-request tool-start callback from the connector's status callback.

        Returns a closure that appends "..." to the display name, or
        ``None`` if no status callback was provided.  The returned
        closure is passed to ``AgentLoop.run()`` as a parameter —
        never stored on the shared loop instance — so concurrent
        requests can't clobber each other's callbacks.
        """
        if on_status is None:
            return None

        async def _tool_start_adapter(display_name: str) -> None:
            await on_status(f"{display_name}...")

        return _tool_start_adapter

    @staticmethod
    def _resolve_source_type(source: str) -> SourceType:
        """Map a ``NormalizedRequest.source`` string to a ``SourceType`` enum.

        Unknown platforms (e.g. ``"teams"``, ``"test"``) fall back to
        ``SourceType.USER`` — the channel hint still renders correctly
        via the fallback branch in ``LayeredPromptBuilder._channel_hint``.
        """
        try:
            return SourceType(source)
        except ValueError:
            return SourceType.USER

    # ------------------------------------------------------------------
    # Inference + transcript repair (non-tool path)
    # ------------------------------------------------------------------

    async def _inference_with_repair(
        self,
        messages: list[Message],
        request_id: str,
    ) -> str:
        """Call the inference backend without tools, with transcript repair.

        Used when no ``ToolRegistry`` is configured.  The response is
        passed through ``repair_response`` which handles empty replies,
        content-filter blocks, and truncation (``finish_reason="length"``
        triggers continuation retries).

        Args:
            messages: Full message list (system + history + user).
            request_id: Correlation ID for structured logging.

        Returns:
            The model's final text content, possibly assembled from
            multiple continuation chunks.
        """
        # Snapshot the original messages — continuation scaffolding is
        # rebuilt from scratch each iteration so we never accumulate
        # stale assistant/user pairs.
        base_messages: list[dict[str, Any]] = [dict(m) for m in messages]
        prior_continuation_chunks: list[str] = []

        # attempt 0 = initial call; attempts 1..N = continuation retries
        # after finish_reason="length" truncation.
        for attempt in range(1 + MAX_CONTINUATION_RETRIES):
            # Rebuild the full message list: original messages + all
            # prior (partial assistant → "please continue" user) pairs.
            # This gives the model the full context of what it already
            # said so it can pick up exactly where it left off.
            call_messages: list[dict[str, Any]] = list(base_messages)
            for chunk in prior_continuation_chunks:
                call_messages.append({"role": MessageRole.ASSISTANT, "content": chunk})
                call_messages.append({"role": MessageRole.USER, "content": CONTINUATION_PROMPT})

            if attempt > 0:
                logger.info(
                    "Retrying with continuation prompt",
                    extra={
                        "request_id": request_id,
                        "continuation_attempt": attempt,
                    },
                )

            inference_request = InferenceRequest(
                messages=call_messages,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                request_id=request_id,
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

            # Case 1: repair changed the content (e.g. replaced an
            # empty reply with a fallback) and no more data is needed.
            if repair.was_repaired and not repair.needs_continuation:
                return repair.content

            # Case 2: model finished normally — concatenate any prior
            # chunks with this final piece and return.
            if not repair.needs_continuation:
                return "".join(prior_continuation_chunks) + repair.content

            # Case 3: finish_reason="length" — save this chunk and
            # loop again with a continuation prompt.
            prior_continuation_chunks.append(repair.content)

        # If we exhaust all retries the model is producing very long
        # output — return what we have rather than dropping it entirely.
        logger.warning(
            "Exhausted continuation retries, returning partial content",
            extra={"request_id": request_id},
        )
        return "".join(prior_continuation_chunks)

    # ------------------------------------------------------------------
    # Write-approval helpers
    # ------------------------------------------------------------------

    async def _handle_write_approval(
        self,
        request: NormalizedRequest,
        thread_key: str,
        on_status: StatusCallback | None = None,
    ) -> RichResponse:
        """Execute a previously-blocked write after the user clicks Approve.

        Pops the saved ``PendingApproval`` for this thread, appends the
        assistant tool-call message and tool results to the saved
        conversation, then resumes the agent loop so the model can
        generate a final response.

        Args:
            request: The Approve button-click request.
            thread_key: Conversation thread identifier.
            on_status: Optional callback for thinking-indicator updates.

        Returns:
            A ``RichResponse`` with the model's post-approval reply, or
            a new approval prompt if a cascading write was detected.
        """
        # Pop atomically — if the user double-clicks Approve, the second
        # click gets the "no pending" message instead of re-executing.
        pending = self._pending_approvals.pop(thread_key, None)
        if not pending:
            return self._shape_response(
                request, "No pending write operation found for this thread."
            )

        if self._agent_loop is None:
            return self._shape_response(request, "Tool execution is not available.")

        logger.info(
            "User approved write operation",
            extra={
                "request_id": pending.request_id,
                "thread_key": thread_key,
                "tool_calls": [tc.name for tc in pending.tool_calls],
            },
        )

        # Reconstruct the conversation to the point where the loop
        # paused.  The assistant's tool-call message must precede the
        # tool results (OpenAI protocol requirement).
        pending.working_messages.append(pending.assistant_message)

        # Now execute the previously-blocked write tools.  This runs
        # outside the loop's normal cycle because the loop already
        # exited via WriteApprovalError.
        callback = self._make_tool_start_callback(on_status)
        tool_results = await self._agent_loop.execute_tool_calls(
            pending.tool_calls,
            pending.request_id,
            thread_ts=thread_key,
            on_tool_start=callback,
        )
        pending.working_messages.extend(tool_results)

        # Re-enter the agent loop so the model can see the tool results
        # and generate a final response.  If the model requests *another*
        # write tool (cascading approval), we catch and re-save — the
        # user will see a second Approve/Deny prompt.
        try:
            result = await self._agent_loop.run(
                pending.working_messages,
                pending.request_id,
                thread_ts=thread_key,
                on_tool_start=callback,
            )
            content = result.content
        except WriteApprovalError as exc:
            # Cascading write: the model's post-approval response
            # triggered yet another write tool.  Preserve the original
            # user text so the eventual commit uses the right turn pair.
            exc.pending.original_user_text = pending.original_user_text
            self._pending_approvals[thread_key] = exc.pending
            return self._build_approval_response(request, exc.pending)

        # Commit the full turn (original user text → final assistant
        # response) now that the write has been executed and the model
        # has produced its summary.
        self._prompt.commit_turn(thread_key, pending.original_user_text, content)
        return self._shape_response(request, content)

    def _handle_write_denial(
        self,
        request: NormalizedRequest,
        thread_key: str,
    ) -> RichResponse:
        """Discard the pending write when the user clicks Deny.

        Args:
            request: The Deny button-click request.
            thread_key: Conversation thread identifier.

        Returns:
            A ``RichResponse`` confirming cancellation.
        """
        # Discard the saved state — the write will never execute.
        # No history commit: the user's original message and the
        # model's tool-call attempt are both dropped, keeping the
        # thread history clean for the next turn.
        pending = self._pending_approvals.pop(thread_key, None)
        if pending:
            logger.info(
                "User denied write operation",
                extra={
                    "request_id": pending.request_id,
                    "thread_key": thread_key,
                    "tool_calls": [tc.name for tc in pending.tool_calls],
                },
            )
        return self._shape_response(request, "Got it — the write operation was cancelled.")

    def _build_approval_response(
        self,
        request: NormalizedRequest,
        pending: PendingApproval,
    ) -> RichResponse:
        """Build a ``RichResponse`` with Approve / Deny buttons for a blocked write."""
        # The description (rendered by AgentLoop._format_write_description)
        # shows exactly which tools will run and with what arguments, so
        # the user can make an informed decision.
        text = (
            ":warning: *Write operation requires your approval*\n\n"
            f"{pending.description}\n\n"
            "Click *Approve* to proceed or *Deny* to cancel."
        )
        # The button values carry the thread_ts so the connector can
        # route the click back to the correct conversation thread.
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[
                TextBlock(text=text),
                ActionBlock(
                    actions=[
                        ActionButton(
                            label="Approve",
                            action_id=APPROVAL_ACTION_APPROVE,
                            value=request.thread_ts or request.request_id,
                            style="primary",
                        ),
                        ActionButton(
                            label="Deny",
                            action_id=APPROVAL_ACTION_DENY,
                            value=request.thread_ts or request.request_id,
                            style="danger",
                        ),
                    ]
                ),
            ],
        )

    # ------------------------------------------------------------------
    # Response shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _shape_response(request: NormalizedRequest, content: str) -> RichResponse:
        """Wrap a text string in a ``RichResponse`` addressed to the request's thread."""
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[TextBlock(text=content)],
        )

    @staticmethod
    def _error_response(request: NormalizedRequest, category: ErrorCategory) -> RichResponse:
        """Build a user-friendly error response for a classified failure."""
        # Each category maps to a specific message tone: auth errors
        # suggest escalating to an admin, rate limits suggest waiting,
        # etc.  Internal details (stack traces, HTTP codes) are never
        # exposed to the user — they're in the structured log instead.
        messages = {
            ErrorCategory.AUTH_ERROR: (
                "I'm having a configuration issue and can't reach the model right now. "
                "Please let the admin know."
            ),
            ErrorCategory.RATE_LIMIT: (
                "I'm being rate-limited right now. Please try again in a moment."
            ),
            ErrorCategory.TIMEOUT: ("The model didn't respond in time. Please try again."),
            ErrorCategory.MODEL_ERROR: (
                "Something went wrong with the model. Please try again shortly."
            ),
            ErrorCategory.CONNECTOR_ERROR: ("I had trouble communicating. Please try again."),
            ErrorCategory.UNKNOWN: (
                "Something unexpected happened. Please try again, and if the issue "
                "persists, let the admin know."
            ),
        }
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[TextBlock(text=messages.get(category, messages[ErrorCategory.UNKNOWN]))],
            error_category=category,
        )
