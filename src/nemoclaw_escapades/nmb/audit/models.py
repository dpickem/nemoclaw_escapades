"""SQLAlchemy ORM models for the NMB audit database.

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
