"""Tests for two-tier context compaction."""

from __future__ import annotations

from typing import Any

import pytest

from nemoclaw_escapades.agent.compaction import (
    ContextCompactor,
    _extract_tool_name,
)
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import Message, MessageRole
from tests.conftest import MockBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend(response_text="Summary of the conversation.")


@pytest.fixture
def config() -> AgentLoopConfig:
    return AgentLoopConfig(
        model="test-model",
        micro_compaction_chars=100,
        compaction_threshold_chars=500,
        compaction_compress_ratio=0.5,
        compaction_min_keep=2,
    )


@pytest.fixture
def compactor(backend: MockBackend, config: AgentLoopConfig) -> ContextCompactor:
    return ContextCompactor(backend, config)


def _make_messages(
    tool_content: str = "short result",
    count: int = 1,
) -> list[Message]:
    """Build a message list with a system prompt, user message, and tool results."""
    msgs: list[Message] = [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": "Do something"},
    ]
    for i in range(count):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"tc-{i}",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"tc-{i}", "content": tool_content})
    return msgs


# ---------------------------------------------------------------------------
# Micro-compaction
# ---------------------------------------------------------------------------


class TestMicroCompaction:
    """Tier 1: tool result truncation."""

    def test_short_tool_results_unchanged(self, compactor: ContextCompactor) -> None:
        messages = _make_messages("short result")
        result = compactor.truncate_tool_results(messages)
        assert result is messages

    def test_long_tool_result_truncated(self, compactor: ContextCompactor) -> None:
        long_content = "x" * 200
        messages = _make_messages(long_content)
        result = compactor.truncate_tool_results(messages)

        tool_msg = [m for m in result if m.get("role") == "tool"][0]
        assert len(tool_msg["content"]) < len(long_content) + 100
        assert "[Truncated" in tool_msg["content"]
        assert "200 chars" in tool_msg["content"]

    def test_truncation_preserves_limit(self, compactor: ContextCompactor) -> None:
        long_content = "y" * 500
        messages = _make_messages(long_content)
        result = compactor.truncate_tool_results(messages)

        tool_msg = [m for m in result if m.get("role") == "tool"][0]
        content = tool_msg["content"]
        # The truncated portion should be exactly micro_limit chars
        # (the notice is appended after).
        assert content.startswith("y" * compactor.micro_limit)

    def test_non_tool_messages_unchanged(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "x" * 200},
            {"role": "user", "content": "x" * 200},
            {"role": "assistant", "content": "x" * 200},
        ]
        result = compactor.truncate_tool_results(messages)
        assert result is messages

    def test_original_list_not_mutated(self, compactor: ContextCompactor) -> None:
        long_content = "z" * 200
        messages = _make_messages(long_content)
        original_tool_content = messages[3]["content"]
        compactor.truncate_tool_results(messages)
        assert messages[3]["content"] == original_tool_content

    def test_multiple_tool_results_truncated(self, compactor: ContextCompactor) -> None:
        messages = _make_messages("a" * 200, count=3)
        result = compactor.truncate_tool_results(messages)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert all("[Truncated" in m["content"] for m in tool_msgs)


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    """Threshold detection for full compaction."""

    def test_below_threshold_returns_false(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "short"},
            {"role": "user", "content": "hello"},
        ]
        assert compactor.should_compact(messages) is False

    def test_above_threshold_returns_true(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "x" * 300},
            {"role": "user", "content": "y" * 300},
        ]
        assert compactor.should_compact(messages) is True

    def test_exact_threshold_returns_false(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "a" * 250},
            {"role": "user", "content": "b" * 250},
        ]
        assert compactor.should_compact(messages) is False


# ---------------------------------------------------------------------------
# Full compaction
# ---------------------------------------------------------------------------


class TestFullCompaction:
    """Tier 2: LLM summary + session roll."""

    async def test_compact_produces_summary_messages(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "System prompt"},
        ]
        for i in range(10):
            messages.append({"role": "user", "content": f"User message {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})

        result = await compactor.compact(messages, "req-1")

        assert result[0]["role"] == "system"
        assert result[0]["content"] == "System prompt"
        assert result[1]["role"] == "user"
        assert "[Previous conversation summary]" in result[1]["content"]
        assert result[2]["role"] == "assistant"
        assert "context" in result[2]["content"].lower()

    async def test_compact_keeps_recent_messages(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "System prompt"},
        ]
        for i in range(10):
            messages.append({"role": "user", "content": f"User message {i}"})
            messages.append({"role": "assistant", "content": f"Response {i}"})

        result = await compactor.compact(messages, "req-2")

        kept_contents = [m.get("content", "") for m in result[3:]]
        assert "Response 9" in kept_contents

    async def test_compact_reduces_message_count(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "System prompt"},
        ]
        for i in range(20):
            messages.append({"role": "user", "content": f"Msg {i}"})
            messages.append({"role": "assistant", "content": f"Reply {i}"})

        original_count = len(messages)
        result = await compactor.compact(messages, "req-3")
        assert len(result) < original_count

    async def test_compact_calls_backend(
        self, backend: MockBackend, compactor: ContextCompactor
    ) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "System prompt"},
        ]
        for i in range(10):
            messages.append({"role": "user", "content": f"Msg {i}"})
            messages.append({"role": "assistant", "content": f"Reply {i}"})

        await compactor.compact(messages, "req-4")
        assert len(backend.calls) == 1
        assert "compact" in backend.calls[0].request_id

    async def test_compact_skips_tiny_conversation(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        result = await compactor.compact(messages, "req-5")
        assert result is messages

    async def test_compact_without_system_message(self, compactor: ContextCompactor) -> None:
        messages: list[Message] = []
        for i in range(10):
            messages.append({"role": "user", "content": f"Msg {i}"})
            messages.append({"role": "assistant", "content": f"Reply {i}"})

        result = await compactor.compact(messages, "req-6")
        assert result[0]["role"] == "user"
        assert "[Previous conversation summary]" in result[0]["content"]


# ---------------------------------------------------------------------------
# Helper: _extract_tool_name
# ---------------------------------------------------------------------------


class TestExtractToolName:
    """Utility for extracting tool names from tool_call dicts."""

    def test_valid_dict(self) -> None:
        tc: dict[str, Any] = {"function": {"name": "grep", "arguments": "{}"}}
        assert _extract_tool_name(tc) == "grep"

    def test_missing_function(self) -> None:
        tc: dict[str, Any] = {"id": "tc-1"}
        assert _extract_tool_name(tc) == "unknown"

    def test_non_dict(self) -> None:
        assert _extract_tool_name("not a dict") == "unknown"


# ---------------------------------------------------------------------------
# Summary-transcript formatting (tool name resolution)
# ---------------------------------------------------------------------------


def _tool_calls_msg(calls: list[tuple[str, str]]) -> Message:
    """Build an assistant message carrying tool_calls.

    Args:
        calls: List of (id, function_name) tuples.

    Returns:
        A message dict in the OpenAI wire format used by AgentLoop.
    """
    return {
        "role": MessageRole.ASSISTANT,
        "content": "",
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for tc_id, name in calls
        ],
    }


class TestRegisterToolCall:
    """register_tool_call populates the compactor's id → name map."""

    def test_registers_call_id(self, backend: MockBackend, config: AgentLoopConfig) -> None:
        c = ContextCompactor(backend, config)
        c.register_tool_call("call_abc", "read_file")
        assert c._tool_names_by_id == {"call_abc": "read_file"}

    def test_multiple_registrations(self, backend: MockBackend, config: AgentLoopConfig) -> None:
        c = ContextCompactor(backend, config)
        c.register_tool_call("tc-1", "read_file")
        c.register_tool_call("tc-2", "grep")
        assert c._tool_names_by_id == {"tc-1": "read_file", "tc-2": "grep"}

    def test_empty_call_id_ignored(self, backend: MockBackend, config: AgentLoopConfig) -> None:
        """Defensive: empty call-ids aren't stored (avoids a spurious "" key)."""
        c = ContextCompactor(backend, config)
        c.register_tool_call("", "read_file")
        assert c._tool_names_by_id == {}

    def test_duplicate_registration_overwrites(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        """Registering the same id twice takes the newest name.

        In practice ids are unique per call, but this documents the
        behaviour so a caller that retries registration doesn't get
        stale state.
        """
        c = ContextCompactor(backend, config)
        c.register_tool_call("tc-1", "old")
        c.register_tool_call("tc-1", "new")
        assert c._tool_names_by_id["tc-1"] == "new"


class TestFormatForSummaryToolName:
    """_format_for_summary surfaces the function name, not the opaque id."""

    def test_tool_result_uses_registered_name(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        """Regression: the transcript reads ``[Tool result (read_file)]``,
        not ``[Tool result (call_abc123)]``.  Names come from the
        compactor's registered-call map, not from message walking.
        """
        c = ContextCompactor(backend, config)
        c.register_tool_call("call_abc123", "read_file")

        messages: list[Message] = [
            _tool_calls_msg([("call_abc123", "read_file")]),
            {
                "role": MessageRole.TOOL,
                "tool_call_id": "call_abc123",
                "content": "file contents",
            },
        ]
        transcript = c._format_for_summary(messages)
        assert "[Tool result (read_file)]" in transcript
        # The opaque id does appear in the assistant tool_calls entry's
        # raw dict, but NOT as the tool-result label.
        assert "[Tool result (call_abc123)]" not in transcript

    def test_multiple_registered_calls_resolved_independently(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        c = ContextCompactor(backend, config)
        c.register_tool_call("c1", "read_file")
        c.register_tool_call("c2", "grep")

        messages: list[Message] = [
            _tool_calls_msg([("c1", "read_file"), ("c2", "grep")]),
            {"role": MessageRole.TOOL, "tool_call_id": "c1", "content": "A"},
            {"role": MessageRole.TOOL, "tool_call_id": "c2", "content": "B"},
        ]
        transcript = c._format_for_summary(messages)
        assert "[Tool result (read_file)]: A" in transcript
        assert "[Tool result (grep)]: B" in transcript

    def test_unregistered_call_falls_back_to_id(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        """If a tool result's id was never registered (e.g. carried over
        from a different AgentLoop instance's history), we fall back to
        the id itself so the audit trail stays correlatable.
        """
        c = ContextCompactor(backend, config)
        # Deliberately don't register the id.

        messages: list[Message] = [
            {"role": MessageRole.TOOL, "tool_call_id": "unknown-id", "content": "x"},
        ]
        transcript = c._format_for_summary(messages)
        assert "[Tool result (unknown-id)]" in transcript

    def test_tool_result_with_no_id_falls_back_to_tool(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        """Missing ``tool_call_id`` entirely falls back to the word 'tool'."""
        c = ContextCompactor(backend, config)
        messages: list[Message] = [{"role": MessageRole.TOOL, "content": "x"}]
        transcript = c._format_for_summary(messages)
        assert "[Tool result (tool)]" in transcript

    def test_assistant_tool_calls_listed_by_name(
        self, backend: MockBackend, config: AgentLoopConfig
    ) -> None:
        """The assistant's ``[called tools: ...]`` line reads ``name`` from
        the tool_calls entry directly — no map lookup needed.
        """
        c = ContextCompactor(backend, config)
        messages: list[Message] = [
            _tool_calls_msg([("c1", "read_file"), ("c2", "grep")]),
        ]
        transcript = c._format_for_summary(messages)
        assert "called tools: read_file, grep" in transcript
