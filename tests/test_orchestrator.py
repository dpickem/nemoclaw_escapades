"""Tests for the Orchestrator — multi-turn conversation, error handling, history,
transcript repair integration, and approval gate.
"""

from __future__ import annotations

import pytest

from nemoclaw_escapades.config import OrchestratorConfig
from nemoclaw_escapades.models.types import (
    ApprovalResult,
    ErrorCategory,
    InferenceError,
    InferenceRequest,
    InferenceResponse,
    NormalizedRequest,
    TextBlock,
)
from nemoclaw_escapades.orchestrator import Orchestrator
from nemoclaw_escapades.orchestrator.approval import ApprovalGate
from nemoclaw_escapades.orchestrator.transcript_repair import EMPTY_RESPONSE_FALLBACK
from tests.conftest import MockBackend


class TestOrchestrator:
    """Core orchestrator flow tests."""

    @pytest.fixture
    def orchestrator(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> Orchestrator:
        return Orchestrator(mock_backend, orchestrator_config)

    async def test_handle_returns_rich_response(
        self, orchestrator: Orchestrator, sample_request: NormalizedRequest
    ) -> None:
        response = await orchestrator.handle(sample_request)
        assert response.channel_id == sample_request.channel_id
        assert response.thread_ts == sample_request.thread_ts
        assert len(response.blocks) == 1
        assert isinstance(response.blocks[0], TextBlock)

    async def test_handle_calls_backend(
        self,
        orchestrator: Orchestrator,
        mock_backend: MockBackend,
        sample_request: NormalizedRequest,
    ) -> None:
        await orchestrator.handle(sample_request)
        assert len(mock_backend.calls) == 1
        call = mock_backend.calls[0]
        assert any(m["role"] == "system" for m in call.messages)
        assert any(m["content"] == sample_request.text for m in call.messages)

    async def test_response_contains_model_output(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> None:
        mock_backend.response_text = "The answer is 42."
        orch = Orchestrator(mock_backend, orchestrator_config)
        request = NormalizedRequest(
            text="What is the answer?",
            user_id="U1",
            channel_id="C1",
            timestamp=0,
            source="test",
        )
        response = await orch.handle(request)
        assert isinstance(response.blocks[0], TextBlock)
        assert response.blocks[0].text == "The answer is 42."


class TestMultiTurnHistory:
    """Thread history management tests."""

    @pytest.fixture
    def orchestrator(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> Orchestrator:
        return Orchestrator(mock_backend, orchestrator_config)

    async def test_history_accumulates_across_messages(
        self,
        orchestrator: Orchestrator,
        mock_backend: MockBackend,
    ) -> None:
        thread = "thread-1"
        for i in range(3):
            req = NormalizedRequest(
                text=f"Message {i}",
                user_id="U1",
                channel_id="C1",
                thread_ts=thread,
                timestamp=float(i),
                source="test",
            )
            await orchestrator.handle(req)

        assert len(mock_backend.calls) == 3
        last_call = mock_backend.calls[2]
        user_messages = [m for m in last_call.messages if m["role"] == "user"]
        assert len(user_messages) == 3

    async def test_history_truncation(self, mock_backend: MockBackend) -> None:
        config = OrchestratorConfig(
            system_prompt_path="nonexistent.md",
            max_thread_history=4,
        )
        orch = Orchestrator(mock_backend, config)
        thread = "thread-truncate"

        for i in range(10):
            req = NormalizedRequest(
                text=f"msg-{i}",
                user_id="U1",
                channel_id="C1",
                thread_ts=thread,
                timestamp=float(i),
                source="test",
            )
            await orch.handle(req)

        last_call = mock_backend.calls[-1]
        non_system = [m for m in last_call.messages if m["role"] != "system"]
        assert len(non_system) <= 4

    async def test_separate_threads_have_separate_history(
        self,
        orchestrator: Orchestrator,
        mock_backend: MockBackend,
    ) -> None:
        for thread in ("thread-a", "thread-b"):
            req = NormalizedRequest(
                text=f"Hello from {thread}",
                user_id="U1",
                channel_id="C1",
                thread_ts=thread,
                timestamp=0,
                source="test",
            )
            await orchestrator.handle(req)

        call_a = mock_backend.calls[0]
        call_b = mock_backend.calls[1]
        user_a = [m for m in call_a.messages if m["role"] == "user"]
        user_b = [m for m in call_b.messages if m["role"] == "user"]
        assert len(user_a) == 1
        assert len(user_b) == 1


class TestErrorHandling:
    """Error response tests."""

    async def test_inference_error_returns_user_message(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        class FailingBackend(MockBackend):
            async def complete(self, request: InferenceRequest) -> InferenceResponse:
                raise InferenceError("boom", category=ErrorCategory.TIMEOUT)

        orch = Orchestrator(FailingBackend(), orchestrator_config)
        req = NormalizedRequest(
            text="test",
            user_id="U1",
            channel_id="C1",
            timestamp=0,
            source="test",
        )
        response = await orch.handle(req)
        assert len(response.blocks) == 1
        assert isinstance(response.blocks[0], TextBlock)
        assert "time" in response.blocks[0].text.lower()
        assert response.error_category is ErrorCategory.TIMEOUT

    async def test_unexpected_error_returns_generic_message(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        class CrashingBackend(MockBackend):
            async def complete(self, request: InferenceRequest) -> InferenceResponse:
                raise RuntimeError("unexpected")

        orch = Orchestrator(CrashingBackend(), orchestrator_config)
        req = NormalizedRequest(
            text="test",
            user_id="U1",
            channel_id="C1",
            timestamp=0,
            source="test",
        )
        response = await orch.handle(req)
        assert len(response.blocks) == 1
        assert isinstance(response.blocks[0], TextBlock)
        assert "unexpected" in response.blocks[0].text.lower()
        assert response.error_category is ErrorCategory.UNKNOWN

    async def test_auth_error_returns_config_message(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        class AuthFailBackend(MockBackend):
            async def complete(self, request: InferenceRequest) -> InferenceResponse:
                raise InferenceError("unauthorized", category=ErrorCategory.AUTH_ERROR)

        orch = Orchestrator(AuthFailBackend(), orchestrator_config)
        req = NormalizedRequest(
            text="test",
            user_id="U1",
            channel_id="C1",
            timestamp=0,
            source="test",
        )
        response = await orch.handle(req)
        block = response.blocks[0]
        assert isinstance(block, TextBlock)
        assert "configuration" in block.text.lower()
        assert response.error_category is ErrorCategory.AUTH_ERROR

    async def test_failed_inference_does_not_leave_partial_history(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        class FlakyBackend(MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self._attempt = 0

            async def complete(self, request: InferenceRequest) -> InferenceResponse:
                self._attempt += 1
                if self._attempt == 1:
                    raise InferenceError("timeout", category=ErrorCategory.TIMEOUT)
                return await super().complete(request)

        orch = Orchestrator(FlakyBackend(), orchestrator_config)
        thread = "thread-retry"
        req1 = NormalizedRequest(
            text="first",
            user_id="U1",
            channel_id="C1",
            thread_ts=thread,
            timestamp=0,
            source="test",
        )
        await orch.handle(req1)
        assert orch._prompt.thread_history[thread] == []

        req2 = NormalizedRequest(
            text="second",
            user_id="U1",
            channel_id="C1",
            thread_ts=thread,
            timestamp=1,
            source="test",
        )
        await orch.handle(req2)
        user_msgs = [m for m in orch._prompt.thread_history[thread] if m["role"] == "user"]
        assert user_msgs == [{"role": "user", "content": "second"}]


class TestTranscriptRepairIntegration:
    """Transcript repair wired into the orchestrator."""

    async def test_empty_response_returns_fallback(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        backend = MockBackend(response_text="")
        orch = Orchestrator(backend, orchestrator_config)
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        assert isinstance(response.blocks[0], TextBlock)
        assert response.blocks[0].text == EMPTY_RESPONSE_FALLBACK

    async def test_truncated_response_triggers_continuation(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        backend = MockBackend()
        backend.set_response_sequence(
            [
                ("Part one...", "length"),
                (" Part two.", "stop"),
            ]
        )
        orch = Orchestrator(backend, orchestrator_config)
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        assert isinstance(response.blocks[0], TextBlock)
        assert "Part one..." in response.blocks[0].text
        assert "Part two." in response.blocks[0].text
        assert len(backend.calls) == 2

    async def test_continuation_sends_resume_prompt(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        backend = MockBackend()
        backend.set_response_sequence(
            [
                ("Partial", "length"),
                (" done.", "stop"),
            ]
        )
        orch = Orchestrator(backend, orchestrator_config)
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        await orch.handle(req)
        continuation_call = backend.calls[1]
        user_msgs = [m for m in continuation_call.messages if m["role"] == "user"]
        assert any("resume" in m["content"].lower() for m in user_msgs)
        assistant_msgs = [m for m in continuation_call.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Partial"

    async def test_exhausted_continuations_returns_partial(
        self, orchestrator_config: OrchestratorConfig
    ) -> None:
        backend = MockBackend()
        backend.set_response_sequence(
            [
                ("Part 1", "length"),
                ("Part 2", "length"),
                ("Part 3", "length"),
            ]
        )
        orch = Orchestrator(backend, orchestrator_config)
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        assert isinstance(response.blocks[0], TextBlock)
        assert "Part 1" in response.blocks[0].text
        assert len(backend.calls) == 3


class TestApprovalGateIntegration:
    """Approval gate wired into the orchestrator."""

    async def test_auto_approval_lets_response_through(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> None:
        orch = Orchestrator(mock_backend, orchestrator_config)
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        assert isinstance(response.blocks[0], TextBlock)
        assert response.blocks[0].text == mock_backend.response_text

    async def test_denial_replaces_response(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> None:
        class DenyGate(ApprovalGate):
            async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
                return ApprovalResult(approved=False, reason="policy_violation")

        orch = Orchestrator(mock_backend, orchestrator_config, approval=DenyGate())
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        block = response.blocks[0]
        assert isinstance(block, TextBlock)
        assert "not approved" in block.text.lower()

    async def test_custom_approval_gate_accepted(
        self, mock_backend: MockBackend, orchestrator_config: OrchestratorConfig
    ) -> None:
        class ConditionalGate(ApprovalGate):
            async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
                return ApprovalResult(approved=True, reason="allowed")

        orch = Orchestrator(mock_backend, orchestrator_config, approval=ConditionalGate())
        req = NormalizedRequest(
            text="test", user_id="U1", channel_id="C1", timestamp=0, source="test"
        )
        response = await orch.handle(req)
        assert response.blocks[0].text == mock_backend.response_text
