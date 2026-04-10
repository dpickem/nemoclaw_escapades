"""Orchestrator — the agent loop with multi-turn tool use.

The orchestrator is the central component of the NemoClaw runtime.  It
owns the full request lifecycle: receive a platform-neutral request from
a connector, build prompt context, call the inference backend, execute
tool calls, check the approval gate, and return a platform-neutral
response.

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

**Tool-use loop** — When the model emits ``tool_calls`` in its response,
the orchestrator executes each tool (via the ``ToolRegistry``), feeds
results back as ``tool`` role messages, and re-invokes the model.  This
loop continues until the model produces a text response or a safety
limit is reached.

**Transcript repair** — After every text inference call the response
passes through a repair layer that handles empty replies, truncated
output (``finish_reason="length"`` triggers a continuation retry), and
content-filter blocks.  See ``transcript_repair.py`` for details.

**Approval gate** — Every response passes through an ``ApprovalGate``
before being returned to the user.  WRITE tool calls are gated before
execution.
"""

from __future__ import annotations

import json
from typing import Any

from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import OrchestratorConfig, load_system_prompt
from nemoclaw_escapades.connectors.base import StatusCallback
from nemoclaw_escapades.models.types import (
    ActionBlock,
    ActionButton,
    ErrorCategory,
    InferenceError,
    InferenceRequest,
    InferenceResponse,
    Message,
    NormalizedRequest,
    PendingApproval,
    RichResponse,
    TextBlock,
    ToolCall,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.observability.timer import Timer
from nemoclaw_escapades.orchestrator.approval import ApprovalGate, AutoApproval
from nemoclaw_escapades.orchestrator.prompt_builder import PromptBuilder
from nemoclaw_escapades.orchestrator.transcript_repair import (
    CONTINUATION_PROMPT,
    MAX_CONTINUATION_RETRIES,
    repair_response,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

logger = get_logger("orchestrator")

# Safety limit: the agent loop calls the model at most this many times
# per request before returning a partial answer.
MAX_TOOL_ROUNDS = 10

# Slack ``action_id`` values attached to the Approve / Deny buttons
# rendered by ``_build_approval_response``.  The connector routes
# button clicks back to ``handle()`` keyed on these IDs.
APPROVAL_ACTION_APPROVE = "approve_write"
APPROVAL_ACTION_DENY = "deny_write"


class WriteApprovalError(Exception):
    """Raised inside the agent loop when a write tool requires user approval."""

    def __init__(self, pending: PendingApproval) -> None:
        self.pending = pending


class Orchestrator:
    """Central agent loop with multi-turn conversation and tool use.

    The orchestrator owns the full request lifecycle:

    1. **Prompt assembly** — ``PromptBuilder`` prepends the system prompt
       to the per-thread conversation history and the latest user message.
    2. **Inference** — the prompt (plus tool definitions when a
       ``ToolRegistry`` is provided) is sent to the backend.
    3. **Tool execution** — if the model emits ``tool_calls``, each tool
       is executed via the registry, results are appended, and inference
       is called again.  This loops up to ``MAX_TOOL_ROUNDS`` times.
    4. **Approval gating** — before executing a write tool the
       ``ApprovalGate`` is consulted.  Denied calls pause the loop,
       save the conversation state, and return Approve / Deny buttons.
       The loop resumes when the user clicks Approve.
    5. **Transcript repair** — empty replies, truncated output
       (``finish_reason="length"``), and content-filter blocks are
       handled transparently.
    6. **Response delivery** — the final text is wrapped in a
       platform-neutral ``RichResponse`` and returned to the connector.

    The orchestrator imports *no* platform SDK and contains *no*
    provider-specific logic.  It communicates with connectors through
    ``NormalizedRequest`` / ``RichResponse`` and with backends through
    ``InferenceRequest`` / ``InferenceResponse``.

    Thread history is kept in memory (keyed by ``thread_ts``) and lost
    on restart.  Persistent storage is planned for a future milestone.
    """

    def __init__(
        self,
        backend: BackendBase,
        config: OrchestratorConfig,
        approval: ApprovalGate | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        """Initialise the orchestrator.

        Args:
            backend: Inference backend used for chat-completions calls.
            config: Orchestrator-level settings (model, temperature,
                max tokens, system prompt path, history cap).
            approval: Gate consulted before executing write tools.
                Defaults to ``AutoApproval`` (everything allowed).
            tools: Optional tool registry.  When provided, the agent
                loop sends tool definitions to the model and can
                execute tool calls.
        """
        self._backend = backend
        self._config = config
        self._approval = approval or AutoApproval()
        self._tools = tools
        self._prompt = PromptBuilder(
            system_prompt=load_system_prompt(config.system_prompt_path),
            max_thread_history=config.max_thread_history,
        )
        self._pending_approvals: dict[str, PendingApproval] = {}

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
        thread_key = request.thread_ts or request.request_id

        # ── 1. Fast-path: handle Approve / Deny button clicks ────
        # These arrive as NormalizedRequests with an ActionPayload.
        # They bypass inference entirely and resume or discard the
        # saved pending-approval state.
        if request.action:
            if request.action.action_id == APPROVAL_ACTION_APPROVE:
                return await self._handle_write_approval(request, thread_key, on_status)
            if request.action.action_id == APPROVAL_ACTION_DENY:
                return self._handle_write_denial(request, thread_key)

        # ── 2. Clear stale pending approvals ─────────────────────
        # If the user sends a new regular message in a thread that
        # has an unanswered approval prompt, the context has changed
        # and the old pending write is no longer relevant.
        if thread_key in self._pending_approvals:
            logger.info(
                "Clearing stale pending approval (new message in thread)",
                extra={"thread_key": thread_key},
            )
            self._pending_approvals.pop(thread_key)

        try:
            # ── 3. Build the prompt ──────────────────────────────
            # System prompt + capped thread history + new user message.
            # History is not mutated here — commit_turn persists it
            # only after a successful round-trip.
            messages = self._prompt.messages_for_inference(thread_key, request.text)

            logger.info(
                "Prompt built",
                extra={
                    "request_id": request.request_id,
                    "thread_ts": thread_key,
                    "history_length": len(messages) - 1,
                },
            )

            # ── 4. Run inference ─────────────────────────────────
            # With tools: multi-turn agent loop (model ↔ tools).
            # Without tools: single inference call + transcript repair.
            if self._tools and len(self._tools) > 0:
                content = await self._run_agent_loop(messages, request.request_id, on_status)
            else:
                content = await self._inference_with_repair(messages, request.request_id)

            # ── 5. Gate the final text response ──────────────────
            # The approval gate can also inspect the assistant's text
            # (e.g. to block sensitive content).  In practice the
            # AutoApproval gate always approves here.
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

            # ── 6. Persist and return ────────────────────────────
            # Only commit the turn to history after everything succeeded
            # so failed requests never pollute the conversation.
            self._prompt.commit_turn(thread_key, request.text, content)

            logger.info(
                "Request completed",
                extra={
                    "request_id": request.request_id,
                    "latency_ms": round(timer.ms, 1),
                },
            )

            return self._shape_response(request, content)

        # ── Error handling ───────────────────────────────────────

        except WriteApprovalError as exc:
            # A write tool was blocked by the approval gate.  Save
            # the conversation state so it can resume after the user
            # clicks Approve, and return an approval prompt.
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
            # Classified backend failure (auth, rate limit, timeout, etc.).
            # Map to a user-friendly message without exposing internals.
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
            # Catch-all for unexpected errors.  Logged with full
            # traceback; the user sees a generic apology.
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
    # Tool-use agent loop
    # ------------------------------------------------------------------

    async def _run_agent_loop(
        self,
        messages: list[Message],
        request_id: str,
        on_status: StatusCallback | None = None,
    ) -> str:
        """Run the multi-turn tool-use loop.

        Calls the inference backend with tool definitions.  If the model
        responds with ``tool_calls``, executes each tool, appends results
        as ``tool`` role messages, and re-invokes the model.  Continues
        until the model produces a text response or ``MAX_TOOL_ROUNDS``
        is reached.

        Args:
            messages: Initial message list (system + history + user).
            request_id: Correlation ID for structured logging.
            on_status: Optional callback for thinking-indicator updates.

        Returns:
            The model's final text content.

        Raises:
            WriteApprovalError: When a write tool is blocked by the
                approval gate.  The caller saves the pending state and
                returns an approval prompt to the user.
        """
        if self._tools is None:
            raise RuntimeError("_run_agent_loop called without a ToolRegistry")

        # Snapshot tool definitions once — they don't change mid-request.
        tool_defs = self._tools.tool_definitions()

        # Shallow-copy messages so the caller's list is not mutated
        # as we append assistant / tool messages during the loop.
        working_messages = [dict(m) for m in messages]

        for round_num in range(MAX_TOOL_ROUNDS):
            # ── A. Call the model with the current conversation ───
            inference_request = InferenceRequest(
                messages=working_messages,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                request_id=request_id,
                tools=tool_defs if tool_defs else None,
            )
            result = await self._backend.complete(inference_request)

            logger.info(
                "Agent loop inference call",
                extra={
                    "request_id": request_id,
                    "round": round_num,
                    "finish_reason": result.finish_reason,
                    "has_tool_calls": bool(result.tool_calls),
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                },
            )

            # ── B. Terminal condition: model produced text ────────
            # No tool calls → the model is done.  Handle truncation
            # (finish_reason="length") or apply transcript repair,
            # then return the final text.
            if not result.tool_calls:
                if result.finish_reason == "length":
                    return await self._continue_truncated(working_messages, result, request_id)
                repair = repair_response(result, request_id)
                return repair.content

            # ── C. Approval gate for write tools ─────────────────
            # Pre-scan the batch for write operations.  If any are
            # denied, save the full conversation state and raise so
            # the caller can present Approve / Deny buttons.
            write_calls = self._get_write_tool_calls(result.tool_calls)
            if write_calls and await self._needs_write_approval(write_calls, request_id):
                assistant_msg = self._build_assistant_tool_message(result)
                raise WriteApprovalError(
                    PendingApproval(
                        tool_calls=list(result.tool_calls),
                        working_messages=list(working_messages),
                        assistant_message=assistant_msg,
                        request_id=request_id,
                        description=self._format_write_description(write_calls),
                    )
                )

            # ── D. Execute tools and feed results back ───────────
            # Record the assistant's tool-call message, run each tool,
            # and append tool-result messages.  The next iteration
            # sends the updated conversation back to the model.
            assistant_msg = self._build_assistant_tool_message(result)
            working_messages.append(assistant_msg)

            tool_results = await self._execute_tool_calls(result.tool_calls, request_id, on_status)
            working_messages.extend(tool_results)

        # ── E. Safety limit reached ──────────────────────────────
        # The model kept calling tools without producing a final text
        # response.  Return a graceful partial answer.
        logger.warning(
            "Agent loop hit max tool rounds",
            extra={"request_id": request_id, "max_rounds": MAX_TOOL_ROUNDS},
        )
        return (
            "I've been working on your request but reached the maximum number "
            "of tool calls. Here's what I've gathered so far — please ask a "
            "follow-up question if you need more."
        )

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        request_id: str,
        on_status: StatusCallback | None = None,
    ) -> list[Message]:
        """Execute a batch of tool calls and return tool-result messages.

        Each tool is invoked sequentially.  Before execution the
        ``on_status`` callback is fired so the connector can update
        its thinking indicator. Exceptions from individual tools are
        caught and serialised as JSON error objects so the model can
        reason about the failure.

        Args:
            tool_calls: Tool invocations from the model's response.
            request_id: Correlation ID for structured logging.
            on_status: Optional callback for thinking-indicator updates.

        Returns:
            A list of ``tool`` role message dicts, one per tool call.
        """
        if self._tools is None:
            raise RuntimeError("_execute_tool_calls called without a ToolRegistry")

        results: list[Message] = []
        for tc in tool_calls:
            if on_status:
                display = self._tools.display_name(tc.name)
                try:
                    await on_status(f"{display}...")
                except Exception:
                    logger.debug("Status callback failed", exc_info=True)

            tool_timer = Timer()
            try:
                output = await self._tools.execute(tc.name, tc.arguments)
                logger.info(
                    "Tool call succeeded",
                    extra={
                        "request_id": request_id,
                        "tool": tc.name,
                        "tool_call_id": tc.id,
                        "duration_ms": round(tool_timer.ms, 1),
                    },
                )
            except Exception as exc:
                output = json.dumps(
                    {
                        "error": str(exc),
                        "tool": tc.name,
                    }
                )
                logger.error(
                    "Tool call failed",
                    extra={
                        "request_id": request_id,
                        "tool": tc.name,
                        "tool_call_id": tc.id,
                        "duration_ms": round(tool_timer.ms, 1),
                    },
                    exc_info=True,
                )

            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output,
                }
            )

        return results

    @staticmethod
    def _build_assistant_tool_message(result: InferenceResponse) -> Message:
        """Build the assistant message that carries tool_calls for the conversation.

        Constructs the OpenAI-format assistant message dict with the
        model's text content (if any) and the tool-call array so the
        conversation history faithfully records what the model emitted.

        Args:
            result: The inference response containing tool calls.

        Returns:
            A ``Message`` dict with role ``assistant``, content, and
            a ``tool_calls`` list in OpenAI wire format.
        """
        msg: Message = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in result.tool_calls
            ]
        return msg

    async def _continue_truncated(
        self,
        messages: list[Message],
        result: InferenceResponse,
        request_id: str,
    ) -> str:
        """Handle ``finish_reason=length`` by appending continuation prompts.

        When the model's output is truncated mid-sentence, this method
        appends the partial content to the conversation, injects a
        continuation prompt, and re-invokes the model up to
        ``MAX_CONTINUATION_RETRIES`` times.  The chunks are concatenated
        into a single response.

        Args:
            messages: Working message list *before* the truncated reply.
            result: The truncated inference response.
            request_id: Correlation ID for structured logging.

        Returns:
            The full concatenated text across all continuation chunks.
        """
        chunks = [result.content]
        working = list(messages)
        working.append({"role": "assistant", "content": result.content})

        for attempt in range(MAX_CONTINUATION_RETRIES):
            working.append({"role": "user", "content": CONTINUATION_PROMPT})
            logger.info(
                "Continuation retry",
                extra={"request_id": request_id, "attempt": attempt + 1},
            )
            cont_request = InferenceRequest(
                messages=working,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                request_id=request_id,
            )
            cont_result = await self._backend.complete(cont_request)
            chunks.append(cont_result.content)

            if cont_result.finish_reason != "length":
                break

            working.append({"role": "assistant", "content": cont_result.content})

        return "".join(chunks)

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
        base_messages: list[dict[str, Any]] = [dict(m) for m in messages]
        prior_continuation_chunks: list[str] = []

        for attempt in range(1 + MAX_CONTINUATION_RETRIES):
            call_messages: list[dict[str, Any]] = list(base_messages)
            for chunk in prior_continuation_chunks:
                call_messages.append({"role": "assistant", "content": chunk})
                call_messages.append({"role": "user", "content": CONTINUATION_PROMPT})

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

            if repair.was_repaired and not repair.needs_continuation:
                return repair.content

            if not repair.needs_continuation:
                return "".join(prior_continuation_chunks) + repair.content

            prior_continuation_chunks.append(repair.content)

        logger.warning(
            "Exhausted continuation retries, returning partial content",
            extra={"request_id": request_id},
        )
        return "".join(prior_continuation_chunks)

    # ------------------------------------------------------------------
    # Write-approval helpers
    # ------------------------------------------------------------------

    def _get_write_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Return the subset of tool calls targeting non-read-only tools.

        Args:
            tool_calls: All tool calls from the model's response.

        Returns:
            Only those ``ToolCall`` objects whose ``ToolSpec.is_read_only``
            is ``False``.
        """
        if self._tools is None:
            raise RuntimeError("_get_write_tool_calls called without a ToolRegistry")

        writes: list[ToolCall] = []
        for tc in tool_calls:
            spec = self._tools.get(tc.name)
            if spec and not spec.is_read_only:
                writes.append(tc)

        return writes

    async def _needs_write_approval(
        self,
        write_calls: list[ToolCall],
        request_id: str,
    ) -> bool:
        """Check the approval gate for write tools.

        Iterates through *write_calls* and consults the approval gate
        for each. Returns as soon as any single call is denied.

        Args:
            write_calls: Write-only tool calls to check.
            request_id: Correlation ID for structured logging.

        Returns:
            ``True`` if at least one tool call was denied (approval
            is needed); ``False`` if all were approved.
        """
        for tc in write_calls:
            result = await self._approval.check(
                "tool_call",
                {
                    "tool_name": tc.name,
                    "arguments": tc.arguments,
                    "is_read_only": False,
                    "request_id": request_id,
                },
            )
            if not result.approved:
                return True

        return False

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

        If the resumed loop triggers *another* write approval, the new
        pending state replaces the old one and a fresh approval prompt
        is returned.

        Args:
            request: The Approve button-click request.
            thread_key: Conversation thread identifier.
            on_status: Optional callback for thinking-indicator updates.

        Returns:
            A ``RichResponse`` with the model's post-approval reply, or
            a new approval prompt if a cascading write was detected.
        """
        # 1. Pop the saved state.  If the user clicks Approve after the
        #    pending approval was already cleared (e.g. by a new message),
        #    return a harmless "nothing to do" response.
        pending = self._pending_approvals.pop(thread_key, None)
        if not pending:
            return self._shape_response(
                request, "No pending write operation found for this thread."
            )

        logger.info(
            "User approved write operation",
            extra={
                "request_id": pending.request_id,
                "thread_key": thread_key,
                "tool_calls": [tc.name for tc in pending.tool_calls],
            },
        )

        # 2. Rebuild the conversation from the snapshot. The assistant
        #    message (which contains the model's tool_calls) was stored
        #    separately so it can be appended after the approval.
        pending.working_messages.append(pending.assistant_message)

        # 3. Execute the tools that were blocked.  These run without a
        #    second approval check — the user already said "yes".
        tool_results = await self._execute_tool_calls(
            pending.tool_calls, pending.request_id, on_status
        )
        pending.working_messages.extend(tool_results)

        # 4. Resume the agent loop. The model sees the tool results and
        #    should produce a final text response.  If it calls *another*
        #    write tool, the loop raises WriteApprovalError again and
        #    we replace the pending state with the new one.
        try:
            content = await self._run_agent_loop(
                pending.working_messages, pending.request_id, on_status
            )
        except WriteApprovalError as exc:
            exc.pending.original_user_text = pending.original_user_text
            self._pending_approvals[thread_key] = exc.pending
            return self._build_approval_response(request, exc.pending)

        # 5. Commit the full turn (original user text + final model reply)
        #    to conversation history and return.
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
        """Build a ``RichResponse`` with Approve / Deny buttons for a blocked write.

        The response contains a ``TextBlock`` describing the proposed
        action and an ``ActionBlock`` with two buttons whose
        ``action_id`` values match the module-level approval constants.

        Args:
            request: The original inbound request (used for channel
                and thread context).
            pending: The saved approval state including the
                human-readable description of the blocked action.

        Returns:
            A ``RichResponse`` ready for the connector to render.
        """
        text = (
            ":warning: *Write operation requires your approval*\n\n"
            f"{pending.description}\n\n"
            "Click *Approve* to proceed or *Deny* to cancel."
        )
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

    def _format_write_description(self, write_calls: list[ToolCall]) -> str:
        """Render a human-readable summary of blocked write tool calls.

        Produces a Slack-mrkdwn-formatted string with the display name
        of each tool and a bulleted list of its arguments. Long values
        are truncated at 200 characters.

        Args:
            write_calls: The write tool calls that were blocked.

        Returns:
            Formatted description string for the approval prompt.
        """
        parts: list[str] = []
        for tc in write_calls:
            display = self._tools.display_name(tc.name) if self._tools else tc.name
            try:
                args: dict[str, Any] = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}
            lines = [f"*{display}*"]
            for key, value in args.items():
                if value:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "…"
                    lines.append(f"  • {key}: {val_str}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Response shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _shape_response(request: NormalizedRequest, content: str) -> RichResponse:
        """Wrap a text string in a ``RichResponse`` addressed to the request's thread.

        Args:
            request: The inbound request (provides channel and thread).
            content: Markdown text for the response body.

        Returns:
            A single-block ``RichResponse`` containing the text.
        """
        return RichResponse(
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            blocks=[TextBlock(text=content)],
        )

    @staticmethod
    def _error_response(request: NormalizedRequest, category: ErrorCategory) -> RichResponse:
        """Build a user-friendly error response for a classified failure.

        Maps each ``ErrorCategory`` to a pre-written message so the
        user never sees raw stack traces or error codes.

        Args:
            request: The inbound request (provides channel and thread).
            category: The classified error type.

        Returns:
            A ``RichResponse`` with a human-readable error message and
            the ``error_category`` field set (connectors may use this
            for rate limiting).
        """
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
