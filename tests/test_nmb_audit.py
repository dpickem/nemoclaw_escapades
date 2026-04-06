"""Tests for the NMB audit database — Alembic migrations, logging, and queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.nmb.audit.db import AuditDB
from nemoclaw_escapades.nmb.models import DeliveryStatus, NMBMessage, Op


@pytest.fixture
async def audit_db(tmp_path: Path) -> AuditDB:
    """Provide an AuditDB pointed at a temp directory."""
    db_path = str(tmp_path / "test_audit.db")
    db = AuditDB(db_path)
    await db.open()
    yield db  # type: ignore[misc]
    await db.close()


class TestAuditDB:
    async def test_migration_creates_tables(self, audit_db: AuditDB) -> None:
        rows = await audit_db.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r["name"] for r in rows}
        assert "messages" in names
        assert "connections" in names

    async def test_log_message(self, audit_db: AuditDB) -> None:
        msg = NMBMessage(
            op=Op.SEND,
            id="test-msg-1",
            from_sandbox="orch",
            to="coding-1",
            type="task.assign",
            timestamp=1000.0,
            payload={"prompt": "hello"},
        )
        await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        rows = await audit_db.query("SELECT * FROM messages WHERE id = ?", ("test-msg-1",))
        assert len(rows) == 1
        assert rows[0]["from_sandbox"] == "orch"
        assert rows[0]["to_sandbox"] == "coding-1"
        assert rows[0]["delivery_status"] == "delivered"
        assert json.loads(rows[0]["payload"]) == {"prompt": "hello"}

    async def test_log_message_without_payload_persistence(self, tmp_path: Path) -> None:
        db = AuditDB(str(tmp_path / "no_payload.db"), persist_payloads=False)
        await db.open()
        try:
            msg = NMBMessage(
                op=Op.SEND,
                id="np-1",
                from_sandbox="a",
                to="b",
                type="t",
                timestamp=1.0,
                payload={"secret": "data"},
            )
            await db.log_message(msg, DeliveryStatus.DELIVERED)
            rows = await db.query("SELECT payload, payload_size FROM messages")
            assert rows[0]["payload"] == ""
            assert rows[0]["payload_size"] > 0
        finally:
            await db.close()

    async def test_log_connection_and_disconnection(self, audit_db: AuditDB) -> None:
        await audit_db.log_connection("sandbox-a")
        q = "SELECT * FROM connections WHERE sandbox_id = ?"
        rows = await audit_db.query(q, ("sandbox-a",))
        assert len(rows) == 1
        assert rows[0]["disconnected_at"] is None

        await audit_db.log_disconnection("sandbox-a", "crashed")
        rows = await audit_db.query(q, ("sandbox-a",))
        assert rows[0]["disconnect_reason"] == "crashed"
        assert rows[0]["disconnected_at"] is not None

    async def test_export_jsonl(self, audit_db: AuditDB, tmp_path: Path) -> None:
        for i in range(3):
            msg = NMBMessage(
                op=Op.SEND,
                id=f"export-{i}",
                from_sandbox="a",
                to="b",
                type="t",
                timestamp=float(i),
                payload={},
            )
            await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        out_path = str(tmp_path / "export.jsonl")
        count = await audit_db.export_jsonl(out_path)
        assert count == 3

        with open(out_path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    async def test_export_jsonl_with_since(self, audit_db: AuditDB, tmp_path: Path) -> None:
        for i in range(5):
            msg = NMBMessage(
                op=Op.SEND,
                id=f"since-{i}",
                from_sandbox="a",
                to="b",
                type="t",
                timestamp=float(i * 100),
                payload={},
            )
            await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        # timestamps: 0, 100, 200, 300, 400
        # Verify total, then filter
        all_rows = await audit_db.query("SELECT * FROM messages ORDER BY timestamp")
        out_path = str(tmp_path / "since.jsonl")
        # since=250 should return rows with timestamp >= 250 → 300, 400
        count = await audit_db.export_jsonl(out_path, since=250.0)
        assert count == len([r for r in all_rows if r["timestamp"] >= 250.0])
