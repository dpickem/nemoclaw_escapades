# Milestone 2b — Multi-Agent Orchestration: Delegation, NMB & Concurrency

> **Split from:** [Milestone 2 (original)](design_m2.md)
>
> **Predecessor:** [Milestone 2a — Reusable Agent Loop](design_m2a.md)
>
> **Successor:** [Milestone 3 — Review Agent](design.md#milestone-3--review-agent)
>
> **Last updated:** 2026-04-14

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [Architecture](#3--architecture)
4. [Sandbox Lifecycle & Workspace Setup](#4--sandbox-lifecycle--workspace-setup)
5. [Agent Setup: Policy, Tools, and Comms](#5--agent-setup-policy-tools-and-comms)
6. [Orchestrator ↔ Sub-Agent Protocol](#6--orchestrator--sub-agent-protocol)
7. [Work Collection and Finalization](#7--work-collection-and-finalization)
8. [NMB Event Loop and Concurrency Model](#8--nmb-event-loop-and-concurrency-model)
9. [At-Least-Once NMB Delivery](#9--at-least-once-nmb-delivery)
10. [`ToolSearch` Meta-Tool](#10--toolsearch-meta-tool)
11. [Basic Cron](#11--basic-cron)
12. [Audit and Observability](#12--audit-and-observability)
13. [End-to-End Walkthrough](#13--end-to-end-walkthrough)
14. [Implementation Plan](#14--implementation-plan)
15. [Testing Plan](#15--testing-plan)
16. [Risks and Mitigations](#16--risks-and-mitigations)
17. [Open Questions](#17--open-questions)

---

## 1  Overview

Milestone 2b delivers the first **multi-agent capability**: the orchestrator
delegates tasks to a coding sub-agent via the NemoClaw Message Bus (NMB) and
collects completed work through a model-driven finalization flow.

M2b builds on the reusable `AgentLoop`, file tools, compaction, and prompt
builder delivered in [M2a](design_m2a.md). The sub-agent coding process reuses
the same `AgentLoop` class with a different tool registry and configuration —
no code duplication.

> **Scope note:** In M2b, the coding agent runs as a **separate process in the
> same sandbox** as the orchestrator. This exercises the full NMB protocol
> (`task.assign` → `progress` → `task.complete`), the delegation flow, and
> concurrency controls without introducing multi-sandbox complexity (separate
> images, policies, credential isolation). Multi-sandbox delegation is deferred
> to M3, where the same NMB-based protocol works unchanged — only the spawn
> mechanism changes (from `subprocess` to `openshell sandbox create`).

### What was promoted into M2b

| Feature | Original Target | Rationale |
|---------|----------------|-----------|
| Basic cron (operational) | M6 | BYOO tutorial builds cron at step 12 (right after routing). The always-on orchestrator benefits from cron early: sandbox TTL watchdog, stale-session cleanup, health checks. Only operational cron; self-learning cron remains M6. |

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Implement the **sub-agent coding process** that uses M2a's `AgentLoop` +
   file tools to execute coding tasks.
2. Define the full **sandbox setup sequence**: workspace, tools, comms, policy.
3. Implement **orchestrator → sub-agent delegation** via NMB `task.assign` and
   result collection via `task.complete`.
4. Build the **work collection and finalization** flow: collect sub-agent
   results, present to user, commit/push/create PR on approval.
5. Implement **per-agent concurrency caps** via `asyncio.Semaphore` and
   **spawn depth limits** (`max_spawn_depth`, `max_children_per_agent`).
6. Implement **at-least-once NMB delivery** for critical messages
   (`task.complete`, `audit.flush`).
7. Implement **`ToolSearch` meta-tool** for progressive tool loading.
9. Implement **basic operational cron**: sandbox TTL watchdog, stale-session
   cleanup, health checks.
10. Maintain audit, approval, and safety guarantees from M1.

### 2.2 Non-Goals

1. Multi-sandbox delegation (M3 — same protocol, different spawn mechanism).
2. Review agent or multi-agent collaboration loops (M3).
3. Skills auto-creation or the self-learning loop (M6).
4. Self-learning cron jobs (M6 — only operational cron in M2b).
5. Full memory system (M5).
6. Web UI integration (incremental across milestones).
7. Multi-host sandbox deployment (single-host only).

---

## 3  Architecture

*Full specification: [original §3](design_m2.md#3--architecture)*

### 3.1 Process Topology (M2b)

```
┌──────────────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox                                                    │
│                                                                      │
│  ┌──────────────────────────────────┐                                │
│  │  Orchestrator Process             │                                │
│  │                                  │                                │
│  │  SlackConnector ─→ Orchestrator  │──── NMB ────┐                  │
│  │                      │           │              │                  │
│  │            AgentLoop (from M2a)  │              │                  │
│  │            PromptBuilder         │              │                  │
│  │            Compaction            │              │                  │
│  │            AuditDB               │              │                  │
│  └──────────────────────────────────┘              │                  │
│                                                    ▼                  │
│  ┌──────────────────────────────────┐   ┌────────────────────────┐   │
│  │  NMB Broker                       │   │  Coding Agent Process  │   │
│  │  (WebSocket, single-host)         │   │                        │   │
│  └──────────────────────────────────┘   │  AgentLoop (from M2a)  │   │
│                                          │  File Tools            │   │
│                                          │  Scratchpad            │   │
│                                          │  AuditBuffer           │   │
│                                          └────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Map

| Component | Owned By | Description |
|-----------|---------|-------------|
| `AgentLoop` | M2a | Reusable tool-calling loop (shared by orchestrator and sub-agent) |
| `PromptBuilder` | M2a | Layered system prompt with cache boundary |
| `Compaction` | M2a | Two-tier context management |
| `MessageBus` | M2b | NMB client library for inter-process messaging |
| `DelegationManager` | M2b | Spawn sub-agent, track lifecycle, collect results |
| `FinalizationTools` | M2b | Model-driven work review and git operations |
| `ConcurrencyManager` | M2b | Per-agent semaphores, spawn depth tracking |
| `AuditBuffer` | M2b | Sub-agent-side audit accumulator with NMB-batched flush |
| `ToolSearch` | M2b | Progressive tool loading meta-tool |
| `CronWorker` | M2b | Operational cron for sandbox cleanup and health checks |

---

## 4  Sandbox Lifecycle & Workspace Setup

*Full specification: [original §5](design_m2.md#5--agent-process-lifecycle--workspace-setup)*

### 4.1 Spawn Sequence (M2b: same-sandbox process)

1. Orchestrator receives coding task from user.
2. `DelegationManager` checks concurrency limits (semaphore + spawn depth).
3. Sub-agent process spawned via `subprocess` in the same sandbox.
4. Workspace directory created and seeded with task context.
5. Sub-agent connects to NMB broker, sends `sandbox.ready`.
6. Orchestrator sends `task.assign` with task description and workspace path.
7. Sub-agent runs `AgentLoop` with coding file tools.
8. Sub-agent sends `task.complete` with result, diff, scratchpad.
9. Orchestrator runs model-driven finalization (§7).

### 4.2 Workspace Content Seeding

*Full specification: [original §5.2–5.3](design_m2.md#52--workspace-setup-setup-workspacesh)*

The orchestrator prepares the workspace before spawning the sub-agent:
- Clone/checkout the target repository (shallow clone for speed)
- Create scratchpad with initial template
- Seed skills directory (placeholder for M5+)
- Seed memory directory (placeholder for M5+)

### 4.3 Sandbox Cleanup

After `task.complete` or timeout:
1. Read artifacts from the sub-agent's workspace (diff, scratchpad, audit
   JSONL fallback). Same sandbox — direct filesystem access, no download.
   *(Multi-sandbox delegation in M3 will require `openshell sandbox exec` to
   pull files across sandbox boundaries.)*
2. Kill the sub-agent process.
3. Clean up workspace directory.
4. TTL watchdog ensures cleanup even if the orchestrator misses `task.complete`.

---

## 5  Agent Setup: Policy, Tools, and Comms

*Full specification: [original §6](design_m2.md#6--agent-setup-policy-tools-and-comms)*

### 5.1 Policy

In M2b, the sub-agent process shares the orchestrator's sandbox and inherits
its policy. There is no per-agent policy boundary to manage.

Policy hot-reload (`policy.request` NMB messages, auto-approve allowlists,
Slack escalation for unknown endpoints) is deferred to **M3**, where
multi-sandbox delegation introduces distinct per-sandbox policies that can
diverge from the orchestrator's. See [original §6.3](design_m2.md#63--policy-hot-reload)
for the full design.

### 5.2 NMB Setup in Sub-Agents

Sub-agents connect to the NMB broker at startup. The broker URL is provided
via environment variable. Authentication is by sandbox identity.

---

## 6  Orchestrator ↔ Sub-Agent Protocol

*Full specification: [original §9](design_m2.md#9--orchestrator--sub-agent-protocol)*

### 6.1 Message Flow

```
Orchestrator                    NMB Broker                    Coding Agent
    │                              │                              │
    │── task.assign ──────────────▶│──────────────────────────────▶│
    │                              │                              │
    │                              │◀─── task.progress (opt) ─────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │                              │◀─── audit.flush ─────────────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │                              │◀─── task.complete ───────────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │── task.complete.ack ────────▶│──────────────────────────────▶│
```

### 6.2 Sub-Agent Entrypoint

The sub-agent is a standalone Python process:

```python
# agent/__main__.py
async def main():
    cfg = NemoClawConfig.load()  # loads from config.yaml + env overlay
    bus = await MessageBus.connect(cfg.nmb.broker_url)
    agent = CodingAgent(bus=bus, backend=backend, config=cfg)
    await agent.run()
```

The `CodingAgent` (Layer 3) uses the same `AgentLoop` (Layer 1) from M2a with
a coding-specific tool registry and scratchpad.

---

## 7  Work Collection and Finalization

*Full specification: [original §10](design_m2.md#10--work-collection-and-finalization)*

### 7.1 Model-Driven Finalization

After receiving `task.complete`, the orchestrator runs a second `AgentLoop`
invocation with **finalization tools**. The model sees the sub-agent's result
(diff, scratchpad, summary, test output) and **synthesizes** it before deciding
what to do:

- **Quality assessment** — inspect the diff for obvious issues, check whether
  tests passed, note any open questions from the scratchpad.
- **Result summarization** — produce a user-facing summary that distills the
  sub-agent's work into a concise description (the sub-agent's raw output is
  often too verbose or technical for direct presentation).
- **Multi-agent synthesis** (M3+) — when multiple sub-agents contribute to the
  same task (e.g., coding agent + review agent), the finalization model merges
  their outputs, resolves conflicts, and presents a unified result.
- **Proactive iteration** — if the model notices failing tests or incomplete
  work in the scratchpad, it can call `re_delegate` with a fix prompt without
  waiting for user feedback.

After synthesis, the model calls one of the finalization tools:

| Tool | Description |
|------|-------------|
| `present_work_to_user` | Show synthesized summary + diff to user via Slack with action buttons |
| `push_and_create_pr` | Read git state from sub-agent workspace, push branch, create PR |
| `push_branch` | Push branch without creating a PR |
| `discard_work` | Discard the sub-agent's work and clean up |
| `re_delegate` | Send updated instructions back to the same sub-agent (with synthesis feedback) |
| `destroy_sandbox` | Explicitly tear down the sub-agent process |

### 7.2 User-Facing Slack Rendering

The finalization flow renders results to Slack with interactive buttons:
- **[Push & PR]** → calls `push_and_create_pr`
- **[Iterate]** → prompts for feedback, calls `re_delegate`
- **[Discard]** → calls `discard_work`

---

## 8  NMB Event Loop and Concurrency Model

*Full specification: [original §10.7](design_m2.md#107--nmb-event-loop-and-concurrency-model)*

### 8.1 Per-Agent Semaphore Concurrency

The `DelegationManager` maintains per-agent semaphores:

```python
class DelegationManager:
    def __init__(self):
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def delegate(self, agent_id: str, task: dict) -> None:
        sem = self._get_or_create_semaphore(agent_id)
        async with sem:
            await self._spawn_and_wait(agent_id, task)
        self._maybe_cleanup_semaphore(agent_id)
```

`max_concurrent_tasks` and `max_spawn_depth` are configured per agent role.
Validated by the BYOO tutorial (deep dive §9.1).

### 8.2 Non-Blocking Event Loop

The NMB event loop runs as a background `asyncio.Task`. It processes
`task.complete`, `audit.flush`, and `task.progress` messages
without blocking the Slack connector's `handle()` method. Finalization runs as
independent `asyncio.Task`s, allowing multiple finalizations to run
concurrently.

---

## 9  At-Least-Once NMB Delivery

*Validated by BYOO tutorial (deep dive §4.3).*

Critical messages (`task.complete`, `audit.flush`) are persisted to disk before
sending, using atomic writes:

```python
async def reliable_send(self, message: NMBMessage) -> None:
    tmp_path = self.pending_dir / f".tmp_{message.id}"
    final_path = self.pending_dir / f"{message.id}.json"
    with open(tmp_path, "w") as f:
        f.write(message.to_json())
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp_path), str(final_path))
    await self._bus.send(message)

def ack(self, message_id: str) -> None:
    path = self.pending_dir / f"{message_id}.json"
    if path.exists():
        path.unlink()
```

On broker startup, pending messages are replayed.

---

## 10  `ToolSearch` Meta-Tool

As the tool surface grows (enterprise + coding + MCP), loading all tool
definitions into the system prompt bloats context and degrades agent
performance. Anthropic's
[multi-agent systems guide](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)
identifies three signals that an agent's tool surface has grown too large:

1. **Quantity** — an agent with too many tools (often 20+) struggles to select
   the appropriate one.
2. **Domain confusion** — tools spanning unrelated domains (database, API, file
   system) cause the agent to confuse which domain applies.
3. **Degraded performance** — adding new tools degrades performance on existing
   tasks, indicating the agent has reached its tool management capacity.

NemoClaw's orchestrator already crosses the 20-tool threshold when enterprise
tools (Jira, GitLab, Gerrit, Confluence, Slack) are loaded alongside coding
tools. `ToolSearch` addresses this by keeping non-core tools out of the prompt
until explicitly needed:

- `ToolSpec.is_core` flag — core tools always in prompt; non-core discoverable.
- `ToolSearch` takes a natural-language query, searches all registered tool
  definitions by keyword, returns matching tool specs.
- Target: 40%+ prompt token reduction when enterprise tools are present.

This is also one of the arguments for multi-agent delegation itself — rather
than giving one agent 30+ tools, delegate to specialized sub-agents that each
have a focused tool surface (coding agent: ~10 file tools, review agent: ~5
read-only tools).

---

## 11  Basic Cron

> **Promoted from M6.** The BYOO tutorial builds cron at step 12 (right after
> routing). The always-on orchestrator benefits from operational cron early.
> Only operational tasks; self-learning cron remains M6.

### 12.1 Operational Cron Jobs

| Job | Schedule | Description |
|-----|----------|-------------|
| Sandbox TTL watchdog | Every 5 min | Kill sub-agent processes that exceed their TTL |
| Stale session cleanup | Every 30 min | Archive sessions with no activity for 24h |
| Health check | Every 10 min | Verify NMB broker, inference backend, Slack connectivity |
| Audit DB maintenance | Daily | Vacuum and checkpoint SQLite, rotate old entries |

### 12.2 Implementation

Lightweight cron using `asyncio` scheduling with Markdown-based persistence:

```python
class CronWorker:
    def __init__(self, state_path: str, jobs: list[CronJob]):
        self._state_path = state_path
        self._jobs = jobs
        self._last_run: dict[str, float] = self._load_state()

    async def run(self):
        while True:
            await asyncio.sleep(60)
            for job in self._jobs:
                if job.is_due(self._last_run.get(job.name, 0)):
                    asyncio.create_task(self._run_and_record(job))

    async def _run_and_record(self, job: CronJob):
        await job.execute()
        self._last_run[job.name] = time.time()
        self._save_state()
```

#### Cron State File

Job state is persisted to a Markdown file (`data/cron_state.md`) so the
orchestrator knows when each job last ran across restarts. The file is
human-readable and trivially editable:

```markdown
# Cron State

| Job | Last Run (UTC) | Status |
|-----|---------------|--------|
| sandbox_ttl_watchdog | 2026-04-14T09:35:00Z | ok |
| stale_session_cleanup | 2026-04-14T09:00:00Z | ok |
| health_check | 2026-04-14T09:40:00Z | ok |
| audit_db_maintenance | 2026-04-14T03:00:00Z | ok |
```

On startup, the `CronWorker` reads this file and skips jobs that ran recently
enough (e.g., the daily audit maintenance doesn't re-run if the orchestrator
restarts mid-day). If the file is missing or corrupt, all jobs are treated as
overdue and run on the next cycle.

All M2b operational jobs are idempotent, so running one twice after a crash is
harmless. The state file prevents unnecessary duplicate runs, not correctness
failures.

`CRON.md` definitions (BYOO pattern) are deferred to M6 when the self-learning
loop needs user-configurable cron jobs with side effects. M6 will likely move
state tracking into the audit DB for queryability and atomic updates. M2b uses
hardcoded operational jobs with this lightweight Markdown persistence.

---

## 12  Audit and Observability

*Full specification: [original §12](design_m2.md#12--audit-and-observability)*

### 13.1 Sub-Agent Audit: NMB-Batched Flush

Sub-agents use `AuditBuffer` (not direct `AuditDB`). Tool calls accumulate in
memory and flush to the orchestrator via NMB `audit.flush` messages at round
boundaries. The orchestrator ingests them into the central `AuditDB`.

### 13.2 Fallback: JSONL Ingest on Task Completion

If NMB flush fails (broker down, crash), the sub-agent writes audit records to
a JSONL file in its workspace. On `task.complete`, the orchestrator reads and
ingests the fallback file directly (same sandbox, shared filesystem).

### 13.3 Progress Reporting

`task.progress` messages relay sub-agent status to the orchestrator for Slack
rendering (thinking indicator, step count, current tool).

---

## 13  End-to-End Walkthrough

*Full specification: [original §13](design_m2.md#13--end-to-end-walkthrough)*

1. **User** sends Slack message: "Add rate limiting to the /api/users endpoint"
2. **Orchestrator** determines this is a coding task requiring delegation.
3. **DelegationManager** checks concurrency limits → available.
4. Sub-agent process spawned; workspace seeded with repo clone.
5. `task.assign` sent via NMB with task description.
6. **Coding Agent** runs `AgentLoop` → reads files, edits code, runs tests.
7. `task.progress` messages stream to orchestrator → Slack thinking indicator.
8. `task.complete` sent with diff, scratchpad, summary.
9. **Orchestrator** runs finalization `AgentLoop` → model calls
   `present_work_to_user`.
10. **User** sees diff in Slack, clicks **[Push & PR]**.
11. Model calls `push_and_create_pr` → branch pushed, PR created.
12. Sub-agent process cleaned up.

---

## 14  Implementation Plan

### Phase 1 — Coding agent process + sub-agent entrypoint

| Task | Files |
|------|-------|
| Create sub-agent `__main__` entrypoint | `agent/__main__.py` |
| Create `AgentSetupBundle` dataclass | `agent/types.py` |
| Create coding agent system prompt template | `prompts/coding_agent.md` |
| End-to-end test: agent process starts, handles task, returns result | `tests/integration/test_coding_agent.py` |

**Exit criteria:** Coding agent process starts, connects to NMB, receives
`task.assign`, runs the M2a `AgentLoop` with file tools, sends `task.complete`.

### Phase 2 — Orchestrator delegation, NMB event loop, concurrency caps, and finalization

| Task | Files |
|------|-------|
| Create `delegate_task` tool for the orchestrator | `tools/delegation.py` |
| Implement spawn → workspace setup → `task.assign` flow | `orchestrator/delegation.py` |
| Implement per-agent `asyncio.Semaphore` concurrency control | `orchestrator/delegation.py` |
| Implement `max_spawn_depth` and `max_children_per_agent` limits | `orchestrator/delegation.py` |
| Implement NMB event loop (`start_nmb_listener`, event dispatch) | `orchestrator/orchestrator.py` |
| Implement finalization tools (`present_work_to_user`, `push_and_create_pr`, `discard_work`, `re_delegate`, `destroy_sandbox`) | `tools/finalization.py` |
| Implement `_finalize_workflow` (build context, run `AgentLoop` with finalization tools) | `orchestrator/orchestrator.py` |
| Implement `AuditBuffer` with NMB-batched flush + JSONL fallback | `agent/audit_buffer.py` |
| Integration test: orchestrator → coding agent → result → finalize | `tests/integration/test_delegation.py` |

**Exit criteria:** Orchestrator delegates coding tasks, collects results, runs
finalization. Concurrency caps enforced. Audit flush works via NMB and fallback.

### Phase 3 — At-least-once NMB delivery

| Task | Files |
|------|-------|
| Implement reliable send (persist → send → ack → delete) | `nmb/reliable_send.py` |
| Implement NMB crash recovery (replay pending on startup) | `nmb/broker.py` |
| Tests for reliable send and crash recovery | `tests/test_reliable_send.py` |

**Exit criteria:** Critical messages (`task.complete`, `audit.flush`) survive
broker crashes and are replayed on restart.

### Phase 4 — `ToolSearch` meta-tool + basic cron

| Task | Files |
|------|-------|
| Implement `ToolSearch` meta-tool (keyword search over tool definitions) | `tools/tool_search.py` |
| Add `ToolSpec.is_core` flag; partition tools into core (in prompt) and searchable | `agent/types.py`, `agent/loop.py` |
| Implement `CronWorker` with hardcoded operational jobs | `orchestrator/cron.py` |
| Implement TTL watchdog, stale-session cleanup, health check jobs | `orchestrator/cron.py` |
| Tests for `ToolSearch`, cron execution | `tests/test_tool_search.py`, `tests/test_cron.py` |

**Exit criteria:** Non-core tools discoverable via `ToolSearch`. Prompt tokens
decrease 40%+ with enterprise tools. Operational cron jobs run on schedule.

### Phase 5 — Polish, hardening, and gaps document

| Task | Files |
|------|-------|
| Progress relaying to Slack | `orchestrator/delegation.py` |
| File tool edge case hardening (symlinks, binary files, encoding) | `tools/files.py` |
| Create `docs/DEFERRED.md` — features punted from M2b | `docs/DEFERRED.md` |

**Exit criteria:** Production-quality delegation with cleanup guarantees,
progress reporting, and robust handling.

---

## 15  Testing Plan

### 16.1 Unit Tests

| Test | What it verifies |
|------|-----------------|
| Delegation concurrency cap | Semaphore blocks at `max_concurrent_tasks`; unblocks on completion |
| Delegation spawn depth cap | `max_spawn_depth` exceeded → delegation rejected with error |
| NMB reliable send | Message persisted to disk before send; deleted after ack |
| NMB crash recovery | Pending messages replayed on broker startup |
| `ToolSearch` meta-tool | Returns correct tools for keyword queries; non-core excluded from prompt |
| Finalization tools | Each tool produces correct output with mock sandbox/git |
| Cron scheduling | Jobs fire at correct intervals; missed jobs caught up |

### 16.2 Integration Tests

| Test | What it verifies |
|------|-----------------|
| Sub-agent NMB lifecycle | Connect, `sandbox.ready`, `task.assign`, `task.complete` |
| Coding agent end-to-end | Agent receives task, uses file tools, returns diff |
| Orchestrator delegation full flow | Spawn → assign → complete → finalize → cleanup |
| Delegation concurrency enforcement | Third delegation waits when `max_concurrent_tasks=2` |
| Model-driven finalization | `task.complete` → model calls `present_work_to_user` → user clicks [Push & PR] |
| Iteration flow | User feedback → `re_delegate` → same agent → updated result |
| Concurrent finalization | Two sub-agents complete simultaneously; both finalize concurrently |
| NMB at-least-once delivery | Kill broker after persist, restart, verify replay |
| Audit NMB flush + fallback | Tool calls arrive via NMB batch and/or JSONL fallback |
| TTL watchdog | Watchdog fires → sub-agent process killed → workspace cleaned |

### 16.3 Safety Tests

| Test | What it verifies |
|------|-----------------|
| Tool surface enforcement | Sub-agent cannot use tools not in its `tool_surface` |
| Workspace path sandboxing | File tools cannot access outside `/sandbox/workspace/` |
| No recursive delegation | Coding agent cannot spawn sub-agents |
| Scratchpad size cap | Large writes truncated |

---

## 16  Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Sandbox spawn latency (5-15s) | Adds to end-to-end time | Accept for M2b; warm pool deferred |
| NMB broker unavailability | Cannot communicate with sub-agent | Fail-open: detect broker down, surface error to user, retry on recovery |
| Sub-agent infinite loop | Consumes tokens without progress | `max_tool_rounds` + TTL watchdog |
| Large workspace clones | Slow setup, high storage | Shallow clones; NMB context for small tasks |
| Credential leakage via scratchpad | Agent writes secrets | Scratchpad sanitization before return to orchestrator. *(M2b: same sandbox, so process-level isolation only. M3 adds kernel-level sandbox isolation.)* |

---

## 17  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should sub-agent system prompts be generated by the orchestrator or stored as static templates? | Start with templates, evolve to generation. |
| 2 | How should the orchestrator decide when to delegate vs. handle itself? | Start with explicit user intent; evolve to model-driven routing. |
| 3 | Git finalization: orchestrator pull+push (Strategy A) or sub-agent push directly (Strategy B)? | Strategy A maximizes isolation; Strategy B is simpler. See [original §10.4](design_m2.md#104--git-operations-for-finalization). |

---

### Sources

- [Original M2 Design Document](design_m2.md)
- [M2a Design Document](design_m2a.md) — reusable `AgentLoop`, file tools, compaction, skills
- [NMB Design](nmb_design.md) — inter-sandbox messaging protocol
- [Sandbox Spawn Design](sandbox_spawn_design.md) — OpenShell sandbox lifecycle
- [Audit DB Design](audit_db_design.md) — tool-call auditing, NMB-batched flush
- [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md) — §4 (event bus), §7 (routing), §8 (dispatch), §9 (concurrency), §13 (cron)
- [GAPS.md](../GAPS.md) — feature tracking
