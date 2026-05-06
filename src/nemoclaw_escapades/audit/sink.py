"""Structural type for tool-call audit consumers.

Two implementations satisfy this Protocol:

- :class:`nemoclaw_escapades.audit.db.AuditDB` — writes rows directly
  to the orchestrator's SQLite audit DB.  Used by the orchestrator's
  own ``AgentLoop``.
- :class:`nemoclaw_escapades.agent.audit_buffer.AuditBuffer` — buffers
  rows in memory for batched ``audit.flush`` shipment over NMB.
  Used by the sub-agent's ``AgentLoop`` (the sub-agent has no direct
  filesystem path to the orchestrator's DB; see design §13).

``AgentLoop.audit`` accepts either via this Protocol so the loop
itself stays agnostic about which side it's running on.
"""

from __future__ import annotations

from typing import Protocol


class AuditSink(Protocol):
    """Anything that can record a single tool-call audit row."""

    async def log_tool_call(
        self,
        *,
        row_id: str | None = None,
        session_id: str | None = None,
        thread_ts: str | None = None,
        service: str,
        command: str,
        args: str,
        operation_type: str,
        approval_status: str | None = None,
        approved_by: str | None = None,
        approval_time_ms: float | None = None,
        exit_code: int | None = None,
        duration_ms: float,
        success: bool,
        error_code: str | None = None,
        error_message: str | None = None,
        response_payload: str = "",
        workflow_id: str | None = None,
        parent_sandbox_id: str | None = None,
        agent_id: str | None = None,
        agent_role: str | None = None,
    ) -> str:
        """Record one tool invocation.  Returns the row id."""
        ...
