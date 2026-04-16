"""Agent package — reusable inference + tool execution loop."""

from nemoclaw_escapades.agent.approval import ApprovalGate, AutoApproval, WriteApproval
from nemoclaw_escapades.agent.loop import AgentLoop
from nemoclaw_escapades.agent.types import AgentLoopResult
from nemoclaw_escapades.config import AgentLoopConfig

__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopResult",
    "ApprovalGate",
    "AutoApproval",
    "WriteApproval",
]
