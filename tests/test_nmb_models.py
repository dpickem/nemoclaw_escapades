"""Tests for NMB wire protocol types — parse/serialize round-trip and validation."""

from __future__ import annotations

import json

import pytest

from nemoclaw_escapades.nmb.models import (
    ErrorCode,
    FrameValidationError,
    NMBMessage,
    Op,
)


class TestSerializeRoundTrip:
    def test_send_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.SEND,
            to_sandbox="coding-1",
            type="task.assign",
            payload={"prompt": "hello"},
        )
        raw = msg.to_json()
        parsed = NMBMessage.from_json(raw)
        assert parsed.op == Op.SEND
        assert parsed.to_sandbox == "coding-1"
        assert parsed.type == "task.assign"
        assert parsed.payload == {"prompt": "hello"}

    def test_request_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.REQUEST,
            to_sandbox="review-1",
            type="review.request",
            timeout=60.0,
            payload={"diff": "..."},
        )
        raw = msg.to_json()
        parsed = NMBMessage.from_json(raw)
        assert parsed.op == Op.REQUEST
        assert parsed.timeout == 60.0

    def test_reply_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.REPLY,
            reply_to="abc123",
            type="review.feedback",
            payload={"verdict": "approve"},
        )
        raw = msg.to_json()
        parsed = NMBMessage.from_json(raw)
        assert parsed.reply_to == "abc123"

    def test_subscribe_round_trip(self) -> None:
        msg = NMBMessage(op=Op.SUBSCRIBE, channel="progress.c1")
        raw = msg.to_json()
        parsed = NMBMessage.from_json(raw)
        assert parsed.op == Op.SUBSCRIBE
        assert parsed.channel == "progress.c1"

    def test_none_fields_omitted(self) -> None:
        msg = NMBMessage(op=Op.ACK, id="test-id")
        raw = msg.to_json()
        data = json.loads(raw)
        assert "to_sandbox" not in data
        assert "payload" not in data

    def test_wire_keys_match_dataclass_fields(self) -> None:
        """Wire JSON keys must be the dataclass field names — no renaming layer."""
        msg = NMBMessage(
            op=Op.DELIVER,
            id="wire-test",
            from_sandbox="orchestrator",
            to_sandbox="target-1",
            type="task.assign",
            payload={"a": 1},
        )
        raw = msg.to_json()
        data = json.loads(raw)
        assert data["from_sandbox"] == "orchestrator"
        assert data["to_sandbox"] == "target-1"
        assert "from" not in data
        assert "to" not in data

    def test_addressing_fields_round_trip(self) -> None:
        """Ensure serialize→parse round-trip preserves both sandbox addressing fields."""
        msg = NMBMessage(
            op=Op.DELIVER,
            id="rt",
            from_sandbox="agent-1",
            to_sandbox="agent-2",
            type="t",
            payload={},
        )
        parsed = NMBMessage.from_json(msg.to_json())
        assert parsed.from_sandbox == "agent-1"
        assert parsed.to_sandbox == "agent-2"


class TestValidation:
    def test_valid_send(self) -> None:
        msg = NMBMessage(op=Op.SEND, to_sandbox="x", type="t", payload={})
        msg.validate()

    def test_missing_required_field(self) -> None:
        msg = NMBMessage(op=Op.SEND, type="t", payload={})
        with pytest.raises(FrameValidationError) as exc_info:
            msg.validate()
        assert exc_info.value.code == ErrorCode.INVALID_FRAME
        assert "to_sandbox" in str(exc_info.value)

    def test_payload_too_large(self) -> None:
        msg = NMBMessage(op=Op.SEND, to_sandbox="x", type="t", payload={"data": "x" * 11_000_000})
        with pytest.raises(FrameValidationError) as exc_info:
            msg.validate()
        assert exc_info.value.code == ErrorCode.PAYLOAD_TOO_LARGE

    def test_malformed_json(self) -> None:
        with pytest.raises(FrameValidationError) as exc_info:
            NMBMessage.from_json("not json")
        assert exc_info.value.code == ErrorCode.INVALID_FRAME

    def test_missing_op(self) -> None:
        with pytest.raises(FrameValidationError):
            NMBMessage.from_json('{"id": "x"}')

    def test_unknown_op(self) -> None:
        with pytest.raises(FrameValidationError):
            NMBMessage.from_json('{"op": "explode"}')
