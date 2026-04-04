"""Approval gate — tiered auto-approval for operations.

M1 is conversational only (no tools), so all responses auto-approve.
The interface is defined here so M2 can plug in the tiered classifier
and Slack escalation without restructuring the orchestrator loop.

M2+ tiers:
  - Fast-path: pattern matching for known-safe read operations
  - LLM classifier: evaluates ambiguous operations
  - Slack escalation: dangerous operations pause and send approval request
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
                    "tool_call", "file_write").
            context: Action-specific metadata for the decision.

        Returns:
            ApprovalResult indicating whether the action is approved.
        """


class AutoApproval(ApprovalGate):
    """M1 stub: approves all actions.

    In M1 there are no side-effect tools, so every action is safe.
    This class exists to establish the interface for M2.
    """

    async def check(self, action: str, context: dict[str, object]) -> ApprovalResult:
        logger.debug(
            "Auto-approved action",
            extra={"action": action},
        )
        return ApprovalResult(approved=True, reason="auto_approved")
