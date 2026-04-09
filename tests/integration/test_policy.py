"""Integration tests: policy enforcement (egress, ingress, channel, op, connection)."""

from __future__ import annotations

import pytest

from nemoclaw_escapades.nmb.client import MessageBus, NMBConnectionError
from nemoclaw_escapades.nmb.models import Op
from nemoclaw_escapades.nmb.testing import IntegrationHarness, SandboxPolicy

pytestmark = pytest.mark.integration


class TestEgressPolicy:
    """Egress restrictions: what a sandbox is allowed to send to."""

    async def test_egress_to_allowed_target_succeeds(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        await orch.send("coding-1", "task.assign", {"ok": True})
        msg = await worker.wait_for_message("task.assign")
        assert msg.payload == {"ok": True}

    async def test_egress_to_blocked_target_denied(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """coding-1 tries to send directly to review-1 — blocked by egress."""
        coder = three_sandbox_harness["coding-1"]

        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            await coder.send("review-1", "task.assign", {"sneaky": True})

    async def test_egress_to_unknown_target_denied(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """Sending to a name not in allowed_egress_targets is denied."""
        orch = two_sandbox_harness["orchestrator"]

        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            await orch.send("nobody", "ping", {})


class TestIngressPolicy:
    """Ingress restrictions: who is allowed to deliver to a sandbox."""

    async def test_ingress_from_allowed_source(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        await worker.send("orchestrator", "task.complete", {"ok": True})
        msg = await orch.wait_for_message("task.complete")
        assert msg.payload == {"ok": True}

    async def test_ingress_from_blocked_source(
        self, harness: IntegrationHarness
    ) -> None:
        """review-1 has egress to orch, but orch blocks ingress from review-1."""
        await harness.start(
            [
                SandboxPolicy(
                    sandbox_id="orch",
                    allowed_ingress_sources={"coding-1"},  # review-1 blocked
                ),
                SandboxPolicy(
                    sandbox_id="coding-1",
                    allowed_egress_targets={"orch"},
                ),
                SandboxPolicy(
                    sandbox_id="review-1",
                    allowed_egress_targets={"orch"},
                ),
            ]
        )
        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            await harness["review-1"].send("orch", "task.complete", {"denied": True})


class TestChannelPolicy:
    """Channel restrictions: which pub/sub channels a sandbox may use."""

    async def test_subscribe_to_allowed_channel(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """coding-1 has progress.coding-1 in its allowed_channels."""
        worker = two_sandbox_harness["coding-1"]
        # subscribe ACKs without error — no exception means success
        import asyncio

        async def quick_sub() -> None:
            async for _ in worker.subscribe("progress.coding-1"):
                return

        task = asyncio.create_task(quick_sub())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_subscribe_to_blocked_channel_denied(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """coding-1 may not subscribe to progress.review-1."""
        worker = two_sandbox_harness["coding-1"]

        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            async for _ in worker.subscribe("progress.review-1"):
                break

    async def test_wildcard_channel_match(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """orchestrator has progress.* — should match progress.coding-1."""
        import asyncio

        orch = two_sandbox_harness["orchestrator"]

        async def sub() -> None:
            async for _ in orch.subscribe("progress.coding-1"):
                return

        task = asyncio.create_task(sub())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_publish_to_blocked_channel_denied(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """coding-1 tries to publish to progress.review-1 — blocked."""
        worker = two_sandbox_harness["coding-1"]

        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            await worker.publish("progress.review-1", "sneaky", {})


class TestOpPolicy:
    """Op restrictions: which operations a sandbox may use."""

    async def test_allowed_op_succeeds(
        self, harness: IntegrationHarness
    ) -> None:
        await harness.start(
            [
                SandboxPolicy(
                    sandbox_id="sender",
                    allowed_ops={Op.SEND, Op.SUBSCRIBE, Op.UNSUBSCRIBE, Op.PUBLISH},
                ),
                SandboxPolicy(sandbox_id="target"),
            ]
        )
        await harness["sender"].send("target", "ping", {})
        msg = await harness["target"].wait_for_message("ping")
        assert msg.payload == {}

    async def test_blocked_op_denied(
        self, harness: IntegrationHarness
    ) -> None:
        """Sandbox restricted to SEND cannot use REQUEST."""
        await harness.start(
            [
                SandboxPolicy(
                    sandbox_id="limited",
                    allowed_ops={Op.SEND},
                ),
                SandboxPolicy(sandbox_id="target"),
            ]
        )
        with pytest.raises(NMBConnectionError, match="POLICY_DENIED"):
            await harness["limited"].request("target", "req", {}, timeout=1.0)


class TestConnectionPolicy:
    """Connection-level policy: can_connect."""

    async def test_allowed_sandbox_connects(
        self, harness: IntegrationHarness
    ) -> None:
        await harness.start(
            [SandboxPolicy(sandbox_id="allowed")]
        )
        health = harness.broker.health()
        assert health["num_connections"] == 1

    async def test_blocked_sandbox_cannot_connect(
        self, harness: IntegrationHarness
    ) -> None:
        """A sandbox with can_connect=False is rejected at handshake."""
        await harness.start(
            [
                SandboxPolicy(sandbox_id="good"),
                SandboxPolicy(sandbox_id="blocked", can_connect=False),
            ]
        )
        health = harness.broker.health()
        assert health["num_connections"] == 1  # only "good" connected

        # Connect directly via websockets using the rekeyed sandbox_id
        # so the broker's _process_request finds the can_connect=False policy.
        import websockets

        blocked_unique = harness._resolve("blocked")
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            await websockets.connect(
                harness.broker_url,
                additional_headers={"X-Sandbox-ID": blocked_unique},
            )
        assert exc_info.value.response.status_code == 403
