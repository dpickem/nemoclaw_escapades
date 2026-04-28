"""SQLAlchemy ORM models for the unified audit database.

These classes mirror the tables created by the Alembic migrations in
``alembic/versions/``.  The application never issues DDL directly —
schema changes go through Alembic.
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all audit ORM models."""


class MessageRow(Base):
    """A single audited message routed through the broker.

    Every ``send``, ``request``, ``reply``, ``publish``, and ``stream``
    that passes through the broker is persisted here (one row per
    message).  The ``delivery_status`` column is updated in-place when
    a request times out.

    Attributes:
        id: Message UUID (primary key, matches ``NMBMessage.id``).
        timestamp: Unix epoch seconds when the broker received the
            message.
        op: Wire operation code (e.g. ``"send"``, ``"request"``).
        from_sandbox: Authenticated sender sandbox ID.
        to_sandbox: Target sandbox ID (``None`` for pub/sub).
        type: Application-level message type (e.g. ``"task.assign"``).
        reply_to: Original request ID for reply correlation
            (``None`` unless ``op == "reply"``).
        channel: Pub/sub channel name (``None`` for point-to-point).
        payload: Full JSON payload string.  Empty string when
            ``persist_payloads`` is disabled.
        payload_size: Original payload size in bytes (always stored,
            even when the payload itself is elided).
        delivery_status: Outcome — ``"delivered"``, ``"error"``, or
            ``"timeout"``.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    op: Mapped[str] = mapped_column(String, nullable=False)
    from_sandbox: Mapped[str] = mapped_column(String, nullable=False)
    to_sandbox: Mapped[str | None] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String)
    channel: Mapped[str | None] = mapped_column(String)
    payload: Mapped[str] = mapped_column(String, nullable=False)
    payload_size: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False)


class ConnectionRow(Base):
    """Connection lifecycle record for a sandbox.

    A new row is inserted every time a sandbox connects, preserving
    full connection history.  ``log_disconnection`` updates only the
    most recent open row (``disconnected_at IS NULL``) for a given
    sandbox.

    Attributes:
        sandbox_id: Globally unique sandbox identifier (primary key,
            e.g. ``"coding-sandbox-1-a3f7b2c8"``).  Each launch
            generates a new ID, so the same row is never reused.
        connected_at: Unix epoch seconds when the connection was
            established.
        disconnected_at: Unix epoch seconds when the connection closed
            (``None`` while still connected).
        disconnect_reason: Human-readable reason for the disconnect
            (e.g. ``"crashed"``, ``"disconnected"``).  ``None`` while
            still connected.
    """

    __tablename__ = "connections"

    sandbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    connected_at: Mapped[float] = mapped_column(Float, nullable=False)
    disconnected_at: Mapped[float | None] = mapped_column(Float)
    disconnect_reason: Mapped[str | None] = mapped_column(String)


class DelegationRow(Base):
    """A single orchestrator → sub-agent delegation.

    Inserted at ``delegate_task`` start (status="started"); updated
    in-place when the sub-agent replies with ``task.complete`` or
    ``task.error``.  Phase 3a populates the request-side fields and
    the terminal outcome; Phase 3b's finalisation flow may extend
    with finalisation-action fields (push / discard / iterate).

    Attributes:
        workflow_id: Primary key — the same UUID that threads through
            every NMB message and audit record for the workflow.
        started_at: Unix epoch seconds when ``delegate_task`` was
            invoked.
        completed_at: Unix epoch seconds when the sub-agent replied
            (``None`` while in flight).
        parent_sandbox_id: NMB sandbox identity of the orchestrator
            that issued the delegation.
        agent_id: NMB sandbox identity of the spawned sub-agent.
        workspace_root: Path the sub-agent's per-task workspace
            subdirectory landed at.
        prompt: Natural-language task description.
        requested_model: Model the orchestrator pinned on
            ``TaskAssignPayload.model``.  ``None`` means "fall back
            to ``cfg.agent_loop.model``" — recorded even though M2b's
            L7 proxy may have rewritten the request en route, so M3
            can correlate against the realised upstream.
        requested_max_turns: Per-task ``max_turns`` cap.  ``None``
            means "use ``cfg.agent_loop.max_tool_rounds``".
        base_sha: ``WorkspaceBaseline.base_sha`` if the orchestrator
            pinned one (the diff anchor); ``None`` for non-diff
            tasks.
        base_repo_url: ``WorkspaceBaseline.repo_url``.
        base_branch: ``WorkspaceBaseline.branch``.
        status: ``"started"`` while in flight, ``"complete"`` on
            success, ``"error"`` on ``task.error``.
        error_kind: One of the ``TaskErrorPayload.error_kind`` literals
            (``"max_turns_exceeded"`` etc.); ``None`` on success.
        error_message: Human-readable error description; ``None`` on
            success.
        recoverable: Whether the finalisation model may
            ``re_delegate`` — preserved from
            ``TaskErrorPayload.recoverable``.
        rounds_used: Total inference calls the sub-agent made.
        tool_calls_made: Total tool invocations.
        model_used: Echoed from ``TaskCompletePayload.model_used``.
            With M3's Option D this becomes the *realised* model;
            in M2b it equals ``requested_model`` because the wire
            field is the same one we recorded at delegation time.
        summary: One-paragraph user-facing description from the
            sub-agent's reply.  May exceed prompt-display length;
            the row stores the full text.
        diff_size: Bytes of unified diff in the reply.  The diff
            itself is *not* stored here — finalisation owns the
            diff content (Phase 3b) and persists it elsewhere.
    """

    __tablename__ = "delegations"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[float | None] = mapped_column(Float)

    parent_sandbox_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    workspace_root: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(String, nullable=False)

    requested_model: Mapped[str | None] = mapped_column(String)
    requested_max_turns: Mapped[int | None] = mapped_column(Integer)

    base_sha: Mapped[str | None] = mapped_column(String)
    base_repo_url: Mapped[str | None] = mapped_column(String)
    base_branch: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)
    recoverable: Mapped[int | None] = mapped_column(Integer)

    rounds_used: Mapped[int | None] = mapped_column(Integer)
    tool_calls_made: Mapped[int | None] = mapped_column(Integer)
    model_used: Mapped[str | None] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(String)
    diff_size: Mapped[int | None] = mapped_column(Integer)


class ToolCallRow(Base):
    """A single audited nv-tools invocation.

    Every ``nv_tools_execute`` call — READ or WRITE, success or failure —
    is persisted here.

    Attributes:
        id: UUID primary key.
        timestamp: Unix epoch seconds.
        session_id: Conversation session / thread identifier.
        thread_ts: Slack thread timestamp.
        service: nv-tools service name (e.g. ``"jira"``).
        command: Subcommand name (e.g. ``"search"``).
        args: Full argument string.
        operation_type: ``"READ"`` or ``"WRITE"``.
        approval_status: ``"auto_approved"``, ``"approved"``,
            ``"denied"``, ``"timeout"``, or ``None``.
        approved_by: User who approved (Slack user ID).
        approval_time_ms: Time from request to approval decision.
        exit_code: Subprocess exit code.
        duration_ms: Wall-clock execution time.
        success: 1 for success, 0 for failure.
        error_code: Error code string if failed.
        error_message: Error message string if failed.
        response_payload: Full JSON response (empty if payloads disabled).
        payload_size: Original response size in bytes.
    """

    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String)
    thread_ts: Mapped[str | None] = mapped_column(String)

    service: Mapped[str] = mapped_column(String, nullable=False)
    command: Mapped[str] = mapped_column(String, nullable=False)
    args: Mapped[str] = mapped_column(String, nullable=False)
    operation_type: Mapped[str] = mapped_column(String, nullable=False)

    approval_status: Mapped[str | None] = mapped_column(String)
    approved_by: Mapped[str | None] = mapped_column(String)
    approval_time_ms: Mapped[float | None] = mapped_column(Float)

    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)

    response_payload: Mapped[str | None] = mapped_column(String)
    payload_size: Mapped[int] = mapped_column(Integer, nullable=False)
