# NemoClaw Escapades — Design Document

> **Tagline:** The enterprisified OpenClaw with practical use-cases.
>
> **Started:** 2026-02-24 &nbsp;|&nbsp; **Last updated:** 2026-04-01

---

## Table of Contents

1. [Vision](#1--vision)
2. [Background & Inspiration](#2--background--inspiration)
3. [Northstar Architecture](#3--northstar-architecture)
   - [3.1 Key Components](#31--key-components)
   - [3.2 Workflow Execution Model](#32--workflow-execution-model)
4. [Milestones](#4--milestones)
5. [Open Questions](#5--open-questions)
6. [Blog Post Series](#6--blog-post-series)
7. [Capabilities the System Should Eventually Have](#7--capabilities-the-system-should-eventually-have)
8. [Design Principles](#8--design-principles)
9. [Web UI — Mission Control Dashboard](#9--web-ui--mission-control-dashboard)
   - [9.1 Design Goals](#91--design-goals)
   - [9.2 UI Surfaces](#92--ui-surfaces)
   - [9.3 Technical Architecture](#93--technical-architecture)
   - [9.4 UX Principles](#94--ux-principles)
   - [9.5 Milestone Mapping](#95--milestone-mapping)
   - [9.6 Open Questions (Web UI)](#96--open-questions-web-ui)
10. [Future Work — Features Inspired by Claude Code](#10--future-work--features-inspired-by-claude-code)
11. [References & Related Projects](#11--references--related-projects)

### Companion Design Documents

- **[Orchestrator Agent Design](orchestrator_design.md)** — agent loop
  architecture, streaming tool execution, system prompt construction, multi-agent
  coordinator mode, permission system, session compaction, model behavioral
  contract, task store. Draws from the Claude Code leak analysis.
- **[NemoClaw Message Bus (NMB) Design](nmb_design.md)** — real-time
  inter-sandbox messaging protocol, broker, client library, security model,
  multi-host deployment, coordinator integration, session forking, peer discovery.
- **[Training Flywheel Design](training_flywheel_deep_dive.md)** — turning
  daily agent interactions into SFT/RL training data; two-layer trace capture,
  quality filtering, DPO pairs from review loops, Nemotron fine-tuning.

---

## 1  Vision

Build an always-on agentic system — a "super IC" — that performs useful work
on the user's behalf around the clock, even while asleep. The system
communicates via Slack, runs sandboxed workloads through
[OpenShell](https://github.com/NVIDIA/OpenShell), and continuously improves
itself using a self-learning loop modeled after Hermes and OpenClaw.

The project doubles as an extensive learning exercise: every development
milestone produces a blog post documenting the setup, the decisions, and the
lessons learned.

## 2  Background & Inspiration

| System | Role / Takeaway |
|--------|-----------------|
| **[OpenClaw](https://github.com/openclaw/openclaw)** | Reference architecture for an agentic coding & task system (341k stars, MIT). Multi-channel personal AI assistant with sandbox execution, skills system, cron scheduling, multi-agent routing, and a plugin/extension ecosystem. Read & distill its use-cases and setups. |
| **[Hermes Agent](https://github.com/nousresearch/hermes-agent)** | Self-improving AI agent by Nous Research (17k stars, MIT). Key features: closed learning loop (skills created from experience, self-improving during use), managed memory via [Honcho](https://github.com/plastic-labs/honcho), auto-skill creation, pluggable inference backends (OpenRouter, OpenAI, etc.), cron scheduling, sub-agent delegation, and multi-platform messaging (Telegram, Discord, Slack, WhatsApp, Signal). Provides a `hermes claw migrate` command for OpenClaw migration. Key question: can we port Hermes to NemoClaw while preserving the self-learning loop? |
| **[OpenShell](https://github.com/NVIDIA/OpenShell)** | Secure runtime for autonomous AI agents (Apache 2.0). Sandbox containers, kernel-level isolation (Landlock + seccomp), declarative network policy, inference routing, credential injection. Agent-agnostic — doesn't care what runs inside. **This is the infrastructure layer we use directly.** |
| **[NemoClaw](https://github.com/NVIDIA/NemoClaw)** | Setup harness that automates deploying OpenClaw into an OpenShell sandbox (Apache 2.0, alpha). Contains a blueprint (default policies + setup script) and a plugin (inference provider registration). **Not an agent** — no agent loop, no skills, no memory, no tools. Since this project builds a custom orchestrator rather than vanilla OpenClaw, NemoClaw provides no runtime value. Studied for policy patterns but not used in the stack. |
| **[SecondBrain](https://github.com/dpickem/project_second_brain)** | Personal knowledge management & learning system (own project). Features: multi-source ingestion (PDF, web, books, code), LLM-powered summarization, Neo4j knowledge graph, spaced repetition learning (FSRS), Obsidian-based vault, and a React/FastAPI web UI. Serves as the "academic memory" layer for this project. |
| **[Claude Code](https://github.com/zackautocracy/claude-code)** | Anthropic's terminal-native AI coding assistant (proprietary, source leaked March 2026). 1,884 TypeScript files, 40+ tools, 80+ slash commands, 90 feature flags. Key patterns adopted for this project: streaming-first async generator agent loop, three-tier context compaction (micro/full/session memory), two-stage auto-permission classifier, prompt cache boundary (`__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`), model behavioral contract with transcript repair, coordinator mode for multi-agent orchestration, `skillify` for automatic workflow capture, `extractMemories` for passive memory extraction. See [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) and [Orchestrator Design](orchestrator_design.md). |
| **nv-tools** | Unified CLI for NVIDIA services (Jira, Gerrit, GitLab, Slack, etc.). Provides read/write access to the professional ecosystem. |


## 3  Northstar Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User (via Slack)                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Orchestrator Agent                          │
│  ┌───────────┐  ┌────────────┐  ┌──────────────┐                │
│  │ Scheduler │  │ Policy     │  │ Self-Learning│                │
│  │ (cron)    │  │ Engine     │  │ Loop         │                │
│  └───────────┘  └────────────┘  └──────────────┘                │
│                   delegates to sub-agents                       │
└──────┬──────────────┬───────────────┬───────────────┬───────────┘
       │              │               │               │
       ▼              ▼               ▼               ▼
 ┌───────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐
 │  Coding   │ │  Review    │ │  Research  │ │  Note-Taking │
 │  Agent    │ │  Agent     │ │  Agent     │ │  / Scraper   │
 │ (CC in    │ │ (pre-push  │ │ (2nd Brain │ │ (Slack,      │
 │ OpenShell)│ │  collab)   │ │  + web)    │ │  Teams)      │
 └─────┬─────┘ └─────┬──────┘ └─────┬──────┘ └──────┬───────┘
       │              │               │               │
       ▼              ▼               ▼               ▼
 ┌───────────────────────────────────────────────────────────┐
 │                  Sandbox Layer (OpenShell)                │
 │   each workflow runs in its own sandbox container         │
 └───────────────────────────────────────────────────────────┘
       │              │               │               │
       ▼              ▼               ▼               ▼
 ┌───────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐
 │ GitLab /  │ │ Gerrit     │ │ Memory     │ │ Professional │
 │ GitHub    │ │ (code      │ │ System     │ │ KB           │
 │ (push)    │ │  review)   │ │ (Honcho /  │ │ (distilled   │
 │           │ │            │ │  SB?)      │ │  notes)      │
 └───────────┘ └────────────┘ └────────────┘ └──────────────┘
```

### 3.1  Key Components

**Orchestrator Agent** — The "main brain." Receives tasks from Slack (or cron),
applies policies, delegates to sub-agents, and manages bookkeeping of running
tasks and their results. Must own the agentic loop.

**Connector Layer (generic base class)**
- First instantiation: Slack.
- Future: Telegram (not on company VPN — no work materials exposed there).
- Design pattern: abstract base class so new channels are plug-and-play.

**Inference Backend (generic base class)**
- Pluggable backend similar to Hermes.
- Must be compatible with inference hub, or adaptable to it.

**Coding Agent** — A custom coding sub-agent that runs inside an ephemeral
OpenShell sandbox. Input: task + source code. Output: PR / patch. Containers
are auto-garbage-collected. Calls `inference.local` (model-agnostic via
OpenShell routing) so it can run on Nemotron, Claude, or any other model.

The coding agent evolves across milestones:
- **M2:** Start with Claude Code for fast proof-of-concept (best code quality,
  one-command setup, but Anthropic-locked and off-policy for the training
  flywheel).
- **M6+:** Replace with a custom coding agent that lifts the best patterns from
  both OpenClaw (Pi's 20+ tools, `apply_patch`, block streaming, tool
  profiles) and Hermes (provider resolver, context compression, prompt
  caching, concurrent tool execution). The custom agent is model-agnostic,
  Python-based (same stack as the orchestrator), and generates on-policy
  training data for the Nemotron flywheel.

Supports git-worktree-based parallelism for multiple concurrent coding tasks.

**Review Agent** — Collaborates with the coding agent *before* the PR is
pushed ("local" collaboration without Git in the loop). Can also post reviews
on Gerrit/GitLab after push.

**Memory System** — Distinct from the knowledge base. Provides the agent with
persistent, queryable memory for the self-improvement loop. Candidates: Honcho
(as used by Hermes) and/or SecondBrain. Open question: how do these combine?

**Self-Improvement Loop** — Modeled after Hermes / OpenClaw. The agent learns
from past task outcomes, auto-creates and updates its own skills, and refines
policies over time. Requires the memory system to be in place first.

**NemoClaw Message Bus (NMB)** — A lightweight WebSocket-based message broker
running on the host alongside the OpenShell gateway. Provides real-time
inter-sandbox messaging (~20-50ms) as an alternative to file-based
coordination (~2-5s). Follows the same `inference.local` pattern used by
OpenShell for inference routing: sandboxes call `messages.local`, the proxy
authenticates by sandbox identity, and routes to the broker. Supports
point-to-point send, request-reply, pub/sub channels, and streaming. See
[NMB Design Document](nmb_design.md) for the full specification.

**Training Flywheel** — Every agent interaction generates training data. A
two-layer trace capture system (per-sandbox `trace.db` + NMB `audit.db`)
feeds into a merge pipeline that produces SFT and DPO datasets for
fine-tuning Nemotron. Review-loop iterations automatically generate preference
pairs. See [Training Flywheel Design](training_flywheel_deep_dive.md) for the
full specification.

**Knowledge Management** — SecondBrain CLI as the personal/academic KB.
Separate professional KB built from distilled Slack and Teams notes.
Separation: 2nd Brain → personal KB; everything else → professional KB.

### 3.2  Workflow Execution Model

- Existing slackbot workflows should be convertible to OpenShell policies.
- Each workflow should ideally run in its own sandbox container (isolation).
- **Skills declare their own sandbox policy** via a `nemoclaw.infrastructure`
  block in SKILL.md metadata (network endpoints, filesystem paths, binaries).
  A policy generator produces the OpenShell YAML automatically. See
  [OpenClaw Deep Dive §20-Q9](deep_dives/openclaw_deep_dive.md#q9-can-we-auto-identify-a-workflows-required-permissions)
  for the full design.
- For skills without policy metadata, use OpenShell's deny-and-approve TUI
  to iteratively discover required permissions, then write them back into the
  skill for future runs.

### 3.3  Why NemoClaw Is Not in This Architecture

NemoClaw is a **setup harness** for deploying the vanilla OpenClaw agent into
an OpenShell sandbox. It contains no agent intelligence — no agent loop, no
skills, no memory, no tools, no cron, no self-learning loop. It is a setup
wizard that runs `openshell` CLI commands to create a sandbox, apply default
policies, and register inference providers. (See
[NemoClaw Deep Dive §1, §13](deep_dives/nemoclaw_deep_dive.md#1--overview)
for details.)

Since this project builds a **custom orchestrator** — with its own agent loop,
Hermes-style self-learning, custom memory system, and Slack connector — rather
than deploying vanilla OpenClaw, NemoClaw provides no runtime value:

| Layer | What provides it | Why not NemoClaw? |
|-------|-----------------|-------------------|
| **Agent intelligence** (agent loop, skills, memory, self-learning, cron, sub-agent delegation) | Custom orchestrator (this project) | NemoClaw contains none of this. It's a setup script, not an agent. |
| **Sandbox runtime** (isolation, policy enforcement, inference routing, credentials) | OpenShell directly | NemoClaw is just a thin wrapper around OpenShell CLI commands. We call those commands ourselves. |
| **Policies** (network, filesystem, process) | Custom OpenShell policy YAML files | NemoClaw ships default policies for OpenClaw. Our orchestrator needs different policies. |
| **Inference routing** | OpenShell provider registration | NemoClaw configures Nemotron endpoints. We configure our own. |

**The stack for this project is: custom orchestrator + OpenShell. NemoClaw is
not part of it.** The NemoClaw deep dive remains useful as a reference for how
NVIDIA packages an agent for OpenShell (blueprint architecture, policy
patterns), but nothing from NemoClaw is used at runtime.

## 4  Milestones

The milestones below are listed in planned execution order, and milestone IDs
are now sequential (`M1` → `M6`). Each milestone corresponds to a blog post in
the series (see §6).

### Milestone 1 — Foundation: Slack + Inference Hub + Orchestrator

Set up the basic loop: a Slack connector talks to an orchestrator agent backed
by an inference hub endpoint.

**Deliverables:**
- GitHub repo (`nemoclaw_escapades`) with README and docs (this document).
- Slack connector (generic connector base class, Slack as first impl).
- Inference hub integration (generic backend base class).
- Minimal orchestrator that receives a Slack message, calls the LLM, and
  replies. See [Orchestrator Design](orchestrator_design.md) for the detailed
  agent loop, tool system, and session management architecture.
- Architecture diagrams (the ones missing from NemoClaw's own docs).
- Defensive model output handling (transcript repair) — orphan stripping,
  JSON fallback, empty-message filtering, recovery prompts. *(Inspired by
  Claude Code's behavioral contract.)*
- Tiered auto-approval for safe operations — fast-path pattern matcher for
  known-safe operations + async Slack escalation for dangerous ones.
  *(Inspired by Claude Code's YOLO classifier.)*

### Milestone 2 — Coding Agent via OpenShell

Add a sub-agent that can write code inside a sandboxed OpenShell container.

**Deliverables:**
- OpenShell container provisioning and lifecycle management (auto-GC).
- Claude Code running inside the container as the initial coding agent
  (fastest path to a working system; replaced by custom agent in M6+).
- Policy definition for the coding agent (permissions, tools, constraints).
- Sub-agent delegation from orchestrator → coding agent.
- Bookkeeping of spawned sub-agents and result processing.
- Git worktree support for parallel coding tasks.
- Input/output contract: seed workspace → task via NMB → results via NMB →
  cleanup. Contract is agent-agnostic so the underlying coding agent can be
  swapped without changing the orchestrator.

### Milestone 3 — Review Agent

Add a review agent that collaborates with the coding agent before code is
pushed.

**Deliverables:**
- Review agent that reads diffs and provides structured feedback.
- "Local" collaboration loop: coding agent ↔ review agent iterate without
  Git round-trips.
- Post-push review integration via Gerrit/GitLab (using nv-tools).

### Milestone 4 — Note-Taking & Professional Knowledge Base

Build a scraping/summarization system that distills information from Slack
(and ideally Teams) into a professional knowledge base.

**Deliverables:**
- Slack message scraping and summarization pipeline.
- Teams integration (open question: how to access Teams?).
- Distilled notes stored in the professional KB.

### Milestone 5 — Knowledge + Memory Orchestration (SecondBrain + Honcho)

Add a structured memory layer that combines SecondBrain knowledge storage with
Honcho user-memory patterns, borrowing proven memory-management techniques from
both Hermes and OpenClaw.

**Deliverables:**
- SecondBrain integration for durable knowledge capture and retrieval.
- Honcho integration for user modeling and personalized memory state.
- Unified memory manager with clear roles (working memory, user memory,
  knowledge memory) and retrieval routing.
- Memory hygiene policies (deduplication, retention/decay, conflict handling,
  and source traceability).
- Explicit separation of personal vs. professional knowledge stores.
- Passive memory extraction from conversations — automatically extract key
  facts (user preferences, project conventions, recurring patterns) from every
  conversation without explicit user action. *(Inspired by Claude Code's
  `extractMemories` service.)*

### Milestone 6 — Self-Improvement Loop + Autonomous Skill Evolution

Build the continuous learning loop on top of the memory foundation so the agent
can evaluate outcomes and improve behavior over time.

**Deliverables:**
- Post-task evaluator that records outcomes and lessons into the memory stack.
- Skill/policy update pipeline driven by observed successes and failures.
- Auto-skill creation/update flow with review checkpoints and rollback path.
- Cron-driven background audits (Slack/Jira/docs), issue triage, and backlog
  shaping.
- Automatic skill capture from successful sessions — after a task succeeds,
  offer to package the workflow as a reusable `SKILL.md`. *(Inspired by
  Claude Code's `skillify` bundled skill.)*

## 5  Open Questions

These are captured directly from the notebook and should be resolved as the
project progresses.

| # | Question | Related Milestone | Answered In |
|---|----------|-------------------|-------------|
| 1 | What is the exact Hermes agent architecture? Can we duplicate it or lift applicable parts? | M6 | [Hermes Deep Dive §2, §4, §18-Q1](deep_dives/hermes_deep_dive.md#q1-what-is-the-exact-hermes-agent-architecture-can-we-duplicate-it-or-lift-applicable-parts) |
| 2 | What is the block diagram for Hermes? | M6 | [Hermes Deep Dive §2](deep_dives/hermes_deep_dive.md#2--high-level-architecture) |
| 3 | Is Hermes compatible with inference hub, or can it be made compatible? | M1 | [Hermes Deep Dive §5, §18-Q3](deep_dives/hermes_deep_dive.md#q3-is-hermes-compatible-with-inference-hub-or-can-it-be-made-compatible) — **Yes**, via `base_url` config for any OpenAI-compatible endpoint |
| 4 | How can we port Hermes to NemoClaw while preserving the self-learning loop? | M6 | [Hermes Deep Dive §14, §18-Q4](deep_dives/hermes_deep_dive.md#q4-how-can-we-port-hermes-to-nemoclaw-while-preserving-the-self-learning-loop) — replicate skills + memory + session search + prompt nudges |
| 5 | How does Honcho combine with SecondBrain? Should we use one or both? | M6 | [Hermes Deep Dive §10, §18-Q5](deep_dives/hermes_deep_dive.md#q5-how-does-honcho-combine-with-secondbrain-should-we-use-one-or-both) — **Use both**: Honcho = user modeling, SB = domain knowledge, built-in memory = working context |
| 6 | Does NemoClaw provide a harness? Or computer use? | M1 | [NemoClaw Deep Dive §15-Q6](deep_dives/nemoclaw_deep_dive.md#q6-does-nemoclaw-provide-a-harness-or-computer-use) — **Harness**, not computer use; wraps OpenClaw in sandboxed runtime with policy controls, inference routing, lifecycle mgmt |
| 7 | What should be the "main brain" — where does the orchestrator run? | M1 | [NemoClaw Deep Dive §15-Q7](deep_dives/nemoclaw_deep_dive.md#q7-what-should-be-the-main-brain--where-does-the-orchestrator-run) + [Hosting Deep Dive §11](deep_dives/hosting_deep_dive.md#11--where-the-core-agent-loop-runs) — Inside OpenShell sandbox; **Brev** recommended for always-on, DGX Spark for local |
| 8 | Can existing slackbot workflows convert to NemoClaw policies? | M1 | [NemoClaw Deep Dive §15-Q8](deep_dives/nemoclaw_deep_dive.md#q8-can-existing-slackbot-workflows-convert-to-nemoclaw-policies) — Partially; workflow logic → OpenClaw skills, API access → NemoClaw network policies |
| 9 | Can we auto-generate OpenShell sandbox policies from skills? | M2 | [OpenClaw Deep Dive §20-Q9](deep_dives/openclaw_deep_dive.md#q9-can-we-auto-identify-a-workflows-required-permissions) — **Yes**, via a `nemoclaw.infrastructure` block in SKILL.md metadata that declares network endpoints, filesystem paths, and binaries; a policy generator produces the OpenShell YAML. Fallback: deny-and-approve discovery via [OpenShell TUI](deep_dives/openshell_deep_dive.md#q9-can-we-auto-identify-a-workflows-required-permissions). |
| 10 | Should each workflow run in its own sandbox container? | M2 | [NemoClaw Deep Dive §15-Q10](deep_dives/nemoclaw_deep_dive.md#q10-should-each-workflow-run-in-its-own-sandbox-container) + [OpenShell Deep Dive §17](deep_dives/openshell_deep_dive.md#17--what-to-lift-for-nemoclaw-escapades) — **Yes**; one orchestrator sandbox (always-on) + ephemeral per-workflow sandboxes |
| 11 | Which coding agent to run in OpenShell? Input/output contract? | M2 | **Phased approach:** M2 = Claude Code (fastest path, best code quality, but Anthropic-locked). M6+ = custom coding agent that lifts the best patterns from Claude Code (streaming tool execution, three-tier compaction, behavioral contract with transcript repair, prompt cache boundary), OpenClaw (Pi's 20+ tools, `apply_patch`, block streaming, tool profiles), and Hermes (provider resolver, context compression, concurrent tool execution). Custom agent is Python, model-agnostic (`inference.local`), and on-policy for the Nemotron training flywheel. I/O contract is agent-agnostic: seed workspace → task via NMB → results via NMB → cleanup. See [Orchestrator Design](orchestrator_design.md) for the detailed agent loop architecture. |
| 12 | Can the review agent and coding agent collaborate locally without Git in the loop? | M3 | [Hermes §13](deep_dives/hermes_deep_dive.md#13--sub-agent-delegation) + [OpenShell §17](deep_dives/openshell_deep_dive.md#17--what-to-lift-for-nemoclaw-escapades) — Hermes sub-agent delegation + OpenShell multi-sandbox architecture enables local coordination |
| 13 | How can we access Teams for the note-taking system? | M5 | |
| 14 | How to add another server backend to the Slack integration? | M1 | [Hermes §11](deep_dives/hermes_deep_dive.md#11--messaging-gateway) + [NemoClaw §13](deep_dives/nemoclaw_deep_dive.md#13--relationship-to-openclaw) — Hermes gateway uses platform adapter pattern; NemoClaw adds Telegram bridge |
| 15 | What are formal sources on harness engineering? | — | [OpenShell blog](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/) — OpenShell is the definitive example of agent harness engineering (out-of-process policy enforcement) |
| 16 | Does this project cleanly separate work vs. hobby, or does it mix them? (2nd Brain = personal; else = professional) | M4 | [Hermes §18-Q5](deep_dives/hermes_deep_dive.md#q5-how-does-honcho-combine-with-secondbrain-should-we-use-one-or-both) — proposed 3-layer memory separation |
| 17 | Where should the NemoClaw Escapades agent be hosted for always-on operation? | M1 | [Hosting Deep Dive §9](deep_dives/hosting_deep_dive.md#9--recommended-architecture) — **Brev** for cloud, **DGX Spark** for local; start local, deploy to Brev at M1 |
| 18 | What NVIDIA infrastructure options exist for hosting persistent agent workloads? | M1 | [Hosting Deep Dive §3–§6](deep_dives/hosting_deep_dive.md#3--option-1-nvidia-brev-recommended) — Brev (recommended), DGX Spark, Remote SSH, Base Command Platform |
| 19 | Should the NMB be implemented as part of Milestone 1 (foundation) or Milestone 2 (coding agent)? | M1/M2 | [NMB Design](nmb_design.md) — NMB is most valuable for M2+ (multi-sandbox coordination), but the broker is simple enough to deploy with M1 |
| 20 | Should we propose `messages.local` as an upstream OpenShell feature? | M2 | [NMB Design §15](nmb_design.md#15--future-upstream-contribution-to-openshell) — If NMB proves valuable, contributing the pattern upstream eliminates the standalone broker |
| 21 | For the NMB broker, should we start with a custom Python asyncio server or use NATS from the beginning? | M1 | [NMB Design §4](nmb_design.md#4--message-broker) — Custom for v1 (minimal dependencies), NATS for v2 if production hardness needed |
| 22 | Should NemoClaw adopt a feature flag system for progressive rollout of capabilities? | M1 | Claude Code uses GrowthBook (remote) + Bun's `feature()` (build-time dead code elimination) to gate 90+ features. A similar system would let NemoClaw ship experimental features (voice input, browser automation, advanced coordinator modes) behind flags without destabilizing the core. Options: LaunchDarkly, Unleash, or a simple config-file-based system for v1. |

## 6  Blog Post Series

The series starts with an introduction post, then each milestone produces a
corresponding blog post. Posts should be mostly auto-generated by the system
itself (once capable), then reviewed and revised by the author. All posts must
list sources and references (including Hermes & OpenClaw). Every
post should explicitly include:
- What we are intending to build.
- Learning objectives for that milestone.
- Milestone deliverables and acceptance criteria.

| # | Title (working) | Milestone | Draft |
|---|-----------------|-----------|-------|
| 0 | **Building Agents from Scratch — Series Introduction** | — | [Intro post](blog_posts/series_introduction/series_introduction.md) |
| 1 | **Building Our Own Agent: Local Orchestrator + NVIDIA Inference Hub** | M1 | [M1 post](blog_posts/m1/m1_setting_up_nemoclaw.md) |
| 2 | **Sandboxed Coding Agents with OpenShell** | M2 | TBD |
| 3 | **Adding a Review Agent: Local Collaboration Before Push** | M3 | TBD |
| 4 | **Giving the Agent a Memory: SecondBrain + Honcho Integration** | M4 | TBD |
| 5 | **Building a Professional Knowledge Base from Slack & Teams** | M5 | TBD |
| 6 | **The Self-Improvement Loop: Teaching the Agent to Learn** | M6 | TBD |

## 7  Capabilities the System Should Eventually Have

Captured from the original brainstorm (2026-02-24):

- Check Slack, Google Docs, Jira for issues, blockers, gaps, and bugs.
- Categorize & prioritize issues automatically.
- Create design docs & prototypes overnight.
- Produce SW prototypes, analyses, code cleanups, refactors.
- Generate roadmaps.
- Slack outreach — **only with explicit confirmation**.
- Project idea generation.
- **IDE integration via ACP** — Expose the NemoClaw orchestrator as an
  editor-native agent over stdio/JSON-RPC (the Agent Communication Protocol
  used by Hermes). This would let Cursor, VS Code, or other ACP-compatible
  editors talk directly to the orchestrator — triggering tasks, viewing
  sub-agent progress, and reviewing results without leaving the IDE. See
  [Hermes Deep Dive §16](deep_dives/hermes_deep_dive.md#16--acp-editor-integration)
  for how Hermes implements this.
- **Subscription model support** *(inspired by OpenClaw)* — Allow the
  inference backend to route requests through existing provider subscriptions
  (e.g. ChatGPT Plus, Claude Pro) in addition to raw API keys. OpenClaw
  supports this with auth profile rotation and model failover across
  providers. This reduces per-token costs for heavy workloads by leveraging
  flat-rate subscription tiers where available.
- **OpenClaw-style multi-agent management** *(inspired by OpenClaw)* — The
  orchestrator's multi-agent setup should adopt OpenClaw's more sophisticated
  coordination model. Key capabilities to lift:
  - **Per-agent configuration** via `agents.list[]` — each sub-agent declares
    its own workspace, sandbox policy, tool set, and model.
  - **Thread binding** — bind messaging threads to specific sub-agents (e.g.
    a Slack thread pinned to the coding agent, another to the research agent)
    to preserve context and avoid cross-talk.
  - **Depth limits** — cap how deep sub-agent delegation chains can go to
    prevent unbounded recursion.
  - **Concurrency caps** — limit the number of simultaneously running
    sub-agents to prevent resource exhaustion.
  - **Per-spawn overrides** — allow the orchestrator to override a sub-agent's
    default config (model, tools, timeout) at spawn time based on task needs.
  - **Shared budget model** *(from Hermes)* — complement the above with
    Hermes's approach of a shared token/cost budget across all sub-agents to
    prevent runaway spending.

  The NMB already moves in this direction by providing real-time inter-sandbox
  messaging. These multi-agent controls layer on top: NMB handles the
  communication plane, while the orchestrator enforces the coordination
  policies (depth, concurrency, budgets, thread affinity).

## 8  Design Principles

1. **Generic connectors** — Every external integration (Slack, Telegram, etc.)
   goes through an abstract connector base class.
2. **Generic inference backends** — The LLM backend is pluggable, similar to
   Hermes.
3. **Sandbox isolation** — Every workflow runs in its own OpenShell container.
4. **Safety by default** — Write operations require explicit confirmation (same
   philosophy as nv-tools).
5. **Incremental & documented** — Each milestone is self-contained and produces
   a blog post. The system documents its own development.
6. **Self-improvement** — The agent should learn from outcomes and refine its
   own skills and policies over time.
7. **Self-describing skills** — Skills declare not just agent-level
   requirements (tools, env vars) but also sandbox-level policy
   (`nemoclaw.infrastructure`). The orchestrator auto-generates OpenShell
   policies from skill metadata, eliminating manual policy authoring.
8. **Streaming-first tool execution** *(inspired by Claude Code)* — Tools
   execute *during* the model's streaming response, not after it completes.
   Concurrent-safe tools run in parallel. This cuts perceived latency by
   50%+ for multi-tool turns. See
   [Orchestrator Design §3](orchestrator_design.md#3--the-agent-loop).
9. **Defensive model output handling** *(inspired by Claude Code)* — The
   orchestrator never trusts LLM output structure. Malformed JSON falls back
   to `{}`, orphaned tool calls get synthetic placeholders, empty messages are
   filtered, and recovery prompts guide the model back on track. See
   [Orchestrator Design §9](orchestrator_design.md#9--model-behavioral-contract--defensive-llm-programming).

## 9  Web UI — Mission Control Dashboard

The primary interface is Slack (§3), but the system should also expose a locally
hosted web dashboard for deep observability, multi-agent orchestration, and
human-in-the-loop control. This section draws inspiration from two projects:

- **[Cline Kanban](https://cline.bot/kanban)** — Browser-based kanban board for
  orchestrating multiple coding agents in parallel. Key ideas: task cards backed
  by git worktrees, dependency chains, real-time diff viewer with inline
  comments, auto-commit/auto-PR, sidebar chat for board management.
- **[OpenClaw Studio](https://github.com/grp06/openclaw-studio)** — Open-source
  web dashboard for OpenClaw. Key ideas: WebSocket-powered live agent
  monitoring, approval gates, cron management UI, direct browser chat, multi-
  device access.

### 9.1  Design Goals

1. **Single pane of glass** — One URL to see everything the system is doing.
2. **Real-time** — WebSocket streaming, not polling. Agent state, logs, and
   outputs appear as they happen.
3. **Human-in-the-loop without bottlenecks** — Agents run autonomously by
   default; the UI surfaces only what needs human attention (approvals, errors,
   review requests).
4. **Multi-device** — Accessible from laptop, phone, or tablet via LAN or
   Tailscale. Responsive layout.
5. **Complementary to Slack** — The web UI is for deep work and oversight; Slack
   remains the quick-interaction and notification channel.

### 9.2  UI Surfaces

#### 9.2.1  Kanban Task Board

Inspired by Cline Kanban. The primary view of the dashboard.

```
┌─────────────────────────────────────────────────────────────────────┐
│  NemoClaw Mission Control                              [⚙] [🔔] [💬]  │
├─────────────┬──────────────┬──────────────┬─────────────────────────┤
│  QUEUED (3) │ RUNNING (4)  │ REVIEW (1)   │ DONE (12)               │
├─────────────┼──────────────┼──────────────┼─────────────────────────┤
│ ┌─────────┐ │ ┌──────────┐ │ ┌──────────┐ │ ┌─────────┐             │
│ │ Refactor│ │ │ Auth API │ │ │ Schema   │ │ │ Lint    │             │
│ │ logger  │ │ │ endpoint │ │ │ migration│ │ │ cleanup │ ✓           │
│ │         │ │ │ ■■■■░░░  │ │ │ needs    │ │ └─────────┘             │
│ │ blocks: │ │ │ Coding   │ │ │ sign-off │ │ ┌─────────┐             │
│ │ Auth API│ │ │ Agent    │ │ │ Review   │ │ │ Dep     │             │
│ └─────────┘ │ └──────────┘ │ │ Agent    │ │ │ upgrade │ ✓           │
│ ┌─────────┐ │ ┌──────────┐ │ └──────────┘ │ └─────────┘             │
│ │ Blog    │ │ │ Jira     │ │              │ ...                     │
│ │ post #3 │ │ │ triage   │ │              │                         │
│ └─────────┘ │ │ Research │ │              │                         │
│             │ │ Agent    │ │              │                         │
│             │ └──────────┘ │              │                         │
└─────────────┴──────────────┴──────────────┴─────────────────────────┘
```

**Features:**

| Feature | Inspiration | Description |
|---------|-------------|-------------|
| **Task cards** | Cline Kanban | Each card shows: agent type, progress, elapsed time, dependency links. Click to expand into detail view. |
| **Dependency chains** | Cline Kanban | Link tasks so completing one auto-starts the next. Visualize as directed arrows between cards. Blocked tasks show which predecessor they wait on. |
| **Ephemeral worktrees** | Cline Kanban | Each coding task runs in its own git worktree — full isolation, no merge conflicts between parallel agents. Worktrees are auto-cleaned on task completion. Gitignored deps (e.g. `node_modules`) are symlinked from the main repo. |
| **Drag-and-drop** | Standard kanban | Manually reprioritize the queue. Drag to "Trash" to cancel. |
| **Auto-commit & auto-PR** | Cline Kanban | Coding agents commit incrementally as they work. On completion, optionally auto-create a PR. Both toggleable in settings. |
| **Filters & search** | — | Filter by agent type, priority, tag, or date range. Full-text search across task descriptions and logs. |

#### 9.2.2  Live Agent Dashboard

Inspired by OpenClaw Studio. A real-time operations view.

- **Agent roster** — Shows every running agent: type, current tool call, sandbox
  container ID, uptime, resource usage (CPU/mem of OpenShell container).
- **Activity stream** — Chronological feed of agent actions, tool invocations,
  and outcomes. WebSocket-powered, no polling. Filterable by agent.
- **Thinking logs toggle** — Show or hide the LLM's chain-of-thought for any
  agent. Useful for debugging unexpected behavior.
- **Health indicators** — Green/yellow/red status for each subsystem:
  orchestrator, inference backend, sandbox layer, memory system, Slack connector.

#### 9.2.3  Diff Viewer & Code Review

Inspired by Cline Kanban's checkpoint-scoped diffs.

- **Per-task diff view** — Click any coding task card to see a full diff of
  changes in that worktree. Syntax-highlighted, side-by-side or unified.
- **Checkpoint scoping** — View diffs per commit / per agent step, not just the
  cumulative change. Useful for understanding what each iteration produced.
- **Inline commenting** — Click any diff line to leave a comment that gets
  routed back to the coding or review agent. The agent addresses the comment
  and the diff updates in real-time. This is the web UI equivalent of the
  "local collaboration" loop between coding and review agents (§3.1).
- **Review status** — Visual indicator: pending review, changes requested,
  approved. Maps to the review agent's output.

#### 9.2.4  Approval Gate Panel

Inspired by OpenClaw Studio's approval gates.

- **Pending approvals queue** — Dangerous operations (WRITE commands, external
  API calls, file deletions, Slack outreach, Gerrit submissions) pause and
  surface here for human review.
- **Context preview** — Each approval shows: which agent requested it, what
  exactly will happen, and a risk assessment.
- **One-click approve / reject** — With optional "always allow this pattern"
  to reduce friction for repeated safe operations.
- **Notification bridge** — Pending approvals also push to Slack so you can
  approve from your phone without opening the dashboard.
- **Audit log** — Every approval/rejection is logged with timestamp and reason.

#### 9.2.5  Scheduler & Cron View

Inspired by OpenClaw Studio's cron management.

- **Visual cron editor** — Create, edit, and toggle scheduled jobs with a
  calendar/timeline UI (no raw crontab editing).
- **Upcoming runs** — Timeline showing when each scheduled job will next fire.
- **Run history** — Past executions with status (success/failure), duration,
  and link to the task card / logs.
- **Quick actions** — Trigger any scheduled job manually ("run now"). Pause /
  resume individual schedules.

#### 9.2.6  Chat Interface

Inspired by OpenClaw Studio's browser chat + Cline Kanban's sidebar chat.

- **Sidebar chat** — Persistent chat panel (collapsible) for direct
  conversation with the orchestrator. Same capabilities as the Slack connector
  but with richer rendering (markdown, code blocks, embedded diffs).
- **Board-aware commands** — Natural language to manipulate the kanban board:
  "break this Jira ticket into three sub-tasks," "link the migration task to
  the API task," "start all queued coding tasks."
- **Agent-specific chat** — Click any running agent card and open a direct
  chat with that specific sub-agent for targeted guidance or course correction.
- **Conversation history** — Persisted to the memory system. Searchable.

#### 9.2.7  Memory & Self-Learning Inspector

Unique to NemoClaw — surfaces the self-improvement loop (§3.1, Milestone 6).

- **Skills inventory** — Browse all auto-created skills. See creation date,
  usage count, success rate, and the originating task.
- **Learning timeline** — Chronological view of lessons extracted from past
  tasks: what worked, what failed, what policy was updated.
- **Policy diff** — When the agent updates its own policies, show a before/after
  diff so the human can audit the change.
- **Memory search** — Query the Honcho + SecondBrain memory layers directly
  from the dashboard.

#### 9.2.8  Knowledge Base Browser

Surfaces SecondBrain and the professional KB (Milestones 2, 6).

- **Unified search** — Single search bar that queries both personal (2nd Brain)
  and professional (distilled Slack/Teams) knowledge bases.
- **Knowledge graph view** — Interactive visualization of the Neo4j graph from
  SecondBrain. Explore connections between concepts, notes, and sources.
- **Spaced repetition queue** — If the user has active review cards (FSRS),
  surface them as a widget on the dashboard.
- **Ingestion status** — Shows pending ingestion jobs (PDFs, web pages, code)
  and their progress.

### 9.3  Technical Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         Browser (SPA)                              │
│  React + TailwindCSS + shadcn/ui                                   │
│  ┌───────────┬──────────┬────────────┬──────────┬───────────────┐  │
│  │  Kanban   │  Agent   │   Diff     │ Approval │  Scheduler    │  │
│  │  Board    │  Dash    │   Viewer   │  Gates   │  / Cron       │  │
│  └─────┬─────┴────┬─────┴─────┬──────┴────┬─────┴──────┬────────┘  │
│        └───────────┴───────────┴───────────┴────────────┘          │
│                            │ WebSocket + REST                      │
└────────────────────────────┼───────────────────────────────────────┘
                             │
┌────────────────────────────┼───────────────────────────────────────┐
│                    Dashboard Backend                               │
│              (FastAPI or Next.js API routes)                       │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │  WS Hub      │  │  REST API    │  │  Event Bus               │  │
│  │  (agent      │  │  (CRUD for   │  │  (broadcasts agent       │  │
│  │   streams)   │  │   tasks,     │  │   events to all          │  │
│  │              │  │   approvals) │  │   connected clients)     │  │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬──────────────┘  │
│         └─────────────────┴──────────────────────┘                 │
│                            │                                       │
└────────────────────────────┼───────────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              │   Orchestrator Agent (§3)   │
              │   (existing core system)    │
              └─────────────────────────────┘
```

**Key decisions:**

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Frontend framework | React + Vite | Fast dev cycle, huge ecosystem, compatible with SecondBrain's existing React frontend. |
| Component library | shadcn/ui + TailwindCSS | Modern, accessible, highly customizable. Consistent dark-mode support. |
| Real-time transport | WebSocket (via Socket.IO or native WS) | Required for live agent streaming. REST as fallback for CRUD. |
| Dashboard backend | FastAPI (Python) | Matches the orchestrator's Python stack. Lightweight, async-native, easy WebSocket support. |
| Deployment | Local-first | Runs on `localhost:3000`. Optional Tailscale exposure for remote/mobile access. |

### 9.4  UX Principles

1. **Calm by default, detailed on demand** — The board shows high-level status.
   Details (logs, diffs, thinking traces) are one click away, not in your face.
2. **Notification, not interruption** — Badge counts and toast notifications for
   events that need attention. No modal pop-ups blocking the view.
3. **Keyboard-first** — Shortcuts for common actions: `C` to create task,
   `Cmd+Click` to link, `Esc` to close panels. Power users should never need
   the mouse.
4. **Dark mode as default** — Developers live in dark mode. Light mode available
   but not prioritized.
5. **Progressive disclosure** — New users see the kanban board and chat.
   Advanced surfaces (memory inspector, knowledge graph, cron editor) are
   accessible via nav but not overwhelming on first visit.
6. **Mobile-responsive** — Task board collapses to a vertical list on narrow
   screens. Approval gates are fully functional on mobile (approve from phone
   via Tailscale).

### 9.5  Milestone Mapping

The web UI is not a standalone milestone — it evolves alongside the core system.

| Core Milestone | Web UI Additions |
|----------------|------------------|
| M1 — Foundation | Chat interface, basic agent dashboard, health indicators |
| M2 — Coding Agent | Kanban board, diff viewer, worktree management, auto-commit/PR |
| M3 — Review Agent | Inline commenting on diffs, review status indicators |
| M4 — Memory orchestration | Memory routing view, working / user / knowledge tier indicators |
| M5 — Knowledge capture | KB browser, Slack/Teams ingestion dashboard, search, ingestion status |
| M6 — Self-Learning Loop | Memory inspector, skills inventory, learning timeline, policy diffs |
| — (cross-cutting) | Approval gates, scheduler view, notification bridge |

### 9.6  Open Questions (Web UI)

| # | Question | Notes |
|---|----------|-------|
| W1 | Should the dashboard backend be a separate process or embedded in the orchestrator? | Separate is cleaner but adds deployment complexity. |
| W2 | Can we reuse SecondBrain's existing React frontend as a starting point? | Same stack (React + FastAPI). Could share components. |
| W3 | How to handle auth for the web UI? | Local-only = no auth needed. Tailscale = identity via Tailscale ACLs. Open internet = needs auth layer. |
| W4 | Should we adopt OpenClaw Studio directly and extend it, or build from scratch? | Studio is Next.js; our stack leans FastAPI + React. Evaluate effort to fork vs. build. |
| W5 | How granular should the approval gate policies be? Per-agent? Per-tool? Per-target? | Start coarse (per-operation-type), refine based on usage. |

## 10  Future Work — Features Inspired by Claude Code

The [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) revealed
several production features that are worth incorporating into NemoClaw's
roadmap. These are not assigned to specific milestones yet but should be
considered as the system matures.

### High Priority (incorporate as soon as practical)

| Feature | Source | Description | Likely Milestone |
|---------|--------|-------------|-----------------|
| **Three-tier context compaction** | Claude Code `compact/` | Micro-compaction (~256 tokens, no API call), full compaction (~4K tokens, LLM summary), session memory (zero-cost in-memory key-fact cache). Essential for long-running conversations. | M2 |
| **Cache-aware system prompt** | Claude Code `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` | Split system prompt into static prefix (cached) and dynamic suffix (per-turn). Reduces cost by ~90% on subsequent turns via provider prompt caching. | M2 |
| **Prompt cache break detection** | Claude Code `promptCacheBreakDetection.ts` | Monitor whether the system prompt's static prefix changed between turns; log warnings when cache effectiveness drops | M2 |
| **Proactive tick system** | Claude Code `KAIROS` / `PROACTIVE` flags | Periodic `<proactive_tick>` events for always-on daemon behavior; check for pending Slack messages, cron jobs, stalled tasks | M1 |
| **ToolSearch (deferred loading)** | Claude Code `ToolSearchTool` | As tool count grows (built-in + MCP + plugins), lazy-load tool definitions on demand to keep prompt size manageable | M2 |
| **`batch` skill pattern** | Claude Code bundled skill | Research → decompose → distribute across worktree agents → verify → track; essential for large multi-file tasks | M2 |
| **`verify` skill pattern** | Claude Code bundled skill | "Prove it works" workflow that pushes the model toward real validation (run the app, check CLI output) rather than static reasoning | M2 |
| **IDE bridge system** | Claude Code `bridge/` + `BRIDGE_MODE` | Bidirectional VS Code / JetBrains integration; adopt for ACP integration (§7) | M2+ |

### Medium Priority (nice to have)

| Feature | Source | Description |
|---------|--------|-------------|
| **Copy-on-write speculation** | Claude Code | Pre-compute next response on overlay filesystem for fast session switching |
| **Team memory sync** | Claude Code `teamMemorySync/` | Shared memory across agent teams; relevant when multiple sub-agents collaborate on related tasks |
| **Browser automation tool** | Claude Code `WEB_BROWSER_TOOL` + `claude-in-chrome` skill | Programmatic browser automation beyond MCP-based Chrome integration |
| **Feature flag system** | Claude Code GrowthBook + `bun:bundle` | Progressive rollout of experimental features behind runtime/build-time flags (see Q22) |
| **Session forking** | Claude Code `FORK_SUBAGENT` | Fork current session context into a sub-agent via NMB `task.fork` (see [NMB Design §14](nmb_design.md#14--coordinator-integration--extended-message-types)) |

### Lower Priority (aspirational)

| Feature | Source | Description |
|---------|--------|-------------|
| **Voice input** | Claude Code `VOICE_MODE` | Voice-to-text input for the orchestrator; interesting for mobile/hands-free use |
| **Desktop/mobile handoff** | Claude Code `/desktop`, `/mobile` commands | Seamless session transfer between devices; NemoClaw's Slack-first approach already handles this partially |
| **Frustration detection** | Claude Code `useFrustrationDetection.ts` | Detect user frustration via regex patterns; trigger feedback surveys or adjust agent behavior |

### Explicitly not adopting

| Feature | Reason |
|---------|--------|
| **Single-provider lock-in** | NemoClaw's multi-provider design (Inference Hub, Anthropic, OpenAI, custom) is intentionally more flexible |
| **Terminal-only interface** | NemoClaw's Slack + Web UI + future IDE integration is more accessible for an always-on agent |
| **JSON file sessions** | NemoClaw uses SQLite from the start (matching Hermes) for searchability and concurrent access |
| **Bun runtime** | NemoClaw is Python-based; the streaming architecture translates via `async for` generators |

---

## 11  References & Related Projects

| Project | Repo | Relevance |
|---------|------|-----------|
| **NemoClaw** | [NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw) | Setup harness for deploying vanilla OpenClaw into OpenShell (Apache 2.0, alpha). Plugin (TypeScript) + Blueprint (Python) architecture. Contains no agent intelligence — it's a setup wizard, not an agent. **Studied for policy patterns and blueprint architecture but not used in this project's stack** (see [§3.3](#33--why-nemoclaw-is-not-in-this-architecture)). |
| **OpenShell** | [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) | NVIDIA's secure runtime for autonomous AI agents. Four core components: Gateway (control plane), Sandbox (isolated execution), Policy Engine (defense-in-depth), Privacy Router (inference routing). Agent-agnostic — supports Claude Code, OpenClaw, Codex, and custom agents. Apache 2.0 license. |
| **OpenClaw** | [openclaw/openclaw](https://github.com/openclaw/openclaw) | Primary reference architecture. Multi-channel personal AI assistant with sandbox execution (Docker/Podman), skills system, cron scheduling, multi-agent routing, and Canvas UI. Supports 20+ messaging platforms. MIT license. |
| **Hermes Agent** | [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) | Self-improving agent with closed learning loop. Key subsystems to study: `skills/` (auto-created procedural memory), `honcho_integration/` (managed memory via Honcho), `cron/` (scheduled automations), `agent/` (core agent loop), `gateway/` (multi-platform messaging), `tools/` (40+ built-in tools). Six terminal backends including Docker and serverless (Modal, Daytona). MIT license. |
| **SecondBrain** | [dpickem/project_second_brain](https://github.com/dpickem/project_second_brain) | Own project — personal knowledge management system. Provides the knowledge storage, retrieval, and learning infrastructure that NemoClaw Escapades will integrate as its memory/KB layer. Key subsystems: ingestion pipelines, Neo4j knowledge graph, FSRS spaced repetition, LLM-powered summarization, REST API (`/api/knowledge/*`, `/api/assistant/*`). MIT license. |
| **Honcho** | [plastic-labs/honcho](https://github.com/plastic-labs/honcho) | User-modeling and memory system used by Hermes. Candidate for the persistent memory layer in Milestone 5. |
| **NVIDIA Brev** | [brev.nvidia.com](https://brev.nvidia.com/) | GPU-accelerated cloud platform for agent hosting. Supports always-on instances, serverless deployments, and native NemoClaw deployment (`nemoclaw deploy`). Recommended hosting for this project. |
| **Cline Kanban** | [cline.bot/kanban](https://cline.bot/kanban) | Browser-based kanban board for multi-agent orchestration. Key UI patterns adopted in §9: task cards with dependency chains, ephemeral git worktrees, checkpoint-scoped diff viewer with inline commenting, auto-commit/auto-PR, sidebar chat for board management. Agent-agnostic. |
| **OpenClaw Studio** | [grp06/openclaw-studio](https://github.com/grp06/openclaw-studio) | Open-source web dashboard for OpenClaw (1.8k stars). Key UI patterns adopted in §9: WebSocket-powered live agent monitoring, approval gates, cron management UI, direct browser chat, multi-device access via Tailscale. Next.js + Gateway architecture. |
| **VibeClaw** | [jasonkneen/vibeclaw](https://github.com/jasonkneen/vibeclaw) | Browser-based OpenClaw interface with sandbox mode (run agents in-browser) and live gateway mode. Useful reference for zero-install onboarding experience. |
| **Claude Code** | [zackautocracy/claude-code](https://github.com/zackautocracy/claude-code) (source), [instructkr/claw-code](https://github.com/instructkr/claw-code) (rewrite), [thtskaran/claude-code-analysis](https://github.com/thtskaran/claude-code-analysis) (analysis) | Anthropic's terminal-native coding assistant. Source leaked March 2026 via `.map` file. 1,884 TS files, 40+ tools, 90 feature flags. Studied for agent loop, compaction, permission, and multi-agent patterns. **Proprietary — not used directly but patterns adopted.** |

### Deep Dives

- **[Hermes Agent Deep Dive](deep_dives/hermes_deep_dive.md)** — architecture,
  components, self-learning loop, memory system, setup, and answers to all
  Hermes-related open questions from §5.
- **[NemoClaw Deep Dive](deep_dives/nemoclaw_deep_dive.md)** — plugin/blueprint
  architecture, sandbox lifecycle, inference routing, network policy, CLI
  reference, deployment modes, and answers to NemoClaw-related questions from §5.
- **[OpenShell Deep Dive](deep_dives/openshell_deep_dive.md)** — core components
  (gateway, sandbox, policy engine, privacy router), request flow, defense-in-depth
  enforcement, policy schema, community sandboxes, IDE integration, and comparison
  with Hermes terminal backends.
- **[Hosting & Infrastructure Deep Dive](deep_dives/hosting_deep_dive.md)** —
  NVIDIA Brev, DGX Spark, remote SSH, Base Command Platform; cost analysis,
  recommended architecture phases, and where the core agent loop runs.
- **[Hermes vs OpenClaw Comparison](deep_dives/hermes_vs_openclaw_comparison.md)** —
  side-by-side comparison of architecture, skills, memory, sandboxing,
  self-learning, and a per-milestone lift strategy for NemoClaw Escapades.
- **[Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md)** — leaked
  source analysis: async generator agent loop, 40+ tools with 5-layer filtering,
  three-tier compaction, two-stage YOLO classifier, prompt cache boundary,
  security architecture (4,437-line bash parser, NO_TOOLS sandwich), daemon mode,
  proactive agent, bundled skills, model behavioral contract, 90 feature flags,
  and hidden features behind build-time dead code elimination.

### System Designs

- **[Training Flywheel Design](training_flywheel_deep_dive.md)** —
  turning daily agent interactions into SFT and RL training data; trace capture,
  quality filtering, DPO preference pairs, Nemotron fine-tuning pipeline, and
  the compound improvement loop (runtime self-learning + model weight adaptation).
- **[NemoClaw Message Bus (NMB) Design](nmb_design.md)** — real-time
  inter-sandbox messaging: broker architecture, wire protocol, client library
  API, security model, failure modes, deployment, coordinator integration,
  session forking, and peer discovery.
- **[Orchestrator Agent Design](orchestrator_design.md)** — agent loop
  architecture (streaming-first async generator), system prompt construction
  with cache boundary, tool system with 5-layer filtering, coordinator mode
  for multi-agent orchestration, permission system with tiered auto-approval,
  three-tier session compaction, model behavioral contract with transcript
  repair, task store, and proactive agent tick.

### Key Documentation Links

**NemoClaw:**
- **NemoClaw docs:** [docs.nvidia.com/nemoclaw](https://docs.nvidia.com/nemoclaw/latest/)
- **NemoClaw architecture:** [docs.nvidia.com/nemoclaw/latest/reference/architecture.html](https://docs.nvidia.com/nemoclaw/latest/reference/architecture.html)
- **NemoClaw quickstart:** [docs.nvidia.com/nemoclaw/latest/get-started/quickstart.html](https://docs.nvidia.com/nemoclaw/latest/get-started/quickstart.html)
- **NemoClaw commands:** [docs.nvidia.com/nemoclaw/latest/reference/commands.html](https://docs.nvidia.com/nemoclaw/latest/reference/commands.html)
- **NemoClaw remote deploy:** [docs.nvidia.com/nemoclaw/latest/deployment/deploy-to-remote-gpu.html](https://docs.nvidia.com/nemoclaw/latest/deployment/deploy-to-remote-gpu.html)

**OpenShell:**
- **OpenShell docs:** [docs.nvidia.com/openshell](https://docs.nvidia.com/openshell/latest/)
- **OpenShell architecture:** [docs.nvidia.com/openshell/latest/about/architecture.html](https://docs.nvidia.com/openshell/latest/about/architecture.html)
- **OpenShell sandboxes:** [docs.nvidia.com/openshell/latest/sandboxes/manage-sandboxes.html](https://docs.nvidia.com/openshell/latest/sandboxes/manage-sandboxes.html)
- **OpenShell policies:** [docs.nvidia.com/openshell/latest/sandboxes/policies.html](https://docs.nvidia.com/openshell/latest/sandboxes/policies.html)
- **OpenShell blog:** [developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/)

**Hosting & Infrastructure:**
- **NVIDIA Brev:** [brev.nvidia.com](https://brev.nvidia.com/)
- **Brev docs:** [docs.nvidia.com/brev](https://docs.nvidia.com/brev/latest/)
- **DGX Spark + agents blog:** [developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark/](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark/)

**Hermes:**
- **Hermes architecture:** [hermes-agent.nousresearch.com/docs/developer-guide/architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- **Hermes skills system:** [hermes-agent.nousresearch.com/docs/user-guide/features/skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- **Hermes memory:** [hermes-agent.nousresearch.com/docs/user-guide/features/memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- **Hermes cron:** [hermes-agent.nousresearch.com/docs/user-guide/features/cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)

**OpenClaw:**
- **OpenClaw vision:** [github.com/openclaw/openclaw/blob/main/VISION.md](https://github.com/openclaw/openclaw/blob/main/VISION.md)
- **OpenClaw skills:** [github.com/openclaw/openclaw/tree/main/skills](https://github.com/openclaw/openclaw/tree/main/skills)
- **OpenClaw sandbox:** [github.com/openclaw/openclaw/blob/main/Dockerfile.sandbox](https://github.com/openclaw/openclaw/blob/main/Dockerfile.sandbox)

**SecondBrain:**
- **SecondBrain API:** [github.com/dpickem/project_second_brain/tree/main/backend](https://github.com/dpickem/project_second_brain/tree/main/backend)
- **SecondBrain design docs:** [github.com/dpickem/project_second_brain/tree/main/docs](https://github.com/dpickem/project_second_brain/tree/main/docs)
