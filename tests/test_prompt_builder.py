"""Tests for the unified layered prompt builder (system prompt + thread history)."""

from __future__ import annotations

import re

import pytest

from nemoclaw_escapades.agent.prompt_builder import (
    CACHE_BOUNDARY_MARKER,
    LayeredPromptBuilder,
    SourceType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def builder() -> LayeredPromptBuilder:
    return LayeredPromptBuilder(
        identity="You are NemoClaw, an AI coding assistant.",
        task_context="Workspace: /sandbox/project\nLanguage: Python",
    )


@pytest.fixture
def minimal_builder() -> LayeredPromptBuilder:
    return LayeredPromptBuilder(identity="You are a test agent.")


# ---------------------------------------------------------------------------
# Layer ordering
# ---------------------------------------------------------------------------


class TestLayerOrdering:
    """Layers appear in the correct order with the cache boundary."""

    def test_all_five_layers_present(self, builder: LayeredPromptBuilder) -> None:
        prompt = builder.build(
            agent_id="agent-001",
            source_type=SourceType.SLACK,
            scratchpad="My notes here",
            tools_summary="read_file, grep, bash",
        )

        parts = prompt.split("\n\n")
        texts = [p.strip() for p in parts]

        assert texts[0] == "You are NemoClaw, an AI coding assistant."
        assert "Workspace: /sandbox/project" in texts[1]

        boundary_idx = texts.index(CACHE_BOUNDARY_MARKER)
        assert boundary_idx == 2

        after_boundary = "\n\n".join(texts[boundary_idx + 1 :])
        assert "agent-001" in after_boundary
        assert "slack" in after_boundary
        assert "<scratchpad>" in after_boundary
        assert "My notes here" in after_boundary

    def test_identity_is_first(self, builder: LayeredPromptBuilder) -> None:
        prompt = builder.build()
        assert prompt.startswith("You are NemoClaw")

    def test_cache_boundary_present(self, builder: LayeredPromptBuilder) -> None:
        prompt = builder.build()
        assert CACHE_BOUNDARY_MARKER in prompt

    def test_cache_boundary_splits_static_and_dynamic(self, builder: LayeredPromptBuilder) -> None:
        prompt = builder.build(agent_id="test-id", source_type=SourceType.USER)
        before, after = prompt.split(CACHE_BOUNDARY_MARKER)

        assert "NemoClaw" in before
        assert "Workspace" in before

        assert "test-id" in after
        assert "user" in after


# ---------------------------------------------------------------------------
# Channel hint
# ---------------------------------------------------------------------------


class TestChannelHint:
    """Layer 4: channel hint varies by source_type."""

    def test_user_channel(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(source_type=SourceType.SLACK)
        assert "responding to a user via slack" in prompt.lower()

    def test_agent_channel(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(source_type=SourceType.AGENT)
        assert "sub-agent" in prompt.lower()
        assert "parent agent" in prompt.lower()

    def test_cron_channel(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(source_type=SourceType.CRON)
        assert "cron" in prompt.lower()
        assert "background" in prompt.lower()

    def test_default_source_type_is_user(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build()
        assert "responding to a user via user" in prompt.lower()


# ---------------------------------------------------------------------------
# Scratchpad layer
# ---------------------------------------------------------------------------


class TestScratchpadLayer:
    """Layer 5: scratchpad injection."""

    def test_scratchpad_included(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(scratchpad="## Plan\nStep 1: read code")
        assert "<scratchpad>" in prompt
        assert "Step 1: read code" in prompt
        assert "</scratchpad>" in prompt

    def test_empty_scratchpad_excluded(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(scratchpad="")
        assert "<scratchpad>" not in prompt

    def test_whitespace_scratchpad_excluded(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(scratchpad="   \n  ")
        assert "<scratchpad>" not in prompt


# ---------------------------------------------------------------------------
# Runtime metadata
# ---------------------------------------------------------------------------


class TestRuntimeMetadata:
    """Layer 3: runtime metadata."""

    def test_includes_timestamp(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build()
        assert "Current time:" in prompt
        assert re.search(r"\d{4}-\d{2}-\d{2}", prompt)

    def test_includes_agent_id(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(agent_id="agent-42")
        assert "agent-42" in prompt

    def test_includes_tools_summary(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(tools_summary="read_file, grep")
        assert "read_file, grep" in prompt

    def test_omits_empty_agent_id(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(agent_id="")
        assert "Agent ID:" not in prompt

    def test_omits_empty_tools_summary(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build(tools_summary="")
        assert "Available tools:" not in prompt


# ---------------------------------------------------------------------------
# Task context
# ---------------------------------------------------------------------------


class TestTaskContext:
    """Layer 2: task context."""

    def test_included_when_set(self, builder: LayeredPromptBuilder) -> None:
        prompt = builder.build()
        assert "Workspace: /sandbox/project" in prompt
        assert "Language: Python" in prompt

    def test_omitted_when_empty(self, minimal_builder: LayeredPromptBuilder) -> None:
        prompt = minimal_builder.build()
        parts = prompt.split(CACHE_BOUNDARY_MARKER)
        before = parts[0].strip()
        assert before == "You are a test agent."


# ---------------------------------------------------------------------------
# Static prefix
# ---------------------------------------------------------------------------


class TestStaticPrefix:
    """The cacheable prefix property."""

    def test_ends_with_boundary(self, builder: LayeredPromptBuilder) -> None:
        prefix = builder.static_prefix
        assert prefix.endswith(CACHE_BOUNDARY_MARKER)

    def test_contains_identity_and_context(self, builder: LayeredPromptBuilder) -> None:
        prefix = builder.static_prefix
        assert "NemoClaw" in prefix
        assert "Workspace" in prefix

    def test_no_dynamic_content(self, builder: LayeredPromptBuilder) -> None:
        prefix = builder.static_prefix
        assert "Current time:" not in prefix
        assert "<scratchpad>" not in prefix


# ---------------------------------------------------------------------------
# Thread history management
# ---------------------------------------------------------------------------


class TestThreadHistory:
    """Per-thread conversation history (formerly in orchestrator/prompt_builder)."""

    def test_empty_history(self, minimal_builder: LayeredPromptBuilder) -> None:
        msgs = minimal_builder.messages_for_inference("t1", "Hello")
        assert msgs[0]["role"] == "system"
        assert msgs[1] == {"role": "user", "content": "Hello"}
        assert len(msgs) == 2

    def test_history_accumulates_after_commit(self, minimal_builder: LayeredPromptBuilder) -> None:
        minimal_builder.commit_turn("t1", "msg1", "reply1")
        minimal_builder.commit_turn("t1", "msg2", "reply2")
        msgs = minimal_builder.messages_for_inference("t1", "msg3")

        non_system = [m for m in msgs if m["role"] != "system"]
        assert len(non_system) == 5
        assert non_system[-1] == {"role": "user", "content": "msg3"}

    def test_history_capped_at_max(self) -> None:
        builder = LayeredPromptBuilder(identity="sys", max_thread_history=4)
        for i in range(10):
            builder.commit_turn("t1", f"u{i}", f"a{i}")

        msgs = builder.messages_for_inference("t1", "final")
        non_system = [m for m in msgs if m["role"] != "system"]
        assert len(non_system) <= 4

    def test_separate_threads_independent(self, minimal_builder: LayeredPromptBuilder) -> None:
        minimal_builder.commit_turn("t1", "thread1-msg", "thread1-reply")
        minimal_builder.commit_turn("t2", "thread2-msg", "thread2-reply")

        msgs_t1 = minimal_builder.messages_for_inference("t1", "new1")
        msgs_t2 = minimal_builder.messages_for_inference("t2", "new2")

        users_t1 = [m for m in msgs_t1 if m["role"] == "user"]
        users_t2 = [m for m in msgs_t2 if m["role"] == "user"]
        assert len(users_t1) == 2
        assert len(users_t2) == 2

    def test_messages_for_inference_does_not_mutate_history(
        self, minimal_builder: LayeredPromptBuilder
    ) -> None:
        minimal_builder.messages_for_inference("t1", "preview")
        assert minimal_builder.thread_history["t1"] == []

    def test_commit_then_failed_does_not_pollute(
        self, minimal_builder: LayeredPromptBuilder
    ) -> None:
        minimal_builder.commit_turn("t1", "good", "reply")
        minimal_builder.messages_for_inference("t1", "bad_attempt")
        assert len(minimal_builder.thread_history["t1"]) == 2

    def test_system_prompt_uses_build(self, minimal_builder: LayeredPromptBuilder) -> None:
        """The system message uses the layered build() output."""
        msgs = minimal_builder.messages_for_inference("t1", "hi")
        system_content = msgs[0]["content"]
        assert "You are a test agent." in system_content
        assert CACHE_BOUNDARY_MARKER in system_content

    def test_thread_history_property(self, minimal_builder: LayeredPromptBuilder) -> None:
        minimal_builder.commit_turn("t1", "u", "a")
        assert "t1" in minimal_builder.thread_history
        assert len(minimal_builder.thread_history["t1"]) == 2
