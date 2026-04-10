"""Initial audit DB schema.

Creates the ``messages`` and ``connections`` tables, all required
indexes, and the FTS5 virtual table for full-text payload search.

Revision ID: 001
Revises: None
Create Date: 2026-04-06
"""

from __future__ import annotations

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE messages (
            id              TEXT PRIMARY KEY,
            timestamp       REAL NOT NULL,
            op              TEXT NOT NULL,
            from_sandbox    TEXT NOT NULL,
            to_sandbox      TEXT,
            type            TEXT NOT NULL,
            reply_to        TEXT,
            channel         TEXT,
            payload         TEXT NOT NULL,
            payload_size    INTEGER NOT NULL,
            delivery_status TEXT NOT NULL
        )
    """)

    op.execute("CREATE INDEX idx_messages_timestamp ON messages(timestamp)")
    op.execute("CREATE INDEX idx_messages_from ON messages(from_sandbox)")
    op.execute("CREATE INDEX idx_messages_to ON messages(to_sandbox)")
    op.execute("CREATE INDEX idx_messages_type ON messages(type)")
    op.execute("CREATE INDEX idx_messages_reply_to ON messages(reply_to)")

    op.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            payload,
            content=messages,
            content_rowid=rowid
        )
    """)

    op.execute("""
        CREATE TABLE connections (
            sandbox_id        TEXT PRIMARY KEY,
            connected_at      REAL NOT NULL,
            disconnected_at   REAL,
            disconnect_reason TEXT
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connections")
    op.execute("DROP TABLE IF EXISTS messages_fts")
    op.execute("DROP TABLE IF EXISTS messages")
