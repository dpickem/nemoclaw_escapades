"""Tests for NMB wire protocol types — parse/serialize round-trip and validation."""

from __future__ import annotations

import json

import pytest

from nemoclaw_escapades.nmb.models import (
    ErrorCode,
    FrameValidationError,
    NMBMessage,
    Op,
    parse_frame,
    serialize_frame,
    validate_frame,
)


class TestSerializeRoundTrip:
    def test_send_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.SEND,
            to="coding-1",
            type="task.assign",
            payload={"prompt": "hello"},
        )
        raw = serialize_frame(msg)
        parsed = parse_frame(raw)
        assert parsed.op == Op.SEND
        assert parsed.to == "coding-1"
        assert parsed.type == "task.assign"
        assert parsed.payload == {"prompt": "hello"}

    def test_request_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.REQUEST,
            to="review-1",
            type="review.request",
            timeout=60.0,
            payload={"diff": "..."},
        )
        raw = serialize_frame(msg)
        parsed = parse_frame(raw)
        assert parsed.op == Op.REQUEST
        assert parsed.timeout == 60.0

    def test_reply_round_trip(self) -> None:
        msg = NMBMessage(
            op=Op.REPLY,
            reply_to="abc123",
            type="review.feedback",
            payload={"verdict": "approve"},
        )
        raw = serialize_frame(msg)
        parsed = parse_frame(raw)
        assert parsed.reply_to == "abc123"

    def test_subscribe_round_trip(self) -> None:
        msg = NMBMessage(op=Op.SUBSCRIBE, channel="progress.c1")
        raw = serialize_frame(msg)
        parsed = parse_frame(raw)
        assert parsed.op == Op.SUBSCRIBE
        assert parsed.channel == "progress.c1"

    def test_none_fields_omitted(self) -> None:
        msg = NMBMessage(op=Op.ACK, id="test-id")
        raw = serialize_frame(msg)
        data = json.loads(raw)
        assert "to" not in data
        assert "payload" not in data

    def test_from_sandbox_mapped_from_json_key(self) -> None:
        raw = json.dumps({"op": "deliver", "id": "x", "from": "orch", "type": "t", "payload": {}})
        parsed = parse_frame(raw)
        assert parsed.from_sandbox == "orch"


class TestValidation:
    def test_valid_send(self) -> None:
        msg = NMBMessage(op=Op.SEND, to="x", type="t", payload={})
        validate_frame(msg)

    def test_missing_required_field(self) -> None:
        msg = NMBMessage(op=Op.SEND, type="t", payload={})
        with pytest.raises(FrameValidationError) as exc_info:
            validate_frame(msg)
        assert exc_info.value.code == ErrorCode.INVALID_FRAME
        assert "to" in str(exc_info.value)

    def test_payload_too_large(self) -> None:
        msg = NMBMessage(op=Op.SEND, to="x", type="t", payload={"data": "x" * 11_000_000})
        with pytest.raises(FrameValidationError) as exc_info:
            validate_frame(msg)
        assert exc_info.value.code == ErrorCode.PAYLOAD_TOO_LARGE

    def test_malformed_json(self) -> None:
        with pytest.raises(FrameValidationError) as exc_info:
            parse_frame("not json")
        assert exc_info.value.code == ErrorCode.INVALID_FRAME

    def test_missing_op(self) -> None:
        with pytest.raises(FrameValidationError):
            parse_frame('{"id": "x"}')

    def test_unknown_op(self) -> None:
        with pytest.raises(FrameValidationError):
            parse_frame('{"op": "explode"}')
