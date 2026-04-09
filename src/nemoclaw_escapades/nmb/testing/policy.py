"""Policy enforcement layer for NMB integration testing.

In production, OpenShell network policies control which sandboxes can
reach the broker (NMB design doc §8).  For integration tests,
:class:`PolicyBroker` simulates this by enforcing per-sandbox rules at
the broker routing level — blocking unauthorised egress, ingress,
channel access, and op types before messages reach the standard
handlers.

Policy dimensions:

- **Connection** — ``can_connect`` gates the WebSocket handshake.
- **Egress** — ``allowed_egress_targets`` restricts which sandbox_ids
  a sender may target with ``send`` / ``request`` / ``stream``.
- **Ingress** — ``allowed_ingress_sources`` restricts which senders
  may deliver to a given sandbox.
- **Channels** — ``allowed_channels`` restricts ``subscribe`` /
  ``publish`` with wildcard support (``progress.*``).
- **Ops** — ``allowed_ops`` restricts which operation types a sandbox
  may use.

A ``None`` value on any rule field means *unrestricted*.  An empty set
means *nothing allowed*.

The :class:`PolicyBroker` is a subclass of :class:`NMBBroker` that
overrides ``_process_request`` (connection gating) and ``_dispatch``
(per-message policy check inserted before routing).  All standard
broker functionality (routing, audit, timeouts) is inherited
unchanged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request, Response

from nemoclaw_escapades.config import BrokerConfig
from nemoclaw_escapades.nmb.broker import NMBBroker
from nemoclaw_escapades.nmb.models import (
    ErrorCode,
    FrameValidationError,
    NMBMessage,
    Op,
)

logger = logging.getLogger("nmb.testing.policy")


# ---------------------------------------------------------------------------
# Sandbox policy declaration
# ---------------------------------------------------------------------------


@dataclass
class SandboxPolicy:
    """Per-sandbox policy for the test environment.

    Each field defaults to ``None`` (unrestricted).  Set a field to
    an explicit ``set()`` to deny everything of that type, or to a
    populated set to allow only those values.

    Attributes:
        sandbox_id: The sandbox display name this policy applies to.
            Re-keyed to the globally unique ID by the harness after
            ``MessageBus`` construction.
        can_connect: Whether this sandbox may connect at all.
            ``False`` causes the broker to reject the WebSocket
            handshake with HTTP 403.
        allowed_egress_targets: Set of sandbox IDs this sandbox may
            send to (``send`` / ``request`` / ``stream``).
            ``None`` = unrestricted.
        allowed_ingress_sources: Set of sandbox IDs allowed to
            deliver messages *to* this sandbox.
            ``None`` = unrestricted.
        allowed_channels: Channel name patterns this sandbox may
            subscribe to or publish on.  Supports exact names
            (``"progress.coding-1"``), wildcard suffixes
            (``"progress.*"``), and the global wildcard (``"*"``).
            ``None`` = unrestricted.
        allowed_ops: Set of ``Op`` types this sandbox may use.
            ``None`` = unrestricted.
    """

    sandbox_id: str
    can_connect: bool = True
    allowed_egress_targets: set[str] | None = None
    allowed_ingress_sources: set[str] | None = None
    allowed_channels: set[str] | None = None
    allowed_ops: set[Op] | None = None


# ---------------------------------------------------------------------------
# Channel pattern matching
# ---------------------------------------------------------------------------


def _channel_matches(channel: str, patterns: set[str]) -> bool:
    """Return ``True`` if *channel* matches any pattern in *patterns*.

    Supported patterns:

    - ``"progress.coding-1"`` — exact match
    - ``"progress.*"`` — wildcard suffix (matches ``progress.coding-1``
      but not ``progressx``)
    - ``"*"`` — global wildcard (matches everything)

    Args:
        channel: The channel name to test.
        patterns: Set of allowed patterns.

    Returns:
        ``True`` if at least one pattern matches.
    """
    for pattern in patterns:
        if pattern == "*" or pattern == channel:
            return True
        # "progress.*" matches "progress.coding-1" but not "progressx"
        # because we compare against "progress." (pattern minus the "*").
        if pattern.endswith(".*") and channel.startswith(pattern[:-1]):
            return True
    return False


# ---------------------------------------------------------------------------
# Policy-enforcing broker
# ---------------------------------------------------------------------------


class PolicyBroker(NMBBroker):
    """NMB broker extended with per-sandbox policy enforcement.

    Designed for integration testing — simulates the OpenShell proxy's
    role of gating connectivity and the network-policy engine's role
    of restricting traffic patterns.

    Enforcement points:

    1. **WebSocket handshake** (``_process_request``) — rejects
       sandboxes whose ``can_connect`` is ``False`` with HTTP 403.
    2. **Dispatch** (``_dispatch`` → ``_enforce_policy``) — checks
       ``allowed_ops``, egress/ingress rules, and channel rules
       before routing to the standard handlers.  Denied messages
       get a ``POLICY_DENIED`` error frame.

    Policies are stored in ``_policies`` keyed by sandbox_id.  The
    harness calls :meth:`rekey_policy` after each ``MessageBus`` is
    constructed to replace display-name keys with globally unique IDs.

    Attributes:
        _policies: ``sandbox_id`` → ``SandboxPolicy`` lookup table.
    """

    def __init__(
        self,
        config: BrokerConfig | None = None,
        policies: list[SandboxPolicy] | None = None,
    ) -> None:
        """Create a policy-enforcing broker.

        Args:
            config: Broker configuration (passed to ``NMBBroker``).
            policies: Initial policy declarations.  Keyed by
                ``policy.sandbox_id`` (typically a display name at
                this point; re-keyed later by the harness).
        """
        super().__init__(config)
        # Build the lookup table from the policy list.
        self._policies: dict[str, SandboxPolicy] = {p.sandbox_id: p for p in (policies or [])}

    def add_policy(self, policy: SandboxPolicy) -> None:
        """Register a new policy at runtime.

        Use this for sandboxes added after the broker was constructed
        (e.g. via ``IntegrationHarness.add_sandbox``).  The policy is
        indexed by ``policy.sandbox_id`` — call :meth:`rekey_policy`
        afterwards to translate the display name to the unique ID.

        Args:
            policy: The sandbox policy to register.
        """
        self._policies[policy.sandbox_id] = policy

    def rekey_policy(self, old_sandbox_id: str, new_sandbox_id: str) -> None:
        """Re-index a policy under a new (unique) ``sandbox_id``.

        Called by the integration harness after a ``MessageBus``
        generates its globally unique ``sandbox_id``.  Updates the
        policy's ``sandbox_id`` attribute and the internal dict key
        so that ``_process_request`` and ``_enforce_policy`` find the
        policy under the ID the client actually sends.

        Also patches any egress/ingress rules in **other** policies
        that reference the old name so that cross-sandbox allow-lists
        still match after the rename.

        Args:
            old_sandbox_id: The display name the policy was originally
                registered under.
            new_sandbox_id: The globally unique ``sandbox_id`` that
                the client will use.
        """
        # Move the policy to the new key.
        policy = self._policies.pop(old_sandbox_id, None)
        if policy is None:
            return
        policy.sandbox_id = new_sandbox_id
        self._policies[new_sandbox_id] = policy

        # Patch cross-references in other policies so allow-lists that
        # mentioned the old display name now reference the unique ID.
        for p in self._policies.values():
            if p.allowed_egress_targets is not None and old_sandbox_id in p.allowed_egress_targets:
                p.allowed_egress_targets.discard(old_sandbox_id)
                p.allowed_egress_targets.add(new_sandbox_id)
            if (
                p.allowed_ingress_sources is not None
                and old_sandbox_id in p.allowed_ingress_sources
            ):
                p.allowed_ingress_sources.discard(old_sandbox_id)
                p.allowed_ingress_sources.add(new_sandbox_id)

    # -- Connection policy -------------------------------------------------
    # Overrides the @staticmethod on the parent.  When ``start()``
    # evaluates ``process_request=self._process_request``, Python
    # resolves this to a bound method, which websockets invokes with
    # ``(connection, request)`` just like the static variant.

    async def _process_request(  # type: ignore[override]
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Gate the WebSocket handshake based on ``can_connect``.

        Extends the parent's header extraction with a policy check:
        if a policy exists for this sandbox_id and ``can_connect``
        is ``False``, the connection is rejected with HTTP 403.

        Args:
            connection: The nascent server connection.
            request: The HTTP upgrade request.

        Returns:
            ``None`` to proceed, or an HTTP error ``Response``.
        """
        sandbox_id = request.headers.get("X-Sandbox-ID")
        if not sandbox_id:
            return connection.respond(400, "Missing X-Sandbox-ID header\n")

        # Stash the identity for _handler to read.
        connection.sandbox_id = sandbox_id  # type: ignore[attr-defined]

        # Check the connection-level policy gate.
        policy = self._policies.get(sandbox_id)
        if policy is not None and not policy.can_connect:
            logger.info("Connection denied by policy: %s", sandbox_id)
            return connection.respond(403, "Connection denied by policy\n")

        return None

    # -- Routing policy (overrides _dispatch) ------------------------------

    async def _dispatch(self, sender_id: str, ws: ServerConnection, raw: str) -> None:
        """Parse, validate, enforce policy, then route.

        Identical to the parent's ``_dispatch`` except for the
        ``_enforce_policy`` call inserted between validation and
        handler dispatch.  If the policy check fails, the message
        is dropped and a ``POLICY_DENIED`` error is sent to the
        client.

        Args:
            sender_id: The sender's ``sandbox_id``.
            ws: The sender's WebSocket connection.
            raw: The raw JSON text frame.
        """
        # ── Parse ──
        try:
            msg = NMBMessage.from_json(raw)
        except FrameValidationError as exc:
            await self._send_error(ws, "", exc.code, str(exc))
            return

        # ── Enforce identity ──
        msg.from_sandbox = sender_id
        msg.timestamp = time.time()

        # ── Validate required fields ──
        try:
            msg.validate_frame()
        except FrameValidationError as exc:
            await self._send_error(ws, msg.id, exc.code, str(exc))
            return

        # ── Policy gate (the only difference from the parent) ──
        if not await self._enforce_policy(sender_id, ws, msg):
            return

        # ── Route to handler ──
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
                ws,
                msg.id,
                ErrorCode.INVALID_FRAME,
                f"Client cannot send op={msg.op.value}",
            )
            return

        await handler(sender_id, ws, msg)

    # -- Policy enforcement ------------------------------------------------

    async def _enforce_policy(
        self,
        sender_id: str,
        ws: ServerConnection,
        msg: NMBMessage,
    ) -> bool:
        """Check all policy dimensions for a single message.

        Returns ``True`` if the message is allowed to proceed to
        routing.  Returns ``False`` if denied — in which case a
        ``POLICY_DENIED`` error frame has already been sent to the
        client.

        Checks are evaluated in order:

        1. **Op restriction** — is this operation type allowed?
        2. **Egress restriction** — may the sender target this
           sandbox_id?
        3. **Ingress restriction** — does the target sandbox accept
           messages from this sender?
        4. **Channel restriction** — does the channel name match
           the sender's allowed patterns?

        Args:
            sender_id: The sender's ``sandbox_id``.
            ws: The sender's WebSocket (for error replies).
            msg: The validated message to check.

        Returns:
            ``True`` if allowed, ``False`` if denied.
        """
        policy = self._policies.get(sender_id)

        # ── 1. Op restriction ──
        if policy and policy.allowed_ops is not None:
            if msg.op not in policy.allowed_ops:
                await self._send_error(
                    ws,
                    msg.id,
                    ErrorCode.POLICY_DENIED,
                    f"Op '{msg.op.value}' not allowed for sandbox '{sender_id}'",
                )
                return False

        # ── 2 & 3. Egress / ingress for targeted ops ──
        if msg.op in (Op.SEND, Op.REQUEST, Op.STREAM) and msg.to_sandbox:
            target_id = msg.to_sandbox

            # 2. Does the sender's policy allow egress to this target?
            if policy and policy.allowed_egress_targets is not None:
                if target_id not in policy.allowed_egress_targets:
                    await self._send_error(
                        ws,
                        msg.id,
                        ErrorCode.POLICY_DENIED,
                        f"Egress to '{target_id}' denied for sandbox '{sender_id}'",
                    )
                    return False

            # 3. Does the target's policy allow ingress from this sender?
            target_policy = self._policies.get(target_id)
            if target_policy and target_policy.allowed_ingress_sources is not None:
                if sender_id not in target_policy.allowed_ingress_sources:
                    await self._send_error(
                        ws,
                        msg.id,
                        ErrorCode.POLICY_DENIED,
                        f"Ingress from '{sender_id}' denied for sandbox '{target_id}'",
                    )
                    return False

        # ── 4. Channel restriction ──
        # Unsubscribe is always allowed (tearing down is never harmful).
        if msg.op in (Op.SUBSCRIBE, Op.PUBLISH) and msg.channel:
            if policy and policy.allowed_channels is not None:
                if not _channel_matches(msg.channel, policy.allowed_channels):
                    await self._send_error(
                        ws,
                        msg.id,
                        ErrorCode.POLICY_DENIED,
                        f"Channel '{msg.channel}' not allowed for sandbox '{sender_id}'",
                    )
                    return False

        # All checks passed.
        return True
