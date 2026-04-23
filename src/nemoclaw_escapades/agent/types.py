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

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol

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
        working_messages: Full conversation including tool results,
            useful for debugging and approval-resume flows.
    """

    content: str
    tool_calls_made: int
    rounds: int
    hit_safety_limit: bool
    working_messages: list[Message] = field(default_factory=list)


@dataclass
class AgentSetupBundle:
    """Setup payload the orchestrator sends to a sub-agent via ``task.assign``.

    In M2b Phase 1 this is the protocol surface between the
    orchestrator and the coding sub-agent.  The orchestrator builds
    it per task and sends it in the NMB ``task.assign`` payload; the
    sub-agent unpacks it at task start to configure the ``AgentLoop``:

    - ``task_description`` seeds the initial user message.
    - ``workspace_root`` scopes the file / search / bash / git tools.
    - ``agent_id`` is surfaced in the system prompt's runtime-metadata
      layer so the ``scratchpad`` skill can key its
      ``notes-<task-slug>-<agent-id>.md`` filename off it.
    - ``source_type`` feeds the channel-hint layer so the model knows
      it's running as a delegated sub-agent (concise output, no
      conversational pleasantries).

    See ``docs/design_m2b.md`` §4.1 (Spawn Sequence) and §6.2
    (Sub-Agent Entrypoint).

    Attributes:
        task_id: Globally unique task identifier.  Stamped into
            ``task.complete`` replies so the orchestrator can match
            results back to the request.
        agent_id: Unique identifier for this sub-agent invocation.
            Used as the notes-file owner tag and as the NMB sandbox
            identity when applicable.  Short enough to embed in a
            filename (truncate UUIDs to 8 chars for readability).
        parent_agent_id: Identifier of the agent that spawned this
            one — normally the orchestrator.  Included in audit
            records so the delegation tree is traceable.
        task_description: Natural-language description of the work
            to be done.  Forms the initial user message for the
            AgentLoop.
        workspace_root: Absolute filesystem path the sub-agent
            operates in.  File / bash / git tools treat this as their
            root; path traversal outside is rejected.
        source_type: Channel-hint source label.  Defaults to
            ``"agent"`` — the prompt builder uses this to craft a
            sub-agent-appropriate channel hint.
    """

    task_id: str
    agent_id: str
    parent_agent_id: str
    task_description: str
    workspace_root: str
    source_type: str = "agent"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for NMB transport.

        Thin wrapper around :func:`dataclasses.asdict` — kept as an
        explicit method so the serde surface matches the custom
        :meth:`from_dict` below (which needs explicit handling of
        missing required fields).
        """
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AgentSetupBundle:
        """Deserialise from an NMB payload dict.

        Dataclasses don't provide a built-in ``from_dict`` that fails
        loudly on a missing required field.  We want the "missing
        required field in a ``task.assign`` payload" case to raise
        rather than silently construct with defaults, so this stays
        custom — one ``[key]`` lookup per required field, one
        ``.get(..., default)`` for the one optional field.

        Raises:
            KeyError: If a required field is missing.
        """
        return cls(
            task_id=payload["task_id"],
            agent_id=payload["agent_id"],
            parent_agent_id=payload["parent_agent_id"],
            task_description=payload["task_description"],
            workspace_root=payload["workspace_root"],
            source_type=payload.get("source_type", "agent"),
        )
