"""Integration tests: sandbox lifecycle (connect, disconnect, reconnect)."""

from __future__ import annotations

import asyncio

import pytest

from nemoclaw_escapades.nmb.models import NMBMessage
from nemoclaw_escapades.nmb.testing import IntegrationHarness, SandboxPolicy

pytestmark = pytest.mark.integration


class TestSandboxConnect:
    """Connection and registration tests."""

    async def test_sandboxes_appear_in_health(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        health = two_sandbox_harness.broker.health()
        assert health["num_connections"] == 2
        connected = health["connected_sandboxes"]
        assert any(s.startswith("orchestrator-") for s in connected)
        assert any(s.startswith("coding-1-") for s in connected)

    async def test_add_sandbox_at_runtime(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        health_before = two_sandbox_harness.broker.health()
        assert health_before["num_connections"] == 2

        await two_sandbox_harness.add_sandbox(
            SandboxPolicy(sandbox_id="coding-2")
        )

        health_after = two_sandbox_harness.broker.health()
        assert health_after["num_connections"] == 3
        assert any(s.startswith("coding-2-") for s in health_after["connected_sandboxes"])


class TestSandboxDisconnect:
    """Disconnect and cleanup tests."""

    async def test_disconnect_unregisters_sandbox(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        assert two_sandbox_harness.broker.health()["num_connections"] == 2

        await two_sandbox_harness.remove_sandbox("coding-1")

        health = two_sandbox_harness.broker.health()
        assert health["num_connections"] == 1
        assert not any(s.startswith("coding-1-") for s in health["connected_sandboxes"])

    async def test_system_shutdown_notification(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        """When coding-1 disconnects, subscribers of the ``system``
        channel receive ``sandbox.shutdown``.

        The broker broadcasts to system-channel subscribers, but the
        deliver frame carries no ``channel`` field, so the client
        routes it to the listen queue (→ ``_collect_loop`` → ``received``).
        We must subscribe first so the broker includes us.
        """
        orch = two_sandbox_harness["orchestrator"]
        coding_bus_id = two_sandbox_harness["coding-1"].bus.sandbox_id

        # Subscribe to "system" so the broker adds us to the fanout list.
        async def _sub() -> None:
            async for _ in orch.subscribe("system"):
                break

        sub_task = asyncio.create_task(_sub())
        await asyncio.sleep(0.15)

        await two_sandbox_harness.remove_sandbox("coding-1")

        shutdown = await orch.wait_for_message("sandbox.shutdown", timeout=5.0)

        assert shutdown.payload is not None
        assert shutdown.payload["sandbox_id"] == coding_bus_id
        assert shutdown.payload["reason"] == "disconnected"

        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass


class TestSandboxReconnect:
    """Reconnection behaviour."""

    async def test_reconnect_after_disconnect(
        self, harness: IntegrationHarness
    ) -> None:
        await harness.start(
            [
                SandboxPolicy(sandbox_id="orchestrator"),
                SandboxPolicy(sandbox_id="worker"),
            ]
        )

        # Remove and re-add the worker
        await harness.remove_sandbox("worker")
        health = harness.broker.health()
        assert health["num_connections"] == 1

        await harness.add_sandbox(SandboxPolicy(sandbox_id="worker"))
        health = harness.broker.health()
        assert health["num_connections"] == 2

        # Verify the new connection works
        await harness["orchestrator"].send("worker", "ping", {"after": "reconnect"})
        msg = await harness["worker"].wait_for_message("ping")
        assert msg.payload == {"after": "reconnect"}

    async def test_audit_records_connection_history(
        self, harness: IntegrationHarness
    ) -> None:
        await harness.start(
            [SandboxPolicy(sandbox_id="audited")]
        )

        unique_id = harness["audited"].bus.sandbox_id

        # Disconnect
        await harness.remove_sandbox("audited")
        await asyncio.sleep(0.3)

        assert harness.broker._audit is not None
        rows = await harness.broker._audit.query(
            "SELECT sandbox_id, disconnect_reason "
            "FROM connections WHERE sandbox_id = :sid",
            {"sid": unique_id},
        )
        assert len(rows) >= 1
        assert rows[0]["disconnect_reason"] == "disconnected"
