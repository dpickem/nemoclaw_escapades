"""Add triggers to keep messages_fts in sync with messages.

FTS5 external-content tables (``content=messages``) require explicit
triggers to populate the inverted index.  Without these, the FTS table
stays permanently empty regardless of how many rows are inserted into
the content table.

After creating the triggers, we run ``rebuild`` to back-fill the index
with any rows already present from migration 001.

Revision ID: 002
Revises: 001
Create Date: 2026-04-09
"""

from __future__ import annotations

from alembic import op

revision: str = "002"
down_revision: str = "001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, payload)
            VALUES (new.rowid, new.payload);
        END
    """)

    op.execute("""
        CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, payload)
            VALUES ('delete', old.rowid, old.payload);
        END
    """)

    op.execute("""
        CREATE TRIGGER messages_fts_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, payload)
            VALUES ('delete', old.rowid, old.payload);
            INSERT INTO messages_fts(rowid, payload)
            VALUES (new.rowid, new.payload);
        END
    """)

    op.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS messages_fts_au")
    op.execute("DROP TRIGGER IF EXISTS messages_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS messages_fts_ai")
