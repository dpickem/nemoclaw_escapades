"""NMB Broker — asyncio WebSocket message router.

The broker is the central routing component of the NemoClaw Message Bus.
It accepts WebSocket connections from sandbox agents (via the OpenShell
proxy at ``messages.local:9876``), authenticates them by their
``X-Sandbox-ID`` header, and routes messages between them.

Supported operations:

- **send** — point-to-point fire-and-forget
- **request / reply** — correlated request-reply with timeout tracking
- **subscribe / unsubscribe / publish** — pub/sub channels
- **stream** — ordered chunk delivery to a target

Every routed message is logged to the audit DB (SQLite via Alembic).

Run standalone::

    python -m nemoclaw_escapades.nmb.broker --port 9876 \\
        --audit-db ~/.nemoclaw/nmb/audit.db
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.http11 import Request, Response

from nemoclaw_escapades.nmb.audit.db import AuditDB
from nemoclaw_escapades.nmb.models import (
    DeliveryStatus,
    ErrorCode,
    FrameValidationError,
    NMBMessage,
    Op,
    PendingRequest,
    parse_frame,
    serialize_frame,
)

logger = logging.getLogger("nmb.broker")

# ---------------------------------------------------------------------------
# Broker configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9876
DEFAULT_AUDIT_DB = "~/.nemoclaw/nmb/audit.db"
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_PENDING_PER_SANDBOX = 100
MAX_CHANNELS_PER_SANDBOX = 50
DEFAULT_REQUEST_TIMEOUT = 300.0


@dataclass
class BrokerConfig:
    """Runtime configuration for the NMB broker.

    Attributes:
        host: Bind address.
        port: Bind port.
        audit_db_path: Path to the SQLite audit database.
        persist_payloads: Whether to store full payloads in the audit DB.
        max_message_size: Maximum allowed payload size in bytes.
        max_pending_per_sandbox: Maximum in-flight requests per sandbox.
        default_request_timeout: Default timeout for request-reply in seconds.
        max_channels_per_sandbox: Maximum channel subscriptions per sandbox.
    """

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    audit_db_path: str = DEFAULT_AUDIT_DB
    persist_payloads: bool = True
    max_message_size: int = MAX_MESSAGE_SIZE
    max_pending_per_sandbox: int = MAX_PENDING_PER_SANDBOX
    default_request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_channels_per_sandbox: int = MAX_CHANNELS_PER_SANDBOX


# ---------------------------------------------------------------------------
# Tracked pending request (adds the timeout task handle)
# ---------------------------------------------------------------------------


@dataclass
class TrackedPending:
    """A pending request with its associated timeout task.

    Attributes:
        pending: The pending request metadata.
        timeout_task: The asyncio task that fires the timeout frame.
    """

    pending: PendingRequest
    timeout_task: asyncio.Task[None] | None = field(repr=False, default=None)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


class NMBBroker:
    """Asyncio WebSocket message broker for the NemoClaw Message Bus.

    Attributes:
        config: Broker configuration.
    """

    def __init__(self, config: BrokerConfig | None = None) -> None:
        """Create a broker instance with the given configuration.

        Args:
            config: Runtime settings (port, limits, audit path).
                Defaults to ``BrokerConfig()`` with all default values.
        """
        self.config: BrokerConfig = config or BrokerConfig()

        # sandbox_id -> websocket connection
        self._connections: dict[str, ServerConnection] = {}
        # websocket id(ws) -> sandbox_id (reverse lookup)
        self._ws_to_sandbox: dict[int, str] = {}

        # channel_name -> set of sandbox_ids
        self._channels: dict[str, set[str]] = {}
        # sandbox_id -> set of channel_names (reverse lookup for limits)
        self._sandbox_channels: dict[str, set[str]] = {}

        # request_id -> tracked pending
        self._pending: dict[str, TrackedPending] = {}
        # sandbox_id -> count of pending requests originated by that sandbox
        self._pending_counts: dict[str, int] = {}

        self._audit: AuditDB | None = None
        self._server: Server | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the audit DB and start the WebSocket server.

        Expands ``~`` in the audit DB path, runs Alembic migrations,
        and binds the WebSocket server to ``config.host:config.port``.
        """
        from pathlib import Path

        db_path = str(Path(self.config.audit_db_path).expanduser())
        self._audit = AuditDB(db_path, persist_payloads=self.config.persist_payloads)
        await self._audit.open()

        self._server = await websockets.serve(
            self._handler,
            self.config.host,
            self.config.port,
            max_size=self.config.max_message_size,
            process_request=self._process_request,
        )
        logger.info("NMB broker listening on %s:%d", self.config.host, self.config.port)

    async def stop(self) -> None:
        """Shut down the server, cancel pending timeouts, and close the audit DB."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for tracked in list(self._pending.values()):
            if tracked.timeout_task and not tracked.timeout_task.done():
                tracked.timeout_task.cancel()
        self._pending.clear()
        if self._audit:
            await self._audit.close()
        logger.info("NMB broker stopped")

    async def serve_forever(self) -> None:
        """Start the broker and block until the server is closed or interrupted."""
        await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @staticmethod
    async def _process_request(connection: ServerConnection, request: Request) -> Response | None:
        """Extract ``X-Sandbox-ID`` from the WebSocket upgrade request.

        Called by ``websockets.serve`` before the handshake completes.
        If the header is missing the connection is rejected with HTTP 400.

        Args:
            connection: The nascent server connection.
            request: The HTTP upgrade request containing headers.

        Returns:
            ``None`` to proceed with the handshake, or an HTTP error
            ``Response`` to reject the connection.
        """
        sandbox_id = request.headers.get("X-Sandbox-ID")
        if not sandbox_id:
            return connection.respond(400, "Missing X-Sandbox-ID header\n")
        # Stash on the connection object for use in _handler
        connection.sandbox_id = sandbox_id  # type: ignore[attr-defined]
        return None

    async def _handler(self, websocket: ServerConnection) -> None:
        """Per-connection message loop: register, dispatch frames, clean up on close.

        Args:
            websocket: The accepted WebSocket connection (already
                authenticated via ``_process_request``).
        """
        sandbox_id: str = websocket.sandbox_id  # type: ignore[attr-defined]
        self._register(sandbox_id, websocket)

        try:
            async for raw_frame in websocket:
                if isinstance(raw_frame, bytes):
                    raw_frame = raw_frame.decode()
                await self._dispatch(sandbox_id, websocket, raw_frame)
        except websockets.ConnectionClosed:
            pass
        finally:
            await self._unregister(sandbox_id)

    def _register(self, sandbox_id: str, ws: ServerConnection) -> None:
        """Register a newly connected sandbox in the connection registry.

        If *sandbox_id* is already connected (stale connection), the old
        entry is replaced.

        Args:
            sandbox_id: Proxy-authenticated sandbox identity.
            ws: The WebSocket connection for this sandbox.
        """
        old = self._connections.get(sandbox_id)
        if old is not None:
            self._ws_to_sandbox.pop(id(old), None)
        self._connections[sandbox_id] = ws
        self._ws_to_sandbox[id(ws)] = sandbox_id
        logger.info("Sandbox connected: %s", sandbox_id)
        if self._audit:
            asyncio.create_task(self._audit.log_connection(sandbox_id))

    async def _unregister(self, sandbox_id: str) -> None:
        """Unregister a disconnected sandbox and clean up all associated state.

        Removes channel subscriptions, cancels pending request timeouts,
        publishes a ``sandbox.shutdown`` event to the ``system`` channel,
        and logs the disconnection in the audit DB.

        Args:
            sandbox_id: The identity of the disconnected sandbox.
        """
        ws = self._connections.pop(sandbox_id, None)
        if ws is not None:
            self._ws_to_sandbox.pop(id(ws), None)

        # Clean up channel subscriptions
        for ch in list(self._sandbox_channels.get(sandbox_id, set())):
            subs = self._channels.get(ch)
            if subs:
                subs.discard(sandbox_id)
                if not subs:
                    del self._channels[ch]
        self._sandbox_channels.pop(sandbox_id, None)

        # Expire pending requests from this sandbox
        expired = [rid for rid, t in self._pending.items() if t.pending.from_sandbox == sandbox_id]
        for rid in expired:
            tracked = self._pending.pop(rid, None)
            if tracked and tracked.timeout_task and not tracked.timeout_task.done():
                tracked.timeout_task.cancel()
        self._pending_counts.pop(sandbox_id, None)

        # Publish system shutdown event
        shutdown_msg = NMBMessage(
            op=Op.DELIVER,
            from_sandbox="system",
            type="sandbox.shutdown",
            timestamp=time.time(),
            payload={"sandbox_id": sandbox_id, "reason": "disconnected"},
        )
        await self._broadcast_system(shutdown_msg)

        if self._audit:
            await self._audit.log_disconnection(sandbox_id, "disconnected")
        logger.info("Sandbox disconnected: %s", sandbox_id)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, sender_id: str, ws: ServerConnection, raw: str) -> None:
        """Parse, validate, and route an inbound frame to the correct handler.

        Enforces sender identity by overwriting ``from_sandbox`` with
        the authenticated *sender_id*, then dispatches based on ``op``.

        Args:
            sender_id: Authenticated sandbox identity of the sender.
            ws: The sender's WebSocket connection (for ACK/error replies).
            raw: The raw JSON text frame received from the WebSocket.
        """
        try:
            msg = parse_frame(raw)
        except FrameValidationError as exc:
            await self._send_error(ws, "", exc.code, str(exc))
            return

        # Enforce identity: overwrite from_sandbox
        msg.from_sandbox = sender_id
        msg.timestamp = time.time()

        try:
            from nemoclaw_escapades.nmb.models import validate_frame

            validate_frame(msg)
        except FrameValidationError as exc:
            await self._send_error(ws, msg.id, exc.code, str(exc))
            return

        handler_map: dict[Op, Any] = {
            Op.SEND: self._handle_send,
            Op.REQUEST: self._handle_request,
            Op.REPLY: self._handle_reply,
            Op.SUBSCRIBE: self._handle_subscribe,
            Op.UNSUBSCRIBE: self._handle_unsubscribe,
            Op.PUBLISH: self._handle_publish,
            Op.STREAM: self._handle_stream,
        }

        handler = handler_map.get(msg.op)
        if handler is None:
            await self._send_error(
                ws, msg.id, ErrorCode.INVALID_FRAME, f"Client cannot send op={msg.op.value}"
            )
            return

        await handler(sender_id, ws, msg)

    # ------------------------------------------------------------------
    # Op handlers
    # ------------------------------------------------------------------

    async def _handle_send(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a fire-and-forget ``send`` to the target sandbox.

        Delivers the message, ACKs the sender, and logs to audit.
        Returns a ``TARGET_OFFLINE`` error if the target is not connected.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``send`` message.
        """
        assert msg.to is not None
        target_ws = self._connections.get(msg.to)
        if target_ws is None:
            await self._send_error(ws, msg.id, ErrorCode.TARGET_OFFLINE, f"{msg.to} not connected")
            await self._audit_msg(msg, DeliveryStatus.ERROR)
            return

        deliver = self._make_deliver(msg)
        await target_ws.send(serialize_frame(deliver))
        await self._send_ack(ws, msg.id)
        await self._audit_msg(msg, DeliveryStatus.DELIVERED)

    async def _handle_request(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a ``request`` and register it for reply correlation and timeout.

        Checks the per-sandbox pending request limit, delivers to the
        target, starts a timeout task, and ACKs the sender.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``request`` message.
        """
        assert msg.to is not None

        count = self._pending_counts.get(sender_id, 0)
        if count >= self.config.max_pending_per_sandbox:
            await self._send_error(ws, msg.id, ErrorCode.RATE_LIMITED, "Too many pending requests")
            return

        target_ws = self._connections.get(msg.to)
        if target_ws is None:
            await self._send_error(ws, msg.id, ErrorCode.TARGET_OFFLINE, f"{msg.to} not connected")
            await self._audit_msg(msg, DeliveryStatus.ERROR)
            return

        timeout = msg.timeout or self.config.default_request_timeout
        pending = PendingRequest(request_id=msg.id, from_sandbox=sender_id, timeout=timeout)
        timeout_task = asyncio.create_task(self._timeout_request(msg.id, sender_id, timeout))
        self._pending[msg.id] = TrackedPending(pending=pending, timeout_task=timeout_task)
        self._pending_counts[sender_id] = count + 1

        deliver = self._make_deliver(msg)
        await target_ws.send(serialize_frame(deliver))
        await self._send_ack(ws, msg.id)
        await self._audit_msg(msg, DeliveryStatus.DELIVERED)

    async def _handle_reply(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a ``reply`` back to the original requester.

        Cancels the pending timeout, decrements the requester's pending
        count, and delivers the reply. Returns ``INVALID_FRAME`` if no
        matching pending request exists.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``reply`` message (must have ``reply_to``).
        """
        assert msg.reply_to is not None
        tracked = self._pending.pop(msg.reply_to, None)
        if tracked is None:
            await self._send_error(
                ws, msg.id, ErrorCode.INVALID_FRAME, f"No pending request with id={msg.reply_to}"
            )
            return

        if tracked.timeout_task and not tracked.timeout_task.done():
            tracked.timeout_task.cancel()
        requester_id = tracked.pending.from_sandbox
        self._pending_counts[requester_id] = max(0, self._pending_counts.get(requester_id, 1) - 1)

        target_ws = self._connections.get(requester_id)
        if target_ws is None:
            await self._audit_msg(msg, DeliveryStatus.ERROR)
            return

        deliver = self._make_deliver(msg)
        await target_ws.send(serialize_frame(deliver))
        await self._audit_msg(msg, DeliveryStatus.DELIVERED)

    async def _handle_subscribe(
        self, sender_id: str, ws: ServerConnection, msg: NMBMessage
    ) -> None:
        """Add a sandbox to a pub/sub channel.

        Returns ``CHANNEL_FULL`` if the sandbox has reached the
        per-sandbox channel subscription limit.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``subscribe`` message.
        """
        assert msg.channel is not None
        sandbox_chans = self._sandbox_channels.setdefault(sender_id, set())
        if len(sandbox_chans) >= self.config.max_channels_per_sandbox:
            await self._send_error(
                ws, msg.id or "", ErrorCode.CHANNEL_FULL, "Too many channel subscriptions"
            )
            return

        self._channels.setdefault(msg.channel, set()).add(sender_id)
        sandbox_chans.add(msg.channel)
        await self._send_ack(ws, msg.id)

    async def _handle_unsubscribe(
        self, sender_id: str, ws: ServerConnection, msg: NMBMessage
    ) -> None:
        """Remove a sandbox from a pub/sub channel.

        Cleans up the channel entry entirely if no subscribers remain.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``unsubscribe`` message.
        """
        assert msg.channel is not None
        subs = self._channels.get(msg.channel)
        if subs:
            subs.discard(sender_id)
            if not subs:
                del self._channels[msg.channel]
        sandbox_chans = self._sandbox_channels.get(sender_id)
        if sandbox_chans:
            sandbox_chans.discard(msg.channel)
        await self._send_ack(ws, msg.id)

    async def _handle_publish(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Broadcast a ``publish`` message to all subscribers of a channel.

        The sender does not receive its own message.  Silently skips
        subscribers whose connections have closed.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``publish`` message.
        """
        assert msg.channel is not None
        deliver = self._make_deliver(msg)
        frame = serialize_frame(deliver)

        subscribers = self._channels.get(msg.channel, set())
        for sub_id in list(subscribers):
            if sub_id == sender_id:
                continue
            sub_ws = self._connections.get(sub_id)
            if sub_ws:
                try:
                    await sub_ws.send(frame)
                except websockets.ConnectionClosed:
                    pass

        await self._send_ack(ws, msg.id)
        await self._audit_msg(msg, DeliveryStatus.DELIVERED)

    async def _handle_stream(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Forward a ``stream`` chunk to the target sandbox.

        Stream chunks are not ACKed individually.  Returns
        ``TARGET_OFFLINE`` if the target is not connected.

        Args:
            sender_id: Authenticated sender sandbox ID.
            ws: Sender's WebSocket connection.
            msg: The validated ``stream`` message.
        """
        assert msg.to is not None
        target_ws = self._connections.get(msg.to)
        if target_ws is None:
            await self._send_error(ws, msg.id, ErrorCode.TARGET_OFFLINE, f"{msg.to} not connected")
            return

        deliver = self._make_deliver(msg)
        await target_ws.send(serialize_frame(deliver))

    # ------------------------------------------------------------------
    # Timeout handling
    # ------------------------------------------------------------------

    async def _timeout_request(self, request_id: str, requester_id: str, timeout: float) -> None:
        """Sleep for *timeout* seconds, then send a ``timeout`` frame to the requester.

        If the request has already been replied to (popped from
        ``_pending``) by the time this fires, the timeout is a no-op.

        Args:
            request_id: The original request message ID.
            requester_id: Sandbox that sent the request.
            timeout: Seconds to wait before firing.
        """
        await asyncio.sleep(timeout)
        tracked = self._pending.pop(request_id, None)
        if tracked is None:
            return

        self._pending_counts[requester_id] = max(0, self._pending_counts.get(requester_id, 1) - 1)

        ws = self._connections.get(requester_id)
        if ws:
            timeout_msg = NMBMessage(
                op=Op.TIMEOUT,
                id=request_id,
                message=f"No reply within {timeout}s",
            )
            try:
                await ws.send(serialize_frame(timeout_msg))
            except websockets.ConnectionClosed:
                pass

        # Log the timeout in audit
        original_msg = NMBMessage(
            op=Op.REQUEST,
            id=request_id,
            from_sandbox=requester_id,
            type="unknown",
            payload={},
        )
        await self._audit_msg(original_msg, DeliveryStatus.TIMEOUT)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_deliver(msg: NMBMessage) -> NMBMessage:
        """Wrap an inbound message as a ``deliver`` frame for the recipient.

        Args:
            msg: The original inbound message.

        Returns:
            A new ``NMBMessage`` with ``op=DELIVER`` and all relevant
            fields copied from the original.
        """
        return NMBMessage(
            op=Op.DELIVER,
            id=msg.id,
            from_sandbox=msg.from_sandbox,
            type=msg.type,
            reply_to=msg.reply_to,
            channel=msg.channel,
            stream_id=msg.stream_id,
            seq=msg.seq,
            done=msg.done,
            timestamp=msg.timestamp,
            payload=msg.payload,
        )

    @staticmethod
    async def _send_ack(ws: ServerConnection, msg_id: str) -> None:
        """Send an ACK frame confirming receipt of *msg_id*.

        Args:
            ws: The client's WebSocket connection.
            msg_id: The ``id`` of the message being acknowledged.
        """
        ack = NMBMessage(op=Op.ACK, id=msg_id)
        try:
            await ws.send(serialize_frame(ack))
        except websockets.ConnectionClosed:
            pass

    @staticmethod
    async def _send_error(ws: ServerConnection, msg_id: str, code: ErrorCode, message: str) -> None:
        """Send an error frame to the client.

        Args:
            ws: The client's WebSocket connection.
            msg_id: The ``id`` of the message that caused the error.
            code: Structured error code.
            message: Human-readable error description.
        """
        err = NMBMessage(op=Op.ERROR, id=msg_id, code=code.value, message=message)
        try:
            await ws.send(serialize_frame(err))
        except websockets.ConnectionClosed:
            pass

    async def _broadcast_system(self, msg: NMBMessage) -> None:
        """Broadcast a system message to all subscribers of the ``system`` channel.

        Args:
            msg: The system-level message to broadcast (e.g.
                ``sandbox.shutdown``).
        """
        frame = serialize_frame(msg)
        for sub_id in list(self._channels.get("system", set())):
            ws = self._connections.get(sub_id)
            if ws:
                try:
                    await ws.send(frame)
                except websockets.ConnectionClosed:
                    pass

    async def _audit_msg(self, msg: NMBMessage, status: DeliveryStatus) -> None:
        """Log a message to the audit DB (if available).

        Failures are logged as warnings but never propagated — audit
        errors must not break message routing.

        Args:
            msg: The message to audit.
            status: Delivery outcome (delivered, error, or timeout).
        """
        if self._audit:
            try:
                await self._audit.log_message(msg, status)
            except Exception:
                logger.warning("Failed to audit message %s", msg.id, exc_info=True)

    # ------------------------------------------------------------------
    # Health / stats
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return broker health statistics.

        Returns:
            A dict with connected sandbox count, pending requests,
            channel count, and active subscriptions.
        """
        return {
            "connected_sandboxes": list(self._connections.keys()),
            "num_connections": len(self._connections),
            "num_pending_requests": len(self._pending),
            "channels": {ch: len(subs) for ch, subs in self._channels.items()},
        }


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def _main() -> None:
    """CLI entry point for running the broker standalone.

    Supports ``--health``, ``--query``, and ``--export-jsonl`` admin
    commands (which open the audit DB, execute, and exit) as well as
    the default mode of running the WebSocket server.
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="NMB Broker — NemoClaw Message Bus")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port")
    parser.add_argument(
        "--audit-db",
        default=DEFAULT_AUDIT_DB,
        help="Path to audit SQLite DB",
    )
    parser.add_argument(
        "--no-audit-payloads",
        action="store_true",
        help="Do not persist full payloads in the audit DB",
    )
    parser.add_argument("--health", action="store_true", help="Print health and exit")
    parser.add_argument("--query", type=str, help="Run an SQL query against the audit DB and exit")
    parser.add_argument("--export-jsonl", type=str, help="Export messages to JSONL file and exit")
    parser.add_argument("--since", type=str, help="ISO date for --export-jsonl (e.g. 2026-04-01)")

    args = parser.parse_args()
    db_path = str(Path(args.audit_db).expanduser())

    if args.health or args.query or args.export_jsonl:
        asyncio.run(_admin_command(args, db_path))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = BrokerConfig(
        host=args.host,
        port=args.port,
        audit_db_path=db_path,
        persist_payloads=not args.no_audit_payloads,
    )
    broker = NMBBroker(config)
    asyncio.run(broker.serve_forever())


async def _admin_command(args: Any, db_path: str) -> None:
    """Run an admin command (health, query, or JSONL export) and exit.

    Args:
        args: Parsed argparse namespace with ``query``, ``export_jsonl``,
            and ``since`` attributes.
        db_path: Expanded filesystem path to the audit database.
    """
    import json as json_mod

    audit = AuditDB(db_path)
    await audit.open()
    try:
        if args.query:
            rows = await audit.query(args.query)
            for row in rows:
                print(json_mod.dumps(row, default=str))
        elif args.export_jsonl:
            since = None
            if args.since:
                from datetime import UTC, datetime

                since = datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp()
            count = await audit.export_jsonl(args.export_jsonl, since=since)
            print(f"Exported {count} messages to {args.export_jsonl}")
        else:
            print("Health check requires a running broker (use the API).")
    finally:
        await audit.close()


if __name__ == "__main__":
    _main()
