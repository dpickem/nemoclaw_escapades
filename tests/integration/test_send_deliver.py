"""Integration tests: point-to-point send / deliver between sandboxes."""

from __future__ import annotations

import pytest

from nemoclaw_escapades.nmb.client import NMBConnectionError
from nemoclaw_escapades.nmb.testing import IntegrationHarness, SandboxPolicy

pytestmark = pytest.mark.integration


class TestSendDeliver:
    """Tests for Op.SEND → Op.DELIVER with ACK tracking."""

    async def test_orchestrator_sends_task_to_worker(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        await orch.send("coding-1", "task.assign", {"prompt": "implement feature X"})
        msg = await worker.wait_for_message("task.assign")

        assert msg.type == "task.assign"
        assert msg.payload == {"prompt": "implement feature X"}
        assert msg.from_sandbox.startswith("orchestrator-")

    async def test_worker_sends_result_to_orchestrator(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        await worker.send("orchestrator", "task.complete", {"diff": "--- a/..."})
        msg = await orch.wait_for_message("task.complete")

        assert msg.type == "task.complete"
        assert msg.payload == {"diff": "--- a/..."}
        assert msg.from_sandbox.startswith("coding-1-")

    async def test_bidirectional_exchange(self, two_sandbox_harness: IntegrationHarness) -> None:
        """Full round-trip: assign → progress → complete."""
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        await orch.send("coding-1", "task.assign", {"step": 1})
        await worker.wait_for_message("task.assign")

        await worker.send("orchestrator", "task.progress", {"pct": 50})
        await orch.wait_for_message("task.progress")

        await worker.send("orchestrator", "task.complete", {"result": "done"})
        msg = await orch.wait_for_message("task.complete")
        assert msg.payload == {"result": "done"}

    async def test_send_to_offline_allowed_target(self, harness: IntegrationHarness) -> None:
        """Sending to an allowed but offline target returns TARGET_OFFLINE."""
        await harness.start(
            [
                SandboxPolicy(
                    sandbox_id="sender",
                    allowed_egress_targets={"offline-peer"},
                ),
            ]
        )
        with pytest.raises(NMBConnectionError, match="TARGET_OFFLINE"):
            await harness["sender"].send("offline-peer", "ping", {})

    async def test_multiple_messages_arrive_in_order(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        for i in range(5):
            await orch.send("coding-1", "task.assign", {"seq": i})

        import asyncio

        await asyncio.sleep(0.3)

        assigns = worker.messages_of_type("task.assign")
        assert len(assigns) == 5
        for i, msg in enumerate(assigns):
            assert msg.payload == {"seq": i}
