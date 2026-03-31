# Hermes vs OpenClaw — Comparative Deep Dive

> **Sources:**
> [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) (17k stars, MIT, v0.5.0) &nbsp;|&nbsp;
> [openclaw/openclaw](https://github.com/openclaw/openclaw) (341k stars, MIT, 2026.3.28)
>
> **Last reviewed:** 2026-03-29
>
> **Companion docs:**
> [Hermes Deep Dive](hermes_deep_dive.md) &nbsp;|&nbsp;
> [OpenClaw Deep Dive](openclaw_deep_dive.md)

---

## Table of Contents

1. [Executive Summary](#1--executive-summary)
2. [Philosophy & Design Goals](#2--philosophy--design-goals)
3. [Architecture Comparison](#3--architecture-comparison)
4. [Agent Loop / Runtime](#4--agent-loop--runtime)
5. [Inference & Provider System](#5--inference--provider-system)
6. [Tools & Plugins](#6--tools--plugins)
7. [Skills System](#7--skills-system)
8. [Memory & Context](#8--memory--context)
9. [Sandboxing & Execution](#9--sandboxing--execution)
10. [Sub-Agents & Multi-Agent](#10--sub-agents--multi-agent)
11. [Cron & Scheduling](#11--cron--scheduling)
12. [Messaging & Channels](#12--messaging--channels)
13. [Security Model](#13--security-model)
14. [Self-Learning Loop](#14--self-learning-loop)
15. [Companion Apps & UI](#15--companion-apps--ui)
16. [Ecosystem & Community](#16--ecosystem--community)
17. [Feature Matrix](#17--feature-matrix)
18. [Implications for NemoClaw Escapades](#18--implications-for-nemoclaw-escapades)

---

## 1  Executive Summary

Hermes and OpenClaw occupy the same category — open-source personal AI
assistants with sandbox execution and multi-channel messaging — but they
optimize for fundamentally different things.

```
┌───────────────────────────────────────────────────────────────────────┐
│                     Core Identity                                     │
│                                                                       │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐   │
│  │  HERMES                      │  │  OPENCLAW                    │   │
│  │  "The self-improving agent"  │  │  "The complete assistant     │   │
│  │                              │  │   platform"                  │   │
│  │  Optimizes for:              │  │                              │   │
│  │  • Learning from experience  │  │  Optimizes for:              │   │
│  │  • Memory that deepens       │  │  • Breadth of integrations   │   │
│  │  • Skills created at runtime │  │  • Product polish (native    │   │
│  │  • Flexible inference        │  │    apps, Canvas, voice)      │   │
│  │    backends                  │  │  • Plugin ecosystem          │   │
│  │                              │  │  • Multi-device experience   │   │
│  │  Written in: Python (92%)    │  │                              │   │
│  │  Stars: 17k                  │  │  Written in: TypeScript (89%)│   │
│  │  License: MIT                │  │  Stars: 341k                 │   │
│  │                              │  │  License: MIT                │   │
│  └──────────────────────────────┘  └──────────────────────────────┘   │
│                                                                       │
│  Where they converge:                                                 │
│  • AgentSkills standard (SKILL.md)                                    │
│  • Gateway-centric architecture                                       │
│  • Multi-platform messaging                                           │
│  • Cron scheduling                                                    │
│  • Sub-agent delegation                                               │
│  • Any-LLM provider support                                           │
│                                                                       │
│  Where they diverge:                                                  │
│  • Self-learning (Hermes has it, OpenClaw doesn't)                    │
│  • Native apps (OpenClaw has them, Hermes doesn't)                    │
│  • OpenShell integration (OpenClaw native, Hermes not integrated)     │
│  • Memory depth (Hermes 3-layer + Honcho, OpenClaw file injection)    │
│  • RL/training pipeline (Hermes has it, OpenClaw doesn't)             │
└───────────────────────────────────────────────────────────────────────┘
```

**Bottom line for NemoClaw Escapades:** Use OpenClaw as the structural
reference (Gateway pattern, OpenShell integration, plugin architecture) and
Hermes as the intelligence reference (self-learning loop, memory system,
skills auto-creation). The two are complementary, not competing.

---

## 2  Philosophy & Design Goals

| Dimension | Hermes | OpenClaw |
|-----------|--------|----------|
| **Primary audience** | Power users, researchers, self-hosters | Broad consumer + developer audience |
| **Core thesis** | An agent should learn from experience and get better over time | An agent should be a complete personal assistant platform |
| **Design surface** | CLI-first, messaging as extension | Multi-surface: CLI, native apps, Canvas, WebChat, 25+ channels |
| **Extensibility model** | External skill dirs, MCP tools | Full plugin architecture (channels, tools, skills, providers, speech, image) |
| **Deployment model** | Single Python process, any $5 VPS | Node.js daemon, supervised by launchd/systemd |
| **RL / training** | First-class (environments, trajectories, Atropos integration) | None |
| **User modeling** | Deep (Honcho dialectic reasoning across sessions) | Minimal (context file injection) |
| **OpenShell support** | Not integrated (uses own terminal backends) | Native backend since v2026.3 |

### What Each Gets Right

**Hermes excels at:**
- Making the agent genuinely improve over time (the closed learning loop)
- Deep user modeling via Honcho's dual-peer dialectic architecture
- Providing the most flexible inference backend system (any OpenAI-compatible endpoint)
- RL infrastructure for training next-gen tool-calling models

**OpenClaw excels at:**
- Product completeness (native apps, Canvas, voice wake, device nodes)
- Broadest messaging coverage (25+ platforms vs Hermes's 6)
- OpenShell-native sandboxing with mirror/remote workspace modes
- Plugin ecosystem with a proper package registry (ClawHub)
- Multi-agent routing with per-agent workspaces and sandbox policies

---

## 3  Architecture Comparison

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Architecture Comparison                            │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  Entry Points        │            │  User Interfaces     │         │
│  │  • CLI TUI           │            │  • 25+ channels      │         │
│  │  • Gateway (6 plats) │            │  • macOS/iOS/Android │         │
│  │  • ACP (editor)      │            │  • WebChat           │         │
│  │  • Batch Runner      │            │  • Canvas (A2UI)     │         │
│  └──────────┬───────────┘            └──────────┬───────────┘         │
│             │                                   │                     │
│             ▼                                   ▼                     │
│  ┌───────────────────────┐            ┌──────────────────────┐        │
│  │  AIAgent (Python)     │            │  Gateway Daemon      │        │
│  │  • Single class       │            │  (Node.js, WS control│        │
│  │  • Prompt builder     │            │   plane)             │        │
│  │  • Provider resolver  │            │  • Channel adapters  │        │
│  │  • Tool dispatcher    │            │  • Session router    │        │
│  │  • Context compressor │            │  • Plugin loader     │        │
│  │  • Session persistence│            │  • Node manager      │        │
│  └──────────┬────────────┘            └──────────┬───────────┘        │
│             │                                   │                     │
│             ▼                                   ▼                     │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  Tools Runtime       │            │  Pi Agent Runtime    │         │
│  │  • 40+ built-in      │            │  (RPC mode)          │         │
│  │  • MCP tools         │            │  • System prompt     │         │
│  │  • Skills tools      │            │    builder           │         │
│  │  • Memory tools      │            │  • Provider selection│         │
│  │  • Honcho tools      │            │  • Tool executor     │         │
│  └──────────┬───────────┘            └──────────┬───────────┘         │
│             │                                   │                     │
│             ▼                                   ▼                     │
│  ┌──────────────────────┐            ┌───────────────────────┐        │
│  │  Terminal Backends   │            │  Sandbox Backends     │        │
│  │  • local             │            │  • Docker (default)   │        │
│  │  • docker            │            │  • SSH (remote)       │        │
│  │  • ssh               │            │  • OpenShell (managed)│        │
│  │  • daytona           │            │                       │        │
│  │  • singularity (HPC) │            │  Workspace modes:     │        │
│  │  • modal (serverless)│            │  • mirror (sync each) │        │
│  │                      │            │  • remote (seed once) │        │
│  └──────────────────────┘            └───────────────────────┘        │
│                                                                       │
│  KEY STRUCTURAL DIFFERENCE:                                           │
│                                                                       │
│  Hermes: AIAgent is a self-contained Python class. Every entry point  │
│  instantiates one. The agent owns its own session persistence and     │
│  compression. The Gateway is one of several entry points.             │
│                                                                       │
│  OpenClaw: The Gateway IS the system. It's a long-lived daemon that   │
│  owns all state, messaging, and control plane. Pi (the agent runtime) │
│  is invoked by the Gateway via RPC. The Gateway is not optional.      │
└───────────────────────────────────────────────────────────────────────┘
```

### Architectural Trade-offs

| Concern | Hermes | OpenClaw | Implication |
|---------|--------|----------|-------------|
| **Coupling** | Agent is loosely coupled to entry points. Can run standalone. | Pi is tightly coupled to Gateway. Cannot run without it. | Hermes is easier to embed; OpenClaw is more cohesive as a product. |
| **State ownership** | Agent owns SQLite + local files | Gateway owns sessions + WS state | Hermes state is portable; OpenClaw state is centralized. |
| **Scaling** | Single process, in-process sub-agents | Single daemon, sub-agents via session spawning | Neither scales horizontally out of the box. |
| **Protocol** | Direct function calls + callbacks | Typed WebSocket JSON-RPC | OpenClaw's protocol enables remote clients (apps, WebChat). |
| **Extensibility** | MCP tools, external skill dirs | Full plugin system (channels, tools, skills, providers) | OpenClaw has a richer extension surface. |

---

## 4  Agent Loop / Runtime

Both systems implement a similar core loop (prompt → LLM → tool calls →
loop), but with important differences in how they manage context and
state.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Agent Loop Comparison                              │
│                                                                       │
│  HERMES (run_agent.py::AIAgent)       OPENCLAW (Pi agent runtime)     │
│                                                                       │
│  1. Generate task ID                  1. Receive message from Gateway │
│  2. Append user message               2. Build system prompt          │
│  3. Load/build cached system prompt      (AGENTS.md + SOUL.md +       │
│     (frozen snapshot at session          TOOLS.md + IDENTITY.md +     │
│      start; changes take effect          USER.md + HEARTBEAT.md +     │
│      next session)                       skills XML + context files)  │
│  4. Maybe preflight-compress          3. Resolve model provider       │
│     (if context is large)                + fallback chain             │
│  5. Build API messages +              4. Stream to LLM                │
│     ephemeral prompt layers              (block streaming for tools)  │
│  6. Apply prompt caching              5. Execute tool calls           │
│     (provider-specific)               6. Loop until final response    │
│  7. Interruptible API call            7. Deliver response to Gateway  │
│  8. Execute tools (seq or conc)                                       │
│  9. Loop until final text             Differences:                    │
│  10. Persist session + cleanup        • No context compression        │
│                                       • No prompt caching             │
│  Unique to Hermes:                    • No iteration budget           │
│  • Context compression mid-convo      • Block streaming (not just     │
│  • Provider-specific prompt caching     text streaming)               │
│  • Budget tracking across agents      • Thinking level control        │
│  • Fallback model on primary fail       (off → xhigh)                 │
│  • Session lineage across splits      • Media pipeline (images,       │
│                                         audio, video)                 │
└───────────────────────────────────────────────────────────────────────┘
```

### System Prompt Assembly

| Component | Hermes | OpenClaw |
|-----------|--------|----------|
| Agent instructions | System prompt (built by `prompt_builder.py`) | `AGENTS.md` |
| Persona / identity | Embedded in system prompt | `SOUL.md` + `IDENTITY.md` |
| User profile | `USER.md` (bounded, ~500 tokens) | `USER.md` (unbounded) |
| Agent memory | `MEMORY.md` (bounded, ~800 tokens) | — (no equivalent) |
| Tool guidance | Inline in system prompt | `TOOLS.md` |
| Time awareness | — | `HEARTBEAT.md` |
| First-run bootstrap | — | `BOOTSTRAP.md` |
| Skills | Progressive disclosure (list only in prompt, load on demand) | Full compact XML list injected into prompt |
| Cross-session context | Honcho dialectic summary (auto-injected) | — |

**Key insight:** Hermes is more token-efficient (bounded memory files,
progressive skill disclosure, context compression), while OpenClaw is more
explicit (everything injected upfront, no compression). Hermes's approach
scales better for long sessions and large skill inventories.

---

## 5  Inference & Provider System

Both support multiple LLM providers, but the configuration and fallback
mechanisms differ.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Provider Comparison                                │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  Config: config.yaml                 Config: openclaw.json            │
│                                                                       │
│  providers:                          agent:                           │
│    openrouter:                         model: "anthropic:claude-..."  │
│      api_key: ...                      imageModel: "..."              │
│    openai:                                                            │
│      api_key: ...                    + per-agent model overrides      │
│    anthropic:                        + per-spawn model overrides      │
│      api_key: ...                    + auth profile rotation          │
│    custom:                           + subscription model support     │
│      base_url: https://...                                            │
│      api_key: ...                                                     │
│                                                                       │
│  Resolution chain:                   Resolution chain:                │
│  1. Explicit provider prefix         1. Per-spawn override            │
│  2. Model → provider mapping         2. Per-agent override            │
│  3. Base URL heuristics              3. Global agent.model            │
│  4. Fallback to default              4. Failover chain                │
│                                                                       │
│  API modes:                          API modes:                       │
│  • chat_completions (OpenAI compat)  • OpenAI                         │
│  • codex_responses (Codex/Responses) • Anthropic (native)             │
│  • anthropic_messages (native Claude)• Google (native)                │
│                                      • OpenRouter                     │
│                                      • Custom endpoint                │
│                                                                       │
│  Unique features:                    Unique features:                 │
│  • Any base_url (inference hub!)     • Subscription model support     │
│  • Provider-specific prompt caching  • Auth profile rotation          │
│  • Fallback model on primary fail    • Per-agent + per-spawn models   │
│  • Budget tracking across agents     • Thinking level control         │
│                                        (off → xhigh)                  │
└───────────────────────────────────────────────────────────────────────┘
```

### Inference Hub Compatibility

| | Hermes | OpenClaw |
|---|--------|----------|
| **How** | Set `base_url` to inference hub endpoint under `providers.custom` | Register as a custom provider in `openclaw.json` |
| **Effort** | Zero code changes — config only | Zero code changes — config only |
| **Caveat** | Non-standard auth headers or tool formats need a thin adapter | Same |

Both are compatible with any OpenAI-compatible endpoint, making inference hub
integration straightforward for either.

---

## 6  Tools & Plugins

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Tools Comparison                                   │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  Registration:                       Registration:                    │
│  • Python functions with decorators  • TypeScript, typed schemas      │
│  • Self-registering at import time   • Built-in + plugin-provided     │
│  • Central registry (registry.py)    • Central executor in Pi         │
│                                                                       │
│  Built-in tools (~40+):              Built-in tools:                  │
│  • bash, process management          • exec, bash, process            │
│  • read, write, edit, search         • read, write, edit, apply_patch │
│  • memory (MEMORY.md/USER.md CRUD)   • memory_search, memory_get      │
│  • skills_list, skill_view,          • browser (CDP)                  │
│    skill_manage                      • web_search, x_search, web_fetch│
│  • honcho_* (profile/search/         • canvas (A2UI)                  │
│    context/conclude)                 • message (cross-channel)        │
│  • sessions_list/history/send        • sessions_list/history/send/    │
│  • browser (CDP)                       spawn/yield                    │
│  • web_search, scrape                • image, image_generate          │
│  • cron (create/list/update/remove)  • cron, gateway                  │
│  • gateway tools                     • nodes (device control)         │
│                                      • agents_list                    │
│                                                                       │
│  Grouping:                           Grouping:                        │
│  • Named toolsets (terminal, files,  • Tool profiles (full, coding,   │
│    memory, skills, web, browser,       messaging, minimal)            │
│    honcho, cron, mcp)                • Tool groups (runtime, fs,      │
│  • Platform presets (cli, telegram,    web, ui, sessions, memory,     │
│    discord)                            messaging, automation, nodes)  │
│                                                                       │
│  Access control:                     Access control:                  │
│  • Toolsets enabled/disabled         • tools.allow / tools.deny       │
│  • Per-platform presets              • tools.profile (base allowlist) │
│  • MCP tools dynamically loaded      • tools.byProvider (per-LLM)     │
│                                      • Per-agent tool overrides       │
│                                      • Sub-agent tool policies        │
│                                                                       │
│  Extensions:                         Extensions:                      │
│  • MCP tools (dynamic)              • Full plugin system              │
│  • External skill dirs              • Lobster (typed workflows)       │
│                                      • LLM Task (structured output)   │
│                                      • OpenProse (markdown workflows) │
└───────────────────────────────────────────────────────────────────────┘
```

### Key Differences

| Aspect | Hermes | OpenClaw |
|--------|--------|----------|
| **Tool creation at runtime** | Agent can create skills (which contain tool-usage procedures) | Not supported |
| **MCP support** | Yes (dynamically loaded MCP tools) | Via plugins |
| **Device control** | None | `nodes` tool — camera, voice, screen, location, SMS, contacts |
| **Canvas / UI generation** | None | `canvas` tool — push HTML/CSS/JS to visual workspace |
| **Execution modes** | Sequential or concurrent | Block streaming (tool output streams as generated) |
| **Approval gates** | Configurable allowlists | Configurable allow/deny + elevated exec escape hatch |

---

## 7  Skills System

Both use the [AgentSkills](https://agentskills.io/) standard (`SKILL.md`
format), making skills theoretically interchangeable. The differences lie in
how skills are discovered, loaded, and — crucially — created.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Skills Lifecycle Comparison                        │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  ┌──────────────────────┐            ┌───────────────────────┐        │
│  │  1. DISCOVERY        │            │  1. RESOLUTION        │        │
│  │  skills_list() tool  │            │  Precedence order:    │        │
│  │  → compact list      │            │  workspace > project  │        │
│  │  (~3k tokens)        │            │  > personal > managed │        │
│  │  Names + descriptions│            │  > bundled > extraDirs│        │
│  │  only (Level 0)      │            │  > plugin             │        │
│  └──────────┬───────────┘            │                       │        │
│             ▼                        │  All skills injected  │        │
│  ┌───────────────────────┐            │  as compact XML into  │       │
│  │  2. LOADING           │            │  system prompt at     │       │
│  │  skill_view(name)     │            │  session start.       │       │
│  │  → full SKILL.md      │            └──────────┬───────────┘        │
│  │  (Level 1)            │                       │                    │
│  │  skill_view(name,path)│                       ▼                    │
│  │  → reference file     │            ┌──────────────────────┐        │
│  │  (Level 2)            │            │  2. EXECUTION        │        │
│  └──────────┬────────────┘            │  Agent follows skill │        │
│             ▼                        │  instructions when    │        │
│  ┌──────────────────────┐            │  matched to task.     │        │
│  │  3. EXECUTION        │            │                       │        │
│  │  Agent follows skill │            │  Available as /slash  │        │
│  │  instructions.       │            │  commands or via      │        │
│  │  Available as /slash │            │  natural language.    │        │
│  │  commands or NL.     │            └──────────────────────┘         │
│  └──────────┬───────────┘                                             │
│             ▼                                                         │
│  ┌───────────────────────┐                                            │
│  │  4. SELF-IMPROVEMENT  │            ┌──────────────────────┐        │
│  │  After complex tasks: │            │  ❌ NOT AVAILABLE     │        │
│  │  • skill_manage(create│            │                      │        │
│  │  • skill_manage(patch)│            │  OpenClaw does not   │        │
│  │  • skill_manage(edit) │            │  auto-create or auto-│        │
│  │                       │            │  improve skills.     │        │
│  │  Triggers:            │            └──────────────────────┘        │
│  │  • 5+ tool calls      │                                            │
│  │  • Errors → fix found │                                            │
│  │  • User correction    │                                            │
│  │  • Novel workflow     │                                            │
│  └───────────────────────┘                                            │
└───────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Format** | SKILL.md (AgentSkills) | SKILL.md (AgentSkills) |
| **Agent-created skills** | Yes — `skill_manage` tool (create, patch, edit) | No |
| **Progressive disclosure** | Yes — 3 levels (list → view → detail) | No — all skills injected at session start |
| **Token cost** | ~3k tokens for the list; details loaded on demand | ~195 chars base + ~97 chars per skill; all upfront |
| **Gating** | `metadata.hermes.fallback_for_toolsets`, `requires_toolsets` | `metadata.openclaw.requires` (bins, env, config, os) |
| **Hub** | Skills Hub (multi-source: official, skills.sh, GitHub, ClawHub, LobeHub) | ClawHub (clawhub.com) |
| **Plugin-shipped skills** | No (external dirs) | Yes (`openclaw.plugin.json`) |
| **Trust levels** | builtin > official > trusted > community (security scanning) | Not formalized |
| **Skill sharing standard** | agentskills.io | agentskills.io (same) |

**Key takeaway:** The formats are interchangeable, but Hermes's progressive
disclosure is more token-efficient and its self-improvement cycle is the
critical differentiator. OpenClaw's gating system (`requires`) is more
practical for environment checks.

---

## 8  Memory & Context

This is the area of greatest divergence. Hermes has a 3-layer memory
architecture with active curation; OpenClaw relies on file-based context
injection without an active learning component.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Memory Architecture Comparison                     │
│                                                                       │
│  HERMES (3 layers)                   OPENCLAW (file injection)        │
│                                                                       │
│  ┌───────────────────────┐            ┌───────────────────────┐       │
│  │  Layer 1: Working     │            │  Prompt Files:        │       │
│  │  Memory               │            │  • AGENTS.md (behavior│       │
│  │                       │            │    rules, context)    │       │
│  │  MEMORY.md            │            │  • SOUL.md (persona)  │       │
│  │  (~800 tokens, bounded│            │  • TOOLS.md (tool     │       │
│  │   at 2,200 chars)     │            │    usage guidance)    │       │
│  │  • Environment facts  │            │  • IDENTITY.md        │       │
│  │  • Project conventions│            │  • USER.md (user      │       │
│  │  • Tool quirks        │            │    profile)           │       │
│  │  • Lessons learned    │            │  • HEARTBEAT.md (time/│       │
│  │                       │            │    state awareness)   │       │
│  │  USER.md              │            │  • BOOTSTRAP.md       │       │
│  │  (~500 tokens, bounded│            │    (first-run)        │       │
│  │   at 1,375 chars)     │            │                       │       │
│  │  • Name, role, tz     │            │  All injected into    │       │
│  │  • Preferences        │            │  system prompt at     │       │
│  │  • Pet peeves         │            │  session start.       │       │
│  │  • Workflow habits    │            │  Not bounded. Not     │       │
│  │                       │            │  actively curated.    │       │
│  │  Injected: frozen     │            │                       │       │
│  │  snapshot at session  │            │  Memory tools:        │       │
│  │  start. Auto-         │            │  • memory_search      │       │
│  │  consolidates at 80%. │            │  • memory_get         │       │
│  └──────────┬────────────┘            └───────────────────────┘       │
│             │                                                         │
│             ▼                                                         │
│  ┌───────────────────────┐                                            │
│  │  Layer 2: Honcho      │                                            │
│  │  (Cross-Session       │            ┌──────────────────────┐        │
│  │   User Modeling)      │            │  ❌ NO EQUIVALENT     │        │
│  │                       │            │                      │        │
│  │  • Dual-peer model    │            │  OpenClaw does not   │        │
│  │    (user + AI peers)  │            │  have cross-session  │        │
│  │  • Dialectic reasoning│            │  user modeling.      │        │
│  │  • Auto-learned from  │            │                      │        │
│  │    conversations      │            │  Context resets each │        │
│  │  • Cloud or self-host │            │  session (unless the │        │
│  │                       │            │  user manually edits │        │
│  │  Tools:               │            │  AGENTS.md/SOUL.md). │        │
│  │  • honcho_profile     │            └──────────────────────┘        │
│  │  • honcho_search      │                                            │
│  │  • honcho_context     │                                            │
│  │  • honcho_conclude    │                                            │
│  └──────────┬────────────┘                                            │
│             │                                                         │
│             ▼                                                         │
│  ┌───────────────────────┐                                            │
│  │  Layer 3: Session     │            ┌───────────────────────┐       │
│  │  Search (Episodic)    │            │  ❌ NO EQUIVALENT      │       │
│  │                       │            │                       │       │
│  │  • All sessions in    │            │  OpenClaw sessions    │       │
│  │    SQLite with FTS5   │            │  are not searchable   │       │
│  │  • session_search tool│            │  across conversations.│       │
│  │  • LLM summarization  │            └───────────────────────┘       │
│  │    of search results  │                                            │
│  │  • "Did we discuss X  │                                            │
│  │    last week?"        │                                            │
│  └───────────────────────┘                                            │
└───────────────────────────────────────────────────────────────────────┘
```

### Memory Comparison Table

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Working memory** | MEMORY.md + USER.md (bounded, actively curated by agent) | AGENTS.md + SOUL.md + USER.md + TOOLS.md + IDENTITY.md (unbounded, human-curated) |
| **Cross-session memory** | Honcho (auto-learned user model) + session search (FTS5) | None (context resets per session) |
| **Active memory curation** | Agent adds/replaces/removes entries via `memory` tool | Agent does not modify context files |
| **Memory nudges** | System prompt reminds agent to persist knowledge | Not present |
| **Capacity management** | Auto-consolidates at 80% capacity | No capacity management (unbounded files) |
| **Session search** | SQLite FTS5 — query all past sessions | Not available |
| **User modeling** | Honcho dialectic reasoning (learns from both user and AI messages) | Static USER.md file |
| **Context compression** | Mid-conversation compression when context grows too large | `/compact` slash command (manual) |
| **Token overhead** | ~1,300 tokens (fixed, bounded) | Varies (depends on file sizes, not bounded) |

---

## 9  Sandboxing & Execution

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Sandbox Comparison                                 │
│                                                                       │
│  HERMES (6 backends)                 OPENCLAW (3 backends)            │
│                                                                       │
│  ┌──────────┐                        ┌──────────┐                     │
│  │  local   │  Host OS, no isolation │  Docker  │  Local container    │
│  └──────────┘                        └──────────┘  (default)          │
│  ┌──────────┐                        ┌──────────┐                     │
│  │  docker  │  Container isolated    │   SSH    │  Any remote host    │
│  └──────────┘                        └──────────┘                     │
│  ┌──────────┐                        ┌──────────┐                     │
│  │   ssh    │  Remote host           │ OpenShell│  Managed + policies │
│  └──────────┘                        └──────────┘                     │
│  ┌──────────┐                                                         │
│  │ daytona  │  Serverless, persists                                   │
│  └──────────┘                                                         │
│  ┌───────────┐                                                        │
│  │singularity│ HPC container                                          │
│  └───────────┘                                                        │
│  ┌──────────┐                                                         │
│  │  modal   │  Serverless, pay-per-use                                │
│  └──────────┘                                                         │
│                                                                       │
│  Configuration:                      Configuration:                   │
│  hermes config set                   agents.defaults.sandbox in       │
│    terminal.backend <name>           openclaw.json                    │
│                                                                       │
│                                      Scope options:                   │
│                                      • session (1 per session)        │
│                                      • agent (1 per agent type)       │
│                                      • shared (1 for all)             │
│                                                                       │
│                                      Mode options:                    │
│                                      • off (host execution)           │
│                                      • non-main (groups sandboxed)    │
│                                      • all (everything sandboxed)     │
│                                                                       │
│                                      Workspace access:                │
│                                      • none, ro, rw                   │
│                                                                       │
│                                      OpenShell workspace modes:       │
│                                      • mirror (sync local↔remote)     │
│                                      • remote (remote canonical)      │
└───────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Backends** | 6 (local, docker, ssh, daytona, singularity, modal) | 3 (docker, ssh, openshell) |
| **Serverless options** | Modal, Daytona | None (but OpenShell could be extended) |
| **HPC support** | Singularity | None |
| **OpenShell integration** | None | Native (first-class backend) |
| **Sandbox scope** | Per-session (implicit) | Configurable (session / agent / shared) |
| **Workspace modes** | N/A (terminal-based, no workspace sync) | mirror (sync each exec) / remote (seed once) |
| **Browser sandbox** | None (browser is a tool, not sandboxed) | Dedicated browser sandbox container |
| **Kernel isolation** | Depends on backend | Landlock + seccomp + network namespaces (via OpenShell) |
| **Network policy** | None | OpenShell policy engine (per-binary, per-endpoint) |
| **Elevated exec** | N/A | `/elevated on` (escape hatch to host) |

**Key takeaway:** Hermes has more backend variety (especially for
research/HPC and serverless), but OpenClaw's OpenShell integration provides
stronger security guarantees through kernel-level isolation and declarative
network policy. For NemoClaw Escapades, OpenClaw's OpenShell pattern is the
one to follow.

---

## 10  Sub-Agents & Multi-Agent

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Sub-Agent Comparison                               │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  Model: Isolated AIAgent sessions    Model: Spawned sessions via      │
│  within a single process.            Gateway WS protocol.             │
│                                                                       │
│  Spawning:                           Spawning:                        │
│  • sessions_send(target, message,    • sessions_spawn(task, model,    │
│    reply_back=True)                    thinking, tools, ...)          │
│  • Each sub-agent is a separate      • Non-blocking (returns runId)   │
│    AIAgent instance                  • Announces result when done     │
│                                                                       │
│  Coordination:                       Coordination:                    │
│  • sessions_list (discover)          • /subagents list/kill/log/info  │
│  • sessions_history (fetch)          • Announce chain (depth-2 →      │
│  • sessions_send (message)             depth-1 → depth-0 → user)      │
│  • REPLY_SKIP / ANNOUNCE_SKIP        • sessions_yield (pause self,    │
│    (flow control)                      send message to user)          │
│                                                                       │
│  Depth limits:                       Depth limits:                    │
│  • No formal depth limit             • maxSpawnDepth: 1-5 (default 1) │
│  • Shared iteration budget across    • maxChildrenPerAgent: 1-20      │
│    parent + children                 • maxConcurrent: global cap      │
│                                      • runTimeoutSeconds: per-spawn   │
│                                                                       │
│  Multi-agent routing:                Multi-agent routing:             │
│  • Via session targeting             • agents.list[] (per-agent       │
│                                        workspace, sandbox, tools,     │
│                                        model)                         │
│                                      • Channel → agent routing        │
│                                      • Thread binding (Discord)       │
│                                                                       │
│  Isolation:                          Isolation:                       │
│  • Same process, same permissions    • Separate sessions, can have    │
│  • No credential isolation             different sandbox scope +      │
│  • Shared failure domain               tools + model per agent        │
│                                      • Thread-bound sessions          │
└───────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Spawn mechanism** | `sessions_send` (in-process) | `sessions_spawn` (via Gateway RPC) |
| **Depth control** | No formal limit (shared budget) | `maxSpawnDepth` (1-5), `maxChildrenPerAgent` (1-20) |
| **Concurrency cap** | No global cap | `maxConcurrent` (default 8) |
| **Per-spawn overrides** | No (all sub-agents share parent config) | Yes (model, thinking, tools per spawn) |
| **Thread binding** | No | Yes (Discord threads → persistent sessions) |
| **Isolation level** | Same process | Per-session sandbox possible |
| **Coordination latency** | Zero (in-process) | Low (WS message passing) |

**Key takeaway:** OpenClaw has more sophisticated multi-agent management
(depth limits, concurrency caps, per-spawn overrides, thread binding). Hermes
has simpler coordination but a shared budget model that prevents runaway
sub-agents.

---

## 11  Cron & Scheduling

Both provide cron-style scheduling where jobs run in fresh agent sessions.

| Feature | Hermes | OpenClaw |
|---------|--------|----------|
| **Schedule formats** | Relative (30m), intervals (every 2h), cron syntax, ISO timestamps | Cron syntax, natural language (both support NL) |
| **Storage** | `~/.hermes/cron/jobs.json` | Managed by Gateway |
| **Output** | `~/.hermes/cron/output/{job_id}/{timestamp}.md` | Delivered to connected channel |
| **Tick mechanism** | Gateway scheduler (every 60s) | Gateway scheduler (periodic) |
| **Skill attachment** | Yes (optionally inject skills per job) | Yes (skills matched at runtime) |
| **Delivery targets** | Specific: origin, local, telegram:id, discord:id | Any connected channel |
| **Safety** | Cron sessions cannot create more cron jobs | No explicit restriction |
| **Management** | CLI (`hermes cron`) + `/cron` slash + NL | CLI (`openclaw cron`) + `/cron` slash + NL + agent tool |

The cron systems are functionally equivalent. Hermes adds a safety rail
(cron sessions can't create more cron jobs) that prevents runaway scheduling.

---

## 12  Messaging & Channels

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Channel Coverage                                   │
│                                                                       │
│  HERMES (6 platforms)                OPENCLAW (25+ platforms)         │
│                                                                       │
│  Built-in:                           Core:                            │
│  ☑ Telegram (grammY)                ☑ WhatsApp (Baileys)              │
│  ☑ Discord (discord.js)             ☑ Telegram (grammY)               │
│  ☑ Slack                            ☑ Slack (Bolt)                    │
│  ☑ WhatsApp (Baileys)               ☑ Discord (discord.js)            │
│  ☑ Signal (signal-cli)              ☑ Signal (signal-cli)             │
│  ☑ CLI TUI                          ☑ BlueBubbles (iMessage)          │
│                                      ☑ Google Chat                    │
│  Also mentioned:                     ☑ IRC                            │
│  ☐ iMessage (BlueBubbles/imsg)      ☑ WebChat (WS UI)                 │
│  ☐ IRC                                                                │
│  ☐ Teams                            Plugin:                           │
│  ☐ Matrix                           ☑ Teams, Matrix, Feishu, LINE,    │
│  ☐ Feishu                             Mattermost, Nextcloud Talk,     │
│  ☐ LINE                               Nostr, Synology Chat, Tlon,     │
│  ☐ Mattermost                         Twitch, WeChat, Zalo,           │
│  ☐ Nextcloud Talk                     Voice Call, iMessage (legacy)   │
│  ☐ Nostr                                                              │
│  ☐ Twitch                                                             │
│  ☐ WeChat                                                             │
│  ☐ WebChat                                                            │
│                                                                       │
│  Architecture:                       Architecture:                    │
│  • Platform adapters in gateway/     • Channel adapters in src/       │
│    platforms/                          channels/ (core) + plugins     │
│  • Session routing per platform +    • All channels run simultaneously│
│    chat ID                           • Shared session router          │
│  • Cross-platform mirroring          • DM pairing + allowlists        │
│  • DM pairing (code-based auth)      • Per-channel group behavior     │
│                                                                       │
│  Unique:                             Unique:                          │
│  • Cross-platform mirroring          • Node model (device capabilities│
│                                        from macOS/iOS/Android)        │
│                                      • Voice wake, push-to-talk       │
│                                      • Canvas visual workspace        │
└───────────────────────────────────────────────────────────────────────┘
```

OpenClaw has a 4:1 advantage in channel coverage. For NemoClaw Escapades,
Slack is the primary channel (both support it), so channel count is not a
differentiator for the project's initial milestones.

---

## 13  Security Model

| Layer | Hermes | OpenClaw |
|-------|--------|----------|
| **Auth** | DM pairing (code-based) | DM pairing + platform allowlists + gateway token |
| **Tool gating** | Toolset enable/disable | Allow/deny lists, profiles, per-provider, per-agent |
| **Command approval** | Configurable allowlists | Approval callbacks + elevated exec escape hatch |
| **Sandbox isolation** | Depends on backend (docker = container, local = none) | Docker + OpenShell (Landlock + seccomp + namespaces) |
| **Network policy** | None (agent has full network access) | OpenShell policy engine (per-binary, per-endpoint) |
| **Credential handling** | Config file / env vars | Config file / env vars + OpenShell credential injection |
| **Prompt injection defense** | Skills scanned for injection/exfiltration; memory scanned | Gateway treats all inbound DMs as untrusted input |

**Key takeaway:** OpenClaw (especially with OpenShell) has a significantly
stronger security posture due to kernel-level sandbox isolation and
declarative network policy. Hermes relies more on application-level
controls.

---

## 14  Self-Learning Loop

This is where Hermes and OpenClaw diverge most sharply.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Self-Learning Loop                                 │
│                                                                       │
│  HERMES                              OPENCLAW                         │
│                                                                       │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  1. TASK INTAKE      │            │  1. TASK INTAKE      │         │
│  └──────────┬───────────┘            └──────────┬───────────┘         │
│             ▼                                   ▼                     │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  2. SKILL RECALL     │            │  2. SKILLS MATCHED   │         │
│  │  Check skills_list() │            │  From XML list in    │         │
│  │  Load if relevant    │            │  system prompt       │         │
│  └──────────┬───────────┘            └──────────┬───────────┘         │
│             ▼                                   ▼                     │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  3. EXECUTION        │            │  3. EXECUTION        │         │
│  │  Track what works    │            │  Execute task        │         │
│  └──────────┬───────────┘            └──────────┬───────────┘         │
│             ▼                                   ▼                     │
│  ┌──────────────────────┐            ┌──────────────────────┐         │
│  │  4. MEMORY PERSIST   │            │  4. DONE             │         │
│  │  Save discoveries to │            │                      │         │
│  │  MEMORY.md/USER.md/  │            │  No learning step.   │         │
│  │  Honcho.             │            │  No memory update.   │         │
│  │  Memory nudges remind│            │  No skill creation.  │         │
│  │  agent to persist.   │            │                      │         │
│  └──────────┬───────────┘            │  Context resets at   │         │
│             ▼                        │  next session.       │         │
│  ┌──────────────────────┐            └──────────────────────┘         │
│  │  5. SKILL CREATION/  │                                             │
│  │     UPDATE           │                                             │
│  │  After 5+ tool calls:│                                             │
│  │  • Create new skill  │                                             │
│  │  • Patch existing    │                                             │
│  │  • Rewrite skill     │                                             │
│  └──────────┬───────────┘                                             │
│             ▼                                                         │
│  ┌──────────────────────┐                                             │
│  │  6. SESSION ARCHIVE  │                                             │
│  │  All history → SQLite│                                             │
│  │  FTS5. Searchable    │                                             │
│  │  later via           │                                             │
│  │  session_search.     │                                             │
│  └──────────┬───────────┘                                             │
│             │                                                         │
│             └── Next similar task → skill is now available ──►        │
│                                                                       │
│  HERMES HAS:                         OPENCLAW LACKS:                  │
│  ☑ Agent-created skills              ☒ Agent-created skills           │
│  ☑ Active memory curation            ☒ Active memory curation         │
│  ☑ Memory nudges                     ☒ Memory nudges                  │
│  ☑ Cross-session user modeling       ☒ Cross-session user modeling    │
│  ☑ Session search (episodic memory)  ☒ Session search                 │
│  ☑ Self-reflection on outcomes       ☒ Self-reflection                │
│  ☑ RL infrastructure                 ☒ RL infrastructure              │
└───────────────────────────────────────────────────────────────────────┘
```

**This is the single biggest reason Hermes is a key reference for
NemoClaw Escapades.** The self-learning loop is not a single module — it
emerges from the interaction of skills, memory, session search, Honcho,
and prompt nudges. OpenClaw provides none of these learning components.

---

## 15  Companion Apps & UI

| Surface | Hermes | OpenClaw |
|---------|--------|----------|
| **CLI** | Rich TUI (`cli.py`) | `openclaw` CLI with sub-commands |
| **macOS app** | None | Menu bar app (Swift): voice wake, WebChat, debug, remote gateway |
| **iOS app** | None | Node: Canvas, voice, camera, screen recording, Bonjour pairing |
| **Android app** | None | Node: chat, voice, Canvas, camera, screen, device commands |
| **WebChat** | None | Gateway-hosted static UI over WS |
| **Canvas** | None | A2UI visual workspace (push HTML/CSS/JS from agent) |
| **Editor integration** | ACP adapter (Cursor/VS Code JSON-RPC) | Via plugins |
| **Batch/training UI** | `batch_runner.py` + `trajectory_compressor.py` | None |

OpenClaw is a full product with native apps and visual surfaces. Hermes is
developer-focused with an editor integration.

---

## 16  Ecosystem & Community

| Metric | Hermes | OpenClaw |
|--------|--------|----------|
| **GitHub stars** | 17k | 341k |
| **Primary language** | Python (92%) | TypeScript (89%) |
| **License** | MIT | MIT |
| **Org** | Nous Research | OpenClaw Foundation |
| **Release cadence** | Frequent (v0.5.0 as of 2026-03-28) | Rapid (2026.3.28 latest) |
| **Plugin ecosystem** | MCP tools, external skill dirs | ClawHub, plugin packages, channel plugins |
| **Migration path** | `hermes claw migrate` (imports from OpenClaw) | — (OpenClaw is the "upstream") |
| **NemoClaw integration** | Not integrated | Native (NemoClaw wraps OpenClaw) |
| **RL community** | Atropos, environments, batch runner | Not applicable |

---

## 17  Feature Matrix

| Feature | Hermes | OpenClaw | Notes |
|---------|:------:|:--------:|-------|
| **Core Agent Loop** | ✅ | ✅ | Both implement prompt → LLM → tools → loop |
| **Multi-provider support** | ✅ | ✅ | Both support OpenAI, Anthropic, OpenRouter, custom |
| **Custom endpoint (`base_url`)** | ✅ | ✅ | Both compatible with inference hub |
| **Provider fallback** | ✅ | ✅ | Both support failover chains |
| **Context compression** | ✅ | ⚠️ | Hermes automatic; OpenClaw manual (`/compact`) |
| **Prompt caching** | ✅ | ❌ | Hermes only (provider-specific) |
| **Skills (AgentSkills)** | ✅ | ✅ | Same standard, interchangeable format |
| **Agent-created skills** | ✅ | ❌ | Hermes's defining feature |
| **Progressive skill disclosure** | ✅ | ❌ | Hermes loads on demand; OpenClaw injects all upfront |
| **Self-learning loop** | ✅ | ❌ | Hermes only |
| **Active memory curation** | ✅ | ❌ | Hermes bounded MEMORY.md/USER.md with add/replace/remove |
| **Cross-session user modeling** | ✅ | ❌ | Hermes via Honcho |
| **Session search (episodic)** | ✅ | ❌ | Hermes via SQLite FTS5 |
| **Memory nudges** | ✅ | ❌ | Hermes prompt engineering |
| **Sub-agent delegation** | ✅ | ✅ | OpenClaw has richer controls (depth, concurrency, per-spawn) |
| **Cron scheduling** | ✅ | ✅ | Functionally equivalent |
| **Messaging channels** | 6 | 25+ | OpenClaw significantly broader |
| **Sandbox backends** | 6 | 3 | Hermes more varied; OpenClaw has OpenShell |
| **OpenShell integration** | ❌ | ✅ | OpenClaw native |
| **Kernel-level isolation** | ❌ | ✅ | Via OpenShell (Landlock + seccomp) |
| **Network policy** | ❌ | ✅ | Via OpenShell policy engine |
| **Native apps** | ❌ | ✅ | macOS, iOS, Android |
| **Canvas / visual workspace** | ❌ | ✅ | A2UI |
| **Device node model** | ❌ | ✅ | Camera, voice, screen, location, SMS |
| **Plugin architecture** | ⚠️ | ✅ | Hermes has MCP; OpenClaw has a full plugin system |
| **Editor integration** | ✅ | ⚠️ | Hermes ACP (Cursor/VS Code); OpenClaw via plugins |
| **RL / training pipeline** | ✅ | ❌ | Hermes: environments, trajectories, Atropos |
| **Migration tool** | ✅ | — | `hermes claw migrate` imports from OpenClaw |

---

## 18  Implications for NemoClaw Escapades

### Which to Lift, and When

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Lift Strategy                                      │
│                                                                       │
│  MILESTONE           PRIMARY REFERENCE        SECONDARY REFERENCE     │
│                                                                       │
│  M1 — Foundation     OpenClaw                 Hermes                  │
│  (Slack + inference  • Gateway pattern         • Provider resolver    │
│   + orchestrator)    • Channel adapter pattern   (base_url config)    │
│                      • Session routing                                │
│                                                                       │
│  M2 — Knowledge      Equal weight             —                       │
│  Management          • OpenClaw: context files                        │
│  (SecondBrain)         (AGENTS.md pattern)                            │
│                      • Hermes: memory system                          │
│                        (for future active curation)                   │
│                                                                       │
│  M3 — Coding Agent   OpenClaw                 Hermes                  │
│  (OpenShell)         • OpenShell backend       • Terminal backend     │
│                      • sandbox.scope:session     pattern (model)      │
│                      • mirror/remote modes                            │
│                      • sessions_spawn                                 │
│                                                                       │
│  M4 — Self-Learning  ★ HERMES ★               OpenClaw                │
│  Loop                • Skill auto-creation     • Skills format        │
│                      • MEMORY.md/USER.md         (shared standard)    │
│                      • Session search                                 │
│                      • Honcho integration                             │
│                      • Memory nudges                                  │
│                      • Self-reflection                                │
│                                                                       │
│  M5 — Review Agent   OpenClaw                 Hermes                  │
│                      • Orchestrator pattern    • Sub-agent sessions   │
│                        (maxSpawnDepth: 2)                             │
│                      • Announce chain                                 │
│                                                                       │
│  M6 — Professional   Equal weight             —                       │
│  KB                  • OpenClaw: cron + Slack channel adapter         │
│                      • Hermes: cron + skill-backed jobs               │
└───────────────────────────────────────────────────────────────────────┘
```

### Component-Level Lift Recommendations

| Component | Lift From | Rationale |
|-----------|-----------|-----------|
| **Gateway / daemon pattern** | OpenClaw | Typed WS protocol, session routing, plugin loader — more production-ready |
| **Channel adapters** | OpenClaw | Broader coverage, cleaner interface pattern |
| **Provider resolver** | Hermes | More flexible (`base_url` for any endpoint), prompt caching, budget tracking |
| **Skills format** | Both (same standard) | AgentSkills SKILL.md — interchangeable |
| **Skill auto-creation** | Hermes | `skill_manage` tool — OpenClaw doesn't have this |
| **Memory system** | Hermes | 3-layer bounded memory with active curation — OpenClaw's is passive |
| **User modeling** | Hermes | Honcho — OpenClaw has nothing comparable |
| **Session search** | Hermes | SQLite FTS5 — OpenClaw has no cross-session search |
| **Self-learning loop** | Hermes | Only Hermes has this |
| **Sandbox backend** | OpenClaw | OpenShell-native integration (mirror/remote modes) |
| **Security / policy** | OpenClaw | Kernel-level isolation, network policy via OpenShell |
| **Sub-agent management** | OpenClaw | Richer controls (depth limits, concurrency caps, per-spawn overrides) |
| **Context compression** | Hermes | Automatic mid-conversation compression — OpenClaw requires manual `/compact` |
| **Prompt caching** | Hermes | Provider-specific cache hints — OpenClaw doesn't have this |

### The Hybrid Strategy

The recommended path for NemoClaw Escapades:

1. **Use OpenClaw's Gateway pattern** as the structural foundation — a
   long-running daemon that connects Slack, manages sessions, and delegates
   to the agent runtime.

2. **Use OpenClaw's OpenShell integration** for sandbox management — kernel
   isolation, network policy, workspace mirroring, and sandbox scoping.

3. **Port Hermes's self-learning loop** into the orchestrator — skills
   auto-creation, bounded memory, session search, Honcho integration, and
   prompt nudges.

4. **Use Hermes's provider resolver** for inference backend flexibility —
   `base_url` config for inference hub, prompt caching, and budget tracking.

This gives you OpenClaw's product maturity and security posture combined
with Hermes's self-improving intelligence — exactly the combination the
design document calls for.

---

### Sources

- [Hermes Agent Deep Dive](hermes_deep_dive.md)
- [OpenClaw Deep Dive](openclaw_deep_dive.md)
- [NemoClaw Escapades Design Document](../design.md)
- [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
- [openclaw/openclaw](https://github.com/openclaw/openclaw)
- [AgentSkills Standard](https://agentskills.io/)
