"""Prompt builder — thread history and message assembly for inference calls.

Owns the in-memory conversation history and the logic for turning
(system prompt + history + new user message) into the message list sent
to the inference backend.  Extracted from ``orchestrator.py`` so the
orchestrator loop stays focused on control flow (call backend → repair →
approve → respond) and prompt construction can evolve independently
(e.g. when M4 adds persistent memory or context compaction).

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


class PromptBuilder:
    """Assembles inference message lists and manages per-thread conversation history.

    Separates prompt construction from the orchestrator control loop so
    each concern can evolve independently (e.g. adding context compaction
    in M4 or persistent storage in M5).

    All public methods that accept a ``thread_key`` use it to look up
    the corresponding conversation history.  History is stored in memory
    and lost on process restart.

    Attributes:
        thread_history: Mapping of thread key to its message list.
            Each entry is a ``{"role": ..., "content": ...}`` dict in
            OpenAI message format.  Exposed for testing and inspection.
    """

    def __init__(self, system_prompt: str, max_thread_history: int) -> None:
        """Initialise the builder with a fixed system prompt and history cap.

        Args:
            system_prompt: Static system-prompt text prepended to every
                inference call.  Loaded once at startup from the path
                specified in ``OrchestratorConfig``.
            max_thread_history: Maximum number of user + assistant
                messages retained per thread.  When exceeded, the oldest
                messages are silently dropped.
        """
        self._system_prompt: str = system_prompt
        self._max_history: int = max_thread_history
        self._thread_history: dict[str, list[dict[str, str]]] = defaultdict(list)

    @property
    def thread_history(self) -> dict[str, list[dict[str, str]]]:
        """Mapping of thread key to its stored message list."""
        return self._thread_history

    def history_with_user_message(self, thread_key: str, user_text: str) -> list[dict[str, str]]:
        """Build a snapshot of thread history with *user_text* appended.

        Returns a **copy** — stored history is never mutated. The
        orchestrator calls this to preview the prompt before a
        successful model round-trip commits the turn via
        ``commit_turn``.

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
        hist.append({"role": "user", "content": user_text})
        if len(hist) > self._max_history:
            return hist[-self._max_history :]
        return hist

    def messages_for_inference(self, thread_key: str, user_text: str) -> list[dict[str, str]]:
        """Assemble the full message list for an inference call.

        Prepends the static system prompt to the capped history
        snapshot produced by ``history_with_user_message``.  Does not
        commit anything — history is only persisted when the caller
        invokes ``commit_turn`` after a successful backend response.

        Args:
            thread_key: Conversation identifier.
            user_text: The new user message.

        Returns:
            An OpenAI-format message list starting with the system
            prompt, followed by conversation history, ending with the
            latest user message.
        """
        hist = self.history_with_user_message(thread_key, user_text)
        return [{"role": "system", "content": self._system_prompt}] + hist

    def commit_turn(self, thread_key: str, user_text: str, assistant_content: str) -> None:
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
        hist.append({"role": "assistant", "content": assistant_content})
        self._thread_history[thread_key] = hist
