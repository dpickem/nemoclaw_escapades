"""Add delegations table for orchestrator → sub-agent task tracking.

One row per ``delegate_task`` invocation.  Captures the typed-protocol
fields (workflow_id, parent_sandbox_id, agent_id, requested_model,
max_turns, baseline) plus the eventual outcome (success / error,
rounds_used, tool_calls_made, model_used, summary).

Revision ID: 005
Revises: 004
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op

revision: str = "005"
down_revision: str = "004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE delegations (
            workflow_id          TEXT PRIMARY KEY,
            started_at           REAL NOT NULL,
            completed_at         REAL,
            parent_sandbox_id    TEXT NOT NULL,
            agent_id             TEXT NOT NULL,
            workspace_root       TEXT NOT NULL,
            prompt               TEXT NOT NULL,
            requested_model      TEXT,
            requested_max_turns  INTEGER,
            base_sha             TEXT,
            base_repo_url        TEXT,
            base_branch          TEXT,
            status               TEXT NOT NULL,
            error_kind           TEXT,
            error_message        TEXT,
            recoverable          INTEGER,
            rounds_used          INTEGER,
            tool_calls_made      INTEGER,
            model_used           TEXT,
            summary              TEXT,
            diff_size            INTEGER
        )
    """)

    op.execute("CREATE INDEX idx_dlg_started ON delegations(started_at)")
    op.execute("CREATE INDEX idx_dlg_agent ON delegations(agent_id)")
    op.execute("CREATE INDEX idx_dlg_status ON delegations(status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS delegations")
