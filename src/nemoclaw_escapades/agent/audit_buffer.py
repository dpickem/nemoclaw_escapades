"""Sub-agent audit buffer flushed to the orchestrator over NMB.

Sub-agents do not write directly to the orchestrator's audit DB
(the DB lives in the orchestrator's sandbox; the sub-agent has no
shared write lock).  Instead, every tool call accumulates in this
in-memory buffer and is flushed to the orchestrator either:

- as an ``audit.flush`` NMB message at task end (the happy path), or
- through a JSONL fallback file that the orchestrator's
  :class:`FinalizationCoordinator` reads from disk if the NMB send
  failed (``docs/design_m2b.md`` §13).

The class deliberately mirrors :meth:`AuditDB.log_tool_call` so the
sub-agent's :class:`AgentLoop` can swap one for the other through the
:class:`AuditSink` Protocol without conditional logic.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from nemoclaw_escapades.nmb.protocol import AuditFlushPayload, AuditToolCallPayload


class AuditBuffer:
    """In-memory audit sink with the :meth:`AuditDB.log_tool_call` interface."""

    def __init__(
        self,
        *,
        workflow_id: str,
        parent_sandbox_id: str,
        agent_id: str,
        agent_role: str = "coding",
    ) -> None:
        self.workflow_id = workflow_id
        self.parent_sandbox_id = parent_sandbox_id
        self.agent_id = agent_id
        self.agent_role = agent_role
        self._tool_calls: list[AuditToolCallPayload] = []

    @property
    def is_empty(self) -> bool:
        """Whether any tool calls have been buffered."""
        return not self._tool_calls

    @property
    def tool_calls(self) -> list[AuditToolCallPayload]:
        """Snapshot of buffered tool-call rows.

        Returns a *copy* so callers iterating during further
        ``log_tool_call`` calls don't see surprises.
        """
        return list(self._tool_calls)

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
        """Buffer one tool-call audit row and return its stable id.

        Parameter names match :class:`AuditSink` exactly so the
        :class:`AgentLoop` can call us interchangeably with a real
        :class:`AuditDB`.  ``session_id`` / ``thread_ts`` and the
        per-row ``workflow_id`` / ``parent_sandbox_id`` / ``agent_id``
        / ``agent_role`` arguments are deliberately unused — the
        buffer carries the workflow-level attribution on the
        :class:`AuditFlushPayload` envelope, and the orchestrator's
        :meth:`AuditDB.ingest_audit_flush` re-applies it to every
        row at write time.
        """
        del session_id, thread_ts  # carried separately on the flush envelope
        del workflow_id, parent_sandbox_id, agent_id, agent_role
        row_id = row_id or uuid.uuid4().hex[:16]
        self._tool_calls.append(
            AuditToolCallPayload(
                id=row_id,
                service=service,
                command=command,
                args=args,
                operation_type=operation_type,  # type: ignore[arg-type]
                approval_status=approval_status,
                approved_by=approved_by,
                approval_time_ms=approval_time_ms,
                exit_code=exit_code,
                duration_ms=duration_ms,
                success=success,
                error_code=error_code,
                error_message=error_message,
                response_payload=response_payload,
            )
        )
        return row_id

    def to_payload(self) -> AuditFlushPayload:
        """Build the typed ``audit.flush`` payload."""
        return AuditFlushPayload(
            workflow_id=self.workflow_id,
            parent_sandbox_id=self.parent_sandbox_id,
            agent_id=self.agent_id,
            agent_role=self.agent_role,
            tool_calls=self.tool_calls,
        )

    def write_jsonl_fallback(self, path: str | Path) -> Path | None:
        """Write buffered rows as JSONL for orchestrator-side fallback ingest.

        No-op (returns ``None``) when the buffer is empty — there's
        no point creating a zero-row file just for the orchestrator
        to read it back.

        Format: one row per line.  Each row is a JSON object with
        the workflow-level envelope fields (``workflow_id``,
        ``parent_sandbox_id``, ``agent_id``, ``agent_role``) plus
        a nested ``tool_call`` object whose shape matches
        :class:`AuditToolCallPayload`.  The duplication of
        envelope fields per row is deliberate: it keeps each line
        independently parseable, so an orchestrator that crashes
        midway through ingest can resume from any line without
        replaying the whole file.

        Args:
            path: Target JSONL path.  Parent directories are
                created if missing.

        Returns:
            The written path, or ``None`` when the buffer was empty.
        """
        if self.is_empty:
            return None
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "workflow_id": self.workflow_id,
            "parent_sandbox_id": self.parent_sandbox_id,
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
        }
        with target.open("w", encoding="utf-8") as fh:
            for item in self._tool_calls:
                row = {
                    **envelope,
                    "tool_call": item.model_dump(mode="json"),
                }
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return target
