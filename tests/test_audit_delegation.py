"""Tests for ``AuditDB`` delegation logging.

Covers the three write paths added in Phase 3a-6:

- ``log_delegation_started`` inserts a status="started" row.
- ``log_delegation_complete`` updates with success outcome.
- ``log_delegation_error`` updates with failure outcome.

The migration itself (``005_delegations.py``) is exercised by the
``test_migration_creates_table`` test below — opening a fresh DB
runs ``alembic upgrade head`` and any missing column or table
fails the read-back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nemoclaw_escapades.audit.db import AuditDB


@pytest.fixture
async def audit_db(tmp_path: Path) -> AuditDB:
    db = AuditDB(str(tmp_path / "test_audit.db"))
    await db.open()
    yield db  # type: ignore[misc]
    await db.close()


class TestDelegationsTableMigration:
    async def test_migration_creates_table(self, audit_db: AuditDB) -> None:
        rows = await audit_db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='delegations'"
        )
        assert len(rows) == 1

    async def test_migration_creates_indexes(self, audit_db: AuditDB) -> None:
        rows = await audit_db.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delegations'"
        )
        names = {r["name"] for r in rows}
        # Three explicit indexes from the migration; SQLite also adds
        # an automatic index for the primary key.  Don't assert on the
        # exact count to avoid coupling to that.
        assert "idx_dlg_started" in names
        assert "idx_dlg_agent" in names
        assert "idx_dlg_status" in names


class TestLogDelegationStarted:
    async def test_minimal_row_inserts(self, audit_db: AuditDB) -> None:
        await audit_db.log_delegation_started(
            workflow_id="wf-1",
            parent_sandbox_id="orch",
            agent_id="coding-12345678",
            workspace_root="/sandbox/ws/agent-12345678",
            prompt="add a /api/health endpoint",
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-1"},
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "started"
        assert row["completed_at"] is None
        assert row["parent_sandbox_id"] == "orch"
        assert row["agent_id"] == "coding-12345678"
        assert row["prompt"] == "add a /api/health endpoint"
        # Optional fields are NULL when unset.
        assert row["requested_model"] is None
        assert row["base_sha"] is None

    async def test_full_row_inserts(self, audit_db: AuditDB) -> None:
        await audit_db.log_delegation_started(
            workflow_id="wf-2",
            parent_sandbox_id="orch",
            agent_id="coding-deadbeef",
            workspace_root="/sandbox/ws",
            prompt="task",
            requested_model="azure/anthropic/claude-haiku-4",
            requested_max_turns=42,
            base_sha="cafebabe" * 5,
            base_repo_url="https://example.com/x.git",
            base_branch="main",
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-2"},
        )
        row = rows[0]
        assert row["requested_model"] == "azure/anthropic/claude-haiku-4"
        assert row["requested_max_turns"] == 42
        assert row["base_sha"] == "cafebabe" * 5
        assert row["base_repo_url"] == "https://example.com/x.git"
        assert row["base_branch"] == "main"


class TestLogDelegationComplete:
    async def test_marks_row_complete(self, audit_db: AuditDB) -> None:
        await audit_db.log_delegation_started(
            workflow_id="wf-3",
            parent_sandbox_id="orch",
            agent_id="coding-1",
            workspace_root="/ws",
            prompt="task",
        )
        await audit_db.log_delegation_complete(
            workflow_id="wf-3",
            rounds_used=4,
            tool_calls_made=12,
            model_used="azure/anthropic/claude-opus-4-6",
            summary="Wrote it.",
            diff_size=2048,
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-3"},
        )
        row = rows[0]
        assert row["status"] == "complete"
        assert row["completed_at"] is not None
        assert row["rounds_used"] == 4
        assert row["tool_calls_made"] == 12
        assert row["model_used"] == "azure/anthropic/claude-opus-4-6"
        assert row["summary"] == "Wrote it."
        assert row["diff_size"] == 2048
        # Error fields stay NULL.
        assert row["error_kind"] is None
        assert row["error_message"] is None

    async def test_complete_without_started_row_is_no_op(
        self,
        audit_db: AuditDB,
    ) -> None:
        # Idempotency: a duplicate complete (NMB replay) shouldn't
        # crash if no in-flight row exists.  The ``UPDATE … WHERE
        # workflow_id`` simply matches zero rows.
        await audit_db.log_delegation_complete(
            workflow_id="wf-missing",
            rounds_used=1,
            tool_calls_made=0,
            model_used=None,
            summary="x",
            diff_size=0,
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-missing"},
        )
        assert len(rows) == 0


class TestLogDelegationError:
    async def test_marks_row_error(self, audit_db: AuditDB) -> None:
        await audit_db.log_delegation_started(
            workflow_id="wf-4",
            parent_sandbox_id="orch",
            agent_id="coding-2",
            workspace_root="/ws",
            prompt="task",
        )
        await audit_db.log_delegation_error(
            workflow_id="wf-4",
            error_kind="max_turns_exceeded",
            error_message="ran out of rounds",
            recoverable=True,
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-4"},
        )
        row = rows[0]
        assert row["status"] == "error"
        assert row["completed_at"] is not None
        assert row["error_kind"] == "max_turns_exceeded"
        assert row["error_message"] == "ran out of rounds"
        # SQLite has no native bool → stored as 0/1 int.
        assert row["recoverable"] == 1

    async def test_recoverable_false_stored_as_zero(
        self,
        audit_db: AuditDB,
    ) -> None:
        await audit_db.log_delegation_started(
            workflow_id="wf-5",
            parent_sandbox_id="orch",
            agent_id="coding-3",
            workspace_root="/ws",
            prompt="task",
        )
        await audit_db.log_delegation_error(
            workflow_id="wf-5",
            error_kind="other",
            error_message="boom",
            recoverable=False,
        )
        rows = await audit_db.query(
            "SELECT * FROM delegations WHERE workflow_id = :wf",
            {"wf": "wf-5"},
        )
        assert rows[0]["recoverable"] == 0
