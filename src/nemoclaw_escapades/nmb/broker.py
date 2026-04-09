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

from nemoclaw_escapades.config import (
    DEFAULT_NMB_AUDIT_DB_PATH,
    DEFAULT_NMB_PORT,
    DEFAULT_NMB_SUBSCRIBER_SEND_TIMEOUT,
    BrokerConfig,
)
from nemoclaw_escapades.nmb.audit.db import AuditDB
from nemoclaw_escapades.nmb.models import (
    DeliveryStatus,
    ErrorCode,
    FrameValidationError,
    NMBMessage,
    Op,
    PendingRequest,
)

logger = logging.getLogger("nmb.broker")

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

    The broker is the single routing hub for all inter-sandbox
    communication.  Sandbox agents connect via WebSocket (through the
    OpenShell proxy at ``messages.local:9876``), authenticate with an
    ``X-Sandbox-ID`` header, and exchange JSON frames defined in
    :mod:`nemoclaw_escapades.nmb.models`.

    ``sandbox_id`` is the **only** identity concept.  It is globally
    unique per launch (the client appends a random suffix) and serves
    as the primary key for routing, channel subscriptions,
    pending-request counts, and audit records.

    Supported operations:

    - **send** — point-to-point fire-and-forget delivery with ACK.
    - **request / reply** — correlated request-reply with broker-side
      timeout tracking and per-sandbox pending-request limits.
    - **subscribe / unsubscribe / publish** — pub/sub channels with
      concurrent fanout (``asyncio.gather``) and per-subscriber send
      timeouts so one slow consumer cannot stall the others.
    - **stream** — ordered chunk delivery to a target sandbox.

    Lifecycle:

    1. ``start()`` — opens the audit DB, starts the background audit
       writer, and binds the WebSocket server.
    2. ``serve_forever()`` — convenience wrapper that calls ``start()``
       then blocks.
    3. ``stop()`` — closes the server, cancels pending timeouts, flushes
       and stops the audit writer, and disposes the DB engine.

    Every routed message is logged to a SQLite audit DB
    (:class:`~nemoclaw_escapades.nmb.audit.db.AuditDB`) via a
    non-blocking background batch writer so audit I/O never sits on
    the message-routing hot path.

    Attributes:
        config: Broker runtime configuration (port, limits, audit
            path, etc.).
    """

    def __init__(self, config: BrokerConfig | None = None) -> None:
        """Create a broker instance with the given configuration.

        Args:
            config: Runtime settings (port, limits, audit path).
                Defaults to ``BrokerConfig()`` with all default values.
        """
        self.config: BrokerConfig = config or BrokerConfig()

        # --- Connection registry (keyed by sandbox_id) ---
        # Each sandbox_id is globally unique per launch (the client
        # appends a random suffix), so key collisions do not occur
        # under normal operation.
        self._connections: dict[str, ServerConnection] = {}  # sandbox_id → ws
        self._ws_to_sandbox: dict[int, str] = {}  # id(ws) → sandbox_id

        # --- Pub/sub (keyed by sandbox_id) ---
        self._channels: dict[str, set[str]] = {}  # channel → set of sandbox_ids
        self._sandbox_channels: dict[str, set[str]] = {}  # sandbox_id → channels

        # --- Pending request tracking (keyed by sandbox_id) ---
        self._pending: dict[str, TrackedPending] = {}  # request_id → tracked
        self._pending_counts: dict[str, int] = {}  # sandbox_id → in-flight count

        self._audit: AuditDB | None = None
        self._server: Server | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the audit DB and start the WebSocket server.

        Expands ``~`` in the audit DB path, runs Alembic migrations,
        starts the background audit writer, and binds the WebSocket
        server to ``config.host:config.port``.

        Raises:
            subprocess.CalledProcessError: If Alembic migrations fail.
            OSError: If the server port is already in use.

        Side effects:
            Populates ``_audit`` and ``_server``.  Spawns the audit
            background flush task.
        """
        from pathlib import Path

        db_path = str(Path(self.config.audit_db_path).expanduser())
        self._audit = AuditDB(db_path, persist_payloads=self.config.persist_payloads)
        await self._audit.open()
        await self._audit.start_background_writer()

        self._server = await websockets.serve(
            self._handler,
            self.config.host,
            self.config.port,
            max_size=self.config.max_message_size,
            process_request=self._process_request,
        )
        logger.info("NMB broker listening on %s:%d", self.config.host, self.config.port)

    async def stop(self) -> None:
        """Shut down the server, cancel pending timeouts, and close the audit DB.

        Safe to call even if ``start()`` was never called (no-op for
        each sub-system that is ``None``).

        Side effects:
            Closes the WebSocket server, cancels all pending timeout
            tasks, flushes and stops the audit background writer, and
            disposes the DB engine.
        """
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for tracked in list(self._pending.values()):
            if tracked.timeout_task and not tracked.timeout_task.done():
                tracked.timeout_task.cancel()
        self._pending.clear()
        if self._audit:
            await self._audit.stop_background_writer()
            await self._audit.close()
        logger.info("NMB broker stopped")

    async def serve_forever(self) -> None:
        """Start the broker and block until the server is closed or interrupted.

        Convenience wrapper: calls ``start()`` then
        ``server.serve_forever()``.  Typically run via
        ``asyncio.run(broker.serve_forever())``.

        Raises:
            AssertionError: If ``start()`` fails to create a server.
        """
        await self.start()
        if self._server is None:
            raise RuntimeError("start() did not create a server")
        await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @staticmethod
    async def _process_request(connection: ServerConnection, request: Request) -> Response | None:
        """Extract ``X-Sandbox-ID`` from the WebSocket upgrade request.

        Called by ``websockets.serve`` before the handshake completes.
        If the header is missing the connection is rejected with HTTP 400.

        The header is set by the **client** at connect time (see
        ``MessageBus.connect``).  In production the OpenShell proxy
        sits between the client and the broker; it forwards the header
        but does **not** inject or override it — the value is
        client-supplied and trusted only because the proxy already
        authenticates the container before allowing the connection
        through.

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
        connection.sandbox_id = sandbox_id  # type: ignore[attr-defined]
        return None

    async def _handler(self, websocket: ServerConnection) -> None:
        """Per-connection message loop: register, dispatch frames, clean up on close.

        Runs for the full lifetime of one WebSocket connection.
        Registers on entry, loops over inbound frames calling
        ``_dispatch``, and unregisters in the ``finally`` block.

        Args:
            websocket: The accepted WebSocket connection (already
                authenticated via ``_process_request``).

        Side effects:
            Calls ``_register`` / ``_unregister`` to manage connection
            state.  Each inbound frame is routed via ``_dispatch``.
        """
        # Stashed on the connection object by _process_request during
        # the HTTP upgrade handshake.
        sandbox_id: str = websocket.sandbox_id  # type: ignore[attr-defined]
        self._register(sandbox_id, websocket)

        try:
            async for raw_frame in websocket:
                if isinstance(raw_frame, bytes):
                    raw_frame = raw_frame.decode()
                await self._dispatch(sandbox_id, websocket, raw_frame)
        except websockets.ConnectionClosed:
            pass  # Normal disconnection — _unregister handles cleanup.
        finally:
            await self._unregister(sandbox_id, websocket)

    def _register(self, sandbox_id: str, ws: ServerConnection) -> None:
        """Register a newly connected sandbox.

        Rejects the connection if ``sandbox_id`` is already registered
        (indicates a UUID collision or a client bug).  Under normal
        operation this never happens since the client appends 8 random
        hex chars to each launch.

        Args:
            sandbox_id: Globally unique sandbox identifier.
            ws: The WebSocket connection for this sandbox.

        Side effects:
            Populates ``_connections`` and ``_ws_to_sandbox``.
            Spawns a ``log_connection`` audit task.  Closes *ws* with
            an error if the sandbox_id is already taken.
        """
        # Collision guard: sandbox_ids are globally unique (client appends
        # 8 random hex chars), so this only fires on a UUID collision or
        # a client bug.  Reject the newcomer; keep the existing connection.
        if sandbox_id in self._connections:
            logger.error(
                "Duplicate sandbox_id rejected: %s (already connected)", sandbox_id,
            )
            async def _reject(w: ServerConnection) -> None:
                try:
                    await w.close()
                except Exception:
                    pass

            asyncio.create_task(_reject(ws))
            return

        self._connections[sandbox_id] = ws
        self._ws_to_sandbox[id(ws)] = sandbox_id
        logger.info("Sandbox connected: %s", sandbox_id)

        # Fire-and-forget: audit the connection event without blocking
        # the handshake.
        if self._audit:
            asyncio.create_task(self._audit.log_connection(sandbox_id))

    async def _unregister(self, sandbox_id: str, ws: ServerConnection) -> None:
        """Unregister a disconnected sandbox and clean up all associated state.

        Only proceeds if *ws* is still the registered connection for
        *sandbox_id*.

        Args:
            sandbox_id: The globally unique sandbox identifier.
            ws: The specific WebSocket connection being closed.

        Side effects:
            Removes the sandbox from all registries, cancels any
            pending request timeout tasks originated by it, broadcasts
            a ``sandbox.shutdown`` system event, and logs the
            disconnection to the audit DB.
        """
        # Stale-connection guard: if _register has already replaced this
        # sandbox_id with a newer WebSocket (shouldn't happen since IDs
        # are unique, but defensive), skip cleanup for the old one.
        if self._connections.get(sandbox_id) is not ws:
            self._ws_to_sandbox.pop(id(ws), None)
            return

        # ── Remove from connection registry ──
        self._connections.pop(sandbox_id, None)
        self._ws_to_sandbox.pop(id(ws), None)

        # ── Clean up channel subscriptions ──
        # Remove this sandbox from every channel it was subscribed to.
        # Delete channels that become empty.
        for ch in list(self._sandbox_channels.get(sandbox_id, set())):
            subs = self._channels.get(ch)
            if subs:
                subs.discard(sandbox_id)
                if not subs:
                    del self._channels[ch]
        self._sandbox_channels.pop(sandbox_id, None)

        # ── Cancel pending requests originated by this sandbox ──
        # Without the requester connected, replies have nowhere to go
        # and timeout tasks would fire uselessly.
        expired = [
            rid
            for rid, t in self._pending.items()
            if t.pending.from_sandbox == sandbox_id
        ]
        for rid in expired:
            tracked = self._pending.pop(rid, None)
            if tracked and tracked.timeout_task and not tracked.timeout_task.done():
                tracked.timeout_task.cancel()
        self._pending_counts.pop(sandbox_id, None)

        # ── Notify other sandboxes ──
        # Subscribers to the "system" channel receive a shutdown event
        # so the orchestrator can react (e.g., reschedule work).
        shutdown_msg = NMBMessage(
            op=Op.DELIVER,
            from_sandbox="system",
            type="sandbox.shutdown",
            timestamp=time.time(),
            payload={"sandbox_id": sandbox_id, "reason": "disconnected"},
        )
        await self._broadcast_system(shutdown_msg)

        # ── Audit ──
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
            sender_id: The sender's ``sandbox_id``.
            ws: The sender's WebSocket connection (for ACK/error replies).
            raw: The raw JSON text frame received from the WebSocket.
        """
        # ── Parse ──
        try:
            msg = NMBMessage.from_json(raw)
        except FrameValidationError as exc:
            # No audit: malformed JSON — no NMBMessage to log.
            await self._send_error(ws, "", exc.code, str(exc))
            return

        # ── Enforce identity ──
        # The broker is the source of truth for sender identity.
        # Overwrite whatever the client put in from_sandbox with the
        # authenticated sandbox_id from the connection handshake.
        msg.from_sandbox = sender_id
        msg.timestamp = time.time()

        # ── Validate required fields for this op ──
        try:
            msg.validate_frame()
        except FrameValidationError as exc:
            # No audit: validation failure is a client protocol error,
            # not a delivery attempt.
            await self._send_error(ws, msg.id, exc.code, str(exc))
            return

        # ── Route to the appropriate handler ──
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
            # No audit: broker-only ops (DELIVER, ACK, ERROR, TIMEOUT)
            # are not valid from a client.
            await self._send_error(
                ws, msg.id, ErrorCode.INVALID_FRAME, f"Client cannot send op={msg.op.value}"
            )
            return

        await handler(sender_id, ws, msg)

    # ------------------------------------------------------------------
    # Op handlers
    # ------------------------------------------------------------------

    async def _handle_send(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a fire-and-forget ``send`` to the target.

        Resolves the target ``sandbox_id`` in ``msg.to_sandbox``.
        Delivers the message, ACKs the sender, and enqueues an audit
        write.  Returns ``TARGET_OFFLINE`` if the target is not
        connected or its WebSocket closes mid-send.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``send`` message.

        Side effects:
            Sends a ``deliver`` frame to the target, an ``ack`` or
            ``error`` frame to the sender, and enqueues an audit write.
        """
        # No audit: guard clause — malformed frame, not a delivery attempt.
        if not msg.to_sandbox:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing to_sandbox")
            return

        # Look up the target's WebSocket by its globally unique sandbox_id.
        target_ws = self._connections.get(msg.to_sandbox)
        if target_ws is None:
            # Target not in _connections — either never connected or
            # already disconnected.  Audit as ERROR so the sender's
            # failed delivery is visible in the audit trail.
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} not connected", audit_msg=msg,
            )
            return

        # Wrap the original message as a DELIVER frame and forward it.
        deliver = self._make_deliver(msg)
        try:
            await target_ws.send(deliver.to_json())
        except websockets.ConnectionClosed:
            # Target's WebSocket closed between the lookup and the send.
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} disconnected", audit_msg=msg,
            )
            return

        # Delivery succeeded — ACK the sender and record in audit.
        await self._send_ack(ws, msg.id, audit_msg=msg)

    async def _handle_request(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a ``request`` and register it for reply correlation and timeout.

        Flow:

        1. Check the per-sandbox pending-request limit.
        2. Look up the target in ``_connections``.
        3. Create a ``PendingRequest`` and spawn a timeout task.
        4. Deliver to the target.
        5. On success: ACK the sender and audit as ``DELIVERED``.
        6. On failure: clean up pending state, send ``TARGET_OFFLINE``,
           and audit as ``ERROR``.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``request`` message.

        Side effects:
            Modifies ``_pending``, ``_pending_counts``.  Spawns a
            ``_timeout_request`` task.  Sends ``deliver``/``ack``/
            ``error`` frames.  Enqueues an audit write.
        """
        # No audit: guard clause — malformed frame.
        if not msg.to_sandbox:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing to_sandbox")
            return

        # ── Pre-flight checks ──
        count = self._pending_counts.get(sender_id, 0)
        if count >= self.config.max_pending_per_sandbox:
            # No audit: rate-limited — message never entered the routing path.
            await self._send_error(ws, msg.id, ErrorCode.RATE_LIMITED, "Too many pending requests")
            return

        target_ws = self._connections.get(msg.to_sandbox)
        if target_ws is None:
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} not connected", audit_msg=msg,
            )
            return

        # ── Register pending state BEFORE delivery ──
        # The timeout task fires after `timeout` seconds and sends a
        # TIMEOUT frame if the request hasn't been replied to by then.
        timeout = msg.timeout or self.config.default_request_timeout
        pending = PendingRequest(request_id=msg.id, from_sandbox=sender_id, timeout=timeout)
        timeout_task = asyncio.create_task(self._timeout_request(msg.id, sender_id, timeout))
        self._pending[msg.id] = TrackedPending(pending=pending, timeout_task=timeout_task)
        self._pending_counts[sender_id] = count + 1

        # ── Deliver ──
        deliver = self._make_deliver(msg)
        try:
            await target_ws.send(deliver.to_json())
        except websockets.ConnectionClosed:
            # Target disconnected mid-send — roll back the pending state
            # we just set up so the timeout task doesn't fire uselessly.
            self._pending.pop(msg.id, None)
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
            self._pending_counts[sender_id] = max(0, self._pending_counts.get(sender_id, 1) - 1)
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} disconnected", audit_msg=msg,
            )
            return
        await self._send_ack(ws, msg.id, audit_msg=msg)

    async def _handle_reply(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Route a ``reply`` back to the original requester.

        Pops the matching ``TrackedPending``, cancels its timeout task,
        decrements the requester's pending count, and delivers the
        reply.  Returns ``INVALID_FRAME`` if no matching pending
        request exists.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``reply`` message (must have ``reply_to``).

        Side effects:
            Modifies ``_pending``, ``_pending_counts``.  Cancels the
            timeout task.  Sends ``deliver``/``error`` frames.
            Enqueues an audit write.
        """
        # No audit: guard clause — malformed frame.
        if not msg.reply_to:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing reply_to")
            return

        # Pop the pending request that this reply correlates to.
        # If it's gone (already replied or timed out), reject.
        tracked = self._pending.pop(msg.reply_to, None)
        if tracked is None:
            # No audit: stale/invalid reply — the original request was
            # already replied to or timed out.
            await self._send_error(
                ws, msg.id, ErrorCode.INVALID_FRAME, f"No pending request with id={msg.reply_to}"
            )
            return

        # The reply arrived in time — cancel the timeout task and
        # free the pending slot so the requester can issue new requests.
        if tracked.timeout_task and not tracked.timeout_task.done():
            tracked.timeout_task.cancel()
        requester_id = tracked.pending.from_sandbox
        self._pending_counts[requester_id] = max(0, self._pending_counts.get(requester_id, 1) - 1)

        # Deliver the reply to the original requester.  No ACK is sent
        # to the replier — replies are silent from the sender's
        # perspective.
        target_ws = self._connections.get(requester_id)
        if target_ws is None:
            # Requester disconnected while waiting — nothing to deliver.
            self._audit_msg(msg, DeliveryStatus.ERROR)
            return

        deliver = self._make_deliver(msg)
        try:
            await target_ws.send(deliver.to_json())
        except websockets.ConnectionClosed:
            self._audit_msg(msg, DeliveryStatus.ERROR)
            return
        self._audit_msg(msg, DeliveryStatus.DELIVERED)

    async def _handle_subscribe(
        self, sender_id: str, ws: ServerConnection, msg: NMBMessage
    ) -> None:
        """Add a sandbox to a pub/sub channel.

        Returns ``CHANNEL_FULL`` if the sandbox has reached the
        per-sandbox channel subscription limit
        (``config.max_channels_per_sandbox``).

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``subscribe`` message.

        Side effects:
            Adds *sender_id* to ``_channels[channel]`` and
            ``_sandbox_channels[sender_id]``.  Sends an ``ack`` or
            ``error`` frame.
        """
        # No audit: subscribe is connection management, not a message delivery.
        if not msg.channel:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing channel")
            return

        # Enforce per-sandbox channel limit to prevent a single sandbox
        # from monopolising broker memory with thousands of subscriptions.
        sandbox_chans = self._sandbox_channels.setdefault(sender_id, set())
        if len(sandbox_chans) >= self.config.max_channels_per_sandbox:
            await self._send_error(
                ws, msg.id or "", ErrorCode.CHANNEL_FULL, "Too many channel subscriptions"
            )
            return

        # Update both sides of the bidirectional mapping:
        #   _channels[channel] → {sandbox_ids}   (used by publish fanout)
        #   _sandbox_channels[sandbox_id] → {channels}  (used by unregister cleanup + limit check)
        self._channels.setdefault(msg.channel, set()).add(sender_id)
        sandbox_chans.add(msg.channel)
        await self._send_ack(ws, msg.id)

    async def _handle_unsubscribe(
        self, sender_id: str, ws: ServerConnection, msg: NMBMessage
    ) -> None:
        """Remove a sandbox from a pub/sub channel.

        Cleans up the channel entry entirely if no subscribers remain.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``unsubscribe`` message.

        Side effects:
            Removes *sender_id* from ``_channels[channel]`` and
            ``_sandbox_channels[sender_id]``.  Sends an ``ack`` frame.
        """
        # No audit: unsubscribe is connection management.
        if not msg.channel:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing channel")
            return

        # Remove from channel → subscribers mapping.
        # Delete the channel entry entirely if this was the last subscriber
        # to avoid unbounded growth of empty sets.
        subs = self._channels.get(msg.channel)
        if subs:
            subs.discard(sender_id)
            if not subs:
                del self._channels[msg.channel]

        # Remove from the reverse mapping (sandbox → channels) so the
        # per-sandbox channel limit stays accurate.
        sandbox_chans = self._sandbox_channels.get(sender_id)
        if sandbox_chans:
            sandbox_chans.discard(msg.channel)

        # No audit: ack is connection management, not a message delivery.
        await self._send_ack(ws, msg.id)

    async def _handle_publish(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Broadcast a ``publish`` message to all channel subscribers.

        Sends concurrently via ``asyncio.gather`` with per-subscriber
        timeouts.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``publish`` message.

        Side effects:
            Sends a ``deliver`` frame to each subscriber, an ``ack``
            frame to the sender, and enqueues an audit write.
        """
        # No audit: guard clause — malformed frame.
        if not msg.channel:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing channel")
            return

        # Serialize once, send to all subscribers concurrently.
        deliver = self._make_deliver(msg)
        frame = deliver.to_json()

        subscribers = self._channels.get(msg.channel, set())
        tasks = []
        for sub_id in list(subscribers):
            if sub_id == sender_id:
                continue  # Don't echo the publisher's own message back.
            sub_ws = self._connections.get(sub_id)
            if sub_ws:
                tasks.append(self._safe_send(sub_ws, frame))

        # Concurrent fanout — one slow subscriber can't block the rest.
        if tasks:
            await asyncio.gather(*tasks)

        await self._send_ack(ws, msg.id, audit_msg=msg)

    async def _handle_stream(self, sender_id: str, ws: ServerConnection, msg: NMBMessage) -> None:
        """Forward a ``stream`` chunk to the target.

        Stream chunks are **not** individually ACKed.  Returns
        ``TARGET_OFFLINE`` if the target is not connected or its
        WebSocket closes mid-send.

        Args:
            sender_id: Sender's ``sandbox_id``.
            ws: Sender's WebSocket connection.
            msg: The validated ``stream`` message.

        Side effects:
            Sends a ``deliver`` frame to the target, or an ``error``
            frame to the sender if the target is offline.
        """
        # No audit: guard clause — malformed frame.
        if not msg.to_sandbox:
            await self._send_error(ws, msg.id, ErrorCode.INVALID_FRAME, "Missing to_sandbox")
            return

        target_ws = self._connections.get(msg.to_sandbox)
        if target_ws is None:
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} not connected", audit_msg=msg,
            )
            return

        deliver = self._make_deliver(msg)
        try:
            await target_ws.send(deliver.to_json())
        except websockets.ConnectionClosed:
            await self._send_error(
                ws, msg.id, ErrorCode.TARGET_OFFLINE,
                f"{msg.to_sandbox} disconnected", audit_msg=msg,
            )

    # ------------------------------------------------------------------
    # Timeout handling
    # ------------------------------------------------------------------

    async def _timeout_request(self, request_id: str, requester_id: str, timeout: float) -> None:
        """Sleep for *timeout* seconds, then send a ``timeout`` frame.

        If the request has already been replied to (popped from
        ``_pending``) by the time this fires, the method is a no-op.

        Args:
            request_id: The original request message ID.
            requester_id: ``sandbox_id`` of the requester.
            timeout: Seconds to wait before firing.

        Side effects:
            Removes the request from ``_pending``, decrements
            ``_pending_counts``, sends a ``timeout`` frame to the
            requester, and enqueues an audit status UPDATE from
            ``delivered`` to ``timeout``.
        """
        await asyncio.sleep(timeout)

        # If _handle_reply already popped this request, the reply beat
        # the timeout — nothing to do.
        tracked = self._pending.pop(request_id, None)
        if tracked is None:
            return

        self._pending_counts[requester_id] = max(0, self._pending_counts.get(requester_id, 1) - 1)

        # Notify the requester that their request timed out.
        ws = self._connections.get(requester_id)
        if ws:
            timeout_msg = NMBMessage(
                op=Op.TIMEOUT,
                id=request_id,
                message=f"No reply within {timeout}s",
            )
            try:
                await ws.send(timeout_msg.to_json())
            except websockets.ConnectionClosed:
                pass

        # Update the existing audit row from DELIVERED → TIMEOUT
        # (the initial log_message recorded it as DELIVERED when the
        # request was first routed).
        self._audit_update_status(request_id, DeliveryStatus.TIMEOUT)

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

    async def _send_ack(
        self,
        ws: ServerConnection,
        msg_id: str,
        *,
        audit_msg: NMBMessage | None = None,
    ) -> None:
        """Send an ACK frame and optionally audit the message as ``DELIVERED``.

        Args:
            ws: The client's WebSocket connection.
            msg_id: The ``id`` of the message being acknowledged.
            audit_msg: If provided, the message is enqueued for audit
                with status ``DELIVERED``.
        """
        ack = NMBMessage(op=Op.ACK, id=msg_id)
        try:
            await ws.send(ack.to_json())
        except websockets.ConnectionClosed:
            pass

        if audit_msg is not None:
            self._audit_msg(audit_msg, DeliveryStatus.DELIVERED)

    async def _send_error(
        self,
        ws: ServerConnection,
        msg_id: str,
        code: ErrorCode,
        message: str,
        *,
        audit_msg: NMBMessage | None = None,
    ) -> None:
        """Send an error frame and optionally audit the message as ``ERROR``.

        Args:
            ws: The client's WebSocket connection.
            msg_id: The ``id`` of the message that caused the error.
            code: Structured error code.
            message: Human-readable error description.
            audit_msg: If provided, the message is enqueued for audit
                with status ``ERROR``.
        """
        err = NMBMessage(op=Op.ERROR, id=msg_id, code=code.value, message=message)
        try:
            await ws.send(err.to_json())
        except websockets.ConnectionClosed:
            pass
        if audit_msg is not None:
            self._audit_msg(audit_msg, DeliveryStatus.ERROR)

    async def _broadcast_system(self, msg: NMBMessage) -> None:
        """Broadcast a system message to all subscribers of the ``system`` channel.

        Uses concurrent sends via ``asyncio.gather`` so a slow
        subscriber cannot delay others.

        Args:
            msg: The system-level message to broadcast (e.g.
                ``sandbox.shutdown``).

        Side effects:
            Sends a frame to every subscriber of the ``system``
            channel.  Slow or closed connections are silently skipped
            via ``_safe_send``.
        """
        frame = msg.to_json()
        tasks = []
        for sub_id in list(self._channels.get("system", set())):
            ws = self._connections.get(sub_id)
            if ws:
                tasks.append(self._safe_send(ws, frame))
        if tasks:
            await asyncio.gather(*tasks)

    @staticmethod
    async def _safe_send(ws: ServerConnection, frame: str) -> None:
        """Send a frame to a subscriber, silently handling slow/closed connections.

        Args:
            ws: Target WebSocket.
            frame: Serialized JSON frame.
        """
        try:
            await asyncio.wait_for(ws.send(frame), timeout=DEFAULT_NMB_SUBSCRIBER_SEND_TIMEOUT)
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass

    def _audit_msg(self, msg: NMBMessage, status: DeliveryStatus) -> None:
        """Enqueue a message for background batch audit writing.

        Non-blocking — adds the item to the audit DB's write queue.

        Args:
            msg: The message to audit.
            status: Delivery outcome (delivered, error, or timeout).
        """
        if self._audit:
            self._audit.enqueue_message(msg, status)

    def _audit_update_status(self, msg_id: str, status: DeliveryStatus) -> None:
        """Enqueue a delivery-status UPDATE for a previously audited message.

        Args:
            msg_id: The message's primary key in the audit table.
            status: New delivery status.
        """
        if self._audit:
            self._audit.enqueue_status_update(msg_id, status)

    # ------------------------------------------------------------------
    # Health / stats
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return broker health statistics.

        Returns:
            A dict with connected sandbox IDs, pending requests,
            and channel subscriptions.
        """
        return {
            "connected_sandboxes": list(self._connections.keys()),
            "num_connections": len(self._connections),
            "num_pending_requests": len(self._pending),
            "channels": {ch: list(subs) for ch, subs in self._channels.items()},
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
    parser.add_argument("--port", type=int, default=DEFAULT_NMB_PORT, help="Bind port")
    parser.add_argument(
        "--audit-db",
        default=DEFAULT_NMB_AUDIT_DB_PATH,
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
        args: Parsed argparse namespace.
        db_path: Expanded filesystem path to the audit database.
    """
    import json as json_mod

    audit = AuditDB(db_path)
    await audit.open()
    try:
        if args.health:
            stats = await audit.query(
                "SELECT COUNT(*) AS total_messages, "
                "COUNT(DISTINCT from_sandbox) AS unique_senders, "
                "MAX(timestamp) AS last_message_at "
                "FROM messages"
            )
            conns = await audit.query(
                "SELECT sandbox_id, connected_at, disconnected_at, disconnect_reason "
                "FROM connections ORDER BY connected_at DESC LIMIT 20"
            )
            health = {
                **(stats[0] if stats else {}),
                "recent_connections": conns,
            }
            print(json_mod.dumps(health, default=str, indent=2))
        elif args.query:
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
    finally:
        await audit.close()


if __name__ == "__main__":
    _main()
