# Milestone 2a — Reusable Agent Loop, Coding Tools & Context Management

> **Split from:** [Milestone 2 (original)](design_m2.md)
>
> **Predecessor:** [Milestone 1 — Foundation](design_m1.md)
>
> **Successor:** [Milestone 2b — Multi-Agent Orchestration](design_m2b.md)
>
> **Last updated:** 2026-04-14

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [The Reusable Agent Loop](#3--the-reusable-agent-loop)
4. [Coding Agent File Tools](#4--coding-agent-file-tools)
5. [Agent Scratchpad](#5--agent-scratchpad)
6. [Context Compaction](#6--context-compaction)
7. [Basic Skill Loading](#7--basic-skill-loading)
8. [Layered Prompt Builder](#8--layered-prompt-builder)
9. [Implementation Plan](#9--implementation-plan)
10. [Testing Plan](#10--testing-plan)
11. [Open Questions](#11--open-questions)

---

## 1  Overview

Milestone 2a extracts the orchestrator's multi-turn tool-calling loop into a
**reusable `AgentLoop`**, equips it with a concrete set of **coding file tools**,
and adds **context compaction** and **basic skill loading** — two capabilities
promoted from later milestones because the BYOO tutorial validates they are
essential early (steps 02 and 04, before even the event system).

The central design challenge is **factoring the "agent" out of the
orchestrator**. Today the multi-turn tool-calling loop lives inside
`Orchestrator._run_agent_loop()` and is tightly coupled to orchestrator-specific
concerns (Slack thread keys, approval buttons, connector callbacks). M2a
extracts this into a reusable `AgentLoop` that can run identically inside:

- The orchestrator process (the "root agent")
- A co-located sub-agent process (M2b)
- A separate sandbox process (M3+)
- A local process (for development without OpenShell)

M2a is a **single-agent milestone** — no delegation, no NMB, no sandbox
orchestration. Those capabilities land in [M2b](design_m2b.md). By the end of
M2a, the orchestrator has a more powerful, tool-equipped agent loop that can
handle coding tasks directly (without sub-agent delegation), manage long
conversations via compaction, and load task-specific skills.

### What was promoted into M2a

| Feature | Original Target | Rationale |
|---------|----------------|-----------|
| Context compaction | M3 | BYOO tutorial builds this at step 04 (before event system). Any coding session that hits the context window fails with no recovery. |
| Basic `SKILL.md` loading | M6 | BYOO tutorial builds this at step 02 (immediately after tools). The loading mechanism is independent of auto-creation/self-learning. Useful for coding agent task templates. |

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Extract a reusable `AgentLoop` class from the orchestrator's
   `_run_agent_loop()` that is infrastructure-agnostic and testable in isolation.
2. Implement **concurrent tool execution** by default via `asyncio.gather`;
   unsafe tools opt out via `is_concurrency_safe=False`.
3. Equip the agent with a concrete **file tool suite**: `read_file`,
   `write_file`, `edit_file`, `grep`, `glob`, `list_directory`, `bash`,
   `git_diff`, `git_commit`, `git_log`.
4. Implement the **scratchpad** mechanism for working notes with context
   injection and snapshot return.
5. Implement **two-tier context compaction**: micro-compaction (tool result
   truncation, no API call) and full compaction (LLM summary + session roll).
6. Implement **basic `SKILL.md` loading** via a `skill` tool that injects skill
   content into the conversation.
7. Implement the **layered prompt builder** with cache boundary for provider
   prompt caching.
8. Refactor the orchestrator to use `AgentLoop` internally with no behavioral
   regression.
9. Design the `AgentLoop` to be forward-compatible with M2b's sub-agent usage
   (same class, different tool registries and config).

### 2.2 Non-Goals

1. Sub-agent delegation, NMB messaging, or sandbox orchestration (M2b).
2. OpenShell sandbox provisioning or lifecycle management (M2b).
3. Work collection, finalization tools, or git push/PR automation (M2b).
4. Auto-skill creation or the self-learning loop (M6).
5. Streaming tool execution (M6+).
6. Full memory system — Honcho, SecondBrain integration (M5).

---

## 3  The Reusable Agent Loop

*Full specification: [original design_m2.md §4](design_m2.md#4--the-reusable-agent-loop-agentloop)*

### 3.1 Design

Factor out the core loop into a class that is infrastructure-agnostic and
reusable by any agent.

```python
class AgentLoop:
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
    content: str
    tool_calls_made: int
    rounds: int
    hit_safety_limit: bool
    scratchpad_contents: str | None
    working_messages: list[Message]
```

### 3.2 Concurrent Tool Execution

Every tool declares `is_concurrency_safe` (default `True`). The `AgentLoop`
partitions tool calls into a concurrent batch (safe) and a sequential tail
(unsafe):

```python
safe = [tc for tc in tool_calls if self._tools.get(tc.name).is_concurrency_safe]
unsafe = [tc for tc in tool_calls if not self._tools.get(tc.name).is_concurrency_safe]

results = await asyncio.gather(*[self._exec(tc) for tc in safe])
for tc in unsafe:
    results.append(await self._exec(tc))
```

Validated by the BYOO tutorial, which runs all tool calls via `asyncio.gather`
from step 01 with no reported issues.

**Concurrency-safe tools** (read-only or idempotent): `read_file`, `grep`,
`glob`, `list_directory`, `scratchpad_read`, `git_diff`, `git_log`.

**Not concurrency-safe** (mutate workspace state): `write_file`, `edit_file`,
`bash`, `git_commit`, `scratchpad_write`, `scratchpad_append`.

### 3.3 Loop Internals

The `AgentLoop.run()` method preserves the proven mechanics from the current
orchestrator loop:

1. Tool definitions snapshot — captured once per `run()` call.
2. Shallow-copy messages — caller's list is never mutated.
3. Per-round inference — send messages + tool defs to backend.
4. Terminal condition — no `tool_calls` in response → return text.
5. Approval gate — checks write tools before execution (if configured).
6. Tool execution — concurrent by default (see §3.2).
7. Audit logging — every tool invocation recorded via `audit.log_tool_call()`.
8. Scratchpad auto-update — after each round, contents injected into context.
9. Truncation handling — `finish_reason=length` triggers continuation retry.
10. Safety limit — returns partial answer after `max_tool_rounds`.

### 3.4 Three-Layer Agent Architecture

The `AgentLoop` is Layer 1 of a three-layer composition model:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 3: Role-Specific Agents                            │
│  OrchestratorAgent | CodingAgent | ReviewAgent            │
│  (different tools, connectors, event loops)               │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Agent Base Class                                │
│  Agent(ABC): owns AgentLoop + MessageBus + lifecycle      │
│  NMB lives here — tools receive MessageBus at             │
│  registration time, not through AgentLoop                 │
├─────────────────────────────────────────────────────────┤
│  Layer 1: AgentLoop                                       │
│  Pure inference + tool execution loop.                    │
│  No NMB. No connectors. No event handling.                │
│  Role-agnostic. Stateless per run() call.                 │
└─────────────────────────────────────────────────────────┘
```

*Full specification including `Agent` base class: [original §4.7–4.8](design_m2.md#47--the-three-layer-agent-architecture)*

### 3.5 How the Orchestrator Uses `AgentLoop`

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

---

## 4  Coding Agent File Tools

*Full specification: [original §7](design_m2.md#7--coding-agent-file-tools)*

### 4.1 Tool Catalog

| Tool | Mode | Concurrency | Description |
|------|------|-------------|-------------|
| `read_file` | READ | Safe | Read a file with optional line-range selection |
| `write_file` | WRITE | Unsafe | Create or overwrite a file |
| `edit_file` | WRITE | Unsafe | Targeted old/new string replacement |
| `list_directory` | READ | Safe | List files and directories |
| `grep` | READ | Safe | Search file contents by regex |
| `glob` | READ | Safe | Find files matching a glob pattern |
| `bash` | WRITE | Unsafe | Execute a shell command with timeout |
| `git_diff` | READ | Safe | Show uncommitted changes |
| `git_commit` | WRITE | Unsafe | Stage and commit changes |
| `git_log` | READ | Safe | Show recent commit history |

### 4.2 Key Design Decisions

- **Workspace-rooted paths** — all tools resolve relative to workspace root.
  Absolute paths and `..` traversals are rejected.
- **Output truncation** — large reads and grep results capped at 200 lines /
  32 KB. Tool output indicates when truncation occurred.
- **`edit_file` over `write_file`** — system prompt steers model toward
  surgical edits for cleaner diffs.
- **`bash` safety** — timeout (120s), combined stdout+stderr capped at 64 KB.
  Sandbox policy provides the real security boundary.

### 4.3 Tool Registry Factory

```python
def create_coding_tool_registry(workspace_root: str) -> ToolRegistry:
    registry = ToolRegistry()
    for tool_fn in [read_file, write_file, edit_file, list_directory,
                    grep, glob, bash, git_diff, git_commit, git_log]:
        registry.register(tool_fn(workspace_root))
    return registry
```

---

## 5  Agent Scratchpad

*Full specification: [original §8](design_m2.md#8--agent-scratchpad)*

The scratchpad is a Markdown file on the workspace filesystem. It serves as the
agent's working memory for the current task — observations, plans, open
questions, decisions.

```python
@dataclass
class Scratchpad:
    path: str
    max_size: int = 32_768  # 32 KB cap

    def read(self) -> str: ...
    def write(self, content: str) -> str: ...
    def append(self, section: str, content: str) -> str: ...
    def snapshot(self) -> str: ...
```

Three tools (`scratchpad_read`, `scratchpad_write`, `scratchpad_append`) are
registered automatically when a `Scratchpad` is provided to the `AgentLoop`.
Contents are injected into the system prompt as a `<scratchpad>` block on every
inference round.

---

## 6  Context Compaction

> **Promoted from M3.** The BYOO tutorial builds compaction at step 04 (Phase 1),
> before even the event system. Any coding session that hits the context window
> during M2a will fail with no recovery. The tutorial's `ContextGuard` pattern
> validates that compaction is essential for a capable single agent.

### 6.1 Two-Tier Compaction

NemoClaw adopts a two-tier compaction model, drawing from both Claude Code's
three-tier design and the BYOO tutorial's `ContextGuard`:

| Tier | Trigger | Cost | Method |
|------|---------|------|--------|
| **Micro** | Tool result > 10K chars | Zero (no API call) | Truncate large tool results in place; append `[Truncated — original: N chars]` |
| **Full** | Total message tokens > threshold | One inference call | LLM-generated summary of oldest ~50% of messages; keep newest ~20%; session roll |

### 6.2 Micro-Compaction: Tool Result Truncation

Applied automatically before each inference call:

```python
def _truncate_large_tool_results(self, messages: list[Message]) -> list[Message]:
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > self.max_tool_result_chars:
                truncated = content[:self.max_tool_result_chars]
                msg = {**msg, "content": f"{truncated}\n\n[Truncated — original: {len(content)} chars]"}
    return result
```

This is the BYOO tutorial's `ContextGuard._truncate_large_tool_results` pattern
(§5.1 of the deep dive). It handles the common case of `bash` or `grep`
returning massive output without consuming an inference call.

### 6.3 Full Compaction: LLM Summary + Session Roll

When the total message token count exceeds a configurable threshold:

1. Summarize the oldest ~50% of messages via a dedicated inference call.
2. Keep the newest ~20% of messages verbatim (preserves recent context).
3. Create a new session with the summary + tail messages.
4. Update any routing caches to point to the new session.

```python
async def compact_and_roll(self, state: SessionState) -> SessionState:
    compress_count = max(2, int(len(state.messages) * 0.5))
    keep_count = max(4, int(len(state.messages) * 0.2))

    summary = await self._summarize(state.messages[:compress_count])

    new_messages = [
        {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
        {"role": "assistant", "content": "Understood, I have the context."},
        *state.messages[compress_count:],
    ]
    return state.roll_to_new_session(new_messages)
```

This is the BYOO tutorial's `ContextGuard.compact_and_roll` session-rolling
pattern (deep dive §5.2). It creates a new session rather than mutating in
place, which is cleaner for audit and persistence.

### 6.4 Integration with AgentLoop

The `AgentLoop` runs micro-compaction on every round (cheap, no API call) and
checks for full compaction when message count exceeds the threshold:

```python
async def run(self, messages, request_id):
    while True:
        messages = self._truncate_large_tool_results(messages)
        if self._should_compact(messages):
            messages = await self._compact_and_roll(messages)
        response = await self._backend.chat(messages, self._tool_defs)
        # ... tool execution loop ...
```

---

## 7  Basic Skill Loading

> **Promoted from M6.** The BYOO tutorial builds skill loading at step 02
> (Phase 1), immediately after tools. The loading mechanism (`SKILL.md` files +
> a `skill` tool) is 50 lines and independent of the self-learning loop. It
> enables task-specific prompt injection for coding tasks.

### 7.1 Skill Definition Format

Skills use `SKILL.md` files with optional YAML frontmatter:

```markdown
---
name: Code Review
description: Perform a structured code review
---

# Code Review Skill

Review the provided code changes and provide feedback on:
1. Correctness — does the code do what it claims?
2. Style — does it follow project conventions?
...
```

### 7.2 Skill Tool

A single `skill` tool with an enum of available skill IDs:

```python
@tool(
    name="skill",
    description="Load a specialized skill to guide your approach.",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "enum": available_skill_ids,
                "description": "The skill to load",
            }
        },
        "required": ["skill_name"],
    },
)
async def skill_tool(skill_name: str, session: AgentLoop) -> str:
    content = skill_loader.load(skill_name)
    return f"[Skill: {skill_name}]\n{content}"
```

The skill content is returned as a tool result, which the model sees on the
next inference round. This is the BYOO tutorial's tool-based approach (deep
dive §6.2).

### 7.3 Scope Boundary

M2a implements only **reading and loading** skills. The following are deferred:

| Deferred to | Feature |
|-------------|---------|
| M6 | Auto-skill creation from successful sessions (`skillify`) |
| M6 | Skill policy/update pipeline driven by outcomes |
| M6 | Progressive disclosure (Level 0/1/2) |
| M6 | Template substitution (`{{workspace}}` variables) |

---

## 8  Layered Prompt Builder

*Validated by BYOO tutorial step 13 (deep dive §10).*

### 8.1 Five-Layer System Prompt

The `PromptBuilder` constructs the system prompt from five layers:

| Layer | Content | Static/Dynamic |
|-------|---------|----------------|
| 1. Identity | Agent role definition (from template or `AGENT.md`) | Static |
| 2. Task context | Skill content, workspace description, task instructions | Dynamic |
| 3. Runtime metadata | Agent ID, timestamp, available tools summary | Dynamic |
| 4. Channel hint | Whether responding to user, parent agent, or cron | Dynamic |
| 5. Scratchpad | Current scratchpad contents (if enabled) | Dynamic |

### 8.2 Cache Boundary

The system prompt is split at a `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker. Content
before the boundary (layers 1–2 for stable tasks) is cached by the provider;
content after (layers 3–5) changes per turn. This reduces cost by ~90% on
subsequent turns via provider prompt caching.

```python
class PromptBuilder:
    def build(self, agent_id: str, source_type: str, scratchpad: str = "") -> str:
        layers = []
        layers.append(self._identity)
        layers.append(self._task_context)
        layers.append("__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__")
        layers.append(self._runtime_metadata(agent_id))
        layers.append(self._channel_hint(source_type))
        if scratchpad:
            layers.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
        return "\n\n".join(layers)
```

### 8.3 Channel Hint

The channel hint tells the agent how its response will be used:

```python
def _channel_hint(self, source_type: str) -> str:
    if source_type == "cron":
        return "You are running as a background cron job."
    if source_type == "agent":
        return "You are running as a dispatched subagent. Your response will be sent to the parent agent."
    return f"You are responding to a user via {source_type}."
```

---

## 9  Implementation Plan

### Phase 1 — `AgentLoop` extraction + concurrent tool execution ✅

| Task | Files | Status |
|------|-------|--------|
| Create `ToolSpec` dataclass with `is_concurrency_safe` flag (default `True`) | `agent/types.py` | ✅ Done |
| Create `AgentLoop` class with `AgentLoopConfig` and `AgentLoopResult` | `agent/loop.py` | ✅ Done |
| Implement concurrent tool execution: partition by `is_concurrency_safe`, `asyncio.gather` for safe, sequential for unsafe | `agent/loop.py` | ✅ Done |
| Refactor `Orchestrator` to use `AgentLoop` internally | `orchestrator/orchestrator.py` | ✅ Done |
| Unit tests for `AgentLoop` (mock backend + tools) | `tests/test_agent_loop.py` | ✅ Done |

**Exit criteria:** ✅ Existing orchestrator tests pass with `AgentLoop` under the
hood. Safe tools run concurrently; unsafe tools run sequentially.

### Phase 2 — File tools, scratchpad, and search tools ✅

| Task | Files | Status |
|------|-------|--------|
| Simplify `@tool` decorator to explicit JSON Schema (BYOO style) | `tools/registry.py` | ✅ Done |
| Implement workspace-rooted file tools | `tools/files.py` | ✅ Done |
| Implement search tools (`grep`, `glob`) | `tools/search.py` | ✅ Done |
| Implement `bash` tool with timeout and output truncation | `tools/bash.py` | ✅ Done |
| Implement git tools (`git_diff`, `git_commit`, `git_log`) | `tools/git.py` | ✅ Done |
| Implement `Scratchpad` class | `agent/scratchpad.py` | ✅ Done |
| Register scratchpad tools; add scratchpad context injection to `AgentLoop` | `tools/scratchpad.py`, `agent/loop.py` | ✅ Done |
| Create `create_coding_tool_registry()` factory | `tools/tool_registry_factory.py` | ✅ Done |
| Implement web search (`web_search`) and URL fetch (`web_fetch`) tools | `tools/web_search.py` | ✅ Done |
| Standardize orchestrator tools (confluence, gerrit, gitlab, jira, slack_search) | `tools/*.py` | ✅ Done |
| Unit tests for all tools (136 passing) | `tests/test_*_tools.py` | ✅ Done |
| Add constant annotation rule to CONTRIBUTING.md | `CONTRIBUTING.md` | ✅ Done |

**Exit criteria:** ✅ A `ToolRegistry` with all coding file tools can be created.
Scratchpad reads/writes work and are included in `AgentLoopResult`. All
orchestrator tools use the `@tool` decorator with closures (no global state).

### Phase 3 — Context compaction + basic skill loading + prompt builder ✅

| Task | Files | Status |
|------|-------|--------|
| Implement micro-compaction (tool result truncation at configurable char limit) | `agent/compaction.py` | ✅ Done |
| Implement full compaction (LLM summary + session roll with synthetic messages) | `agent/compaction.py` | ✅ Done |
| Integrate compaction into `AgentLoop` (micro on every round, full on threshold) | `agent/loop.py` | ✅ Done |
| Add compaction configuration to `AgentLoopConfig` | `agent/types.py`, `config.py` | ✅ Done |
| Implement `SkillLoader` — scan skills directory, load by name | `agent/skill_loader.py` | ✅ Done |
| Implement `skill` tool with enum of available skill IDs | `tools/skill.py` | ✅ Done |
| Implement layered `LayeredPromptBuilder` with cache boundary | `agent/prompt_builder.py` | ✅ Done |
| Implement channel hint layer (user / agent / cron) | `agent/prompt_builder.py` | ✅ Done |
| Unit tests for compaction (truncation, summary, session roll) | `tests/test_compaction.py` | ✅ Done |
| Unit tests for skill loading | `tests/test_skill_loader.py` | ✅ Done |
| Unit tests for prompt builder (layer ordering, cache boundary) | `tests/test_prompt_builder.py` | ✅ Done |

**Exit criteria:** ✅ Long conversations compact without crashing. Skills load
via tool. System prompt has cache boundary and channel hint.

---

## 10  Testing Plan

### 10.1 Unit Tests

| Test | What it verifies |
|------|-----------------|
| `AgentLoop` with mock backend and tools | Multi-turn loop, tool execution, safety limit, truncation handling |
| `AgentLoop` concurrent tool execution | Safe tools run via `asyncio.gather`; unsafe run sequentially; mixed batches respect ordering |
| `AgentLoop` with scratchpad | Scratchpad context injection, read/write tools, snapshot in result |
| `AgentLoop` approval gate | Write tool gating (orchestrator mode), pre-approved pass-through (sub-agent mode) |
| `Scratchpad` class | Read, write, append, size cap, snapshot |
| `Orchestrator` refactor | All existing tests pass with `AgentLoop` under the hood |
| File tools path validation | Rejects `..` traversals, absolute paths, paths outside workspace root |
| File tools output truncation | Large file reads and grep results truncated at configured limits |
| `edit_file` replacement | Correct old→new string replacement; fails on non-unique matches |
| `bash` tool timeout | Commands killed after timeout; stderr captured |
| Micro-compaction | Tool results > 10K chars are truncated; annotation appended |
| Full compaction | Summary generated; session rolled; newest messages preserved |
| Compaction threshold | Compaction triggers at configured token threshold; not before |
| `SkillLoader` | Scans directory; loads by name; returns content |
| `skill` tool | Returns skill content as tool result; enum matches available skills |
| `PromptBuilder` layer ordering | Layers in correct order; cache boundary present; channel hint correct |

### 10.2 Integration Tests

| Test | What it verifies |
|------|-----------------|
| Orchestrator + `AgentLoop` end-to-end | Slack message → agent loop with file tools → response |
| Long conversation compaction | 50+ message conversation triggers full compaction; conversation continues coherently |
| Skill-guided coding task | Load a coding skill → agent follows skill instructions |

---

## 11  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | What token threshold should trigger full compaction? | Claude Code uses ~128K threshold with 8K summary target. Start with 80% of model's context window. |
| 2 | Should the compaction summary be generated by the same model or a cheaper/faster model? | Using a cheaper model (e.g. Haiku) saves cost but may lose nuance. Start with same model; optimize later. |
| 3 | What should the initial skill set include? | Candidates: `code-review`, `refactor`, `test-writing`, `debugging`. Start with 2-3 and expand. |
| 4 | Should skill content be injected as a tool result or a system prompt layer? | Tool result (BYOO approach) keeps skills out of the cached prompt prefix. System prompt injection would cache the skill. Start with tool result; evaluate cache efficiency. |

---

### Sources

- [Original M2 Design Document](design_m2.md)
- [M1 Design Document](design_m1.md)
- [Orchestrator Design](orchestrator_design.md)
- [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md) — §3 (agent loop), §5 (compaction), §6 (skills), §10 (prompt layering)
- [GAPS.md](../GAPS.md) — feature tracking
