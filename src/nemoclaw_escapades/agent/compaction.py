"""Two-tier context compaction for long agent conversations.

Implements the compaction strategy described in design_m2a.md §6, drawing
from both Claude Code's three-tier model and the BYOO tutorial's
``ContextGuard`` pattern.

**Micro-compaction** (Tier 1) — truncates tool results that exceed a
configurable char limit.  Applied before every inference call at zero
cost (no API call).  Handles the common case of ``bash`` or ``grep``
returning massive output.

**Full compaction** (Tier 2) — when total message chars exceed a
threshold, the oldest ~50% of messages are summarized via a dedicated
inference call and replaced with synthetic summary messages.  The newest
~20% are kept verbatim to preserve recent context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nemoclaw_escapades.models.types import InferenceRequest, Message, MessageRole
from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.backends.base import BackendBase
    from nemoclaw_escapades.config import AgentLoopConfig

logger = get_logger("agent.compaction")

# Minimum number of conversation messages required before compaction
# is worth attempting (need at least a head to summarize and a tail
# to keep).
_MIN_MESSAGES_FOR_COMPACTION: int = 2

# Floor on how many messages the compressible head must contain —
# summarizing fewer than this produces a summary longer than the input.
_MIN_COMPRESS_COUNT: int = 2

# Max chars of a tool result included in the summary transcript.
# Keeps the summary inference call's input from exploding when a
# tool returned a large payload.
_MAX_TOOL_RESULT_IN_SUMMARY: int = 500

# Annotation appended to truncated tool results so the model knows
# the original was larger.
_TRUNCATION_NOTICE: str = "\n\n[Truncated — original: {} chars]"

# System prompt for the compaction summary inference call.
_SUMMARY_SYSTEM_PROMPT: str = (
    "You are a concise summarizer. Summarize the following conversation "
    "excerpt, preserving all key facts, decisions, tool results, and "
    "action items. Do not add commentary — just summarize."
)

# Temperature for the compaction summary call (deterministic).
_SUMMARY_TEMPERATURE: float = 0.0

# Max tokens for the summary response.
_SUMMARY_MAX_TOKENS: int = 4096


class ContextCompactor:
    """Two-tier context compaction engine.

    Injected into ``AgentLoop`` and called on every round.  Micro-compaction
    is synchronous (pure string slicing); full compaction is async (one
    inference call for the summary).

    Attributes:
        micro_limit: Char limit for individual tool results.
        threshold_chars: Total message chars that trigger full compaction.
        compress_ratio: Fraction of oldest messages to summarize.
        min_keep: Minimum messages to keep verbatim after compaction.
    """

    def __init__(
        self,
        backend: BackendBase,
        config: AgentLoopConfig,
    ) -> None:
        """Initialise the compactor.

        Args:
            backend: Inference backend used for the summary call.
            config: Agent loop config with compaction parameters.
        """
        self._backend = backend
        self.micro_limit = config.micro_compaction_chars
        self.threshold_chars = config.compaction_threshold_chars
        self.compress_ratio = config.compaction_compress_ratio
        self.min_keep = config.compaction_min_keep
        self._model = config.compaction_model or config.model

        # Tool-call-id → function-name map.  ``AgentLoop`` populates this
        # via ``register_tool_call`` at dispatch time, so the summary
        # transcript can render ``[Tool result (read_file)]`` instead of
        # ``[Tool result (call_abc123)]`` without walking the message
        # history at compaction time.
        self._tool_names_by_id: dict[str, str] = {}

    def register_tool_call(self, call_id: str, tool_name: str) -> None:
        """Record a dispatched tool call so the summary can use its name.

        Called by ``AgentLoop`` each time it dispatches a tool.  The map
        is append-only and persists across ``run()`` calls on the same
        compactor instance — useful when a later round compacts tool
        results whose ids were produced in an earlier round.

        Args:
            call_id: The opaque ``tool_call_id`` the model assigned.
            tool_name: The ``ToolSpec.name`` (e.g. ``"read_file"``).
        """
        if call_id:
            self._tool_names_by_id[call_id] = tool_name

    def truncate_tool_results(self, messages: list[Message]) -> list[Message]:
        """Micro-compaction: truncate large tool results in-place.

        Scans for ``tool`` role messages whose content exceeds
        ``micro_limit`` and replaces them with truncated copies plus
        a notice indicating the original size.  Non-tool messages are
        returned unchanged.

        Args:
            messages: The working message list.  Not modified — a new
                list with truncated copies is returned only when needed.

        Returns:
            A message list with all tool results within the char limit.
        """
        result: list[Message] = []
        changed = False
        for msg in messages:
            if msg.get("role") == MessageRole.TOOL:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.micro_limit:
                    truncated = content[: self.micro_limit]
                    notice = _TRUNCATION_NOTICE.format(len(content))
                    result.append({**msg, "content": truncated + notice})
                    changed = True
                    continue

            result.append(msg)

        return result if changed else messages

    def should_compact(self, messages: list[Message]) -> bool:
        """Check whether full compaction should trigger.

        Args:
            messages: The current working message list.

        Returns:
            ``True`` if the total char count exceeds the threshold.
        """
        total = sum(len(str(m.get("content", ""))) for m in messages)
        return total > self.threshold_chars

    async def compact(
        self,
        messages: list[Message],
        request_id: str,
    ) -> list[Message]:
        """Full compaction: summarize oldest messages and session-roll.

        Splits messages into a compressible head and a kept tail.  The
        head is summarized via a dedicated inference call and replaced
        with two synthetic messages (summary + acknowledgment).

        The system message (index 0) is always preserved and never
        included in the summary.

        Args:
            messages: The full working message list (system + history).
            request_id: Correlation ID for structured logging.

        Returns:
            A compacted message list: [system, summary_user,
            summary_ack, ...kept_tail].
        """
        # Nothing to compact if the conversation is trivially short.
        if len(messages) < _MIN_MESSAGES_FOR_COMPACTION:
            return messages

        # Separate the system message (always preserved verbatim) from
        # the conversation body that is eligible for compaction.
        system_msg = messages[0] if messages[0].get("role") == MessageRole.SYSTEM else None
        conversation = messages[1:] if system_msg else list(messages)

        # Guard: need at least min_keep + _MIN_COMPRESS_COUNT messages
        # so there is something worth summarizing after reserving the
        # kept tail.
        if len(conversation) < self.min_keep + _MIN_COMPRESS_COUNT:
            return messages

        # Split the conversation into a compressible head (oldest) and
        # a kept tail (newest).  keep_count is floored at min_keep so
        # recent context is never lost; compress_count is then derived
        # as the remainder to ensure head + tail == full conversation.
        compress_count = max(_MIN_COMPRESS_COUNT, int(len(conversation) * self.compress_ratio))
        keep_count = max(self.min_keep, len(conversation) - compress_count)
        compress_count = len(conversation) - keep_count

        to_compress = conversation[:compress_count]  # oldest (head)
        to_keep = conversation[compress_count:]  # newest (tail)

        # One inference call to distill the head into a concise summary.
        summary = await self._summarize(to_compress, request_id)

        logger.info(
            "Full compaction applied",
            extra={
                "request_id": request_id,
                "compressed_messages": compress_count,
                "kept_messages": len(to_keep),
                "summary_chars": len(summary),
            },
        )

        # Reassemble: system prompt → synthetic summary pair → kept tail.
        # The two synthetic messages (user summary + assistant ack) give
        # the model a natural conversation anchor so it doesn't treat the
        # summary as an instruction to execute.
        new_messages: list[Message] = []
        if system_msg:
            new_messages.append(system_msg)
        new_messages.append(
            {"role": MessageRole.USER, "content": f"[Previous conversation summary]\n{summary}"}
        )
        new_messages.append(
            {
                "role": MessageRole.ASSISTANT,
                "content": "Understood, I have the context from our previous conversation.",
            }
        )
        new_messages.extend(to_keep)
        return new_messages

    async def _summarize(
        self,
        messages: list[Message],
        request_id: str,
    ) -> str:
        """Generate a summary of a message slice via inference.

        Args:
            messages: The messages to summarize.
            request_id: Correlation ID for logging.

        Returns:
            The summary text from the model.
        """
        conversation_text = self._format_for_summary(messages)

        summary_request = InferenceRequest(
            messages=[
                {"role": MessageRole.SYSTEM, "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": MessageRole.USER, "content": conversation_text},
            ],
            model=self._model,
            temperature=_SUMMARY_TEMPERATURE,
            max_tokens=_SUMMARY_MAX_TOKENS,
            request_id=f"{request_id}-compact",
        )

        result = await self._backend.complete(summary_request)

        logger.info(
            "Compaction summary generated",
            extra={
                "request_id": request_id,
                "input_messages": len(messages),
                "summary_tokens": result.usage.completion_tokens,
            },
        )

        return result.content

    def _format_for_summary(self, messages: list[Message]) -> str:
        """Render messages as a readable transcript for the summary model.

        Uses ``self._tool_names_by_id`` (populated by ``AgentLoop`` via
        ``register_tool_call`` at dispatch time) to resolve tool-result
        messages — which only carry the opaque ``tool_call_id`` — back
        to their human-readable function name.  The map-based approach
        keeps the compactor decoupled from message structure: no
        walking, no pre-scans.

        Args:
            messages: Messages to format.

        Returns:
            A plain-text transcript with role labels.
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == MessageRole.TOOL:
                tc_id = str(msg.get("tool_call_id", ""))
                # Fall back to the id itself (then "tool") if the call
                # wasn't registered — defensive against messages carried
                # over from a different AgentLoop instance's history.
                tool_name = self._tool_names_by_id.get(tc_id) or tc_id or "tool"
                if isinstance(content, str) and len(content) > _MAX_TOOL_RESULT_IN_SUMMARY:
                    content = content[:_MAX_TOOL_RESULT_IN_SUMMARY] + "..."
                lines.append(f"[Tool result ({tool_name})]: {content}")
            elif role == MessageRole.ASSISTANT and "tool_calls" in msg:
                # Assistant's tool_calls entries carry the name inline
                # (``function.name``) — no map lookup needed.
                tc_list = msg.get("tool_calls", [])
                calls_desc = ", ".join(_extract_tool_name(tc) for tc in tc_list)
                lines.append(f"Assistant: [called tools: {calls_desc}]")
                if content:
                    lines.append(f"Assistant: {content}")
            else:
                lines.append(f"{role.capitalize()}: {content}")
        return "\n".join(lines)


def _extract_tool_name(tc: dict[str, object] | object) -> str:
    """Extract the tool function name from a single ``tool_calls`` entry.

    Used only for the assistant-role branch, which carries the name
    inline via ``tool_calls[i].function.name``.  Not used for tool-role
    messages (those are resolved via the registered-call map).

    Args:
        tc: A tool call in dict format (from working messages).

    Returns:
        The function name, or "unknown" if extraction fails.
    """
    if isinstance(tc, dict):
        func = tc.get("function", {})
        if isinstance(func, dict):
            name = func.get("name", "unknown")
            return str(name)
    return "unknown"
