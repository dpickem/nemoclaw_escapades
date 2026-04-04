# Orchestrator Agent — Design Document

> **Status:** Proposed
>
> **Last updated:** 2026-04-01
>
> **Related:**
> [Design Doc §3.1](design.md#31--key-components) |
> [NMB Design](nmb_design.md) |
> [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) |
> [Hermes Deep Dive §4](deep_dives/hermes_deep_dive.md#4--the-agent-loop)

---

## Table of Contents

1. [Overview](#1--overview)
2. [Architecture Layers](#2--architecture-layers)
3. [The Agent Loop](#3--the-agent-loop)
4. [System Prompt Construction](#4--system-prompt-construction)
5. [Tool System](#5--tool-system)
6. [Coordinator Mode — Multi-Agent Orchestration](#6--coordinator-mode--multi-agent-orchestration)
7. [Permission & Approval System](#7--permission--approval-system)
8. [Session Management & Compaction](#8--session-management--compaction)
9. [Model Behavioral Contract — Defensive LLM Programming](#9--model-behavioral-contract--defensive-llm-programming)
10. [Task Store](#10--task-store)
11. [Proactive Agent Tick](#11--proactive-agent-tick)
12. [Reference Implementations](#12--reference-implementations)

---

## 1  Overview

The orchestrator is the "main brain" of NemoClaw Escapades. It receives tasks
from Slack (or cron, or the web UI), applies policies, delegates to sub-agents
running in isolated OpenShell sandboxes, and manages bookkeeping of running
tasks and their results. It communicates with sub-agents via the
[NMB](nmb_design.md) and with the user via the Slack connector.

This document covers the orchestrator's internal architecture — the agent loop,
tool system, multi-agent coordination, session management, and defensive
programming patterns. It draws heavily from the
[Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md), which revealed
production-grade patterns for all of these concerns.

---

## 2  Architecture Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Orchestrator Agent                                  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Connector Layer                                               │  │
│  │  (Slack, Web UI, future: Telegram, IDE/ACP)                    │  │
│  └──────────────────────────┬─────────────────────────────────────┘  │
│                              │                                       │
│  ┌──────────────────────────┴─────────────────────────────────────┐  │
│  │  Agent Loop (async generator)                                  │  │
│  │                                                                │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │  │
│  │  │ System      │  │  Inference   │  │  Streaming Tool       │  │  │
│  │  │ Prompt      │  │  Backend     │  │  Executor             │  │  │
│  │  │ Builder     │  │              │  │                       │  │  │
│  │  │             │  │ • Inference  │  │ • Tools execute       │  │  │
│  │  │ • identity  │  │   Hub       │  │   DURING streaming    │  │  │
│  │  │ • safety    │  │ • Anthropic  │  │ • Concurrent-safe     │  │  │
│  │  │   rules     │  │ • OpenAI    │  │   tools run parallel  │  │  │
│  │  │ • context   │  │ • custom    │  │ • Permission gating   │  │  │
│  │  │             │  │             │  │   before execution    │  │  │
│  │  └─────────────┘  └──────────────┘  └───────────────────────┘  │  │
│  │                                                                │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │  │
│  │  │ Session     │  │  Context     │  │  Transcript Repair    │  │  │
│  │  │ Persistence │  │  Management  │  │  (behavioral          │  │  │
│  │  │             │  │              │  │   contract)           │  │  │
│  │  │ • SQLite    │  │ • msg-count │  │                       │  │  │
│  │  │ • FTS5      │  │   truncation│  │ • orphan stripping    │  │  │
│  │  │ • export    │  │ • 3-tier    │  │ • JSON fallback       │  │  │
│  │  │             │  │   (M2+)     │  │ • placeholder inject  │  │  │
│  │  └─────────────┘  └──────────────┘  └───────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                              │                                       │
│  ┌──────────────────────────┴─────────────────────────────────────┐  │
│  │  Coordination Layer                                            │  │
│  │                                                                │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │  │
│  │  │ Coordinator │  │  Task Store  │  │  Approval Gate        │  │  │
│  │  │ Mode        │  │  (SQLite)    │  │                       │  │  │
│  │  │             │  │              │  │ • tiered classifier   │  │  │
│  │  │ • parallel  │  │ • CRUD      │  │ • async Slack         │  │  │
│  │  │   dispatch  │  │ • lifecycle  │  │   escalation          │  │  │
│  │  │ • result    │  │ • history   │  │ • audit log           │  │  │
│  │  │   synthesis │  │              │  │                       │  │  │
│  │  └─────────────┘  └──────────────┘  └───────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                              │                                       │
│  ┌──────────────────────────┴─────────────────────────────────────┐  │
│  │  NMB Client  →  messages.local  →  NMB Broker  →  Sub-Agents   │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3  The Agent Loop

Inspired by Claude Code's `async function* query()` — a streaming-first
async generator that yields events as they arrive.

### Python equivalent

```python
async def agent_loop(user_input: str, session: Session) -> AsyncIterator[Event]:
    """Core agent loop. Yields events as they stream from the LLM."""
    messages = session.build_messages(user_input)
    system_prompt = build_system_prompt(session.context)

    while True:
        async for event in inference.stream(system_prompt, messages, tools):
            match event:
                case TextDelta(text):
                    yield StreamEvent(type="text", data=text)

                case ToolUse(id, name, input):
                    if not await permission_check(name, input, session.mode):
                        result = format_denial(name)
                    else:
                        result = await execute_tool(name, input)
                    messages.append(tool_result(id, result))
                    yield StreamEvent(type="tool_result", data=result)

                case EndTurn():
                    session.persist()
                    return

        # If we reach here, more tool results need processing — loop back
```

### Key design choices (from Claude Code analysis)

1. **Streaming tool execution** — tools execute as soon as their `tool_use`
   block completes in the stream, not after the entire response finishes.
   This cuts perceived latency by 50%+ for multi-tool turns.

2. **Concurrent-safe tools run in parallel** — tools that declare
   `is_concurrency_safe = True` (e.g., `read_file`, `grep`) can execute
   simultaneously when the LLM requests multiple tool calls in one turn.

3. **Recovery on token-limit cutoff** — if the model's output is truncated
   by the token limit, the loop automatically retries up to 3 times with a
   "resume directly, no apology, no recap" continuation prompt.

---

## 4  System Prompt Construction

The system prompt assembles several sections:

1. **Agent identity and behavioral guidelines**
2. **Safety rules (sandwich pattern — START)**
3. **Tool descriptions** (from tool registry, when tools are active in M2+)
4. **Current date/time, OS, shell context**
5. **Project context** (from SKILL.md / config files)
6. **Memory files** (working memory, when available in M5+)
7. **Active task context**
8. **Safety rules (sandwich pattern — END)**

The prompt is loaded from a configuration file so it can be changed without
modifying code.

### NO_TOOLS sandwich pattern

Security-critical instructions (safety rules, tool restrictions) appear at
the **start** of the system prompt AND again at the **end**. This ensures
enforcement even under adversarial prompt conditions where the model might
"forget" early instructions.

### Future: cache-aware prompt boundary

When cost optimization becomes important (M2+), the system prompt should be
split into a **static prefix** (cacheable across turns) and a **dynamic
suffix** (per-turn). Claude Code uses a `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`
marker for this — providers that support prompt caching can skip
re-processing ~90% of the system prompt on subsequent turns. This is
deferred from M1 to keep the initial system prompt simple and transparent.

---

## 5  Tool System

The tool system combines patterns from both Hermes and Claude Code:

- **Hermes** contributes: self-registering tools via a central registry,
  named toolset bundles with platform presets, progressive disclosure for
  token-efficient skill loading, concurrent/sequential execution modes,
  approval callbacks for dangerous commands, and MCP dynamic toolsets.
  See [Hermes Deep Dive §6](deep_dives/hermes_deep_dive.md#6--tools-runtime).
- **Claude Code** contributes: 5-layer filtering pipeline, `ToolSpec`
  declarations with JSON Schema + permission levels, and the `ToolSearch`
  meta-tool for deferred loading.
  See [Claude Code Deep Dive §8](deep_dives/claude_code_deep_dive.md#8--tool-system).

### Tool registration (from Hermes)

Tools are self-registering Python functions. Each tool module calls
`registry.register()` at import time, keeping tool definitions co-located
with their implementation:

```python
from nemoclaw.tools.registry import register

@register(
    name="read_file",
    description="Read a text file from the workspace.",
    toolset="files",             # Hermes-style toolset grouping
    input_schema={...},          # JSON Schema
    required_permission=PermissionMode.ReadOnly,
    is_concurrency_safe=True,
)
async def read_file(path: str, offset: int = 0, limit: int | None = None) -> str:
    ...
```

The registry auto-discovers tools at startup. `model_tools.py` (Hermes
pattern) builds the schema payload for the LLM from the registry.

### Toolset bundles (from Hermes)

Tools are grouped into named **toolsets** — logical bundles that can be
enabled/disabled together:

| Toolset | Tools | Notes |
|---------|-------|-------|
| `terminal` | `bash`, `process_*` | Shell execution |
| `files` | `read_file`, `write_file`, `edit_file`, `glob`, `grep` | Filesystem access |
| `web` | `web_fetch`, `web_search` | Internet access (requires API key) |
| `memory` | `memory_*`, `session_search` | Memory system tools (M5+) |
| `skills` | `skills_list`, `skill_view`, `skill_manage` | Skill CRUD (M6+) |
| `mcp` | *(dynamic)* | Loaded from connected MCP servers |

**Platform presets** select which toolsets are active for each context:
- `orchestrator` preset: all toolsets
- `coding-agent` preset: `terminal` + `files`
- `review-agent` preset: `files` only
- `research-agent` preset: `files` + `web`

### Tool spec declaration (from Claude Code)

Each registered tool carries metadata for filtering and permission checks:

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    toolset: str                 # Hermes-style toolset grouping
    input_schema: dict           # JSON Schema
    required_permission: PermissionMode
    is_read_only: bool
    is_concurrency_safe: bool    # safe to run in parallel with other tools
    is_destructive: bool         # requires extra confirmation
```

### 5-layer filtering pipeline (from Claude Code)

Before tools reach the LLM, they pass through five filtering layers:

```
Layer 1: Full registry (all self-registered tool definitions)
    │
    ▼
Layer 2: Toolset/environment gating (platform presets select which toolsets are active)
    │
    ▼
Layer 3: Deny-rule pruning (per-agent deny lists remove tools before model sees them)
    │
    ▼
Layer 4: Permission-mode filtering (read-only agents don't see write tools)
    │
    ▼
Layer 5: Execution-mode rewriting (coordinator gets Agent+TaskStop; workers get Bash+File*)
```

### Progressive disclosure & deferred loading (from Hermes + Claude Code)

Both Hermes and Claude Code solve the same problem — too many tools to fit
in the prompt — but with complementary approaches:

**Hermes's progressive disclosure** for skills:

| Level | Call | Tokens | What loads |
|-------|------|--------|-----------|
| 0 | `skills_list()` | ~3k | Names + descriptions only |
| 1 | `skill_view(name)` | Varies | Full SKILL.md + metadata |
| 2 | `skill_view(name, path)` | Varies | Specific reference file |

The agent sees a cheap directory listing first (Level 0), then loads full
definitions only for the skills it actually needs. This keeps token costs
bounded regardless of how many skills exist.

**Claude Code's `ToolSearch`** for deferred tool definitions:

1. LLM calls `ToolSearch(query="database migration")`
2. System returns matching tool definitions with full schemas
3. LLM can now use the discovered tool in subsequent turns

NemoClaw should adopt **both patterns**: progressive disclosure for skills
(where the count grows unboundedly as the agent creates new skills) and
ToolSearch for tools (where the count grows as MCP servers and plugins are
added). The two mechanisms complement each other:

- **Skills** use Hermes's 3-level progressive disclosure (skills are
  procedural knowledge — the agent needs to read the full SKILL.md to
  execute them, so the drill-down pattern fits naturally)
- **Tools** use Claude Code's ToolSearch (tools are API definitions — the
  agent just needs the schema, not a multi-page procedure, so a search
  + load pattern fits better)

### Execution modes (from Hermes)

- **Sequential:** for single or interactive tools (e.g., bash commands that
  need user approval).
- **Concurrent:** for multiple non-interactive tools (e.g., parallel file
  reads). Results are reinserted in the original tool-call order to maintain
  a deterministic transcript.

Claude Code's `StreamingToolExecutor` adds a third mode:

- **Streaming-concurrent:** tools begin executing as their `tool_use` blocks
  complete in the LLM's streaming response, not after the full response is
  generated. This is the default for M2+ when the agent has tools.

---

## 6  Coordinator Mode — Multi-Agent Orchestration

Inspired by Claude Code's `COORDINATOR_MODE` but adapted for NMB's
cross-sandbox isolation model. The coordinator runs inside the orchestrator
and uses NMB as the transport layer.

### Coordinator pattern

```
┌──────────────────────────────────────────────────────────────────┐
│  Coordinator (orchestrator agent)                                 │
│                                                                  │
│  1. Decompose task into parallel work units                      │
│  2. Select tool surface per sub-agent type:                      │
│     • Coding: bash, read_file, edit_file, write_file, grep       │
│     • Review: read_file, grep, glob                              │
│     • Research: web_fetch, web_search, read_file                 │
│  3. Spawn sandboxes (multiple of the same type allowed)          │
│  4. Dispatch via NMB: task.assign → each sandbox                 │
│  5. Monitor progress via pub/sub: progress.* channels            │
│  6. Collect results: task.complete from each sandbox             │
│  7. Synthesize: combine results into coherent output             │
└──────┬──────────┬──────────┬──────────┬──────────┬───────────────┘
       │          │          │          │          │
       ▼          ▼          ▼          ▼          ▼
  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
  │Coding-1│ │Coding-2│ │Coding-3│ │Review-1│ │Research│
  │ auth   │ │ api    │ │ tests  │ │        │ │        │
  │ module │ │ layer  │ │        │ │        │ │        │
  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘
     ▲           ▲           ▲
     └───────────┴───────────┘
      Each in its own sandbox with
      a seeded copy of the source files
      (no git — diffs sent back via NMB)
```

### Parallel agents of the same type

A key scaling pattern: the coordinator can spawn **multiple agents of the
same type** working in parallel on different work units. This is how large
tasks get decomposed:

| Scenario | Agents spawned | Isolation mechanism |
|----------|---------------|---------------------|
| Refactor 3 independent modules | 3 coding agents, each in its own sandbox | Separate filesystems — no conflicts during work; coordinator merges results |
| Review a large PR split by area | 2 review agents, each reviewing a subset of files | Read-only sandboxes — no conflict risk; coordinator merges feedback |
| Research + implement | 1 research agent + 1 coding agent in parallel | Different sandboxes, different tool surfaces |
| Batch migration (e.g., API upgrade across 20 files) | 5-10 coding agents, each handling a subset | Claude Code's `batch` skill pattern: decompose → distribute → verify |

**Naming convention:** sandboxes are named `{agent_type}-{task_id}` (e.g.,
`coding-abc123`, `coding-def456`). The coordinator tracks which sandbox is
working on which work unit via the [Task Store](#10--task-store). NMB
messages are routed by sandbox name, so multiple coding agents don't
interfere with each other.

**Sandbox-level isolation:** Each coding agent runs in its own OpenShell
sandbox with a **seeded copy** of the relevant source files. Sandboxes have
separate filesystems, process namespaces, and network policies. There is no
shared state between parallel agents — no git, no branches, no commits
inside the sandbox. Coordination happens exclusively through NMB messages.

The coding agent's job is to **edit files and produce a diff**, not to
manage version control. Git operations (branching, committing, merging,
pushing) are the coordinator's responsibility.

The coordinator seeds and collects from each sandbox:
1. `openshell sandbox create coding-{task_id}` with the coding-agent policy
2. Upload the relevant source files into the sandbox
3. `task.assign` via NMB with the work-unit scope (which files/modules to
   modify)
4. The agent edits files inside its sandbox and produces a diff
5. On `task.complete`, the agent sends the diff back via NMB
6. The coordinator receives the diff and applies it to its own working copy

Since each sandbox operates on an isolated copy, there are no conflicts or
lock contention during parallel work. Conflicts only surface when the
coordinator merges multiple diffs into its working copy.

**Concurrency limits:** The coordinator enforces per-type concurrency caps
(from [design.md §7](design.md#7--capabilities-the-system-should-eventually-have)):

```python
CONCURRENCY_CAPS = {
    "coding": 5,      # max simultaneous coding agents
    "review": 3,      # max simultaneous review agents
    "research": 2,    # max simultaneous research agents
    "default": 2,     # fallback for unknown types
}
```

Caps prevent resource exhaustion (CPU, memory, inference API rate limits)
and are configurable per deployment. When the cap is reached, new tasks
queue in the [Task Store](#10--task-store) until a slot opens.

**Result synthesis and merge strategy:**

Merging diffs from multiple parallel agents is a first-class concern. Since
sandboxes have no git, the coordinator owns all merge logic — and this can
become a bottleneck when agents touch overlapping files. Three strategies
work together to address this:

**Strategy 1 — Prevention (primary).** The coordinator invests in
decomposition quality *before* dispatching work. This is Claude Code's
`batch` skill approach: research the codebase first, identify independent
units (non-overlapping file sets), and only parallelize across boundaries
that won't conflict. Good decomposition eliminates most merge work.

**Strategy 2 — Merge agent (for non-trivial conflicts).** When diffs do
conflict, the coordinator delegates resolution to a dedicated **merge agent**
rather than attempting resolution itself. The merge agent is a specialized
sub-agent role:

```
┌──────────────────────────────────────────────────────────┐
│  Merge flow when conflicts arise                          │
│                                                          │
│  Coordinator receives diffs from Coding-1, Coding-2, ... │
│       │                                                  │
│       ▼                                                  │
│  Apply Coding-1 diff → success                           │
│  Apply Coding-2 diff → CONFLICT on src/auth.py           │
│       │                                                  │
│       ▼                                                  │
│  Spawn merge agent sandbox with:                         │
│    • base version of src/auth.py                         │
│    • Coding-1's version (already applied)                │
│    • Coding-2's version (conflicting)                    │
│    • Both agents' task descriptions (intent context)     │
│       │                                                  │
│       ▼                                                  │
│  Merge agent produces a unified src/auth.py              │
│  that preserves both agents' intent                      │
│       │                                                  │
│       ▼                                                  │
│  Coordinator applies the merged file and continues       │
└──────────────────────────────────────────────────────────┘
```

The merge agent has a narrow tool surface (`read_file` + `edit_file` only)
and receives rich context: the base file, both modified versions, and both
task descriptions explaining *what* each agent was trying to accomplish.
This is much more effective than naive three-way merge because the agent
understands semantic intent, not just text differences.

**Strategy 3 — Re-base and retry (fallback).** If the merge agent fails or
the conflict is too complex, the coordinator can re-seed a new coding-agent
sandbox with the already-merged state and ask the original agent to redo its
work against the updated baseline. This is slower (the agent redoes work)
but guarantees a clean result.

**The complete synthesis flow:**

1. Collect diffs from all sandboxes (delivered via NMB `task.complete`)
2. Sort diffs by size (apply largest first — they are most likely to be the
   "primary" changes that smaller diffs should adapt to)
3. Apply diffs sequentially to the coordinator's working copy:
   - Clean apply → continue
   - Conflict → spawn merge agent with base + both versions + intent context
   - Merge agent fails → re-seed and retry (strategy 3)
4. Run a review agent across the combined changes (optional)
5. Commit and push the merged result (git operations happen only in the
   coordinator, never inside sub-agent sandboxes)
6. Destroy all ephemeral sandboxes
7. Report the combined result to the user via Slack

The merge agent is a lightweight role — it doesn't need bash or web access,
just file read/edit. It can be added to the agent type registry alongside
coding, review, and research agents:

| Agent type | Tool surface | Purpose |
|-----------|-------------|---------|
| `coding` | bash, read_file, edit_file, write_file, grep, glob | Write code |
| `review` | read_file, grep, glob | Review code |
| `research` | web_fetch, web_search, read_file | Research topics |
| `merge` | read_file, edit_file | Resolve conflicting diffs with semantic understanding |

### Permission scope enforcement

Claude Code has a known security gap: sub-agents can widen permissions beyond
their parent's scope. NemoClaw solves this with NMB policy-gated access:

- Each sub-agent sandbox has an OpenShell policy that restricts its tool
  surface and network access
- The coordinator cannot spawn a sub-agent with broader permissions than
  the coordinator's own policy allows
- NMB message-type restrictions can further limit what a sandbox can do
  (e.g., a coding sandbox can send `task.complete` but not `task.assign`)

### Session forking

Inspired by Claude Code's `FORK_SUBAGENT` / `/fork` command. A new NMB
message type `task.fork` sends a serialized conversation snapshot to create
a sub-agent that already has the parent's context:

```python
await bus.send("new-sandbox", "task.fork", {
    "parent_session": session.serialize(),
    "focus": "Refactor the auth module only",
    "tool_surface": ["bash", "read_file", "edit_file", "write_file"]
})
```

This is cheaper than re-packaging all context in `task.assign` and
preserves conversation history for continuity.

---

## 7  Permission & Approval System

### Three-tier permission model

Adopted from Claude Code:

| Mode | Label | Scope |
|------|-------|-------|
| `ReadOnly` | `read-only` | Read/search tools only |
| `WorkspaceWrite` | `workspace-write` | Edit files inside the workspace |
| `FullAccess` | `full-access` | Unrestricted tool access |

### Tiered auto-approval classifier

Adapted from Claude Code's two-stage YOLO classifier for NemoClaw's
always-on daemon context (user may be asleep):

```
Tool call requested
        │
        ▼
┌───────────────────────┐
│  Stage 1: Fast        │
│  Pattern matching     │
│  Known-safe patterns  │
│  (e.g., read_file in  │
│   workspace, grep)    │
└────────┬──────────────┘
         │
    ┌────┴────┐
    │         │
  approve   uncertain
    │         │
    │    ┌────┴─────────────────┐
    │    │  Stage 2: LLM eval   │
    │    │  Is this safe given   │
    │    │  the current task?    │
    │    └────────┬──────────────┘
    │             │
    │        ┌────┴────┐
    │        │         │
    │      approve    deny/uncertain
    │        │         │
    └────┬───┘    ┌────┘
         │        │
         ▼        ▼
      execute   escalate to Slack
                (async approval)
```

The Slack escalation is critical for always-on operation — dangerous
operations pause and send a Slack message to the user for approval. The
user can approve from their phone at any time.

---

## 8  Session Management & Compaction

### M1: simple message-count truncation

In M1, the orchestrator uses in-memory thread history keyed by Slack
`thread_ts`. When the thread exceeds a configured limit (default: 50
messages), the oldest messages are dropped. This is adequate for the
conversational-only M1 loop.

### Target architecture (M2+): three-tier compaction

Adopted from Claude Code's production-proven approach. This becomes essential
when tool outputs and multi-sandbox coordination produce much larger
transcripts:

| Tier | Token Budget | API Call? | Purpose |
|------|-------------|-----------|---------|
| **Micro** | ~256 tokens | No | Local heuristic pruning: strip tool outputs, collapse verbose results |
| **Full** | ~4K tokens | Yes | LLM-generated summary of older conversation history |
| **Session memory** | Zero-cost | No | In-memory cache of extracted key facts (user preferences, project conventions, active task state) |

### Session persistence

Unlike Claude Code's JSON files, NemoClaw uses **SQLite from the start**
(matching Hermes):

- `sessions` table: session metadata (id, created, updated, title)
- `messages` table: conversation messages with role, content blocks
- FTS5 virtual table for full-text search across all sessions
- WAL mode for concurrent reads (web UI) + writes (orchestrator)

---

## 9  Model Behavioral Contract — Defensive LLM Programming

Adopted from the [Claude Code analysis](deep_dives/claude_code_deep_dive.md#24--model-behavioral-contract).
The orchestrator must handle malformed model output gracefully.

### Transcript repair rules

| Violation | Repair |
|-----------|--------|
| Missing `tool_result` for a `tool_use` | Inject synthetic placeholder: `"[Tool execution interrupted]"` |
| Duplicate tool-use IDs | Deduplicate, keep first occurrence |
| Orphaned `tool_result` (no matching `tool_use`) | Strip from transcript |
| Empty/whitespace-only assistant message | Filter or replace with placeholder |
| Trailing thinking blocks | Truncate before sending to API |
| Malformed JSON in `tool_use.input` | Fall back to `{}`; return error `tool_result` with schema hint |

### Recovery prompts

| Scenario | Recovery message |
|----------|-----------------|
| Token-limit cutoff | "Resume directly, no apology, no recap. Pick up mid-thought. Break remaining work into smaller pieces." |
| User cancellation | "The user cancelled this operation. Stop and wait for new instructions. Do not assume the tool action succeeded." |
| Permission denial | "Permission denied for this operation. You may try a reasonable alternative, but do not covertly bypass the denial." |
| Classifier outage | "The approval system is temporarily unavailable. Wait briefly and retry, or continue with other tasks that don't require approval." |
| Deferred tool not loaded | "This tool's schema is not loaded. Call ToolSearch to load it, then retry." |

---

## 10  Task Store

A persistent task database that the coordinator uses to track work across
sub-agents. Sits alongside the session store in SQLite.

### Schema

```sql
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT,                  -- for sub-tasks
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
        -- queued | assigned | running | review | complete | failed | cancelled
    agent_type      TEXT,                  -- coding | review | research | note_taking
    sandbox_id      TEXT,                  -- OpenShell sandbox ID
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    assigned_at     REAL,
    completed_at    REAL,
    prompt          TEXT NOT NULL,          -- the task description
    result          TEXT,                   -- final output
    tool_surface    TEXT,                   -- JSON array of allowed tools
    metadata        TEXT                    -- JSON blob for extensibility
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);
CREATE INDEX idx_tasks_agent ON tasks(agent_type);
```

### Task lifecycle

```
queued  ──(coordinator dispatches)──▶  assigned
                                          │
                                     (sandbox picks up)
                                          │
                                          ▼
                                       running
                                          │
                               ┌──────────┴──────────┐
                               │                     │
                          (needs review)         (done)
                               │                     │
                               ▼                     │
                            review                   │
                               │                     │
                          (approved)                  │
                               │                     │
                               ▼                     ▼
                            complete              complete
```

The web UI's kanban board (design.md §9.2.1) reads directly from this table.

---

## 11  Proactive Agent Tick

For always-on daemon behavior, adopted from Claude Code's `KAIROS` pattern:

1. The orchestrator runs a background timer that fires periodic
   `<proactive_tick>` events
2. On each tick, the agent loop checks for pending work:
   - Unread Slack messages
   - Cron jobs due to fire
   - Stalled tasks needing attention
   - Memory consolidation opportunities
3. If useful work exists, the agent processes it
4. If nothing useful exists, the agent sleeps until the next tick or until
   an external event (Slack message, NMB message) wakes it

Ticks are hidden from the conversation transcript (`is_meta: True`).
The user never sees the periodic wake-ups.

---

## 12  Reference Implementations

| Component | Claude Code | Hermes | OpenClaw |
|-----------|-------------|--------|----------|
| **Agent loop** | `async function* query()` (streaming generator) | `AIAgent.run_agent()` (callback-based) | Pi runtime (streaming) |
| **System prompt** | `SystemPromptBuilder` with cache boundary | `prompt_builder.py` (frozen at session start) | Dynamic assembly |
| **Tool registration** | Static `mvp_tool_specs()` + `GlobalToolRegistry` | Self-registering `registry.register()` + `model_tools.py` builder | Tool registry + plugins |
| **Toolset bundles** | Simple/REPL/coordinator modes | Named toolsets (`terminal`, `files`, `web`, `memory`, `skills`, `mcp`) with platform presets | Plugin-based bundles |
| **Deferred loading** | `ToolSearch` meta-tool | 3-level progressive disclosure for skills (`skills_list` → `skill_view` → `skill_view(path)`) | None |
| **Tool dispatch** | `execute_tool()` match table | `model_tools.py` dispatcher | Tool registry |
| **Compaction** | 3-tier (micro/full/session memory) | Mid-convo compression | Configurable |
| **Permission** | 3-tier + 2-stage YOLO classifier | Trust-based | 3-tier + sandbox |
| **Multi-agent** | Coordinator mode + AgentTool + UDS | `sessions_send` (in-process) | Multi-agent routing |
| **Task management** | `TaskCreate/Get/Update/List/Stop/Output` tools | None | None |
| **Session persistence** | JSON files | SQLite with FTS5 | SQLite |
| **Behavioral contract** | 21 invariants, repair mechanisms | Basic error handling | Basic error handling |

### Sources

- [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) — primary
  source for agent loop, 5-layer tool filtering, ToolSearch deferred loading,
  compaction, permission model, behavioral contract, and coordinator mode.
- [Hermes Deep Dive](deep_dives/hermes_deep_dive.md) — primary source for
  self-registering tool registry, toolset bundles with platform presets,
  progressive disclosure for skills, concurrent/sequential execution modes,
  self-learning loop, memory system, and session persistence.
- [NMB Design](nmb_design.md) — transport layer for multi-agent coordination.
- [Design Doc §3.1](design.md#31--key-components) — high-level orchestrator
  requirements.
