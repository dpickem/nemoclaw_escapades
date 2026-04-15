"""Tests for the audit database — NMB message logging, connections, and queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.audit.db import AuditDB
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
        assert "tool_calls" in names

    async def test_log_message(self, audit_db: AuditDB) -> None:
        msg = NMBMessage(
            op=Op.SEND,
            id="test-msg-1",
            from_sandbox="orch",
            to_sandbox="coding-1",
            type="task.assign",
            timestamp=1000.0,
            payload={"prompt": "hello"},
        )
        await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        rows = await audit_db.query("SELECT * FROM messages WHERE id = :id", {"id": "test-msg-1"})
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
                to_sandbox="b",
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
        q = "SELECT * FROM connections WHERE sandbox_id = :sid"
        rows = await audit_db.query(q, {"sid": "sandbox-a"})
        assert len(rows) == 1
        assert rows[0]["disconnected_at"] is None

        await audit_db.log_disconnection("sandbox-a", "crashed")
        rows = await audit_db.query(q, {"sid": "sandbox-a"})
        assert rows[0]["disconnect_reason"] == "crashed"
        assert rows[0]["disconnected_at"] is not None

    async def test_export_messages_jsonl(self, audit_db: AuditDB, tmp_path: Path) -> None:
        for i in range(3):
            msg = NMBMessage(
                op=Op.SEND,
                id=f"export-{i}",
                from_sandbox="a",
                to_sandbox="b",
                type="t",
                timestamp=float(i),
                payload={},
            )
            await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        out_path = str(tmp_path / "export.jsonl")
        count = await audit_db.export_messages_jsonl(out_path)
        assert count == 3

        with open(out_path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    async def test_fts_search_returns_matching_payloads(self, audit_db: AuditDB) -> None:
        for i, keyword in enumerate(["alpha bravo", "charlie delta", "bravo echo"]):
            msg = NMBMessage(
                op=Op.SEND,
                id=f"fts-{i}",
                from_sandbox="a",
                to_sandbox="b",
                type="t",
                timestamp=float(i),
                payload={"text": keyword},
            )
            await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        rows = await audit_db.query(
            "SELECT * FROM messages_fts WHERE messages_fts MATCH :term",
            {"term": "bravo"},
        )
        payloads = {r["payload"] for r in rows}
        assert len(rows) == 2
        assert any("alpha" in p for p in payloads)
        assert any("echo" in p for p in payloads)

    async def test_fts_index_empty_without_payload_persistence(self, tmp_path: Path) -> None:
        db = AuditDB(str(tmp_path / "no_payload_fts.db"), persist_payloads=False)
        await db.open()
        try:
            msg = NMBMessage(
                op=Op.SEND,
                id="fts-np-1",
                from_sandbox="a",
                to_sandbox="b",
                type="t",
                timestamp=1.0,
                payload={"secret": "data"},
            )
            await db.log_message(msg, DeliveryStatus.DELIVERED)
            rows = await db.query(
                "SELECT * FROM messages_fts WHERE messages_fts MATCH :term",
                {"term": "secret"},
            )
            assert len(rows) == 0
        finally:
            await db.close()

    async def test_update_delivery_status(self, audit_db: AuditDB) -> None:
        """update_delivery_status changes the row without creating a duplicate."""
        msg = NMBMessage(
            op=Op.REQUEST,
            id="upd-status-1",
            from_sandbox="a",
            to_sandbox="b",
            type="slow.request",
            timestamp=1.0,
            payload={},
        )
        await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        await audit_db.update_delivery_status("upd-status-1", DeliveryStatus.TIMEOUT)

        rows = await audit_db.query(
            "SELECT delivery_status FROM messages WHERE id = :id",
            {"id": "upd-status-1"},
        )
        assert len(rows) == 1
        assert rows[0]["delivery_status"] == "timeout"

    async def test_background_writer_batch_commit(self, audit_db: AuditDB) -> None:
        """Background writer should batch-commit enqueued messages."""
        await audit_db.start_background_writer()
        try:
            for i in range(5):
                msg = NMBMessage(
                    op=Op.SEND,
                    id=f"bg-{i}",
                    from_sandbox="a",
                    to_sandbox="b",
                    type="t",
                    timestamp=float(i),
                    payload={"i": i},
                )
                audit_db.enqueue_message(msg, DeliveryStatus.DELIVERED)

            import asyncio

            await asyncio.sleep(0.3)

            rows = await audit_db.query("SELECT COUNT(*) AS cnt FROM messages")
            assert rows[0]["cnt"] == 5
        finally:
            await audit_db.stop_background_writer()

    async def test_background_writer_status_update(self, audit_db: AuditDB) -> None:
        """Background writer should handle interleaved inserts and updates."""
        msg = NMBMessage(
            op=Op.REQUEST,
            id="bg-upd-1",
            from_sandbox="a",
            to_sandbox="b",
            type="t",
            timestamp=1.0,
            payload={},
        )
        await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        await audit_db.start_background_writer()
        try:
            audit_db.enqueue_status_update("bg-upd-1", DeliveryStatus.TIMEOUT)

            import asyncio

            await asyncio.sleep(0.3)

            rows = await audit_db.query(
                "SELECT delivery_status FROM messages WHERE id = :id",
                {"id": "bg-upd-1"},
            )
            assert rows[0]["delivery_status"] == "timeout"
        finally:
            await audit_db.stop_background_writer()

    async def test_export_messages_jsonl_with_since(
        self, audit_db: AuditDB, tmp_path: Path
    ) -> None:
        for i in range(5):
            msg = NMBMessage(
                op=Op.SEND,
                id=f"since-{i}",
                from_sandbox="a",
                to_sandbox="b",
                type="t",
                timestamp=float(i * 100),
                payload={},
            )
            await audit_db.log_message(msg, DeliveryStatus.DELIVERED)

        all_rows = await audit_db.query("SELECT * FROM messages ORDER BY timestamp")
        out_path = str(tmp_path / "since.jsonl")
        count = await audit_db.export_messages_jsonl(out_path, since=250.0)
        assert count == len([r for r in all_rows if r["timestamp"] >= 250.0])


class TestWALCheckpoint:
    """Verify WAL checkpoint behaviour so downloads always get full data."""

    async def test_checkpoint_folds_wal_into_main_db(self, tmp_path: Path) -> None:
        """After checkpoint(), the main .db file is self-contained."""
        db_path = str(tmp_path / "ckpt.db")
        db = AuditDB(db_path)
        await db.open()
        try:
            await db.log_connection("sandbox-ckpt-1")
            await db.checkpoint()

            import sqlite3

            wal_path = Path(db_path + "-wal")
            wal_size = wal_path.stat().st_size if wal_path.exists() else 0
            assert wal_size == 0, f"WAL should be truncated after checkpoint, got {wal_size}"

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
            conn.close()
            assert row[0] == 1
        finally:
            await db.close()

    async def test_periodic_checkpoint_triggers_at_interval(self, tmp_path: Path) -> None:
        """Auto-checkpoint fires after checkpoint_interval commits."""
        db_path = str(tmp_path / "periodic.db")
        db = AuditDB(db_path, checkpoint_interval=3)
        await db.open()
        try:
            for i in range(2):
                await db.log_connection(f"sandbox-{i}")
            assert db._commits_since_checkpoint == 2

            await db.log_connection("sandbox-2")
            assert db._commits_since_checkpoint == 0, "Should reset after checkpoint"
        finally:
            await db.close()

    async def test_checkpoint_disabled_when_interval_zero(self, tmp_path: Path) -> None:
        """checkpoint_interval=0 disables periodic checkpoints."""
        db_path = str(tmp_path / "no_periodic.db")
        db = AuditDB(db_path, checkpoint_interval=0)
        await db.open()
        try:
            for i in range(20):
                await db.log_connection(f"sandbox-{i}")
            assert db._commits_since_checkpoint == 0, "Counter should stay at 0 when disabled"
        finally:
            await db.close()

    async def test_close_checkpoints_wal(self, tmp_path: Path) -> None:
        """close() should checkpoint, leaving no WAL data behind."""
        db_path = str(tmp_path / "close_ckpt.db")
        db = AuditDB(db_path, checkpoint_interval=0)
        await db.open()
        await db.log_connection("sandbox-close")
        await db.close()

        wal_path = Path(db_path + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        assert wal_size == 0

        import sqlite3

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT COUNT(*) FROM connections").fetchone()
        conn.close()
        assert row[0] == 1

    async def test_tool_call_triggers_checkpoint(self, tmp_path: Path) -> None:
        """log_tool_call increments the checkpoint counter."""
        db_path = str(tmp_path / "tc_ckpt.db")
        db = AuditDB(db_path, checkpoint_interval=2)
        await db.open()
        try:
            await db.log_tool_call(
                service="test",
                command="test_cmd",
                args="{}",
                operation_type="READ",
                duration_ms=1.0,
                success=True,
            )
            assert db._commits_since_checkpoint == 1

            await db.log_tool_call(
                service="test",
                command="test_cmd_2",
                args="{}",
                operation_type="READ",
                duration_ms=2.0,
                success=True,
            )
            assert db._commits_since_checkpoint == 0
        finally:
            await db.close()
