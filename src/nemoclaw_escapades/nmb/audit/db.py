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

import aiosqlite

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
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Run Alembic migrations and open an async connection in WAL mode."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._run_migrations()
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

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
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("AuditDB is not open — call open() first")
        return self._conn

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
        payload_json = json.dumps(msg.payload) if msg.payload else ""
        if not self.persist_payloads:
            stored_payload = ""
            payload_size = len(payload_json.encode()) if payload_json else 0
        else:
            stored_payload = payload_json
            payload_size = len(payload_json.encode()) if payload_json else 0

        await self._db.execute(
            """
            INSERT INTO messages
                (id, timestamp, op, from_sandbox, to_sandbox, type,
                 reply_to, channel, payload, payload_size, delivery_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                msg.timestamp or time.time(),
                msg.op.value,
                msg.from_sandbox,
                msg.to,
                msg.type,
                msg.reply_to,
                msg.channel,
                stored_payload,
                payload_size,
                delivery_status.value,
            ),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Connection logging
    # ------------------------------------------------------------------

    async def log_connection(self, sandbox_id: str) -> None:
        """Record a sandbox connecting to the broker.

        Args:
            sandbox_id: The connecting sandbox's identity.
        """
        await self._db.execute(
            """
            INSERT OR REPLACE INTO connections (sandbox_id, connected_at)
            VALUES (?, ?)
            """,
            (sandbox_id, time.time()),
        )
        await self._db.commit()

    async def log_disconnection(self, sandbox_id: str, reason: str = "") -> None:
        """Record a sandbox disconnecting from the broker.

        Args:
            sandbox_id: The disconnecting sandbox's identity.
            reason: Optional human-readable disconnect reason.
        """
        await self._db.execute(
            """
            UPDATE connections
            SET disconnected_at = ?, disconnect_reason = ?
            WHERE sandbox_id = ?
            """,
            (time.time(), reason, sandbox_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run an arbitrary SELECT and return rows as dicts.

        Args:
            sql: SQL query string (should be SELECT only).
            params: Positional bind parameters.

        Returns:
            A list of row dicts keyed by column name.
        """
        cursor = await self._db.execute(sql, params)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def export_jsonl(self, path: str, since: float | None = None) -> int:
        """Export messages to a JSONL file.

        Args:
            path: Output file path.
            since: Optional Unix timestamp; only export messages after
                this time.

        Returns:
            Number of messages exported.
        """
        sql = "SELECT * FROM messages"
        params: tuple[Any, ...] = ()
        if since is not None:
            sql += " WHERE timestamp >= ?"
            params = (since,)
        sql += " ORDER BY timestamp"

        rows = await self.query(sql, params)
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
        return len(rows)
