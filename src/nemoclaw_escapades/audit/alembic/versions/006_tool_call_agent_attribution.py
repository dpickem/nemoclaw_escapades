"""Add workflow and agent attribution to tool calls.

Revision ID: 006
Revises: 005
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op

revision: str = "006"
down_revision: str = "005"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE tool_calls ADD COLUMN workflow_id TEXT")
    op.execute("ALTER TABLE tool_calls ADD COLUMN parent_sandbox_id TEXT")
    op.execute("ALTER TABLE tool_calls ADD COLUMN agent_id TEXT")
    op.execute("ALTER TABLE tool_calls ADD COLUMN agent_role TEXT")
    op.execute("CREATE INDEX idx_tc_workflow ON tool_calls(workflow_id)")
    op.execute("CREATE INDEX idx_tc_agent ON tool_calls(agent_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tc_agent")
    op.execute("DROP INDEX IF EXISTS idx_tc_workflow")
    # SQLite cannot drop columns portably; leave nullable attribution
    # columns in place on downgrade.
