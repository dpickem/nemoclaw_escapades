"""Agent package — reusable inference + tool execution loop.

This package groups the role-agnostic pieces of the NemoClaw runtime:
the multi-turn loop (``loop.py``), the layered system-prompt builder
and per-thread history store (``prompt_builder.py``), the two-tier
context compactor (``compaction.py``), the skill loader (``skill_loader.py``),
approval gates (``approval.py``), and the shared data types
(``types.py``).

The two-list message model
---------------------------

NemoClaw keeps two distinct message lists, and every contributor to this
package should understand which list they're touching:

- ``LayeredPromptBuilder._thread_history`` is **permanent per-thread
  state**.  Only ``user`` / ``assistant`` role messages.  Grows across
  turns and is capped at ``max_thread_history``.  Persisted via
  ``LayeredPromptBuilder.commit_turn`` after a successful round-trip.
- ``AgentLoop``'s ``working_messages`` is **ephemeral per-run state**.
  Starts as what the prompt builder produced (``[system, …history…,
  user]``) and grows with ``assistant``-with-``tool_calls`` and
  ``tool``-role messages as the loop executes tools.  Discarded when
  ``run()`` returns.

Tool results live exclusively in ``working_messages``.  They are
micro-compacted (truncated) by ``ContextCompactor.truncate_tool_results``
before each inference call, but never written back to thread history —
otherwise a single large ``read_file`` would dominate every subsequent
prompt in the thread.  See the module docstrings in ``loop.py`` and
``prompt_builder.py`` for the full rationale.
"""

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
