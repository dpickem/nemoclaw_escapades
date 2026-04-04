"""Tests for the transcript repair layer."""

from __future__ import annotations

from nemoclaw_escapades.models.types import InferenceResponse, TokenUsage
from nemoclaw_escapades.orchestrator.transcript_repair import (
    EMPTY_RESPONSE_FALLBACK,
    RepairReason,
    repair_response,
)


def _make_response(
    content: str = "Hello!",
    finish_reason: str = "stop",
) -> InferenceResponse:
    return InferenceResponse(
        content=content,
        model="test-model",
        usage=TokenUsage(),
        latency_ms=10.0,
        finish_reason=finish_reason,
    )


class TestRepairResponse:

    def test_normal_response_not_repaired(self) -> None:
        result = repair_response(_make_response("Normal reply"))
        assert result.content == "Normal reply"
        assert result.was_repaired is False
        assert result.needs_continuation is False

    def test_empty_response_replaced(self) -> None:
        result = repair_response(_make_response(""))
        assert result.content == EMPTY_RESPONSE_FALLBACK
        assert result.was_repaired is True
        assert result.repair_reason is RepairReason.EMPTY_RESPONSE

    def test_whitespace_only_response_replaced(self) -> None:
        result = repair_response(_make_response("   \n\t  "))
        assert result.content == EMPTY_RESPONSE_FALLBACK
        assert result.was_repaired is True
        assert result.repair_reason is RepairReason.EMPTY_RESPONSE

    def test_truncated_response_needs_continuation(self) -> None:
        result = repair_response(_make_response("Partial output...", "length"))
        assert result.content == "Partial output..."
        assert result.was_repaired is False
        assert result.needs_continuation is True
        assert result.repair_reason is RepairReason.TRUNCATED

    def test_content_filter_replaced(self) -> None:
        result = repair_response(_make_response("blocked", "content_filter"))
        assert result.was_repaired is True
        assert result.repair_reason is RepairReason.CONTENT_FILTER
        assert "filtered" in result.content.lower()

    def test_none_content_treated_as_empty(self) -> None:
        resp = _make_response("")
        resp.content = ""
        result = repair_response(resp)
        assert result.was_repaired is True
        assert result.repair_reason is RepairReason.EMPTY_RESPONSE
