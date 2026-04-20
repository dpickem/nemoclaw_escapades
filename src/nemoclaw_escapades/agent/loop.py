"""AgentLoop — reusable multi-turn inference + tool execution loop.

Layer 1 of the three-layer agent architecture (see design_m2.md §4.7).
The loop is role-agnostic: it knows nothing about Slack, NMB, or
OpenShell.  Role-specific behaviour (approval UX, delegation, event
handling) lives in the layers above.

Both the orchestrator and future sub-agents (coding, review) share
this loop — they differ only in which tools, approval gate, and audit
backend they inject.

The two-list message model
---------------------------

NemoClaw uses **two separate message lists** that should not be
confused:

1. ``LayeredPromptBuilder._thread_history`` — long-lived per-thread
   state, persisted across requests.  Contains only ``user`` and
   ``assistant`` role messages (the final assistant text per turn).
   Owned by ``agent/prompt_builder.py``.
2. ``AgentLoop``'s ``working_messages`` — ephemeral, local to a single
   ``run()`` call.  Seeded with what the prompt builder produced
   (``[system, …history…, user]``), it then grows as tool calls execute.

This module owns #2.  On every round where the model returns
``tool_calls``:

- ``_build_assistant_tool_message`` converts the response into an
  ``assistant`` message carrying the requested ``tool_calls`` and
  appends it to ``working_messages``.
- ``execute_tool_calls`` runs the tools (concurrent for
  ``is_concurrency_safe=True``, sequential for the rest) and emits one
  ``{"role": "tool", "tool_call_id": ..., "content": ...}`` message per
  result, appended to ``working_messages`` so the next inference call
  sees them.

Tool results are matched back to their requesting ``tool_call`` via the
``tool_call_id`` field — this is the OpenAI / Anthropic tool-calling
protocol.  Before each inference call, ``ContextCompactor`` (see
``agent/compaction.py``) micro-compacts oversized ``tool``-role messages
so a giant ``read_file`` doesn't blow the context window within the
current run.

When the loop terminates (model returns text with no ``tool_calls``),
``run()`` returns ``AgentLoopResult(content=..., working_messages=...)``.
The orchestrator reads ``result.content`` (the final assistant text) and
calls ``prompt.commit_turn(thread_key, user_text, result.content)``.  The
tool-call round-trips in ``working_messages`` are **intentionally not
written back to thread history** — otherwise every subsequent turn in
that thread would drag along every previous turn's tool outputs.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from nemoclaw_escapades.agent.approval import ApprovalGate, AutoApproval
from nemoclaw_escapades.agent.compaction import ContextCompactor
from nemoclaw_escapades.agent.types import (
    AgentLoopResult,
    ToolEndCallback,
    ToolStartCallback,
)
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    Message,
    MessageRole,
    PendingApproval,
    ToolCall,
)
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.observability.timer import Timer
from nemoclaw_escapades.orchestrator.transcript_repair import (
    CONTINUATION_PROMPT,
    repair_response,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nemoclaw_escapades.audit.db import AuditDB

logger = get_logger("agent.loop")


class WriteApprovalError(Exception):
    """Raised when a write tool requires user approval before execution.

    The caller (typically the orchestrator) catches this, saves the
    ``pending`` state, and presents approval UI to the user.
    """

    def __init__(self, pending: PendingApproval) -> None:
        """Wrap the pending approval state for propagation to the orchestrator.

        Args:
            pending: Snapshot of the conversation at the point the
                write was blocked — includes working messages, the
                assistant's tool-call message, and a human-readable
                description of the proposed write.
        """
        self.pending = pending


class AgentLoop:
    """Multi-turn inference + tool execution loop.

    Runs a model ↔ tool cycle until the model produces a text response
    or the safety limit (``config.max_tool_rounds``) is reached.  Each
    invocation of ``run()`` is stateless — all mutable state (messages,
    counters) is local to the call.

    Injected dependencies:

    - **backend** — inference provider (``BackendBase``).
    - **tools** — tool registry with definitions and handlers.
    - **config** — model, temperature, limits.
    - **audit** — optional audit DB for tool-call logging.
    - **approval** — optional gate checked before write tools.
    - **on_tool_start / on_tool_end** — optional callbacks for
      thinking-indicator updates and telemetry.
    """

    def __init__(
        self,
        backend: BackendBase,
        tools: ToolRegistry,
        config: AgentLoopConfig,
        audit: AuditDB | None = None,
        approval: ApprovalGate | None = None,
        on_tool_start: ToolStartCallback | None = None,
        on_tool_end: ToolEndCallback | None = None,
    ) -> None:
        """Initialise the loop with its injected dependencies.

        Args:
            backend: Inference provider used for chat-completion calls.
            tools: Registry of available tool definitions and handlers.
            config: Model parameters and safety limits (max rounds,
                continuation retries, temperature, etc.).
            audit: Optional audit database.  When provided, every tool
                invocation is logged with service, args, latency, and
                success/failure status.
            approval: Gate consulted before executing write tools.
                Defaults to ``AutoApproval`` (allow everything).
                Inject ``WriteApproval`` for interactive approval.
            on_tool_start: Instance-level default callback invoked
                before each tool execution.  Per-request callbacks
                passed to ``run()`` take precedence.
            on_tool_end: Callback invoked after each tool execution
                with timing and outcome.
        """
        self._backend = backend
        self._tools = tools
        self._config = config
        self._audit = audit
        self._approval = approval or AutoApproval()
        self._on_tool_start = on_tool_start
        self._on_tool_end = on_tool_end
        self._compactor = ContextCompactor(backend, config)

    async def run(
        self,
        messages: list[Message],
        request_id: str,
        thread_ts: str | None = None,
        on_tool_start: ToolStartCallback | None = None,
    ) -> AgentLoopResult:
        """Run the multi-turn tool-use loop.

        Calls the inference backend with tool definitions.  If the model
        responds with ``tool_calls``, executes each tool, appends results
        as ``tool`` role messages, and re-invokes the model.  Continues
        until the model produces a text response or
        ``config.max_tool_rounds`` is reached.

        The caller provides the prompt-builder-produced message list
        (``[system, …history…, user]``).  This method copies that list
        into a local ``working_messages`` and appends
        ``assistant``-with-``tool_calls`` and ``tool``-role messages to
        it as the loop progresses.  The caller's original list is never
        mutated.  The returned ``working_messages`` is the ephemeral
        per-run log — the caller is expected to take
        ``result.content`` (the final text-only reply) and hand *only
        that* to ``LayeredPromptBuilder.commit_turn`` for persistence.
        See the module docstring for the rationale.

        Args:
            messages: Initial message list (system + history + user).
            request_id: Correlation ID for structured logging.
            thread_ts: Optional thread timestamp for audit correlation.
            on_tool_start: Per-request callback invoked before each tool
                execution (e.g. to update a thinking indicator).  Takes
                precedence over the instance-level ``_on_tool_start``.
                Passed as a parameter (not shared state) so concurrent
                requests don't clobber each other's callbacks.

        Returns:
            An ``AgentLoopResult`` with the final text, round/tool
            counters, and the full working message list (including every
            ``assistant``-with-``tool_calls`` and ``tool``-role message
            that was appended during the run — useful for debugging and
            approval-resume flows, *not* for writing back to thread
            history).

        Raises:
            WriteApprovalError: When a write tool is blocked by the
                approval gate.
        """
        # Snapshot tool definitions once — they don't change within a run.
        tool_defs = self._tools.tool_definitions()

        # Shallow-copy so callers' original list is never mutated.
        # We append assistant/tool messages to this as the loop progresses.
        working_messages: list[Message] = [dict(m) for m in messages]
        total_tool_calls = 0

        # Core loop: call the model, check the response, execute tools, repeat.
        # The safety cap prevents infinite spirals if the model never stops
        # emitting tool calls (e.g. cyclic tool dependencies or hallucinated
        # tool names that always error).
        for round_num in range(self._config.max_tool_rounds):
            # Micro-compaction: truncate oversized tool results (zero
            # cost — pure string slicing, no API call).  Applied before
            # every inference round so the context window stays healthy.
            working_messages = self._compactor.truncate_tool_results(working_messages)

            # Full compaction: when total message chars exceed the
            # threshold, summarize the oldest half via a dedicated
            # inference call and session-roll.
            if self._compactor.should_compact(working_messages):
                working_messages = await self._compactor.compact(working_messages, request_id)

            inference_request = InferenceRequest(
                messages=working_messages,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                request_id=request_id,
                # Pass None (not []) when there are no tools — some backends
                # treat an empty list differently from no tools at all.
                tools=tool_defs if tool_defs else None,
            )
            result = await self._backend.complete(inference_request)

            logger.info(
                "Agent loop round",
                extra={
                    "request_id": request_id,
                    "round": round_num,
                    "finish_reason": result.finish_reason,
                    "has_tool_calls": bool(result.tool_calls),
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                },
            )

            # ── Terminal condition: model produced text, no tool calls ──
            # This is the happy-path exit from the loop.
            if not result.tool_calls:
                # finish_reason="length" means the model ran out of tokens
                # mid-sentence — re-prompt with a continuation request so
                # the user gets a complete answer.
                if result.finish_reason == "length":
                    content = await self._continue_truncated(working_messages, result, request_id)
                else:
                    # Handles empty replies, content-filter blocks, etc.
                    repair = repair_response(result, request_id)
                    content = repair.content

                return AgentLoopResult(
                    content=content,
                    tool_calls_made=total_tool_calls,
                    rounds=round_num + 1,
                    hit_safety_limit=False,
                    working_messages=working_messages,
                )

            # ── Write-approval gate ────────────────────────────────────
            # If any requested tool is a write operation, pause the loop
            # and surface Approve/Deny to the user.  The orchestrator
            # catches WriteApprovalError, saves the full conversation
            # state (working_messages + the assistant's tool-call message),
            # and resumes here after the user clicks Approve.
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

            # ── Execute tool calls and feed results back ───────────────
            # The OpenAI protocol requires the assistant's tool-call
            # message to precede the tool-result messages in the
            # conversation — otherwise the model cannot correlate results
            # back to its requests via tool_call_id.
            assistant_msg = self._build_assistant_tool_message(result)
            working_messages.append(assistant_msg)

            tool_results = await self.execute_tool_calls(
                result.tool_calls,
                request_id,
                thread_ts=thread_ts,
                on_tool_start=on_tool_start,
            )
            working_messages.extend(tool_results)
            total_tool_calls += len(result.tool_calls)

        # ── Safety-limit exit ──────────────────────────────────────────
        # If we exhaust all rounds without the model producing a final
        # text response, return a partial answer rather than looping
        # forever.  This is a backstop — well-prompted models rarely
        # hit it.
        logger.warning(
            "Agent loop hit max tool rounds",
            extra={
                "request_id": request_id,
                "max_rounds": self._config.max_tool_rounds,
            },
        )
        return AgentLoopResult(
            content=(
                "I've been working on your request but reached the maximum number "
                "of tool calls. Here's what I've gathered so far — please ask a "
                "follow-up question if you need more."
            ),
            tool_calls_made=total_tool_calls,
            rounds=self._config.max_tool_rounds,
            hit_safety_limit=True,
            working_messages=working_messages,
        )

    async def execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        request_id: str,
        thread_ts: str | None = None,
        on_tool_start: ToolStartCallback | None = None,
    ) -> list[Message]:
        """Execute a batch of tool calls and return tool-result messages.

        Concurrency-safe tools (``ToolSpec.is_concurrency_safe=True``)
        run in parallel via ``asyncio.gather``.  Unsafe tools run
        sequentially afterwards so workspace mutations are serialized.

        Results are returned in the **same order** as ``tool_calls`` —
        the OpenAI protocol requires each tool-result message to appear
        in a stable position matching the assistant's tool-call message.

        Public so the orchestrator can call this during approval-resume
        flows (execute the previously-blocked tools, then call ``run()``
        to continue the loop).

        Args:
            tool_calls: Tool invocations from the model's response.
            request_id: Correlation ID for structured logging.
            thread_ts: Optional thread timestamp for audit correlation.
            on_tool_start: Per-request callback, takes precedence over
                the instance-level default.

        Returns:
            A list of ``tool`` role messages, one per tool call, in the
            same order as the input.
        """
        # Per-request callback takes precedence over the instance default.
        # This avoids shared mutable state — critical for concurrent
        # requests whose callbacks target different Slack channels.
        effective_on_tool_start = on_tool_start or self._on_tool_start

        # Partition by is_concurrency_safe.  Unknown tool names (spec is
        # None) are treated as safe — they'll fail at execute time with a
        # clear error, which is better than blocking the whole batch on a
        # sequential run for a hallucinated tool.
        safe_indices: list[int] = []
        unsafe_indices: list[int] = []
        for i, tc in enumerate(tool_calls):
            spec = self._tools.get(tc.name)
            if spec is None or spec.is_concurrency_safe:
                safe_indices.append(i)
            else:
                unsafe_indices.append(i)

        # Slot-based reassembly: each tool call has a fixed output slot
        # so the final list matches input order even after parallel
        # execution reorders completions.
        results: list[Message | None] = [None] * len(tool_calls)

        # ── Concurrent batch (safe tools) ──────────────────────────────
        # Fire all safe tools at once via asyncio.gather.  Thinking-
        # indicator callbacks fire upfront so the user sees them in
        # input order, not the completion-order (which would be jittery).
        if safe_indices:
            for i in safe_indices:
                await self._notify_tool_start(tool_calls[i], effective_on_tool_start)

            safe_results = await asyncio.gather(
                *(self._execute_one(tool_calls[i], request_id, thread_ts) for i in safe_indices),
                return_exceptions=False,
            )
            for i, msg in zip(safe_indices, safe_results):
                results[i] = msg

        # ── Sequential tail (unsafe tools) ─────────────────────────────
        # Workspace mutations (write_file, bash, git_commit, etc.) must
        # serialize to avoid races on the filesystem.
        for i in unsafe_indices:
            await self._notify_tool_start(tool_calls[i], effective_on_tool_start)
            results[i] = await self._execute_one(tool_calls[i], request_id, thread_ts)

        # Defensive: every slot should be populated at this point
        # (``_execute_one`` never raises — errors return an error-payload
        # Message).  But if a slot somehow stayed ``None`` we MUST NOT
        # drop it: the OpenAI tool-call protocol requires exactly one
        # ``tool``-role message per assistant ``tool_call``, keyed by
        # ``tool_call_id``.  A dropped slot would leave the next
        # inference call with an unmatched tool_call and the backend
        # would reject the request.  Substitute a synthetic error so
        # the output list always has the same length as the input.
        return [
            r if r is not None else self._synthesize_missing_tool_result(tc)
            for tc, r in zip(tool_calls, results)
        ]

    @staticmethod
    def _synthesize_missing_tool_result(tc: ToolCall) -> Message:
        """Build a placeholder tool-result message for a slot that was never filled.

        Should never be reached in practice — ``_execute_one`` handles
        every exception path.  Acts as belt-and-suspenders so a logic
        bug in partitioning or dispatch can't break the invariant
        "one tool-result per tool-call" that the OpenAI protocol
        requires.

        Args:
            tc: The tool call whose slot was left empty.

        Returns:
            A tool-role message carrying a JSON error payload and the
            correct ``tool_call_id``.
        """
        payload = json.dumps(
            {
                "error": "Internal error: tool dispatch produced no result",
                "tool": tc.name,
            }
        )
        return {"role": MessageRole.TOOL, "tool_call_id": tc.id, "content": payload}

    async def _notify_tool_start(
        self,
        tc: ToolCall,
        callback: ToolStartCallback | None,
    ) -> None:
        """Fire the thinking-indicator callback for a single tool call.

        Failures are swallowed — a flaky connector must never block
        tool execution.
        """
        if callback is None:
            return
        display = self._tools.display_name(tc.name)
        try:
            await callback(display)
        except Exception:
            logger.debug("on_tool_start callback failed", exc_info=True)

    async def _execute_one(
        self,
        tc: ToolCall,
        request_id: str,
        thread_ts: str | None,
    ) -> Message:
        """Execute a single tool call and produce its tool-result message.

        Handles tool dispatch, error serialization, audit logging, and
        the per-tool ``on_tool_end`` telemetry callback.  Never raises —
        errors are serialized as JSON and fed back to the model.

        Args:
            tc: The tool invocation to execute.
            request_id: Correlation ID for structured logging.
            thread_ts: Optional thread timestamp for audit correlation.

        Returns:
            A tool-role message referencing the input's tool_call_id.
        """
        # Look up the spec before execution — needed later for audit
        # metadata even if execute() raises.
        spec = self._tools.get(tc.name)

        # Tell the compactor about this call.  At summary time it uses
        # the (id → name) map to render ``[Tool result (read_file)]``
        # instead of the opaque tool_call_id — no message walking.
        self._compactor.register_tool_call(tc.id, tc.name)

        tool_timer = Timer()
        success = True
        error_msg: str | None = None
        try:
            # The registry handles argument parsing (JSON → kwargs),
            # dispatch, and output truncation (max_result_chars).
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
            # Tool errors are serialized as JSON and fed back to the
            # model — this lets the model explain the failure to the
            # user or try an alternative approach.
            success = False
            error_msg = str(exc)
            output = json.dumps({"error": error_msg, "tool": tc.name})
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

        duration_ms = round(tool_timer.ms, 1)

        # Telemetry callback — used by the orchestrator for latency
        # tracking and by future sub-agents for SLA monitoring.
        if self._on_tool_end:
            try:
                await self._on_tool_end(tc.name, duration_ms, success)
            except Exception:
                logger.debug("on_tool_end callback failed", exc_info=True)

        # Audit logging is fire-and-forget: the background writer
        # batches inserts off the hot path so a slow DB never blocks
        # the tool loop.  Failures are swallowed to avoid poisoning
        # the user-facing conversation.
        if self._audit:
            try:
                await self._audit.log_tool_call(
                    session_id=request_id,
                    thread_ts=thread_ts,
                    service=spec.toolset if spec else "",
                    command=tc.name,
                    args=tc.arguments,
                    operation_type=("READ" if (spec and spec.is_read_only) else "WRITE"),
                    duration_ms=duration_ms,
                    success=success,
                    error_message=error_msg,
                    response_payload=output,
                )
            except Exception:
                logger.debug("Audit log_tool_call failed", exc_info=True)

        # Each tool result must reference the tool_call_id from the
        # assistant message — this is how the model matches results
        # back to the tool invocation it requested.
        return {"role": MessageRole.TOOL, "tool_call_id": tc.id, "content": output}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_assistant_tool_message(result: InferenceResponse) -> Message:
        """Build the assistant message that carries tool_calls for the conversation.

        The OpenAI chat protocol requires this message to appear in the
        conversation before the corresponding tool-result messages.

        Args:
            result: The inference response containing the model's
                content and any tool-call requests.

        Returns:
            A ``Message`` dict with role ``assistant``, the model's
            text content, and a ``tool_calls`` list in OpenAI wire
            format (if the model requested tool calls).
        """
        # The OpenAI chat protocol requires the assistant message with
        # tool_calls to appear in the conversation *before* the
        # corresponding tool-result messages.  We reconstruct it from
        # the InferenceResponse so it matches the wire format exactly.
        msg: Message = {"role": MessageRole.ASSISTANT, "content": result.content or ""}
        if result.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
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
        """Re-prompt the model when ``finish_reason=length`` truncates output.

        Appends the partial response and a continuation prompt, then
        calls the backend again (up to ``max_continuation_retries``
        times).  All chunks are stitched into one seamless string.

        Args:
            messages: The message list at the point of truncation
                (not modified — a local copy is used).
            result: The truncated inference response.
            request_id: Correlation ID for structured logging.

        Returns:
            The concatenated text from the initial truncated response
            and all continuation chunks.
        """
        chunks = [result.content]
        # Build a separate working list — we don't want continuation
        # scaffolding (assistant partial + "please continue" user msgs)
        # to leak into the caller's message history.
        working: list[Message] = list(messages)
        working.append({"role": MessageRole.ASSISTANT, "content": result.content})

        for attempt in range(self._config.max_continuation_retries):
            # Append a nudge asking the model to pick up where it left off.
            working.append({"role": MessageRole.USER, "content": CONTINUATION_PROMPT})
            logger.info(
                "Continuation retry",
                extra={"request_id": request_id, "attempt": attempt + 1},
            )
            # Continuation calls omit tool definitions — we only want
            # text output, not new tool invocations.
            cont_request = InferenceRequest(
                messages=working,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
                request_id=request_id,
            )
            cont_result = await self._backend.complete(cont_request)
            chunks.append(cont_result.content)

            # If the model finished naturally this time, we're done.
            # Otherwise, stack another round of partial output.
            if cont_result.finish_reason != "length":
                break
            working.append({"role": MessageRole.ASSISTANT, "content": cont_result.content})

        # Stitch all chunks into one seamless response — the model is
        # instructed to continue mid-sentence, so the seams should be
        # invisible to the user.
        return "".join(chunks)

    def _get_write_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Filter *tool_calls* to only those targeting non-read-only tools.

        Args:
            tool_calls: All tool invocations from the model's response.

        Returns:
            Subset of *tool_calls* whose ``ToolSpec.is_read_only`` is
            ``False``.  Unknown tool names (spec not found) are
            excluded — they'll fail at execute time instead.
        """
        writes: list[ToolCall] = []
        for tc in tool_calls:
            spec = self._tools.get(tc.name)
            # Unknown tools (spec is None) are treated as reads — they'll
            # fail at execute time with KeyError, which is safer than
            # falsely blocking on approval for a hallucinated tool name.
            if spec and not spec.is_read_only:
                writes.append(tc)
        return writes

    async def _needs_write_approval(self, write_calls: list[ToolCall], request_id: str) -> bool:
        """Check the approval gate for write tools.

        Short-circuits on the first denial — if any write is blocked,
        the entire batch is paused for approval together.

        Args:
            write_calls: Write-only tool calls to check.
            request_id: Correlation ID forwarded to the gate.

        Returns:
            ``True`` if at least one tool call was denied by the gate,
            ``False`` if all were approved.
        """
        # Short-circuit on the first denied call.  We don't need to
        # check every write — if any is blocked, the entire batch is
        # paused and presented for approval together.
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

    def _format_write_description(self, write_calls: list[ToolCall]) -> str:
        """Render a human-readable summary of blocked write tool calls.

        Shown in the Slack approval prompt so the user knows exactly
        what will happen if they click Approve.  Long argument values
        (e.g. MR descriptions) are truncated to 200 characters.

        Args:
            write_calls: The write tool calls to describe.

        Returns:
            Slack mrkdwn-formatted string with one section per tool
            call, each listing the display name and key arguments.
        """
        parts: list[str] = []
        for tc in write_calls:
            display = self._tools.display_name(tc.name)
            try:
                args: dict[str, Any] = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}
            lines = [f"*{display}*"]
            for key, value in args.items():
                if value:
                    # Truncate long values (e.g. MR descriptions or
                    # comment bodies) to keep the approval prompt readable.
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "…"
                    lines.append(f"  • {key}: {val_str}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)
