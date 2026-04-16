"""Data types for the reusable agent loop (Layer 1).

This module defines the result dataclass and callback protocols that
keep ``AgentLoop`` decoupled from connectors (Slack), NMB, and
specific tool implementations.  It is part of the three-layer agent
architecture described in `design_m2.md §4.7
<../../../docs/design_m2.md#47--the-three-layer-agent-architecture>`_:

- **Layer 1 — AgentLoop** (this package): pure inference + tool
  execution loop.  No NMB, no connectors, no event handling.
- **Layer 2 — Agent base class** (future): owns MessageBus + lifecycle.
- **Layer 3 — Role-specific agents** (future): OrchestratorAgent,
  CodingAgent, ReviewAgent.

``AgentLoopResult`` captures the outcome of a single
``AgentLoop.run()`` invocation.  The callback protocols
(``ToolStartCallback``, ``ToolEndCallback``) allow connectors to
display thinking indicators without the loop importing any platform SDK.

``AgentLoopConfig`` lives in ``config.py`` alongside the other
configuration dataclasses.

See also:
    - `AgentLoop <./loop.py>`_ — the loop implementation.
    - `design_m2.md §4.2
      <../../../docs/design_m2.md#42--agentloop-interface>`_ — interface spec.
    - `design_m2.md §14 Phase 1
      <../../../docs/design_m2.md#phase-1--agentloop-extraction>`_ —
      implementation plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from nemoclaw_escapades.models.types import Message


class ToolStartCallback(Protocol):
    """Invoked before each tool execution with the display label.

    Connectors use this to update a thinking indicator (e.g.
    "Searching Jira...").
    """

    async def __call__(self, display_name: str) -> None: ...


class ToolEndCallback(Protocol):
    """Invoked after each tool execution with timing and outcome."""

    async def __call__(self, tool_name: str, duration_ms: float, success: bool) -> None: ...


@dataclass
class AgentLoopResult:
    """Outcome of a single ``AgentLoop.run()`` invocation.

    Attributes:
        content: Final text response from the model.
        tool_calls_made: Total tool invocations across all rounds.
        rounds: Number of inference calls made.
        hit_safety_limit: ``True`` if ``max_tool_rounds`` was reached
            without the model producing a text-only response.
        scratchpad_contents: Snapshot of the scratchpad after the run
            (``None`` until scratchpad support is added in Phase 2).
        working_messages: Full conversation including tool results,
            useful for debugging and approval-resume flows.
    """

    content: str
    tool_calls_made: int
    rounds: int
    hit_safety_limit: bool
    scratchpad_contents: str | None = None
    working_messages: list[Message] = field(default_factory=list)
