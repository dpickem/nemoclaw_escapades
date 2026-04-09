"""Integration tests: request / reply between sandboxes."""

from __future__ import annotations

import asyncio

import pytest

from nemoclaw_escapades.nmb.testing import IntegrationHarness, SandboxPolicy

pytestmark = pytest.mark.integration


class TestRequestReply:
    """Tests for Op.REQUEST → Op.DELIVER → Op.REPLY → Op.DELIVER."""

    async def test_orchestrator_requests_review(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = three_sandbox_harness["orchestrator"]
        reviewer = three_sandbox_harness["review-1"]

        async def reviewer_responds() -> None:
            msg = await reviewer.wait_for_message("review.request")
            await reviewer.reply(msg, "review.feedback", {"verdict": "approve"})

        review_task = asyncio.create_task(reviewer_responds())

        reply = await orch.request("review-1", "review.request", {"diff": "..."}, timeout=5.0)

        assert reply.type == "review.feedback"
        assert reply.payload == {"verdict": "approve"}
        await review_task

    async def test_request_reply_between_orchestrator_and_coder(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]

        async def coder_responds() -> None:
            msg = await coder.wait_for_message("task.assign")
            await coder.reply(msg, "task.complete", {"diff": "fixed"})

        coder_task = asyncio.create_task(coder_responds())

        reply = await orch.request("coding-1", "task.assign", {"prompt": "fix bug"}, timeout=5.0)

        assert reply.type == "task.complete"
        assert reply.payload == {"diff": "fixed"}
        await coder_task

    async def test_request_timeout_fires(self, harness: IntegrationHarness) -> None:
        """Broker fires a timeout frame when no reply arrives."""
        await harness.start(
            [
                SandboxPolicy(sandbox_id="requester"),
                SandboxPolicy(sandbox_id="silent"),
            ],
        )
        with pytest.raises(TimeoutError):
            await harness["requester"].request("silent", "slow.op", {}, timeout=0.5)

    async def test_concurrent_requests_to_different_targets(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]
        reviewer = three_sandbox_harness["review-1"]

        async def coder_responds() -> None:
            msg = await coder.wait_for_message("task.assign")
            await coder.reply(msg, "task.complete", {"who": "coder"})

        async def reviewer_responds() -> None:
            msg = await reviewer.wait_for_message("review.request")
            await reviewer.reply(msg, "review.feedback", {"who": "reviewer"})

        coder_task = asyncio.create_task(coder_responds())
        reviewer_task = asyncio.create_task(reviewer_responds())

        code_reply, review_reply = await asyncio.gather(
            orch.request("coding-1", "task.assign", {"prompt": "code"}, timeout=5.0),
            orch.request("review-1", "review.request", {"diff": "..."}, timeout=5.0),
        )

        assert code_reply.payload == {"who": "coder"}
        assert review_reply.payload == {"who": "reviewer"}
        await coder_task
        await reviewer_task
