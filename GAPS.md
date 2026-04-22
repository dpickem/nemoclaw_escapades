# NemoClaw Escapades — Feature Gaps & Adoption Tracker

> **Purpose:** Single source of truth for features NemoClaw should adopt from
> reference systems. Each item is sourced from a deep dive or design document,
> assigned a target milestone, and tracked through implementation.
>
> **Last updated:** 2026-04-22

## Status Legend

| Status | Meaning |
|--------|---------|
| **Done** | Implemented and merged |
| **Designed** | Design exists in a doc; implementation not started |
| **Planned** | Identified as needed; no detailed design yet |
| **Deferred** | Explicitly pushed to a later milestone |
| **Accepted** | Known limitation; no plan to address |
| **Rejected** | Evaluated and intentionally not adopting |

## Sources

- [Build Your Own OpenClaw Deep Dive](docs/deep_dives/build_your_own_openclaw_deep_dive.md) — §3–§16 inline lessons, §17 architecture comparison, §18 "What to Lift"
- [Design Document §10](docs/design.md#10--future-work--features-inspired-by-claude-code) — Claude Code-inspired features
- [M2 Design §18.6](docs/design_m2.md#186-summary-strengths-and-gaps) — M2 gap analysis vs Hermes, OpenClaw, Claude Code
- [Claude Code Deep Dive](docs/deep_dives/claude_code_deep_dive.md)
- [Hermes Deep Dive](docs/deep_dives/hermes_deep_dive.md)
- [OpenClaw Deep Dive](docs/deep_dives/openclaw_deep_dive.md)

---

## 1  Agent Loop & Tool Execution

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| `@tool` decorator with auto-schema | BYOO `tools/base.py` | M2a | **Done** | Decorator generates JSON Schema from type hints + docstrings; all `ToolSpec` metadata supported as kwargs. Implemented in `tools/registry.py`. All coding tools (files, search, bash, git) migrated. |
| Consistent `@tool` usage across all tools | BYOO `tools/base.py` | M2b+ | **Planned** | Enterprise tools (Jira, GitLab, Gerrit, Confluence, Slack) still use the verbose `ToolSpec(...)` constructor with hand-written `input_schema` dicts. Migrate to `@tool` decorator for single source of truth — description, parameters, and schema all derived from the function's docstring and type annotations. |
| Concurrent tool execution | BYOO §3.2, Claude Code, Hermes | M2a P1 | **Designed** | Default to concurrent via `asyncio.gather`; `is_concurrency_safe=False` flag for write tools that mutate shared state. |
| Streaming tool execution | Claude Code `query()` async generator | M6+ | **Deferred** | Tools execute *during* the model's streaming response, not after. Single biggest latency improvement available. Requires refactoring `AgentLoop` to async generator pattern. |
| Tool result truncation | BYOO §5.1 `ContextGuard._truncate_large_tool_results` | M2a P3 | **Designed** | Cap tool result content at configurable char limit (tutorial uses 10K). Micro-compaction tier — no API call needed. Promoted from M3 to M2a. |
| Per-tool error isolation | BYOO §3.2 | M2a | **Designed** | Exceptions caught per-tool and returned as error strings in the tool message. The loop never crashes from a tool failure. |
| Git identity pre-configured in sandbox image | Sandbox bring-up (observed) | M2b P1 | **Planned** | `docker/Dockerfile.orchestrator` installs `git` for the coding agent's `git_commit` / `git_clone` / `git_checkout` tools, but never sets `user.email` / `user.name`. Any agent-driven commit fails with `*** Please tell me who you are`; operators have been running `git config user.email nemoclaw@agent.bot && git config user.name "NemoClaw Agent"` by hand on every fresh sandbox. Bake a default identity into the image via `RUN git config --system` so commits work out of the box; seeded `.gitconfig` per workspace can still override for tasks that need a specific author. |
| Policy hostnames in public source | Review of M2b P1 | M2b P1 | **Done** | `scripts/gen_policy.py` now extracts the hostname from `.env`'s `GITLAB_URL` / `GERRIT_URL` and substitutes it into `policies/orchestrator.yaml`'s `host: ""` placeholders at build time.  Same public-base + private-overlay pattern as `gen_config.py`; same env vars already consumed by the REST-tool configs so operators don't maintain the hostname twice.  Covered by `tests/test_gen_policy.py`. |

## 2  Context Management

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Two-tier context compaction | Claude Code `compact/`, BYOO §5 | M2a P3 | **Designed** | Micro-compaction (tool result truncation, no API call) and full compaction (LLM summary + session roll). Promoted from M3 to M2a. Session memory (key-fact cache) deferred to M5. |
| Session-rolling compaction | BYOO §5.2 `ContextGuard.compact_and_roll` | M2a P3 | **Designed** | Create new session, copy LLM-generated summary + tail messages, update routing cache. Cleaner than mutating the existing session in place. |
| Cache-aware system prompt | Claude Code `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` | M2a P3 | **Designed** | Split system prompt into static prefix (cached) + dynamic suffix (per-turn). Reduces cost by ~90% on subsequent turns via provider prompt caching. |
| Prompt cache break detection | Claude Code `promptCacheBreakDetection.ts` | M2a P3 | **Designed** | Monitor whether the system prompt's static prefix changed between turns; log warnings when cache effectiveness drops. |

## 3  Prompt Construction

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Layered PromptBuilder | BYOO §10.1, tutorial step 13 | M2a P3 | **Designed** | Five-layer system prompt: identity (AGENT.md) → soul (personality) → bootstrap (agents + crons) → runtime (agent ID, timestamp) → channel hint. |
| Channel hint in system prompt | BYOO §10.2 `PromptBuilder._build_channel_hint` | M2a P3 | **Designed** | Sub-agents receive a hint indicating whether they respond to a user, a parent agent, or a cron trigger. Essential for correct behavioral framing. |

## 4  Skills & Progressive Loading

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Basic `SKILL.md` loading | BYOO §6 `create_skill_tool`, tutorial step 02 | M2a P3 | **Designed** | Single `skill` tool with enum of available skill IDs. Loads skill content and injects into the conversation. Promoted from M6 to M2a. |
| `ToolSearch` meta-tool | Claude Code `ToolSearchTool`, Hermes progressive disclosure | M2b P4 | **Designed** | Core tools always in prompt; non-core tools discoverable on demand via natural-language search. Reduces prompt token count by 40%+ when enterprise tools are present. |
| `batch` skill pattern | Claude Code bundled skill | M2b+ | **Planned** | Research → decompose → distribute across worktree agents → verify → track. Essential for large multi-file tasks. |
| `verify` skill pattern | Claude Code bundled skill | M2b+ | **Planned** | "Prove it works" workflow: run the app, check CLI output, validate behavior rather than static reasoning. |
| Progressive skill disclosure | Hermes Level 0/1/2 | M6 | **Planned** | As skill count grows, layer discovery: Level 0 (always loaded), Level 1 (loaded on mention), Level 2 (loaded on explicit request). |
| Cron-ops as skill | BYOO §13.3 | M6 | **Planned** | Expose cron management operations as a SKILL the agent reads and follows, rather than adding dedicated cron management tools. Reduces tool count. |

## 5  Event System & Messaging

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| At-least-once outbound delivery | BYOO §4.3 `EventBus._persist_outbound` | M2b P3 | **Designed** | Persist `task.complete` and `audit.flush` to disk (atomic write: tmp → fsync → rename) before sending. Delete after orchestrator ack. Replay pending messages on crash recovery. |
| NMB crash recovery | BYOO §4.3 `EventBus._recover` | M2b P3 | **Designed** | On broker startup, scan pending directory and replay unacknowledged outbound events. |
| ~~In-process dispatch for dev mode~~ | BYOO §8.1 `subagent_dispatch` | — | **Cut** | `LocalDispatcher` with `Future`-based rendezvous when OpenShell unavailable. Cut from M2b — throwaway code that won't be needed once the NMB broker is running. |
| Retry on failure | BYOO §9.2 `AgentWorker` retry logic | M3+ | **Planned** | Failed sessions retry up to N times by republishing a modified event with incremented `retry_count`. Add to NMB dispatch for `task.assign`. |

## 6  Multi-Agent Orchestration

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Per-agent semaphore concurrency | BYOO §9.1 `AgentWorker._semaphores` | M2b P2 | **Designed** | `dict[agent_id, asyncio.Semaphore]` with `max_concurrent_tasks` from agent config. Enforce before `openshell sandbox create`. Auto-cleanup when no waiters remain. |
| Spawn depth and children caps | BYOO §9, OpenClaw `maxSpawnDepth` | M2b P2 | **Designed** | `max_spawn_depth` and `max_children_per_agent` config limits to prevent unbounded delegation recursion. |
| Regex routing with tier specificity | BYOO §7.1 `RoutingTable` with `Binding.tier` | M3+ | **Planned** | Tier 0 = literal match, Tier 1 = regex without `.*`, Tier 2 = wildcard. Most specific match wins. Apply to Slack channel/thread → sub-agent routing. |
| Session affinity via routing cache | BYOO §7.2 `get_or_create_session_id` | M3+ | **Planned** | First message from a source resolves an agent and creates a session; subsequent messages from the same source hit the cache directly. Persist to runtime config. |
| Session forking | Claude Code `FORK_SUBAGENT` | M3+ | **Deferred** | Fork current session context into a sub-agent via NMB `task.fork`. Review agent needs coding context to provide useful feedback; forking avoids re-packaging all context in `task.assign`. |
| Per-user concurrency limits | BYOO §9.3 (identified gap) | M3+ | **Planned** | Tutorial lacks per-user limits. NemoClaw should add per-user task caps to prevent one user from monopolizing agent resources. |
| Priority queuing | BYOO §9.3 (identified gap) | M3+ | **Planned** | Tutorial lacks priority queuing. NemoClaw should support task priority levels for urgent vs background work. |
| Cross-agent budget tracking | BYOO §9.3 (identified gap), Hermes shared budget | M3+ | **Planned** | Shared token/cost budget across all sub-agents to prevent runaway spending. |

## 7  Cron & Proactive Behavior

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Basic operational cron | BYOO §13.2 | M2b P4 | **Designed** | Hardcoded operational jobs (TTL watchdog, session cleanup, health check). Promoted from M6 to M2b. |
| `CRON.md` definitions | BYOO §13.1 `CronLoader` | M6 | **Planned** | YAML-frontmatter cron files specifying name, description, schedule, agent, and task body. Minimum 5-minute granularity. Supports `one_off` jobs. Configurable cron deferred to M6. |
| CronWorker with `croniter` | BYOO §13.2 | M6 | **Planned** | Full cron worker using `croniter.match`; publishes `DispatchEvent` with cron body. M2b uses a simpler `asyncio`-based scheduler for operational jobs only. |
| Proactive tick system | Claude Code `KAIROS` / `PROACTIVE` flags | M1+ | **Planned** | Periodic `<proactive_tick>` events for always-on daemon behavior: check for pending Slack messages, cron jobs, stalled tasks, sandbox cleanup. |

## 8  Configuration

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Two-file config (user + runtime) | BYOO §11.1 `Config._load_merged_configs` | — | **Planned** | `config.user.yaml` (durable, user-edited, watched) + `config.runtime.yaml` (ephemeral, programmatic). Deep merge: runtime overrides user. Useful for local development mode. |
| Config hot-reload via watchdog | BYOO §11.2 | — | **Planned** | On `config.user.yaml` change, `Config.reload()` replaces all fields on the same instance (in-place setattr). All components holding a reference see updates immediately. For production, use orchestrator config system with NMB-based propagation. |

## 9  Persistence & Memory

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Three-layer memory | Hermes, design.md §M5 | M5 | **Planned** | Working memory (in-session scratchpad), user memory (Honcho for modeling and personalization), knowledge memory (SecondBrain for durable knowledge capture and retrieval). |
| File-based working memory via `scratchpad` skill | BYOO §15 step 17; Claude Code `CLAW.md` convention | M2a | **Done** | Agents manage their own working-memory file using ordinary `read_file`/`write_file`/`edit_file` tools. The `scratchpad` skill (`skills/scratchpad/SKILL.md`) documents the naming convention and section structure. No dedicated class, no dedicated tools, no prompt-layer injection — the agent loads the skill when it decides notes are warranted. |
| Passive memory extraction | Claude Code `extractMemories` | M5 | **Planned** | Automatically extract key facts (user preferences, project conventions, recurring patterns) from every conversation without explicit user action. |
| Team memory sync | Claude Code `teamMemorySync/` | M5+ | **Planned** | Shared memory across agent teams. Relevant when multiple sub-agents collaborate on related tasks. |

## 10  Channel & Connector

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Channel ABC | BYOO §12.1 `Channel(ABC, Generic[T])` | M1 | **Designed** | Abstract base with `run`, `reply`, `is_allowed`, `stop`. NemoClaw's `ConnectorBase` ABC maps to this pattern. |
| First-message session binding | BYOO §12.3 | M2b+ | **Planned** | First non-CLI platform message sets `default_delivery_source` in runtime config. Ensures all outbound events (including cron responses) are delivered to the correct platform. Relevant for Slack thread → sub-agent binding. |
| EventSource namespace registry | BYOO §4.4 `EventSource._namespace` + `from_string` | M2b+ | **Planned** | Each event source has a `_namespace` (e.g. `telegram`, `slack`) and a serialization format `namespace:identifier`. Reference for NemoClaw's connector type system. |

## 11  Observability & Safety

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| YOLO-style permission classifier | Claude Code two-stage classifier | M5+ | **Deferred** | Fast-path pattern matcher for known-safe operations + async Slack escalation for dangerous ones. M1 ships a stub auto-approve-all interface. |
| Feature flag system | Claude Code GrowthBook + `bun:bundle` | M2b+ | **Planned** | Progressive rollout of experimental features behind runtime/build-time flags. Options: LaunchDarkly, Unleash, or simple config-file-based system for v1. |
| Frustration detection | Claude Code `useFrustrationDetection.ts` | — | **Planned** | Detect user frustration via regex patterns; trigger feedback surveys or adjust agent behavior. Lower priority. |
| Per-milestone gaps document | BYOO §16 `GAP.md` | M2b P5 | **Designed** | `docs/DEFERRED.md` tracking features explicitly punted from each milestone. Prevents scope creep and makes "not in scope" decisions visible to contributors. |

## 12  Web UI & Integration

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| Mission Control Dashboard | design.md §9 (Cline Kanban + OpenClaw Studio) | M2b+ | **Planned** | Kanban board, live agent dashboard, diff viewer, approval gates, scheduler view, chat interface. React + FastAPI + WebSocket. |
| IDE bridge system | Claude Code `bridge/` + `BRIDGE_MODE` | M2b+ | **Planned** | Bidirectional VS Code / JetBrains integration via ACP (Agent Communication Protocol). Expose orchestrator as editor-native agent over stdio/JSON-RPC. |
| Browser automation tool | Claude Code `WEB_BROWSER_TOOL` + `claude-in-chrome` | M5+ | **Planned** | Programmatic browser automation beyond MCP-based Chrome integration. |
| Copy-on-write speculation | Claude Code | M5+ | **Planned** | Pre-compute next response on overlay filesystem for fast session switching. |
| Voice input | Claude Code `VOICE_MODE` | — | **Planned** | Voice-to-text input for the orchestrator. Interesting for mobile/hands-free use. Lower priority. |
| Desktop/mobile handoff | Claude Code `/desktop`, `/mobile` commands | — | **Planned** | Seamless session transfer between devices. NemoClaw's Slack-first approach already handles this partially. |

## 13  MCP & Ecosystem

| Feature | Source | Target | Status | Description |
|---------|--------|--------|--------|-------------|
| MCP bridge | Hermes, OpenClaw, Claude Code (all support MCP) | M5+ | **Deferred** | Dynamic tool registration via Model Context Protocol. Follow [Tools Integration Design §4.3](docs/tools_integration_design.md). |

---

## 14  Explicitly Not Adopting

Items evaluated from reference systems and intentionally rejected.

| Feature | Source | Reason for Rejection |
|---------|--------|---------------------|
| Single-process architecture | BYOO tutorial | NemoClaw requires multi-sandbox isolation via OpenShell for security. |
| JSONL persistence | BYOO tutorial | SQLite is more appropriate for concurrent access, querying, and the audit/training flywheel. |
| No audit trail | BYOO tutorial | NemoClaw's audit DB is a core requirement for the training flywheel. |
| WebSocket-only server | BYOO tutorial | NemoClaw needs REST APIs for the Mission Control Dashboard. |
| No template substitution | BYOO tutorial | NemoClaw skills will use template variables (`{{workspace}}`, `{{agent_id}}`) for path injection. |
| Single-provider lock-in | Claude Code | NemoClaw's multi-provider design (Inference Hub, Anthropic, OpenAI, custom) is intentionally more flexible. |
| Terminal-only interface | Claude Code | NemoClaw's Slack + Web UI + future IDE integration is more accessible for an always-on agent. |
| JSON file sessions | Claude Code | NemoClaw uses SQLite from the start (matching Hermes) for searchability and concurrent access. |
| Bun runtime | Claude Code | NemoClaw is Python-based; the streaming architecture translates via `async for` generators. |
| Sub-agent communication latency (~20-50ms) | M2b gap analysis | Accepted trade-off for kernel-level sandbox isolation. In-process dispatch path (M2b P3) available for development. |

---

## 15  Milestone Readiness Considerations

The M2a/M2b split (April 2026) promoted three features forward based on the
[BYOO tutorial per-step mapping](docs/deep_dives/build_your_own_openclaw_deep_dive.md#24-per-step-mapping-tutorial-steps--nemoclaw-milestones--phases):

| Feature | Original Target | Promoted To | Tutorial Step | Rationale |
|---------|----------------|-------------|---------------|-----------|
| Basic `SKILL.md` loading | M6 | **M2a P3** | Step 02 (Phase 1) | Loading mechanism is independent of auto-creation/self-learning. Enables coding agent task templates. |
| Context compaction | M3 | **M2a P3** | Step 04 (Phase 1) | Any coding session that hits the context window fails with no recovery. Essential for single-agent capability. |
| Basic operational cron | M6 | **M2b P4** | Step 12 (Phase 3) | Always-on orchestrator needs sandbox TTL watchdog, session cleanup, health checks before self-learning. |

Remaining consideration not yet promoted:

| Feature | Current Target | Tutorial Step | Consideration |
|---------|---------------|---------------|---------------|
| Proactive messaging | M6 | Step 14 (Phase 3) | Tutorial builds this in the multi-agent phase. The proactive tick enables periodic health checks and sandbox cleanup even before self-learning. Consider M2b or M3. |
