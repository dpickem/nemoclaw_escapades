"""Orchestrator — connector-facing request handler with conversation management.

The orchestrator receives platform-neutral connector requests, builds prompt
context, runs inference/tool execution, and returns platform-neutral responses.
It imports no Slack/provider SDKs directly; those concerns stay behind connector
and backend interfaces.

Each thread has an in-memory conversation history managed by
``LayeredPromptBuilder``.  A request becomes system prompt + capped thread
history + latest user message before it reaches the model.

When tools are registered, ``AgentLoop`` owns the multi-turn tool-use cycle.
The orchestrator still owns connector concerns around approval prompts,
finalization button routing, error responses, and history commits.

Write tools pause for user approval.  The paused loop state is stored by thread
key and resumed only when the user clicks Approve; Deny or a new user message
clears the pending write.

Thread history stores user/final-assistant pairs only.  Tool-call round-trips
live in audit surfaces, not future prompts, so large tool outputs do not poison
later turns in the same thread.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from nemoclaw_escapades.agent.approval import ApprovalGate, WriteApproval
from nemoclaw_escapades.agent.loop import AgentLoop, WriteApprovalError
from nemoclaw_escapades.agent.prompt_builder import LayeredPromptBuilder
from nemoclaw_escapades.agent.types import ToolStartCallback
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import AgentLoopConfig, OrchestratorConfig, load_system_prompt
from nemoclaw_escapades.connectors.base import StatusCallback
from nemoclaw_escapades.models.types import (
    APPROVAL_ACTION_APPROVE,
    APPROVAL_ACTION_DENY,
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
from nemoclaw_escapades.orchestrator.finalization_actions import (
    FinalizationActionHandler,
    is_finalization_action,
)
from nemoclaw_escapades.orchestrator.request_context import (
    RequestContext,
    set_request_context,
)
from nemoclaw_escapades.orchestrator.transcript_repair import (
    CONTINUATION_PROMPT,
    MAX_CONTINUATION_RETRIES,
    repair_response,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB

logger = get_logger("orchestrator")

# Number of decimal places for latency values in structured logs.
_LATENCY_LOG_DECIMALS: int = 1

# Initial inference attempt before continuation retries begin.
_INITIAL_INFERENCE_ATTEMPT_COUNT: int = 1


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
        *,
        agent_loop: AgentLoopConfig | None = None,
        approval: ApprovalGate | None = None,
        tools: ToolRegistry | None = None,
        audit: AuditDB | None = None,
        agent_id: str = "",
        finalization_action_handler: FinalizationActionHandler | None = None,
    ) -> None:
        """Initialise the orchestrator and optional tool loop.

        Args:
            backend: Inference backend.
            config: Model and prompt-history settings.
            agent_loop: Runtime knobs for tool-capable loops.
            approval: Write-approval gate; defaults to user approval.
            tools: Optional tool registry.
            audit: Optional tool-call audit DB.
            agent_id: Optional id surfaced in prompt metadata.
            finalization_action_handler: Optional button-click handler.
        """
        # Inference backend used by direct and tool-capable paths.
        self._backend = backend
        # Orchestrator-level model, prompt, and history settings.
        self._config = config
        # Approval gate for write tools and response-level checks.
        self._approval = approval or WriteApproval()
        # Optional tool registry; absence uses direct inference path.
        self._tools = tools
        # Optional audit DB passed through to AgentLoop.
        self._audit = audit
        # Agent id rendered in runtime prompt metadata.
        self._agent_id = agent_id
        # Optional router for finalization action button clicks.
        self._finalization_actions = finalization_action_handler
        # Per-thread prompt history and message assembly.
        self._prompt = LayeredPromptBuilder(
            identity=load_system_prompt(config.system_prompt_path),
            max_thread_history=config.max_thread_history,
        )
        # Comma-separated tool names for runtime prompt metadata.
        self._tools_summary = ", ".join(sorted(tools.names)) if tools else ""
        # Thread key -> paused write approval state.
        self._pending_approvals: dict[str, PendingApproval] = {}

        # The AgentLoop is created once and reused for every request.
        # It's only instantiated when tools are registered — without
        # tools, the orchestrator uses the simpler _inference_with_repair
        # path which doesn't support multi-turn tool calling.
        base_loop_cfg = agent_loop or AgentLoopConfig()
        merged_loop_cfg = dataclasses.replace(
            base_loop_cfg,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        # Shared AgentLoop for tool-capable requests.
        self._agent_loop: AgentLoop | None = None
        if tools and len(tools) > 0:
            self._agent_loop = AgentLoop(
                backend=backend,
                tools=tools,
                config=merged_loop_cfg,
                audit=audit,
                approval=self._approval,
            )

    async def handle(
        self,
        request: NormalizedRequest,
        on_status: StatusCallback | None = None,
    ) -> RichResponse:
        """Process one connector request and return a connector response.

        Handles button clicks before inference, routes pending Iterate replies,
        runs either AgentLoop or direct inference, and commits successful turns
        to thread history.

        Args:
            request: Platform-neutral request from the connector.
            on_status: Optional thinking-indicator callback.

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

        # Make channel/thread metadata available to tools.
        set_request_context(
            RequestContext(
                request_id=request.request_id,
                channel_id=request.channel_id,
                thread_ts=request.thread_ts,
                source=request.source,
            )
        )

        # ── 1. Button-click dispatch ─────────────────────────────────
        # Button clicks bypass the planning model.
        if request.action:
            if request.action.action_id == APPROVAL_ACTION_APPROVE:
                return await self._handle_write_approval(request, thread_key, on_status)
            if request.action.action_id == APPROVAL_ACTION_DENY:
                return self._handle_write_denial(request, thread_key)
            if self._finalization_actions is not None and is_finalization_action(request):
                return await self._finalization_actions.handle(request)

        # ── 2. Pending iteration text ────────────────────────────────
        # Text after an Iterate click becomes the re-delegation prompt.
        if (
            self._finalization_actions is not None
            and self._finalization_actions.is_pending_iteration(thread_key)
        ):
            return await self._finalization_actions.consume_iteration_feedback(request, thread_key)

        # ── 3. Stale approval cleanup ────────────────────────────────
        # A fresh message supersedes any pending write approval.
        if thread_key in self._pending_approvals:
            logger.info(
                "Clearing stale pending approval (new message in thread)",
                extra={"thread_key": thread_key},
            )
            self._pending_approvals.pop(thread_key)

        try:
            # ── 4. Prompt assembly ───────────────────────────────────
            messages = self._prompt.messages_for_inference(
                thread_key,
                request.text,
                agent_id=self._agent_id,
                source_type=request.source,
                tools_summary=self._tools_summary,
            )

            logger.info(
                "Prompt built",
                extra={
                    "request_id": request.request_id,
                    "thread_ts": thread_key,
                    "history_length": len(messages) - _INITIAL_INFERENCE_ATTEMPT_COUNT,
                },
            )

            # ── 5. Inference ─────────────────────────────────────────
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

            # ── 6. Response approval ─────────────────────────────────
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

            # ── 7. Commit + respond ──────────────────────────────────
            # Persist only the final assistant text, not tool round-trips.
            self._prompt.commit_turn(thread_key, request.text, content)

            logger.info(
                "Request completed",
                extra={
                    "request_id": request.request_id,
                    "latency_ms": round(timer.ms, _LATENCY_LOG_DECIMALS),
                },
            )

            return self._shape_response(request, content)

        # ── Error handling ───────────────────────────────────────────
        except WriteApprovalError as exc:
            # Save paused loop state so Approve can resume exactly here.
            exc.pending.original_user_text = request.text
            self._pending_approvals[thread_key] = exc.pending
            logger.info(
                "Write approval requested",
                extra={
                    "request_id": request.request_id,
                    "thread_key": thread_key,
                    "latency_ms": round(timer.ms, _LATENCY_LOG_DECIMALS),
                },
            )
            return self._build_approval_response(request, exc.pending)

        except InferenceError as exc:
            logger.error(
                "Inference failed",
                extra={
                    "request_id": request.request_id,
                    "error_category": exc.category.value,
                    "latency_ms": round(timer.ms, _LATENCY_LOG_DECIMALS),
                },
                exc_info=True,
            )
            return self._error_response(request, exc.category)

        except Exception:
            logger.error(
                "Unhandled error in orchestrator",
                extra={
                    "request_id": request.request_id,
                    "error_category": ErrorCategory.UNKNOWN.value,
                    "latency_ms": round(timer.ms, _LATENCY_LOG_DECIMALS),
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

        The closure is passed to ``AgentLoop.run`` for one request only, so
        concurrent requests cannot clobber status callbacks.
        """
        if on_status is None:
            return None

        async def _tool_start_adapter(display_name: str) -> None:
            await on_status(f"{display_name}...")

        return _tool_start_adapter

    # ------------------------------------------------------------------
    # Inference + transcript repair (non-tool path)
    # ------------------------------------------------------------------

    async def _inference_with_repair(
        self,
        messages: list[Message],
        request_id: str,
    ) -> str:
        """Call the inference backend without tools, with transcript repair.

        Used when no ``ToolRegistry`` is configured.  Handles empty replies,
        content-filter blocks, and truncation continuations.

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

        for attempt in range(_INITIAL_INFERENCE_ATTEMPT_COUNT + MAX_CONTINUATION_RETRIES):
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

        Replays the paused tool call, appends its result, and resumes the agent
        loop so the model can produce a final response.

        Args:
            request: The Approve button-click request.
            thread_key: Conversation thread identifier.
            on_status: Optional callback for thinking-indicator updates.

        Returns:
            A ``RichResponse`` with the model's post-approval reply, or
            a new approval prompt if a cascading write was detected.
        """
        # Pop atomically — if the user double-clicks Approve, the second
        # click finds no pending approval and we return a suppressed
        # response so the connector silently drops it instead of posting
        # a confusing "No pending write operation" reply.
        pending = self._pending_approvals.pop(thread_key, None)
        if not pending:
            logger.info(
                "Stale Approve click — no pending approval",
                extra={
                    "request_id": request.request_id,
                    "thread_key": thread_key,
                },
            )
            return RichResponse(
                channel_id=request.channel_id,
                thread_ts=request.thread_ts,
                suppress_post=True,
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

        # The assistant's tool-call message must precede tool results.
        pending.working_messages.append(pending.assistant_message)

        callback = self._make_tool_start_callback(on_status)
        tool_results = await self._agent_loop.execute_tool_calls(
            pending.tool_calls,
            pending.request_id,
            thread_ts=thread_key,
            on_tool_start=callback,
        )
        pending.working_messages.extend(tool_results)

        # Re-enter the agent loop so the model can see the tool results
        # and generate a final response.  ``pre_surfaced_tools`` carries
        # forward the non-core tools ``tool_search`` had surfaced in the
        # original request's task — that task ended when we returned
        # the approval prompt to Slack, and the click event runs in a
        # fresh task with its own ``ContextVar`` context, so the
        # surface set is otherwise lost.  Without this, the model
        # couldn't follow up on the post-approval round with the
        # tools its own ``working_messages`` reference.  If the model
        # requests *another* write tool (cascading approval), we catch
        # and re-save — the user will see a second Approve/Deny prompt.
        try:
            result = await self._agent_loop.run(
                pending.working_messages,
                pending.request_id,
                thread_ts=thread_key,
                on_tool_start=callback,
                pre_surfaced_tools=pending.surfaced_tools,
            )
            content = result.content
        except WriteApprovalError as exc:
            # Cascading write: show another approval prompt.
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
        pending = self._pending_approvals.pop(thread_key, None)
        if not pending:
            logger.info(
                "Stale Deny click — no pending approval",
                extra={
                    "request_id": request.request_id,
                    "thread_key": thread_key,
                },
            )
            return RichResponse(
                channel_id=request.channel_id,
                thread_ts=request.thread_ts,
                suppress_post=True,
            )
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
