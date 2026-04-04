"""Tests for the approval gate interface."""

from __future__ import annotations

from nemoclaw_escapades.models.types import ApprovalResult
from nemoclaw_escapades.orchestrator.approval import ApprovalGate, AutoApproval


class TestAutoApproval:
    """AutoApproval should approve everything in M1."""

    async def test_auto_approves_respond(self) -> None:
        gate = AutoApproval()
        result = await gate.check("respond", {"content": "Hello"})
        assert result.approved is True
        assert result.reason == "auto_approved"

    async def test_auto_approves_any_action(self) -> None:
        gate = AutoApproval()
        for action in ("respond", "tool_call", "file_write", "arbitrary"):
            result = await gate.check(action, {})
            assert result.approved is True


class TestApprovalGateInterface:
    """Verify the ABC contract is correct."""

    async def test_custom_gate_can_deny(self) -> None:
        class DenyAll(ApprovalGate):
            async def check(
                self, action: str, context: dict[str, object]
            ) -> ApprovalResult:
                return ApprovalResult(approved=False, reason="denied_by_policy")

        gate = DenyAll()
        result = await gate.check("tool_call", {"tool": "rm_rf"})
        assert result.approved is False
        assert result.reason == "denied_by_policy"
