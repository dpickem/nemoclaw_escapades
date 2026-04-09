"""Async SQLite audit database for the NMB broker.

Every message routed through the broker is logged here with full payload
content (configurable).  The audit DB serves double duty: operational
debugging and training-data source for the training flywheel.

Schema is managed by Alembic — the application never issues DDL directly.
On first open, ``AuditDB`` runs ``alembic upgrade head`` to ensure the
schema is current.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nemoclaw_escapades.nmb.audit.models import ConnectionRow, MessageRow
from nemoclaw_escapades.nmb.models import DeliveryStatus, NMBMessage

_ALEMBIC_DIR = Path(__file__).parent / "alembic"
_ALEMBIC_INI = Path(__file__).parent / "alembic.ini"


class AuditDB:
    """Async wrapper around the NMB audit SQLite database.

    Attributes:
        db_path: Filesystem path to the SQLite database file.
        persist_payloads: Whether to store full message payloads.
            Set to ``False`` to save disk space at the cost of losing
            training data.
    """

    def __init__(self, db_path: str, *, persist_payloads: bool = True) -> None:
        """Initialise the audit DB handle (does not open the connection).

        Args:
            db_path: Path to the SQLite file.  Created automatically
                by Alembic if it does not exist.
            persist_payloads: Store full JSON payloads in the
                ``messages`` table.  When ``False``, payloads are
                replaced with an empty string.
        """
        self.db_path: str = db_path
        self.persist_payloads: bool = persist_payloads
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def open(self) -> None:
        """Run Alembic migrations and open an async SQLAlchemy engine in WAL mode."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._run_migrations()

        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")

        @event.listens_for(self._engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA auto_vacuum=INCREMENTAL")
            cursor.close()

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    async def close(self) -> None:
        """Dispose of the engine and release all connections."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    def _run_migrations(self) -> None:
        """Run ``alembic upgrade head`` synchronously.

        Called once during ``open()`` to ensure the schema is current.
        Uses a subprocess so the synchronous Alembic engine does not
        block the event loop.
        """
        subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(_ALEMBIC_INI),
                "upgrade",
                "head",
            ],
            check=True,
            capture_output=True,
            env={
                **__import__("os").environ,
                "NMB_AUDIT_DB_PATH": self.db_path,
            },
        )

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("AuditDB is not open — call open() first")
        return self._session_factory

    # ------------------------------------------------------------------
    # Message logging
    # ------------------------------------------------------------------

    async def log_message(
        self,
        msg: NMBMessage,
        delivery_status: DeliveryStatus,
    ) -> None:
        """Log a routed message to the audit table.

        Args:
            msg: The message that was routed.
            delivery_status: Outcome of the delivery attempt.
        """
        payload_json = json.dumps(msg.payload) if msg.payload is not None else ""
        payload_size = len(payload_json.encode()) if payload_json else 0
        stored_payload = payload_json if self.persist_payloads else ""

        async with self._session() as session:
            session.add(
                MessageRow(
                    id=msg.id,
                    timestamp=msg.timestamp or time.time(),
                    op=msg.op.value,
                    from_sandbox=msg.from_sandbox,
                    to_sandbox=msg.to,
                    type=msg.type,
                    reply_to=msg.reply_to,
                    channel=msg.channel,
                    payload=stored_payload,
                    payload_size=payload_size,
                    delivery_status=delivery_status.value,
                )
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Connection logging
    # ------------------------------------------------------------------

    async def log_connection(self, sandbox_id: str) -> None:
        """Record a sandbox connecting to the broker.

        Args:
            sandbox_id: The connecting sandbox's identity.
        """
        async with self._session() as session:
            stmt = (
                insert(ConnectionRow)
                .prefix_with("OR REPLACE")
                .values(sandbox_id=sandbox_id, connected_at=time.time())
            )
            await session.execute(stmt)
            await session.commit()

    async def log_disconnection(self, sandbox_id: str, reason: str = "") -> None:
        """Record a sandbox disconnecting from the broker.

        Args:
            sandbox_id: The disconnecting sandbox's identity.
            reason: Optional human-readable disconnect reason.
        """
        async with self._session() as session:
            stmt = (
                update(ConnectionRow)
                .where(ConnectionRow.sandbox_id == sandbox_id)
                .values(disconnected_at=time.time(), disconnect_reason=reason)
            )
            await session.execute(stmt)
            await session.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run an arbitrary SELECT and return rows as dicts.

        Args:
            sql: SQL query string (should be SELECT only).  Use
                ``:name`` style bind parameters.
            params: Named bind parameters.

        Returns:
            A list of row dicts keyed by column name.
        """
        async with self._session() as session:
            result = await session.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings()]

    async def export_jsonl(self, path: str, since: float | None = None) -> int:
        """Export messages to a JSONL file.

        Args:
            path: Output file path.
            since: Optional Unix timestamp; only export messages after
                this time.

        Returns:
            Number of messages exported.
        """
        stmt = select(MessageRow).order_by(MessageRow.timestamp)
        if since is not None:
            stmt = stmt.where(MessageRow.timestamp >= since)

        async with self._session() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()

        with open(path, "w") as f:
            for row in rows:
                row_dict = {
                    c.key: getattr(row, c.key) for c in MessageRow.__table__.columns
                }
                f.write(json.dumps(row_dict, default=str) + "\n")
        return len(rows)
