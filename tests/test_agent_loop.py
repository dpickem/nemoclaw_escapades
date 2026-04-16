"""Tests for AgentLoop — the reusable multi-turn inference + tool execution loop."""

from __future__ import annotations

import json

import pytest

from nemoclaw_escapades.agent.approval import WriteApproval
from nemoclaw_escapades.agent.loop import AgentLoop, WriteApprovalError
from nemoclaw_escapades.agent.types import AgentLoopResult
from nemoclaw_escapades.config import AgentLoopConfig
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
    ToolCall,
)
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec
from tests.conftest import MockBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ToolMockBackend(MockBackend):
    """Backend that returns a programmable sequence of responses."""

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


async def _echo_tool(message: str = "default") -> str:
    return json.dumps({"result": f"echo: {message}"})


async def _write_tool(content: str = "") -> str:
    return json.dumps({"written": True, "content": content})


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="Echoes the input",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            handler=_echo_tool,
            is_read_only=True,
            display_name="Echoing",
            toolset="test",
        )
    )
    return reg


@pytest.fixture
def config() -> AgentLoopConfig:
    return AgentLoopConfig(
        model="test-model",
        temperature=0.0,
        max_tokens=4096,
        max_tool_rounds=10,
        max_continuation_retries=2,
    )


def _make_messages(user_text: str = "Hello") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": user_text},
    ]


# ---------------------------------------------------------------------------
# Core loop behaviour
# ---------------------------------------------------------------------------


class TestAgentLoopBasic:
    """Basic run() behaviour with and without tool calls."""

    async def test_text_response_returns_result(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = ToolMockBackend()
        backend.add_response(content="Hello!", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-1")

        assert isinstance(result, AgentLoopResult)
        assert result.content == "Hello!"
        assert result.tool_calls_made == 0
        assert result.rounds == 1
        assert result.hit_safety_limit is False

    async def test_single_tool_round_trip(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments='{"message": "hi"}')],
        )
        backend.add_response(content="Got it: hi", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-2")

        assert result.content == "Got it: hi"
        assert result.tool_calls_made == 1
        assert result.rounds == 2
        assert len(backend.calls) == 2

    async def test_multi_round_tool_use(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = ToolMockBackend()
        for i in range(3):
            backend.add_response(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(id=f"tc-{i}", name="echo", arguments=f'{{"message": "round {i}"}}')
                ],
            )
        backend.add_response(content="Done after 3 rounds", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-3")

        assert result.content == "Done after 3 rounds"
        assert result.tool_calls_made == 3
        assert result.rounds == 4
        assert len(backend.calls) == 4

    async def test_messages_not_mutated(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        """The caller's message list must not be modified by run()."""
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments='{"message": "x"}')],
        )
        backend.add_response(content="done", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        messages = _make_messages()
        original_len = len(messages)
        await loop.run(messages, request_id="req-4")

        assert len(messages) == original_len

    async def test_working_messages_in_result(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments='{"message": "x"}')],
        )
        backend.add_response(content="final", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-5")

        roles = [m["role"] for m in result.working_messages]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles


# ---------------------------------------------------------------------------
# Safety limit
# ---------------------------------------------------------------------------


class TestSafetyLimit:
    """The loop must stop after max_tool_rounds."""

    async def test_safety_limit_returns_partial(self, tool_registry: ToolRegistry) -> None:
        max_rounds = 3
        config = AgentLoopConfig(model="test", max_tool_rounds=max_rounds)
        backend = ToolMockBackend()
        for i in range(max_rounds + 1):
            backend.add_response(
                content="",
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id=f"tc-{i}", name="echo", arguments="{}")],
            )
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-limit")

        assert result.hit_safety_limit is True
        assert result.rounds == max_rounds
        assert "maximum number" in result.content.lower()


# ---------------------------------------------------------------------------
# Truncation / continuation
# ---------------------------------------------------------------------------


class TestContinuationRetries:
    """finish_reason=length triggers continuation retries."""

    async def test_truncated_response_continues(
        self, tool_registry: ToolRegistry, config: AgentLoopConfig
    ) -> None:
        backend = ToolMockBackend()
        backend.add_response(content="Part one...", finish_reason="length")
        backend.add_response(content=" Part two.", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-trunc")

        assert "Part one..." in result.content
        assert "Part two." in result.content
        assert len(backend.calls) == 2

    async def test_exhausted_continuations(self, tool_registry: ToolRegistry) -> None:
        config = AgentLoopConfig(model="test", max_continuation_retries=2)
        backend = ToolMockBackend()
        backend.add_response(content="A", finish_reason="length")
        backend.add_response(content="B", finish_reason="length")
        backend.add_response(content="C", finish_reason="length")
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        result = await loop.run(_make_messages(), request_id="req-exhaust")

        assert result.content == "ABC"


# ---------------------------------------------------------------------------
# Tool call error handling
# ---------------------------------------------------------------------------


class TestToolErrors:
    """Tool execution errors are serialised and fed back to the model."""

    async def test_tool_error_is_returned_to_model(self, config: AgentLoopConfig) -> None:
        async def _failing_tool() -> str:
            raise RuntimeError("kaboom")

        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="bad_tool",
                description="Always fails",
                input_schema={"type": "object", "properties": {}},
                handler=_failing_tool,
                toolset="test",
            )
        )
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-err", name="bad_tool", arguments="{}")],
        )
        backend.add_response(content="I see the error", finish_reason="stop")
        loop = AgentLoop(backend=backend, tools=reg, config=config)

        result = await loop.run(_make_messages(), request_id="req-err")

        assert result.content == "I see the error"
        second_call = backend.calls[1]
        tool_msgs = [m for m in second_call.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        error_payload = json.loads(tool_msgs[0]["content"])
        assert "kaboom" in error_payload["error"]


# ---------------------------------------------------------------------------
# Write approval gate
# ---------------------------------------------------------------------------


class TestWriteApproval:
    """Write tools raise WriteApprovalError when gated."""

    async def test_write_tool_raises_approval_error(self, config: AgentLoopConfig) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="write_op",
                description="A write operation",
                input_schema={
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                },
                handler=_write_tool,
                is_read_only=False,
                toolset="test",
            )
        )
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-w", name="write_op", arguments='{"content": "data"}')],
        )
        loop = AgentLoop(
            backend=backend,
            tools=reg,
            config=config,
            approval=WriteApproval(),
        )

        with pytest.raises(WriteApprovalError) as exc_info:
            await loop.run(_make_messages(), request_id="req-write")

        assert len(exc_info.value.pending.tool_calls) == 1
        assert exc_info.value.pending.tool_calls[0].name == "write_op"

    async def test_read_tools_execute_without_approval(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-r", name="echo", arguments='{"message": "safe"}')],
        )
        backend.add_response(content="done", finish_reason="stop")
        loop = AgentLoop(
            backend=backend,
            tools=tool_registry,
            config=config,
            approval=WriteApproval(),
        )

        result = await loop.run(_make_messages(), request_id="req-read")
        assert result.content == "done"
        assert result.tool_calls_made == 1


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class TestCallbacks:
    """on_tool_start and on_tool_end callbacks are invoked."""

    async def test_on_tool_start_called(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        invocations: list[str] = []

        async def _on_start(display_name: str) -> None:
            invocations.append(display_name)

        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments="{}")],
        )
        backend.add_response(content="done", finish_reason="stop")
        loop = AgentLoop(
            backend=backend,
            tools=tool_registry,
            config=config,
            on_tool_start=_on_start,
        )

        await loop.run(_make_messages(), request_id="req-cb")
        assert len(invocations) == 1
        assert "Echoing" in invocations[0]

    async def test_on_tool_end_called(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        end_invocations: list[tuple[str, float, bool]] = []

        async def _on_end(tool_name: str, duration_ms: float, success: bool) -> None:
            end_invocations.append((tool_name, duration_ms, success))

        backend = ToolMockBackend()
        backend.add_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="tc-1", name="echo", arguments="{}")],
        )
        backend.add_response(content="done", finish_reason="stop")
        loop = AgentLoop(
            backend=backend,
            tools=tool_registry,
            config=config,
            on_tool_end=_on_end,
        )

        await loop.run(_make_messages(), request_id="req-cb2")
        assert len(end_invocations) == 1
        assert end_invocations[0][0] == "echo"
        assert end_invocations[0][2] is True


# ---------------------------------------------------------------------------
# execute_tool_calls (public API for approval resume)
# ---------------------------------------------------------------------------


class TestExecuteToolCalls:
    """The public execute_tool_calls method for approval-resume flows."""

    async def test_execute_returns_tool_messages(
        self,
        tool_registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        backend = MockBackend()
        loop = AgentLoop(backend=backend, tools=tool_registry, config=config)

        results = await loop.execute_tool_calls(
            [ToolCall(id="tc-1", name="echo", arguments='{"message": "test"}')],
            request_id="req-exec",
        )

        assert len(results) == 1
        assert results[0]["role"] == "tool"
        assert results[0]["tool_call_id"] == "tc-1"
        payload = json.loads(results[0]["content"])
        assert payload["result"] == "echo: test"
