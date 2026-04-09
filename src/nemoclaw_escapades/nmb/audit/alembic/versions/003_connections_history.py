"""Recreate connections table with globally unique sandbox_id PK.

The ``sandbox_id`` column remains the primary key but is now a
globally unique per-launch identifier (the client appends a random
suffix).  Each launch produces a distinct row, preserving full
connection history without an extra surrogate key.

Revision ID: 003
Revises: 002
Create Date: 2026-04-09
"""

from __future__ import annotations

from alembic import op

revision: str = "003"
down_revision: str = "002"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE connections RENAME TO _connections_old")

    op.execute("""
        CREATE TABLE connections (
            sandbox_id        TEXT PRIMARY KEY,
            connected_at      REAL NOT NULL,
            disconnected_at   REAL,
            disconnect_reason TEXT
        )
    """)

    op.execute("CREATE INDEX idx_connections_connected_at ON connections(connected_at)")

    op.execute("""
        INSERT INTO connections (sandbox_id, connected_at, disconnected_at, disconnect_reason)
        SELECT sandbox_id, connected_at, disconnected_at, disconnect_reason
        FROM _connections_old
    """)

    op.execute("DROP TABLE _connections_old")


def downgrade() -> None:
    op.execute("ALTER TABLE connections RENAME TO _connections_new")

    op.execute("""
        CREATE TABLE connections (
            sandbox_id        TEXT PRIMARY KEY,
            connected_at      REAL NOT NULL,
            disconnected_at   REAL,
            disconnect_reason TEXT
        )
    """)

    op.execute("""
        INSERT OR REPLACE INTO connections (sandbox_id, connected_at, disconnected_at, disconnect_reason)
        SELECT sandbox_id, MAX(connected_at), disconnected_at, disconnect_reason
        FROM _connections_new
        GROUP BY sandbox_id
    """)

    op.execute("DROP TABLE _connections_new")
