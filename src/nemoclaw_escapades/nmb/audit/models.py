"""SQLAlchemy ORM models for the NMB audit database."""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MessageRow(Base):
    """ORM model mirroring the ``messages`` audit table."""

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
    """ORM model mirroring the ``connections`` audit table."""

    __tablename__ = "connections"

    sandbox_id: Mapped[str] = mapped_column(String, primary_key=True)
    connected_at: Mapped[float] = mapped_column(Float, nullable=False)
    disconnected_at: Mapped[float | None] = mapped_column(Float)
    disconnect_reason: Mapped[str | None] = mapped_column(String)
