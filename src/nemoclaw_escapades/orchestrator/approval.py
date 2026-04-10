"""Approval gate — tiered approval for tool operations.

The gate classifies every orchestrator action as safe or unsafe:

- **READ** tool calls and text responses: auto-approved (fast path).
- **WRITE** tool calls: denied so the orchestrator can present the
  proposed action to the user with Approve / Deny buttons.

The orchestrator consults the gate before executing each tool batch.
When a write tool is blocked, the conversation state is saved and
execution resumes only after explicit user approval.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nemoclaw_escapades.models.types import ApprovalResult
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("approval")


class ApprovalGate(ABC):
    """Abstract approval gate. Subclasses implement tiered approval logic."""

    @abstractmethod
    async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
        """Check whether an action should be approved.

        Args:
            action: The type of action being checked (e.g., "respond",
                    "tool_call", "write_tool_call").
            context: Action-specific metadata for the decision.

        Returns:
            ApprovalResult indicating whether the action is approved.
        """


class AutoApproval(ApprovalGate):
    """Approves all actions automatically.

    Used for READ operations and as the default gate when no
    Slack-based approval is configured.
    """

    async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
        logger.debug(
            "Auto-approved action",
            extra={"action": action},
        )
        return ApprovalResult(approved=True, reason="auto_approved")


class WriteApproval(ApprovalGate):
    """Approval gate for the native tool system.

    READ operations are auto-approved.  WRITE tool calls are **denied**
    so the orchestrator can present the proposed action to the user for
    interactive approval via Approve / Deny buttons.
    """

    HIGH_RISK_TOOLS: set[str] = {
        "jira_transition_issue",
        "jira_update_issue",
    }

    async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
        if action == "respond":
            return ApprovalResult(approved=True, reason="auto_approved")

        if action == "tool_call":
            is_read_only = context.get("is_read_only", True)
            if is_read_only:
                return ApprovalResult(approved=True, reason="read_auto_approved")

            tool_name = str(context.get("tool_name", ""))
            risk = self._assess_risk(tool_name)
            logger.info(
                "WRITE operation blocked for approval",
                extra={
                    "tool_name": tool_name,
                    "risk": risk,
                },
            )
            return ApprovalResult(
                approved=False,
                reason=f"write_requires_approval (risk={risk})",
            )

        return ApprovalResult(approved=True, reason="auto_approved")

    def _assess_risk(self, tool_name: str) -> str:
        """Classify a write tool as LOW or HIGH risk."""
        return "HIGH" if tool_name in self.HIGH_RISK_TOOLS else "LOW"
