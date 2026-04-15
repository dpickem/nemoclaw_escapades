# Milestone 2 — Sandboxed Coding Agents with OpenShell (Original)

> **Status:** Superseded by M2a/M2b split (April 2026). Content retained in
> full — detailed specs, code examples, comparison tables, and implementation
> phases are referenced by the new documents and remain valuable for later
> milestones.
>
> **Last updated:** 2026-04-14
>
> **Implementation documents:**
> - [M2a — Reusable Agent Loop, Coding Tools & Context Management](design_m2a.md)
> - [M2b — Multi-Agent Orchestration: Delegation, NMB & Concurrency](design_m2b.md)
>
> **Related:**
> [Design Doc §4](design.md#4--milestones) |
> [Orchestrator Design](orchestrator_design.md) |
> [NMB Design](nmb_design.md) |
> [Sandbox Spawn Design](sandbox_spawn_design.md) |
> [Audit DB Design](audit_db_design.md) |
> [Inference Call Auditing](inference_call_auditing_design.md) |
> [Agent Trace Design](agent_trace_design.md) |
> [Tools Integration Design](tools_integration_design.md) |
> [Executor/Advisor NMB Design](executor_advisor_nmb_design.md) |
> [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md)

### What Changed in the M2a/M2b Split

This document was written as a single milestone covering everything from
`AgentLoop` extraction to multi-sandbox delegation. The April 2026 split
separated it into two milestones and made several scope changes:

| Change | This Document | M2a/M2b |
|--------|--------------|---------|
| **Milestone scope** | Single M2 with 7 phases | M2a (single capable agent, 3 phases) + M2b (multi-agent, 5 phases) |
| **Context compaction** | Deferred to M3 | Promoted to M2a P3 |
| **Basic SKILL.md loading** | Deferred to M6 | Promoted to M2a P3 |
| **Basic operational cron** | Deferred to M6 | Promoted to M2b P4 |
| **In-process dispatch (`LocalDispatcher`)** | Included (Phase 5) | Cut — throwaway code once NMB broker runs |
| **Policy hot-reload** | Included (§6.3) | Deferred to M3 — no separate policy boundary in same-sandbox M2b |
| **Git worktree support** | Included (Phase 7) | Cut — each sub-agent gets its own workspace |
| **Multi-sandbox delegation** | In scope (§5) | Deferred to M3 — M2b uses same-sandbox subprocess |
| **Artifact download via `openshell sandbox download`** | Throughout (§5, §10, §12) | M2b uses direct filesystem reads (same sandbox) |

Sections below reflect the **original unified design**. Where M2a/M2b diverge,
the implementation documents take precedence. Content here that describes
multi-sandbox flows (downloads, policy hot-reload, separate sandbox policies)
remains valuable as the M3 design foundation.

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [Architecture](#3--architecture)
4. [The Reusable Agent Loop (`AgentLoop`)](#4--the-reusable-agent-loop-agentloop)
5. [Sandbox Lifecycle & Workspace Setup](#5--sandbox-lifecycle--workspace-setup)
6. [Agent Setup: Policy, Tools, and Comms](#6--agent-setup-policy-tools-and-comms)
7. [Coding Agent File Tools](#7--coding-agent-file-tools)
8. [Agent Scratchpad](#8--agent-scratchpad)
9. [Orchestrator ↔ Sub-Agent Protocol](#9--orchestrator--sub-agent-protocol)
10. [Work Collection and Finalization](#10--work-collection-and-finalization)
11. [Preparing for Skills and Memory (M5+)](#11--preparing-for-skills-and-memory-m5)
12. [Audit and Observability](#12--audit-and-observability)
13. [End-to-End Walkthrough](#13--end-to-end-walkthrough)
14. [Implementation Plan](#14--implementation-plan)
15. [Testing Plan](#15--testing-plan)
16. [Risks and Mitigations](#16--risks-and-mitigations)
17. [Open Questions](#17--open-questions)
18. [Comparison with Hermes, OpenClaw, and Claude Code](#18--comparison-with-hermes-openclaw-and-claude-code)

---

## 1  Overview

Milestone 2 delivers the first multi-agent capability: the orchestrator spawns
sandboxed sub-agents (starting with a coding agent), delegates tasks via NMB,
and collects completed work. Every sub-agent runs in its own OpenShell sandbox
with an independent filesystem, credential scope, and network policy.

The central design challenge is **factoring the "agent" out of the
orchestrator**. Today the multi-turn tool-calling loop lives inside
`Orchestrator._run_agent_loop()` and is tightly coupled to orchestrator-specific
concerns (Slack thread keys, approval buttons, connector callbacks). M2 extracts
this into a reusable `AgentLoop` that can run identically inside:

- The orchestrator sandbox (the "root agent")
- Any child sandbox (coding agent, review agent, research agent, etc.)
- A local process (for development without OpenShell)

Sub-agents are **siblings, not children** at the container level — OpenShell
does not support nested sandboxes. Parent-child relationships are tracked at the
application layer via NMB metadata (`parent_sandbox_id`, `workflow_id`).
Regardless of whether a sub-agent runs in an isolated sandbox or as a separate
process on the same host, the communication and setup protocol is identical.

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Extract a reusable `AgentLoop` from the orchestrator that any agent can use.
2. Build a **native coding agent** using `AgentLoop` + coding file tools + NMB.
   This is a natural consequence of the `AgentLoop` extraction — the coding
   agent is just `AgentLoop` configured with file/git/bash tools and a coding
   system prompt. Claude Code cannot participate in NMB-based coordination, so
   a native agent is required for M2.
3. Equip sub-agents with a concrete **file tool suite** (read, write, edit,
   grep, glob, bash, git) so they can operate on workspace contents.
4. Define the full sandbox setup sequence: workspace, tools, comms, policy.
5. Implement a scratchpad mechanism (backed by file tools) for sub-agents to
   take working notes.
6. Delegate a coding task from the orchestrator to a sandboxed coding agent
   and collect the completed work.
7. Build the orchestrator-side **work collection and finalization** flow:
   collect sub-agent results, present to the user for review, and
   commit/push/create PR on approval.
8. Design the setup contract to be forward-compatible with skills (M5) and
   persistent memory (M5+).
9. Maintain existing audit, approval, and safety guarantees.

### 2.2 Non-Goals

1. Implementing the review agent or multi-agent collaboration loops (M3).
2. Implementing skills, memory, or the self-learning loop (M5-M6).
3. Multi-host sandbox deployment (single-host only for M2).
4. Web UI integration (deferred to incremental additions per milestone).
5. Advanced coding agent features (context compression, concurrent tool
   execution, prompt caching) — these are M6+ polish on top of the
   M2 foundation.

---

## 3  Architecture

### 3.1 Sandbox Topology

All sandboxes are siblings managed by the OpenShell gateway. The
orchestrator-to-sub-agent hierarchy is a logical concept tracked in NMB
metadata, not a container nesting relationship.

```
┌─────────────────────────────────────────────────────────────────────┐
│  HOST MACHINE                                                       │
│                                                                     │
│  ┌───────────────────────────┐  ┌────────────────────────────────┐  │
│  │  OpenShell Gateway        │  │  NMB Broker (port 9876)        │  │
│  │  (sandbox lifecycle)      │  │  (message routing + audit)     │  │
│  └─────────┬─────────────────┘  └───────────┬────────────────────┘  │
│            │                                │                       │
│  ┌─────────┴─────────────────────────────────┴────────────────────┐ │
│  │                       Sibling Sandboxes                        │ │
│  │                                                                │ │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐ │ │
│  │  │ ORCHESTRATOR     │  │ CODING AGENT     │  │ REVIEW AGENT │ │ │
│  │  │                  │  │                  │  │ (M3)         │ │ │
│  │  │ AgentLoop        │  │ AgentLoop        │  │ AgentLoop    │ │ │
│  │  │ + Coordinator    │  │ + Coding tools   │  │ + Review     │ │ │
│  │  │ + Slack conn.    │  │ + Workspace      │  │   tools      │ │ │
│  │  │ + Approval gate  │  │ + Scratchpad     │  │ + Scratchpad │ │ │
│  │  │ + NMB client     │  │ + NMB client     │  │ + NMB client │ │ │
│  │  │ + Audit DB       │  │ + Audit DB       │  │ + Audit DB   │ │ │
│  │  └──────────────────┘  └──────────────────┘  └──────────────┘ │ │
│  └────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Map

| Component | Orchestrator (`OrchestratorAgent`) | Sub-Agent (`CodingAgent`) |
|-----------|-------------|-------------------|
| **Layer 1: AgentLoop** | Shared `AgentLoop` class (§4.2) | Same `AgentLoop` class |
| **Layer 2: Agent base** | Inherits `Agent(ABC)` (§4.8) — owns `AgentLoop` + `MessageBus` + lifecycle | Same `Agent(ABC)` base class |
| **Layer 3: Role config** | `OrchestratorAgent(Agent)` — overrides tool registry, approval, audit, event loop | `CodingAgent(Agent)` — overrides tool registry, audit; inherits default event loop |
| **ToolRegistry** | Enterprise tools + delegation tools + **finalization tools** (§10.3) | Coding tools (bash, read/write file, grep, git, etc.) |
| **Event loops** | Dual: Slack request loop + NMB coordinator loop (§10.7), concurrent via `asyncio.gather` | Single: NMB listen loop (inherited from `Agent` base) |
| **NMB role** | Client (like all agents) — but subscribes to `system` channel and dispatches a wider set of event types (`task.complete`, `policy.request`, `audit.flush`) with concurrent `asyncio.Task` dispatch | Client — listens for `task.assign`, replies with `task.complete` |
| **Audit DB** | Central audit, receives NMB-batched flushes from children | Lightweight buffer, flushes to orchestrator via NMB (JSONL fallback) |
| **Scratchpad** | Not used (has full conversation context) | Active — working notes returned with results |
| **Approval gate** | Full (Slack-based write approval) | Simplified (pre-approved scope from policy) |
| **Connector** | Slack connector | None (NMB is the only interface) |
| **System prompt** | Orchestrator persona + delegation instructions | Coding persona + task-scoped instructions |

---

## 4  The Reusable Agent Loop (`AgentLoop`)

### 4.1 Problem

The current `Orchestrator._run_agent_loop()` (see
[orchestrator.py](../src/nemoclaw_escapades/orchestrator/orchestrator.py)) is a
solid multi-turn tool-use loop, but it is bound to orchestrator concerns:

- Slack `thread_ts` for audit correlation
- `StatusCallback` tied to connector thinking indicators
- `WriteApprovalError` exception flow for Slack button rendering
- In-memory `PromptBuilder` with per-thread history

Sub-agents need the same loop mechanics (model ↔ tools until text response or
safety limit) but with different surrounding infrastructure. A coding agent has
no Slack thread, no approval buttons, and no connector callbacks.

### 4.2 Design: Extract `AgentLoop` as a Standalone Class

Factor out the core loop into a class that is **infrastructure-agnostic** and
**reusable by any agent**.

```python
class AgentLoop:
    """Reusable multi-turn tool-calling agent loop.

    Generalized from the orchestrator's _run_agent_loop. Can be used by
    the orchestrator, coding agents, review agents, or any future agent
    type. Inspired by Hermes agent loop and OpenClaw Pi agent patterns.
    """

    def __init__(
        self,
        backend: BackendBase,
        tools: ToolRegistry,
        config: AgentLoopConfig,
        audit: AuditDB | None = None,
        scratchpad: Scratchpad | None = None,
        approval: ApprovalGate | None = None,
        on_tool_start: ToolStartCallback | None = None,
        on_tool_end: ToolEndCallback | None = None,
    ) -> None: ...

    async def run(
        self,
        messages: list[Message],
        request_id: str,
    ) -> AgentLoopResult: ...
```

#### `AgentLoopConfig`

```python
@dataclass
class AgentLoopConfig:
    model: str
    temperature: float = 0.0
    max_tokens: int = 16384
    max_tool_rounds: int = 10
    max_continuation_retries: int = 3
    system_prompt: str = ""
```

#### `AgentLoopResult`

```python
@dataclass
class AgentLoopResult:
    content: str                          # final text response
    tool_calls_made: int                  # total tool invocations
    rounds: int                           # inference rounds used
    hit_safety_limit: bool                # True if max_tool_rounds reached
    scratchpad_contents: str | None       # scratchpad snapshot (if enabled)
    working_messages: list[Message]       # full conversation for debugging
```

### 4.3 How the Orchestrator Uses `AgentLoop`

The orchestrator wraps `AgentLoop` to add its own concerns:

```python
class Orchestrator:
    def __init__(self, ...):
        self._agent_loop = AgentLoop(
            backend=backend,
            tools=tools,
            config=agent_loop_config,
            audit=audit,
            approval=approval_gate,
            on_tool_start=self._notify_connector_tool_start,
            on_tool_end=self._notify_connector_tool_end,
        )

    async def handle(self, request: NormalizedRequest, ...) -> RichResponse:
        messages = self._prompt.messages_for_inference(thread_key, request.text)
        result = await self._agent_loop.run(messages, request.request_id)
        self._prompt.commit_turn(thread_key, request.text, result.content)
        return self._shape_response(request, result.content)
```

### 4.4 How a Sub-Agent Uses `AgentLoop`

A coding sub-agent is a standalone process that listens on NMB, runs the loop,
and returns results:

```python
class CodingAgent:
    def __init__(self, bus: MessageBus, backend: BackendBase, ...):
        self._bus = bus
        self._loop = AgentLoop(
            backend=backend,
            tools=coding_tool_registry,
            config=AgentLoopConfig(
                model=config.model,
                system_prompt=coding_system_prompt,
                max_tool_rounds=20,   # coding tasks are longer
            ),
            audit=audit_buffer,
            scratchpad=Scratchpad(path="/sandbox/scratchpad.md"),
        )

    async def run(self):
        async for msg in self._bus.listen():
            if msg.type == "task.assign":
                result = await self._handle_task(msg)
                await self._bus.reply(msg, "task.complete", result)

    async def _handle_task(self, msg: NMBMessage) -> dict:
        messages = self._build_messages(msg.payload)
        result = await self._loop.run(messages, request_id=msg.id)
        return {
            "result": result.content,
            "scratchpad": result.scratchpad_contents,
            "files_changed": self._get_changed_files(),
            "diff": self._get_workspace_diff(),
            "tool_calls_made": result.tool_calls_made,
        }
```

### 4.5 Loop Internals (Shared Behavior)

The `AgentLoop.run()` method preserves the proven mechanics from the current
orchestrator loop:

1. **Tool definitions snapshot** — captured once per `run()` call.
2. **Shallow-copy messages** — caller's list is never mutated.
3. **Per-round inference** — send messages + tool defs to backend.
4. **Terminal condition** — no `tool_calls` in response → repair → return text.
5. **Approval gate** — if configured, checks write tools before execution.
   In the orchestrator this raises `WriteApprovalError`; in a sub-agent it
   logs a policy violation (sub-agents should only call pre-approved tools).
6. **Tool execution** — sequential by default, concurrent for safe tools (M2+).
7. **Audit logging** — every tool invocation is recorded via
   `audit.log_tool_call()`. The `AgentLoop` doesn't know whether `audit` is
   an `AuditDB` (orchestrator — writes directly to SQLite) or an
   `AuditBuffer` (sub-agent — accumulates in memory, flushes via NMB).
   Both implement the same `log_tool_call()` interface.
8. **Round-boundary audit flush** — at the end of each tool-execution round
   (after all tool results are appended and before the next inference call),
   the loop calls `audit.flush_round()`. For `AuditDB` this is a no-op
   (writes are already persisted). For `AuditBuffer` this triggers a batched
   NMB send of accumulated tool-call records to the orchestrator (see §4.9).
9. **Scratchpad auto-update** — after each round, the scratchpad's contents are
   included as a system-level context injection (see §8).
10. **Truncation handling** — `finish_reason=length` triggers continuation retry.
11. **Safety limit** — returns partial answer after `max_tool_rounds`.

### 4.6 Comparison with Reference Architectures

| Aspect | Current Orchestrator | AgentLoop (M2) | Hermes `AgentRunner` | OpenClaw Pi |
|--------|---------------------|----------------|---------------------|-------------|
| Multi-turn loop | `_run_agent_loop` | `AgentLoop.run()` | `agent_loop()` | `processUserMessage()` |
| Tool execution | Sequential | Sequential (concurrent M2+) | Concurrent by default | Sequential w/ streaming |
| Scratchpad | None | `Scratchpad` class | `skills/` + `memory/` | `update-plan-tool` |
| Approval gate | Slack buttons | Pluggable callback | `approval_callback` | Permission levels |
| Audit | `AuditDB.log_tool_call` | Same (via injection) | JSONL logs | None |
| Truncation repair | `_continue_truncated` | Built-in | Context compression | Block streaming |
| Reusable by sub-agents | No (tightly coupled) | Yes | Yes (single process) | Yes (single process) |

### 4.7 The Three-Layer Agent Architecture

The `AgentLoop` is Layer 1 of a three-layer composition model. Role
differentiation (powerful orchestrator vs limited coding agent) and NMB
integration are handled at Layers 2 and 3 — the `AgentLoop` itself stays
role-agnostic and NMB-free.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Layer 3: Role-Specific Agents                                          │
│                                                                         │
│  ┌─────────────────────┐ ┌──────────────────┐ ┌──────────────────────┐  │
│  │ OrchestratorAgent   │ │ CodingAgent      │ │ ReviewAgent          │  │
│  │                     │ │                  │ │                      │  │
│  │ + Slack connector   │ │ + NMB client     │ │ + NMB client         │  │
│  │ + NMB coordinator   │ │ + File tools     │ │ + Read-only file     │  │
│  │   loop (dispatch    │ │ + Scratchpad     │ │   tools              │  │
│  │   task.complete,    │ │ + AuditBuffer    │ │ + Scratchpad         │  │
│  │   policy.request)   │ │                  │ │ + AuditBuffer        │  │
│  │ + Finalization tools│ │                  │ │                      │  │
│  │ + Delegation tools  │ │                  │ │                      │  │
│  │ + Enterprise tools  │ │                  │ │                      │  │
│  │ + Approval gate     │ │                  │ │                      │  │
│  │ + Central AuditDB   │ │                  │ │                      │  │
│  └──────────┬──────────┘ └────────┬─────────┘ └──────────┬───────────┘  │
│             │                     │                      │              │
├─────────────┼─────────────────────┼──────────────────────┼──────────────┤
│  Layer 2: Agent Base Class        │                      │              │
│             │                     │                      │              │
│  ┌──────────┴─────────────────────┴──────────────────────┴───────────┐  │
│  │  Agent(ABC)                                                       │  │
│  │                                                                   │  │
│  │  Owns: AgentLoop + MessageBus + lifecycle                         │  │
│  │  Provides: connect, start, shutdown, _run_event_loop              │  │
│  │  Abstract: _create_tool_registry, _create_approval, _create_audit │  │
│  │                                                                   │  │
│  │  NMB lives here — tools that need NMB receive the MessageBus      │  │
│  │  as a dependency at registration time, not through AgentLoop.     │  │
│  └──────────────────────────────┬────────────────────────────────────┘  │
│                                  │                                      │
├──────────────────────────────────┼──────────────────────────────────────┤
│  Layer 1: AgentLoop              │                                      │
│                                  │                                      │
│  ┌──────────────────────────────┴────────────────────────────────────┐  │
│  │  AgentLoop                                                        │  │
│  │                                                                   │  │
│  │  Pure inference + tool execution loop.                            │  │
│  │  No NMB. No connectors. No event handling.                        │  │
│  │  Role-agnostic. Stateless per run() call.                         │  │
│  │                                                                   │  │
│  │  Injected: BackendBase, ToolRegistry, AgentLoopConfig,            │  │
│  │            AuditDB|AuditBuffer, Scratchpad, ApprovalGate          │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Why three layers?**

- **Layer 1 (`AgentLoop`)** is testable in isolation with mock backends and
  tools. It never imports NMB, Slack, or OpenShell. This is what makes it
  reusable across all agent types without modification.
- **Layer 2 (`Agent`)** adds NMB connectivity and lifecycle management. Every
  agent process — orchestrator or sub-agent — is an `Agent`. The `MessageBus`
  lives here, and tools that need NMB (delegation, finalization, audit buffer)
  receive the bus as a dependency when they are registered in the tool registry.
- **Layer 3** adds role-specific behavior: which tools are loaded, which event
  loops run, which connectors are active. This is where the orchestrator becomes
  strictly more powerful than a coding agent — not through a different
  `AgentLoop`, but through different tools, event handling, and connectors
  composed around the same loop.

### 4.8 The `Agent` Base Class

The `Agent` base class is the composition point for `AgentLoop` + `MessageBus`
+ role-specific configuration. It follows the same abstract-base-class pattern
used by the connector and backend layers.

```python
class Agent(ABC):
    """Base class for all NemoClaw agent processes.

    Composes AgentLoop (Layer 1) with NMB connectivity and lifecycle
    management. Subclasses customize the tool surface, approval gate,
    audit strategy, and event loop to create role-specific agents.
    """

    def __init__(self, config: AgentSetupBundle):
        self._config = config
        self._bus = MessageBus(
            url=config.nmb_broker_url,
            sandbox_id=config.sandbox_id,
        )
        self._loop = AgentLoop(
            backend=create_backend(config),
            tools=self._create_tool_registry(config),
            config=AgentLoopConfig(
                model=config.model,
                system_prompt=config.system_prompt,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                max_tool_rounds=config.max_tool_rounds,
            ),
            audit=self._create_audit(config),
            scratchpad=(
                Scratchpad(path=config.scratchpad_path)
                if config.scratchpad_path else None
            ),
            approval=self._create_approval(config),
        )

    # ── Abstract: subclasses define these ──────────────────────

    @abstractmethod
    def _create_tool_registry(self, config: AgentSetupBundle) -> ToolRegistry:
        """Build the tool surface for this agent role."""

    @abstractmethod
    def _create_approval(self, config: AgentSetupBundle) -> ApprovalGate | None:
        """Orchestrator: SlackApproval. Sub-agents: None (pre-approved)."""

    @abstractmethod
    def _create_audit(self, config: AgentSetupBundle) -> AuditDB | AuditBuffer:
        """Orchestrator: central AuditDB. Sub-agents: AuditBuffer(bus)."""

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self):
        await self._bus.connect()
        await self._bus.publish("system", "sandbox.ready", {
            "sandbox_id": self._config.sandbox_id,
            "role": self._config.role,
            "capabilities": list(self._loop._tools.tool_names()),
        })
        await self._run_event_loop()

    async def shutdown(self):
        """Graceful shutdown: flush remaining audit records, close NMB."""
        if hasattr(self._loop._audit, 'flush_remaining'):
            await self._loop._audit.flush_remaining()
        if hasattr(self._loop._audit, 'close'):
            await self._loop._audit.close()
        await self._bus.close()

    # ── Event loop (overridable) ───────────────────────────────

    async def _run_event_loop(self):
        """Default: listen for task.assign, run loop, reply.

        The orchestrator overrides this with a dual Slack + NMB loop.
        """
        async for msg in self._bus.listen():
            match msg.type:
                case "task.assign":
                    await self._on_task(msg)
                case "task.cancel":
                    await self._on_cancel(msg)
                case "task.redirect":
                    await self._on_redirect(msg)

    async def _on_task(self, msg: NMBMessage):
        messages = self._build_task_context(msg.payload)
        result = await self._loop.run(messages, request_id=msg.id)
        await self._bus.reply(msg, "task.complete", {
            "result": result.content,
            "scratchpad": result.scratchpad_contents,
            "files_changed": self._get_changed_files(),
            "diff": self._get_workspace_diff(),
            "tool_calls_made": result.tool_calls_made,
        })
```

#### Role-Specific Subclasses

**`OrchestratorAgent`** — the strictly more powerful agent:

```python
class OrchestratorAgent(Agent):
    """The root agent. Owns Slack, NMB coordinator event loop, delegation,
    finalization, enterprise tools, and the central audit DB.

    Like all agents, the orchestrator is an NMB *client* (connects to
    the broker via MessageBus). What makes it the "coordinator" is that
    it subscribes to system channels and dispatches a wider set of event
    types with concurrent asyncio tasks. The NMB broker itself is a
    separate standalone process — not part of any agent.
    """

    def _create_tool_registry(self, config):
        registry = ToolRegistry()
        # Enterprise tools (Jira, Gerrit, Slack, etc.)
        register_enterprise_tools(registry)
        # Delegation tools (delegate_task, destroy_sandbox)
        register_delegation_tools(registry, bus=self._bus)
        # Finalization tools (present_work_to_user, push_and_create_pr, etc.)
        register_finalization_tools(registry, bus=self._bus)
        return registry

    def _create_approval(self, config):
        return SlackApprovalGate(connector=self._slack)

    def _create_audit(self, config):
        return AuditDB(config.audit_db_path, persist_payloads=True)

    async def _run_event_loop(self):
        """Two concurrent loops: Slack + NMB."""
        await asyncio.gather(
            self._run_slack_loop(),
            self._run_nmb_coordinator_loop(),
        )

    async def _run_nmb_coordinator_loop(self):
        """NMB coordinator: subscribes to system events, dispatches
        sub-agent completions to finalization tasks. Still a MessageBus
        client — the broker is a separate process."""
        async for msg in self._bus.listen():
            match msg.type:
                case "task.complete" | "task.error":
                    asyncio.create_task(self._finalize_workflow(msg))
                case "task.progress":
                    self._relay_progress_to_slack(msg)
                case "policy.request":
                    asyncio.create_task(self._handle_policy_request(msg))
                case "audit.flush":
                    await self._ingest_audit_batch(msg)
                case "sandbox.ready":
                    self._on_sandbox_ready(msg)
```

**`CodingAgent`** — uses the base `Agent` defaults:

```python
class CodingAgent(Agent):
    """Sandboxed coding agent. File tools + scratchpad + NMB client."""

    def _create_tool_registry(self, config):
        registry = create_coding_tool_registry(config.workspace_root)
        register_scratchpad_tools(registry, config.scratchpad_path)
        return registry

    def _create_approval(self, config):
        return None  # pre-approved by sandbox policy

    def _create_audit(self, config):
        return AuditBuffer(
            bus=self._bus,
            orchestrator_id=config.parent_sandbox_id or "orchestrator",
            fallback_path="/sandbox/audit_fallback.jsonl",
        )
    # _run_event_loop: inherits base (listen → task.assign → run → reply)
```

**`ReviewAgent`** — read-only variant:

```python
class ReviewAgent(Agent):
    """Sandboxed review agent. Read-only file tools + scratchpad."""

    def _create_tool_registry(self, config):
        registry = create_review_tool_registry(config.workspace_root)
        register_scratchpad_tools(registry, config.scratchpad_path)
        return registry

    def _create_approval(self, config):
        return None

    def _create_audit(self, config):
        return AuditBuffer(
            bus=self._bus,
            orchestrator_id=config.parent_sandbox_id or "orchestrator",
            fallback_path="/sandbox/audit_fallback.jsonl",
        )
```

#### Where NMB Fits in Each Layer

| Concern | Layer 1 (`AgentLoop`) | Layer 2 (`Agent`) | Layer 3 (Role-specific) |
|---------|----------------------|-------------------|------------------------|
| **MessageBus ownership** | No | Yes — `self._bus` | Inherited |
| **NMB event loop** | No | Default: listen for `task.assign` | Orchestrator overrides with dual Slack + NMB coordinator loop (both are `MessageBus` clients; the broker is a separate process) |
| **Tools that use NMB** | Tools call `bus.send()` but `AgentLoop` doesn't know about NMB | Bus injected into tools at registration time | Orchestrator registers delegation + finalization tools with `bus=self._bus` |
| **Audit via NMB** | `AgentLoop` calls `audit.log_tool_call()` per tool + `audit.flush_round()` per round — doesn't know if it's AuditDB or AuditBuffer (§4.9) | `AuditBuffer` uses `self._bus` to flush batched records to orchestrator | Orchestrator uses AuditDB (local write); sub-agents use AuditBuffer (NMB batched flush + JSONL fallback) |
| **NMB role** | N/A | Client (connect, send, listen) | Orchestrator: host (subscribe to system events, dispatch). Sub-agents: client (listen for tasks, reply). |

This separation means `AgentLoop` is fully testable without NMB — just mock
the backend and tools. The `Agent` base class adds NMB as a cross-cutting
concern. Role-specific agents configure *how* NMB is used (coordinator vs
client, which events to handle) without changing the loop itself.

### 4.9 `AuditBuffer` — NMB-Batched Audit Flush

Sub-agents do not write to a local SQLite audit DB. Instead, they use
`AuditBuffer` — a lightweight in-memory buffer that flushes both tool-call
and inference-call records to the orchestrator via NMB at round boundaries.
This follows the design in
[Audit DB Design §7.4](audit_db_design.md#74--mvp-implementation-sketch-option-c)
and [Inference Call Auditing §4.2](inference_call_auditing_design.md#42--sub-agent-calls-nmb-batched-flush).

The `AuditBuffer` implements the same `log_tool_call()` and
`log_inference_call()` interfaces as `AuditDB`, so the `AgentLoop` doesn't
know which one it's using.

```python
class AuditBuffer:
    """Lightweight audit buffer for sub-agents.

    Accumulates tool-call and inference-call records in memory and
    flushes them to the orchestrator via NMB at round boundaries
    (called by AgentLoop after each tool-execution round).  Falls back
    to local JSONL if NMB is unavailable.

    Implements the same log_tool_call() / log_inference_call() interface
    as AuditDB so the AgentLoop is audit-backend-agnostic.
    """

    def __init__(
        self,
        bus: MessageBus,
        orchestrator_id: str,
        fallback_path: str = "/sandbox/audit_fallback.jsonl",
    ):
        self._bus = bus
        self._orchestrator_id = orchestrator_id
        self._fallback_path = fallback_path
        self._tool_buffer: list[dict] = []
        self._inference_buffer: list[dict] = []

    async def log_tool_call(self, **kwargs):
        """Buffer a single tool-call record (called by AgentLoop per tool)."""
        self._tool_buffer.append({
            "record_type": "tool_call",
            "id": uuid4().hex[:16],
            "timestamp": time.time(),
            **kwargs,
        })

    async def log_inference_call(self, **kwargs):
        """Buffer a single inference-call record (called by AgentLoop per round)."""
        self._inference_buffer.append({
            "record_type": "inference_call",
            "id": uuid4().hex[:16],
            "timestamp": time.time(),
            **kwargs,
        })

    async def flush_round(self):
        """Flush buffered records to orchestrator via NMB.

        Called by AgentLoop at the end of each tool-execution round.
        For AuditDB this method is a no-op (writes are already persisted).
        The payload carries both tool_calls and inference_calls arrays.
        """
        if not self._tool_buffer and not self._inference_buffer:
            return
        try:
            await self._bus.send(
                to=self._orchestrator_id,
                type="audit.flush",
                payload={
                    "tool_calls": self._tool_buffer,
                    "inference_calls": self._inference_buffer,
                },
            )
            self._tool_buffer.clear()
            self._inference_buffer.clear()
        except Exception:
            self._write_fallback()

    async def flush_remaining(self):
        """Final flush on agent shutdown — same as flush_round but logs
        any failure more aggressively since there's no next round."""
        await self.flush_round()

    def _write_fallback(self):
        """Append buffered records to local JSONL as a fallback.

        Each line carries a "record_type" discriminator so the
        orchestrator can route to the correct ingest method.
        """
        with open(self._fallback_path, "a") as f:
            for record in self._tool_buffer + self._inference_buffer:
                f.write(json.dumps(record) + "\n")
        self._tool_buffer.clear()
        self._inference_buffer.clear()
```

#### How It Fits into the Three Layers

```
AgentLoop (Layer 1)              Agent (Layer 2)              NMB Broker
     │                                │                           │
     │  tool call #1                  │                           │
     │  audit.log_tool_call(...)      │                           │
     │  → appends to tool_buffer     │                           │
     │                                │                           │
     │  tool call #2                  │                           │
     │  audit.log_tool_call(...)      │                           │
     │  → appends                    │                           │
     │                                │                           │
     │  inference round completes     │                           │
     │  audit.log_inference_call(...) │                           │
     │  → appends to inference_buffer │                           │
     │                                │                           │
     │  ...round ends...             │                           │
     │  audit.flush_round()          │                           │
     │  → AuditBuffer sends          │                           │
     │    audit.flush via bus         │                           │
     │    {tool_calls, inference_     │                           │
     │     calls} ──────────────────────────────────────────────▶│
     │                                │                    route to│
     │                                │                  orchestrator
     │                                │                           │
     │                          Orchestrator                      │
     │                          receives audit.flush              │
     │                          calls log_tool_call() +           │
     │                          log_inference_call()              │
     │                          for each record into              │
     │                          central AuditDB                   │
```

#### Interface Contract

Both `AuditDB` and `AuditBuffer` implement:

| Method | `AuditDB` (orchestrator) | `AuditBuffer` (sub-agent) |
|--------|-------------------------|--------------------------|
| `log_tool_call(**kwargs)` | Writes directly to SQLite | Appends to in-memory `tool_buffer` |
| `log_inference_call(**kwargs)` | Writes directly to SQLite | Appends to in-memory `inference_buffer` |
| `flush_round()` | No-op (already persisted) | Sends `audit.flush` NMB message (both `tool_calls` and `inference_calls`); falls back to JSONL |
| `flush_remaining()` | No-op | Same as `flush_round()` with aggressive error logging |
| `close()` | Dispose SQLite engine | Flush remaining + close (no engine to dispose) |

The `AgentLoop` calls both `log_tool_call()` and `flush_round()` without
knowing which implementation is behind the interface. This is the same
dependency-inversion pattern used for `BackendBase` (inference) and
`ApprovalGate` (permissions).

---

## 5  Sandbox Lifecycle & Workspace Setup

### 5.1 Spawn Sequence

When the orchestrator delegates a task to a coding agent, it runs the following
sequence. Steps 1-3 use the OpenShell CLI (via the sandbox lifecycle tool from
[sandbox_spawn_design.md](sandbox_spawn_design.md)); steps 4-7 use NMB.

```
 Orchestrator                     Gateway              Child Sandbox        NMB Broker
     │                               │                      │                  │
     │ 1. openshell sandbox create   │                      │                  │
     │   --name coding-<wf_id>       │                      │                  │
     │   --policy coding-agent.yaml  │                      │                  │
     │   --from <image>              │                      │                  │
     │──────────────────────────────▶│                      │                  │
     │                               │ 2. provision sandbox │                  │
     │                               │─────────────────────▶│                  │
     │                               │                      │                  │
     │ 3. openshell sandbox exec     │                      │                  │
     │   coding-<wf_id>              │                      │                  │
     │   /app/setup-workspace.sh     │                      │                  │
     │──────────────────────────────▶│ run setup script     │                  │
     │                               │─────────────────────▶│                  │
     │                               │                      │ connects to NMB  │
     │                               │                      │─────────────────▶│
     │                               │                      │ sandbox.ready    │
     │                               │                      │─────────────────▶│
     │                               │                      │                  │
     │◀─────────────────────────────────────────────────────────────────────────│
     │                 sandbox.ready received                                   │
     │                                                                         │
     │ 4. NMB: task.assign           │                      │                  │
     │   { prompt, context_files,    │                      │                  │
     │     tool_surface, ... }       │                      │                  │
     │─────────────────────────────────────────────────────────────────────────▶│
     │                               │                      │◀─────────────────│
     │                               │                      │                  │
     │       ... agent works ...     │                      │                  │
     │                               │                      │                  │
     │ 5. NMB: task.progress (×N)    │                      │                  │
     │◀─────────────────────────────────────────────────────────────────────────│
     │                               │                      │                  │
     │ 6. NMB: task.complete         │                      │                  │
     │   { result, scratchpad,       │                      │                  │
     │     diff, files_changed }     │                      │                  │
     │◀─────────────────────────────────────────────────────────────────────────│
     │                                                                         │
     │ 7. Collect artifacts, pick up any audit fallback file, destroy sandbox  │
```

### 5.2 Workspace Setup (`setup-workspace.sh`)

The workspace setup script runs inside the newly created sandbox and prepares
the environment for the agent. It is a shell script baked into the sandbox
image or uploaded at spawn time.

```bash
#!/usr/bin/env bash
# setup-workspace.sh — called by orchestrator via openshell sandbox exec

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/sandbox/workspace}"
SCRATCHPAD_PATH="${SCRATCHPAD_PATH:-/sandbox/scratchpad.md}"

# ── 1. Workspace directory structure ──────────────────────
mkdir -p "$WORKSPACE_ROOT"
mkdir -p /sandbox/artifacts            # output artifacts (diffs, patches)
mkdir -p /sandbox/.config              # agent config

# ── 2. Scratchpad initialization ──────────────────────────
cat > "$SCRATCHPAD_PATH" << 'EOF'
# Agent Scratchpad

_Working notes for the current task. This file is returned to the
orchestrator on task completion._

## Task Notes

## Observations

## Open Questions

EOF

# ── 3. Clone/checkout workspace (if git repo provided) ────
if [ -n "${GIT_REPO_URL:-}" ]; then
    git clone --depth=1 "$GIT_REPO_URL" "$WORKSPACE_ROOT"
    if [ -n "${GIT_BRANCH:-}" ]; then
        git -C "$WORKSPACE_ROOT" checkout "$GIT_BRANCH"
    fi
fi

# ── 4. Skills directory (placeholder for M5+) ─────────────
mkdir -p /sandbox/skills
# Future: orchestrator uploads relevant SKILL.md files here

# ── 5. Memory directory (placeholder for M5+) ─────────────
mkdir -p /sandbox/memory
# Future: orchestrator seeds relevant memory entries here

# ── 6. Start the agent process ─────────────────────────────
# Note: no --audit-db flag.  Sub-agents use AuditBuffer (in-memory +
# NMB-batched flush to the orchestrator), not local SQLite.  See §4.9.
exec python -m nemoclaw_escapades.agent \
    --role "${AGENT_ROLE:-coding}" \
    --workspace "$WORKSPACE_ROOT" \
    --scratchpad "$SCRATCHPAD_PATH"
```

### 5.3 Workspace Content Seeding

The orchestrator can seed a sub-agent's workspace through three mechanisms,
each suited to different task profiles.

#### Mechanism 1: OpenShell Upload (`openshell sandbox upload`)

The orchestrator uploads files or directories directly into the sandbox
filesystem before the agent process starts.

```
Orchestrator                     Gateway               Child Sandbox
    │                               │                       │
    │ openshell sandbox upload      │                       │
    │   coding-<wf_id>             │                       │
    │   /local/repo/src/           │                       │
    │   /sandbox/workspace/src/    │                       │
    │──────────────────────────────▶│ write files ─────────▶│
    │                               │                       │
    │ openshell sandbox upload      │                       │
    │   coding-<wf_id>             │                       │
    │   /local/config/agent.yaml   │                       │
    │   /sandbox/.config/agent.yaml│                       │
    │──────────────────────────────▶│ write file ──────────▶│
```

**When to use:** Seeding specific directories or config files from the
orchestrator's own filesystem before the agent starts. Good for uploading
a subset of a repo, policy files, or pre-built agent configuration.

**Characteristics:**
- Runs before the agent process starts (part of the setup sequence).
- Can transfer arbitrary file trees (directories, binaries, configs).
- Latency depends on file size: ~1-3s for small trees, longer for large repos.
- The orchestrator must have the files locally (or download them first).

**Common upload targets:**

| What | Orchestrator source | Sandbox destination |
|------|--------------------|--------------------|
| Project subtree | `/local/checkout/src/` | `/sandbox/workspace/src/` |
| Agent config | Generated at spawn time | `/sandbox/.config/agent.yaml` |
| System prompt | `prompts/coding_agent.md` | `/sandbox/.config/system_prompt.md` |
| Skill files (M5+) | Selected from skill store | `/sandbox/skills/` |
| Memory seed (M5+) | Extracted from memory store | `/sandbox/memory/` |

#### Mechanism 2: Git Clone (inside sandbox)

The setup script clones a git repo directly inside the sandbox. The repo URL
and branch are passed as environment variables.

```
Orchestrator                     Gateway               Child Sandbox
    │                               │                       │
    │ openshell sandbox exec        │                       │
    │   coding-<wf_id>             │                       │
    │   env GIT_REPO_URL=...       │                       │
    │   env GIT_BRANCH=feature-x   │                       │
    │   /app/setup-workspace.sh    │                       │
    │──────────────────────────────▶│ run script ──────────▶│
    │                               │                  git clone
    │                               │                  (inside sandbox)
```

**When to use:** Full-repo tasks (feature implementation, refactoring,
migration) where the agent needs the complete project context.

**Characteristics:**
- Requires git remote access in the sandbox network policy.
- Uses `--depth=1` shallow clone by default to minimize transfer time.
- The agent gets a full working git tree with history for diffing and
  committing.
- Latency: 5-30s depending on repo size.

**Policy requirement:** The sandbox network policy must allow access to the
git remote (GitHub, GitLab, etc.). This is a task-specific policy overlay
on top of the base coding-agent policy.

#### Mechanism 3: Context Files via NMB

Small files are included inline in the `task.assign` payload. The agent
writes them to its workspace on receipt.

```json
{
  "type": "task.assign",
  "payload": {
    "prompt": "Fix the bug in this function",
    "context_files": [
      { "path": "src/api/routes.py", "content": "..." },
      { "path": "tests/test_routes.py", "content": "..." }
    ]
  }
}
```

**When to use:** Targeted tasks (fix this file, review this diff) where
only a few files are relevant. No git clone or upload overhead.

**Characteristics:**
- Zero filesystem round-trips — files arrive with the task message.
- Subject to NMB payload size limit (10 MB). For larger payloads, fall back
  to `openshell sandbox upload` + NMB signaling.
- No git history — the agent works on standalone files. Diffs are computed
  against the seeded content.

#### Mechanism Comparison

| | OpenShell Upload | Git Clone | NMB Context Files |
|---|---|---|---|
| **Latency** | ~1-3s (small), ~5-15s (large) | ~5-30s | ~50ms (inline with task.assign) |
| **Max size** | Unlimited (file transfer) | Unlimited (repo) | 10 MB (NMB payload limit) |
| **Git history** | No (unless uploading `.git/`) | Yes (full or shallow) | No |
| **Network policy** | No extra (gateway-local) | Requires git remote access | No extra (NMB only) |
| **Best for** | Partial repos, configs, seeding | Full-repo coding tasks | Small targeted fixes |

#### Combining Mechanisms

In practice, mechanisms are combined. A typical full-repo coding task uses:

1. **Git clone** (mechanism 2) for the project workspace.
2. **OpenShell upload** (mechanism 1) for agent config and skill files.
3. **NMB context files** (mechanism 3) for task-specific instructions or
   snippets that the orchestrator extracted from the conversation.

The setup script handles mechanism 2, the orchestrator runs mechanism 1
before launching the setup script, and mechanism 3 is handled by the agent
process after it starts.

#### OpenShell Download (result collection)

The inverse of upload — `openshell sandbox download` — is used by the
orchestrator to extract artifacts from the sandbox after task completion:

```
Orchestrator                     Gateway               Child Sandbox
    │                               │                       │
    │ openshell sandbox download    │                       │
    │   coding-<wf_id>             │                       │
    │   /sandbox/audit.db          │                       │
    │   /tmp/coding-wf-abc_audit.db│                       │
    │──────────────────────────────▶│◀── read file ─────────│
    │◀──────────────────────────────│                       │
    │                               │                       │
    │ openshell sandbox download    │                       │
    │   coding-<wf_id>             │                       │
    │   /sandbox/artifacts/        │                       │
    │   /tmp/coding-wf-abc_arts/   │                       │
    │──────────────────────────────▶│◀── read dir ──────────│
    │◀──────────────────────────────│                       │
```

Downloads are used for:
- **Audit fallback JSONL** — picked up if NMB-batched flush missed records (see §12).
- **Artifacts** — diffs, patches, logs for result processing and finalization.
- **Git state** — `.git/` directory if the orchestrator needs to push
  the sub-agent's commits (Strategy A in §10.4).

### 5.4 Sandbox Filesystem Layout

```
/sandbox/
├── workspace/              # code workspace (git repo or seeded files)
│   └── ...                 # project files
├── scratchpad.md           # agent working notes (see §8)
├── artifacts/              # output artifacts (diffs, patches, logs)
│   ├── final.diff          # cumulative diff
│   └── summary.md          # task completion summary
├── audit_fallback.jsonl     # fallback audit log (picked up if NMB flush fails)
├── skills/                 # SKILL.md files (placeholder for M5+)
├── memory/                 # memory entries (placeholder for M5+)
│   ├── working/            # working memory (context for this task)
│   ├── agent/              # agent-level conventions/preferences
│   └── user/               # user-level preferences (from orchestrator)
└── .config/                # agent configuration
    └── agent.yaml          # runtime config (model, tools, etc.)
```

### 5.5 Sandbox Cleanup

On `task.complete` (or timeout/error), the orchestrator:

1. Downloads `/sandbox/audit_fallback.jsonl` (if it exists) and ingests any
   tool-call records that weren't delivered via NMB-batched flush during the
   task. See [Audit DB Design §7](audit_db_design.md#7--open-question-sub-agent-tool-call-auditing)
   for the full audit delivery design.
2. Downloads `/sandbox/artifacts/` for result processing.
3. Reads scratchpad contents from the `task.complete` payload.
4. Calls `openshell sandbox delete coding-<wf_id>`.

Cleanup is **mandatory** — a watchdog TTL (default: 30 minutes) ensures
sandboxes are garbage-collected even if the orchestrator crashes before
issuing the delete.

---

## 6  Agent Setup: Policy, Tools, and Comms

### 6.1 The Agent Configuration Bundle

Every agent (orchestrator or sub-agent) is initialized with a configuration
bundle. The bundle is the same structure for all agent types; only the contents
differ.

```python
@dataclass
class AgentSetupBundle:
    """Everything needed to initialize an agent in any environment."""

    # Identity
    role: str                              # "orchestrator", "coding", "review", ...
    sandbox_id: str                        # unique sandbox identifier

    # Agent loop
    system_prompt: str                     # role-specific system prompt
    model: str                             # model identifier for inference
    temperature: float = 0.0
    max_tokens: int = 16384
    max_tool_rounds: int = 10

    # Tools
    tool_surface: list[str] | None = None  # allowed tool names (None = all)
    tool_definitions: list[dict] | None = None  # full tool defs (for sub-agents)

    # Comms
    nmb_broker_url: str = "ws://messages.local:9876"
    parent_sandbox_id: str | None = None
    workflow_id: str | None = None

    # Workspace
    workspace_root: str = "/sandbox/workspace"
    scratchpad_path: str = "/sandbox/scratchpad.md"
    audit_fallback_path: str = "/sandbox/audit_fallback.jsonl"

    # Placeholders for future milestones
    skills_dir: str = "/sandbox/skills"       # M5: skill files
    memory_dir: str = "/sandbox/memory"       # M5+: seeded memory
    context_files: list[dict] | None = None   # inline context from orchestrator
```

### 6.2 Policy: OpenShell Sandbox Policy per Agent Role

Each agent role has an OpenShell policy file that declares its permissions.
The orchestrator generates or selects the policy at spawn time.

#### Coding Agent Policy (`policies/coding-agent.yaml`)

```yaml
version: 1

run_as_user: sandbox

network_policies:
  inference:
    name: inference-backend
    endpoints:
      - host: inference.local
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/bin/python3

  nmb:
    name: nemoclaw-message-bus
    endpoints:
      - host: messages.local
        port: 9876
        protocol: rest
        tls: terminate
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/local/bin/nmb-client
      - path: /usr/bin/python3

  # No external network access by default.
  # Task-specific policies can add endpoints (e.g., npm registry,
  # PyPI, GitHub API) based on the task.assign payload.
```

#### Policy Selection at Spawn Time

```python
def select_policy(role: str, task_payload: dict) -> str:
    base = f"policies/{role}-agent.yaml"
    # Future: merge task-specific policy overrides
    # (e.g., add npm registry access for a Node.js task)
    return base
```

### 6.3 Policy Hot-Reload

A sub-agent's initial policy is set at spawn time, but real-world coding tasks
frequently discover they need additional permissions mid-execution. Common
scenarios:

- The agent runs `pip install` to test code but the policy blocks PyPI access.
- The agent needs to fetch a schema definition from an internal API.
- The agent needs git remote access to push a branch (see §10.4).

Rather than requiring sandbox teardown and recreation, the orchestrator can
**hot-reload** the sub-agent's policy using the OpenShell CLI:

```
openshell policy set coding-<wf_id> --policy <updated_policy_file> --wait
```

The `--wait` flag blocks until the policy engine has applied the new rules.
Existing connections are not dropped; new rules take effect on the next
outbound request.

#### 6.3.1 Request Flow

The sub-agent cannot update its own policy (no gateway access in its network
policy). Policy changes are always orchestrator-initiated:

```
Sub-Agent                     NMB                    Orchestrator           Gateway
    │                          │                          │                    │
    │  bash: pip install ...   │                          │                    │
    │  → blocked by policy     │                          │                    │
    │                          │                          │                    │
    │  task.error / task.clarify                          │                    │
    │  "pip install failed:    │                          │                    │
    │   network policy blocks  │                          │                    │
    │   pypi.org"              │                          │                    │
    │─────────────────────────▶│─────────────────────────▶│                    │
    │                          │                          │                    │
    │                          │        Orchestrator evaluates request         │
    │                          │        (auto-approve or ask user)             │
    │                          │                          │                    │
    │                          │                          │ policy set         │
    │                          │                          │ --policy updated   │
    │                          │                          │ --wait             │
    │                          │                          │───────────────────▶│
    │                          │                          │◀───────────────────│
    │                          │                          │ policy applied     │
    │                          │                          │                    │
    │  policy.updated          │                          │                    │
    │  { added: ["pypi.org"] } │                          │                    │
    │◀─────────────────────────│◀─────────────────────────│                    │
    │                          │                          │                    │
    │  retry: pip install ...  │                          │                    │
    │  → succeeds              │                          │                    │
```

#### 6.3.2 NMB Message Types for Policy Requests

| Type | Direction | Payload |
|------|-----------|---------|
| `policy.request` | Sub-Agent → Orchestrator | `{ reason, endpoint?, tool?, error_message? }` |
| `policy.updated` | Orchestrator → Sub-Agent | `{ added_endpoints[], removed_endpoints[]?, revision }` |
| `policy.denied` | Orchestrator → Sub-Agent | `{ reason }` |

The `policy.request` message is sent by the sub-agent when a tool call fails
due to a policy restriction. The orchestrator evaluates the request:

- **Auto-approve** for known-safe endpoints that match the task context
  (e.g., PyPI for a Python project, npm registry for a Node.js project).
- **Escalate to user** for unknown endpoints or sensitive resources (same
  Approve/Deny button flow used for write tool approval).
- **Deny** for endpoints that violate the sandbox's security boundary.

#### 6.3.3 Policy Overlay Model

Rather than replacing the entire policy file, the orchestrator maintains a
**base policy** (role-specific) plus **overlay entries** added during the task.
The overlay is merged at each hot-reload:

```python
@dataclass
class PolicyOverlay:
    sandbox_id: str
    base_policy_path: str
    overlays: list[PolicyEntry] = field(default_factory=list)

    def add_endpoint(self, name: str, host: str, port: int, **kwargs):
        self.overlays.append(PolicyEntry(name=name, host=host, port=port, **kwargs))

    def render(self) -> str:
        """Merge base policy + overlays into a complete policy YAML."""
        ...

    async def apply(self):
        """Write merged policy to temp file and hot-reload via OpenShell CLI."""
        merged_path = self._write_temp_policy()
        await openshell_policy_set(self.sandbox_id, merged_path)
```

#### 6.3.4 Tool Surface Hot-Reload

Policy hot-reload covers the network/filesystem layer. For the **tool surface**
(which tools the agent's `ToolRegistry` exposes), the orchestrator can send a
`config.update` NMB message that tells the agent to load additional tools:

| Type | Direction | Payload |
|------|-----------|---------|
| `config.update` | Orchestrator → Sub-Agent | `{ add_tools?: [...], remove_tools?: [...], add_env?: {...} }` |
| `config.ack` | Sub-Agent → Orchestrator | `{ applied: true, active_tools: [...] }` |

The sub-agent's `AgentLoop` reloads its `ToolRegistry` on receipt. This is
less common than policy updates — the initial `tool_surface` in `task.assign`
should cover most cases — but is needed when the orchestrator discovers
mid-task that the agent needs a tool it wasn't initially given.

#### 6.3.5 Audit Trail

Every policy change is logged:

- The audit DB records the policy update (sandbox, before/after endpoints,
  trigger reason, who approved) in the `tool_calls` table.
- The same audit DB records the `policy.request` / `policy.updated` /
  `policy.denied` NMB message exchange in the `messages` table.
- OpenShell's own policy revision history is queryable via
  `openshell policy list <name>`.

### 6.4 Tools: Declarative Tool Surface

The orchestrator declares which tools a sub-agent receives via the
`tool_surface` field in `task.assign`. This serves two purposes:

1. **Agent-level filtering** — the sub-agent's `ToolRegistry` only loads tools
   in the `tool_surface` list.
2. **Policy-level enforcement** — the sandbox policy blocks system calls and
   network access that would be needed by tools not in the surface.

#### Tool Surface Examples

| Agent Role | Tool Surface |
|-----------|-------------|
| **Coding** | `bash`, `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `git_diff`, `git_commit`, `scratchpad_write`, `scratchpad_read` |
| **Review** | `read_file`, `grep`, `glob`, `git_diff`, `scratchpad_write`, `scratchpad_read` |
| **Research** | `web_search`, `web_fetch`, `read_file`, `scratchpad_write`, `scratchpad_read` |

Sub-agents **cannot** use delegation tools (`delegate_task`,
`openshell_sandbox_create`) unless explicitly granted. This prevents
unbounded recursive spawning and is enforced at both the tool registry level
(tool not loaded) and the OpenShell policy level (gateway access blocked).

### 6.5 Comms: NMB Setup in Sub-Agents

Every sub-agent connects to NMB on startup and registers with its sandbox ID.
The NMB client is initialized from environment variables set by the sandbox
setup script:

```python
bus = MessageBus(
    url=os.environ.get("NMB_BROKER_URL", "ws://messages.local:9876"),
    sandbox_id=os.environ.get("NMB_SANDBOX_ID", socket.gethostname()),
)
await bus.connect()
await bus.publish("system", "sandbox.ready", {
    "sandbox_id": bus.sandbox_id,
    "role": agent_role,
    "capabilities": list(tool_registry.tool_names()),
})
```

The orchestrator subscribes to `sandbox.ready` events and waits for the
sub-agent to signal readiness before sending `task.assign`. This handshake
ensures the agent's loop, tools, and NMB client are fully initialized.

---

## 7  Coding Agent File Tools

The coding agent's primary capability is operating on files in its workspace.
M2 ships a concrete set of file tools that the `AgentLoop` uses via the
`ToolRegistry`. These are the tools that make a coding agent a *coding* agent.

### 7.1 Tool Catalog

| Tool | Mode | Description |
|------|------|-------------|
| `read_file` | READ | Read a file from the workspace. Supports line-range selection for large files. |
| `write_file` | WRITE | Create or overwrite a file with new contents. |
| `edit_file` | WRITE | Apply a targeted edit to a file via old/new string replacement. Preferred over `write_file` for surgical changes — reduces diff noise and avoids clobbering concurrent changes. |
| `list_directory` | READ | List files and directories at a given path. |
| `grep` | READ | Search file contents by regex pattern. Returns matching lines with file paths and line numbers. |
| `glob` | READ | Find files matching a glob pattern (e.g., `**/*.py`). |
| `bash` | WRITE | Execute a shell command in the workspace. Used for running tests, installing dependencies, invoking build tools, etc. Commands run in the workspace root with a configurable timeout. |
| `git_diff` | READ | Show uncommitted changes in the workspace (`git diff`). |
| `git_commit` | WRITE | Stage and commit changes with a message. |
| `git_log` | READ | Show recent commit history. |
| `scratchpad_read` | READ | Read the agent's scratchpad (see §8). |
| `scratchpad_write` | WRITE | Overwrite the scratchpad with new content. |
| `scratchpad_append` | WRITE | Append content under a named scratchpad section. |

### 7.2 Tool Implementation Strategy

Tools are implemented as thin Python wrappers that operate on the sandbox
filesystem. They share common patterns:

```python
@register(
    name="read_file",
    toolset="files",
    description="Read a text file from the workspace.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root"},
            "offset": {"type": "integer", "description": "Start line (1-indexed, optional)"},
            "limit": {"type": "integer", "description": "Max lines to return (optional)"},
        },
        "required": ["path"],
    },
    is_read_only=True,
)
async def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    ...
```

Key design decisions:

- **Workspace-rooted paths** — all file tools resolve paths relative to the
  workspace root (`/sandbox/workspace/`). Absolute paths and `..` traversals
  are rejected. This prevents the agent from reading or modifying files outside
  its workspace (audit DB, NMB config, etc.).
- **Output truncation** — large file reads and grep results are truncated to a
  configurable limit (default: 200 lines / 32 KB) to prevent context window
  blowup. The tool output indicates when truncation occurred.
- **`edit_file` over `write_file`** — the system prompt instructs the model to
  prefer `edit_file` for modifications. This produces cleaner diffs and reduces
  the risk of accidentally overwriting large files.
- **`bash` safety** — the bash tool runs commands in a subprocess with a
  timeout (default: 120s). The sandbox policy provides the real security
  boundary (no network access, no privilege escalation). The tool captures
  both stdout and stderr, truncating combined output at 64 KB.

### 7.3 Tool Registry Factory

Each agent role has a factory that creates a `ToolRegistry` with the
appropriate tools loaded:

```python
def create_coding_tool_registry(workspace_root: str) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(read_file_tool(workspace_root))
    registry.register(write_file_tool(workspace_root))
    registry.register(edit_file_tool(workspace_root))
    registry.register(list_directory_tool(workspace_root))
    registry.register(grep_tool(workspace_root))
    registry.register(glob_tool(workspace_root))
    registry.register(bash_tool(workspace_root))
    registry.register(git_diff_tool(workspace_root))
    registry.register(git_commit_tool(workspace_root))
    registry.register(git_log_tool(workspace_root))

    return registry
```

The scratchpad tools are registered separately by the `AgentLoop` when a
`Scratchpad` instance is provided (see §8).

### 7.4 Relationship to Claude Code / OpenClaw Tools

The M2 file tools are intentionally minimal — they cover the core operations
needed for coding tasks without importing the full complexity of Claude Code's
40+ tools or OpenClaw Pi's tool profiles. The design is forward-compatible:

| M2 Tool | Claude Code Equivalent | OpenClaw Pi Equivalent |
|---------|----------------------|----------------------|
| `read_file` | `Read` | `read-file-tool` |
| `write_file` | `Write` | `write-file-tool` |
| `edit_file` | `Edit` (search/replace) | `apply_patch` |
| `grep` | `Grep` (ripgrep) | built-in grep |
| `glob` | `Glob` | built-in glob |
| `bash` | `Bash` | `execute-command-tool` |
| `git_diff` / `git_commit` | Git via `Bash` | Git via `execute-command-tool` |

Future milestones may add: `undo_edit` (checkpoint-based rollback),
`multi_edit` (batch edits across files), `web_search` / `web_fetch`
(for research agents), and MCP-bridged external tools.

---

## 8  Agent Scratchpad

### 7.1 Purpose

The scratchpad is a sub-agent's working memory for the current task. It serves
as a structured place for the agent to:

- Record observations and intermediate findings
- Track its plan and progress
- Note open questions for the orchestrator
- Document decisions and rationale

The scratchpad is **returned to the orchestrator** on task completion as part of
the `task.complete` payload. This gives the orchestrator visibility into the
sub-agent's reasoning process, not just the final output.

### 7.2 Implementation

The scratchpad is a Markdown file on the sandbox filesystem. Two tools expose
it to the agent loop:

```python
@dataclass
class Scratchpad:
    path: str
    max_size: int = 32_768  # 32 KB cap to prevent context blowup

    def read(self) -> str:
        """Read current scratchpad contents."""
        ...

    def write(self, content: str) -> str:
        """Overwrite scratchpad with new content."""
        ...

    def append(self, section: str, content: str) -> str:
        """Append content under a named section header."""
        ...

    def snapshot(self) -> str:
        """Return contents for inclusion in task.complete payload."""
        ...
```

#### Scratchpad Tools (registered in the sub-agent's `ToolRegistry`)

| Tool | Mode | Description |
|------|------|-------------|
| `scratchpad_read` | READ | Read the current scratchpad contents |
| `scratchpad_write` | WRITE | Overwrite scratchpad with new content |
| `scratchpad_append` | WRITE | Append content under a section header |

### 7.3 Scratchpad as Context

The `AgentLoop` optionally injects the scratchpad into the model's context.
When a `Scratchpad` instance is provided, its contents are appended to the
system prompt as a dynamic section:

```
<scratchpad>
# Agent Scratchpad

## Task Notes
- The user wants feature X implemented in src/api/routes.py
- Existing tests in tests/test_routes.py cover the current behavior

## Observations
- The routes module uses FastAPI with dependency injection
- Auth middleware is applied globally in main.py

## Open Questions
- Should the new endpoint require admin privileges?
</scratchpad>
```

This injection happens on every inference round, so the model always has
access to its latest notes. The scratchpad is kept within `max_size` bytes
to prevent context window bloat.

### 7.4 Scratchpad Return to Orchestrator

On `task.complete`, the scratchpad contents are included in the payload:

```json
{
  "type": "task.complete",
  "payload": {
    "result": "Implemented feature X. See diff below.",
    "scratchpad": "## Task Notes\n- Implemented in src/api/routes.py\n...",
    "diff": "--- a/src/api/routes.py\n+++ b/src/api/routes.py\n...",
    "files_changed": ["src/api/routes.py", "tests/test_routes.py"],
    "summary": "Added /api/feature-x endpoint with admin auth check",
    "tool_calls_made": 12
  }
}
```

The orchestrator can use the scratchpad contents for:

- Understanding the sub-agent's reasoning chain
- Feeding context to a review agent
- Generating user-facing summaries
- Training data (the scratchpad is a structured trace of agent reasoning)

---

## 9  Orchestrator ↔ Sub-Agent Protocol

### 8.1 Message Flow

The orchestrator communicates with sub-agents exclusively through NMB. The
protocol builds on the message types defined in
[NMB Design §6](nmb_design.md#6--message-types-nemoclaw-agent-protocol) and
adds the setup-specific fields defined in this milestone.

#### `task.assign` (Orchestrator → Sub-Agent)

```json
{
  "op": "send",
  "to": "coding-wf-abc123",
  "type": "task.assign",
  "payload": {
    "prompt": "Implement the /api/users endpoint with CRUD operations",
    "context_files": [
      { "path": "src/api/routes.py", "content": "..." },
      { "path": "src/models/user.py", "content": "..." }
    ],
    "tool_surface": ["bash", "read_file", "write_file", "edit_file",
                     "grep", "glob", "git_diff", "git_commit",
                     "scratchpad_write", "scratchpad_read"],
    "constraints": {
      "max_tool_rounds": 20,
      "must_include_tests": true,
      "git_commit_on_success": true
    },
    "workspace_snapshot": null,
    "workflow_id": "wf-abc123",
    "parent_sandbox_id": "orchestrator-main"
  }
}
```

#### `task.progress` (Sub-Agent → Orchestrator)

Sent periodically so the orchestrator (and ultimately the user) can see what
the sub-agent is doing.

```json
{
  "op": "publish",
  "channel": "progress.coding-wf-abc123",
  "type": "task.progress",
  "payload": {
    "status": "writing_code",
    "pct": 45,
    "tool_output": "Created src/api/users.py with 4 endpoints",
    "current_round": 6,
    "tokens_used": 12400
  }
}
```

#### `task.complete` (Sub-Agent → Orchestrator)

```json
{
  "op": "reply",
  "reply_to": "original-task-assign-id",
  "type": "task.complete",
  "payload": {
    "result": "Implemented /api/users endpoint with full CRUD.",
    "scratchpad": "## Task Notes\n- Used SQLAlchemy ORM for User model\n...",
    "diff": "--- a/src/api/users.py\n+++ b/src/api/users.py\n@@ ...",
    "files_changed": ["src/api/users.py", "src/models/user.py",
                      "tests/test_users.py"],
    "summary": "4 endpoints (GET/POST/PUT/DELETE), 8 tests, all passing",
    "tool_calls_made": 15,
    "git_commit_sha": "a1b2c3d"
  }
}
```

#### `task.error` (Sub-Agent → Orchestrator)

```json
{
  "op": "reply",
  "reply_to": "original-task-assign-id",
  "type": "task.error",
  "payload": {
    "error": "Failed to install dependencies: pip timeout",
    "scratchpad": "## Observations\n- Network policy blocks PyPI access\n...",
    "traceback": "...",
    "recoverable": true
  }
}
```

### 8.2 Sub-Agent Entrypoint

With the three-layer architecture (§4.7), the sub-agent entrypoint is a
one-liner that instantiates the appropriate `Agent` subclass:

```python
# nemoclaw_escapades/agent/__main__.py

import asyncio
from nemoclaw_escapades.agent.coding import CodingAgent
from nemoclaw_escapades.agent.review import ReviewAgent
from nemoclaw_escapades.agent.types import AgentSetupBundle

AGENT_CLASSES = {
    "coding": CodingAgent,
    "review": ReviewAgent,
}

async def main(role: str, workspace: str, scratchpad_path: str):
    config = AgentSetupBundle(
        role=role,
        sandbox_id=os.environ.get("NMB_SANDBOX_ID", socket.gethostname()),
        workspace_root=workspace,
        scratchpad_path=scratchpad_path,
        **load_agent_config(role),
    )

    agent_cls = AGENT_CLASSES[role]
    agent = agent_cls(config)
    try:
        await agent.start()
    finally:
        await agent.shutdown()
```

All the boilerplate that was previously in `main()` — creating the
`MessageBus`, `AgentLoop`, `AuditBuffer`, `Scratchpad`, wiring them together,
running the event loop — now lives in the `Agent` base class (§4.8) and the
role-specific subclass (`CodingAgent`, `ReviewAgent`). The entrypoint only
needs to pick the right class and pass configuration.

The orchestrator uses the same pattern but with `OrchestratorAgent`:

```python
# nemoclaw_escapades/main.py (simplified)

async def main():
    config = load_orchestrator_config()
    agent = OrchestratorAgent(config)
    await agent.start()  # runs dual Slack + NMB loops
```

---

## 10  Work Collection and Finalization

When a sub-agent completes a task, the orchestrator must collect the work,
present it to the user, and finalize it (commit, push, create PR, or discard).
This is an orchestrator-side capability — the sub-agent produces artifacts; the
orchestrator decides what to do with them.

### 10.1 Collection Flow

```
 Sub-Agent                  Orchestrator                        User (Slack)
     │                           │                                  │
     │  task.complete            │                                  │
     │  { diff, scratchpad,      │                                  │
     │    files_changed,         │                                  │
     │    summary, commit_sha }  │                                  │
     │──────────────────────────▶│                                  │
     │                           │                                  │
     │                           │ 1. Pick up audit fallback file   │
     │                           │ 2. Download /sandbox/artifacts/  │
     │                           │ 3. Parse task.complete payload   │
     │                           │                                  │
     │                           │ 4. Present summary to user       │
     │                           │──────────────────────────────────▶│
     │                           │  "Coding agent finished:          │
     │                           │   - Added /api/health endpoint    │
     │                           │   - 3 files changed, 8 tests     │
     │                           │   [View Diff] [Commit] [Discard]"│
     │                           │                                  │
     │                           │  5. User clicks [Commit]         │
     │                           │◀──────────────────────────────────│
     │                           │                                  │
     │                           │ 6. Push branch, create PR        │
     │                           │──────────────────────────────────▶│
     │                           │  "PR #42 created: ..."           │
     │                           │                                  │
     │                           │ 7. Destroy sandbox               │
```

### 10.2 Finalization Actions

The orchestrator supports several finalization actions, triggered either by
user choice (Slack buttons) or by constraints in the original `task.assign`:

| Action | Trigger | What happens |
|--------|---------|-------------|
| **Auto-commit** | `constraints.git_commit_on_success = true` in `task.assign` | Sub-agent commits in its workspace before sending `task.complete`. Orchestrator receives the `commit_sha`. |
| **Present for review** | Default behavior | Orchestrator formats the diff and summary, sends to user with action buttons. |
| **Push + PR** | User clicks [Commit] or [Push & PR] | Orchestrator pushes the sub-agent's branch to the remote and optionally creates a PR (via GitHub/GitLab API tools). |
| **Push only** | User clicks [Push] | Orchestrator pushes the branch without creating a PR. |
| **Discard** | User clicks [Discard] | Orchestrator destroys the sandbox without pushing. Diff is preserved in audit for training data. |
| **Iterate** | User provides feedback | Orchestrator sends a new `task.assign` to the *same* sandbox with the feedback as additional context. Sandbox is kept alive. |

### 10.3 Finalization Tools (Model-Driven)

Work finalization is **not** a hardcoded imperative flow. Instead, finalization
actions are tools in the orchestrator's `ToolRegistry`. When a sub-agent sends
`task.complete`, the orchestrator's NMB event loop (§10.7) feeds the result
into the orchestrator's own `AgentLoop`. The model sees the completed work
and decides what to do — present it to the user, push immediately, iterate,
or discard — by calling finalization tools.

This design means the orchestrator's LLM can reason about results: inspect
the diff, notice failing tests, rephrase user feedback as a coherent
re-delegation prompt, or generate a context-aware summary. Deterministic
parts (artifact download, audit fallback ingest, sandbox cleanup) run as
side effects inside the tool implementations.

#### Finalization Tool Catalog

| Tool | Mode | Description |
|------|------|-------------|
| `present_work_to_user` | WRITE | Format a diff/summary and send a Slack message with action buttons (Push & PR, Push, Iterate, Discard). Internally downloads artifacts from the sandbox before rendering. |
| `push_and_create_pr` | WRITE | Download git state from sandbox, push branch to remote, create a PR via GitHub/GitLab API. Returns the PR URL. |
| `push_branch` | WRITE | Push the sub-agent's branch without creating a PR. |
| `discard_work` | WRITE | Destroy the sandbox. Notify the user that work was discarded. The diff is preserved in audit for training data. |
| `re_delegate` | WRITE | Send a new `task.assign` to the same sandbox with an updated prompt (e.g., incorporating user feedback). Keeps the sandbox alive. |
| `destroy_sandbox` | WRITE | Low-level sandbox cleanup. Called by other finalization tools or the TTL watchdog. |

All finalization tools are classified as WRITE and go through the orchestrator's
approval gate. In practice, `present_work_to_user` is auto-approved (it only
sends a Slack message), while `push_and_create_pr` may require user
confirmation depending on the approval policy.

#### Example: Model-Driven Finalization

When `task.complete` arrives, the model sees this context:

```
The coding agent completed the task you delegated.

Summary: Added /api/health endpoint with uptime tracking.
Files changed: src/api/routes.py, src/api/health.py (new), tests/test_health.py (new)
Tests: 8 passing
Diff: [truncated diff]
Scratchpad: [agent's working notes]
Git commit: a1b2c3d

Use the finalization tools to present this to the user, push the code, iterate, or discard.
```

The model then calls `present_work_to_user` to show the result with action
buttons. When the user clicks a button (e.g., [Push & PR]), the button click
arrives as a new event, the model sees it, and calls `push_and_create_pr`.

If the model notices the tests are failing (from the scratchpad or diff), it
can proactively call `re_delegate` with a fix prompt — no user intervention
needed.

### 10.4 Git Operations for Finalization

The orchestrator needs to move code from the sub-agent's sandbox to a git
remote. Two strategies:

**Strategy A: Orchestrator pulls and pushes (default)**

The orchestrator downloads the sub-agent's git state and pushes from its own
sandbox:

1. `openshell sandbox download <child> /sandbox/workspace/.git /tmp/<child>.git`
2. Orchestrator applies the commits to its own checkout of the repo.
3. `git push origin <branch>` from orchestrator sandbox.
4. Create PR via GitHub/GitLab API tool (already in orchestrator enterprise
   tool surface).

This keeps the coding sandbox fully network-isolated — it never needs git
remote access.

**Strategy B: Sub-agent pushes directly**

If the coding sandbox has git remote access in its policy, the sub-agent can
push its own branch. The orchestrator only creates the PR. This is simpler but
widens the coding sandbox's network surface.

**Recommendation:** Start with Strategy A for maximum isolation. Move to
Strategy B when sandbox policy tooling is mature enough to scope git access
narrowly (e.g., push to a single repo only).

### 10.5 Iteration Without Sandbox Teardown

When the user clicks [Iterate] and provides feedback, the orchestrator keeps
the sandbox alive and sends a new `task.assign`:

```json
{
  "type": "task.assign",
  "payload": {
    "prompt": "The user reviewed your work and has feedback:\n\n> Please also add rate limiting to the /api/health endpoint.\n\nPlease address this feedback. Your previous scratchpad and workspace state are preserved.",
    "is_iteration": true,
    "iteration_number": 2
  }
}
```

The sub-agent picks this up in its listen loop and runs another `AgentLoop`
cycle with its existing workspace and scratchpad intact. This avoids the
overhead of sandbox creation and git clone for incremental refinements.

### 10.6 User-Facing Slack Rendering

The orchestrator renders finalization prompts as Slack Block Kit messages:

```
┌──────────────────────────────────────────────────────┐
│ ✅ Coding agent completed task                        │
│                                                      │
│ *Summary:* Added /api/health endpoint with uptime    │
│ tracking and rate limiting.                          │
│                                                      │
│ *Files changed:* 3                                   │
│ • src/api/routes.py                                  │
│ • src/api/health.py (new)                            │
│ • tests/test_health.py (new)                         │
│                                                      │
│ *Tests:* 8 passing                                   │
│                                                      │
│ ```diff                                              │
│ + @app.route("/api/health")                          │
│ + def health():                                      │
│ +     return {"status": "ok", "uptime": get_uptime()}│
│ ```                                                  │
│                                                      │
│ [Push & PR]  [Push Only]  [Iterate]  [Discard]       │
└──────────────────────────────────────────────────────┘
```

This reuses the existing `RichResponse` / `ActionBlock` / `ActionButton`
types from the approval gate system — the same Approve/Deny pattern
extended with work-finalization actions. The `present_work_to_user` tool
generates this rendering internally.

### 10.7 NMB Event Loop and Concurrency Model

The orchestrator must remain responsive to user messages while handling
sub-agent events (task completions, progress updates, policy requests)
concurrently. This requires two independent event loops running in the
same process:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Orchestrator Process                                                │
│                                                                      │
│  ┌──────────────────────────┐  ┌──────────────────────────────────┐  │
│  │  Slack Request Loop      │  │  NMB Event Loop                  │  │
│  │                          │  │                                  │  │
│  │  for each Slack message: │  │  for each NMB message:           │  │
│  │    await handle(request) │  │    match msg.type:               │  │
│  │                          │  │      task.complete → asyncio     │  │
│  │  Blocking per-thread,    │  │        .create_task(finalize)    │  │
│  │  concurrent across       │  │      task.error → asyncio       │  │
│  │  threads.                │  │        .create_task(finalize)    │  │
│  │                          │  │      task.progress → relay       │  │
│  │                          │  │      policy.request → asyncio    │  │
│  │                          │  │        .create_task(handle)      │  │
│  │                          │  │      audit.flush → ingest        │  │
│  └──────────────────────────┘  └──────────────────────────────────┘  │
│                                                                      │
│  Both loops share:                                                   │
│  • AgentLoop instance (stateless — safe for concurrent use)          │
│  • ToolRegistry (read-only tool definitions)                         │
│  • AuditDB (async-safe, WAL mode)                                    │
│  • PromptBuilder (per-thread/per-workflow history, dict-keyed)        │
│                                                                      │
│  Isolation:                                                          │
│  • Each Slack thread has its own conversation history                 │
│  • Each workflow has its own conversation history                     │
│  • AgentLoop.run() is stateless — concurrent calls are safe          │
│  • No shared mutable state between finalization tasks                 │
└──────────────────────────────────────────────────────────────────────┘
```

#### NMB Event Dispatch

```python
class Orchestrator:

    async def start_nmb_listener(self):
        """Start the NMB event loop alongside the Slack listener."""
        self._bus = MessageBus()
        await self._bus.connect()
        await self._bus.subscribe("system")

        async for msg in self._bus.listen():
            match msg.type:
                case "task.complete" | "task.error":
                    asyncio.create_task(self._finalize_workflow(msg))
                case "task.progress":
                    self._relay_progress_to_slack(msg)
                case "policy.request":
                    asyncio.create_task(self._handle_policy_request(msg))
                case "audit.flush":
                    await self._ingest_audit_batch(msg)
                case "sandbox.ready":
                    self._on_sandbox_ready(msg)

    async def _finalize_workflow(self, msg: NMBMessage):
        """Run model-driven finalization as an independent async task."""
        workflow = self._workflows[msg.payload["workflow_id"]]

        messages = self._build_finalization_context(workflow, msg)
        result = await self._agent_loop.run(
            messages, request_id=msg.id
        )
        # The model called finalization tools during the loop
        # (present_work_to_user, push_and_create_pr, etc.)

        if workflow.slack_thread_ts:
            self._prompt.commit_turn(
                workflow.slack_thread_ts,
                f"[sub-agent completed: {workflow.role}]",
                result.content,
            )
```

#### Why `asyncio.create_task` and Not `await`

Each finalization flow may involve:
- An inference call (model reasons about the result)
- Tool execution (download artifacts, push git, send Slack message)
- Waiting for user action (button click on finalization prompt)

If `_finalize_workflow` were awaited directly in the NMB listener loop,
subsequent NMB messages would be queued until finalization completes. By
spawning an `asyncio.Task`, the listener immediately returns to processing
the next message. Multiple finalization flows run concurrently.

#### Concurrency Guarantees

| Scenario | Behavior |
|----------|----------|
| Two sub-agents complete simultaneously | Two independent `_finalize_workflow` tasks run concurrently. Each has its own workflow context. |
| User sends a Slack message while finalization is running | Slack `handle()` runs independently. The user's message is processed immediately. |
| User clicks [Push & PR] button while another finalization is in progress | Button click arrives as a Slack `ActionPayload`. Handled by the Slack loop, not the NMB loop. The finalization tool runs in the Slack thread's context. |
| `task.progress` arrives during finalization | Progress is relayed to Slack immediately (synchronous in the NMB loop). Does not block or interfere with the finalization task. |
| Orchestrator model calls `re_delegate` during finalization | Sends `task.assign` via NMB. A future `task.complete` will spawn a new finalization task. |

---

## 11  Preparing for Skills and Memory (M5+)

M2 does not implement skills or memory, but the sandbox setup contract must
accommodate them without redesign.

### 9.1 Skills (M5)

Skills are `SKILL.md` files that define reusable workflows. In M5, the
orchestrator will:

1. Select relevant skills based on the task
2. Upload them to `/sandbox/skills/` before `task.assign`
3. The agent's system prompt includes a skills inventory section
4. The agent can invoke skills via a `skill_run` tool

**M2 preparation:** The sandbox filesystem layout includes `/sandbox/skills/`
and the `AgentSetupBundle` has a `skills_dir` field. No skill loading logic
is implemented yet, but the directory and config plumbing are in place.

### 9.2 Memory (M5+)

Memory is structured agent knowledge that persists across sessions. In M5+, the
orchestrator will:

1. Seed the sub-agent's `/sandbox/memory/` with relevant entries:
   - `working/` — task-specific context (from the orchestrator's conversation)
   - `agent/` — agent-level conventions (coding style, project norms)
   - `user/` — user preferences (from Honcho / persistent memory store)
2. The agent's system prompt includes a memory section
3. The agent can read/write memory via `memory_read` / `memory_write` tools
4. On task completion, new memory entries are returned to the orchestrator for
   persistence

**M2 preparation:** The sandbox filesystem layout includes `/sandbox/memory/`
with the three subdirectories. The `AgentSetupBundle` has a `memory_dir` field.
No memory loading logic is implemented yet.

### 9.3 Design Contract for Future Integration

| Concern | M2 (current) | M5+ (future) |
|---------|-------------|-------------|
| **Skills** | Empty `/sandbox/skills/` directory | Orchestrator uploads relevant `SKILL.md` files; agent has `skill_run` tool |
| **Working memory** | Scratchpad only (ephemeral) | `/sandbox/memory/working/` seeded with task context; returned on completion |
| **Agent memory** | System prompt only | `/sandbox/memory/agent/` seeded with conventions; `memory_write` persists new learnings |
| **User memory** | Not available | `/sandbox/memory/user/` seeded from Honcho; read-only in sub-agent |
| **Artifacts** | Diffs and patches returned via NMB | Same, plus: artifacts indexed for knowledge base; scratchpad entries feed memory extraction |

The key invariant: **the orchestrator always controls what enters and exits a
sub-agent's sandbox**. Skills and memory are seeded by the orchestrator, and
new entries must be returned through the `task.complete` payload for the
orchestrator to persist. Sub-agents never have direct access to the central
memory store.

---

## 12  Audit and Observability

### 12.1 Sub-Agent Audit: NMB-Batched Flush

Sub-agent tool-call auditing follows the recommendation from
[Audit DB Design §7.3](audit_db_design.md#73--recommendation): **Option C
(NMB-batched flush) for the primary path, with JSONL fallback.**

The sub-agent audit delivery mechanism uses **Option C (NMB-batched flush)**
as recommended in
[Audit DB Design §7.3](audit_db_design.md#73--recommendation). Both tool-call
and inference-call records are flushed in the same `audit.flush` NMB message
(see [Inference Call Auditing §4.2](inference_call_auditing_design.md#42--sub-agent-calls-nmb-batched-flush)).
The mechanism works as follows:

- **Primary path:** Sub-agents accumulate tool-call records in a lightweight
  in-memory buffer (plain dicts, no SQLite). At natural boundaries (end of each
  agent-loop round, task completion, or buffer size threshold), the buffer is
  flushed to the orchestrator via an `audit.flush` NMB message. The
  orchestrator writes the records into its central audit DB with the child's
  `sandbox_id` as provenance.
- **Fallback:** If NMB is unavailable, the sub-agent appends records to a local
  `/sandbox/audit_fallback.jsonl` file. On task completion, the orchestrator
  downloads this file via `openshell sandbox download` and ingests any records
  that weren't delivered via NMB.

This keeps child sandbox images lighter (no Alembic, no SQLite, no audit
module), provides near-real-time audit visibility, and degrades gracefully
when NMB connectivity is interrupted.

### 12.2 Audit Fallback Ingestion on Task Completion

```python
async def _ingest_audit_fallback(self, child_sandbox_id: str):
    local_path = f"/tmp/{child_sandbox_id}_audit_fallback.jsonl"
    try:
        await openshell_download(
            child_sandbox_id,
            "/sandbox/audit_fallback.jsonl",
            local_path,
        )
    except FileNotFoundError:
        return  # no fallback file — all records delivered via NMB

    async with aiofiles.open(local_path) as f:
        async for line in f:
            record = json.loads(line)
            await self._audit.log_tool_call(
                **record,
                source_sandbox=child_sandbox_id,
            )
```

### 12.3 Inference Call Auditing

Inference calls (LLM round-trips) are recorded in a dedicated
`inference_calls` table in the same audit DB, using the same delivery
mechanisms as tool-call auditing:

- **Orchestrator-local:** Written directly after each `backend.complete()`
  call via `AuditDB.log_inference_call()`.
- **Sub-agent:** Buffered alongside tool-call records and flushed in the same
  `audit.flush` NMB message (the payload carries both `tool_calls` and
  `inference_calls` arrays).
- **JSONL fallback:** Each line carries a `"record_type"` discriminator
  (`"inference_call"` or `"tool_call"`) so the orchestrator routes to the
  correct ingestion method.

Together, `tool_calls` + `inference_calls` + NMB `messages` give a complete,
queryable agent trace for any session. See
[Inference Call Auditing Design](inference_call_auditing_design.md) for the
full schema, API, and migration details.

### 12.4 Structured Logging

All agent loop events emit structured logs with consistent fields:

| Field | Description |
|-------|-------------|
| `request_id` | Correlation ID (from `task.assign` message ID) |
| `sandbox_id` | Which sandbox this log is from |
| `workflow_id` | Top-level workflow identifier |
| `role` | Agent role (orchestrator, coding, review) |
| `round` | Agent loop round number |
| `tool` | Tool name (for tool call events) |
| `duration_ms` | Wall-clock time |

### 12.5 Progress Reporting

Sub-agents publish `task.progress` messages to a per-workflow NMB channel. The
orchestrator subscribes and can relay progress to the user via Slack:

```
User: Implement feature X
Bot: 🔨 Working on it...
Bot: [progress] Setting up workspace...
Bot: [progress] Writing src/api/users.py (4 endpoints)...
Bot: [progress] Running tests (8/8 passing)...
Bot: ✅ Done! PR #42 created.
```

---

## 13  End-to-End Walkthrough

A concrete example of the full M2 flow:

### User Request

> "Implement a `/api/health` endpoint that returns `{status: ok}` and
> system uptime. Add it to the Flask app in `src/app.py`."

### Orchestrator Processing

1. Orchestrator receives the Slack message.
2. Orchestrator's `AgentLoop` runs inference. The model decides to delegate
   to a coding agent (via the `delegate_task` tool).
3. Orchestrator creates a sandbox:
   ```
   openshell sandbox create \
     --name coding-wf-7f3a \
     --policy policies/coding-agent.yaml \
     --from nemoclaw-agent:latest
   ```
4. Orchestrator runs workspace setup:
   ```
   openshell sandbox exec coding-wf-7f3a /app/setup-workspace.sh
   ```
   Environment variables set: `GIT_REPO_URL`, `GIT_BRANCH`, `AGENT_ROLE=coding`.
5. Orchestrator waits for `sandbox.ready` on NMB.

### Sub-Agent Execution

6. Coding agent connects to NMB, publishes `sandbox.ready`.
7. Orchestrator sends `task.assign` with prompt and context files.
8. Coding agent's `AgentLoop` runs:
   - Round 1: Reads `src/app.py` (tool: `read_file`)
   - Round 2: Writes scratchpad note: "Flask app, uses blueprints"
     (tool: `scratchpad_write`)
   - Round 3: Edits `src/app.py` to add `/api/health` route
     (tool: `edit_file`)
   - Round 4: Creates `tests/test_health.py` (tool: `write_file`)
   - Round 5: Runs tests (tool: `bash` — `pytest tests/test_health.py`)
   - Round 6: Tests pass. Commits changes (tool: `git_commit`)
   - Final text: "Implemented /api/health endpoint with uptime tracking."
9. Coding agent publishes progress on each round.
10. Coding agent sends `task.complete` with diff, scratchpad, and summary.

### Model-Driven Finalization (orchestrator wrap-up)

11. Orchestrator's NMB event loop receives `task.complete`. Spawns an
    `asyncio.Task` for finalization (does not block the Slack loop).
12. The finalization task builds context (diff, scratchpad, summary) and
    feeds it into the orchestrator's `AgentLoop`.
13. The orchestrator's model sees the result and decides to present it to
    the user. It calls `present_work_to_user` (tool), which downloads
    artifacts, ingests the audit fallback file, and sends the Slack summary
    with [Push & PR] / [Iterate] / [Discard] buttons.
14. The user clicks [Push & PR]. The button click arrives as a Slack
    `ActionPayload`, handled by the Slack request loop.
15. The orchestrator's model sees the approval and calls
    `push_and_create_pr` (tool), which downloads the git state from the
    sandbox, pushes the branch, creates a PR, and returns the PR URL.
16. The model responds to the user: "PR #42 created: ...".
17. The model calls `destroy_sandbox` to clean up the coding sandbox.

---

## 14  Implementation Plan

### Phase 1 — `AgentLoop` extraction

Extract the reusable agent loop from the orchestrator.

| Task | Files |
|------|-------|
| Create `AgentLoop` class with `AgentLoopConfig` and `AgentLoopResult` | `src/nemoclaw_escapades/agent/loop.py` |
| Create `ToolStartCallback` / `ToolEndCallback` protocol types | `src/nemoclaw_escapades/agent/types.py` |
| Refactor `Orchestrator` to use `AgentLoop` internally | `src/nemoclaw_escapades/orchestrator/orchestrator.py` |
| Unit tests for `AgentLoop` (mock backend + tools) | `tests/test_agent_loop.py` |

**Exit criteria:** Existing orchestrator tests pass with `AgentLoop` under the
hood. No behavioral change.

### Phase 2 — File tools and scratchpad

| Task | Files |
|------|-------|
| Implement workspace-rooted file tools (`read_file`, `write_file`, `edit_file`, `list_directory`) | `src/nemoclaw_escapades/tools/files.py` |
| Implement search tools (`grep`, `glob`) | `src/nemoclaw_escapades/tools/search.py` |
| Implement `bash` tool with timeout and output truncation | `src/nemoclaw_escapades/tools/bash.py` |
| Implement git tools (`git_diff`, `git_commit`, `git_log`) | `src/nemoclaw_escapades/tools/git.py` |
| Implement `Scratchpad` class | `src/nemoclaw_escapades/agent/scratchpad.py` |
| Register `scratchpad_read` / `scratchpad_write` / `scratchpad_append` tools | `src/nemoclaw_escapades/tools/scratchpad.py` |
| Add scratchpad context injection to `AgentLoop` | `src/nemoclaw_escapades/agent/loop.py` |
| Create `create_coding_tool_registry()` factory | `src/nemoclaw_escapades/tools/coding.py` |
| Unit tests for all file tools (path validation, truncation, workspace sandboxing) | `tests/test_file_tools.py` |
| Unit tests for scratchpad | `tests/test_scratchpad.py` |

**Exit criteria:** A `ToolRegistry` with all coding file tools can be created.
File tools enforce workspace-root path restrictions. Scratchpad reads/writes
work and are included in `AgentLoopResult`.

### Phase 3 — Coding agent + sub-agent entrypoint

| Task | Files |
|------|-------|
| Create sub-agent `__main__` entrypoint | `src/nemoclaw_escapades/agent/__main__.py` |
| Create `AgentSetupBundle` dataclass | `src/nemoclaw_escapades/agent/types.py` |
| Create coding agent system prompt | `prompts/coding_agent.md` |
| Create coding agent OpenShell policy | `policies/coding-agent.yaml` |
| Create workspace setup script | `docker/setup-workspace.sh` |
| Emit `sandbox.spawn.*` NMB events (`sandbox.spawn.request`, `sandbox.spawn.started`, `sandbox.spawn.failed`, `sandbox.spawn.terminated`) | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| Include spawn metadata in `task.assign` envelope (`workflow_id`, `root_sandbox_id`, `parent_sandbox_id`, `role`, `ttl_s`) per [Sandbox Spawn Design §4](sandbox_spawn_design.md) | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| End-to-end test: agent process starts, connects NMB, handles task.assign, returns task.complete | `tests/integration/test_coding_agent.py` |

> **Note:** Dockerfile changes required for sub-agent support (installing
> OpenShell CLI, CA certs, `XDG_CONFIG_HOME`) are deferred to **Phase 5**
> (`docker/Dockerfile.orchestrator`).  See
> [Sandbox Spawn Design §5](sandbox_spawn_design.md) for the full list.

**Exit criteria:** The coding agent process can start, connect to NMB, receive a
`task.assign`, run the agent loop with file tools, and send `task.complete`
with diff, scratchpad, and summary.  Spawn lifecycle emits `sandbox.spawn.*`
NMB events.

### Phase 4 — Orchestrator delegation, NMB event loop, and finalization tools

| Task | Files |
|------|-------|
| Create `delegate_task` tool for the orchestrator | `src/nemoclaw_escapades/tools/delegation.py` |
| Implement sandbox spawn → workspace setup → task.assign flow | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| Implement NMB event loop (`start_nmb_listener`, event dispatch) | `src/nemoclaw_escapades/orchestrator/orchestrator.py` |
| Implement finalization tools (`present_work_to_user`, `push_and_create_pr`, `push_branch`, `discard_work`, `re_delegate`, `destroy_sandbox`) | `src/nemoclaw_escapades/tools/finalization.py` |
| Wire finalization tools into orchestrator `ToolRegistry` | `src/nemoclaw_escapades/orchestrator/orchestrator.py` |
| Implement `_finalize_workflow` (build context, run `AgentLoop` with finalization tools) | `src/nemoclaw_escapades/orchestrator/orchestrator.py` |
| Implement `PolicyOverlay` + hot-reload via `openshell policy set` | `src/nemoclaw_escapades/orchestrator/policy_overlay.py` |
| Handle `policy.request` NMB messages (auto-approve / escalate / deny) | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| Implement `AuditBuffer` with both `log_tool_call` and `log_inference_call` (child-side NMB flush + JSONL fallback, see §4.9) | `src/nemoclaw_escapades/agent/audit_buffer.py` |
| Implement orchestrator-side `audit.flush` handler (ingest both `tool_calls` and `inference_calls` arrays) + fallback JSONL ingest | `src/nemoclaw_escapades/audit/db.py` |
| Add `AuditDB.log_inference_call()` API per [Inference Call Auditing §5](inference_call_auditing_design.md#5--auditdb-api-extensions) | `src/nemoclaw_escapades/audit/db.py` |
| Add `inference_calls` table migration per [Inference Call Auditing §8](inference_call_auditing_design.md#8--alembic-migration) | `src/nemoclaw_escapades/audit/migrations/005_inference_calls.py` |
| Add `delegations` table for parent/child trace correlation per [Agent Trace Design §4.3](agent_trace_design.md#43--delegation-events) | `src/nemoclaw_escapades/audit/db.py` |
| Add approval gate event columns (`requested_at`, `decided_at`, `decision`, `decided_by`, `wait_ms`) per [Agent Trace Design §4.1](agent_trace_design.md#41--approval-gate-events) | `src/nemoclaw_escapades/audit/db.py` |
| Integration test: orchestrator → coding sandbox → result → model-driven finalize | `tests/integration/test_delegation.py` |

**Exit criteria:** The orchestrator can delegate a coding task to a sandboxed
sub-agent, receive the result via NMB without blocking the Slack loop, run
model-driven finalization (the LLM calls `present_work_to_user` or other
finalization tools), and handle user action buttons for push/iterate/discard.
Multiple finalization flows run concurrently.  Audit flush carries both
tool-call and inference-call records; delegation traces are queryable via
the `delegations` table.

### Phase 5 — Polish and hardening

| Task | Files |
|------|-------|
| TTL watchdog for sandbox cleanup | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| Progress relaying to Slack | `src/nemoclaw_escapades/orchestrator/delegation.py` |
| Git worktree support for parallel tasks | `docker/setup-workspace.sh` |
| Update Dockerfile with setup script and coding tools | `docker/Dockerfile.orchestrator` |
| File tool edge case hardening (symlinks, binary files, encoding) | `src/nemoclaw_escapades/tools/files.py` |

**Exit criteria:** Production-quality delegation with cleanup guarantees,
user-visible progress, and robust file tool handling.

---

## 15  Testing Plan

### 15.1 Unit Tests

| Test | What it verifies |
|------|-----------------|
| `AgentLoop` with mock backend and tools | Multi-turn loop, tool execution, safety limit, truncation handling |
| `AgentLoop` with scratchpad | Scratchpad context injection, read/write tools, snapshot in result |
| `AgentLoop` approval gate | Write tool gating (orchestrator mode), pre-approved pass-through (sub-agent mode) |
| `Scratchpad` class | Read, write, append, size cap, snapshot |
| `Orchestrator` refactor | All existing tests pass with `AgentLoop` under the hood |
| File tools path validation | Rejects `..` traversals, absolute paths, paths outside workspace root |
| File tools output truncation | Large file reads and grep results are truncated at configured limits |
| `edit_file` replacement | Correct old→new string replacement; fails gracefully on non-unique matches |
| `bash` tool timeout | Commands killed after timeout; stderr captured |
| Finalization tools | Each tool (`present_work_to_user`, `push_and_create_pr`, `discard_work`, `re_delegate`) produces correct output and side effects with mock sandbox/git |

### 15.2 Integration Tests

| Test | What it verifies |
|------|-----------------|
| Sub-agent NMB lifecycle | Connect, sandbox.ready, task.assign, task.complete |
| Coding agent end-to-end | Agent receives task, uses file tools to edit code, returns diff |
| Orchestrator delegation | Full spawn → assign → complete → cleanup flow |
| Model-driven finalization | task.complete → model calls `present_work_to_user` → user clicks [Push & PR] → model calls `push_and_create_pr` |
| Iteration flow | User feedback → model calls `re_delegate` → same sandbox → updated result |
| Concurrent finalization | Two sub-agents complete simultaneously; both finalization tasks run concurrently; user sends a Slack message during both — all three proceed without blocking each other |
| NMB event loop independence | Slack `handle()` responds while `_finalize_workflow` asyncio tasks are in progress |
| Policy hot-reload | Sub-agent requests PyPI access → orchestrator hot-reloads policy → retry succeeds |
| Audit NMB flush + fallback | Sub-agent tool calls arrive in central audit via NMB batch and/or JSONL fallback |
| Progress streaming | task.progress messages relayed to orchestrator via NMB event loop |
| Failure recovery | Sub-agent crash → orchestrator detects → cleanup |
| Timeout handling | TTL watchdog fires → sandbox deleted |

### 15.3 Safety Tests

| Test | What it verifies |
|------|-----------------|
| Tool surface enforcement | Sub-agent cannot use tools not in `tool_surface` |
| Workspace path sandboxing | File tools cannot read/write outside `/sandbox/workspace/` |
| Sandbox policy enforcement | Sub-agent cannot access blocked network endpoints |
| No recursive delegation | Coding agent cannot spawn sub-sandboxes |
| Scratchpad size cap | Large scratchpad writes are truncated |
| Bash command isolation | Bash tool respects sandbox process/network restrictions |
| Policy hot-reload scoping | Hot-reload cannot grant access beyond orchestrator's own policy |
| Sub-agent cannot self-update policy | Sub-agent has no gateway access; `policy.request` is the only path |

---

## 16  Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Sandbox spawn latency** | 5-15s to create + setup sandbox; adds to end-to-end time | Warm pool of pre-created sandboxes (v2); accept latency for M2 |
| **NMB broker unavailability** | Cannot communicate with sub-agent | Fail-open: detect broker down, fall back to file-based coordination |
| **Sub-agent infinite loop** | Consumes tokens without progress | `max_tool_rounds` safety limit + TTL watchdog on sandbox |
| **Large workspace clones** | Slow setup, high storage | Shallow clones (`--depth=1`); prefer NMB context files for small tasks |
| **Audit DB merge conflicts** | Duplicate or lost tool-call records | `INSERT OR IGNORE` keyed on unique row `id`; idempotent merge |
| **Credential leakage via scratchpad** | Agent writes secrets to scratchpad | Scratchpad sanitization before return; sub-agents have no access to orchestrator credentials (sandbox isolation) |
| **Policy hot-reload privilege escalation** | Agent tricks orchestrator into granting excessive network access | Auto-approve allowlist is scoped to known-safe registries per language ecosystem; unknown endpoints escalate to user; hot-reload cannot exceed orchestrator's own policy boundary |

---

## 17  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should the `AgentLoop` support concurrent tool execution in M2 or defer? | Concurrent execution reduces latency but adds complexity. Current orchestrator is sequential. Defer unless latency is a problem. |
| 2 | Should sub-agent system prompts be generated by the orchestrator or stored as static templates? | Static templates are simpler; generated prompts can be task-adaptive. Start with templates, evolve to generation. |
| 3 | What is the maximum reasonable scratchpad size? | 32 KB proposed. Need to balance context utility vs. token cost. |
| 4 | How should the orchestrator decide when to delegate vs. handle a task itself? | Start with explicit user intent or task complexity heuristics. Evolve to model-driven routing. |
| 5 | Should sandbox warm pools be implemented in M2 or deferred? | Defer. Accept spawn latency for now. |
| 6 | How should artifacts (diffs, patches) be indexed for the knowledge base (M5+)? | The `artifacts/` directory and structured `task.complete` payload provide the raw material. Indexing logic is M5+. |
| 7 | Git finalization: should the orchestrator pull+push (Strategy A) or should the sub-agent push directly (Strategy B)? | Strategy A maximizes isolation but adds complexity. Strategy B is simpler but widens sub-agent network surface. See §10.4. |
| 8 | Should `edit_file` use exact string match (Claude Code style) or line-range replacement? | Exact match is more robust against line-number drift but can fail on non-unique strings. Start with exact match + context. |
| 9 | What output truncation limits should the `bash` tool enforce? | 64 KB proposed for combined stdout+stderr. Too small and the agent can't see test output; too large and it blows context. |
| 10 | What endpoints should be auto-approved for policy hot-reload? | Candidates: PyPI, npm, crates.io, Maven Central, Go proxy. Should the allowlist be per-language-ecosystem or manually curated? |
| 11 | Should the sub-agent detect policy failures automatically and send `policy.request`, or should this be explicit via a tool? | Auto-detect is more ergonomic (agent retries automatically after policy update) but harder to implement (need to parse error messages from `bash` tool output). Explicit tool is simpler but requires the model to understand when to request policy changes. |

---

## 18  Comparison with Hermes, OpenClaw, and Claude Code

This section compares the M2 design against the three primary reference
architectures studied for this project. The comparison focuses on how each
system handles the core concerns of M2: the agent loop, sub-agent delegation,
tool execution, workspace management, and work finalization. Recommendations
for closing gaps are at the end.

For full analysis, see:
[Hermes Deep Dive](deep_dives/hermes_deep_dive.md) |
[OpenClaw Deep Dive](deep_dives/openclaw_deep_dive.md) |
[Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) |
[Comparison Matrix](deep_dives/hermes_vs_openclaw_vs_claude_code_comparison.md)

### 18.1 Agent Loop

| Dimension | NemoClaw M2 (`AgentLoop`) | Hermes (`AIAgent.run_conversation`) | OpenClaw Pi (Gateway → Pi turn loop) | Claude Code (`query()` async generator) |
|-----------|--------------------------|-------------------------------------|--------------------------------------|----------------------------------------|
| **Language** | Python, async | Python, async | TypeScript, async | TypeScript, async generator |
| **Streaming** | Non-streaming (M2); streaming deferred to M6+ | Non-streaming API call; stream from providers supported at transport level | Block streaming (tool output streamed to Gateway WS) | Streaming-first — tools execute *during* the response stream |
| **Tool execution timing** | After full model response | After full model response | After full model response (with block streaming for output) | During streaming (as soon as a `tool_use` block completes) |
| **Concurrent tools** | Sequential (M2); concurrent deferred | Sequential or concurrent (configurable per-tool) | Sequential | Concurrent for `isConcurrencySafe()` tools |
| **Truncation handling** | Continuation retry (up to 3) | Preflight compression + optional fallback model | `/compact` command | Up to 3 recovery turns with mid-thought resume |
| **Context management** | Message-count cap (M2); three-tier compaction deferred | Frozen system prompt + preflight compression + session search | Prompt files injection + `/compact` | Three-tier compaction (micro/full/session memory) |
| **Reusable by sub-agents** | Yes (extracted as standalone class) | Partially (same `AIAgent` class, but tightly coupled to Hermes internals) | No (Pi is the Gateway's runtime, not reusable standalone) | No (deeply coupled to Anthropic API and CLI rendering) |

**Where M2 improves:**
- `AgentLoop` is the only design that is explicitly **extracted as a reusable,
  infrastructure-agnostic class**. Hermes's `AIAgent` can technically be reused
  but carries Hermes-specific dependencies (provider resolver, session
  persistence, gateway integration). Pi and Claude Code's loops are deeply
  embedded in their respective systems.
- The `AgentLoop` is **model-agnostic** by design (backend is a pluggable
  `BackendBase`), whereas Claude Code is locked to Anthropic.

**Where M2 falls short:**
- **No streaming tool execution.** Claude Code's streaming-first design cuts
  perceived latency by 50%+ for multi-tool turns. M2's loop waits for the full
  response before executing tools.
- **No concurrent tool execution.** Both Hermes and Claude Code support
  running concurrent-safe tools in parallel. M2 is sequential.
- **No context compaction.** Claude Code's three-tier compaction (micro at
  ~256 tokens with no API call, full at ~4K tokens via LLM summary, session
  memory as a zero-cost key-fact cache) is far more sophisticated than M2's
  simple message-count cap.

### 18.2 Sub-Agent Delegation and Isolation

| Dimension | NemoClaw M2 | Hermes | OpenClaw | Claude Code |
|-----------|-------------|--------|----------|-------------|
| **Isolation** | Full (separate OpenShell sandboxes, independent filesystem/network/credentials) | None (in-process `AIAgent` sessions, shared memory space) | Partial (Docker containers for tool execution, but sessions share the Gateway process) | None (in-process, shared filesystem; known permission-widening gap) |
| **Communication** | NMB (WebSocket, ~20-50ms, cross-host capable) | `sessions_send` (in-process, <1ms) | `sessions_send`/`sessions_spawn` (in-process) | UDS inbox / `AgentTool` (in-process + Unix domain sockets) |
| **Multi-host** | Yes (NMB over Tailscale/SSH/TLS) | No (single process) | No (single Gateway process) | No (single machine) |
| **Depth control** | Enforced by tool surface (sub-agents lack delegation tools) | Shared iteration budget across parent + children | `maxSpawnDepth` (1-5, default 1) | No explicit depth limit |
| **Concurrency control** | Per-workflow asyncio tasks; no hard cap yet | Shared budget limits total iterations | `maxChildrenPerAgent` (1-20), `maxConcurrent` (global cap) | No explicit concurrency cap |
| **Identity/provenance** | Proxy-enforced `X-Sandbox-ID` (cannot be forged) | Trusted (same process) | Trusted (same process) | Trusted (same process) |
| **Audit trail** | Full (single audit DB: NMB messages + tool calls + inference calls with payloads) | None for session messaging | None | None |
| **Work collection** | Model-driven finalization tools (§10.3) | Parent reads child session output | Announce chain (depth-2 → depth-1 → depth-0) | `AgentOutput` manifest + polling |

**Where M2 improves:**
- **Three-layer architecture with clean role differentiation (§4.7).** M2's
  `Agent` base class provides the same "Gateway → Pi" separation that OpenClaw
  has, but with stronger guarantees. OpenClaw's Gateway and Pi are tightly
  coupled (Pi is the Gateway's runtime, not reusable standalone). Hermes's
  `AIAgent` is reusable but carries Hermes-specific dependencies. M2's
  `AgentLoop` (Layer 1) is fully standalone and NMB-free; the `Agent` base
  class (Layer 2) adds NMB and lifecycle; role-specific subclasses (Layer 3)
  configure tools and event loops. The orchestrator becomes strictly more
  powerful than a coding agent not through a different loop, but through
  different tools and event handling composed around the same loop.
- **Strongest isolation of any reference system.** OpenShell sandbox isolation
  is kernel-level (Landlock + seccomp + network policy). No other system
  provides this for sub-agents. Claude Code has a known permission-widening
  gap where a sub-agent can escalate beyond its parent's scope.
- **Multi-host delegation.** No reference system supports sub-agents on
  different machines. NMB over Tailscale/SSH makes this transparent.
- **Full audit trail.** Every inter-agent message, tool call, and inference
  call (with full payloads) is logged in a single audit DB. No reference
  system provides this.
- **Model-driven finalization.** The orchestrator's LLM reasons about results
  and calls finalization tools, rather than following a hardcoded flow.

**Where M2 falls short:**
- **Latency.** NMB's ~20-50ms per message is 20-50x slower than Hermes's
  in-process <1ms. For tight collaboration loops (coding ↔ review), this adds
  up. A 3-iteration review loop costs ~200ms in NMB overhead vs ~3ms in Hermes.
- **No spawn depth/concurrency caps yet.** OpenClaw's `maxSpawnDepth` and
  `maxChildrenPerAgent` are more mature than M2's "no delegation tools in
  sub-agent policy" approach.
- **No session forking.** Claude Code's `FORK_SUBAGENT` sends a serialized
  conversation snapshot so the child starts with the parent's full context.
  M2's `task.assign` requires the orchestrator to manually package context.

### 18.3 Tool System

| Dimension | NemoClaw M2 | Hermes | OpenClaw Pi | Claude Code |
|-----------|-------------|--------|-------------|-------------|
| **Tool count** | ~13 (M2 coding tools) + enterprise tools | ~40+ built-in + MCP | ~20+ built-in + plugins | ~40+ built-in + MCP |
| **Registration** | `ToolRegistry.register()` at import time | `registry.register()` at import time | Three layers: tools, skills, plugins | Type-level interface + concrete registry |
| **Filtering** | `tool_surface` list in `task.assign` | Toolset bundles + platform presets + MCP | `tools.allow`/`tools.deny` + profiles + provider rules | 5-layer pipeline (env gating → permission pruning → mode rewriting) |
| **Progressive loading** | Not yet (all tools loaded at startup) | Progressive skill disclosure (Level 0/1/2) | No (all skills loaded at session start) | `ToolSearch` meta-tool for deferred loading |
| **MCP support** | Not yet (Phase 3 in tools integration design) | Yes (`mcp` toolset, dynamic registration) | Yes (plugins can register tools) | Yes (MCP server integration) |
| **Safety** | `is_read_only` flag + approval gate + sandbox policy | Approval callbacks + allowlists + scans | `allow`/`deny` ACLs (deny wins) + profiles | 5-layer filtering + YOLO classifier + NO_TOOLS sandwich |

**Where M2 improves:**
- **Dual-layer enforcement.** M2 enforces tool restrictions at both the
  `ToolRegistry` level (tool not loaded) and the OpenShell policy level
  (network/filesystem blocked). No other system has this defense-in-depth
  for tool access.
- **Policy hot-reload** (§6.3) allows the orchestrator to dynamically add
  endpoints mid-task. No reference system supports this.

**Where M2 falls short:**
- **No `ToolSearch`/progressive loading.** As tool count grows (enterprise +
  coding + MCP), the prompt will bloat. Claude Code's `ToolSearch` and
  Hermes's progressive skill disclosure solve this.
- **No MCP support yet.** All three reference systems support dynamic tool
  ingestion via MCP or plugins.
- **Simpler safety model.** Claude Code's YOLO classifier (two-stage,
  64-token fast path + 4K-token thinking) is more nuanced than M2's binary
  read/write classification.

### 18.4 Workspace and Scratchpad

| Dimension | NemoClaw M2 | Hermes | OpenClaw Pi | Claude Code |
|-----------|-------------|--------|-------------|-------------|
| **Workspace isolation** | Full (sandbox filesystem, workspace-rooted tools) | Shared host filesystem (or Docker/Modal backend) | Docker bind mount (`none`/`ro`/`rw`) | Host filesystem (no isolation) |
| **Workspace seeding** | Three mechanisms: OpenShell upload, git clone, NMB context files (§5.3) | Local filesystem access | Docker bind mount or sandbox setup script | Host filesystem (already present) |
| **Scratchpad** | Dedicated Markdown file + tools + context injection + returned to orchestrator | `MEMORY.md` (bounded, ~2,200 chars) + `USER.md` (~1,375 chars) | `update-plan-tool` (ordered step list) | No explicit scratchpad (uses conversation context) |
| **Scratchpad persistence** | Ephemeral (per-task); returned in `task.complete` | Persistent across sessions (disk) | Per-session plan state | Conversation history (compacted) |

**Where M2 improves:**
- **Scratchpad is a first-class artifact.** It is returned to the orchestrator
  on task completion, providing visibility into the sub-agent's reasoning.
  Hermes's `MEMORY.md` is persistent but not task-scoped. OpenClaw's
  `update-plan-tool` is closer but doesn't get returned to a coordinator.
  Claude Code has no explicit scratchpad.
- **Three seeding mechanisms** provide flexibility that no single reference
  system matches.

**Where M2 falls short:**
- **No persistent memory across tasks.** Hermes's `MEMORY.md` + `USER.md` +
  Honcho provide cross-session learning. M2 defers this to M5+.
- **No context compression.** Claude Code's three-tier compaction keeps long
  sessions viable. M2's scratchpad helps but doesn't replace compaction.

### 18.5 Work Finalization

| Dimension | NemoClaw M2 | Hermes | OpenClaw Pi | Claude Code |
|-----------|-------------|--------|-------------|-------------|
| **Finalization model** | Model-driven (orchestrator LLM calls finalization tools) | Parent reads child session result; no structured finalization | Announce chain (sub-agent → parent → user) | `AgentOutput` manifest + coordinator synthesis |
| **User interaction** | Slack action buttons (Push & PR / Iterate / Discard) | Direct chat | Channel delivery | Terminal output |
| **Iteration** | `re_delegate` tool keeps sandbox alive, re-sends task | New `sessions_send` to same session | Not documented | Not documented |
| **Git integration** | `push_and_create_pr` tool (download git state, push, create PR) | Via terminal tools (manual) | Via exec/bash tools (manual) | Via Bash tool (manual) |

**Where M2 improves:**
- **Structured finalization with user choice.** No reference system has a
  formal finalization flow where the user can choose between push, iterate,
  and discard via UI buttons.
- **Git-aware finalization.** The `push_and_create_pr` tool handles the
  full sequence (download from sandbox, push, create PR) as a single
  operation. In all reference systems, this is a manual multi-step process.
- **Iteration without teardown.** The `re_delegate` tool re-sends work to
  the same sandbox, preserving workspace state. This is more efficient than
  creating a new sub-agent session.

### 18.6 Summary: Strengths and Gaps

#### M2 strengths over all three reference systems

| Strength | vs Hermes | vs OpenClaw | vs Claude Code |
|----------|-----------|-------------|----------------|
| Kernel-level sub-agent isolation | In-process only | Docker only (no Landlock/seccomp) | In-process only + known permission gap |
| Multi-host delegation | No | No | No |
| Full audit trail (NMB + tool calls) | No audit for sessions | No audit | No audit |
| Reusable, infrastructure-agnostic agent loop | Tightly coupled | Embedded in Gateway/Pi | Embedded in CLI |
| Model-driven finalization with user controls | No structured flow | Announce chain only | Coordinator synthesis only |
| Policy hot-reload mid-task | No | No | No |
| Scratchpad returned to orchestrator | Memory is persistent but not task-scoped | Plan is session-scoped but not returned | No scratchpad |

#### M2 gaps vs reference systems

| Gap | Best reference | Severity | Recommended fix | Target milestone |
|-----|---------------|----------|----------------|-----------------|
| No streaming tool execution | Claude Code | High | Implement streaming-first `AgentLoop` variant that executes tools during the inference stream | M6+ |
| No concurrent tool execution | Claude Code / Hermes | Medium | Add `is_concurrency_safe` flag to `ToolSpec`; execute flagged tools via `asyncio.gather` | M2+ (incremental) |
| No context compaction | Claude Code | High | Implement three-tier compaction: micro (heuristic pruning), full (LLM summary), session memory (key-fact cache) | M3 (needed for review iteration loops) |
| No progressive tool loading | Claude Code (`ToolSearch`) / Hermes (progressive disclosure) | Medium | Add `ToolSearch` meta-tool that loads tool definitions on demand; keep prompt lean | M4 (when tool count grows) |
| No MCP support | All three | Medium | Implement MCP bridge as per [Tools Integration Design §4.3](tools_integration_design.md#c1-mcp-dynamic-tools) | M5+ |
| No self-learning loop | Hermes | Low (for M2) | Hermes pattern: skills auto-created from successful sessions + memory persist + session search | M6 |
| No session forking | Claude Code (`FORK_SUBAGENT`) | Low | Add `task.fork` message type (already designed in [NMB Design §14](nmb_design.md#14--coordinator-integration--extended-message-types)) | M3+ |
| No spawn depth/concurrency caps | OpenClaw (`maxSpawnDepth`, `maxChildrenPerAgent`) | Medium | Add configurable caps to the orchestrator's delegation module | M2+ (incremental) |
| Sub-agent communication latency (~20-50ms vs <1ms) | Hermes (in-process) | Low | Accept for M2. For tight loops (coding ↔ review), consider co-locating agents as local processes with shared NMB (§3.1 already supports this) | N/A (architectural trade-off for isolation) |
| No YOLO-style permission classifier | Claude Code | Low | M2's binary read/write + approval gate is sufficient for now. Evolve to a multi-tier classifier when the tool surface grows | M5+ |
| No prompt cache boundary | Claude Code | Medium | Split system prompt into static prefix (cached) + dynamic suffix (per-turn). Already planned in [Orchestrator Design §4](orchestrator_design.md#4--system-prompt-construction) | M2+ |

### 18.7 Lessons from Build Your Own OpenClaw Tutorial

The [Build Your Own OpenClaw tutorial](https://github.com/czl9707/build-your-own-openclaw)
(1.1k stars, MIT) provides a minimal working implementation of many patterns
NemoClaw designs in the abstract. Key lessons for M2:

**1. Concurrent tool execution is simple and safe.**
The tutorial runs *all* tool calls in a turn via `asyncio.gather` from step 01
onward. This validates that NemoClaw's proposed `is_concurrency_safe` flag is
the right granularity — the tutorial defaults to concurrent and has no reported
issues. NemoClaw should do the same for M2 rather than deferring to M2+.

**2. In-process sub-agent dispatch via `Future`-based rendezvous works well.**
The tutorial's `subagent_dispatch` tool publishes a `DispatchEvent`, subscribes
to `DispatchResultEvent` filtered by session ID, and `await`s an
`asyncio.Future` — a clean 50-line pattern. NemoClaw's NMB-based dispatch is
more powerful (cross-sandbox, cross-host) but the tutorial proves that the
*logical flow* is correct. For local-process development (no OpenShell), NemoClaw
should support a similar in-process dispatch path.

**3. Per-agent semaphore concurrency control is the right primitive.**
The tutorial's `AgentWorker` creates `asyncio.Semaphore(max_concurrency)` per
agent ID and auto-cleans semaphores when no waiters remain. This is directly
applicable to NemoClaw's orchestrator delegation module — add
`max_concurrent_tasks` to the delegation config, enforced via semaphore before
`openshell sandbox create`.

**4. `ContextGuard` with session rolling is a proven compaction strategy.**
The tutorial's compaction (token estimate → truncate tool blobs → LLM summary
→ roll to new session with routing cache update) validates NemoClaw's planned
three-tier compaction. The "session roll" pattern (create new session, copy
summary + tail, re-point routing) is worth adopting for the full-compaction
tier.

**5. At-least-once outbound delivery with crash recovery is essential.**
The tutorial's `EventBus` persists `OutboundEvent` to disk (atomic
write: tmp + fsync + rename) and deletes only after `ack()`. On startup,
`_recover()` replays pending events. NemoClaw's NMB broker should adopt
the same pattern for `task.complete` and `audit.flush` messages.

**6. Layered prompt builder separates concerns cleanly.**
The tutorial's 5-layer `PromptBuilder` (identity → soul → bootstrap →
runtime → channel hint) validates NemoClaw's planned system prompt
construction. The "channel hint" layer (cron vs subagent vs platform) is
directly applicable — sub-agents need different behavioral guidance than
user-facing agents.

**7. Config hot-reload with user/runtime file separation works.**
The tutorial's two-file approach (`config.user.yaml` for durable settings +
`config.runtime.yaml` for ephemeral state like session caches) with
`watchdog` file monitoring validates the split. NemoClaw should adopt this
for development mode even if production mode uses the orchestrator's
config system.

**8. Explicit gaps document (`GAP.md`) is good practice.**
The tutorial maintains a `GAP.md` that lists features intentionally excluded
from the tutorial vs the reference implementation. NemoClaw should maintain
a similar document tracking features deferred from each milestone to prevent
scope creep.

### 18.8 Recommendations

**For M2 (immediate):**

1. **Add `is_concurrency_safe` to `ToolSpec`** and run flagged tools via
   `asyncio.gather` in `AgentLoop`. This is a small change that closes one
   of the highest-impact gaps. Mark `read_file`, `grep`, `glob`,
   `scratchpad_read`, `git_diff`, `git_log` as concurrency-safe.
   *(Validated by BYOO tutorial: concurrent by default from step 01.)*
2. **Add spawn depth and concurrency caps** to the delegation module. Lift
   OpenClaw's `maxSpawnDepth` / `maxChildrenPerAgent` pattern — simple
   integer config values checked before `openshell sandbox create`.
   *(BYOO tutorial uses per-agent `asyncio.Semaphore` — adopt the same
   primitive.)*
3. **Implement the prompt cache boundary** (`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`).
   The system prompt is already loaded from a file; splitting it into static
   prefix + dynamic suffix is straightforward and saves ~90% on subsequent
   turns for providers that support prompt caching.
4. **Add at-least-once delivery for NMB `task.complete` messages.** Persist
   completion payloads to disk before sending; delete after orchestrator
   acknowledgement. *(Validated by BYOO tutorial's `EventBus` outbound
   persistence pattern.)*

**For M3 (review agent):**

4. **Implement context compaction.** The review iteration loop (coding agent
   ↔ review agent, potentially 3+ rounds) will pressure the context window.
   Start with micro-compaction (heuristic message pruning, no API call) and
   full compaction (LLM-generated summary). Lift Claude Code's two-tier
   pattern.
5. **Implement `task.fork`** for review agent spawning. The review agent
   needs the coding context to provide useful feedback. Session forking
   avoids re-packaging all context in `task.assign`.

**For M5+ (tools expansion):**

6. **Add `ToolSearch` meta-tool.** As enterprise tools + coding tools + MCP
   tools accumulate, the prompt will bloat. Lift Claude Code's `ToolSearch`
   pattern: load only core tool definitions in the prompt; the model searches
   for specialized tools on demand.
7. **Add MCP bridge.** Follow the design in
   [Tools Integration Design §4.3](tools_integration_design.md#c1-mcp-dynamic-tools).

**For M6+ (agent maturity):**

8. **Implement streaming tool execution.** Refactor `AgentLoop` to use an
   async generator pattern (like Claude Code's `query()`) where tools
   execute during the inference stream. This is the single biggest latency
   improvement available.
9. **Lift Hermes's self-learning loop.** Auto-create skills from successful
   sessions, persist memory across tasks, enable session search. The M2
   infrastructure (scratchpad, audit trail, skills directory) is already
   designed to support this.

---

### Sources

- [Design Document](design.md) — vision, milestones, architecture
- [Orchestrator Design](orchestrator_design.md) — agent loop, tool system, coordinator mode
- [NMB Design](nmb_design.md) — inter-sandbox messaging protocol
- [Sandbox Spawn Design](sandbox_spawn_design.md) — OpenShell sandbox lifecycle
- [Audit DB Design](audit_db_design.md) — tool-call auditing, NMB-batched flush, JSONL fallback
- [Tools Integration Design](tools_integration_design.md) — tool registry, Hermes/OpenClaw patterns
- [Executor/Advisor NMB Design](executor_advisor_nmb_design.md) — advisor consultation pattern
- [Hermes Deep Dive](deep_dives/hermes_deep_dive.md) — `AIAgent.run_conversation`, skills, memory, sub-agent delegation, self-learning loop
- [OpenClaw Deep Dive](deep_dives/openclaw_deep_dive.md) — Pi agent, Gateway sessions, tool profiles, skills, Canvas, sandbox execution
- [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) — streaming `query()`, coordinator mode, three-tier compaction, YOLO classifier, prompt cache boundary
- [Comparison Matrix](deep_dives/hermes_vs_openclaw_vs_claude_code_comparison.md) — side-by-side across all dimensions
- [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md) — 18-step tutorial analysis: event bus, compaction, routing, dispatch, concurrency, prompt layering, config hot-reload
- [Build Your Own OpenClaw](https://github.com/czl9707/build-your-own-openclaw) (source, 1.1k stars, MIT) — minimal working implementation of OpenClaw patterns
