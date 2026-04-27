# Milestone 2b — Multi-Agent Orchestration: Delegation, NMB & Concurrency

> **Split from:** [Milestone 2 (original)](design_m2.md)
>
> **Predecessor:** [Milestone 2a — Reusable Agent Loop](design_m2a.md)
>
> **Successor:** [Milestone 3 — Multi-Sandbox Delegation, Review Agent & Skill Auto-Policy](design_m3.md)
>
> **Last updated:** 2026-04-25

---

## Table of Contents

1. [Overview](#1--overview)
   - [What was promoted into M2b](#what-was-promoted-into-m2b)
   - [Patterns adopted from the OpenAI Agents SDK](#patterns-adopted-from-the-openai-agents-sdk)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
   - [2.1 Goals](#21-goals)
   - [2.2 Non-Goals](#22-non-goals)
3. [Architecture](#3--architecture)
   - [3.1 Process Topology (M2b)](#31-process-topology-m2b)
   - [3.2 Component Map](#32-component-map)
4. [Sandbox Lifecycle & Workspace Setup](#4--sandbox-lifecycle--workspace-setup)
   - [4.1 Spawn Sequence (M2b: same-sandbox process)](#41-spawn-sequence-m2b-same-sandbox-process)
   - [4.2 Workspace Content Seeding](#42-workspace-content-seeding)
   - [4.3 Sandbox Cleanup](#43-sandbox-cleanup)
   - [4.4 Authenticated `git_clone` for Private Hosts](#44-authenticated-git_clone-for-private-hosts)
5. [Agent Setup: Policy, Tools, and Comms](#5--agent-setup-policy-tools-and-comms)
   - [5.1 Policy](#51-policy)
   - [5.2 NMB Setup in Sub-Agents](#52-nmb-setup-in-sub-agents)
   - [5.3 Sandbox Configuration Layer (`/app/config.yaml`)](#53-sandbox-configuration-layer-appconfigyaml)
   - [5.4 Sandbox Detection and Startup Self-Check](#54-sandbox-detection-and-startup-self-check)
6. [Orchestrator ↔ Sub-Agent Protocol](#6--orchestrator--sub-agent-protocol)
   - [6.1 Message Flow](#61-message-flow)
   - [6.2 Sub-Agent Entrypoint](#62-sub-agent-entrypoint)
   - [6.3 Typed Protocol Payloads](#63-typed-protocol-payloads)
   - [6.4 `max_turns` Semantics](#64-max_turns-semantics)
   - [6.5 Per-Task `model` Semantics](#65-per-task-model-semantics)
   - [6.6 Workspace Baseline Semantics](#66-workspace-baseline-semantics)
7. [Work Collection and Finalization](#7--work-collection-and-finalization)
   - [7.1 Model-Driven Finalization](#71-model-driven-finalization)
   - [7.2 User-Facing Slack Rendering](#72-user-facing-slack-rendering)
8. [NMB Event Loop and Concurrency Model](#8--nmb-event-loop-and-concurrency-model)
   - [8.1 Per-Agent Semaphore Concurrency](#81-per-agent-semaphore-concurrency)
   - [8.2 Non-Blocking Event Loop](#82-non-blocking-event-loop)
9. [At-Least-Once NMB Delivery](#9--at-least-once-nmb-delivery)
10. [`ToolSearch` Meta-Tool](#10--toolsearch-meta-tool)
11. [Basic Cron](#11--basic-cron)
    - [12.1 Operational Cron Jobs](#121-operational-cron-jobs)
    - [12.2 Implementation](#122-implementation)
12. [Audit and Observability](#12--audit-and-observability)
    - [13.1 Sub-Agent Audit: NMB-Batched Flush](#131-sub-agent-audit-nmb-batched-flush)
    - [13.2 Fallback: JSONL Ingest on Task Completion](#132-fallback-jsonl-ingest-on-task-completion)
    - [13.3 Progress Reporting](#133-progress-reporting)
13. [End-to-End Walkthrough](#13--end-to-end-walkthrough)
14. [Implementation Plan](#14--implementation-plan)
    - [Phase 1 — Sandbox configuration layer + coding agent process](#phase-1--sandbox-configuration-layer--coding-agent-process)
    - [Phase 2 — `ToolSearch` meta-tool](#phase-2--toolsearch-meta-tool)
    - [Phase 3 — Orchestrator delegation, NMB event loop, concurrency caps, and finalization](#phase-3--orchestrator-delegation-nmb-event-loop-concurrency-caps-and-finalization)
    - [Phase 4 — At-least-once NMB delivery](#phase-4--at-least-once-nmb-delivery)
    - [Phase 5 — Basic operational cron](#phase-5--basic-operational-cron)
    - [Phase 6 — Polish, hardening, and gaps document](#phase-6--polish-hardening-and-gaps-document)
15. [Testing Plan](#15--testing-plan)
    - [16.1 Unit Tests](#161-unit-tests)
    - [16.2 Integration Tests](#162-integration-tests)
    - [16.3 Safety Tests](#163-safety-tests)
16. [Risks and Mitigations](#16--risks-and-mitigations)
17. [Open Questions](#17--open-questions)
- [Appendix A — `git_clone` Authentication: Option Details](#appendix-a--git_clone-authentication-option-details)
- [Appendix B — Trust Model for Sandbox-Initiated Control-Plane Access](#appendix-b--trust-model-for-sandbox-initiated-control-plane-access)
- [Appendix C — Sandbox Configuration Layer: Implementation Detail](#appendix-c--sandbox-configuration-layer-implementation-detail)
- [Appendix D — Sandbox Detection: Implementation Detail](#appendix-d--sandbox-detection-implementation-detail)
- [Appendix E — Typed Protocol Payloads: Full Pydantic Definitions](#appendix-e--typed-protocol-payloads-full-pydantic-definitions)
- [Appendix F — Per-Task `model` Selection: Detailed Trade-offs](#appendix-f--per-task-model-selection-detailed-trade-offs)

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

### Patterns adopted from the OpenAI Agents SDK

After the [OpenAI Agents SDK deep dive](deep_dives/openai_agents_sdk_deep_dive.md),
two of its Tier 1 ("adopt now, low cost, high value") recommendations
land directly in M2b — they fit cleanly inside the
`task.assign` / `task.complete` shape that Phase 3 already needs to
build, and adopting them at protocol-design time is much cheaper than
retrofitting later:

| Pattern (SDK origin) | Lands in | Cost | What it buys |
|---|---|---|---|
| **Per-task `max_turns`** ([SDK §9.3](deep_dives/openai_agents_sdk_deep_dive.md#11--what-to-adopt--prioritized), `Agent.as_tool(max_turns=...)`) | §6 — `task.assign` payload, plumbed through to `AgentLoop.run()` | ~1 hr | Orchestrator-controlled per-task upper bound on tool rounds, independent of the global `agent_loop.max_tool_rounds`. Gives finalization a hard ceiling per delegation. |
| **Per-task `model` override** ([SDK §3](deep_dives/openai_agents_sdk_deep_dive.md#3--architecture-at-a-glance), `RunConfig(model=...)`) | §6 — `task.assign` payload, plumbed through to `AgentLoopConfig.model` | ~1 hr (app layer only) | Protocol scaffolding.  M2b accepts the L7-proxy constraint (one model, gateway-scoped routing — §6.5.1) and runs every sub-agent on the orchestrator's configured model; the field is recorded in the audit DB so the operational unblock — Option A multi-endpoint switching in M2b Phase 6, or Option D per-sandbox binding in M3 (gated on OpenShell support — §17 Q10) — picks it up unchanged. |
| **Typed protocol payloads** ([SDK §9.3](deep_dives/openai_agents_sdk_deep_dive.md#11--what-to-adopt--prioritized), `output_type=...` on sub-agent tools) | §6 — Pydantic models for `task.assign` / `task.complete` / `task.error` | ~2 hr | Catches malformed sub-agent responses at the wire; finalization tools consume typed fields instead of parsing free-form text. |

One more SDK pattern lands in M2b but later than Phase 3, and one is
explicitly **deferred** out of M2b:

- **Lazy `load_skill`** ([SDK §11.1.1](deep_dives/openai_agents_sdk_deep_dive.md#1111-lazy-skill-loading-load_skill), Tier 1) — scheduled for Phase 6 (§14
  implementation plan).  Skill metadata (name + description) ships in
  the system prompt; bodies and their `scripts/` / `references/` /
  `assets/` siblings copy into the sub-agent's workspace on demand
  via a `load_skill` tool.  Cheap (~1 day), pays off the moment a
  skill grows beyond a single `SKILL.md`, and lands in the natural
  cleanup window between Phase 3 (delegation goes live) and M3
  (multi-sandbox spawn pays the per-task cost again).
- **`Manifest`-style declarative workspace contract** ([SDK §11.2.1](deep_dives/openai_agents_sdk_deep_dive.md#1121-manifest-style-workspace-contract), Tier 2) — deferred to M3.  Useful when each sub-agent needs
  its own fresh workspace per task, which is the M3 multi-sandbox
  case.  M2b's same-sandbox process inherits the orchestrator's
  already-set-up filesystem; building a `Manifest` layer in M2b
  would be cost without payoff.  See
  [`design_m3.md` §5](design_m3.md#5--manifest--declarative-workspace-contract).

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Implement the **sub-agent coding process** that uses M2a's `AgentLoop` +
   file tools to execute coding tasks.
2. Define the full **sandbox setup sequence**: workspace, tools, comms, policy.
3. Implement **orchestrator → sub-agent delegation** via NMB `task.assign` and
   result collection via `task.complete`, with **typed Pydantic payloads**
   and a **per-task `max_turns`** override (both adopted from the
   OpenAI Agents SDK; see §6.3).
4. Build the **work collection and finalization** flow: collect sub-agent
   results, present to user, commit/push/create PR on approval.
5. Implement **per-agent concurrency caps** via `asyncio.Semaphore` and
   **spawn depth limits** (`max_spawn_depth`, `max_children_per_agent`).
6. Implement **at-least-once NMB delivery** for critical messages
   (`task.complete`, `audit.flush`).
7. Implement **`ToolSearch` meta-tool** for progressive tool loading.
8. Implement **basic operational cron**: sandbox TTL watchdog, stale-session
   cleanup, health checks.
9. Maintain audit, approval, and safety guarantees from M1.

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
│                                          │  File/Search/Bash/Git  │   │
│                                          │  Skill tool + skills/  │   │
│                                          │   (scratchpad skill →  │   │
│                                          │    notes-<slug>-<id>.md│   │
│                                          │    via file tools)     │   │
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
4. Workspace directory created and seeded with task context.  The
   orchestrator runs `git rev-parse` on the seeded clone and records
   the resulting SHA as the workflow's `WorkspaceBaseline` (§4.2.1,
   §6.6).
5. Sub-agent connects to NMB broker, sends `sandbox.ready`.
6. Orchestrator sends `task.assign` with task description, workspace
   path, and the pinned `WorkspaceBaseline`.
7. Sub-agent runs `AgentLoop` with coding file tools.
8. Sub-agent sends `task.complete` with result, diff (`git diff
   <base_sha>..HEAD`), echoed `WorkspaceBaseline`, and any notes file
   the agent created in its workspace.
9. Orchestrator verifies the echoed baseline matches what it
   assigned (§6.6.3) and runs model-driven finalization (§7).

### 4.2 Workspace Content Seeding

*Full specification: [original §5.2–5.3](design_m2.md#52--workspace-setup-setup-workspacesh)*

The orchestrator prepares the workspace before spawning the sub-agent:
- Clone/checkout the target repository (shallow clone for speed).  The
  coding agent itself also has `git_clone` and `git_checkout` tools
  (M2a §4); `git_clone` is fail-closed behind the
  `GIT_CLONE_ALLOWED_HOSTS` allowlist — empty disables the tool.
- No notes file is pre-seeded.  The `scratchpad` skill
  (`skills/scratchpad/SKILL.md`) teaches the agent to create a
  task-scoped file named `notes-<task-slug>-<agent-id>.md` on demand
  using the ordinary `read_file` / `write_file` / `edit_file` tools.
  Using a bare `notes.md` is forbidden — it collides the moment two
  agents run in the same workspace.
- The `skills/` directory is bundled with the sandbox image (Dockerfile
  copies it into `/app/skills`); M2a's `SkillLoader` discovers
  `SKILL.md` files from `SKILLS_DIR` at sub-agent startup and the
  `skill` tool exposes them via an enum.
- Seed memory directory (placeholder for M5+).

#### 4.2.1 Per-Agent Workspace Subdirectory and Baseline Tracking

Every sub-agent invocation gets its own
`<workspace_root>/agent-<agent_id>/` subdirectory; this lands as part
of M2b Phase 1 (`agent/__main__.py::_run_cli_mode`) and is regression-
tested by `tests/test_coding_agent_main.py::TestCliMode::test_cli_mode
_per_agent_subdirectory_is_created`.  The per-agent subdirectory is
the unit of file-system isolation: two concurrent sub-agents writing
to the same `notes-…-<agent-id>.md` filename can't clobber each other
because they're in different parent directories.

But filesystem isolation alone doesn't tell the **orchestrator** what
the diff in `TaskCompletePayload.diff` (§6.3) is *against*.  The
phrase "against the workspace's starting state" only has a definition
if the starting state is recorded somewhere both sides can refer to.
The orchestrator needs that record to:

1. Independently re-derive the diff for verification (`git diff
   <baseline>..<head>`) instead of trusting the sub-agent's free-text
   `diff` field.
2. Build a sensible PR base when finalisation chooses Strategy A
   (orchestrator pulls + pushes from a known commit — see §17 Q3 and
   [original §10.4](design_m2.md#104--git-operations-for-finalization)).
3. Re-issue a `re_delegate` against the *same* baseline so the second
   iteration's diff stays meaningfully comparable to the first
   iteration's.
4. Reason about parallel agents that each picked up a slightly
   different `HEAD` because upstream advanced between spawns.

The protocol records the baseline in two places: `TaskAssignPayload
.workspace_baseline` (orchestrator → sub-agent — what to start from)
and `TaskCompletePayload.workspace_baseline` (sub-agent → orchestrator
— what it actually observed at start time).  See §6.3 for the typed
fields and §6.6 for the parallel-spawn semantics.

### 4.3 Sandbox Cleanup

After `task.complete` or timeout:
1. Read artifacts from the sub-agent's workspace (diff, notes file,
   audit JSONL fallback). Same sandbox — direct filesystem access, no
   download.
   *(Multi-sandbox delegation in M3 will require `openshell sandbox exec` to
   pull files across sandbox boundaries.)*
2. Kill the sub-agent process.
3. Clean up workspace directory.
4. TTL watchdog ensures cleanup even if the orchestrator misses `task.complete`.

### 4.4 Authenticated `git_clone` for Private Hosts

> **Motivating user report:** A sandbox user running
> `git clone https://gitlab.example.com/…` hit
> `fatal: unable to access '…': CONNECT tunnel failed, response 403`
> even with the `gitlab` network policy correctly applied (host
> allowed, port 443 full access, `/usr/bin/git` on the binary
> allowlist); `curl` against the same host returned 200.

**Problem.**  Public-host HTTPS clones work after this milestone's
image + policy delta.  Private hosts reject with HTTP 401 because
git, unlike our REST tools, doesn't read the provider-injected
`$GITLAB_TOKEN` / `$GERRIT_*` placeholders that the L7 proxy
substitutes at TLS termination.  The credentials are present in the
sandbox env; git just doesn't have a hook to surface them as an
`Authorization` header.  Design constraints and the credential-
source enumeration this rules out are in
[Appendix A §A.0](#a0-design-constraints).

**Options summary** (full per-option details in
[Appendix A](#appendix-a--git_clone-authentication-option-details)):

| Option | Sketch | Secret-leak risk | Composes with provider system? |
|---|---|---|---|
| **A — URL-embedded token** (`https://oauth2:${TOKEN}@host/...`) | Operator-only workaround | High — token in process listing, URL, and `.git/config` | Yes |
| **B — Git credential helper** (recommended) | Image-bundled helper prints `$GITLAB_TOKEN` placeholder; git puts it in `Authorization`; L7 proxy substitutes the real value | Low — in-memory, per-invocation | Yes |
| **C — SSH keys** | Mount key + `known_hosts`; SSH bypasses the L7 proxy | Medium — keys coarser than per-repo tokens | No — parallel credential channel |
| **D — Host-side seed** | Tar the operator's local working tree into the sandbox at spawn; never clone | None (secrets stay on host) | N/A — sidesteps the clone path |

**Recommended phasing:**

- **Phase 1 (landed):** document the problem; private-host clones
  fail loudly with HTTP 401 instead of confusing tool errors.
  `.env.example` documents Option A as a short-term workaround
  with the leakage caveat.
- **Phase 3:** implement Option B (credential helper).  Handles
  the 90% case (clone + subsequent `pull` / `push`) without
  redesigning secret management.
- **Phase 4+:** evaluate Option D (host seed) if interactive Slack
  workflows frequently reference an operator's local checkout.
- **Deferred:** Option C (SSH), unless a host specifically requires
  it.

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

**One inference-policy caveat.** The orchestrator's network policy
([`policies/orchestrator.yaml`](../policies/orchestrator.yaml)) allows
GET / POST to a single host — `inference.local` — and the gateway-side
`openshell inference set --provider … --model …` configures **one**
upstream model that the L7 proxy substitutes into every outbound
request, **overriding the `model` field the app sent**.  This is a
property of the inference proxy, not of the network policy: even if
the orchestrator's `task.assign` carries `model="claude-haiku"`, the
sandbox's chat-completions request comes back from the proxy as
whatever the gateway was configured with at `openshell inference set`
time.  §6.5 spells out what this means for the per-task model
override and the four deployment shapes (multi-endpoint, proxy
passthrough, app-side fan-out, per-sandbox provider) that could
deliver it.  The network-policy file itself **does not change** in
M2b — adding more inference routes is an M3-or-later policy delta.

#### 5.1.1 Sandbox-Initiated Control-Plane Access

Several proposed enhancements (per-task inference selection §6.5.1
Option D, M3 policy hot-reload, M3 sub-agent spawn) ask the same
question: *can a sandbox call the gateway control plane?* In M2b
the mechanical answer is "no" (no `openshell` binary in the image,
no gateway-API policy entry, no mTLS credentials provisioned). The
principled answer — *should* we wire those when later milestones
arrive — matters more.

**The OpenShell trust model is asymmetric on purpose:** the gateway
is the control plane, the sandbox is constrained execution, and
the constraint is enforced *outside* the LLM-steered process. A
sandbox that can reconfigure the gateway inverts that asymmetry —
a prompt-injected agent could re-route inference, widen its own
policy, or spawn permissive sandboxes. The OpenShell deep dive is
explicit that the gateway itself doesn't distinguish "orchestrator
sandbox" from "sub-agent sandbox" — the trust hierarchy is
application-level, so we have to impose it deliberately. Full
breakdown (failure modes, deep-dive citations, per-implication
discussion) lives in [Appendix B](#appendix-b--trust-model-for-sandbox-initiated-control-plane-access).

The principle:

| Caller | Control-plane access | Why |
|---|---|---|
| **Sub-agent sandbox** | **No.** Per-task model / policy / spawn requests go via NMB to the orchestrator. M3's `policy.request` flow ([`design_m3.md` §7](design_m3.md#7--policy-hot-reload)) is the canonical shape. | Containment requires the constraint be enforced from outside the constrained process. |
| **Orchestrator sandbox** | **Narrow yes, behind an approval gate.** Scoped to specific verbs (e.g. `sandbox create / delete / upload / download`, `policy set --sandbox <child>`), gated by the M1 Approve/Deny pattern, audit-logged with `workflow_id`. Self-modification stays disallowed. | The orchestrator is the trusted parent of the sub-agent hierarchy, but is itself LLM-driven and prompt-injectable. |
| **Operator's host shell** | **Yes.** The authoritative trust root. Where M2b pins gateway state (e.g. Option A multi-endpoint provider registrations), that state lives here. | Operator owns the trust root: registered the providers, started the gateway, signed the mTLS root. |

Three direct consequences:

1. §6.5.1 Option A (multi-endpoint switching, host-side state)
   stays the default even after Option D's mechanical preconditions
   land — Option A is *strictly safer* because it keeps the
   control-plane operation off the orchestrator's sandbox.
2. M3's `SandboxClient.create()` from the orchestrator sandbox
   needs scoped CLI surface + per-invocation audit; tracked in
   `design_m3.md`.
3. Future "sub-agent wants to influence the control plane"
   capabilities follow the M3 `policy.request` shape — never direct
   gateway access.

Tracked as the principled side of §17 Q10.

### 5.2 NMB Setup in Sub-Agents

Sub-agents connect to the NMB broker at startup. The broker URL is provided
via environment variable. Authentication is by sandbox identity.

### 5.3 Sandbox Configuration Layer (`/app/config.yaml`)

> **Status (Phase 1 landed).** PR #13 and PR #14 landed the
> resolved-YAML overlay, removed every `in_sandbox` branch and every
> `_SANDBOX_*` constant from `config.py`, and dropped the bare-process
> "local-dev" runtime in favour of sandbox-only execution.  §5.3.6
> records the migration; this section stays as the *why* behind the
> resolved-YAML pattern.

#### 5.3.1 Problem Statement

Pre-Phase 1, non-secret config (log level, workspace root, feature
flags, tuning parameters) reached the sandbox through three
incomplete mechanisms — provider-injected env vars (HTTP-header
values only), Dockerfile `ENV` directives (static, build-time), and
`in_sandbox` branches in `config.py` (`_SANDBOX_*` constants
selected on `OPENSHELL_SANDBOX`).  None of them allowed
per-deployment override without rebuilding the image, and
`openshell sandbox create` has no `--build-arg` or `--env`
escape hatch.  The `_SANDBOX_*` constants also leaked internal
hostnames into every `git push` — visible to `git grep nvidia.com
src/`.

#### 5.3.2 Three Categories of Configuration

The fix starts from the observation that the config problem isn't
a clean secret/non-secret split — there are **three** categories,
each with its own delivery path:

| Category | Example fields | Where it lives now | Why this category exists |
|----------|----------------|--------------------|--------------------------|
| **A — Public non-secret** | `log.level`, `orchestrator.model`, `audit.db_path`, `coding.workspace_root`, tuning knobs | `config/defaults.yaml` (committed) → resolved YAML → `/app/config.yaml` | Operators want to tune these per deployment without rebuilding the image; harmless to ship publicly so the defaults file is committed. |
| **B — Private non-secret** | `coding.git_clone_allowed_hosts`, `ENDPOINTS_NEEDING_ALLOWED_IPS`, `ALLOWED_IPS`, internal URLs | `.env` (gitignored) → resolver → `config/orchestrator.resolved.yaml` (gitignored) → `/app/config.yaml` | Not credentials (the app reads them directly, not via HTTP headers), but they shouldn't ship in the public repo.  The resolver is the bridge. |
| **C — Secret** | `SLACK_BOT_TOKEN`, `JIRA_AUTH`, `GITLAB_TOKEN`, inference API key | `.env` (gitignored) → OpenShell providers → proxy placeholders → process env | Tokens flow through HTTP headers; the L7 proxy substitutes the real value at request time so the sandbox process never holds the secret in plaintext. |

Category B is what the `_SANDBOX_*` workaround was hacking around.

#### 5.3.3 Resolver Pattern

The same shape as `scripts/gen_policy.py` (which already handles
network-policy `allowed_ips` CIDRs):

| File | Purpose | Committed? | Shipped in image? |
|------|---------|------------|-------------------|
| `config/defaults.yaml` | Category A defaults + fail-closed category-B placeholders | ✅ committed | ✅ (intermediate build input) |
| `.env` | Category B overrides + category C secrets | ❌ gitignored | ❌ never |
| `config/orchestrator.resolved.yaml` | Merged: defaults + category-B from `.env` | ❌ gitignored | ✅ as `/app/config.yaml` |
| `scripts/gen_config.py` | Reads `defaults.yaml` + `.env` → writes resolved YAML | ✅ committed | — |

OSS / CI consumers without a `.env`: `gen_config.py` emits a
resolved file byte-identical to `defaults.yaml`, with all category-B
fields at their fail-closed values — correct security posture for a
fresh clone.

Build-time and runtime step lists are in
[Appendix C §C.0](#c0-build-and-runtime-flow).

#### 5.3.4 Secret-Suffix Invariant

`scripts/gen_config.py` enforces a hard line: keys whose names
match `*_TOKEN` / `*_AUTH` / `*_PASSWORD` / `*_KEY` are refused at
the resolver, and the broader secret-suffix list (`auth_header`,
`token`, `api_token`, `username`, `http_password`, `bot_token`,
`app_token`, `user_token`, `api_key`, `jina_api_key`) is excluded
from both the YAML schema and the `_CATEGORY_B_KEYS` allowlist.
Secrets stay on the provider → L7 proxy → process env path so the
sandbox never holds them in plaintext.  The schema dump and the
`_CATEGORY_B_KEYS` mapping live in
[Appendix C §C.1](#c1-defaultsyaml-schema-and-_category_b_keys-mapping).

#### 5.3.5 Configuration Precedence

Load order at runtime, lowest to highest precedence:

| # | Source | Scope | Notes |
|---|--------|-------|-------|
| 1 | Dataclass field defaults | Process-wide | Hardcoded in `config.py` |
| 2 | `/app/config.yaml` (resolved YAML baked into the image) | Per-deployment | Missing file → empty overlay |
| 3 | Env vars (**secrets only**) | Per-run | `SLACK_BOT_TOKEN`, `JIRA_AUTH`, `GITLAB_TOKEN`, …; `NEMOCLAW_CONFIG_PATH` selects an alternate YAML |
| 4 | Provider-injected placeholders | Per-run | Resolved by the L7 proxy at HTTP-request time |

The key invariant is that **YAML is the only source for non-secret
knobs and secrets stay in env** — no env-var overrides for
non-secret fields, and category-B values never touch
`os.environ` inside the sandbox.  `AppConfig.load()` walks this
precedence; `create_orchestrator_config()` /
`create_coding_agent_config()` are thin builders that name intent
at the call site.  Code in
[Appendix C §C.2](#c2-appconfigload-and-helper-functions).

#### 5.3.6 Phase 1 Migration

Headline outcomes from PR #13 + PR #14:

- All `_SANDBOX_*` constants gone from `config.py`; `git grep
  nvidia.com src/` returns the single public SaaS URL allowed by
  the carve-out.
- `config/defaults.yaml` ships category-B placeholders;
  `scripts/gen_config.py` resolves them from `.env` at
  `make setup-sandbox` time; the resolved YAML is gitignored.
- `AppConfig.load()` + the `create_*_config` builders replaced
  `load_config()`; non-secret env-var overrides
  (`LOG_LEVEL`, `AGENT_LOOP_*`, `NMB_URL`, …) removed.
- Runtime detector (`runtime.py`) classifies `SANDBOX` /
  `INCONSISTENT` only — `LOCAL_DEV` and its escape hatches gone.
- `.env.example` rewritten as secrets + category-B only.

Per-PR file-by-file changelog and the operator-side before/after
walkthrough are in
[Appendix C §C.3](#c3-phase-1-migration-record).

#### 5.3.7 Policy-file Resolver (same pattern, different file)

`scripts/gen_policy.py` applies the same shape to
`policies/orchestrator.yaml`: committed policy ships with
`host: ""` placeholders for the GitLab / Gerrit endpoints, and at
build time the resolver substitutes the hostname from `.env`'s
`GITLAB_URL` / `GERRIT_URL`.  Same env vars also drive
`gen_config.py`'s `toolsets.{gitlab,gerrit}.url`, so operators
don't maintain the hostname twice.  Substitution runs *before* the
`allowed_ips` injection so the SSRF-bypass matcher sees the final
host; an empty `.env` leaves the placeholder in place, which
OpenShell rejects at apply time — fail-closed.

The `_POLICY_HOST_SUBSTITUTIONS` mapping is in
[Appendix C §C.4](#c4-gen_policypy-host-substitution-mapping).

### 5.4 Sandbox Detection and Startup Self-Check

#### 5.4.1 Problem Statement

Before M2b P1, `in_sandbox` depended on a single signal:
`os.environ.get("OPENSHELL_SANDBOX")`.  The value is *never set by
this repo* — the code assumed OpenShell injects it automatically.
If the convention changed (new OpenShell version, custom image,
misconfigured profile), the app silently fell back to local-dev
defaults: inference calls failed with empty `INFERENCE_HUB_BASE_URL`,
audit DB written to a path that doesn't exist in the sandbox
filesystem, file tools rooted outside the policy's read-write set.

§5.3 landed the YAML overlay so paths come from the resolved config
directly.  The runtime self-check below catches the residual failure
mode: "the YAML says one thing, but the environment the process is
actually running in looks nothing like a sandbox".

#### 5.4.2 Multi-Signal Detection

`detect_runtime_environment()` evaluates six independent signals and
returns a single classification:

| # | Signal | Meaning |
|---|--------|---------|
| 1 | `OPENSHELL_SANDBOX` env var set | OpenShell's self-identification |
| 2 | `/sandbox` directory exists and is writable | PVC mount point present |
| 3 | `/app/src` exists and is read-only | Dockerfile-copied app source |
| 4 | `HTTPS_PROXY` env var set | L7 proxy is active |
| 5 | `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` set | OpenShell CA installed |
| 6 | `inference.local` DNS resolves | Inference proxy reachable |

- **`SANDBOX`** — at least 4 of 6 signals present.
- **`INCONSISTENT`** — below threshold.  Almost always a deployment
  bug (policy drift, OpenShell version skew, bare-process execution).
  `main.py` / `agent/__main__.py` refuse to start in this state —
  there is no `LOCAL_DEV` fallback (Phase 1 follow-up dropped it),
  the OpenShell sandbox is the only supported runtime.

The 4-of-6 threshold tolerates minor signal drift (e.g. the
`inference.local` DNS lookup is flaky) without weakening the check.
Test-only threshold-tuning, the rationale for the specific signal
choices, and the full structured-log payload format live in
[Appendix D](#appendix-d--sandbox-detection-implementation-detail).

#### 5.4.3 Startup Self-Check Wiring

`main.py` and `agent/__main__.py` call `detect_runtime_environment()`
immediately after logging setup and *before* `AppConfig.load()`.  On
`INCONSISTENT` they raise `SandboxConfigurationError` with a
structured log entry naming present / missing signals, the likely
cause, and a suggested fix.  On `SANDBOX` the same signals log at
INFO so operators can inspect them at any time.

The self-check runs before config loading by design: the loader
trusts that it's inside a healthy sandbox without re-deriving the
classification, so there's no `env=` kwarg to thread through.  The
loader sees only the resolved YAML at `/app/config.yaml` plus
secret env vars; both assume the self-check passed.

The cost is ~50 lines of Python and one extra log line per startup;
the benefit is catching the full family of "I thought I was in a
sandbox" bugs at boot rather than as downstream surprises hours
later.  Detailed justification in [Appendix D §D.3](#d3-why-not-trust-openshell_sandbox-alone).

---

## 6  Orchestrator ↔ Sub-Agent Protocol

*Full specification: [original §9](design_m2.md#9--orchestrator--sub-agent-protocol).
M2b refines the original payload shapes by replacing the JSON sketches
with **Pydantic dataclasses** (§6.3) — the OpenAI Agents SDK's typed-
output pattern, applied at protocol-payload granularity.*

### 6.1 Message Flow

```
Orchestrator                    NMB Broker                    Coding Agent
    │                              │                              │
    │── task.assign ──────────────▶│──────────────────────────────▶│
    │  (TaskAssignPayload,         │                              │
    │   incl. max_turns)           │                              │
    │                              │                              │
    │                              │◀─── task.progress (opt) ─────│
    │◀─────────────────────────────│  (TaskProgressPayload)       │
    │                              │                              │
    │                              │◀─── audit.flush ─────────────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │                              │◀─── task.complete ───────────│
    │◀─────────────────────────────│  (TaskCompletePayload)       │
    │                              │                              │
    │── task.complete.ack ────────▶│──────────────────────────────▶│
```

### 6.2 Sub-Agent Entrypoint

The sub-agent is a standalone Python process:

```python
# agent/__main__.py
async def main():
    # Runtime self-check first — refuses to start if the sandbox
    # signals don't add up (see §5.4).
    runtime = detect_runtime_environment()
    if runtime.classification is RuntimeEnvironment.INCONSISTENT:
        raise SandboxConfigurationError(runtime)

    # Config = defaults.yaml overlay + secret env vars only.  The
    # sub-agent opts out of Slack token validation (``require_slack=False``).
    cfg = create_coding_agent_config()

    bus = await MessageBus.connect(cfg.nmb.broker_url)
    agent = CodingAgent(bus=bus, backend=backend, config=cfg)
    await agent.run()
```

The `CodingAgent` (Layer 3) uses the same `AgentLoop` (Layer 1) from M2a
with a coding-specific tool registry built by
`create_coding_tool_registry()` (file, search, bash, git — M2a §4).
There is no dedicated scratchpad class or pair of
`scratchpad_read` / `_write` tools — that primitive was removed in M2a
in favour of a pure convention.  The `scratchpad` skill
(`skills/scratchpad/SKILL.md`) teaches the agent to create and edit a
task-scoped `notes-<task-slug>-<agent-id>.md` file in its workspace
using the ordinary file tools; the file carries an `Owner:` header
(keyed off the `Agent ID` line in the prompt's runtime-metadata layer)
so concurrent agents in the same workspace can't silently clobber each
other's working memory.  The system prompt is a four-layer builder
(identity / task-context / runtime-metadata / channel-hint) — the
fifth, auto-injected scratchpad layer from earlier drafts no longer
exists.

### 6.3 Typed Protocol Payloads

The original M2 design [`§9`](design_m2.md#9--orchestrator--sub-agent-protocol)
documented `task.assign` / `task.complete` / `task.error` as JSON
sketches. M2b promotes those sketches to **Pydantic models** that
both sides validate at the wire — adopted from the OpenAI Agents SDK's
typed sub-agent output pattern (`Agent.as_tool(output_type=...)`,
[deep dive §11.1.3](deep_dives/openai_agents_sdk_deep_dive.md#1113-structured-taskcomplete-output)).
The motivation is concrete: §7.1's finalization model needs to read
fields like the diff and notes-file path out of `task.complete`;
with a typed payload it does, with a free-form one it has to
LLM-parse.  Bad payloads fail at receive time with a single
`ValidationError` log line instead of cascading into the finalization
flow as malformed text.

The protocol is six models, all in `nmb/protocol.py`:

| Model | Direction | Carries (most-relevant fields) |
|---|---|---|
| `TaskAssignPayload` | Orch → Sub | `prompt`, `workflow_id`, `parent_sandbox_id`, `max_turns` (§6.4), `model` (§6.5), `tool_surface`, `context_files`, `workspace_baseline` (§6.6), `is_iteration` / `iteration_number` |
| `TaskProgressPayload` | Sub → Orch | `workflow_id`, `status` (`starting` / `reading_workspace` / `writing_code` / `running_tests` / `finalizing`), `pct`, `current_round`, `tokens_used`, `note` |
| `TaskCompletePayload` | Sub → Orch | `workflow_id`, `summary`, `diff`, echoed `workspace_baseline`, `files_changed`, `notes_path`, `git_commit_sha`, `tool_calls_made`, `rounds_used`, `model_used`, `suggested_next_step` |
| `TaskErrorPayload` | Sub → Orch | `workflow_id`, `error`, `error_kind` (`max_turns_exceeded` / `tool_failure` / `policy_denied` / `inference_error` / `other`), `recoverable`, `notes_path`, `traceback` |
| `WorkspaceBaseline` | helper, embedded in assign / complete | `repo_url`, `branch`, `base_sha`, `is_shallow` (§6.6) |
| `ContextFile` | helper, embedded in assign | `path` (workspace-relative), `content`, `encoding` (`utf-8` / `base64`) |

Per-field semantics, validators, and full docstrings live in
[Appendix E](#appendix-e--typed-protocol-payloads-full-pydantic-definitions).

Three implementation notes that *don't* belong in the appendix
because they're protocol-design choices rather than per-field
spec:

1. **Validation is on receive, not send.** The orchestrator's NMB
   listener parses every incoming `task.complete` / `task.error`
   / `task.progress` through the matching model and logs a single
   structured `ValidationError` if the wire payload doesn't
   conform.  The audit DB stores the raw JSON alongside the
   validation outcome so a malformed payload from a misbehaving
   sub-agent is recoverable for forensics.
2. **The models are the protocol.**  `nmb/protocol.py` is the
   single source of truth — both the orchestrator and the
   sub-agent import from it, so there is no way for one side to
   drift from the other.
3. **The behavioural new fields** in M2b's protocol vs. M2's
   design sketch are `TaskAssignPayload.max_turns` (§6.4),
   `TaskAssignPayload.model` (§6.5),
   `TaskCompletePayload.model_used` (audit-trail half of §6.5),
   and `TaskAssignPayload.workspace_baseline` /
   `TaskCompletePayload.workspace_baseline` (the diff-baseline
   pair — §4.2.1, §6.6).  Everything else is a typing exercise on
   already-shipped JSON shapes.

### 6.4 `max_turns` Semantics

`AgentLoop` already exposes `max_tool_rounds: int` on its
`AgentLoopConfig`, currently set once at process startup from
`cfg.agent_loop.max_tool_rounds`. M2b extends this so that **the
orchestrator can pin a different ceiling per task.assign**:

```
effective_max_turns = task.max_turns if task.max_turns is not None
                      else cfg.agent_loop.max_tool_rounds
```

Resolution lives in `agent/__main__.py` — when the sub-agent receives
`task.assign`, it builds an `AgentLoopConfig` for that one run with
the resolved value, leaving the process-wide config untouched.

Three motivating cases:

| Task shape | `max_turns` |
|---|---|
| One-shot lookup ("show me the README") | 3 |
| Routine bug fix in a known module | 10–15 |
| Multi-file refactor with verification step | 30 |

The orchestrator picks the value when it builds the
`TaskAssignPayload`; the inference budget per workflow stays
predictable; and a runaway agent in an unbounded loop is bounded
by the cap, not by `cfg.agent_loop.max_tool_rounds` (which would
otherwise have to be set high enough for the worst case at the
expense of every other case).

When the cap is hit, the sub-agent emits
`TaskErrorPayload(error_kind="max_turns_exceeded", recoverable=True)`
with whatever partial work it produced reachable via `notes_path`.
The finalization model (§7.1) treats this as a normal recoverable
error and decides whether to `re_delegate` with a higher ceiling
or surface the partial result to the user.

### 6.5 Per-Task `model` Semantics

The orchestrator and the coding sub-agent are the same `AgentLoop`
class with different tool registries (§6.2); `AgentLoopConfig.model`
is already a per-loop string. M2b ships the protocol path for the
orchestrator to pin a sub-agent's model **per task** in the same
way `max_turns` (§6.4) is per task — but the L7 proxy makes it
operationally a no-op in M2b's same-sandbox topology (§6.5.1).
Real per-task selection lands in M3 with Option D. Phase 3's
investment is the wire field plus the audit trail, sized to be
free for M3 to pick up.

Resolution at the app layer:

```
effective_model = task.model if task.model is not None
                  else cfg.agent_loop.model
```

— picked at `task.assign`-receive time in `agent/__main__.py` and
written into the `AgentLoopConfig` for that one run. The
process-wide config is untouched. In M2b, the value the proxy
forwards is still whatever `openshell inference set --model` was
configured with at gateway setup time, regardless of
`effective_model`; the field is recorded in the audit DB but does
not change which upstream model serves the request.

Motivating task shapes (one-shot lookups → cheap model, routine
bug fix → default, multi-file refactor → reasoning-tier) are
listed in [Appendix F §F.1](#f1-motivating-task-shapes-for-per-task-model-selection)
— illustrative once M3 lands operational selection, irrelevant in
M2b.

#### 6.5.1 The L7-Proxy Constraint and Milestone Split

The L7 proxy at `inference.local` is configured with **one** upstream
model at the **gateway** level and rewrites the chat-completions
`model` field on every request to match — documented in
[`docs/blog_posts/m1/m1_setting_up_nemoclaw.md`](blog_posts/m1/m1_setting_up_nemoclaw.md)
lesson 12 ("`openshell inference set --model` **forces** the model
name"). Three constraints follow: routing is gateway-scoped (no
`--sandbox` flag); mutable but globally (mid-task `inference set`
swaps the model for every in-flight call across every sandbox);
control-plane reach isn't wired from inside the sandbox today,
and §5.1.1 argues it shouldn't be even when wired.  Detailed
breakdown of each constraint in [Appendix F §F.2](#f2-l7-proxy-constraints-detailed-breakdown).

**M2b accepts this and runs every sub-agent on the orchestrator's
single configured model.**  Phase 3a ships the `task.model` field
as protocol scaffolding (so the orchestrator can already record
*which model it would have liked* in the audit DB), but the field
has no operational effect in M2b.

Four options closed the gap; the verdicts:

- **A — Multi-endpoint provider switching:** register one
  provider per upstream, the orchestrator picks `cfg.inference.base_url`
  per task. The only option giving concurrent per-task selection
  without spawning sandboxes; bounded operator-side cost; survives
  unchanged into M3.
- **B — Proxy passthrough:** requires a proxy change we don't own.
- **C — App-side fan-out:** loses the L7 proxy's credential-
  substitution benefit. Developer escape hatch only.
- **D — Per-sandbox inference provider** (speculative): cleaner
  operationally than A *if* (a) OpenShell adds `--sandbox` scoping
  and (b) the trust-model cost in §5.1.1 can be paid. Even then,
  Option A stays preferred because it keeps the control-plane
  operation off the orchestrator's sandbox.

Per-option sketches with full trade-offs are in
[Appendix F §F.3](#f3-options-considered-full-comparison).

Lifecycle:

1. **M2b Phase 3a (protocol foundations):** ship `task.model` on
   the wire; thread to `AgentLoopConfig.model`. Sub-agents run on
   the single configured model; audit DB records what was
   requested.
2. **M2b Phase 6 or M3, whichever needs it first:** land Option A
   — multi-endpoint provider switching. M2b polish if a per-task
   requirement surfaces before M3; default path in M3 regardless.
3. **M3 (only if Option D's preconditions land and §5.1.1's trust-
   boundary cost is paid):** consider D as an alternative, not a
   replacement.

**Option A is the production path for real per-task selection at
both M2b and M3.** Option D is a possible specialization, not the
default.

#### 6.5.2 Phase 3 Scope

Phase 3a (protocol + delegation, §14) ships the app-layer plumbing:
add `model: str | None` to `TaskAssignPayload` (§6.3), thread it
into a one-shot `AgentLoopConfig`, expose it as a `delegate_task`
argument, and audit-record the requested model on every
delegation. That's the whole M2b investment. No multi-endpoint
registration, no extra `inference-*.local` policy entries, no
per-model `base_url` lookup table — all of those land later
(Option A polish in M2b Phase 6, or in M3) and are independent of
the wire field this phase ships.

#### 6.5.3 Who Picks the Per-Task Model?

Deferred to whichever milestone first lands operational selection
(Option A polish in M2b Phase 6, or M3) — there's nothing to pick
in M2b's scaffolding-only phase.  When the question becomes real,
it has the same shape as `max_turns` (open question 4): hardcoded
per-shape lookup table on the orchestrator side vs. model-driven
sizing.  Tracked in §17 Q8.

### 6.6 Workspace Baseline Semantics

§4.2.1 motivated `WorkspaceBaseline` from the diff-interpretation
side; §6.3 typed the field on both `TaskAssignPayload` and
`TaskCompletePayload`.  This subsection covers the runtime
semantics — who chooses the baseline, how parallel agents reach
matching baselines (or deliberately don't), and what the
orchestrator does with the echoed value at finalisation time.

#### 6.6.1 Who Sets the Baseline

The orchestrator owns the choice.  At delegation time it:

1. Determines the target `repo_url` and `branch` from the user
   request and any cached repo metadata.
2. Resolves `branch` to a concrete SHA at the time of delegation —
   for an in-image clone, by reading
   `git rev-parse origin/<branch>` after the workspace is seeded;
   for a fresh `git_clone` from the sub-agent, by passing the
   refspec on through the workspace materialiser and reading the
   SHA back before sending `task.assign`.
3. Constructs a `WorkspaceBaseline(repo_url=..., branch=...,
   base_sha=..., is_shallow=...)` and pins it on
   `TaskAssignPayload.workspace_baseline`.

The sub-agent does **not** invent a baseline.  If the orchestrator
sent `workspace_baseline=None`, the sub-agent is being asked a
non-diff-producing question; the diff field stays `""` regardless
of any incidental file edits the agent may make.

#### 6.6.2 Parallel Agents on the Same Repo

Three concrete parallel cases, each with the resolution that falls
out of "the orchestrator pins the baseline":

| Case | What happens | Resolution |
|------|--------------|------------|
| **A. Same workflow, two sub-agents** (e.g., M3's coding+review pair on one task) | Both get the same `workspace_baseline`; their diffs are comparable to each other and against the same base | `delegate_task` constructs one `WorkspaceBaseline` per workflow and reuses it for every sub-agent in that workflow. |
| **B. Two workflows, same repo, near-simultaneous spawn** (e.g., user files two coding tasks back-to-back) | Each workflow gets its own `WorkspaceBaseline`; they will *usually* match (`origin/main` didn't move) but may differ if upstream advanced between spawns | The orchestrator records both baselines independently in the audit DB.  Finalisation does **not** assume the two diffs are mutually applicable; the user (or M3's review agent) handles cross-workflow merges. |
| **C. `re_delegate` after the first iteration** | The second `task.assign` carries the *same* `workspace_baseline` as the first, even though the sub-agent's HEAD has moved since iteration #1 | Phase 3's `re_delegate` tool reuses the workflow's pinned baseline so iteration #2's diff stays comparable to iteration #1's; the agent's local commits from iteration #1 get rebased onto the pinned base before the new task starts. |

The principle is **the baseline is a property of the workflow, not
of the sandbox**.  Multiple concurrent sandboxes within one
workflow share a baseline; each workflow is independent.

#### 6.6.3 Verification at Finalisation

When `task.complete` arrives, the orchestrator checks two
invariants before running the finalisation `AgentLoop`:

1. **Echo match.** `task_complete.workspace_baseline ==
   task_assign.workspace_baseline` (compared structurally; SHAs
   string-equal).  A mismatch means the sub-agent ran a
   `git_checkout` or `git_reset` mid-task and ended up rooted
   somewhere else; the orchestrator surfaces this to the user as a
   `baseline_drift` error rather than pushing a diff against the
   wrong base.
2. **Diff sanity.** When the orchestrator has the workspace on
   disk (M2b same-sandbox case), it can run `git diff
   <base_sha>..HEAD` itself and compare the result to the
   sub-agent's reported `diff`.  Any disagreement is logged at
   `WARNING` level; the orchestrator's locally-computed diff wins.
   M3's multi-sandbox case skips this — the orchestrator doesn't
   have the working tree by hand — and falls back to trusting the
   echo-match check plus a `git apply --check` of the reported diff
   against a fresh clone at `base_sha`.

#### 6.6.4 Why Not Use `TaskCompletePayload.git_commit_sha` as the Baseline?

Two distinct things, each carrying its own field:

- `workspace_baseline.base_sha` answers "what state should the diff
  be read against?" — pinned by the orchestrator at spawn time,
  echoed by the sub-agent.
- `git_commit_sha` answers "what new SHA did the sub-agent's
  `git_commit` produce?" — `None` when the sub-agent didn't run
  `git_commit` at all, in which case the diff covers uncommitted
  working-tree changes that finalisation will commit on the
  orchestrator side.

Conflating them was the temptation; the reason not to is iteration:
after `re_delegate`, `git_commit_sha` advances per iteration but
`base_sha` stays pinned to the workflow's baseline.  Two fields,
two roles.

---

## 7  Work Collection and Finalization

*Full specification: [original §10](design_m2.md#10--work-collection-and-finalization)*

### 7.1 Model-Driven Finalization

After receiving `task.complete`, the orchestrator runs a second `AgentLoop`
invocation with **finalization tools**. The model sees the sub-agent's result
— a validated `TaskCompletePayload` (§6.3) with named fields for `summary`,
`diff`, `files_changed`, `notes_path`, `git_commit_sha`, and the optional
`suggested_next_step` — and **synthesizes** it before deciding what to do.
Because the payload is typed, the orchestrator's finalization-context builder
is mechanical templating, not LLM-driven free-form parsing:

- **Quality assessment** — inspect the diff for obvious issues, check whether
  tests passed, note any open questions from the sub-agent's notes.
- **Result summarization** — produce a user-facing summary that distills the
  sub-agent's work into a concise description (the sub-agent's raw output is
  often too verbose or technical for direct presentation).
- **Multi-agent synthesis** (M3+) — when multiple sub-agents contribute to the
  same task (e.g., coding agent + review agent), the finalization model merges
  their outputs, resolves conflicts, and presents a unified result.
- **Proactive iteration** — if the model notices failing tests or incomplete
  work in the sub-agent's notes, it can call `re_delegate` with a fix prompt
  without waiting for user feedback.

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
8. `task.complete` sent with diff, notes file, summary.
9. **Orchestrator** runs finalization `AgentLoop` → model calls
   `present_work_to_user`.
10. **User** sees diff in Slack, clicks **[Push & PR]**.
11. Model calls `push_and_create_pr` → branch pushed, PR created.
12. Sub-agent process cleaned up.

---

## 14  Implementation Plan

### Phase 1 — Sandbox configuration layer + coding agent process

| Task | Files | Status |
|------|-------|--------|
| Ship `config/defaults.yaml` (public-safe, no category-B values) | `config/defaults.yaml` | ✅ |
| Implement `scripts/gen_config.py` (mirrors `gen_policy.py`) with category-B allowlist and secret-field guard | `scripts/gen_config.py` | ✅ (commits `7d8f0d2`, `9607192`) |
| Add `gen-config` Make target; make it a prerequisite of `setup-sandbox` | `Makefile` | ✅ `Makefile:193, 200` |
| Gitignore `config/orchestrator.resolved.yaml` | `.gitignore` | ✅ `.gitignore:199` |
| Dockerfile: `COPY config/orchestrator.resolved.yaml /app/config.yaml`; add `/app/config.yaml` to read-only filesystem policy | `docker/Dockerfile.orchestrator`, `policies/orchestrator.yaml` | ✅ `Dockerfile.orchestrator:61`, `policies/orchestrator.yaml:43` |
| **Remove the public-repo leak**: delete `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS`, `_SANDBOX_CODING_WORKSPACE_ROOT`, `_SANDBOX_SKILLS_DIR` constants from `config.py` | `src/nemoclaw_escapades/config.py` | ✅ (commit `28c0041`) |
| Implement `AppConfig.load()` with YAML + secret-env-var precedence | `src/nemoclaw_escapades/config.py` | ✅ `AppConfig.load()` + `create_orchestrator_config()` / `create_coding_agent_config()` builders (PR #14 follow-up) |
| Remove `in_sandbox` branches for paths / host allowlists | `src/nemoclaw_escapades/config.py` | ✅ Paths flow through YAML overlay.  Phase 1 follow-up also dropped the `LOCAL_DEV` runtime and the `env=RuntimeEnvironment` kwarg on `AppConfig.load`; the loader no longer branches on runtime classification at all. |
| Implement `detect_runtime_environment()` with multi-signal check | `src/nemoclaw_escapades/runtime.py` (new) | ✅ `runtime.py::detect_runtime_environment` — two classifications (`SANDBOX` / `INCONSISTENT`) after the P1 follow-up |
| Wire startup self-check in `main.py`; raise `SandboxConfigurationError` on `INCONSISTENT` | `src/nemoclaw_escapades/main.py` | ✅ `main.py:63-68` (and `agent/__main__.py:303-305` for the sub-agent) |
| Unit tests for YAML overlay, signal detection, `gen_config.py` / `gen_policy.py` resolvers | `tests/test_config.py`, `tests/test_gen_config.py`, `tests/test_gen_policy.py`, `tests/test_runtime.py` | ✅ Including `tests/test_config.py::TestSecretEnvOverrides::test_non_secret_env_vars_are_ignored` (regression guard that non-secret env vars stay no-ops) and resolver end-to-end tests in `TestFullyResolvedConfig` / `TestFullyResolvedPolicy` (PR #14 follow-up). |
| Update `.env.example`: document category-B keys, remove references to `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS` | `.env.example` | ✅ Rewritten in the P1 follow-up as secrets + category-B only (stale `LOG_LEVEL` / `AGENT_LOOP_*` / `NMB_URL` / `AUDIT_*` / `CODING_*` / `SKILLS_*` override blocks removed). |
| Create sub-agent `__main__` entrypoint | `agent/__main__.py` | ✅ (commit `c238f73`) |
| Create `AgentSetupBundle` dataclass | `agent/types.py` | ✅ `agent/types.py::AgentSetupBundle` |
| Create coding agent system prompt template | `prompts/coding_agent.md` | ✅ |
| End-to-end test: agent process starts, handles task, returns result | `tests/test_integration_coding_agent.py`, `tests/test_coding_agent_main.py` | ✅ subprocess-level: `tests/test_integration_coding_agent.py` spawns `python -m nemoclaw_escapades.agent --task ...` against a local OpenAI-format mock and asserts the assistant reply reaches stdout.  Function-level: `tests/test_coding_agent_main.py::TestCliMode` covers the same assembly path with a fake `AgentLoop`; `::TestNmbMode` smoke-tests the NMB wiring (receive-loop body itself is Phase 3). |

**Exit criteria (met as of PR #14 merge):**

- ✅ `config.py` contains no internal NVIDIA hostnames;
  `git grep nvidia.com src/` returns the single public SaaS URL
  (`jirasw.nvidia.com`) allowed by the carve-out.  Enforced by
  `tests/test_gen_config.py::TestNoHostnameLeak` +
  `tests/test_gen_policy.py::TestNoHostnameLeak`.
- ✅ `make gen-config` with an empty `.env` produces a resolved file
  whose category-B fields all hold fail-closed values
  (`tests/test_gen_config.py::TestResolverHappyPath`,
  `::TestFullyResolvedConfig`).
- ✅ `make gen-config` with a populated `.env` produces a resolved
  file whose `coding.git_clone_allowed_hosts` matches the operator's
  `.env` value (`tests/test_gen_config.py::TestResolverHappyPath`).
- ✅ `gen_config.py` refuses to write any key whose `.env` name
  matches the secret-suffix list (`*_TOKEN`, `*_AUTH`, `*_PASSWORD`,
  `*_KEY`) (`tests/test_gen_config.py::TestSecretGuard`).
- ✅ `AppConfig.load()` reads dataclass defaults → YAML overlay →
  secret env vars with the documented precedence
  (`tests/test_config.py::{TestYamlOverlay, TestYamlPrecedence,
  TestSecretEnvOverrides, TestSecretValidation,
  TestInferenceModelPropagation}`).
- ✅ `make run-local-sandbox` succeeds on a fresh sandbox; the
  startup log shows `classification: SANDBOX` with at least 4 of 6
  signals present.  Classifier logic is fully unit-tested
  (`tests/test_runtime.py::TestClassification`); the boot itself is
  manual smoke by design (a real OpenShell gateway is required — see
  §15.2 happy-path row).
- ✅ A broken sandbox (e.g. `OPENSHELL_SANDBOX` manually unset) fails
  fast with a structured `SandboxConfigurationError`
  (`tests/test_integration_coding_agent.py::test_agent_subprocess_inconsistent_runtime_fails_fast`,
  `tests/test_runtime.py::TestSandboxConfigurationError`).  There is
  no "local-dev fallback" to revert to — sandbox is the only
  supported runtime.
- ✅ Coding agent process starts and runs the M2a `AgentLoop` with
  the coding tool suite (file / search / bash / git — the latter
  includes the host-allowlisted `git_clone` and `git_checkout`) and
  the `SkillLoader`-discovered `skill` tool
  (`tests/test_coding_agent_main.py::TestCliMode::test_real_cli_mode_assembles_loop_and_runs`,
  `tests/test_integration_coding_agent.py::test_agent_subprocess_executes_file_tool_call`).
  NMB connect / close wiring also in place
  (`tests/test_coding_agent_main.py::TestNmbMode`).  The
  `task.assign` → `task.complete` protocol body itself is Phase 3 —
  see that phase's exit criteria.

**Phase 1 Follow-ups (PR #14 — merged).**  Addressed every unresolved
review thread on PR #13 in a single focused branch:

- Hoisted module-level constants (`APPROVAL_ACTION_*`, Slack error-rate
  windows) to their respective file-top constants blocks.
- Dropped the `LOCAL_DEV` runtime classification; `SANDBOX` /
  `INCONSISTENT` are the only two outputs of the classifier.
- Split `.env` (secrets only) from YAML (everything else).  `_apply_env_overrides`
  (~200 lines) shrunk to `_load_secrets_from_env` (~30 lines); the
  per-knob env-var surface for non-secret config (`LOG_LEVEL`,
  `AGENT_LOOP_*`, `INFERENCE_MODEL`, `NMB_URL`, etc.) is gone.
- Fail-fast on missing `inference.base_url` (default pinned in
  `config/defaults.yaml` — no silent in-code backfill).
- Dropped the spurious `INFERENCE_HUB_API_KEY` check — the sandbox uses
  `OPENAI_API_KEY` under the hood and the app never reads an API key
  directly (see [Appendix C §C.2](#c2-appconfigload-and-helper-functions)).
- Added docstring and commit-message verbosity caps to `CONTRIBUTING.md`.
- `make lint` and `make typecheck` are now clean on this branch.

### Phase 2 — `ToolSearch` meta-tool

Tool descriptions already consume 90%+ of the prompt on real runs,
drowning out user messages and leaving no headroom for the
delegation / finalization tools Phase 3 will add (`delegate_task`,
`present_work_to_user`, `push_and_create_pr`, `discard_work`,
`re_delegate`, `destroy_sandbox`).  Shipping the meta-tool before
growing the tool surface is the cheaper path, so it gets its own
phase ahead of delegation.

Phase 2 lands both the meta-tool **and** its wiring into the two
tool-registry factories — the meta-tool on its own doesn't reduce
prompt size unless the orchestrator and coding sub-agent actually
mark their non-core tools and register it.

| Task | Files | Status |
|------|-------|--------|
| `ToolSpec.is_core` flag; default `True` | `tools/registry.py` | ✅ |
| Extend `ToolRegistry` with surface state + search; default `tool_definitions()` to core-only | `tools/registry.py` | ✅ |
| `tool_search` meta-tool (search + surface side-effect) | `tools/tool_search.py` | ✅ |
| `AgentLoop`: per-round `tool_defs` refresh + `reset_tool_surface()` per `run()` | `agent/loop.py` | ✅ |
| Mark service tools `is_core=False`; register `tool_search` in orchestrator | `tools/{jira,gitlab,gerrit,confluence,slack_search,web_search}.py`, `tools/tool_registry_factory.py` | ✅ |
| Register `tool_search` in coding sub-agent (no-op until non-core tools land) | `tools/tool_registry_factory.py` | ✅ |
| Unit tests: surface state, search relevance, `tool_search` handler | `tests/test_tool_search.py` (`TestIsCoreDefault`, `TestRegistrySurface`, `TestRegistrySearch`, `TestToolSearchTool`, `TestNonCoreServiceToolsetsList`) | ✅ |
| Integration tests: factory flips service toolsets non-core; surface shrinks; `tool_search` surfaces service tools end-to-end | `tests/test_tool_search.py::{TestFullToolRegistryIntegration, TestCodingToolRegistryRegistersToolSearch}` | ✅ Registry-level only — subprocess-level turn deferred to Phase 3 |

**Exit criteria** (all covered by `tests/test_tool_search.py`):

- ✅ Non-core tools excluded from default `tool_definitions()`.
- ✅ `tool_search` returns matches and surfaces them for the next round.
- ✅ Orchestrator service tools default non-core; default prompt
  surface is strictly smaller (structural invariant: core <
  0.75 × full).  Concrete per-model token measurement deferred to
  Phase 3.
- ✅ Coding sub-agent registers `tool_search` for future-compat.
- 🟡 Full subprocess-level delegation turn (`tool_search("jira")` →
  `search_jira` → text reply) deferred to Phase 3 alongside the
  orchestrator-side `AgentLoop` driver.

### Phase 3 — Orchestrator delegation, NMB event loop, concurrency caps, and finalization

Splits into two sub-phases. 3a stops at the moment the orchestrator
can receive a `task.complete` it just logs; 3b adds finalization
plus the audit flush behind it.

#### Phase 3a — Protocol + delegation

| Task | Files | Status |
|------|-------|--------|
| Typed Pydantic payloads (§6.3) | `nmb/protocol.py` | ⏳ |
| Per-task `max_turns` + `model` plumbing into a one-shot `AgentLoopConfig` (§6.4, §6.5.2) | `agent/__main__.py` | ⏳ |
| Per-workflow `WorkspaceBaseline` pinning + sub-agent baseline-anchored diff (§6.6) | `orchestrator/delegation.py`, `agent/__main__.py` | ⏳ |
| `delegate_task` tool | `tools/delegation.py` | ⏳ |
| Spawn → workspace-seeding → `task.assign` flow | `orchestrator/delegation.py` | ⏳ |
| Per-agent `asyncio.Semaphore` + `max_spawn_depth` caps (§8.1) | `orchestrator/delegation.py` | ⏳ |
| NMB event loop (`task.complete` / `task.progress` / `task.error` dispatch to per-workflow handlers) | `orchestrator/orchestrator.py` | ⏳ |
| Audit-record requested `model` per delegation (§6.5.2) | `agent/audit/db.py`, `orchestrator/delegation.py` | ⏳ |
| Tests | `tests/test_protocol.py`, `tests/test_workspace_baseline.py`, `tests/test_delegation.py`, `tests/integration/test_delegation.py` | ⏳ |

**Exit criteria:** orchestrator spawns a sub-agent over NMB; the
sub-agent runs `AgentLoop` with the assigned `max_turns` and
`model` and returns a baseline-anchored `task.complete` that
validates against the typed protocol.  Concurrency and spawn-depth
caps enforced.  Finalization not wired yet — orchestrator just
logs the typed payload.

#### Phase 3b — Finalization + audit

| Task | Files | Status |
|------|-------|--------|
| Baseline verification + `BaselineDriftError` (§6.6.3) | `orchestrator/finalization.py` | ⏳ |
| Finalization tools (`present_work_to_user`, `push_and_create_pr`, `push_branch`, `discard_work`, `re_delegate`, `destroy_sandbox`) | `tools/finalization.py` | ⏳ |
| `_finalize_workflow` (second `AgentLoop` with finalization registry) | `orchestrator/orchestrator.py` | ⏳ |
| `re_delegate` reuses the workflow's pinned baseline (§6.6.2 case C) | `tools/finalization.py`, `orchestrator/delegation.py` | ⏳ |
| Slack rendering for `present_work_to_user` with action buttons (§7.2) | `connectors/slack/finalization.py` | ⏳ |
| `AuditBuffer` (NMB-batched flush + JSONL fallback) (§13) | `agent/audit_buffer.py`, `agent/__main__.py` | ⏳ |
| Orchestrator-side `audit.flush` ingest with `workflow_id` / `parent_sandbox_id` / `agent_role` attribution | `orchestrator/orchestrator.py`, `agent/audit/db.py` | ⏳ |
| Orchestrator-side JSONL-fallback ingest on `task.complete` | `orchestrator/finalization.py` | ⏳ |
| Tests | `tests/test_finalization.py`, `tests/test_audit_buffer.py`, `tests/integration/test_finalization.py`, `tests/integration/test_audit_flush.py` | ⏳ |

**Exit criteria:** every `task.complete` runs through finalization;
drift is caught before push; Slack buttons fire the right
finalization tool; every sub-agent tool call lands in the central
audit DB exactly once with the right attribution.  The two
sub-phases compose to deliver the §7 finalization flow and the
§13 audit story end-to-end.

### Phase 4 — At-least-once NMB delivery

| Task | Files | Status |
|------|-------|--------|
| `reliable_send` (atomic write to `pending/<id>.json` → send → `ack` deletes the file) (§9) | `nmb/reliable_send.py` | ⏳ |
| Per-sandbox `pending/` directory; tracked in audit DB so the orchestrator can see in-flight messages on connect | `nmb/reliable_send.py`, `agent/audit/db.py` | ⏳ |
| Wire `reliable_send` into `task.complete` and `audit.flush` send sites (Phase 3b ships them as plain sends) | `agent/__main__.py`, `agent/audit_buffer.py` | ⏳ |
| Crash-recovery replay on startup (orchestrator scans every sandbox's `pending/` and re-emits unacked messages) | `nmb/broker.py`, `orchestrator/orchestrator.py` | ⏳ |
| Idempotency on the orchestrator side: `task.complete` and `audit.flush` ingest dedup by message id so replay doesn't double-finalize or double-audit | `orchestrator/orchestrator.py`, `agent/audit/db.py` | ⏳ |
| Tests: persist-then-send roundtrip; ack deletes pending; broker-kill mid-flight + restart replays exactly once | `tests/test_reliable_send.py`, `tests/integration/test_reliable_send.py` | ⏳ |

**Exit criteria:** `task.complete` and `audit.flush` survive a
broker or sandbox crash and replay exactly once on restart.
Finalization and audit ingest are idempotent under replay.

### Phase 5 — Basic operational cron

| Task | Files | Status |
|------|-------|--------|
| `CronWorker` skeleton — `asyncio` scheduler running on a 60-second tick, dispatching jobs as `asyncio.Task`s (§11.2) | `orchestrator/cron.py` | ⏳ |
| Markdown state file at `data/cron_state.md` — load on startup to skip recent runs, write after each job | `orchestrator/cron.py` | ⏳ |
| Sandbox TTL watchdog job (5 min): kill sub-agent processes whose TTL has expired and clean up their workspace | `orchestrator/cron.py`, `orchestrator/delegation.py` | ⏳ |
| Stale-session cleanup job (30 min): archive sessions with no activity for 24 h | `orchestrator/cron.py` | ⏳ |
| Health-check job (10 min): verify NMB broker, inference backend, Slack connectivity; surface failures via structured log + Slack DM | `orchestrator/cron.py`, `connectors/slack/health.py` | ⏳ |
| Audit-DB maintenance job (daily): SQLite `VACUUM` + checkpoint + rotate old entries | `orchestrator/cron.py`, `agent/audit/db.py` | ⏳ |
| Tests: scheduler tick + job-due logic; Markdown state round-trip; missed-while-restarting jobs run on next tick; each operational job against fixtures | `tests/test_cron.py` | ⏳ |

**Exit criteria:** the four operational cron jobs run on schedule,
state survives orchestrator restart without duplicate runs, and
each job's failure mode is observable in the log.

### Phase 6 — Polish, hardening, and gaps document

| Task | Files | Status |
|------|-------|--------|
| Progress relaying to Slack | `orchestrator/delegation.py` | ⏳ |
| File tool edge case hardening (symlinks, binary files, encoding) | `tools/files.py` | ⏳ |
| Skill-body-size startup log — observability for §17 Q5 | `agent/skill_loader.py` | ⏳ |
| Lazy `load_skill` capability — sub-agent only; metadata in prompt, body on demand (SDK §11.1.1) | `tools/load_skill.py` (new), `agent/skill_loader.py`, `tools/tool_registry_factory.py`, `prompts/coding_agent.md`, `tests/test_load_skill.py` (new) | ⏳ |
| Lazy-`load_skill` ↔ `tool_search` design sketch (resolves §17 Q6) | `docs/deep_dives/lazy_loading_unification.md` (new) | ⏳ |
| `Capability`-style behaviour bundles (SDK §11.3.1) — skip unless M3 queues 3+ new bundles | `src/nemoclaw_escapades/agent/capabilities/` (new), `tools/tool_registry_factory.py` | ⏳ (gated) |
| `AgentSpec` dataclass (SDK §10 mapping) — clarifies orch/sub-agent boundary for M3 | `src/nemoclaw_escapades/agent/spec.py` (new), `agent/__main__.py`, `orchestrator/delegation.py` | ⏳ (gated) |
| Option A multi-endpoint provider switching (§6.5.1) — only if a per-task model requirement surfaces before M3 | `policies/orchestrator.yaml`, `config/defaults.yaml`, `src/nemoclaw_escapades/config.py`, `tools/delegation.py`, `tests/integration/test_delegation.py` | ⏳ (gated) |
| `docs/DEFERRED.md` — features punted out of M2b (`Manifest`, snapshots, `Memory`, leftover Tier 3, operational per-task model) | `docs/DEFERRED.md` (new) | ⏳ |

**Exit criteria:** production-quality delegation with cleanup,
progress, and edge-case hardening; lazy `load_skill` lands; §17 Q6
design sketch closes; per-task `model` selection stays app-layer-
only unless Option A is pulled in; `Capability` / `AgentSpec` land
only if the M3 design review pulls them in.

---

## 15  Testing Plan

### 16.1 Unit Tests

| Test | What it verifies | Status |
|------|-----------------|--------|
| Config YAML overlay | Missing file → dataclass defaults; partial file → unspecified keys keep defaults | ✅ `tests/test_config.py::TestYamlOverlay` |
| Config secret-env precedence | Only secret env vars overlay YAML; non-secret env vars are deliberate no-ops | ✅ `tests/test_config.py::TestSecretEnvOverrides` (including `test_non_secret_env_vars_are_ignored`) |
| Config unknown keys | Forward-compat: unknown top-level keys log a warning but don't raise | ✅ `tests/test_config.py::TestYamlOverlay::test_unknown_top_level_key_logs_warning_but_loads`, `::test_unknown_field_in_known_section_logs_warning` |
| Config required-field validation | Missing Slack tokens / `inference.base_url` → clear `ValueError`; `INFERENCE_HUB_API_KEY` deliberately *not* required | ✅ `tests/test_config.py::TestSecretValidation` (including `test_missing_inference_api_key_is_accepted`) |
| `gen_config.py` — empty `.env` | Resolved file byte-equals `defaults.yaml` (all category-B fields fail-closed) | ✅ `tests/test_gen_config.py::TestResolverHappyPath` |
| `gen_config.py` — populated `.env` | `coding.git_clone_allowed_hosts` in the resolved file matches the `.env` value | ✅ `tests/test_gen_config.py::TestResolverHappyPath` |
| `gen_config.py` — unknown key | Unrecognised `.env` keys are ignored (never appear in resolved output) | ✅ `tests/test_gen_config.py::TestResolverHappyPath` |
| `gen_config.py` — secret guard | An `.env` key matching `*_TOKEN`/`*_AUTH`/`*_PASSWORD`/`*_KEY` in the category-B allowlist → resolver fails with a clear error | ✅ `tests/test_gen_config.py::TestSecretGuard` |
| `gen_config.py` / `gen_policy.py` — end-to-end | Resolver against a synthetic `.env` produces a fully-populated YAML; no secret leakage; round-trips through `AppConfig.load` | ✅ `tests/test_gen_config.py::TestFullyResolvedConfig`, `tests/test_gen_policy.py::TestFullyResolvedPolicy` |
| No hostname leak in public source | `git grep nvidia.com src/ -- ':!*.md'` returns zero matches outside of public SaaS URLs (`jirasw.nvidia.com`, `nvidia.atlassian.net`) | ✅ `tests/test_gen_config.py::TestNoHostnameLeak` (config layer), `tests/test_gen_policy.py::TestNoHostnameLeak` (policy layer) — both paired so any regression on either file fails CI |
| Sandbox detection — zero signals | No sandbox signals → `INCONSISTENT` with "no sandbox signals present" cause | ✅ `tests/test_runtime.py::TestClassification::test_all_signals_absent_is_inconsistent` |
| Sandbox detection — SANDBOX | ≥ 4 of 6 signals present → classification `SANDBOX` | ✅ `tests/test_runtime.py::TestClassification::test_all_signals_present_is_sandbox`, `::test_threshold_met_with_one_signal_flaky` |
| Sandbox detection — INCONSISTENT | Partial or asymmetric signal mix → `INCONSISTENT` + structured `likely_cause` | ✅ `tests/test_runtime.py::TestClassification::test_env_signals_without_path_signals_is_inconsistent`, `::test_path_signals_without_env_signals_is_inconsistent`, `::test_two_env_signals_only_is_inconsistent` |
| Startup self-check | `INCONSISTENT` classification raises `SandboxConfigurationError` before config load | ✅ `tests/test_runtime.py::TestSandboxConfigurationError` |
| AppConfig prompting-field sync | `orchestrator.model` / `temperature` / `max_tokens` (set via YAML) propagate to `config.agent_loop` unless YAML pins the latter; `agent_loop.model` follows `inference.model` not `orchestrator.model` | ✅ `tests/test_config.py::TestInferenceModelPropagation` |
| AgentLoop + NMB config sections | YAML `agent_loop:` / `nmb:` populate the matching `AppConfig` fields | ✅ `tests/test_config.py::TestYamlOverlay::test_agent_loop_section_populates_config`, `TestYamlPrecedence::test_nmb_section_populates_config`, `::test_agent_loop_section_populates_runtime_knobs` |
| Sub-agent workspace isolation | Each sub-agent invocation lands in a distinct `<base>/agent-<hex>` subdirectory so concurrent runs can't clobber each other's scratchpad / notes | ✅ `tests/test_coding_agent_main.py::TestCliMode::test_cli_mode_per_agent_subdirectory_is_created` |
| Sub-agent tool surface (enforcement-by-construction) | Sub-agent registry excludes `git_commit`; orchestrator retains it for finalisation | ✅ `tests/test_git_tools.py::TestGitToolRegistration::test_include_commit_false_omits_git_commit`, `tests/test_file_tools.py::TestCodingToolRegistry::test_factory_creates_sub_agent_tool_surface` |
| Delegation concurrency cap | Semaphore blocks at `max_concurrent_tasks`; unblocks on completion | ⏳ Pending (Phase 3a) |
| Delegation spawn depth cap | `max_spawn_depth` exceeded → delegation rejected with error | ⏳ Pending (Phase 3a) |
| NMB reliable send | Message persisted to disk before send; deleted after ack | ⏳ Pending (Phase 4) |
| NMB crash recovery | Pending messages replayed on broker startup | ⏳ Pending (Phase 4) |
| `ToolSearch` meta-tool | Returns correct tools for keyword queries; non-core excluded from prompt | ✅ `tests/test_tool_search.py` — `TestRegistrySearch` (scoring), `TestRegistrySurface::test_default_tool_definitions_excludes_non_core`, `TestToolSearchTool::test_tool_search_returns_matches_and_surfaces_them`, `TestFullToolRegistryIntegration::test_default_prompt_surface_shrinks` |
| Finalization tools | Each tool produces correct output with mock sandbox/git | ⏳ Pending (Phase 3b) |
| Cron scheduling | Jobs fire at correct intervals; missed jobs caught up | ⏳ Pending (Phase 5) |

### 16.2 Integration Tests

| Test | What it verifies | Status |
|------|-----------------|--------|
| Sandbox boot — happy path | `make run-local-sandbox` → log shows `classification: SANDBOX` with all 6 signals present | 🟡 Manual smoke only — the positive path needs a real OpenShell sandbox so `/sandbox`, `/app/src`, and `inference.local` DNS resolve.  Automation would require mocking OpenShell itself, out of scope for unit / integration tests.  The runtime classifier logic is fully covered at the unit level (`tests/test_runtime.py::TestClassification`); operators verify the end-to-end wiring with `make run-local-sandbox`. |
| Sandbox boot — broken env | Manually unset `OPENSHELL_SANDBOX` in a test image → self-check fails with `INCONSISTENT` and the process exits nonzero before Slack connects | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_inconsistent_runtime_fails_fast` — spawns `python -m nemoclaw_escapades.agent` with an env mix the multi-signal detector classifies `INCONSISTENT`; asserts non-zero exit and `SandboxConfigurationError` + `refusing to start` on stderr, before any config load or I/O. |
| Config YAML — deployment override | Mount a custom `config.yaml` over the default → `coding.workspace_root` picks up the override without a rebuild | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_honours_yaml_deployment_override` — custom YAML via `NEMOCLAW_CONFIG_PATH` directs the sub-agent's workspace root without a rebuild; asserts the per-agent subdir lands under the YAML-supplied path. |
| Sub-agent NMB lifecycle | Connect, `sandbox.ready`, `task.assign`, `task.complete` | 🟡 Partial — (a) NMB wire-level transport covered by `tests/integration/test_lifecycle.py::{TestSandboxConnect,TestSandboxDisconnect,TestSandboxReconnect}`; (b) sub-agent-side connect / close wiring (reads broker URL + sandbox id from `config.nmb`, calls `connect_with_retry`, closes on shutdown) covered by `tests/test_coding_agent_main.py::TestNmbMode`; (c) `task.assign` / `task.complete` protocol body awaits Phase 3 |
| Coding agent end-to-end | Agent receives task, uses file tools, returns diff | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_executes_file_tool_call` — subprocess + stateful OpenAI-format mock serves a `write_file` tool_call then a terminating reply; assertions: file lands on disk inside the per-agent workspace, final reply reaches stdout. |
| Orchestrator delegation full flow | Spawn → assign → complete → finalize → cleanup | ⏳ Pending (spawn→complete lands in 3a; finalize closes the loop in 3b) |
| Delegation concurrency enforcement | Third delegation waits when `max_concurrent_tasks=2` | ⏳ Pending (Phase 3a) |
| Model-driven finalization | `task.complete` → model calls `present_work_to_user` → user clicks [Push & PR] | ⏳ Pending (Phase 3b) |
| Iteration flow | User feedback → `re_delegate` → same agent → updated result | ⏳ Pending (Phase 3b) |
| Concurrent finalization | Two sub-agents complete simultaneously; both finalize concurrently | ⏳ Pending (Phase 3b) |
| NMB at-least-once delivery | Kill broker after persist, restart, verify replay | ⏳ Pending (Phase 4) |
| Audit NMB flush + fallback | Tool calls arrive via NMB batch and/or JSONL fallback | ⏳ Pending (Phase 3b) |
| TTL watchdog | Watchdog fires → sub-agent process killed → workspace cleaned | ⏳ Pending (Phase 5) |

### 16.3 Safety Tests

| Test | What it verifies | Status |
|------|-----------------|--------|
| Tool surface enforcement | Sub-agent cannot use tools not in its `tool_surface` | 🟡 Partial — Phase 1 enforces by *construction*: `create_coding_tool_registry` deliberately omits `git_commit` (orchestrator-only, per §7.1), so the sub-agent's `ToolRegistry` has no entry the model could invoke.  Covered by `tests/test_git_tools.py::TestGitToolRegistration::test_include_commit_false_omits_git_commit` and `tests/test_file_tools.py::TestCodingToolRegistry::test_factory_creates_sub_agent_tool_surface`.  Phase 3 will add a runtime allow-list check for the broader "tool_surface"-as-policy story (e.g. orchestrator-granted per-task tool restrictions). |
| Workspace path sandboxing | File tools cannot access outside `/sandbox/workspace/` | ✅ `tests/test_file_tools.py::TestSafeResolve`, `TestReadFile::test_read_path_escape_blocked`, `::test_read_absolute_path_blocked`, `TestWriteFile::test_write_path_escape_blocked` (M2a) |
| No recursive delegation | Coding agent cannot spawn sub-agents | ⏳ Pending (Phase 3a) |
| Notes file size cap | Enforced at the orchestrator when reading back the notes file — large writes are truncated before being fed into finalization context | ⏳ Pending (Phase 3b) |
| Diff baseline integrity | (a) `task.complete` carrying a `workspace_baseline.base_sha` that doesn't match the assigned one fails finalisation with `BaselineDriftError`; (b) two sub-agents in the same workflow both observe the same `base_sha` after independent spawn; (c) sub-agent's reported `diff` matches `git diff <base_sha>..HEAD` re-derived by the orchestrator; (d) `re_delegate`'s second `task.assign` carries the same baseline as the first (§6.6) | ⏳ Pending: (b) shared-baseline parallel spawn in Phase 3a; (a), (c), (d) in Phase 3b |

---

## 16  Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Sandbox spawn latency (5-15s) | Adds to end-to-end time | Accept for M2b; warm pool deferred |
| NMB broker unavailability | Cannot communicate with sub-agent | Fail-open: detect broker down, surface error to user, retry on recovery |
| Sub-agent infinite loop | Consumes tokens without progress | `max_tool_rounds` + TTL watchdog |
| Large workspace clones | Slow setup, high storage | Shallow clones; NMB context for small tasks |
| Arbitrary `git_clone` targets | Data exfiltration / supply-chain risk via sub-agent clones | Fail-closed host allowlist on the `git_clone` tool via `GIT_CLONE_ALLOWED_HOSTS` (M2a §4).  Empty allowlist disables the tool entirely; only explicitly listed hosts are reachable. |
| Concurrent notes-file clobber | Two sub-agents sharing a workspace overwrite each other's working memory | `scratchpad` skill mandates filenames of the form `notes-<task-slug>-<agent-id>.md` with an `Owner:` header; the `Agent ID` is seeded by the runtime-metadata prompt layer so every agent gets a unique id.  Bare `notes.md` is explicitly forbidden by the skill. |
| Credential leakage via notes file | Agent writes secrets | Notes-file sanitisation before return to orchestrator. *(M2b: same sandbox, so process-level isolation only. M3 adds kernel-level sandbox isolation.)* |
| Silent sandbox-detection drift | App starts inside a broken sandbox, fails later in non-obvious ways (e.g. inference 403, audit written to wrong path) | Multi-signal `detect_runtime_environment()` + fail-fast `SandboxConfigurationError` on `INCONSISTENT`. There is no "local-dev fallback" to revert to — sandbox is the only supported runtime, so below-threshold signal counts refuse startup. Signals logged at every startup so drift is visible in the structured log. (§5.4) |
| Non-secret config drift across sandboxes | Two deployments from the same image need different log levels / workspace roots / feature flags, but `openshell sandbox create` has no `--env` flag | File-based `/app/config.yaml` overlay with per-field env-var overrides (§5.3). Secrets stay on providers; non-secrets get a single, auditable configuration surface. |
| Internal infrastructure leaked via public source | Category-B values (internal hostnames, host allowlists, infra URLs) inlined as `_SANDBOX_*` constants in `config.py` ship with every public `git push` | Public `config/defaults.yaml` holds only fail-closed placeholders for category-B fields. Real values live in gitignored `.env` and are merged into a gitignored `config/orchestrator.resolved.yaml` at build time by `scripts/gen_config.py` (same pattern as `gen_policy.py`). (§5.3.2 / §5.3.3) |
| Accidental secret exposure via config YAML | An operator could copy a token into `defaults.yaml` or `gen_config.py` could inadvertently route a `.env` secret into the resolved YAML | `gen_config.py` maintains an explicit category-B allowlist of keys it will honour; any `.env` key outside the allowlist is ignored. Any `.env` key matching the secret suffix list (`*_TOKEN`, `*_AUTH`, `*_PASSWORD`, `*_KEY`) included in the allowlist is a hard error. Loader-side guard rejects any YAML key that maps to a known secret field. (§5.3.4) |
| Operators expect per-task `model` selection to work in M2b | The wire field exists (§6.3, §6.5) but is operationally a no-op until Option A polish (M2b Phase 6) or Option D (M3, gated on §17 Q10) lands; an operator setting `task.model="haiku"` and watching their Opus bill not change would reasonably file a bug | Documented as a deliberate scope choice in §6.5 / §6.5.1; `delegate_task` audit-records the requested model so operators can see the field is being respected at the protocol layer; Option A is the documented unblock path if a real per-task requirement shows up before M3. |
| Diff is "against the workspace's starting state" but the starting state isn't tracked | Without a pinned baseline, `TaskCompletePayload.diff` is unverifiable, parallel sub-agents on the same repo could end up rooted at different commits if upstream advanced between spawns, and `re_delegate` can't reproduce the original baseline for iteration #2 — silent correctness failure at finalisation, not a crash | `WorkspaceBaseline` Pydantic dataclass on both `TaskAssignPayload` and `TaskCompletePayload` (§6.3); orchestrator pins one baseline per workflow at delegation time and reuses it across all sub-agents in that workflow (§6.6.2 cases A and C); sub-agent computes `diff` as `git diff <base_sha>..HEAD` and echoes the baseline back; orchestrator verifies the echo and (same-sandbox case) re-derives the diff to cross-check (§6.6.3). Phase 3 unit + integration tests cover both the parallel-spawn shared-baseline case and the mid-task drift case. |

---

## 17  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should sub-agent system prompts be generated by the orchestrator or stored as static templates? | Start with templates, evolve to generation. |
| 2 | How should the orchestrator decide when to delegate vs. handle itself? | Start with explicit user intent; evolve to model-driven routing. |
| 3 | Git finalization: orchestrator pull+push (Strategy A) or sub-agent push directly (Strategy B)? | Strategy A maximizes isolation; Strategy B is simpler. See [original §10.4](design_m2.md#104--git-operations-for-finalization). |
| 4 | Who picks the per-task `max_turns` value (§6.4)? | Phase 3 hardcodes a small per-shape lookup table (one-shot=3, routine=15, refactor=30) on the orchestrator side. Model-driven sizing — let the orchestrator's planning model classify the task and pick the cap — is plausible but unproven; revisit after Phase 6 smoke testing. |
| 5 | What's the trigger to ship lazy `load_skill` *earlier* than Phase 6? | Resolved direction: lazy `load_skill` is now a Phase 6 task (§14), not a "land if a skill ever crosses N lines" deferral.  The remaining open question is whether to backport it into Phase 3 if the first M3-targeted skill (review-agent rubric, the larger debugging walkthrough) lands during Phase 3 stabilisation.  Trigger if so: any skill body ≥ 500 lines or any skill with a `references/` / `assets/` subtree.  Phase 6's startup-log row makes the trigger observable. |
| 6 | Should `tool_search` and lazy `load_skill` share infrastructure? | Resolved: a one-page design sketch is part of Phase 6 (§14) — it lands alongside the lazy-`load_skill` capability.  Both surfaces are metadata-first + load-on-demand but operate on different data planes (`tool_search` over `ToolSpec` JSON, `load_skill` over a skill directory).  Sketch decides whether they share `is_core` plumbing, share a search index, or stay independent.  Until that sketch closes, treat as decided-by-Phase-6, not open. |
| 7 | Are typed `task.complete` payloads (§6.3) too rigid for future agent roles? | A review agent (M3) returns review verdicts, not diffs. M3 will introduce `ReviewCompletePayload` as a sibling — same `BaseModel` parent, different fields — rather than overloading `TaskCompletePayload`. The protocol-as-typed-payload pattern from M2b extends naturally; the constraint is that every new role gets its own model, not a unioned mega-payload. |
| 8 | Who picks the per-task `model` value (§6.5)? | Same shape as Q4 but deferred until operational selection lands (Option A polish in M2b Phase 6, or M3). Once that happens, the same per-shape lookup-table-vs-model-driven-routing decision applies as for `max_turns`; likely to land together with Q4's resolution since the two decisions are correlated (an Opus task is almost always also a high-`max_turns` task). |
| 9 | Should the per-task `model` choice be auditable as a separate field? | Provisional answer: yes — one nullable `requested_model` column on the workflow audit row, populated from `TaskAssignPayload.model` regardless of whether the L7 proxy actually honoured it. Reasons to spend the column even when M2b can't act on it: (a) the audit format stays stable across "scaffolding-only" → "operational" so neither Option A nor Option D requires a schema migration; (b) it's cheap. A separate `realised_model` column only becomes interesting once selection is operationally real; defer that decision. |
| 10 | Three things to settle for Option D (§6.5.1) — two mechanical, one principled: (a) does `openshell inference set` accept `--sandbox <name>` to scope routing per sandbox? (b) can the orchestrator reach the gateway control plane from inside its own sandbox? (c) *should* the orchestrator's sandbox modify gateway state at all, or should that operation stay on the operator's host? | (a) Open — documented routing surface is gateway-global today. (b) Mechanically open — orchestrator image lacks the `openshell` binary, the policy has no gateway-API entry, mTLS client credentials aren't provisioned. M3 has to solve (b) for `SandboxClient.create()` regardless. (c) Provisional answer in §5.1.1: sub-agents *never* should reach the gateway; the orchestrator *can*, but only behind narrow scoping + an approval gate, and Option A keeps the operation on the operator's host where the trust boundary is most natural. The principled answer biases the choice toward Option A even after (a) and (b) resolve favourably — Option D becomes worth considering only when its operational benefit (per-task selection without a per-model URL table) materially exceeds the trust-boundary cost. Track OpenShell release notes for `--sandbox`; revisit (a)+(b) when M3 implementation kicks off; the principled side (c) is settled in §5.1.1 unless we discover concrete deployment evidence that the operational benefit justifies relaxing it. |

---

## Appendix A — `git_clone` Authentication: Option Details

Companion to §4.4 — the design constraints, per-option dataflow,
code, and trade-offs that informed the Option B recommendation.
§4.4 is the design-level view (problem, options summary, phasing);
this appendix is the implementation-level evidence behind it.

### A.0 Design constraints

**Why `$GITLAB_TOKEN` doesn't reach git.**  M2a ships `git_clone`
as part of the coding tool suite ([M2a §4 tools](design_m2a.md#4--coding-agent-tools)),
scoped to a fail-closed host allowlist.  After this milestone's
sandbox-image and policy delta (`git` binary installed,
`/usr/bin/git` whitelisted for the `github` / `gitlab` / `gerrit`
network policies), *unauthenticated* HTTPS clones work — the
obvious case is a public `github.com` repo.  Private hosts reject
with 401: we already hold the right credentials (`GITLAB_TOKEN`,
`GERRIT_USERNAME`, `GERRIT_HTTP_PASSWORD` are OpenShell providers,
injected as placeholder env vars like
`GITLAB_TOKEN=openshell:resolve:env:GITLAB_TOKEN` that the L7 proxy
substitutes for real values in the `Authorization` header at TLS
termination), but git doesn't read them.  Our REST tools consume
those placeholders for free because *we* wrote the code that sets
`Authorization: Bearer $GITLAB_TOKEN` on every request
(`GitLabClient`, `JiraClient`, `GerritClient`, `ConfluenceClient`).

Git's credential sources are URL userinfo, credential helpers,
`~/.git-credentials` / `.netrc`, and interactive prompts — none of
which natively read an env var.  The placeholder sits in
`$GITLAB_TOKEN`; git just doesn't know to look.  The four options
in §4.4 are three different *bridges* from "placeholder in env" →
"`Authorization` header the proxy can substitute" (URL embedding
A, credential helper B, SSH sidestep C), plus a fourth that avoids
the clone entirely (host seed D).  Every option except C composes
with the existing provider flow; C is the only one that introduces
a parallel credential channel.

**Two properties matter regardless of which bridge we pick:**

1. **No secret leakage into the filesystem image or the cloned
   repo's `.git/config`.**  Tokens baked into files survive
   container rebuilds, operator `git push`es, and audit-log
   captures.  The same URL that holds a harmless placeholder
   inside the sandbox holds the *real* token on the operator's
   host (where `$GITLAB_TOKEN` is the unwrapped value from
   `.env`, read by host-side tooling like `scripts/test_auth.py`),
   so the leakage is not purely theoretical.
2. **Reuse the existing provider / placeholder system where
   possible.**  We already hold `GITLAB_TOKEN` /
   `GERRIT_USERNAME` / `GERRIT_HTTP_PASSWORD` as OpenShell
   providers.  Adding a parallel credential channel for git — SSH
   keys mounted into the sandbox, a new secret store, a parallel
   rotation story — doubles the surface.  Options A and B satisfy
   this; C does not; D sidesteps the question.

§A.1–A.4 score each option against these two properties.

### A.1 Option A — URL-embedded token

Embed the token in the clone URL:

```
https://oauth2:${GITLAB_TOKEN}@gitlab.example.com/acme/demo-repo
```

**Pros.**  Zero new infrastructure — works today with the existing
`GITLAB_TOKEN` provider.  Operator documentation only; no image
changes.

**Cons.**  Token leaks into:

- The sandbox's process listing (`ps -ef` shows the full
  `git clone` command line).
- The cloned repo's `.git/config` (git stores the auth URL
  verbatim as the remote `origin.url`).
- Any error message or log line that echoes the URL.

Leaked tokens are cheap to rotate, but the workflow is error-prone:
every future `git pull` / `git push` inside the clone re-uses the
embedded credential, so the exposure persists until the operator
manually rewrites the remote URL.

**Use case.**  A short-lived workaround.  `.env.example` can document
it for developers who need to clone a specific private repo today;
a `scratchpad`-style skill can surface it when the agent hits a 401.
Not the long-term answer.

### A.2 Option B — Git credential helper (recommended long-term)

Option B uses git's built-in [credential-helper protocol](https://git-scm.com/docs/gitcredentials)
to hand the provider-injected placeholder to git.  Git then lands it
in the `Authorization` header, where the L7 proxy can substitute the
real value — *exactly the same substitution path* the REST tools
use, just with a custom helper replacing the
`Authorization: Bearer $TOKEN` line our Python clients set
explicitly.

**End-to-end flow.**  At image-build time, a short helper script
(`git-credential-from-env`) lands in `/usr/local/bin` and
`/etc/gitconfig` is configured to invoke it for every private host.
At runtime:

1. Agent runs `git clone https://<host>/…`.
2. git reads `/etc/gitconfig`, finds
   `credential.https://<host>.helper = !/usr/local/bin/git-credential-from-env`,
   execs the helper, and writes `host=<host>\n` (plus other request
   fields) to the helper's stdin.
3. The helper reads `$GITLAB_TOKEN` / `$GERRIT_*` from its
   environment — which inside the sandbox hold the OpenShell
   placeholder strings (e.g. `openshell:resolve:env:GITLAB_TOKEN`),
   **not** the real secrets — and prints
   `username=…\npassword=<placeholder>\n` on stdout.
4. git composes the HTTPS request with
   `Authorization: Basic base64("<user>:<placeholder>")` and sends
   it through `HTTPS_PROXY`.
5. The L7 proxy terminates TLS, decodes the Basic header, matches
   the placeholder, substitutes the real token, re-encodes, and
   forwards the request to the origin.

```mermaid
sequenceDiagram
    actor Agent as Coding agent
    box Sandbox
        participant Git as git
        participant Helper as git-credential-from-env
    end
    participant Proxy as OpenShell L7 proxy
    participant Host as Private host (gitlab.example.com)

    Agent->>Git: git clone https://gitlab.example.com/...
    Note over Git: ① reads /etc/gitconfig<br/>credential.gitlab.example.com.helper<br/>= !/usr/local/bin/git-credential-from-env
    Git->>Helper: ② exec, writes "host=gitlab.example.com" to stdin
    Note over Helper: ③ reads $GITLAB_TOKEN<br/>= "openshell:resolve:env:GITLAB_TOKEN"<br/>(placeholder — never the real PAT)
    Helper-->>Git: stdout: username=oauth2,<br/>password=(placeholder)
    Git->>Proxy: ④ HTTPS via HTTPS_PROXY<br/>Authorization: Basic b64("oauth2:(placeholder)")
    Note over Proxy: ⑤ TLS-terminate, decode Basic,<br/>match (placeholder),<br/>swap in real PAT
    Proxy->>Host: ⑥ HTTPS with real PAT in Authorization header
    Host-->>Proxy: 200 OK
    Proxy-->>Git: clone data streams back
```

The real PAT only ever lives on the OpenShell host (in the provider
vault) and briefly inside the L7 proxy during substitution.  The
sandbox — the helper, git, the Authorization header git composes —
only ever handles the placeholder string.

**The helper script** (implements steps ②–③):

```bash
# /usr/local/bin/git-credential-from-env  (bundled in the image)
#!/bin/sh
# Git invokes "git-credential-<name> get" and reads username / password
# lines on stdout.  Host comes in on stdin as "host=<name>".
[ "$1" = "get" ] || exit 0
while IFS='=' read -r k v; do [ "$k" = host ] && host="$v"; done
case "$host" in
  gitlab.example.com)
    printf 'username=oauth2\npassword=%s\n' "$GITLAB_TOKEN" ;;
  gerrit.example.com)
    printf 'username=%s\npassword=%s\n' "$GERRIT_USERNAME" "$GERRIT_HTTP_PASSWORD" ;;
esac
```

**Image-build registration** (implements step ①):

```dockerfile
RUN git config --system \
      credential.https://gitlab.example.com.helper \
      '!/usr/local/bin/git-credential-from-env' \
 && git config --system \
      credential.https://gerrit.example.com.helper \
      '!/usr/local/bin/git-credential-from-env'
```

**Pros.**

- Token never lands in URLs, process listings, or repo configs —
  git asks the helper on demand, the helper reads `os.environ[…]`,
  the value exists in memory for one invocation.
- Composes cleanly with OpenShell's provider flow: the
  proxy-injected `$GITLAB_TOKEN` is already in the sandbox env;
  the helper just reads it there.
- Handles `git pull` / `git push` inside the cloned repo
  automatically once installed — no per-invocation plumbing from
  the agent.
- Adding a new private host is a two-line change (one `case` arm
  in the helper + one `git config` line in the Dockerfile).

**Cons.**

- Requires a new executable in the image + a `RUN git config --system`
  block.  The helper path must be on the policy's `binaries`
  allowlist for the git-host network policies (git execs the helper
  to resolve credentials *before* it starts the HTTPS request).
- Secret-exposure caveat: `printf` writes the password to the
  helper's stdout, which lives in pipe buffers and could in
  principle be captured.  This is strictly better than the
  URL-embedding case — the exposure is process-local, in-memory,
  and short-lived — but it's not zero.  Rotating providers still
  rotates all downstream usage cleanly.

**Use case.**  The recommended long-term path for any host whose
credentials already flow through an OpenShell provider.  Phase 3
implementation target.

### A.3 Option C — SSH keys

Install `openssh-client` in the image, mount an SSH private key
plus a `known_hosts` file into `/sandbox/.ssh`, allow SSH egress to
the git hosts (or leave it unmediated by the L7 proxy, since
OpenShell doesn't do protocol-level SSH interception).

**Pros.**  Matches most developers' intuition for "how do I clone a
private repo."  SSH's authentication is independent of the HTTPS
interception path, so TLS-termination quirks don't affect it.

**Cons.**

- SSH keys are coarser-grained than HTTPS tokens — most deployments
  don't scope SSH keys per-repo, so a leaked key exposes everything
  the key-owner can reach.
- `known_hosts` pinning is annoying to maintain: the image either
  ships a static copy (brittle when the host rotates its host key)
  or disables host-key verification (undermines the point of
  pinning).
- OpenShell's L7 proxy doesn't intercept SSH, so we lose the
  observability the HTTPS path gives us (no per-request audit, no
  credential-placeholder resolution).
- Doesn't compose with the `GITLAB_TOKEN` provider — it's a second
  credential channel to provision, rotate, and audit.

**Use case.**  Only pick this up if a host *requires* SSH (push
restrictions that reject HTTPS tokens, repo mirrors that only
expose SSH endpoints).  Not a general-purpose answer.

### A.4 Option D — Host-side seed (no in-sandbox clone)

For repos the operator already has checked out on their host, skip
the clone entirely: copy the local working tree into the sandbox
workspace at task-spawn time.  Conceptually:

```make
# Not yet implemented — sketch only.
seed-workspace:
    tar -c -C "$(HOST_REPO)" . \
      | openshell sandbox exec -n $(SANDBOX_NAME) --no-tty -- \
        tar -x -C /sandbox/workspace/$(notdir $(HOST_REPO))
```

The orchestrator / sub-agent then operates on the pre-populated
workspace; it never needs to fetch or push if the task is
strictly-local (code review, refactor, debugging).

**Pros.**

- Zero credential plumbing in the sandbox: the host already has
  whatever git / SSH / token configuration works there.  The
  sandbox doesn't need to know about authentication at all.
- Works for any repo the operator can access on their machine —
  private mirrors, partially-merged branches, repos behind
  2FA-protected hosts that don't tolerate automated tokens.
- Snapshots the operator's exact working state (uncommitted edits,
  untracked files, stashes applied).  Useful for "review my
  current changes" workflows where the point *is* the uncommitted
  state.

**Cons.**

- **One-shot snapshot.**  `git pull` / `git push` inside the
  sandbox still need credentials; this option doesn't solve that,
  it only sidesteps the initial clone.  Paired with Option B when
  both are in play.
- Doesn't compose with autonomous delegation.  The orchestrator
  would need a way to ask the host "do you have this repo locally?
  please tar it over," which is outside the current NMB protocol
  (host ↔ sandbox, not broker ↔ broker).
- Large repos upload slowly and cost sandbox-side disk.  For tasks
  that only need a subset of the tree, a full copy is wasteful.
- Host-side staleness: if the operator hasn't `git pull`'d lately,
  the sub-agent sees the state as of the last local pull, not HEAD
  on the remote.

**Use case.**  Interactive developer workflows — "look at my
current branch and suggest a refactor", "review the diff I have
staged right now."  Complements Option B rather than replacing it:
B handles the fetch path for autonomous / CI-driven work, D handles
the "I already have this locally, use that" path for interactive
work.

---

## Appendix B — Trust Model for Sandbox-Initiated Control-Plane Access

Companion to §5.1.1 — the failure-mode analysis, OpenShell deep-dive
citations, and per-implication discussion that informed the
"sub-agents never reach the control plane; the orchestrator does
narrowly + audited; the operator's host is the trust root" rule
stated there.  §5.1.1 is the design-level view (principle table +
three direct consequences); this appendix is the reasoning behind
it.

### B.1 Why the asymmetry matters

The gateway is the *control plane* (sandbox lifecycle, credential
storage, policy delivery, inference routing).  The sandbox is
*isolated execution* under enforcement (Landlock, seccomp,
network-namespace isolation, L7-proxied egress).  See
[openshell deep dive §4](deep_dives/openshell_deep_dive.md) for the
canonical breakdown.  The sandbox is constrained *by* the gateway;
the sandbox does not constrain the gateway.  That asymmetry is
what lets us hand an LLM-driven agent a `bash` tool without it
being able to exfiltrate credentials or reach arbitrary endpoints
— the constraint is enforced *outside* the process the LLM is
steering.

A sandbox calling `openshell inference set` (or `policy set`, or
`sandbox create` for a non-child of itself) inverts that
asymmetry.  A prompt-injected, hallucinated, or simply buggy agent
could:

- Re-route every subsequent inference call (including the
  orchestrator's own) through an attacker-chosen upstream that
  exfiltrates context — and the L7 proxy faithfully attaches the
  real upstream credential to those requests, because the proxy's
  credential-substitution model assumes the sandbox never sees the
  real backend.
- Widen its own network allowlist or weaken its own filesystem
  read-only set, eliminating the constraint that policy was meant
  to enforce.
- Spawn a fresh sandbox with a permissive policy and exec
  arbitrary commands inside.

### B.2 Why the trust hierarchy is application-level

The OpenShell deep dive explicitly notes that the gateway itself
**does not distinguish "orchestrator sandbox" from "sub-agent
sandbox"** ([§4](deep_dives/openshell_deep_dive.md#4--core-components)
— "the hierarchy is an APPLICATION-LEVEL concern, defined by how
YOU wire the agent code, not by OpenShell").  The trust split
between "trusted parent that constrains children" and "untrusted
child" is something *we* impose at the application level.  The
caller table in §5.1.1 is therefore a *design choice*, not an
enforced OpenShell property; it works only because we wire the
sub-agent's `policy.request` flow (M3 §7) to never reach the
gateway directly, and because we deliberately omit the
gateway-API surface from the sandbox image.

### B.3 Per-implication detail

#### Option A preferred over Option D even when D is feasible

§6.5.1 lists Option A (multi-endpoint provider switching, host-side
state) and Option D (per-sandbox inference provider, requires the
orchestrator to call `openshell inference set --sandbox` from
inside its sandbox).  Option A keeps the control-plane operation
on the operator's host — natural trust origin, no in-sandbox
`openshell` binary, no gateway-reachable network policy.  Option D
moves the same operation into the orchestrator's sandbox, which is
doable *with the right gating* but is strictly more attack surface
than Option A.

The §6.5.1 verdict ("Option A is the production path; Option D is
strictly better only if the preconditions hold") understated this
— Option A is also *strictly safer*, and for that reason it should
remain the default even after Option D's preconditions land,
unless the operational benefit (per-task selection without a
per-model endpoint table) outweighs the trust-boundary cost in a
specific deployment.

#### M3's `SandboxClient.create()` needs scoped CLI surface

When M3 implementation kicks off, the gateway-API surface the
orchestrator's image exposes should be deliberately narrow —
`sandbox create / delete / upload / download` and
`policy set --sandbox <child>` only, not the full `openshell` CLI
— and every invocation should be audited with `workflow_id`.  This
is tracked in [`design_m3.md`](design_m3.md) but worth flagging
here because the principle is shared: control-plane reach from
inside a sandbox is granted to *specific verbs the orchestrator
needs to constrain its children*, not to "the orchestrator can do
anything an operator can do."

#### Sub-agent control-plane requests always route through NMB

The sub-agent's NMB-mediated `policy.request` flow
([`design_m3.md` §7.2](design_m3.md#72-request-flow)) is the
pattern for any future "sub-agent wants to influence the control
plane" capability.  The sub-agent never reaches the gateway
directly; it asks the orchestrator over NMB; the orchestrator
decides (auto-approve, ask user via Slack, or deny).  Auto-approval
is allowlisted (PyPI etc.); anything else escalates.  The
orchestrator may then call the gateway control plane on the
sub-agent's behalf — bringing us back to the "orchestrator sandbox
narrow yes" row of the §5.1.1 table.

---

## Appendix C — Sandbox Configuration Layer: Implementation Detail

Companion to §5.3 — the build/runtime flow, YAML schema, the loader
code, the migration record, and the policy-resolver mapping.  §5.3
is the design-level view (three-category model, resolver pattern,
precedence table, secret-suffix invariant); this appendix is the
implementation-level material behind it.

### C.0 Build and runtime flow

Build time:

1. `make setup-sandbox` depends on `make gen-config` (mirrors
   `gen-policy`).
2. `gen_config.py` reads `config/defaults.yaml`, applies overrides
   from `.env` for the `_CATEGORY_B_KEYS` allowlist, and writes
   `config/orchestrator.resolved.yaml`.
3. `docker/Dockerfile.orchestrator` has
   `COPY --chown=root:root config/orchestrator.resolved.yaml /app/config.yaml`.
4. The resolved file is gitignored (alongside
   `policies/orchestrator.resolved.yaml`).

Runtime:

1. App reads `/app/config.yaml` at startup — already contains the
   merged category-A + category-B values baked in at build time.
2. Category-C secrets come in via env vars (OpenShell provider
   placeholders in the sandbox; `.env` on the host for tooling).
   Non-secret knobs do **not** have env-var overrides — the YAML
   is the single source of truth.

### C.1 `defaults.yaml` schema and `_CATEGORY_B_KEYS` mapping

`config/defaults.yaml` — shipped publicly, **no sensitive values**:

```yaml
orchestrator:
  model: azure/anthropic/claude-opus-4-6
  system_prompt_path: prompts/system_prompt.md
  max_thread_history: 50
  temperature: 0.7
  max_tokens: 2048

agent_loop:
  max_tool_rounds: 10
  max_continuation_retries: 2
  micro_compaction_chars: 10000
  compaction_threshold_chars: 400000
  compaction_compress_ratio: 0.5
  compaction_min_keep: 4

log:
  level: INFO
  # log_file deliberately unset — Dockerfile ENV wins so the path stays
  # image-structure-dependent (/app/logs/nemoclaw.log).

audit:
  enabled: true
  db_path: /sandbox/audit.db
  persist_payloads: true

coding:
  enabled: true
  workspace_root: /sandbox/workspace
  git_clone_allowed_hosts: ""   # category B — resolver fills from .env; empty = fail-closed

skills:
  enabled: true
  skills_dir: /app/skills

toolsets:
  jira:
    enabled: true
    url: https://jirasw.nvidia.com  # public SaaS URL
  gitlab:
    enabled: true
    url: ""   # category B — resolver fills from .env if GITLAB_URL is set
  gerrit:
    enabled: true
    url: ""   # category B — resolver fills from .env if GERRIT_URL is set
  confluence:
    enabled: true
    url: https://nvidia.atlassian.net/wiki  # public SaaS URL
  slack_search:
    enabled: true
  web_search:
    enabled: true
    default_limit: 5
```

`scripts/gen_config.py`'s allowlist of the keys it will fill from
`.env`:

```python
# Category-B keys: .env var name → YAML dotted path
_CATEGORY_B_KEYS: dict[str, str] = {
    "GIT_CLONE_ALLOWED_HOSTS":    "coding.git_clone_allowed_hosts",
    "GITLAB_URL":                 "toolsets.gitlab.url",
    "GERRIT_URL":                 "toolsets.gerrit.url",
    # Future: any other host/IP-flavoured override.
}
```

### C.2 `AppConfig.load()` and helper functions

`AppConfig.load()` (classmethod) replaces the legacy
`load_config()`.  Production entrypoints call one of two thin
builders that name the intent at the call site:

```python
# config.py
@classmethod
def load(cls, path: str | Path | None = None, *, require_slack: bool = True) -> AppConfig:
    config = cls()                               # 1. dataclass defaults
    _apply_yaml_overlay(config, path)            # 2. YAML overlay
    _load_secrets_from_env(config)               # 3. secret env vars only
    _sync_agent_loop_prompting_fields(config)    # 4. inherit orchestrator prompting knobs
    _check_required_config(config, require_slack=require_slack)
    return config


def create_orchestrator_config(path=None) -> AppConfig:
    return AppConfig.load(path=path, require_slack=True)

def create_coding_agent_config(path=None) -> AppConfig:
    return AppConfig.load(path=path, require_slack=False)
```

- `_apply_yaml_overlay` walks the nested dict and assigns to the
  matching sub-config dataclass fields.  Unknown top-level keys log
  a warning but don't fail (forward-compat).
- `_load_secrets_from_env` reads the credential env vars (§5.3.5
  precedence row 3) into the typed config.  In the sandbox these
  are OpenShell-provider-injected placeholders; on the host they
  come from `.env` via `load_dotenv_if_present`.
- `_sync_agent_loop_prompting_fields` propagates
  `orchestrator.model` / `temperature` / `max_tokens` into
  `agent_loop.*` when YAML hasn't pinned the latter.
- `_check_required_config` enforces Slack tokens (when
  `require_slack=True`) and `inference.base_url`.  Deliberately
  **not** checked: `INFERENCE_HUB_API_KEY` — the sandbox's
  inference provider is registered under a different credential
  name (`OPENAI_API_KEY`), and the L7 proxy injects the real key
  at HTTP-request time; the app never reads it.

### C.3 Phase 1 migration record

The migration described in §5.3.6 shipped in Phase 1 (PR #13) and
the Phase 1 follow-up (PR #14).  File-by-file:

1. **Removed the leak**: `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS`,
   `_SANDBOX_CODING_WORKSPACE_ROOT`, and `_SANDBOX_SKILLS_DIR`
   constants deleted from `config.py`.  Values now live in
   `config/defaults.yaml` (public-safe versions) and — for the
   host-allowlist case — in `.env` via the resolver.
2. **`config/defaults.yaml`** ships with fail-closed / empty-string
   values for all category-B fields.
3. **`scripts/gen_config.py`** mirrors `scripts/gen_policy.py` and
   writes `config/orchestrator.resolved.yaml`; the resolved file
   is gitignored.
4. **Makefile** gained `gen-config`; `setup-sandbox` depends on
   both `gen-config` and the pre-existing `gen-policy`.
5. **`docker/Dockerfile.orchestrator`** copies the resolved file
   in as `/app/config.yaml` and the filesystem policy makes it
   read-only.
6. **`AppConfig.load()`** replaces the legacy `load_config()`; the
   `in_sandbox` branches for paths / allowlists are gone.
   Non-secret env-var overrides (`LOG_LEVEL`, `AGENT_LOOP_*`,
   `NMB_URL`, etc.) were also removed — YAML is now the single
   source of truth for non-secret knobs.  New builders
   `create_orchestrator_config()` / `create_coding_agent_config()`
   name intent at the call site.
7. **Runtime detector** (`runtime.py`) produces `SANDBOX` /
   `INCONSISTENT` only — the `LOCAL_DEV` classification and the
   associated escape hatches were dropped; bare-process execution
   outside a sandbox is no longer supported.
8. **`.env.example`** is now a secrets-only + category-B-only
   document; stale `_SANDBOX_*` references and the `LOG_LEVEL` /
   `AGENT_LOOP_*` / `NMB_URL` / `AUDIT_*` / `CODING_*` /
   `SKILLS_*` optional-override blocks were removed.

**What changes for the operator** — before / after walkthrough:

```bash
# .env (unchanged across the migration)
GIT_CLONE_ALLOWED_HOSTS=url1.nvidia.com,url2.nvidia.com,github.com
```

Before (pre-M2b P1):

- Worked locally (Makefile exports → `os.environ` → `load_config`).
- Did **not** work in the sandbox — the `.env` wasn't read inside;
  the `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS` constant was used
  instead, silently ignoring `.env`.

After (post-M2b P1):

- `make gen-config` reads `.env`, writes
  `config/orchestrator.resolved.yaml:coding.git_clone_allowed_hosts`.
- `make setup-sandbox` COPYs the resolved file into the image →
  the sandbox starts with the allowlist honoured.  No `_SANDBOX_*`
  constant in public code, no silent drift between host and
  sandbox.

### C.4 `gen_policy.py` host-substitution mapping

The companion to `_CATEGORY_B_KEYS` for the policy-file resolver
(§5.3.7), kept next to it in source for symmetry:

```python
# scripts/gen_policy.py
_POLICY_HOST_SUBSTITUTIONS: dict[str, str] = {
    "GITLAB_URL": "gitlab",
    "GERRIT_URL": "gerrit",
}
```

`scripts/gen_policy.py` reads `.env`'s `GITLAB_URL` / `GERRIT_URL`,
extracts the hostname via `urllib.parse.urlparse`, and substitutes
into the matching `network_policies.<name>.endpoints[0].host`.
Substitution runs *before* the `allowed_ips` injection so the
SSRF-bypass matcher sees the final host.  Malformed URLs without a
scheme fall through (detected via `urlparse`) and are treated as
missing — same fail-closed posture as an empty `.env`.

---

## Appendix D — Sandbox Detection: Implementation Detail

Companion to §5.4 — the threshold-tuning escape hatch, the
structured-log payload format, and the justification narrative for
the multi-signal-vs-single-env-var choice.  §5.4 is the design-level
view (problem, signal table, classification rule, where the check
runs); this appendix is the implementation-level material behind
it.

### D.1 Test-only threshold escape hatch

The 4-of-6 default lives in `_DEFAULT_SANDBOX_SIGNAL_THRESHOLD` and
applies in production unconditionally.  Subprocess integration tests
on a developer laptop can only fake the three env-based signals
(`OPENSHELL_SANDBOX`, `HTTPS_PROXY`, `SSL_CERT_FILE`) — the path-
based and DNS-based signals require an actual sandbox.  The
`NEMOCLAW_SANDBOX_SIGNAL_THRESHOLD` env var lowers the threshold for
those tests.  Production MUST NOT set it; the only thing keeping the
hatch from being a foot-gun is the convention "tests set it
explicitly, prod never reads it."

The earlier `LOCAL_DEV` classification and its associated escape
hatches (`_BENIGN_LOCAL_ENV_SIGNALS`, `_LOCAL_DEV_SIGNAL_CEILING`)
were removed in Phase 1 follow-up alongside the rest of the
local-dev-mode infrastructure: the OpenShell sandbox is the only
supported runtime, so "not in a sandbox" is a deployment bug, not
a legitimate mode.

### D.2 Structured-log payload format

On `INCONSISTENT` the self-check raises
`SandboxConfigurationError` with a structured log entry shaped
like:

```json
{
  "level": "ERROR",
  "component": "main",
  "message": "Sandbox detection inconsistent — refusing to start",
  "signals_present": ["HTTPS_PROXY", "sandbox_dir", "ssl_cert_file"],
  "signals_missing": ["OPENSHELL_SANDBOX", "inference_local_resolves"],
  "classification": "INCONSISTENT",
  "likely_cause": "OpenShell version drift or gateway misconfigured",
  "suggested_fix": "Run `make status` to inspect providers; recreate sandbox with `make run-local-sandbox`"
}
```

The `signals_present` / `signals_missing` arrays make the failure
mode self-describing — an operator hitting this in the field can
read off which signals fired and which didn't without inspecting
the source.  `likely_cause` is derived heuristically from the
signal pattern (e.g. all env signals present + all path signals
missing → "running outside an OpenShell-managed container"); the
heuristic table is small enough to live next to the classifier.

### D.3 Why not trust `OPENSHELL_SANDBOX` alone?

A multi-signal check costs roughly 50 lines of Python and one
extra log line per startup.  The alternative — trusting a single
env var — has already caused one class of silent failure: pre-M2b
`load_config()` happily produced a config with an empty
`INFERENCE_HUB_BASE_URL` if that env var was missing while
`OPENSHELL_SANDBOX` was set, or vice versa.  Either signal
disagreeing with the other (without the app noticing) was enough
to break the config silently.

The general failure pattern is "one signal lied" — OpenShell's
self-identification env var is set but the L7 proxy isn't actually
intercepting (no `HTTPS_PROXY`), or the proxy is set but the
sandbox PVC isn't mounted, or the inference DNS doesn't resolve.
Each of these turns into a confusing downstream symptom (401,
disk-full, audit-write failure) hours after startup.  The
multi-signal check turns all of them into a single fail-fast log
line.  That's the reason to keep it even when the single-env-var
path "would be enough" in any individual deployment.

---

## Appendix E — Typed Protocol Payloads: Full Pydantic Definitions

Companion to §6.3 — the per-field source for the six Pydantic
models that make up the M2b orchestrator ↔ sub-agent protocol.
§6.3 is the design-level view (model summary table + the three
protocol-design choices); this appendix is the reference material.
All models live in `nmb/protocol.py`; both sides import from there
so neither can drift.

```python
# nmb/protocol.py

from typing import Literal
from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt
```

### E.1 `TaskAssignPayload`

```python
class TaskAssignPayload(BaseModel):
    """Orchestrator → Sub-Agent. Carries everything needed to run one task."""

    prompt: str = Field(min_length=1)
    """The natural-language task description."""

    workflow_id: str
    """UUID generated at delegation time. Threads through every NMB
    message and audit record for the workflow."""

    parent_sandbox_id: str
    """The orchestrator's sandbox identity. Used by the sub-agent's
    `AuditBuffer` to attribute records back to the parent."""

    max_turns: PositiveInt | None = None
    """Per-task cap on AgentLoop tool rounds. Overrides
    ``cfg.agent_loop.max_tool_rounds`` for this delegation. ``None``
    means "fall back to the global cap". See §6.4."""

    model: str | None = None
    """Per-task model override.  Passed straight through to the
    sub-agent's ``AgentLoopConfig.model`` for this run; ``None`` means
    "use ``cfg.agent_loop.model``" (which itself defaults to
    ``cfg.inference.model``).  Whether the override actually selects a
    different upstream depends on the inference-routing topology — see
    §5.1 caveat and §6.5."""

    tool_surface: list[str] | None = None
    """Optional explicit allowlist of tool names the sub-agent may
    use. ``None`` means "use the role's default registry as built by
    ``create_coding_tool_registry``"; non-empty narrows it. The
    sub-agent's `AgentLoop` filters its `ToolRegistry` against this
    list (Phase 3 — see Implementation Plan)."""

    context_files: list["ContextFile"] = Field(default_factory=list)
    """Inline files the orchestrator wants seeded into the sub-agent's
    workspace before the run starts. Bounded by NMB message-size cap
    — large repos still go through ``git_clone``."""

    workspace_baseline: "WorkspaceBaseline | None" = None
    """The state the diff in ``TaskCompletePayload.diff`` is computed
    against.  ``None`` means "no git repo in the workspace, the task
    isn't expected to produce a diff" (e.g. a one-shot data-extraction
    task).  See §4.2.1 for why this is a protocol field rather than
    implicit, and §6.6 for the parallel-spawn semantics."""

    is_iteration: bool = False
    iteration_number: NonNegativeInt = 0
    """Set when this assignment is a ``re_delegate`` follow-up after
    a finalization-driven iteration (§7.1)."""
```

### E.2 `ContextFile` and `WorkspaceBaseline` helpers

```python
class ContextFile(BaseModel):
    path: str  # workspace-relative
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class WorkspaceBaseline(BaseModel):
    """The git state the orchestrator pinned the sub-agent's workspace
    to at spawn time.  Carried on ``TaskAssignPayload`` and echoed
    back on ``TaskCompletePayload`` so the orchestrator can verify
    the sub-agent didn't silently re-base mid-task."""

    repo_url: str
    """Canonical clone URL (e.g.
    ``https://gitlab.example.com/acme/demo-repo.git``).  Lets the
    orchestrator distinguish two parallel agents working on the same
    repo from two parallel agents working on different repos."""

    branch: str
    """The branch name the workspace was checked out on.  Carried for
    audit / PR-construction; the SHA below is the actual baseline."""

    base_sha: str
    """The 40-char commit SHA the workspace's working tree was at
    when ``setup-workspace.sh`` finished.  ``TaskCompletePayload.diff``
    is ``git diff <base_sha>..HEAD`` after the sub-agent's edits."""

    is_shallow: bool = True
    """Most M2b clones are shallow (`depth=1`); recorded so finalisation
    can decide whether `git pull --rebase` against the upstream is safe
    or whether it needs to deepen first."""
```

### E.3 `TaskProgressPayload`

```python
class TaskProgressPayload(BaseModel):
    """Sub-Agent → Orchestrator. Optional, periodic, best-effort."""

    workflow_id: str
    status: Literal[
        "starting", "reading_workspace", "writing_code",
        "running_tests", "finalizing"
    ]
    pct: int | None = Field(default=None, ge=0, le=100)
    current_round: NonNegativeInt | None = None
    tokens_used: NonNegativeInt | None = None
    note: str | None = None
    """Human-readable line, e.g. "Created src/api/health.py"."""
```

### E.4 `TaskCompletePayload`

```python
class TaskCompletePayload(BaseModel):
    """Sub-Agent → Orchestrator. Terminal success message."""

    workflow_id: str

    summary: str = Field(min_length=1)
    """One-paragraph user-facing description of what was done. The
    orchestrator's finalization step renders this verbatim into Slack."""

    diff: str = ""
    """Unified diff between ``workspace_baseline.base_sha`` (set on
    the matching ``TaskAssignPayload``, echoed below) and the
    workspace's HEAD after the sub-agent's edits.  Empty when the
    task didn't modify any tracked files (e.g. "summarize this repo")
    or when the assigned ``workspace_baseline`` was ``None``."""

    workspace_baseline: WorkspaceBaseline | None = None
    """Echoed verbatim from ``TaskAssignPayload.workspace_baseline``.
    Lets the orchestrator detect the (rare but real) case where a
    sub-agent ran ``git_checkout`` mid-task and ended up rooted
    somewhere different from what was assigned: if the orchestrator
    expected ``base_sha=A`` but the sub-agent reports ``base_sha=B``,
    the diff is meaningless against the orchestrator's plan and
    finalisation must surface this as an error rather than push a
    diff against the wrong base.  ``None`` only when the assigned
    baseline was also ``None``."""

    files_changed: list[str] = Field(default_factory=list)
    """Workspace-relative paths. Convenience field — ``diff`` is the
    source of truth."""

    notes_path: str | None = None
    """Workspace-relative path to the agent's scratchpad
    (``notes-<task-slug>-<agent-id>.md``), if it created one. M2a's
    scratchpad skill makes this optional, not mandatory."""

    git_commit_sha: str | None = None
    """When the sub-agent ran ``git_commit``.  This is the *new* SHA
    on top of ``workspace_baseline.base_sha``; the orchestrator's
    ``push_and_create_pr`` pushes the range
    ``<base_sha>..<git_commit_sha>``."""

    tool_calls_made: NonNegativeInt = 0
    rounds_used: NonNegativeInt = 0
    """For audit / cost tracking. ``rounds_used <= max_turns`` always."""

    model_used: str | None = None
    """The model name the sub-agent's `AgentLoop` set on the chat-
    completions request body — i.e., the *requested* model.  In M2b
    the L7 proxy may have rewritten this before reaching the upstream
    (§6.5.1); recorded here so the orchestrator's audit row matches
    what the protocol-level scaffolding said happened.  M3's Option D
    eliminates the rewrite and this becomes the realised model too."""

    suggested_next_step: str | None = None
    """Optional. The sub-agent can hint that more work is needed
    (e.g. "tests pass but coverage dropped — consider adding edge
    case for empty input"). The finalization model may use this to
    proactively call ``re_delegate`` (§7.1)."""
```

### E.5 `TaskErrorPayload`

```python
class TaskErrorPayload(BaseModel):
    """Sub-Agent → Orchestrator. Terminal failure message."""

    workflow_id: str
    error: str
    error_kind: Literal[
        "max_turns_exceeded", "tool_failure",
        "policy_denied", "inference_error", "other"
    ]
    recoverable: bool = False
    notes_path: str | None = None
    traceback: str | None = None
```

---

## Appendix F — Per-Task `model` Selection: Detailed Trade-offs

Companion to §6.5 — the motivating task shapes, the per-constraint
breakdown of the L7-proxy routing surface, and the four-option
comparison.  §6.5 is the design-level view (resolution rule,
no-op-in-M2b caveat, lifecycle, "Option A is the production
path"); this appendix is the supporting material.

### F.1 Motivating task shapes for per-task model selection

Once operational selection lands (Option A in M2b Phase 6 or M3),
these are the task shapes the orchestrator's planning model would
match against (per-shape lookup table or model-driven routing —
§17 Q8).  In M2b's scaffolding-only state these are illustrative
only.

| Task shape | Suggested model | Why |
|---|---|---|
| One-shot lookup ("show me the README") | A small, fast model (e.g. Haiku) | One file read, one summary; the cost difference between Haiku and Opus dominates the wall-clock difference |
| Routine bug fix in a known module | The default Opus baseline | Mainstream coding work; current behaviour |
| Multi-file refactor with verification | Opus (or a reasoning-tier model) | Long context, multi-step planning; cheap models lose tracking |

### F.2 L7-proxy constraints, detailed breakdown

§6.5.1 summarises three constraints in a sentence each. Per-
constraint:

- **Gateway-scoped, not sandbox-scoped.**  `openshell inference get`
  shows one "Gateway inference" routing entry shared by every
  sandbox the gateway hosts (and one optional "System inference"
  fallback).  There is no documented `openshell inference set
  --sandbox <name>` flag — see open question §17 Q10.  Adding one
  is the mechanical precondition (a) of Option D.
- **Mutable at runtime, but globally.**  `openshell inference set`
  can be re-run anytime and the change takes effect on the next
  `inference.local` request, no sandbox restart needed.  The change
  is gateway-global though — flipping the model mid-task swaps it
  for every other in-flight inference call across every sandbox.
  Safe only if delegations are serialised, which the §8 concurrency
  model explicitly rejects.
- **Control-plane reach is not wired from inside the sandbox today,
  and the trust model says it should stay narrow even when wired.**
  `openshell inference set` is a gateway-API call (gRPC + HTTP on
  `localhost:8080` for a local gateway, or the remote equivalent).
  The orchestrator image does not install the `openshell` binary,
  the network policy has no entry for the gateway endpoint or the
  `openshell` binary, and mTLS client credentials for the gateway
  API are not provisioned to the sandbox.  M3 has to fix all three
  to support `SandboxClient.create()` for sub-agent spawn (M3 §3.2);
  inference routing rides the same plumbing.  But the principled
  question — *should* a sandbox modify gateway state? — has a more
  nuanced answer than the mechanical one: sub-agents never should
  (they're the constrained party); the orchestrator can, but only
  behind narrow scoping + an approval gate; the operator's host
  shell remains the authoritative trust root.  See §5.1.1 / Appendix
  B for the full breakdown.  In M2b the sandbox cannot drive the
  control plane at all, so `inference set` runs only from the
  operator's host shell — which §5.1.1 argues is *also* the
  preferred home for it under Option A even after the mechanical
  wiring lands.

### F.3 Options considered, full comparison

| Option | Sketch | Verdict |
|---|---|---|
| **A — Multi-endpoint provider switching** | Register one provider per upstream (`inference-haiku`, `inference-opus`); each is reachable at its own `inference-<name>.local` host with its own gateway-routing config; orchestrator picks the URL per task by switching `cfg.inference.base_url`. | The only option that gives **concurrent** per-task selection without spawning sandboxes. Pays operator-side cost (extra provider registrations + one network-policy entry per host) but the cost is bounded and predictable. Survives unchanged into M3. |
| **B — Proxy passthrough mode** | Have the proxy forward the app's `model` field unchanged. | Requires a proxy change we don't own. |
| **C — App-side fan-out** | Sub-agent talks to `api.anthropic.com` directly, bypassing `inference.local`. | Loses the L7 proxy's credential-substitution benefit. Developer escape hatch only. |
| **D — Per-sandbox inference provider** *(speculative, two preconditions + trust-model trade-off)* | Bind a model-specific provider to *that* sandbox at spawn time: `openshell inference set --sandbox <name> --provider <p>`. Per-task selection becomes per-sandbox provider binding. | Would be cleaner *operationally* than Option A if both preconditions hold (§17 Q10): (a) OpenShell adds `--sandbox` scoping; (b) the orchestrator can reach the gateway control plane from inside its sandbox.  M3 has to solve (b) anyway for `SandboxClient.create()`, so the mechanical question reduces to (a).  But the §5.1.1 trust-model analysis flips the operational verdict: even when Option D is mechanically feasible, it moves a control-plane operation *into* the orchestrator's sandbox, where it adds attack surface — Option A keeps the same operation on the operator's host where it belongs. Option D therefore needs a positive case (per-task selection without a per-model endpoint table is materially better in this deployment), not just feasibility. |

---

### Sources

- [Original M2 Design Document](design_m2.md)
- [M2a Design Document](design_m2a.md) — reusable `AgentLoop`, file tools, compaction, skills
- [M3 Design Document](design_m3.md) — successor milestone (multi-sandbox delegation, review agent, skill auto-policy, `Manifest`, lazy `load_skill`)
- [NMB Design](nmb_design.md) — inter-sandbox messaging protocol
- [Sandbox Spawn Design](sandbox_spawn_design.md) — OpenShell sandbox lifecycle
- [Audit DB Design](audit_db_design.md) — tool-call auditing, NMB-batched flush
- [OpenAI Agents SDK Deep Dive](deep_dives/openai_agents_sdk_deep_dive.md) — adopted Tier 1 patterns (per-task `max_turns`, typed protocol payloads in Phase 3; lazy `load_skill` in Phase 6); deferred Tier 2 to M3 (`Manifest`, snapshots); Tier 3 `Capability`-style bundles optional in Phase 6, gated on M3 demand
- [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md) — §4 (event bus), §7 (routing), §8 (dispatch), §9 (concurrency), §13 (cron)
- [GAPS.md](../GAPS.md) — feature tracking
