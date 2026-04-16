"""Orchestrator — connector-facing handler and agent loop.

Re-exports approval classes from ``agent.approval`` for backwards
compatibility.  ``Orchestrator`` itself is imported directly from
``orchestrator.orchestrator`` (not re-exported here) to avoid a
circular import with ``agent.loop``.
"""

from nemoclaw_escapades.agent.approval import ApprovalGate, AutoApproval, WriteApproval
from nemoclaw_escapades.agent.prompt_builder import LayeredPromptBuilder

__all__ = [
    "ApprovalGate",
    "AutoApproval",
    "WriteApproval",
    "LayeredPromptBuilder",
]
