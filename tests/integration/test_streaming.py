"""Integration tests: ordered streaming between sandboxes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nemoclaw_escapades.nmb.testing import IntegrationHarness

pytestmark = pytest.mark.integration


class TestStreaming:
    """Tests for Op.STREAM → Op.DELIVER with sequence ordering."""

    async def test_worker_streams_chunks_to_orchestrator(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        async def chunk_gen() -> AsyncIterator[dict[str, object]]:
            for i in range(3):
                yield {"chunk": f"part-{i}", "index": i}

        await worker.stream("orchestrator", "code.output", chunk_gen())
        await asyncio.sleep(0.3)

        stream_msgs = [m for m in orch.received if m.stream_id is not None]
        data_chunks = [m for m in stream_msgs if not m.done]
        done_chunks = [m for m in stream_msgs if m.done]

        assert len(data_chunks) == 3
        assert len(done_chunks) == 1
        assert data_chunks[0].seq == 0
        assert data_chunks[1].seq == 1
        assert data_chunks[2].seq == 2
        assert data_chunks[0].payload == {"chunk": "part-0", "index": 0}

    async def test_stream_preserves_ordering(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        async def many_chunks() -> AsyncIterator[dict[str, object]]:
            for i in range(20):
                yield {"n": i}

        await worker.stream("orchestrator", "big.output", many_chunks())
        await asyncio.sleep(0.5)

        stream_msgs = [m for m in orch.received if m.stream_id is not None]
        data_chunks = [m for m in stream_msgs if not m.done]

        assert len(data_chunks) == 20
        for i, msg in enumerate(data_chunks):
            assert msg.seq == i
            assert msg.payload == {"n": i}

    async def test_stream_done_has_empty_payload(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        async def single_chunk() -> AsyncIterator[dict[str, object]]:
            yield {"data": "only"}

        await worker.stream("orchestrator", "short.stream", single_chunk())
        await asyncio.sleep(0.3)

        stream_msgs = [m for m in orch.received if m.stream_id is not None]
        done = [m for m in stream_msgs if m.done]

        assert len(done) == 1
        assert done[0].payload == {}

    async def test_stream_all_chunks_share_stream_id(
        self, two_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = two_sandbox_harness["orchestrator"]
        worker = two_sandbox_harness["coding-1"]

        async def chunks() -> AsyncIterator[dict[str, object]]:
            for i in range(3):
                yield {"i": i}

        await worker.stream("orchestrator", "coherent.stream", chunks())
        await asyncio.sleep(0.3)

        stream_msgs = [m for m in orch.received if m.stream_id is not None]
        stream_ids = {m.stream_id for m in stream_msgs}
        assert len(stream_ids) == 1  # all chunks share one stream_id
