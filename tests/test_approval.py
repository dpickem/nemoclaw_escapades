"""Tests for the approval gate."""

from __future__ import annotations

from nemoclaw_escapades.orchestrator.approval import AutoApproval, WriteApproval


class TestAutoApproval:
    async def test_approves_everything(self) -> None:
        gate = AutoApproval()
        result = await gate.check("respond", {"content": "hello"})
        assert result.approved is True
        assert result.reason == "auto_approved"


class TestWriteApproval:
    async def test_respond_action_auto_approved(self) -> None:
        gate = WriteApproval()
        result = await gate.check("respond", {"content": "hello"})
        assert result.approved is True

    async def test_read_tool_call_auto_approved(self) -> None:
        gate = WriteApproval()
        result = await gate.check(
            "tool_call",
            {"tool_name": "jira_search", "is_read_only": True},
        )
        assert result.approved is True
        assert "read_auto_approved" in (result.reason or "")

    async def test_write_tool_call_denied(self) -> None:
        gate = WriteApproval()
        result = await gate.check(
            "tool_call",
            {"tool_name": "jira_create_issue", "is_read_only": False},
        )
        assert result.approved is False
        assert "write_requires_approval" in (result.reason or "")

    async def test_high_risk_tool_classified(self) -> None:
        gate = WriteApproval()
        result = await gate.check(
            "tool_call",
            {"tool_name": "jira_transition_issue", "is_read_only": False},
        )
        assert result.approved is False
        assert "HIGH" in (result.reason or "")

    async def test_low_risk_tool_classified(self) -> None:
        gate = WriteApproval()
        result = await gate.check(
            "tool_call",
            {"tool_name": "jira_add_comment", "is_read_only": False},
        )
        assert result.approved is False
        assert "LOW" in (result.reason or "")
