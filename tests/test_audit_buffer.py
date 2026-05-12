"""Tests for sub-agent audit buffering and central ingest."""

from __future__ import annotations

from pathlib import Path

import pytest

from nemoclaw_escapades.agent.audit_buffer import AuditBuffer
from nemoclaw_escapades.audit.db import AuditDB


class TestAuditBuffer:
    @pytest.mark.asyncio
    async def test_buffers_tool_call_and_builds_flush_payload(self) -> None:
        buf = AuditBuffer(
            workflow_id="wf-1",
            parent_sandbox_id="orchestrator",
            agent_id="coding-1",
        )
        row_id = await buf.log_tool_call(
            service="files",
            command="read_file",
            args='{"path":"README.md"}',
            operation_type="READ",
            duration_ms=3.0,
            success=True,
            response_payload="contents",
        )
        payload = buf.to_payload()
        assert payload.workflow_id == "wf-1"
        assert payload.tool_calls[0].id == row_id
        assert payload.tool_calls[0].command == "read_file"

    @pytest.mark.asyncio
    async def test_flush_ingest_is_idempotent(self, tmp_path: Path) -> None:
        buf = AuditBuffer(
            workflow_id="wf-1",
            parent_sandbox_id="orchestrator",
            agent_id="coding-1",
        )
        await buf.log_tool_call(
            row_id="stable-row",
            service="files",
            command="write_file",
            args="{}",
            operation_type="WRITE",
            duration_ms=4.0,
            success=True,
            response_payload="ok",
        )
        db = AuditDB(str(tmp_path / "audit.db"))
        await db.open()
        try:
            assert await db.ingest_audit_flush(buf.to_payload()) == 1
            assert await db.ingest_audit_flush(buf.to_payload()) == 1
            rows = await db.query("SELECT * FROM tool_calls")
            assert len(rows) == 1
            row = rows[0]
            assert row["id"] == "stable-row"
            assert row["workflow_id"] == "wf-1"
            assert row["parent_sandbox_id"] == "orchestrator"
            assert row["agent_id"] == "coding-1"
            assert row["agent_role"] == "coding"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_jsonl_fallback_round_trip(self, tmp_path: Path) -> None:
        buf = AuditBuffer(
            workflow_id="wf-1",
            parent_sandbox_id="orchestrator",
            agent_id="coding-1",
        )
        await buf.log_tool_call(
            row_id="row-bash",
            service="bash",
            command="bash",
            args='{"command":"true"}',
            operation_type="READ",
            duration_ms=1.0,
            success=True,
        )
        path = buf.write_jsonl_fallback(tmp_path / "audit.jsonl")
        assert path is not None
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        # Each row is independently parseable JSON containing both
        # the envelope and the per-row tool_call body.
        import json as _json

        row = _json.loads(lines[0])
        assert row["workflow_id"] == "wf-1"
        assert row["parent_sandbox_id"] == "orchestrator"
        assert row["agent_id"] == "coding-1"
        assert row["agent_role"] == "coding"
        assert row["tool_call"]["id"] == "row-bash"
        assert row["tool_call"]["command"] == "bash"

    def test_jsonl_fallback_skips_when_empty(self, tmp_path: Path) -> None:
        """Empty buffers don't pollute the workspace with zero-row files."""
        buf = AuditBuffer(
            workflow_id="wf-empty",
            parent_sandbox_id="orchestrator",
            agent_id="coding-1",
        )
        path = tmp_path / "audit.jsonl"
        result = buf.write_jsonl_fallback(path)
        assert result is None
        assert not path.exists()

    def test_is_empty_property(self) -> None:
        buf = AuditBuffer(
            workflow_id="wf-2",
            parent_sandbox_id="orchestrator",
            agent_id="coding-2",
        )
        assert buf.is_empty is True

    @pytest.mark.asyncio
    async def test_is_empty_false_after_log(self) -> None:
        buf = AuditBuffer(
            workflow_id="wf-3",
            parent_sandbox_id="orchestrator",
            agent_id="coding-3",
        )
        await buf.log_tool_call(
            service="files",
            command="read_file",
            args="{}",
            operation_type="READ",
            duration_ms=1.0,
            success=True,
        )
        assert buf.is_empty is False
