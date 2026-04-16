"""Layered prompt builder with per-thread conversation history.

Constructs the system prompt from five ordered layers with a cache
boundary between static and dynamic content, as described in
design_m2a.md §8.  Provider prompt caching (e.g. Anthropic's or
OpenAI's) can reuse the prefix before the boundary, reducing cost
by ~90% on subsequent turns.

Also owns per-thread **conversation history** management — the same
concern that was previously split into ``orchestrator/prompt_builder.py``.
By unifying both in one class, every agent (orchestrator, coding agent,
sub-agents) gets layered prompts and history management from the same
source.

**Five-layer system prompt:**

+-------+-------------------+---------+
| Layer | Content           | Type    |
+-------+-------------------+---------+
| 1     | Identity          | Static  |
| 2     | Task context      | Static* |
|       | CACHE BOUNDARY    |         |
| 3     | Runtime metadata  | Dynamic |
| 4     | Channel hint      | Dynamic |
| 5     | Scratchpad        | Dynamic |
+-------+-------------------+---------+

(* Task context is static per task but changes between tasks.)

**Thread history** is keyed by ``thread_ts`` (or the message's own
``request_id`` for top-level messages).  History is capped at a
configurable maximum to prevent unbounded growth and is lost on restart
— persistent storage is deferred to M5.

**Commit semantics** — ``messages_for_inference`` builds the prompt
*without* mutating history.  ``commit_turn`` persists the user +
assistant pair only after a successful model round-trip, so failed
requests never pollute the conversation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum

from nemoclaw_escapades.models.types import MessageRole
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("agent.prompt_builder")

# Marker that splits the prompt into cacheable (above) and dynamic
# (below) portions.  The inference backend can use this to set a
# cache breakpoint for provider prompt caching.
CACHE_BOUNDARY_MARKER: str = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


class SourceType(StrEnum):
    """How the agent was invoked — controls the channel hint in Layer 4.

    Well-known values have dedicated hint text.  Because ``SourceType``
    is a ``StrEnum``, unknown platform names (e.g. ``"teams"``) can be
    constructed at runtime and still work with the fallback hint.
    """

    USER = "user"
    AGENT = "agent"
    CRON = "cron"
    SLACK = "slack"


# Default cap on per-thread conversation history (user + assistant msgs).
_DEFAULT_MAX_THREAD_HISTORY: int = 50


class LayeredPromptBuilder:
    """Five-layer system prompt builder with per-thread conversation history.

    Layers 1-2 (identity + task context) are set at construction
    time and don't change per turn.  Layers 3-5 (runtime metadata,
    channel hint, scratchpad) are assembled dynamically on each
    ``build()`` call.

    Thread history is stored in memory, keyed by an opaque thread key
    (typically ``thread_ts``).  History is capped at
    ``max_thread_history`` and lost on restart.

    Attributes:
        identity: Layer 1 — agent role definition.
        task_context: Layer 2 — skill content, workspace description,
            task instructions.
        thread_history: Mapping of thread key → stored message list.
            Exposed for testing and inspection.
    """

    def __init__(
        self,
        identity: str,
        task_context: str = "",
        max_thread_history: int = _DEFAULT_MAX_THREAD_HISTORY,
    ) -> None:
        """Initialise the builder with static layers and history cap.

        Args:
            identity: Agent role definition text (Layer 1).  Typically
                loaded from an ``AGENT.md`` file or a prompt template.
            task_context: Skill content, workspace description, and/or
                task instructions (Layer 2).  May be empty for general
                agents without a specific task.
            max_thread_history: Maximum number of user + assistant
                messages retained per thread.  When exceeded, the oldest
                messages are silently dropped.
        """
        self.identity = identity
        self.task_context = task_context
        self._max_history = max_thread_history
        self._thread_history: dict[str, list[dict[str, str]]] = defaultdict(list)

    @property
    def thread_history(self) -> dict[str, list[dict[str, str]]]:
        """Mapping of thread key to its stored message list."""
        return self._thread_history

    # ------------------------------------------------------------------
    # System prompt construction (5-layer)
    # ------------------------------------------------------------------

    def build(
        self,
        agent_id: str = "",
        source_type: SourceType = SourceType.USER,
        scratchpad: str = "",
        tools_summary: str = "",
    ) -> str:
        """Assemble the full system prompt from all five layers.

        Args:
            agent_id: Unique identifier for this agent instance
                (included in runtime metadata for traceability).
            source_type: How the agent was invoked.  Controls the
                channel hint layer (Layer 4).
            scratchpad: Current scratchpad contents (may be empty).
            tools_summary: Brief summary of available tools (may be empty).

        Returns:
            The complete system prompt string with the cache boundary
            marker embedded between static and dynamic layers.
        """
        layers: list[str] = []

        # Layer 1 — Identity (static)
        layers.append(self.identity)

        # Layer 2 — Task context (static per task)
        if self.task_context:
            layers.append(self.task_context)

        # Cache boundary — everything above this can be cached by the
        # provider; everything below changes per turn.
        layers.append(CACHE_BOUNDARY_MARKER)

        # Layer 3 — Runtime metadata (dynamic)
        layers.append(self._runtime_metadata(agent_id, tools_summary))

        # Layer 4 — Channel hint (dynamic)
        layers.append(self._channel_hint(source_type))

        # Layer 5 — Scratchpad (dynamic)
        if scratchpad.strip():
            layers.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        return "\n\n".join(layers)

    @property
    def static_prefix(self) -> str:
        """Return just the cacheable prefix (layers 1-2 + boundary).

        Useful for pre-computing cache keys or for providers that
        need the prefix separately.

        Returns:
            The static portion of the prompt ending with the cache
            boundary marker.
        """
        layers: list[str] = [self.identity]
        if self.task_context:
            layers.append(self.task_context)
        layers.append(CACHE_BOUNDARY_MARKER)
        return "\n\n".join(layers)

    # ------------------------------------------------------------------
    # Thread history management
    # ------------------------------------------------------------------

    def history_with_user_message(self, thread_key: str, user_text: str) -> list[dict[str, str]]:
        """Build a snapshot of thread history with *user_text* appended.

        Returns a **copy** — stored history is never mutated.  The
        caller uses this to preview the prompt before a successful model
        round-trip commits the turn via ``commit_turn``.

        The returned list is capped at ``max_thread_history`` entries;
        if appending the new user message would exceed the cap, the
        oldest messages are dropped from the front.

        Args:
            thread_key: Conversation identifier (typically
                ``thread_ts`` or the message's own ``request_id``).
            user_text: The new user message to append.

        Returns:
            A capped list of ``{"role": ..., "content": ...}`` dicts
            ending with the new user message.
        """
        hist = list(self._thread_history[thread_key])
        hist.append({"role": MessageRole.USER, "content": user_text})
        if len(hist) > self._max_history:
            return hist[-self._max_history :]
        return hist

    def messages_for_inference(self, thread_key: str, user_text: str) -> list[dict[str, str]]:
        """Assemble the full message list for an inference call.

        Prepends the system prompt (from ``build()``) to the capped
        history snapshot produced by ``history_with_user_message``.
        Does not commit anything — history is only persisted when the
        caller invokes ``commit_turn`` after a successful backend
        response.

        Args:
            thread_key: Conversation identifier.
            user_text: The new user message.

        Returns:
            An OpenAI-format message list starting with the system
            prompt, followed by conversation history, ending with the
            latest user message.
        """
        system_prompt = self.build()
        hist = self.history_with_user_message(thread_key, user_text)
        return [{"role": MessageRole.SYSTEM, "content": system_prompt}] + hist

    def commit_turn(
        self,
        thread_key: str,
        user_text: str,
        assistant_content: str,
    ) -> None:
        """Persist a completed user + assistant exchange to thread history.

        Should only be called after a successful inference round-trip.
        Builds the same capped snapshot as ``history_with_user_message``,
        appends the assistant reply, and replaces the stored history for
        *thread_key*.

        Args:
            thread_key: Conversation identifier.
            user_text: The user message from this turn.
            assistant_content: The model's response text for this turn.
        """
        hist = self.history_with_user_message(thread_key, user_text)
        hist.append({"role": MessageRole.ASSISTANT, "content": assistant_content})
        self._thread_history[thread_key] = hist

    # ------------------------------------------------------------------
    # Layer helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _runtime_metadata(agent_id: str, tools_summary: str) -> str:
        """Build the runtime metadata layer (Layer 3).

        Args:
            agent_id: Agent instance identifier.
            tools_summary: Brief summary of available tools.

        Returns:
            Formatted runtime metadata string.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        parts: list[str] = [f"Current time: {now}"]
        if agent_id:
            parts.append(f"Agent ID: {agent_id}")
        if tools_summary:
            parts.append(f"Available tools: {tools_summary}")
        return "\n".join(parts)

    @staticmethod
    def _channel_hint(source_type: SourceType) -> str:
        """Build the channel hint layer (Layer 4).

        Tells the agent how its response will be consumed so it can
        adjust tone and format accordingly.

        Args:
            source_type: The invocation channel.

        Returns:
            A single sentence describing the response context.
        """
        if source_type == SourceType.CRON:
            return (
                "You are running as a background cron job. "
                "Your output will be logged, not shown to a user."
            )
        if source_type == SourceType.AGENT:
            return (
                "You are running as a dispatched sub-agent. "
                "Your response will be sent to the parent agent, not directly to a user."
            )
        return f"You are responding to a user via {source_type}."
