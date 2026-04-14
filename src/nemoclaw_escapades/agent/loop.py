"""AgentLoop — reusable multi-turn inference + tool execution loop.

Layer 1 of the three-layer agent architecture (see design_m2.md §4.7).
The loop is role-agnostic: it knows nothing about Slack, NMB, or
OpenShell.  Role-specific behaviour (approval UX, delegation, event
handling) lives in the layers above.

Both the orchestrator and future sub-agents (coding, review) share
this loop — they differ only in which tools, approval gate, and audit
backend they inject.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nemoclaw_escapades.agent.approval import ApprovalGate, AutoApproval
from nemoclaw_escapades.agent.types import (
    AgentLoopConfig,
    AgentLoopResult,
    ToolEndCallback,
    ToolStartCallback,
)
from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    Message,
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
        self._backend = backend
        self._tools = tools
        self._config = config
        self._audit = audit
        self._approval = approval or AutoApproval()
        self._on_tool_start = on_tool_start
        self._on_tool_end = on_tool_end

    async def run(
        self,
        messages: list[Message],
        request_id: str,
        thread_ts: str | None = None,
    ) -> AgentLoopResult:
        """Run the multi-turn tool-use loop.

        Calls the inference backend with tool definitions.  If the model
        responds with ``tool_calls``, executes each tool, appends results
        as ``tool`` role messages, and re-invokes the model.  Continues
        until the model produces a text response or
        ``config.max_tool_rounds`` is reached.

        Args:
            messages: Initial message list (system + history + user).
            request_id: Correlation ID for structured logging.
            thread_ts: Optional thread timestamp for audit correlation.

        Returns:
            An ``AgentLoopResult`` with the final text, round/tool
            counters, and the full working message list.

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
                result.tool_calls, request_id, thread_ts=thread_ts
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
    ) -> list[Message]:
        """Execute a batch of tool calls and return tool-result messages.

        Public so the orchestrator can call this during approval-resume
        flows (execute the previously-blocked tools, then call ``run()``
        to continue the loop).

        Args:
            tool_calls: Tool invocations from the model's response.
            request_id: Correlation ID for structured logging.
            thread_ts: Optional thread timestamp for audit correlation.

        Returns:
            A list of ``tool`` role messages, one per tool call.
        """
        results: list[Message] = []
        for tc in tool_calls:
            # Notify the connector so it can update the thinking indicator
            # in real time (e.g. "Searching Jira…").  Failures here must
            # not block tool execution.
            if self._on_tool_start:
                display = self._tools.display_name(tc.name)
                try:
                    await self._on_tool_start(display)
                except Exception:
                    logger.debug("on_tool_start callback failed", exc_info=True)

            # Look up the spec before execution — needed later for audit
            # metadata even if execute() raises.
            spec = self._tools.get(tc.name)
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
            results.append({"role": "tool", "tool_call_id": tc.id, "content": output})

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_assistant_tool_message(result: InferenceResponse) -> Message:
        """Build the assistant message that carries tool_calls for the conversation."""
        # The OpenAI chat protocol requires the assistant message with
        # tool_calls to appear in the conversation *before* the
        # corresponding tool-result messages.  We reconstruct it from
        # the InferenceResponse so it matches the wire format exactly.
        msg: Message = {"role": "assistant", "content": result.content or ""}
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
        """Re-prompt the model when ``finish_reason=length`` truncates output."""
        chunks = [result.content]
        # Build a separate working list — we don't want continuation
        # scaffolding (assistant partial + "please continue" user msgs)
        # to leak into the caller's message history.
        working: list[Message] = list(messages)
        working.append({"role": "assistant", "content": result.content})

        for attempt in range(self._config.max_continuation_retries):
            # Append a nudge asking the model to pick up where it left off.
            working.append({"role": "user", "content": CONTINUATION_PROMPT})
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
            working.append({"role": "assistant", "content": cont_result.content})

        # Stitch all chunks into one seamless response — the model is
        # instructed to continue mid-sentence, so the seams should be
        # invisible to the user.
        return "".join(chunks)

    def _get_write_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Return tool calls targeting non-read-only tools."""
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

        Returns ``True`` if at least one tool call was denied.
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

        This text is shown in the Slack approval prompt so the user
        knows exactly what will happen if they click Approve.
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
