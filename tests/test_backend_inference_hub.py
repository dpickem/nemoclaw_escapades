"""Tests for the InferenceHubBackend — retry logic, error categorization, parsing."""

from __future__ import annotations

import httpx
import pytest
import respx

from nemoclaw_escapades.backends.inference_hub import InferenceHubBackend
from nemoclaw_escapades.config import InferenceConfig
from nemoclaw_escapades.models.types import ErrorCategory, InferenceError, InferenceRequest

COMPLETIONS_URL = "https://test.example.com/v1/chat/completions"

VALID_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


@pytest.fixture
def config() -> InferenceConfig:
    return InferenceConfig(
        base_url="https://test.example.com/v1",
        api_key="test-key",
        model="test-model",
        timeout_s=5,
        max_retries=2,
    )


@pytest.fixture
def backend(config: InferenceConfig) -> InferenceHubBackend:
    return InferenceHubBackend(config)


@pytest.fixture
def inference_request() -> InferenceRequest:
    return InferenceRequest(
        messages=[{"role": "user", "content": "Hi"}],
        model="test-model",
    )


class TestSuccessfulCalls:

    @respx.mock
    async def test_successful_completion(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).respond(200, json=VALID_RESPONSE)

        result = await backend.complete(inference_request)
        assert result.content == "Hello!"
        assert result.model == "test-model"
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.usage.total_tokens == 15
        assert result.latency_ms > 0

    @respx.mock
    async def test_auth_header_is_sent(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        route = respx.post(COMPLETIONS_URL).respond(200, json=VALID_RESPONSE)
        await backend.complete(inference_request)
        assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


class TestRetryLogic:

    @respx.mock
    async def test_retries_on_429(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).side_effect = [
            httpx.Response(429, text="rate limited"),
            httpx.Response(200, json=VALID_RESPONSE),
        ]

        result = await backend.complete(inference_request)
        assert result.content == "Hello!"

    @respx.mock
    async def test_retries_on_500(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).side_effect = [
            httpx.Response(500, text="server error"),
            httpx.Response(200, json=VALID_RESPONSE),
        ]

        result = await backend.complete(inference_request)
        assert result.content == "Hello!"

    @respx.mock
    async def test_exhausted_retries_raises(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).side_effect = [
            httpx.Response(500, text="server error"),
            httpx.Response(500, text="server error"),
        ]

        with pytest.raises(InferenceError) as exc_info:
            await backend.complete(inference_request)
        assert exc_info.value.category == ErrorCategory.MODEL_ERROR


class TestErrorCategorization:

    @respx.mock
    async def test_401_raises_auth_error(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).respond(401, text="unauthorized")

        with pytest.raises(InferenceError) as exc_info:
            await backend.complete(inference_request)
        assert exc_info.value.category == ErrorCategory.AUTH_ERROR

    @respx.mock
    async def test_403_raises_auth_error(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).respond(403, text="forbidden")

        with pytest.raises(InferenceError) as exc_info:
            await backend.complete(inference_request)
        assert exc_info.value.category == ErrorCategory.AUTH_ERROR

    @respx.mock
    async def test_malformed_response_raises_model_error(
        self, backend: InferenceHubBackend, inference_request: InferenceRequest
    ) -> None:
        respx.post(COMPLETIONS_URL).respond(200, json={"choices": []})

        with pytest.raises(InferenceError) as exc_info:
            await backend.complete(inference_request)
        assert exc_info.value.category == ErrorCategory.MODEL_ERROR
