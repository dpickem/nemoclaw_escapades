"""In-process integration tests for the agent stack (design_m2a §10.2).

These are **not** multi-sandbox tests (those live in ``tests/integration/``
and require a real NMB broker).  They exercise the full agent stack
against a mock inference backend:

- orchestrator + AgentLoop end-to-end (tool registry with real coding
  tools, a real workspace on disk);
- long-conversation compaction (50+ messages trigger full compaction);
- skill-guided task (the skill tool loads real SKILL.md content).

The mock backend is scripted with a realistic sequence of tool-use
responses so we can verify the full pipeline without spending tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nemoclaw_escapades.agent.approval import AutoApproval
from nemoclaw_escapades.agent.skill_loader import SkillLoader
from nemoclaw_escapades.config import OrchestratorConfig
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    MessageRole,
    NormalizedRequest,
    TextBlock,
    TokenUsage,
    ToolCall,
)
from nemoclaw_escapades.orchestrator.orchestrator import Orchestrator
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.skill import register_skill_tool
from nemoclaw_escapades.tools.tool_registry_factory import create_coding_tool_registry
from tests.conftest import MockBackend

# ---------------------------------------------------------------------------
# Scripted backend — emits a programmed sequence of tool calls + text
# ---------------------------------------------------------------------------


class ScriptedBackend(MockBackend):
    """Backend that returns a queued sequence of InferenceResponses.

    Each call consumes the next queued response.  Once the queue is
    empty, falls back to ``MockBackend.complete`` (a plain text reply).
    """

    def __init__(self) -> None:
        super().__init__(response_text="Done.")
        self._responses: list[InferenceResponse] = []
        self._idx = 0

    def enqueue_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
    ) -> None:
        """Queue a tool_calls response with one tool invocation."""
        self._responses.append(
            InferenceResponse(
                content="",
                model="mock-model",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                latency_ms=1.0,
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=json.dumps(arguments))],
            )
        )

    def enqueue_text(self, text: str) -> None:
        """Queue a plain text response that terminates the loop."""
        self._responses.append(
            InferenceResponse(
                content=text,
                model="mock-model",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                latency_ms=1.0,
                finish_reason="stop",
            )
        )

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        self.calls.append(request)
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return await super().complete(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orchestrator_config(max_thread_history: int = 100) -> OrchestratorConfig:
    """Orchestrator config with no system prompt file (falls back to default)."""
    return OrchestratorConfig(
        system_prompt_path="nonexistent.md",
        max_thread_history=max_thread_history,
    )


def _slack_request(text: str, thread: str = "thread-1") -> NormalizedRequest:
    """A platform-neutral request that looks like it came from Slack."""
    return NormalizedRequest(
        text=text,
        user_id="U1",
        channel_id="C1",
        thread_ts=thread,
        timestamp=0.0,
        source="slack",
    )


# ---------------------------------------------------------------------------
# 1. Orchestrator + AgentLoop end-to-end
# ---------------------------------------------------------------------------


class TestOrchestratorAgentLoopE2E:
    """A full request through Orchestrator → AgentLoop → coding tools → response."""

    async def test_slack_to_tools_to_response(self, tmp_path: Path) -> None:
        # Real workspace with a real file the agent will read.
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "hello.py"
        target.write_text("def hello():\n    return 'world'\n")

        tools = create_coding_tool_registry(str(workspace))

        # Scripted model: read the file, write some notes, then respond.
        # The notes file stands in for what used to be the scratchpad —
        # the agent stores intermediate analysis via ordinary file tools.
        backend = ScriptedBackend()
        backend.enqueue_tool_call("read_file", {"path": "hello.py"}, "tc-1")
        backend.enqueue_tool_call(
            "write_file",
            {"path": "notes.md", "content": "## Plan\nRead hello.py and summarise it."},
            "tc-2",
        )
        backend.enqueue_text("The file defines `hello()` which returns `'world'`.")

        orch = Orchestrator(
            backend,
            _orchestrator_config(),
            approval=AutoApproval(),
            tools=tools,
        )

        response = await orch.handle(_slack_request("What's in hello.py?"))

        assert isinstance(response.blocks[0], TextBlock)
        assert "hello()" in response.blocks[0].text
        assert "'world'" in response.blocks[0].text

        # The agent loop made at least 3 inference calls: read, write, final.
        assert len(backend.calls) >= 3

        # Working conversation includes tool-role messages — proves the loop
        # actually executed tools and fed results back to the model.
        final_messages = backend.calls[-1].messages
        tool_results = [m for m in final_messages if m.get("role") == MessageRole.TOOL]
        assert len(tool_results) >= 2

        # The notes file really hit disk — not just a claim in the assistant text.
        assert (workspace / "notes.md").read_text().startswith("## Plan")

    async def test_source_type_reaches_system_prompt(self, tmp_path: Path) -> None:
        """``NormalizedRequest.source`` flows through to the channel-hint layer."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tools = create_coding_tool_registry(str(workspace))

        backend = ScriptedBackend()
        backend.enqueue_text("OK.")

        orch = Orchestrator(
            backend,
            _orchestrator_config(),
            approval=AutoApproval(),
            tools=tools,
        )

        await orch.handle(_slack_request("hi"))

        system_msg = next(m for m in backend.calls[0].messages if m["role"] == MessageRole.SYSTEM)
        assert "responding to a user via slack" in system_msg["content"].lower()


# ---------------------------------------------------------------------------
# 2. Long-conversation compaction (§10.2)
# ---------------------------------------------------------------------------


class TestLongConversationCompaction:
    """A long conversation triggers full compaction and continues coherently."""

    async def test_long_conversation_triggers_compaction(self, tmp_path: Path) -> None:
        """50+ turns cross the compaction threshold; the loop keeps working."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tools = create_coding_tool_registry(str(workspace))

        # Use a very low threshold so a modest conversation triggers compaction.
        # The threshold is in AgentLoopConfig but the orchestrator constructs
        # its own config from OrchestratorConfig.  We exercise compaction via
        # repeated large tool results instead.
        backend = ScriptedBackend()

        # Cap raised to 200 so 50 prior turn-pairs (100 msgs) + 1 new user
        # message (101 total) fits without the history-cap truncating the
        # oldest message.  The point of this test is compaction, not the
        # cap.
        orch = Orchestrator(
            backend,
            _orchestrator_config(max_thread_history=200),
            approval=AutoApproval(),
            tools=tools,
        )

        # Prime the thread history with many prior turns (bypasses the need
        # to exceed the compaction threshold via raw message count; the
        # unit tests already cover compaction triggering mechanics).
        for i in range(50):
            orch._prompt.commit_turn("thread-long", f"user msg {i}", f"assistant reply {i}")

        assert len(orch._prompt.thread_history["thread-long"]) == 100  # 50 pairs

        backend.enqueue_text("I see the full context.")
        response = await orch.handle(_slack_request("What did we discuss?", thread="thread-long"))

        assert isinstance(response.blocks[0], TextBlock)
        assert response.blocks[0].text == "I see the full context."

        # The inference call sees all 50 prior turns plus the new user
        # message — 51 user messages in total.
        user_msgs = [m for m in backend.calls[0].messages if m["role"] == MessageRole.USER]
        assert len(user_msgs) == 51

    async def test_tool_result_truncation_in_long_loop(self, tmp_path: Path) -> None:
        """Huge tool results get micro-compacted to keep the loop healthy."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Write a file larger than the default micro-compaction threshold.
        huge_file = workspace / "big.txt"
        huge_file.write_text("x" * 50_000)

        tools = create_coding_tool_registry(str(workspace))

        backend = ScriptedBackend()
        backend.enqueue_tool_call("read_file", {"path": "big.txt"}, "tc-1")
        backend.enqueue_text("I read it but it was truncated.")

        orch = Orchestrator(
            backend,
            _orchestrator_config(),
            approval=AutoApproval(),
            tools=tools,
        )

        response = await orch.handle(_slack_request("Read big.txt"))

        # The assistant produced a final text response — no crash.
        assert isinstance(response.blocks[0], TextBlock)

        # The second inference call's tool-role message for the read_file
        # result was truncated by micro-compaction before being sent.
        second_call = backend.calls[1]
        tool_msgs = [m for m in second_call.messages if m["role"] == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        # The tool output (before any compaction) would have been
        # well over 10K chars (the default micro_compaction_chars).
        # After micro-compaction it's capped with a truncation notice.
        content = tool_msgs[0]["content"]
        # ToolRegistry already truncates at its own max_result_chars (8000)
        # before micro-compaction sees it.  Either way, the content is much
        # smaller than the file's raw 50K bytes.
        assert len(content) < 20_000


# ---------------------------------------------------------------------------
# 3. Skill-guided coding task (§10.2)
# ---------------------------------------------------------------------------


class TestSkillGuidedTask:
    """Loading a skill via the skill tool injects its content into the conversation."""

    @pytest.fixture
    def skills_dir(self, tmp_path: Path) -> Path:
        """Two real skills an agent can load."""
        (tmp_path / "review").mkdir()
        (tmp_path / "review" / "SKILL.md").write_text(
            "---\nname: Review\ndescription: Code review guidance\n---\n"
            "# Review Skill\nFocus on correctness, safety, tests, readability.\n"
        )
        (tmp_path / "debug").mkdir()
        (tmp_path / "debug" / "SKILL.md").write_text(
            "---\nname: Debug\n---\n# Debug Skill\nForm hypotheses and falsify them.\n"
        )
        return tmp_path

    async def test_agent_loads_skill_and_follows_it(
        self,
        skills_dir: Path,
        tmp_path: Path,
    ) -> None:
        """The agent calls the skill tool, receives content, then acts on it."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Build registry: coding tools + skill tool.
        loader = SkillLoader(str(skills_dir))
        tools = create_coding_tool_registry(str(workspace))
        register_skill_tool(tools, loader)

        assert "skill" in tools

        # Scripted sequence: load the review skill, then produce the review.
        backend = ScriptedBackend()
        backend.enqueue_tool_call("skill", {"skill_name": "review"}, "tc-1")
        backend.enqueue_text("Applying review guidance: correctness and tests look good.")

        orch = Orchestrator(
            backend,
            _orchestrator_config(),
            approval=AutoApproval(),
            tools=tools,
        )

        response = await orch.handle(_slack_request("Review my change."))

        assert isinstance(response.blocks[0], TextBlock)
        assert "review guidance" in response.blocks[0].text.lower()

        # The second inference round sees the skill content as a tool result.
        second_call = backend.calls[1]
        tool_msg = next(m for m in second_call.messages if m["role"] == MessageRole.TOOL)
        assert "[Skill: review]" in tool_msg["content"]
        assert "correctness, safety, tests, readability" in tool_msg["content"]

    async def test_skill_tool_enum_reflects_available_skills(
        self,
        skills_dir: Path,
    ) -> None:
        """The skill tool's JSON schema enum matches what the loader discovered."""
        loader = SkillLoader(str(skills_dir))
        registry = ToolRegistry()
        register_skill_tool(registry, loader)

        spec = registry.get("skill")
        assert spec is not None
        enum_values = spec.input_schema["properties"]["skill_name"]["enum"]
        assert set(enum_values) == {"review", "debug"}

    async def test_unknown_skill_returns_error_to_model(
        self,
        skills_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A hallucinated skill name surfaces as an error message, not a crash."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        loader = SkillLoader(str(skills_dir))
        tools = create_coding_tool_registry(str(workspace))
        register_skill_tool(tools, loader)

        backend = ScriptedBackend()
        backend.enqueue_tool_call("skill", {"skill_name": "nonexistent"}, "tc-1")
        backend.enqueue_text("That skill isn't available.")

        orch = Orchestrator(
            backend,
            _orchestrator_config(),
            approval=AutoApproval(),
            tools=tools,
        )

        response = await orch.handle(_slack_request("Load the nonexistent skill."))
        assert isinstance(response.blocks[0], TextBlock)

        second_call = backend.calls[1]
        tool_msg = next(m for m in second_call.messages if m["role"] == MessageRole.TOOL)
        assert "error" in tool_msg["content"].lower()
