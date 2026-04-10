"""Tests for the audit database — tool-call logging, FTS, and queries."""

from __future__ import annotations

from pathlib import Path

import pytest

from nemoclaw_escapades.audit.db import AuditDB


@pytest.fixture
async def db(tmp_path: Path) -> AuditDB:
    """Provide an AuditDB pointed at a temp directory."""
    db_path = str(tmp_path / "test_tool_calls.db")
    audit_db = AuditDB(db_path)
    await audit_db.open()
    yield audit_db  # type: ignore[misc]
    await audit_db.close()


class TestToolCallAudit:
    async def test_migration_creates_tables(self, db: AuditDB) -> None:
        rows = await db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r["name"] for r in rows}
        assert "tool_calls" in names
        assert "messages" in names

    async def test_log_and_query(self, db: AuditDB) -> None:
        row_id = await db.log_tool_call(
            service="jira",
            command="search",
            args="project = MYPROJ",
            operation_type="READ",
            duration_ms=123.4,
            success=True,
            response_payload='{"success": true}',
        )
        assert row_id

        rows = await db.query("SELECT * FROM tool_calls WHERE id = :id", {"id": row_id})
        assert len(rows) == 1
        assert rows[0]["service"] == "jira"
        assert rows[0]["command"] == "search"
        assert rows[0]["operation_type"] == "READ"
        assert rows[0]["success"] == 1

    async def test_log_write_with_approval(self, db: AuditDB) -> None:
        row_id = await db.log_tool_call(
            service="jira",
            command="create-issue",
            args="--project PROJ --summary test",
            operation_type="WRITE",
            approval_status="approved",
            approved_by="U12345",
            approval_time_ms=2500.0,
            exit_code=0,
            duration_ms=456.7,
            success=True,
            response_payload='{"key": "PROJ-999"}',
        )

        rows = await db.query("SELECT * FROM tool_calls WHERE id = :id", {"id": row_id})
        assert rows[0]["approval_status"] == "approved"
        assert rows[0]["approved_by"] == "U12345"
        assert rows[0]["operation_type"] == "WRITE"

    async def test_log_failure(self, db: AuditDB) -> None:
        row_id = await db.log_tool_call(
            service="gitlab",
            command="list-mrs",
            args="--project foo",
            operation_type="READ",
            exit_code=1,
            duration_ms=100.0,
            success=False,
            error_code="SUBPROCESS_ERROR",
            error_message="exit code 1",
        )

        rows = await db.query("SELECT * FROM tool_calls WHERE id = :id", {"id": row_id})
        assert rows[0]["success"] == 0
        assert rows[0]["error_code"] == "SUBPROCESS_ERROR"

    async def test_payload_persistence_disabled(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "no_payloads.db")
        db = AuditDB(db_path, persist_payloads=False)
        await db.open()
        try:
            row_id = await db.log_tool_call(
                service="jira",
                command="search",
                args="test",
                operation_type="READ",
                duration_ms=50.0,
                success=True,
                response_payload='{"big": "payload"}',
            )

            rows = await db.query("SELECT * FROM tool_calls WHERE id = :id", {"id": row_id})
            assert rows[0]["response_payload"] == ""
            assert rows[0]["payload_size"] > 0
        finally:
            await db.close()

    async def test_query_rejects_non_select(self, db: AuditDB) -> None:
        with pytest.raises(ValueError, match="Only SELECT"):
            await db.query("DELETE FROM tool_calls")

    async def test_export_tool_calls_jsonl(self, db: AuditDB, tmp_path: Path) -> None:
        await db.log_tool_call(
            service="jira",
            command="search",
            args="test",
            operation_type="READ",
            duration_ms=50.0,
            success=True,
        )
        await db.log_tool_call(
            service="gerrit",
            command="get-change",
            args="12345",
            operation_type="READ",
            duration_ms=75.0,
            success=True,
        )

        export_path = str(tmp_path / "export.jsonl")
        count = await db.export_tool_calls_jsonl(export_path)
        assert count == 2

        with open(export_path) as f:
            lines = f.readlines()
        assert len(lines) == 2

    async def test_export_tool_calls_jsonl_with_since(self, db: AuditDB, tmp_path: Path) -> None:
        for i in range(5):
            await db.log_tool_call(
                service="jira",
                command="search",
                args=f"query-{i}",
                operation_type="READ",
                duration_ms=10.0,
                success=True,
            )

        all_rows = await db.query("SELECT * FROM tool_calls ORDER BY timestamp")
        assert len(all_rows) == 5

        mid_ts = all_rows[2]["timestamp"]
        export_path = str(tmp_path / "since.jsonl")
        count = await db.export_tool_calls_jsonl(export_path, since=mid_ts)
        assert count == len([r for r in all_rows if r["timestamp"] >= mid_ts])

    async def test_fts_search_returns_matching_rows(self, db: AuditDB) -> None:
        for i, keyword in enumerate(["alpha bravo", "charlie delta", "bravo echo"]):
            await db.log_tool_call(
                service="jira",
                command="search",
                args=keyword,
                operation_type="READ",
                duration_ms=10.0,
                success=True,
                response_payload=f'{{"text": "{keyword}"}}',
            )

        rows = await db.query(
            "SELECT * FROM tool_calls_fts WHERE tool_calls_fts MATCH :term",
            {"term": "bravo"},
        )
        assert len(rows) == 2
        args_values = {r["args"] for r in rows}
        assert "alpha bravo" in args_values
        assert "bravo echo" in args_values

    async def test_fts_index_empty_without_payload_persistence(self, tmp_path: Path) -> None:
        db = AuditDB(str(tmp_path / "no_payload_fts.db"), persist_payloads=False)
        await db.open()
        try:
            await db.log_tool_call(
                service="jira",
                command="search",
                args="findme ordinary query",
                operation_type="READ",
                duration_ms=10.0,
                success=True,
                response_payload='{"secret": "data"}',
            )
            rows = await db.query(
                "SELECT * FROM tool_calls_fts WHERE tool_calls_fts MATCH :term",
                {"term": "secret"},
            )
            assert len(rows) == 0

            rows = await db.query(
                "SELECT * FROM tool_calls_fts WHERE tool_calls_fts MATCH :term",
                {"term": "findme"},
            )
            assert len(rows) == 1
        finally:
            await db.close()
