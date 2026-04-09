"""Async SQLite audit database for the NMB broker.

Every message routed through the broker is logged here with full payload
content (configurable).  The audit DB serves double duty: operational
debugging and training-data source for the training flywheel.

Schema is managed by Alembic — the application never issues DDL directly.
On first open, ``AuditDB`` runs ``alembic upgrade head`` to ensure the
schema is current.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

from nemoclaw_escapades.config import DEFAULT_NMB_AUDIT_BATCH_SIZE, DEFAULT_NMB_AUDIT_QUEUE_SIZE
from nemoclaw_escapades.nmb.audit.models import ConnectionRow, MessageRow
from nemoclaw_escapades.nmb.models import DeliveryStatus, NMBMessage

logger = logging.getLogger("nmb.audit")

_ALEMBIC_DIR = Path(__file__).parent / "alembic"
_ALEMBIC_INI = Path(__file__).parent / "alembic.ini"


def _discover_head_revision() -> str:
    """Derive the current Alembic head from the migration scripts.

    Uses Alembic's ``ScriptDirectory`` to walk the revision graph and
    find the head, so the value never goes stale when a new migration
    is added.

    Returns:
        The revision identifier of the single head (e.g. ``"003"``).

    Raises:
        RuntimeError: If the script directory has no revisions or
            multiple heads.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ALEMBIC_INI))
    scripts = ScriptDirectory.from_config(cfg)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Expected exactly 1 Alembic head, found {len(heads)}: {heads}")
    return heads[0]


# Resolved once at import time — negligible cost (no DB access, just
# reads the Python migration files from disk).
_HEAD_REVISION: str = _discover_head_revision()


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
        self._write_queue: asyncio.Queue[Any] | None = None
        self._flush_task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        """Run Alembic migrations and open an async SQLAlchemy engine.

        Creates the parent directory if it doesn't exist, invokes
        ``alembic upgrade head`` in a subprocess, then creates an
        ``aiosqlite``-backed async engine with WAL journal mode and
        incremental auto-vacuum.

        Raises:
            subprocess.CalledProcessError: If the Alembic migration
                subprocess exits non-zero.

        Side effects:
            Populates ``_engine`` and ``_session_factory``.
        """
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
        """Dispose of the engine and release all connections.

        Safe to call even if the DB was never opened (no-op in that
        case).  Does **not** stop the background writer — call
        ``stop_background_writer`` first if it is running.

        Side effects:
            Sets ``_engine`` and ``_session_factory`` to ``None``.
        """
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    # ------------------------------------------------------------------
    # Background batch writer
    # ------------------------------------------------------------------

    async def start_background_writer(self) -> None:
        """Start a background task that batch-commits audit writes.

        When active, ``enqueue_message`` and ``enqueue_status_update``
        drop items into an ``asyncio.Queue``.  The background task
        drains the queue and commits in batches, keeping audit I/O off
        the message-routing hot path.
        """
        self._write_queue = asyncio.Queue(maxsize=DEFAULT_NMB_AUDIT_QUEUE_SIZE)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop_background_writer(self) -> None:
        """Cancel the background writer and flush remaining items.

        Cancels the flush-loop task, then drains any items still in
        the queue with a final synchronous batch commit so nothing is
        lost on a clean shutdown.

        Side effects:
            Sets ``_write_queue`` and ``_flush_task`` to ``None``.
        """
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._drain_queue()
        self._write_queue = None
        self._flush_task = None

    def enqueue_message(
        self,
        msg: NMBMessage,
        delivery_status: DeliveryStatus,
    ) -> None:
        """Non-blocking: add a message insert to the background write queue.

        If the queue is at capacity the item is dropped and a warning
        is logged — audit back-pressure must never block routing.

        Args:
            msg: The routed message to persist.
            delivery_status: Outcome of the delivery attempt.
        """
        if self._write_queue is not None:
            try:
                self._write_queue.put_nowait(("insert", msg, delivery_status))
            except asyncio.QueueFull:
                logger.warning("Audit write queue full, dropping message %s", msg.id)

    def enqueue_status_update(
        self,
        msg_id: str,
        delivery_status: DeliveryStatus,
    ) -> None:
        """Non-blocking: enqueue a delivery-status UPDATE for a previously logged message.

        Args:
            msg_id: Primary key of the message row to update.
            delivery_status: New status value.
        """
        if self._write_queue is not None:
            try:
                self._write_queue.put_nowait(("update", msg_id, delivery_status))
            except asyncio.QueueFull:
                logger.warning("Audit write queue full, dropping status update for %s", msg_id)

    async def _flush_loop(self) -> None:
        """Background task: block on the first item, greedily drain more, then batch-commit.

        Blocks on ``Queue.get()`` for the first item (back-pressure
        friendly), then greedily pulls up to
        ``DEFAULT_NMB_AUDIT_BATCH_SIZE - 1`` additional items without
        waiting.  The collected batch is committed in a single
        transaction via ``_write_batch``.  Failures are logged but
        never propagated — the loop continues after a bad batch.

        Runs until cancelled by ``stop_background_writer``.
        """
        assert self._write_queue is not None
        try:
            while True:
                first = await self._write_queue.get()
                batch: list[Any] = [first]
                for _ in range(DEFAULT_NMB_AUDIT_BATCH_SIZE - 1):
                    try:
                        batch.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                try:
                    await self._write_batch(batch)
                except Exception:
                    logger.warning(
                        "Audit batch write failed (%d items)", len(batch), exc_info=True
                    )
        except asyncio.CancelledError:
            pass

    async def _drain_queue(self) -> None:
        """Flush all remaining items from the write queue on shutdown.

        Called by ``stop_background_writer`` after the flush-loop task
        has been cancelled.  Pulls every remaining item without
        blocking and commits them in one batch.  Failures are logged
        but never raised — shutdown must not be blocked by audit
        errors.
        """
        if not self._write_queue:
            return
        batch: list[Any] = []
        while not self._write_queue.empty():
            try:
                batch.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            try:
                await self._write_batch(batch)
            except Exception:
                logger.warning("Audit drain failed (%d items)", len(batch), exc_info=True)

    async def _write_batch(self, batch: list[Any]) -> None:
        """Write a batch of inserts and updates in a single transaction.

        Inserts are added to the session first.  If the batch contains
        both inserts and updates, a ``session.flush()`` is issued
        between them so that the newly inserted rows are visible to the
        UPDATE statements that follow.

        Args:
            batch: List of ``("insert", NMBMessage, DeliveryStatus)``
                or ``("update", msg_id, DeliveryStatus)`` tuples.

        Raises:
            Exception: Any SQLAlchemy or aiosqlite error (caught and
                logged by the caller).
        """
        inserts = [item for item in batch if item[0] == "insert"]
        updates = [item for item in batch if item[0] == "update"]

        async with self._session() as session:
            for _, msg, status in inserts:
                payload_json = json.dumps(msg.payload) if msg.payload is not None else ""
                payload_size = len(payload_json.encode()) if payload_json else 0
                stored_payload = payload_json if self.persist_payloads else ""
                session.add(
                    MessageRow(
                        id=msg.id,
                        timestamp=msg.timestamp or time.time(),
                        op=msg.op.value,
                        from_sandbox=msg.from_sandbox,
                        to_sandbox=msg.to_sandbox,
                        type=msg.type,
                        reply_to=msg.reply_to,
                        channel=msg.channel,
                        payload=stored_payload,
                        payload_size=payload_size,
                        delivery_status=status.value,
                    )
                )

            if inserts and updates:
                await session.flush()

            for _, msg_id, status in updates:
                await session.execute(
                    update(MessageRow)
                    .where(MessageRow.id == msg_id)
                    .values(delivery_status=status.value)
                )

            await session.commit()

    # ------------------------------------------------------------------
    # Direct status update (for callers not using the background writer)
    # ------------------------------------------------------------------

    async def update_delivery_status(
        self,
        msg_id: str,
        delivery_status: DeliveryStatus,
    ) -> None:
        """Update the delivery status of a previously logged message.

        Directly executes an UPDATE + COMMIT (does **not** go through
        the background writer).  Use ``enqueue_status_update`` if you
        want non-blocking behaviour.

        Args:
            msg_id: Primary key of the message row.
            delivery_status: New status value.

        Raises:
            RuntimeError: If the DB is not open.
        """
        async with self._session() as session:
            stmt = (
                update(MessageRow)
                .where(MessageRow.id == msg_id)
                .values(delivery_status=delivery_status.value)
            )
            await session.execute(stmt)
            await session.commit()

    def _run_migrations(self) -> None:
        """Ensure the DB schema is at the latest Alembic revision.

        First does a cheap ``SELECT`` on the ``alembic_version`` table
        to see if the schema is already at ``_HEAD_REVISION``.  If it
        is, the subprocess is skipped entirely (~500 ms saved).  If the
        DB doesn't exist, has no version table, or is at an older
        revision, falls through to ``alembic upgrade head``.

        Raises:
            subprocess.CalledProcessError: If the Alembic subprocess
                exits non-zero.
        """
        if not self._needs_migration():
            return

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

    def _needs_migration(self) -> bool:
        """Check whether the DB schema is behind ``_HEAD_REVISION``.

        Returns ``True`` (needs migration) if the DB file doesn't
        exist, the ``alembic_version`` table is missing, or the stored
        revision doesn't match ``_HEAD_REVISION``.  Any SQLite error
        is treated as "needs migration" to be safe.

        Returns:
            ``False`` if the schema is already current.
        """
        db = Path(self.db_path)
        if not db.exists():
            return True
        try:
            with sqlite3.connect(str(db)) as conn:
                row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            return row is None or row[0] != _HEAD_REVISION
        except Exception:
            return True

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory, raising if the DB is not open.

        Returns:
            The ``async_sessionmaker`` created by ``open()``.

        Raises:
            RuntimeError: If ``open()`` has not been called or
                ``close()`` has already been called.
        """
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

        Directly executes an INSERT + COMMIT (does **not** go through
        the background writer).  Use ``enqueue_message`` if you want
        non-blocking behaviour.

        When ``persist_payloads`` is ``False`` the payload column is
        stored as an empty string, but ``payload_size`` still records
        the original byte count.

        Args:
            msg: The message that was routed.
            delivery_status: Outcome of the delivery attempt.

        Raises:
            RuntimeError: If the DB is not open.
            sqlalchemy.exc.IntegrityError: If a row with the same
                ``id`` already exists.
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
                    to_sandbox=msg.to_sandbox,
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

        Inserts a new row every time so full connection history is
        preserved.  Each ``sandbox_id`` is globally unique per launch.

        Args:
            sandbox_id: Globally unique sandbox identifier.

        Raises:
            RuntimeError: If the DB is not open.
        """
        async with self._session() as session:
            session.add(ConnectionRow(sandbox_id=sandbox_id, connected_at=time.time()))
            await session.commit()

    async def log_disconnection(self, sandbox_id: str, reason: str = "") -> None:
        """Record a sandbox disconnecting from the broker.

        Matches on ``sandbox_id`` (globally unique per launch) so
        that exactly one connection row is closed.

        Args:
            sandbox_id: The globally unique identifier of the
                disconnecting sandbox.
            reason: Human-readable disconnect reason (e.g.
                ``"crashed"``, ``"disconnected"``).

        Raises:
            RuntimeError: If the DB is not open.
        """
        async with self._session() as session:
            stmt = (
                update(ConnectionRow)
                .where(
                    ConnectionRow.sandbox_id == sandbox_id,
                    ConnectionRow.disconnected_at.is_(None),
                )
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
        """Run a read-only SQL query and return rows as dicts.

        Only ``SELECT`` statements are allowed; anything else is
        rejected before it reaches the database.  User-supplied
        *values* must go through ``:name`` bind parameters in
        *params* — they are passed to SQLAlchemy's ``text()`` which
        uses parameterized queries, so values are never interpolated
        into the SQL string.

        Args:
            sql: SQL query string (must start with ``SELECT``).
            params: Named bind parameters for ``:name`` placeholders.

        Returns:
            A list of row dicts keyed by column name.

        Raises:
            ValueError: If *sql* is not a SELECT statement.
            RuntimeError: If the DB is not open.
        """
        normalized = sql.strip().upper()
        if not normalized.startswith("SELECT"):
            raise ValueError(f"Only SELECT queries are allowed, got: {sql[:40]!r}")
        async with self._session() as session:
            result = await session.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings()]

    async def export_jsonl(self, path: str, since: float | None = None) -> int:
        """Export messages to a JSONL file (one JSON object per line).

        Reads all matching rows into memory, then writes them
        sequentially.  Suitable for moderate-sized databases; for very
        large exports consider streaming with a server-side cursor.

        Args:
            path: Output file path (created or overwritten).
            since: Optional Unix epoch timestamp.  When provided, only
                messages with ``timestamp >= since`` are exported.

        Returns:
            Number of messages written.

        Raises:
            RuntimeError: If the DB is not open.
            OSError: If the output file cannot be written.
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
