# Tools Integration Strategy (Hermes + OpenClaw + Claude Code)

> **Status:** Proposed
>
> **Last updated:** 2026-04-10
>
> **Related:**
> [Design Doc §4-§6](design.md#4--milestones) |
> [Orchestrator Design §5](orchestrator_design.md#5--tool-system) |
> [Design M2](design_m2.md) |
> [Audit DB Design](audit_db_design.md) |
> [Agent Trace Design](agent_trace_design.md) |
> [Hermes Deep Dive §6-§9](deep_dives/hermes_deep_dive.md#6--tools-runtime) |
> [OpenClaw Deep Dive §7](deep_dives/openclaw_deep_dive.md#7--tools--plugins-system) |
> [Claude Code Deep Dive §5,§7,§8,§11](deep_dives/claude_code_deep_dive.md#5--feature-flags--tool-gating)

---

## Table of Contents

1. [Overview](#1--overview)
2. [Current Baseline](#2--current-baseline)
3. [Goals and Non-Goals](#3--goals-and-non-goals)
4. [Hermes Tool Integration Strategy](#4--hermes-tool-integration-strategy)
5. [OpenClaw Tool Scan and Adoption Strategy](#5--openclaw-tool-scan-and-adoption-strategy)
6. [Claude Code Tool Patterns](#6--claude-code-tool-patterns)
7. [Target Architecture in NemoClaw Escapades](#7--target-architecture-in-nemoclaw-escapades)
8. [Security, Policy, and Approval Model](#8--security-policy-and-approval-model)
9. [Phased Implementation Plan](#9--phased-implementation-plan)
10. [Testing and Rollout Plan](#10--testing-and-rollout-plan)
11. [Open Questions](#11--open-questions)

---

## 1  Overview

This document defines how NemoClaw Escapades should evolve its tool system by
lifting proven patterns from:

- **Hermes tools** (`hermes-agent/tools`) for procedural memory, persistent
  memory, session recall, scheduling, and sub-agent delegation.
- **OpenClaw agent tools** (`openclaw/src/agents/tools`) for robust
  multi-session orchestration, planning primitives, guarded web I/O, and
  schema/runtime hardening patterns.
- **Claude Code tools** for tool runtime architecture (streaming execution,
  parallel tool calls, concurrency safety, truncation recovery), permission
  tiering, feature-flag gating, and sub-agent tool-surface control.

The intent is **not** to port either toolset verbatim. The intent is to
adopt the highest-leverage capabilities while preserving:

1. OpenShell sandbox isolation
2. explicit write approvals
3. model-agnostic backend architecture
4. SQLite-first auditability
5. NMB-based multi-sandbox coordination

---

## 2  Current Baseline

Today the orchestrator registers a focused enterprise tool surface:

- `jira_*`
- `confluence_*`
- `gitlab_*`
- `gerrit_*`
- `slack_*` (search/history/send)

Strengths:

- Strong NVIDIA workflow coverage
- clear read/write distinction in tool specs
- per-tool availability checks
- centralized tool registry and output truncation
- audit logging for tool calls

Gaps versus roadmap (M5-M6):

- no `SKILL.md` lifecycle tools (list/view/manage/sync/guard)
- no bounded persistent memory (`MEMORY`/`USER`-like layer)
- no cross-session recall tool for prior agent conversations
- no first-class scheduler/cron tool
- no explicit planning/tasklist primitives
- no sub-agent control tools for list/steer/kill/yield
- no extensible dynamic tool ingestion (new services require code changes)

---

## 3  Goals and Non-Goals

### 3.1 Goals

1. Add **self-improvement primitives** (skills + memory + session recall).
2. Add **operational primitives** (planning, cron, sub-agent control).
3. Keep **safety guarantees** stronger than upstream defaults.
4. Maintain **tool schema clarity** for reliable function calling.
5. Support progressive expansion via direct tool implementations that talk to
   service APIs (following the `nv-tools` `commands/` + `clients/` pattern).

### 3.2 Non-Goals

1. No wholesale runtime port of Hermes or OpenClaw internals.
2. No adoption of OpenClaw mobile/node/canvas-specific tools in this phase.
3. No bypass of existing write approval gates.
4. No unmanaged host-level execution outside existing OpenShell policy model.

---

## 4  Hermes Tool Integration Strategy

The Hermes list is large; only a subset should be integrated now.

### 4.1 Tier A (Immediate, highest leverage)

#### A1. Skills Runtime

- Source references:
  - `skills_tool.py` (`skills_list`, `skill_view`)
  - `skill_manager_tool.py` (`skill_manage`)
  - `skills_guard.py` (threat scanning)
  - `skills_sync.py` (bundled seed/sync behavior)

- Why:
  - Unlocks procedural memory and reusable workflow capture.
  - Directly supports roadmap item: automatic skill capture and refinement.

- Integration shape:
  - Add a `skills` toolset with:
    - `skills_list`
    - `skill_view`
    - `skill_manage` (`create`, `patch`, `edit`, `delete`, `write_file`, `remove_file`)
  - Add `skills_guard` scan before any persisted skill write/import.
  - Store skills under a project-scoped root (not hardcoded to `~/.hermes`).

#### A2. Persistent Memory

- Source reference: `memory_tool.py`
- Why:
  - Introduces bounded durable memory for stable facts/preferences.
  - Supports M5 memory layering.
- Integration shape:
  - Add single `memory` tool with actions:
    - `add`
    - `replace`
    - `remove`
    - `read`
  - Split targets:
    - `memory` (agent environment/conventions)
    - `user` (user preferences/constraints)
  - keep strict caps to prevent prompt bloat.

#### A3. Session Recall

- Source reference: `session_search_tool.py`
- Why:
  - Enables "what did we do before?" retrieval without polluting hot context.
  - Natural fit with existing SQLite audit posture.
- Integration shape:
  - Add `session_search` tool using local SQLite FTS over session transcripts.
  - Return compact summaries plus source session metadata.

### 4.2 Tier B (Near-term)

#### B1. Task Planning Primitive

- Source references:
  - Hermes `todo_tool.py`
  - OpenClaw `update-plan-tool.ts`
- Why:
  - Forces explicit decomposition and progress tracking for long tasks.
- Integration shape:
  - Add `todo` tool for task list state.
  - Add `update_plan` tool for concise ordered plan snapshots.
  - Prefer `update_plan` for user-facing progress; `todo` for internal state.

#### B2. Cron Scheduler Tool

- Source reference: `cronjob_tools.py`
- Why:
  - Supports proactive background workflows and autonomous maintenance.
- Integration shape:
  - Add `cronjob` tool with action-based API:
    - `create`
    - `list`
    - `update`
    - `pause`
    - `resume`
    - `run`
    - `remove`
  - Integrate with existing approval policy for mutating schedules.

#### B3. Delegation Tool

- Source reference: `delegate_tool.py`
- Why:
  - Standardizes controlled sub-agent spawning with restricted toolsets.
- Integration shape:
  - Add `delegate_task` as orchestrator-level delegation tool.
  - Adapt execution target to OpenShell + NMB model (not in-process threads).

### 4.3 Tier C (Strategic expansion)

#### C1. New Service Integrations (Direct Implementation)

New external services are added as direct tool implementations following the
`nv-tools` pattern: a `clients/<service>.py` module that wraps the service's
REST/GraphQL API, and a `commands/<service>.py` (or `tools/<service>.py`)
module that exposes it as a tool in the `ToolRegistry`.

This is the same approach the current enterprise tools (Jira, GitLab, Gerrit,
Confluence, Slack) already use and has proven advantages over MCP-based
dynamic ingestion:

- **No middleware layer** — one fewer moving part; the tool talks directly to
  the API with full control over auth, retries, pagination, and error mapping.
- **Schema stability** — tool definitions are checked in and versioned; no
  runtime schema discovery surprises.
- **Audit clarity** — the `service` and `command` columns in the audit DB map
  1:1 to a known tool; no opaque `mcp_call` wrapper.
- **Sandbox-friendly** — each tool needs only an egress rule for its API
  endpoint in the OpenShell policy; no need to allow arbitrary MCP server
  connections.

Adding a new service is ~200 lines: a thin async HTTP client + a `ToolSpec`
registration.  The cost is low enough that the indirection of MCP is not
justified for the current set of target services.

#### C2. Large Result Persistence

- Source references:
  - `tool_result_storage.py`
  - `budget_config.py`
  - `checkpoint_manager.py`

- Why:
  - Limits context blowups from large tool outputs and long multi-tool turns.
- Integration shape:
  - spill oversized outputs to workspace temp files + preview stubs.
  - enforce per-turn aggregate char budgets.
  - optionally add checkpoint snapshots before large file mutations.

#### C3. MCP Bridge (Deprioritized)

MCP dynamic tool ingestion (Hermes `mcp_tool.py`, `mcp_oauth.py`) is
**deprioritized**.  The direct-implementation approach (C1) covers all
currently planned services with less complexity and stronger guarantees.

MCP may be revisited if:

- A large number of third-party services need to be integrated and the
  per-service implementation cost becomes prohibitive.
- An MCP server ecosystem matures with standardised auth, versioned schemas,
  and security scanning (currently not the case).
- A partner team provides a curated, trusted MCP server catalog.

If MCP is ever adopted, it should be gated behind:

1. A strict server allowlist (no arbitrary `npx`/`uvx` server spawning).
2. OAuth flows with explicit operator approval and audit.
3. Malware/vulnerability scanning for package-based server bootstraps
   (`osv_check.py` pattern).
4. Sandboxed execution of MCP servers in their own OpenShell sandbox with
   minimal egress.

---

## 5  OpenClaw Tool Scan and Adoption Strategy

OpenClaw `src/agents/tools` includes a broad catalog. The highest-value pieces
for NemoClaw Escapades are orchestration/runtime patterns, not app-specific
tools.

### 5.1 OpenClaw Tool Families (observed)

#### Orchestration and Planning

- `update-plan-tool.ts`
- `sessions-list-tool.ts`
- `sessions-history-tool.ts`
- `sessions-send-tool.ts`
- `sessions-spawn-tool.ts`
- `sessions-yield-tool.ts`
- `subagents-tool.ts`
- `agents-list-tool.ts`
- `session-status-tool.ts`

#### Scheduling

- `cron-tool.ts`

#### Web Tools and Safety

- `web-search.ts`
- `web-fetch.ts`
- `web-guarded-fetch.ts`
- `web-fetch-utils.ts`

#### Messaging and Control Plane

- `message-tool.ts`
- `gateway-tool.ts`

#### Media/Docs (defer for now)

- `image-tool.ts`
- `pdf-tool.ts`
- `tts-tool.ts`
- `video-generate-tool.ts`
- `music-generate-tool.ts`
- `canvas-tool.ts`
- `nodes-tool.ts`

### 5.2 What to Adopt from OpenClaw

#### Adopt now (pattern-level)

1. **`update_plan` contract**
   - simple ordered step list
   - one `in_progress` invariant
   - clear progress UX

2. **Session visibility guards**
   - allow sub-agent session tools only within authorized session trees
   - explicit forbidden responses instead of silent failure

3. **Schema compatibility hardening**
   - flattened schemas where model providers reject `anyOf`/`oneOf`
   - runtime validation as the authoritative guard

4. **Web fetch hardening**
   - SSRF-aware fetch wrappers
   - external content wrappers/sanitization before model exposure

5. **Sub-agent control verbs**
   - list / steer / kill / yield model for active sub-agent runs

#### Adopt later

1. `cron` advanced payload and delivery routing model
2. session run wait/reply snapshot patterns for multi-agent messaging

#### Do not adopt directly

1. `gateway` config mutation/restart tool (too privileged for default agent path)
2. app/node/canvas tools (outside current project scope)
3. broad channel action matrix in `message-tool.ts` (NemoClaw currently Slack-first)

---

## 6  Claude Code Tool Patterns

Claude Code's tool system
([Deep Dive §5, §7, §8, §11](deep_dives/claude_code_deep_dive.md)) is the
most mature of the three reference systems.  Where Hermes provides the best
*tool ideas* (skills, memory, cron) and OpenClaw the best *orchestration
verbs* (plan, sessions, sub-agents), Claude Code's primary contribution is
**tool runtime architecture and safety patterns**.

### 6.1 Tool Runtime Patterns to Adopt

#### Streaming Tool Execution

Claude Code's `StreamingToolExecutor` starts executing a tool the instant the
model finishes emitting the `tool_use` block — while the rest of the response
is still streaming.  This overlaps tool I/O with inference output generation.

**Adopt:** When the `AgentLoop` supports streaming responses, execute each
tool call as its block completes rather than waiting for the full response.

#### Parallel Tool Calls with Concurrency Safety

Each tool declares `isConcurrencySafe()`.  When the model emits multiple tool
calls in one response, safe tools run concurrently.

**Adopt:** Add a `concurrency_safe: bool` field to `ToolSpec` (default
`False`).  Read-only tools (`is_read_only=True`) default to `True`.  The
agent loop uses `asyncio.gather()` for safe tools and sequential execution
for unsafe ones.

#### Truncation Recovery

When output exceeds the context window, Claude Code retries up to 3 times
with a "resume mid-thought" continuation prompt.

**Adopt:** This is already partially implemented via `transcript_repair.py`
and `_continue_truncated()`.  Extend to cover tool-result truncation: when a
tool output exceeds a configurable threshold, spill to a temp file and inject
a preview stub + file reference (see §4.3 C2 Large Result Persistence).

#### Tool Search / Deferred Loading

Claude Code's `ToolSearch` tool enables lazy loading for large registries —
the model searches for tools by keyword instead of seeing all definitions
upfront.

**Adopt (Phase 3):** Add a `tool_search` meta-tool that queries the
`ToolRegistry` by keyword and returns matching `ToolSpec` summaries.  Useful
when the total tool count exceeds ~30 and including all definitions in every
prompt wastes tokens.

#### Tool Name Aliasing

Claude Code maps short names to canonical names (`read` → `read_file`,
`grep` → `grep_search`).

**Adopt:** Add an `aliases: list[str]` field to `ToolSpec`.  The registry
resolves aliases transparently.  Reduces prompt tokens for frequently-used
tools and tolerates model shorthand.

### 6.2 Permission and Gating Patterns to Adopt

#### Three-Tier Permission Model

Claude Code classifies every tool into one of three tiers:

| Tier | Scope | NemoClaw equivalent |
|------|-------|---------------------|
| `read-only` | No side effects | `ToolSpec.is_read_only = True` |
| `workspace-write` | Mutates files/state within the workspace | `ToolSpec.is_read_only = False` + auto-approval for workspace-scoped writes |
| `danger-full-access` | Arbitrary system access (bash, network) | `ToolSpec.is_read_only = False` + mandatory user approval |

**Adopt:** The current binary `is_read_only` flag maps to `read-only` vs.
everything else.  Adding a `permission_tier` enum (`read`, `workspace_write`,
`danger`) enables finer-grained approval: workspace writes can be
auto-approved in trusted contexts while dangerous operations always require
user confirmation.

#### Feature-Flag Gating

Claude Code gates tools behind feature flags (`SleepTool`, `CronCreateTool`,
`WebBrowserTool`, etc.) and internal-only flags (`USER_TYPE === 'ant'`).

**Adopt:** Already planned in §8.3 (`skills.enabled`, `cron.enabled`, etc.).
Claude Code validates that this pattern works at scale with 40+ gated tools.

#### Per-Agent Tool Surface Control

In coordinator mode, Claude Code restricts which tools each sub-agent
receives via the `tool_surface` field in `task.assign`:

```json
{
  "tool_surface": ["bash", "read_file", "edit_file", "write_file", "grep", "glob"]
}
```

Sub-agents see only their declared tools; the coordinator retains the full
surface.

**Adopt:** Already designed in
[Design M2 §6.4](design_m2.md#64--tools-declarative-tool-surface).  Claude
Code confirms the pattern works — the key insight is that the tool surface is
*declared per task*, not per sandbox, so the same sandbox image can serve
different roles.

### 6.3 Additional Tool Ideas from Claude Code

| Claude Code tool | Purpose | NemoClaw adoption | Priority |
|-----------------|---------|-------------------|----------|
| `NotebookEdit` | Edit Jupyter notebook cells | Add if data-science sub-agent is planned | Low |
| `Sleep` | Async wait without blocking a shell | Useful for polling patterns in sub-agents | Medium |
| `REPL` | Execute code in a persistent subprocess | Add for data analysis / prototyping tasks | Low |
| `StructuredOutput` | Return JSON in a requested schema | Useful for tool-to-tool pipelines | Medium |
| `WebBrowser` | Interactive browser automation | Requires headless browser in sandbox; defer | Low |
| `MonitorTool` | Watch for file/process changes | Useful for CI/test-runner integration | Low |
| `SubscribePR` | Watch a PR for updates | Natural fit for Gerrit/GitLab review workflows | Medium |
| `EnterPlanMode` / `ExitPlanMode` | Mode switching between planning and execution | Already handled via system prompt instructions; tool-based switching is cleaner | Medium |

Tools marked **Medium** are candidates for Phase 2–3.  Tools marked **Low**
are deferred unless a specific use case emerges.

### 6.4 Patterns to NOT Adopt

1. **YOLO auto-approval** — Claude Code's two-stage LLM-based permission
   check (64-token fast pass + 4K-token "thinking") is clever but introduces
   a second inference call per write tool.  NemoClaw's explicit user approval
   via Slack buttons is simpler and more auditable.
2. **4,437-line bash parser** — Claude Code parses bash ASTs to detect
   dangerous commands.  NemoClaw runs bash inside OpenShell sandboxes with
   Landlock + seccomp + network policy, making the parser unnecessary.
3. **`dangerouslyDisableSandbox`** — Claude Code allows per-command sandbox
   bypass.  NemoClaw never allows this; the sandbox is the security boundary.
4. **MCP tool namespace prefixing** — relevant only if MCP is adopted (§4.3
   C3, deprioritized).

---

## 7  Target Architecture in NemoClaw Escapades

### Note on Claude Code tool candidates

The tools from §6.3 marked Medium priority are integrated into the phased
plan below.  `StructuredOutput`, `Sleep`, `SubscribePR`, and `EnterPlanMode`
are added to Phase 2.  `tool_search` is Phase 3.

### 7.1 Proposed Toolset Expansion

Keep service tools as-is and add these toolsets:

- `skills`:
  - `skills_list`
  - `skill_view`
  - `skill_manage`
- `memory`:
  - `memory`
  - `session_search`
- `planning`:
  - `todo`
  - `update_plan`
- `orchestration`:
  - `delegate_task`
  - `subagents`
  - `sessions_*` controls (phased)
- `cron`:
  - `cronjob`
New service integrations follow the direct `clients/` + `tools/` pattern (§4.3 C1).

### 7.2 Repository Layout Proposal

```text
src/nemoclaw_escapades/tools/
├── registry.py
├── jira.py
├── confluence.py
├── gitlab.py
├── gerrit.py
├── slack_search.py
├── skills/
│   ├── catalog.py
│   ├── manager.py
│   ├── guard.py
│   └── sync.py
├── memory/
│   ├── memory_store.py
│   └── session_search.py
├── planning/
│   ├── todo.py
│   └── update_plan.py
├── orchestration/
│   ├── delegate.py
│   └── subagents.py
└── scheduling/
    └── cronjob.py
```

### 7.3 Configuration Additions

Add new config blocks (feature-flagged):

- `skills.enabled`
- `memory.enabled`
- `session_search.enabled`
- `planning.enabled`
- `cron.enabled`
- `delegation.enabled`

Each block should include `enabled`, safety toggles, and strict defaults.

---

## 8  Security, Policy, and Approval Model

### 8.1 Write Controls

- keep current `ToolSpec.is_read_only` enforcement.
- classify all new mutating tools as write operations:
  - `skill_manage` write actions
  - `memory` write actions
  - `cronjob` create/update/remove/run
  - delegation commands that spawn/kill/steer runs

### 8.2 Skill Security

- scan all skill content on create/import/update using `skills_guard` pattern.
- block high-severity exfiltration/injection/destructive signatures by default.
- log scan findings to audit trails.

### 8.3 Web and External Service Safety

- apply SSRF-guarded fetch wrappers for any raw URL fetch tools.
- sanitize external content before insertion into model-visible context.
- new service integrations must use the direct client pattern (§4.3 C1) with
  explicit egress rules in the OpenShell policy.

### 8.4 Permission Tiering (from Claude Code)

Extend `ToolSpec` with a `permission_tier` field (§6.2):

| Tier | Approval behaviour | Examples |
|------|--------------------|----------|
| `read` | Never gated | `jira_search`, `skills_list`, `session_search` |
| `workspace_write` | Auto-approved in trusted contexts; user-approved otherwise | `skill_manage`, `memory` write, `todo` |
| `danger` | Always requires explicit user approval | `delegate_task`, `cronjob` create/run, bash |

The existing `is_read_only` flag remains for backward compatibility and maps
to `read` when `True`.  The `ApprovalGate` checks `permission_tier` first,
falling back to the binary flag.

### 8.5 Concurrency Safety Classification

Add `concurrency_safe: bool` to `ToolSpec` (§6.1).  Default classification:

| Safe (parallel OK) | Unsafe (sequential only) |
|--------------------|--------------------------|
| All `is_read_only=True` tools | `skill_manage` write actions |
| `session_search` | `memory` write actions |
| `update_plan` (read current state) | `delegate_task` |
| `todo` (merge semantics) | `cronjob` create/run |

### 8.6 OpenShell Policy Impact

As toolsets are added, update `policies/orchestrator.yaml` incrementally:

1. add minimum egress only for enabled toolsets.
2. prefer REST-rule constrained policies over full CONNECT access.
3. default new toolsets to disabled in production until policy and approval
   behaviors are validated.

---

## 9  Phased Implementation Plan

### Phase 1 (M4-M5 foundation)

1. `skills_list`, `skill_view`, `skill_manage` (create/edit/patch only)
2. `skills_guard`
3. `memory` tool (`add`, `replace`, `remove`, `read`)
4. `session_search`
5. `update_plan`

**Exit criteria:**

- new tools registered behind config flags
- write operations gated and auditable
- skill writes blocked on security scan findings

### Phase 2 (M5 reliability and autonomy)

1. `todo`
2. `cronjob`
3. `delegate_task` (restricted toolset, bounded depth)
4. initial `subagents` controls (list/kill/steer/yield)
5. `structured_output` — return JSON in a caller-specified schema (from Claude Code)
6. `sleep` — async wait without blocking a shell (from Claude Code)
7. `subscribe_pr` — watch a Gerrit CL / GitLab MR for updates (from Claude Code `SubscribePR`)
8. `enter_plan_mode` / `exit_plan_mode` — tool-based mode switching (from Claude Code)
9. Parallel tool execution with `concurrency_safe` flag (§8.5)
10. Permission tiering: `read` / `workspace_write` / `danger` (§8.4)

**Exit criteria:**

- scheduled tasks execute with proper sandbox constraints
- delegation paths enforce spawn limits and visibility boundaries
- parallel-safe tools execute concurrently in the agent loop
- three-tier approval model active

### Phase 3 (M6+ ecosystem expansion)

1. Additional service integrations via direct client pattern (§4.3 C1) — e.g.
   web search, web fetch, calendar, email, Teams, OneDrive.
2. Large-result persistence budgets and optional checkpointing.
3. `tool_search` meta-tool for deferred loading of large registries (§6.1).
4. Tool name aliasing support in `ToolRegistry` (§6.1).
5. Streaming tool execution — start executing tools during response streaming
   (§6.1).
6. MCP bridge (§4.3 C3) — only if the direct-integration cost becomes
   prohibitive or a trusted MCP server ecosystem emerges.

**Exit criteria:**

- new services can be added in ~200 lines (client + tool spec)
- large tool outputs do not blow up prompt context
- tool registry supports 50+ tools without prompt token bloat

---

## 10  Testing and Rollout Plan

### 10.1 Unit Tests

- input schema validation and action parsing
- security scanner pattern detection
- memory cap behavior and replacement semantics
- session visibility and delegation restrictions

### 10.2 Integration Tests

- orchestrator loop with new toolsets enabled in sandbox mode
- write-approval interruptions and resume flows
- cron job lifecycle and persistence
- sub-agent control flows over NMB

### 10.3 Safety Regression Tests

- prompt injection patterns in skill content
- SSRF attempts in web fetch
- unauthorized cross-session access attempts
- unauthorized cross-agent delegation attempts

### 10.4 Rollout

1. ship disabled-by-default
2. enable per-toolset in local dev
3. enable in staging sandbox with audit review
4. gradually enable in always-on environment

---

## 11  Open Questions

1. Should `MEMORY`/`USER` data live in flat files, SQLite, or both (cached files + DB source)?
2. Should cron execution live in orchestrator process or separate scheduler worker?
3. What is the maximum allowed delegation depth in OpenShell multi-sandbox mode?
4. At what scale (number of service integrations) does MCP's indirection
   become worth the complexity over direct implementations?
5. Should `sessions_*` tools be exposed broadly or only in coordinator mode?

---

### Sources

- Hermes tool tree:
  - [NousResearch/hermes-agent/tools](https://github.com/NousResearch/hermes-agent/tree/main/tools)
- OpenClaw tools tree:
  - [openclaw/src/agents/tools](https://github.com/openclaw/openclaw/tree/main/src/agents/tools)
- Claude Code tool architecture:
  - [Claude Code Deep Dive §5, §7, §8, §11](deep_dives/claude_code_deep_dive.md)
- Key OpenClaw tool references:
  - [update-plan-tool.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/update-plan-tool.ts)
  - [sessions-spawn-tool.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/sessions-spawn-tool.ts)
  - [sessions-send-tool.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/sessions-send-tool.ts)
  - [subagents-tool.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/subagents-tool.ts)
  - [cron-tool.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/cron-tool.ts)
  - [web-fetch.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/web-fetch.ts)
  - [web-search.ts](https://github.com/openclaw/openclaw/blob/main/src/agents/tools/web-search.ts)
