"""Add tool_calls table with FTS5 and sync triggers.

Creates the ``tool_calls`` table for orchestrator tool-invocation
auditing, the ``tool_calls_fts`` FTS5 virtual table for full-text
search over args and response payloads, and the insert/delete/update
triggers to keep the FTS index in sync.

Revision ID: 004
Revises: 003
Create Date: 2026-04-10
"""

from __future__ import annotations

from alembic import op

revision: str = "004"
down_revision: str = "003"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tool_calls (
            id               TEXT PRIMARY KEY,
            timestamp        REAL NOT NULL,
            session_id       TEXT,
            thread_ts        TEXT,
            service          TEXT NOT NULL,
            command          TEXT NOT NULL,
            args             TEXT NOT NULL,
            operation_type   TEXT NOT NULL,
            approval_status  TEXT,
            approved_by      TEXT,
            approval_time_ms REAL,
            exit_code        INTEGER,
            duration_ms      REAL NOT NULL,
            success          INTEGER NOT NULL,
            error_code       TEXT,
            error_message    TEXT,
            response_payload TEXT,
            payload_size     INTEGER NOT NULL
        )
    """)

    op.execute("CREATE INDEX idx_tc_timestamp ON tool_calls(timestamp)")
    op.execute("CREATE INDEX idx_tc_session ON tool_calls(session_id)")
    op.execute("CREATE INDEX idx_tc_service ON tool_calls(service)")
    op.execute("CREATE INDEX idx_tc_operation ON tool_calls(operation_type)")

    op.execute("""
        CREATE VIRTUAL TABLE tool_calls_fts USING fts5(
            args, response_payload,
            content=tool_calls, content_rowid=rowid
        )
    """)

    op.execute("""
        CREATE TRIGGER tool_calls_fts_ai AFTER INSERT ON tool_calls BEGIN
            INSERT INTO tool_calls_fts(rowid, args, response_payload)
            VALUES (new.rowid, new.args, new.response_payload);
        END
    """)

    op.execute("""
        CREATE TRIGGER tool_calls_fts_ad AFTER DELETE ON tool_calls BEGIN
            INSERT INTO tool_calls_fts(tool_calls_fts, rowid, args, response_payload)
            VALUES ('delete', old.rowid, old.args, old.response_payload);
        END
    """)

    op.execute("""
        CREATE TRIGGER tool_calls_fts_au AFTER UPDATE ON tool_calls BEGIN
            INSERT INTO tool_calls_fts(tool_calls_fts, rowid, args, response_payload)
            VALUES ('delete', old.rowid, old.args, old.response_payload);
            INSERT INTO tool_calls_fts(rowid, args, response_payload)
            VALUES (new.rowid, new.args, new.response_payload);
        END
    """)

    op.execute("INSERT INTO tool_calls_fts(tool_calls_fts) VALUES ('rebuild')")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tool_calls_fts_au")
    op.execute("DROP TRIGGER IF EXISTS tool_calls_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS tool_calls_fts_ai")
    op.execute("DROP TABLE IF EXISTS tool_calls_fts")
    op.execute("DROP TABLE IF EXISTS tool_calls")
