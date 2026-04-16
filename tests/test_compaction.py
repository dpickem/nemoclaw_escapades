"""Tests for two-tier context compaction."""

from __future__ import annotations

from typing import Any

import pytest

from nemoclaw_escapades.agent.compaction import ContextCompactor, _extract_tool_name
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import Message
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
