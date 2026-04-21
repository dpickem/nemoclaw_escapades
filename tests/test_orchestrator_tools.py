"""Tests for the orchestrator's tool-use loop."""

from __future__ import annotations

import json

import pytest

from nemoclaw_escapades.agent.approval import WriteApproval
from nemoclaw_escapades.config import OrchestratorConfig
from nemoclaw_escapades.models.types import (
    ActionBlock,
    ActionPayload,
    InferenceRequest,
    InferenceResponse,
    NormalizedRequest,
    TextBlock,
    TokenUsage,
    ToolCall,
)
from nemoclaw_escapades.orchestrator.orchestrator import (
    APPROVAL_ACTION_APPROVE,
    APPROVAL_ACTION_DENY,
    Orchestrator,
)
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec
from tests.conftest import MockBackend


class ToolMockBackend(MockBackend):
    """Backend that can return tool_calls in its responses."""

    def __init__(self) -> None:
        super().__init__()
        self._responses: list[InferenceResponse] = []
        self._resp_idx = 0

    def add_response(
        self,
        content: str = "",
        finish_reason: str = "stop",
        tool_calls: list[ToolCall] | None = None,
    ) -> None:
        self._responses.append(
            InferenceResponse(
                content=content,
                model="mock-model",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                latency_ms=42.0,
                finish_reason=finish_reason,
                tool_calls=tool_calls,
            )
        )

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        self.calls.append(request)
        if self._resp_idx < len(self._responses):
            resp = self._responses[self._resp_idx]
            self._resp_idx += 1
            return resp
        return await super().complete(request)


async def _mock_tool(message: str = "default") -> str:
    return json.dumps({"result": f"processed: {message}"})


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
            },
            handler=_mock_tool,
        )
    )
    return reg


@pytest.fixture
def orchestrator_config() -> OrchestratorConfig:
    return OrchestratorConfig(system_prompt_path="nonexistent.md")


@pytest.fixture
def sample_request() -> NormalizedRequest:
    return NormalizedRequest(
        text="Hello!",
        user_id="U123",
        channel_id="C123",
        thread_ts="123.456",
        timestamp=1700000000.0,
        source="slack",
    )


class TestOrchestratorWithTools:
    async def test_text_response_without_tool_calls(
        self,
        tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """When model returns text without tools, behave normally."""
        backend = ToolMockBackend()
        backend.add_response(content="Hello there!", finish_reason="stop")

        orch = Orchestrator(backend, orchestrator_config, tools=tool_registry)
        resp = await orch.handle(sample_request)

        assert resp.blocks[0].text == "Hello there!"  # type: ignore[union-attr]
        assert len(backend.calls) == 1
        assert backend.calls[0].tools is not None

    async def test_single_tool_call_round_trip(
        self,
        tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Model calls a tool, gets result, then responds with text."""
        backend = ToolMockBackend()

        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(id="call_1", name="test_tool", arguments='{"message": "hello"}'),
            ],
        )
        backend.add_response(
            content="The tool returned: processed hello",
            finish_reason="stop",
        )

        orch = Orchestrator(backend, orchestrator_config, tools=tool_registry)
        resp = await orch.handle(sample_request)

        assert "processed hello" in resp.blocks[0].text  # type: ignore[union-attr]
        assert len(backend.calls) == 2

        second_call_messages = backend.calls[1].messages
        tool_msg = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_msg) == 1
        assert "processed: hello" in tool_msg[0]["content"]

    async def test_multiple_tool_calls_in_one_round(
        self,
        tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Model calls multiple tools in parallel."""
        backend = ToolMockBackend()

        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(id="call_1", name="test_tool", arguments='{"message": "a"}'),
                ToolCall(id="call_2", name="test_tool", arguments='{"message": "b"}'),
            ],
        )
        backend.add_response(
            content="Got both results",
            finish_reason="stop",
        )

        orch = Orchestrator(backend, orchestrator_config, tools=tool_registry)
        resp = await orch.handle(sample_request)

        assert resp.blocks[0].text == "Got both results"  # type: ignore[union-attr]
        tool_msgs = [m for m in backend.calls[1].messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2

    async def test_multi_round_tool_use(
        self,
        tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Model calls tools across multiple rounds before responding."""
        backend = ToolMockBackend()

        # Round 1: tool call
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(id="call_1", name="test_tool", arguments='{"message": "step1"}'),
            ],
        )
        # Round 2: another tool call
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(id="call_2", name="test_tool", arguments='{"message": "step2"}'),
            ],
        )
        # Round 3: final text
        backend.add_response(
            content="Done after two tool rounds",
            finish_reason="stop",
        )

        orch = Orchestrator(backend, orchestrator_config, tools=tool_registry)
        resp = await orch.handle(sample_request)

        assert "Done after two tool rounds" in resp.blocks[0].text  # type: ignore[union-attr]
        assert len(backend.calls) == 3

    async def test_tool_call_error_handled(
        self,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """When a tool raises, the error is sent back as a tool result."""

        async def _failing_tool(**kwargs: object) -> str:
            raise RuntimeError("tool exploded")

        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="fail_tool",
                description="Fails",
                input_schema={},
                handler=_failing_tool,
            )
        )

        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="call_1", name="fail_tool", arguments="{}")],
        )
        backend.add_response(
            content="The tool failed, here's what I know",
            finish_reason="stop",
        )

        orch = Orchestrator(backend, orchestrator_config, tools=reg)
        resp = await orch.handle(sample_request)

        assert "tool failed" in resp.blocks[0].text  # type: ignore[union-attr]
        tool_msg = [m for m in backend.calls[1].messages if m.get("role") == "tool"]
        assert len(tool_msg) == 1
        assert "tool exploded" in tool_msg[0]["content"]

    async def test_no_tools_uses_legacy_path(
        self,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Without tools, the legacy _inference_with_repair path is used."""
        backend = MockBackend(response_text="No tools here")
        orch = Orchestrator(backend, orchestrator_config)
        resp = await orch.handle(sample_request)

        assert resp.blocks[0].text == "No tools here"  # type: ignore[union-attr]
        assert len(backend.calls) == 1
        assert backend.calls[0].tools is None

    async def test_tools_included_in_inference_request(
        self,
        tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Tool definitions are sent to the model in the inference request."""
        backend = ToolMockBackend()
        backend.add_response(content="hi", finish_reason="stop")

        orch = Orchestrator(backend, orchestrator_config, tools=tool_registry)
        await orch.handle(sample_request)

        assert backend.calls[0].tools is not None
        assert len(backend.calls[0].tools) == 1
        assert backend.calls[0].tools[0].function.name == "test_tool"


# ── Write-tool fixtures ──────────────────────────────────────────────


async def _mock_write_tool(project_key: str = "PROJ", summary: str = "x") -> str:
    return json.dumps({"key": f"{project_key}-999", "summary": summary})


@pytest.fixture
def write_tool_registry() -> ToolRegistry:
    """Registry with both a read tool and a write tool."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="test_tool",
            description="A read tool",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            handler=_mock_tool,
            is_read_only=True,
        )
    )
    reg.register(
        ToolSpec(
            name="jira_create_issue",
            description="Create a Jira issue (write)",
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["project_key", "summary"],
            },
            handler=_mock_write_tool,
            is_read_only=False,
        )
    )
    return reg


def _make_approval_action_request(
    action_id: str,
    thread_ts: str = "123.456",
) -> NormalizedRequest:
    return NormalizedRequest(
        text="",
        user_id="U123",
        channel_id="C123",
        thread_ts=thread_ts,
        timestamp=1700000000.0,
        source="slack",
        action=ActionPayload(
            action_id=action_id,
            value=thread_ts,
        ),
    )


# ── Approval-gate tests ─────────────────────────────────────────────


class TestWriteApprovalGate:
    """Verify that write tool calls are blocked until the user approves."""

    async def test_write_tool_returns_approval_prompt(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """A write tool call should produce Approve/Deny buttons, not execute."""
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call_w1",
                    name="jira_create_issue",
                    arguments='{"project_key": "PROJ", "summary": "Test ticket"}',
                ),
            ],
        )

        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )
        resp = await orch.handle(sample_request)

        assert any(isinstance(b, TextBlock) and "approval" in b.text.lower() for b in resp.blocks)
        assert any(isinstance(b, ActionBlock) for b in resp.blocks)
        action_block = next(b for b in resp.blocks if isinstance(b, ActionBlock))
        ids = {a.action_id for a in action_block.actions}
        assert APPROVAL_ACTION_APPROVE in ids
        assert APPROVAL_ACTION_DENY in ids
        # Only one inference call — tool was NOT executed.
        assert len(backend.calls) == 1

    async def test_approve_executes_tool_and_resumes(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Clicking Approve should execute the tool and return a model response."""
        backend = ToolMockBackend()
        # Round 1: model requests a write tool
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call_w1",
                    name="jira_create_issue",
                    arguments='{"project_key": "PROJ", "summary": "Test"}',
                ),
            ],
        )
        # Round 2 (after approval): model sees tool result and responds
        backend.add_response(
            content="Created PROJ-999 for you.",
            finish_reason="stop",
        )

        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )

        # Step 1: initial request triggers approval prompt
        resp1 = await orch.handle(sample_request)
        assert any(isinstance(b, ActionBlock) for b in resp1.blocks)

        # Step 2: user clicks Approve
        approve_req = _make_approval_action_request(
            APPROVAL_ACTION_APPROVE,
            thread_ts=sample_request.thread_ts or "",
        )
        resp2 = await orch.handle(approve_req)

        assert isinstance(resp2.blocks[0], TextBlock)
        assert "PROJ-999" in resp2.blocks[0].text
        # Two inference calls: the initial one + the post-approval one.
        assert len(backend.calls) == 2

    async def test_deny_cancels_without_executing(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Clicking Deny should cancel and never execute the tool."""
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call_w1",
                    name="jira_create_issue",
                    arguments='{"project_key": "PROJ", "summary": "Test"}',
                ),
            ],
        )

        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )

        await orch.handle(sample_request)

        deny_req = _make_approval_action_request(
            APPROVAL_ACTION_DENY,
            thread_ts=sample_request.thread_ts or "",
        )
        resp = await orch.handle(deny_req)

        assert isinstance(resp.blocks[0], TextBlock)
        assert "cancelled" in resp.blocks[0].text.lower()
        # Only one inference call — no post-denial call.
        assert len(backend.calls) == 1

    async def test_read_tools_still_auto_execute(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Read-only tool calls should execute immediately, no approval."""
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(id="call_r1", name="test_tool", arguments='{"message": "hi"}'),
            ],
        )
        backend.add_response(
            content="Here are the results.",
            finish_reason="stop",
        )

        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )
        resp = await orch.handle(sample_request)

        assert isinstance(resp.blocks[0], TextBlock)
        assert "results" in resp.blocks[0].text.lower()
        assert not any(isinstance(b, ActionBlock) for b in resp.blocks)
        assert len(backend.calls) == 2

    async def test_stale_approve_click_is_suppressed(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
    ) -> None:
        """Click Approve with no pending approval → suppressed response.

        Regression: previously this posted "No pending write operation
        found for this thread." into the channel, which the user sees
        as noise when they accidentally double-click Approve or click
        an old button after a newer prompt superseded it.
        """
        backend = ToolMockBackend()
        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )
        approve_req = _make_approval_action_request(APPROVAL_ACTION_APPROVE)
        resp = await orch.handle(approve_req)
        assert resp.suppress_post is True
        assert resp.blocks == []
        # No inference call — we didn't even engage the agent loop.
        assert len(backend.calls) == 0

    async def test_stale_deny_click_is_suppressed(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
    ) -> None:
        """Click Deny with no pending approval → suppressed response."""
        backend = ToolMockBackend()
        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )
        deny_req = _make_approval_action_request(APPROVAL_ACTION_DENY)
        resp = await orch.handle(deny_req)
        assert resp.suppress_post is True
        assert resp.blocks == []
        assert len(backend.calls) == 0

    async def test_valid_deny_still_posts_confirmation(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """Baseline: a Deny click with a real pending approval posts the usual
        cancellation message (suppress_post stays False).  Guards against an
        overzealous stale-click path accidentally silencing real user actions.
        """
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call_w1",
                    name="jira_create_issue",
                    arguments='{"project_key": "PROJ", "summary": "live"}',
                ),
            ],
        )
        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )
        await orch.handle(sample_request)
        deny_req = _make_approval_action_request(
            APPROVAL_ACTION_DENY,
            thread_ts=sample_request.thread_ts or "",
        )
        resp = await orch.handle(deny_req)
        assert resp.suppress_post is False
        assert isinstance(resp.blocks[0], TextBlock)
        assert "cancelled" in resp.blocks[0].text.lower()

    async def test_new_message_clears_stale_pending_approval(
        self,
        write_tool_registry: ToolRegistry,
        orchestrator_config: OrchestratorConfig,
        sample_request: NormalizedRequest,
    ) -> None:
        """A new user message should discard any pending approval for that thread."""
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="call_w1",
                    name="jira_create_issue",
                    arguments='{"project_key": "PROJ", "summary": "stale"}',
                ),
            ],
        )
        # Response for the second user message (no tools).
        backend.add_response(content="OK, new topic.", finish_reason="stop")

        orch = Orchestrator(
            backend,
            orchestrator_config,
            approval=WriteApproval(),
            tools=write_tool_registry,
        )

        await orch.handle(sample_request)
        thread_key = sample_request.thread_ts or sample_request.request_id
        assert thread_key in orch._pending_approvals

        # New regular message in the same thread clears pending state.
        new_msg = NormalizedRequest(
            text="Never mind, different question.",
            user_id="U123",
            channel_id="C123",
            thread_ts=sample_request.thread_ts,
            timestamp=1700000001.0,
            source="slack",
        )
        await orch.handle(new_msg)
        assert thread_key not in orch._pending_approvals
