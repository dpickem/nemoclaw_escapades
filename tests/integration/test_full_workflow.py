"""Integration tests: full multi-sandbox workflows (coding + review loop)."""

from __future__ import annotations

import asyncio

import pytest

from nemoclaw_escapades.nmb.testing import IntegrationHarness

pytestmark = pytest.mark.integration


class TestCodingReviewLoop:
    """End-to-end coding → review → fix → approve workflow from §11."""

    async def test_single_review_iteration(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """Orchestrator assigns code, reviews it, gets approval."""
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]
        reviewer = three_sandbox_harness["review-1"]

        # 1) Assign task
        await orch.send("coding-1", "task.assign", {
            "prompt": "Implement feature X",
            "context_files": [{"path": "src/main.py"}],
        })
        task = await coder.wait_for_message("task.assign")
        assert task.payload["prompt"] == "Implement feature X"

        # 2) Coder completes
        await coder.send("orchestrator", "task.complete", {
            "diff": "--- a/src/main.py\n+++ b/src/main.py",
        })
        complete = await orch.wait_for_message("task.complete")

        # 3) Orchestrator requests review
        async def reviewer_approves() -> None:
            msg = await reviewer.wait_for_message("review.request")
            await reviewer.reply(msg, "review.lgtm", {"summary": "LGTM"})

        review_task = asyncio.create_task(reviewer_approves())

        feedback = await orch.request(
            "review-1",
            "review.request",
            {"diff": complete.payload["diff"]},
            timeout=5.0,
        )
        await review_task

        assert feedback.type == "review.lgtm"
        assert feedback.payload["summary"] == "LGTM"

    async def test_review_with_changes_requested(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """Reviewer requests changes; coder fixes; second review approves."""
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]
        reviewer = three_sandbox_harness["review-1"]

        # --- Round 1: assign → code → review (request_changes) ---

        await orch.send("coding-1", "task.assign", {"prompt": "feature Y"})
        await coder.wait_for_message("task.assign")

        await coder.send("orchestrator", "task.complete", {"diff": "v1"})
        v1 = await orch.wait_for_message("task.complete")

        async def reviewer_rejects() -> None:
            msg = await reviewer.wait_for_message("review.request")
            await reviewer.reply(msg, "review.feedback", {
                "verdict": "request_changes",
                "comments": ["Fix naming conventions"],
            })

        reject_task = asyncio.create_task(reviewer_rejects())
        feedback = await orch.request(
            "review-1", "review.request",
            {"diff": v1.payload["diff"]},
            timeout=5.0,
        )
        await reject_task

        assert feedback.payload["verdict"] == "request_changes"

        # --- Round 2: fix → review (approve) ---

        coder.received.clear()
        await orch.send("coding-1", "task.assign", {
            "prompt": "Fix: " + str(feedback.payload["comments"]),
        })
        fix_task = await coder.wait_for_message("task.assign")
        assert "Fix" in fix_task.payload["prompt"]

        orch.received.clear()
        await coder.send("orchestrator", "task.complete", {"diff": "v2-fixed"})
        v2 = await orch.wait_for_message("task.complete")

        async def reviewer_accepts() -> None:
            reviewer.received.clear()
            msg = await reviewer.wait_for_message("review.request")
            await reviewer.reply(msg, "review.lgtm", {"summary": "LGTM"})

        accept_task = asyncio.create_task(reviewer_accepts())
        approval = await orch.request(
            "review-1", "review.request",
            {"diff": v2.payload["diff"]},
            timeout=5.0,
        )
        await accept_task

        assert approval.type == "review.lgtm"

    async def test_workflow_with_progress_streaming(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """Coder publishes progress updates while working on a task."""
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]

        progress: list[dict[str, object]] = []

        async def progress_collector() -> None:
            async for msg in orch.subscribe("progress.coding-1"):
                progress.append(msg.payload or {})
                if msg.payload and msg.payload.get("pct") == 100:
                    return

        sub_task = asyncio.create_task(progress_collector())
        await asyncio.sleep(0.1)

        # Assign task
        await orch.send("coding-1", "task.assign", {"prompt": "feature Z"})
        await coder.wait_for_message("task.assign")

        # Coder publishes progress
        await coder.publish(
            "progress.coding-1", "task.progress", {"pct": 25, "status": "started"}
        )
        await coder.publish(
            "progress.coding-1", "task.progress", {"pct": 50, "status": "halfway"}
        )
        await coder.publish(
            "progress.coding-1", "task.progress", {"pct": 100, "status": "done"}
        )

        await asyncio.wait_for(sub_task, timeout=5.0)

        assert len(progress) == 3
        assert progress[0]["pct"] == 25
        assert progress[1]["pct"] == 50
        assert progress[2]["pct"] == 100

    async def test_audit_records_all_messages(
        self, three_sandbox_harness: IntegrationHarness
    ) -> None:
        """Verify the audit DB captures messages from the workflow."""
        orch = three_sandbox_harness["orchestrator"]
        coder = three_sandbox_harness["coding-1"]

        await orch.send("coding-1", "task.assign", {"prompt": "audit-test"})
        await coder.wait_for_message("task.assign")
        await coder.send("orchestrator", "task.complete", {"diff": "audit-diff"})
        await orch.wait_for_message("task.complete")

        # Give background audit writer time to flush
        await asyncio.sleep(0.5)

        audit = three_sandbox_harness.broker._audit
        assert audit is not None
        rows = await audit.query(
            "SELECT op, type, delivery_status FROM messages ORDER BY timestamp"
        )
        ops = [(r["op"], r["type"]) for r in rows]
        assert ("send", "task.assign") in ops
        assert ("send", "task.complete") in ops
