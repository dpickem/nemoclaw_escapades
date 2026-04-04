"""Shared test fixtures for the NemoClaw test suite."""

from __future__ import annotations

import pytest

from nemoclaw_escapades.backends.base import BackendBase
from nemoclaw_escapades.config import InferenceConfig, OrchestratorConfig
from nemoclaw_escapades.models.types import (
    InferenceRequest,
    InferenceResponse,
    NormalizedRequest,
    TokenUsage,
)


class MockBackend(BackendBase):
    """In-memory backend that returns canned responses for testing."""

    def __init__(
        self,
        response_text: str = "Hello from the mock model!",
        finish_reason: str = "stop",
    ) -> None:
        self.response_text = response_text
        self.finish_reason = finish_reason
        self.calls: list[InferenceRequest] = []
        self._response_sequence: list[tuple[str, str]] | None = None
        self._call_index = 0

    def set_response_sequence(self, seq: list[tuple[str, str]]) -> None:
        """Set a sequence of (content, finish_reason) for successive calls."""
        self._response_sequence = seq
        self._call_index = 0

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        self.calls.append(request)

        if self._response_sequence and self._call_index < len(self._response_sequence):
            content, reason = self._response_sequence[self._call_index]
            self._call_index += 1
        else:
            content = self.response_text
            reason = self.finish_reason

        return InferenceResponse(
            content=content,
            model="mock-model",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=42.0,
            finish_reason=reason,
            raw_response={"mock": True},
        )


@pytest.fixture
def mock_backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def orchestrator_config(tmp_path: object) -> OrchestratorConfig:
    return OrchestratorConfig(
        system_prompt_path="nonexistent.md",
        max_thread_history=50,
    )


@pytest.fixture
def inference_config() -> InferenceConfig:
    return InferenceConfig(
        base_url="https://test.example.com/v1",
        api_key="test-key",
        model="test-model",
        timeout_s=5,
        max_retries=2,
    )


@pytest.fixture
def sample_request() -> NormalizedRequest:
    return NormalizedRequest(
        text="Hello, NemoClaw!",
        user_id="U12345",
        channel_id="C12345",
        thread_ts="1234567890.123456",
        timestamp=1700000000.0,
        source="slack",
    )


@pytest.fixture
def sample_slack_event() -> dict:
    return {
        "type": "message",
        "text": "Hello, NemoClaw!",
        "user": "U12345",
        "channel": "C12345",
        "ts": "1234567890.123456",
        "thread_ts": "1234567890.000000",
    }
