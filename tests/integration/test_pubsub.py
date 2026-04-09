"""Integration tests: pub/sub channels between sandboxes."""

from __future__ import annotations

import asyncio

import pytest

from nemoclaw_escapades.nmb.models import NMBMessage
from nemoclaw_escapades.nmb.testing import IntegrationHarness

pytestmark = pytest.mark.integration


class TestPubSub:
    """Tests for subscribe / publish / deliver via channels."""

    async def test_worker_publishes_progress_to_orchestrator(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        received: list[NMBMessage] = []

        async def subscriber() -> None:
            async for msg in orch.subscribe("progress.coding-1"):
                received.append(msg)
                if len(received) >= 2:
                    return

        sub_task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.1)

        await worker.publish("progress.coding-1", "task.progress", {"pct": 25})
        await worker.publish("progress.coding-1", "task.progress", {"pct": 75})

        await asyncio.wait_for(sub_task, timeout=5.0)

        assert len(received) == 2
        assert received[0].payload == {"pct": 25}
        assert received[1].payload == {"pct": 75}

    async def test_multiple_subscribers_receive_same_message(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """Both orchestrator and review-1 subscribe to system channel."""
        orch = three_sandbox_harness["orchestrator"]
        reviewer = three_sandbox_harness["review-1"]
        coder = three_sandbox_harness["coding-1"]

        orch_received: list[NMBMessage] = []
        rev_received: list[NMBMessage] = []

        async def orch_sub() -> None:
            async for msg in orch.subscribe("system"):
                orch_received.append(msg)
                return

        async def rev_sub() -> None:
            async for msg in reviewer.subscribe("system"):
                rev_received.append(msg)
                return

        orch_task = asyncio.create_task(orch_sub())
        rev_task = asyncio.create_task(rev_sub())
        await asyncio.sleep(0.1)

        await coder.publish("system", "heartbeat", {"ts": 123})

        await asyncio.wait_for(orch_task, timeout=5.0)
        await asyncio.wait_for(rev_task, timeout=5.0)

        assert len(orch_received) == 1
        assert len(rev_received) == 1
        assert orch_received[0].payload == {"ts": 123}
        assert rev_received[0].payload == {"ts": 123}

    async def test_publisher_does_not_receive_own_message(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        worker = two_sandbox_harness["coding-1"]

        received: list[NMBMessage] = []

        async def sub() -> None:
            async for msg in worker.subscribe("progress.coding-1"):
                received.append(msg)
                return

        sub_task = asyncio.create_task(sub())
        await asyncio.sleep(0.1)

        await worker.publish("progress.coding-1", "task.progress", {"self": True})

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub_task, timeout=0.5)

        assert len(received) == 0

    async def test_unsubscribe_stops_delivery(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        received: list[NMBMessage] = []

        async def sub_one() -> None:
            async for msg in orch.subscribe("progress.coding-1"):
                received.append(msg)
                return  # unsubscribes on exit

        sub_task = asyncio.create_task(sub_one())
        await asyncio.sleep(0.1)

        await worker.publish("progress.coding-1", "task.progress", {"pct": 10})
        await asyncio.wait_for(sub_task, timeout=5.0)
        assert len(received) == 1

        await asyncio.sleep(0.1)
        await worker.publish("progress.coding-1", "task.progress", {"pct": 90})
        await asyncio.sleep(0.2)
        assert len(received) == 1  # second message not received
