# OpenClaw — Deep Dive

> **Source:** [openclaw/openclaw](https://github.com/openclaw/openclaw)
> (341k stars, MIT license, latest release 2026.3.28)
>
> **Official docs:** [docs.openclaw.ai](https://docs.openclaw.ai)
>
> **Last reviewed:** 2026-03-29

---

## Table of Contents

1. [Overview](#1--overview)
2. [High-Level Architecture](#2--high-level-architecture)
3. [Repository Structure](#3--repository-structure)
4. [The Gateway](#4--the-gateway)
5. [The Agent Runtime (Pi)](#5--the-agent-runtime-pi)
6. [Chat Channels (25+ Platforms)](#6--chat-channels-25-platforms)
7. [Tools & Plugins System](#7--tools--plugins-system)
8. [Skills System](#8--skills-system)
9. [Sandboxing & Execution Backends](#9--sandboxing--execution-backends)
10. [OpenShell Integration](#10--openshell-integration)
11. [Sub-Agents & Multi-Agent Routing](#11--sub-agents--multi-agent-routing)
12. [Cron & Scheduled Tasks](#12--cron--scheduled-tasks)
13. [Security Model](#13--security-model)
14. [Context Files & Memory](#14--context-files--memory)
15. [Companion Apps & Nodes](#15--companion-apps--nodes)
16. [Live Canvas (A2UI)](#16--live-canvas-a2ui)
17. [Configuration System](#17--configuration-system)
18. [Setup & Installation](#18--setup--installation)
19. [OpenClaw vs Hermes — Feature Comparison](#19--openclaw-vs-hermes--feature-comparison)
20. [Answers to Design Doc Questions](#20--answers-to-design-doc-questions)
21. [What to Lift for NemoClaw Escapades](#21--what-to-lift-for-nemoclaw-escapades)

---

## 1  Overview

OpenClaw is a **personal AI assistant** you run on your own devices. It's the
largest open-source project in the personal AI assistant space (341k stars),
with a focus on multi-channel messaging, sandboxed tool execution, and a
growing plugin/extension ecosystem.

OpenClaw's defining characteristic compared to Hermes: it is a **full product**
with native apps (macOS, iOS, Android), a visual Canvas, 25+ messaging
channels, and a plugin architecture. Hermes is more focused on the
self-learning loop. OpenClaw is more focused on being a complete, deployable
assistant platform.

Key facts:
- Written primarily in TypeScript (89%)
- Node.js runtime (Node 24 recommended)
- Gateway architecture — single long-lived process owns all messaging
- Supports any LLM provider (OpenAI, Anthropic, Google, OpenRouter, etc.)
- Three sandbox backends: Docker, SSH, OpenShell
- MIT license

---

## 2  High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         USER INTERFACES                                 │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐    │
│  │ WhatsApp │  │ Telegram │  │  Slack   │  │ Discord  │  │  20+    │    │
│  │          │  │          │  │          │  │          │  │  more   │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬────┘    │
│       │              │              │              │             │      │
│       └──────────────┴──────┬───────┴──────────────┴─────────────┘      │
│                             │                                           │
│                             ▼                                           │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    GATEWAY (daemon)                             │    │
│  │                    ws://127.0.0.1:18789                         │    │
│  │                                                                 │    │
│  │  ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐    │    │
│  │  │  Channel     │  │  Session    │  │  WS Control Plane    │    │    │
│  │  │  Adapters    │  │  Router     │  │  (typed JSON frames) │    │    │
│  │  │  (platforms/)│  │             │  │                      │    │    │
│  │  └──────────────┘  └─────────────┘  └──────────────────────┘    │    │
│  │                                                                 │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐     │    │
│  │  │  Auth &     │  │  Delivery   │  │  Cron Scheduler      │     │    │
│  │  │  Pairing    │  │  Engine     │  │  + Background Tasks  │     │    │
│  │  └─────────────┘  └─────────────┘  └──────────────────────┘     │    │
│  │                                                                 │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐     │    │
│  │  │  Plugin     │  │  Hooks      │  │  Canvas / A2UI       │     │    │
│  │  │  Loader     │  │  System     │  │  Host                │     │    │
│  │  └─────────────┘  └─────────────┘  └──────────────────────┘     │    │
│  └────────────────────────────┬────────────────────────────────────┘    │
│                               │                                         │
│                               ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Pi Agent Runtime (RPC)                       │    │
│  │                                                                 │    │
│  │  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │    │
│  │  │  System      │  │  Provider     │  │  Tool Executor       │  │    │
│  │  │  Prompt      │  │  Selection    │  │                      │  │    │
│  │  │  Builder     │  │  + Fallback   │  │  Built-in:           │  │    │
│  │  │              │  │               │  │  • exec / process    │  │    │
│  │  │ • AGENTS.md  │  │  Providers:   │  │  • read / write /    │  │    │
│  │  │ • SOUL.md    │  │  • OpenAI     │  │    edit / apply_patch│  │    │
│  │  │ • TOOLS.md   │  │  • Anthropic  │  │  • browser (CDP)     │  │    │
│  │  │ • IDENTITY   │  │  • Google     │  │  • web_search        │  │    │
│  │  │ • skills     │  │  • OpenRouter │  │  • message           │  │    │
│  │  │ • USER.md    │  │  • Custom     │  │  • canvas            │  │    │
│  │  └──────────────┘  └───────────────┘  │  • cron / gateway   │   │    │
│  │                                        │  • image / image_gen│  │    │
│  │                                        │  • sessions_*       │  │    │
│  │                                        │  • Plugin tools     │  │    │
│  │                                        └──────────────────────┘ │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                               │                                         │
│              ┌────────────────┼────────────────────┐                    │
│              ▼                ▼                    ▼                    │
│  ┌──────────────────┐  ┌──────────────┐  ┌──────────────────────┐       │
│  │  Sandbox Backend │  │   Skills     │  │   Workspace          │       │
│  │                  │  │   Store      │  │                      │       │
│  │  ┌────────────┐  │  │              │  │  ~/.openclaw/         │      │
│  │  │   Docker   │  │  │ • bundled    │  │  ├── workspace/       │      │
│  │  │ (default)  │  │  │ • managed    │  │  │   ├── AGENTS.md    │      │
│  │  ├────────────┤  │  │ • workspace  │  │  │   ├── SOUL.md      │      │
│  │  │    SSH     │  │  │ • external   │  │  │   ├── TOOLS.md     │      │
│  │  │  (remote)  │  │  │ • ClawHub    │  │  │   └── skills/      │      │
│  │  ├────────────┤  │  │              │  │  ├── credentials/    │       │
│  │  │ OpenShell  │  │  │              │  │  ├── openclaw.json    │      │
│  │  │ (managed)  │  │  │              │  │  └── sandboxes/       │      │
│  │  └────────────┘  │  └─────────────┘  └──────────────────────┘        │
│  └──────────────────┘                                                   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                     Clients & Nodes                             │    │
│  │                                                                 │    │
│  │  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │    │
│  │  │  CLI       │  │  macOS   │  │   iOS    │  │   Android    │   │    │
│  │  │  (openclaw)│  │  App     │  │   Node   │  │   Node       │   │    │
│  │  │            │  │  (menu   │  │          │  │              │   │    │
│  │  │ • onboard  │  │  bar)    │  │ • Canvas │  │ • Chat       │   │    │
│  │  │ • gateway  │  │          │  │ • Voice  │  │ • Voice      │   │    │
│  │  │ • agent    │  │ • Voice  │  │ • Camera │  │ • Canvas     │   │    │
│  │  │ • send     │  │   Wake   │  │ • Screen │  │ • Camera     │   │    │
│  │  │ • doctor   │  │ • WebChat│  │          │  │ • Device     │   │    │
│  │  │ • sandbox  │  │ • Debug  │  │          │  │   cmds       │   │    │
│  │  └────────────┘  └──────────┘  └──────────┘  └──────────────┘   │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Design Themes

- **Gateway-centric** — a single daemon owns all messaging, sessions, and
  control plane communication over WebSocket.
- **Channel-agnostic** — all messaging platforms share one agent core; add a
  channel by writing an adapter.
- **Sandbox-first** — tool execution can be isolated via Docker, SSH, or
  OpenShell (configurable per agent, per session).
- **Plugin architecture** — channels, tools, skills, model providers, speech,
  and image generation are all pluggable.
- **Node model** — companion devices (macOS, iOS, Android) connect as WS nodes
  and expose device capabilities (camera, voice, screen, location).

---

## 3  Repository Structure

```
openclaw/
├── src/                    # Core source (TypeScript)
│   ├── gateway/            #   Gateway daemon
│   ├── pi/                 #   Pi agent runtime (RPC mode)
│   ├── tools/              #   Built-in tool implementations
│   ├── channels/           #   Channel adapters (platform-specific)
│   ├── sandbox/            #   Sandbox runtime (Docker, SSH, OpenShell)
│   ├── skills/             #   Skills loading and resolution
│   ├── plugins/            #   Plugin loader and registry
│   └── ...
│
├── apps/                   # Companion applications
│   ├── macos/              #   macOS menu bar app (Swift)
│   ├── ios/                #   iOS node (Swift)
│   └── android/            #   Android node (Kotlin)
│
├── skills/                 # Bundled skills
├── extensions/             # Extension packages
├── packages/               # Shared packages (monorepo)
├── ui/                     # Control UI / WebChat frontend
│
├── Dockerfile              # Gateway container
├── Dockerfile.sandbox      # Sandbox container
├── Dockerfile.sandbox-browser  # Browser sandbox
├── Dockerfile.sandbox-common   # Extended sandbox w/ tooling
│
├── scripts/                # Install, setup, sandbox-setup scripts
├── docs/                   # Documentation source
├── test/                   # Test suite
│
├── openclaw.mjs            # CLI entry point
├── docker-compose.yml      # Docker composition
├── AGENTS.md               # Default agent instructions
├── VISION.md               # Project vision
└── CLAUDE.md               # Context for AI coding agents
```

---

## 4  The Gateway

The Gateway is the heart of OpenClaw — a **single long-lived daemon** that
owns all messaging surfaces and exposes a typed WebSocket control plane.

```
┌───────────────────────────────────────────────────────────────────┐
│                        GATEWAY DAEMON                             │
│                     ws://127.0.0.1:18789                          │
│                                                                   │
│  Responsibilities:                                                │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  1. Maintain provider connections (WhatsApp, Telegram, ...)│   │
│  │  2. Route incoming messages → sessions                     │   │
│  │  3. Authorize users (allowlists, DM pairing)               │   │
│  │  4. Dispatch messages to Pi agent runtime                  │   │
│  │  5. Deliver responses back to channels                     │   │
│  │  6. Manage sandbox lifecycle                               │   │
│  │  7. Tick cron scheduler                                    │   │
│  │  8. Host Canvas / A2UI / WebChat                           │   │
│  │  9. Manage node connections (macOS/iOS/Android)            │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                   │
│  WS Protocol (typed JSON frames):                                 │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  First frame:   { type: "req", method: "connect", ... }    │   │
│  │  Requests:      { type: "req", id, method, params }        │   │
│  │  Responses:     { type: "res", id, ok, payload | error }   │   │
│  │  Events:        { type: "event", event, payload, seq }     │   │
│  │                                                            │   │
│  │  Event types: agent, chat, presence, health, heartbeat,    │   │
│  │               cron, tick, shutdown                         │   │
│  │                                                            │   │
│  │  Auth: OPENCLAW_GATEWAY_TOKEN or connect.params.auth.token │   │
│  │  Idempotency keys required for side-effecting methods      │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                   │
│  Client types:                                                    │
│  • Operators: CLI, macOS app, web UI (send requests, subscribe)   │
│  • Nodes: macOS/iOS/Android (role: "node", expose device caps)    │
│  • WebChat: static UI using WS API for chat + sends               │
└───────────────────────────────────────────────────────────────────┘
```

### Connection Lifecycle

```
Client ──req:connect──► Gateway
                        ├── validate auth token
                        ├── check device identity / pairing
                        └── res: ok + snapshot (presence + health)

Gateway ──event:presence──► Client
Gateway ──event:tick──► Client

Client ──req:agent──► Gateway
                      ├── route to session
                      └── res:agent (ack: {runId, status: "accepted"})

Gateway ──event:agent (streaming)──► Client
Gateway ──res:agent (final)──► Client
```

### Pairing & Local Trust

- All WS clients include a device identity on connect.
- New device IDs require pairing approval (code-based).
- Local connects (loopback) can be auto-approved.
- Signature payload binds platform + device family; re-pairing required for
  metadata changes.

---

## 5  The Agent Runtime (Pi)

The agent runtime is called **Pi** and runs in RPC mode. It's responsible for
prompt construction, model calls, tool execution, and streaming.

```
┌───────────────────────────────────────────────────────────────┐
│                     Pi Agent Runtime                          │
│                                                               │
│  System Prompt Assembly:                                      │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  AGENTS.md      ← Agent instructions and behavior       │  │
│  │  SOUL.md        ← Persona / identity                    │  │
│  │  TOOLS.md       ← Tool usage guidance                   │  │
│  │  IDENTITY.md    ← Identity context                      │  │
│  │  USER.md        ← User profile                          │  │
│  │  HEARTBEAT.md   ← Time/state awareness                  │  │
│  │  BOOTSTRAP.md   ← First-run bootstrap                   │  │
│  │  Skills list    ← Compact XML of eligible skills        │  │
│  │  Context files  ← Project/workspace context             │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Model Providers:                                             │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  OpenAI (ChatGPT/Codex) — including subscriptions       │  │
│  │  Anthropic (Claude)                                     │  │
│  │  Google (Gemini)                                        │  │
│  │  OpenRouter (200+ models)                               │  │
│  │  Any custom endpoint                                    │  │
│  │  + model failover and auth profile rotation             │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Execution:                                                   │
│  • Tool streaming with block streaming                        │
│  • Thinking level control (off → xhigh, GPT-5.2/Codex only)   │
│  • Session model: main for direct, isolated for groups        │
│  • Context compression (/compact)                             │
│  • Media pipeline: images/audio/video with transcription      │
└───────────────────────────────────────────────────────────────┘
```

---

## 6  Chat Channels (25+ Platforms)

OpenClaw supports the broadest set of messaging channels of any open-source
agent.

```
┌───────────────────────────────────────────────────────────────────┐
│                     Channel Adapters                              │
│                                                                   │
│  Core channels (built-in):                                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │ WhatsApp │ │ Telegram │ │  Slack   │ │ Discord  │              │
│  │ (Baileys)│ │ (grammY) │ │ (Bolt)   │ │(discord  │              │
│  │          │ │          │ │          │ │  .js)    │              │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘              │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌──────────┐             │
│  │  Signal  │ │BlueBubbles│ │  Google  │ │   IRC    │             │
│  │(signal-  │ │(iMessage) │ │  Chat    │ │          │             │
│  │ cli)     │ │           │ │          │ │          │             │
│  └──────────┘ └───────────┘ └──────────┘ └──────────┘             │
│  ┌──────────┐                                                     │
│  │ WebChat  │  (Gateway WS UI)                                    │
│  └──────────┘                                                     │
│                                                                   │
│  Plugin channels:                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐             │
│  │  Teams   │ │  Matrix  │ │  Feishu  │ │  LINE     │             │
│  │ (Bot FW) │ │          │ │ (Lark)   │ │           │             │
│  ├──────────┤ ├──────────┤ ├──────────┤ ├──────────┤              │
│  │Mattermost│ │ Nextcloud│ │  Nostr   │ │Synology   │             │
│  │          │ │   Talk   │ │          │ │  Chat     │             │
│  ├──────────┤ ├──────────┤ ├──────────┤ ├──────────┤              │
│  │  Tlon    │ │  Twitch  │ │  WeChat  │ │   Zalo    │             │
│  │ (Urbit)  │ │  (IRC)   │ │(Tencent) │ │(+personal)│             │
│  ├──────────┤ ├──────────┤                                        │
│  │Voice Call│ │ iMessage │                                        │
│  │(Plivo/   │ │ (legacy) │                                        │
│  │ Twilio)  │ │          │                                        │
│  └──────────┘ └──────────┘                                        │
│                                                                   │
│  All channels:                                                    │
│  • Run simultaneously — Gateway routes per chat                   │
│  • Share one agent core (Pi)                                      │
│  • Support DM pairing & allowlists                                │
│  • Group behavior configurable per channel                        │
└───────────────────────────────────────────────────────────────────┘
```

---

## 7  Tools & Plugins System

OpenClaw has three layers: **tools** (typed functions), **skills** (markdown
instructions), and **plugins** (packages that register all of the above).

```
┌───────────────────────────────────────────────────────────────┐
│                    Tools Architecture                         │
│                                                               │
│  Built-in Tools:                                              │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ group:runtime   exec, bash, process, code_execution     │  │
│  │ group:fs        read, write, edit, apply_patch          │  │
│  │ group:web       web_search, x_search, web_fetch         │  │
│  │ group:ui        browser (CDP), canvas                   │  │
│  │ group:sessions  sessions_list/history/send/spawn/yield  │  │
│  │ group:memory    memory_search, memory_get               │  │
│  │ group:messaging message (cross-channel)                 │  │
│  │ group:automation cron, gateway                          │  │
│  │ group:nodes     nodes (device control)                  │  │
│  │ + image, image_generate, agents_list                    │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Tool Profiles:                                               │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌────────────┐     │
│  │   full    │ │  coding   │ │ messaging │ │  minimal   │     │
│  │ (all)     │ │ (fs+exec  │ │ (message  │ │ (status    │     │
│  │           │ │  +session)│ │  +session)│ │  only)     │     │
│  └───────────┘ └───────────┘ └───────────┘ └────────────┘     │
│                                                               │
│  Access Control:                                              │
│  • tools.allow / tools.deny (deny always wins)                │
│  • tools.profile (base allowlist)                             │
│  • tools.byProvider (per-LLM-provider restrictions)           │
│  • Per-agent overrides (agents.list[].tools)                  │
│  • Sub-agent tool policies                                    │
│                                                               │
│  Plugin Tools (examples):                                     │
│  • Lobster — typed workflow runtime with resumable approvals  │
│  • LLM Task — JSON-only LLM for structured output             │
│  • Diffs — diff viewer and renderer                           │
│  • OpenProse — markdown-first workflow orchestration          │
└───────────────────────────────────────────────────────────────┘
```

---

## 8  Skills System

OpenClaw uses [AgentSkills](https://agentskills.io/)-compatible skill folders
— the same standard used by Hermes.

```
┌───────────────────────────────────────────────────────────────┐
│                 Skills Resolution Order                       │
│                (highest to lowest precedence)                 │
│                                                               │
│  1. <workspace>/skills/           (workspace skills)          │
│  2. <workspace>/.agents/skills/   (project agent skills)      │
│  3. ~/.agents/skills/             (personal agent skills)     │
│  4. ~/.openclaw/skills/           (managed/local skills)      │
│  5. Bundled skills                (shipped with install)      │
│  6. skills.load.extraDirs         (shared directories)        │
│  7. Plugin skills                 (from enabled plugins)      │
│                                                               │
│  Same name = highest-precedence wins (workspace overrides     │
│  everything).                                                 │
└───────────────────────────────────────────────────────────────┘
```

### Gating (Load-Time Filters)

```yaml
metadata:
  openclaw:
    requires:
      bins: ["uv"]              # must exist on PATH
      env: ["GEMINI_API_KEY"]   # env var must be set
      config: ["browser.enabled"]  # config key must be truthy
    primaryEnv: "GEMINI_API_KEY"
    os: ["darwin", "linux"]     # platform filter
    always: true                # skip all other gates
```

### Token Impact

Skills are injected as a compact XML list into the system prompt:
- Base overhead: 195 characters (when any skill is active)
- Per skill: ~97 chars + name + description + location

### Key Differences from Hermes Skills

| Feature | OpenClaw | Hermes |
|---------|----------|--------|
| Format | SKILL.md (AgentSkills standard) | SKILL.md (AgentSkills standard) |
| Agent-created skills | Not built-in | Core feature (`skill_manage` tool) |
| Progressive disclosure | No (all loaded at session start) | Yes (3-level: list → view → detail) |
| Hub | ClawHub (clawhub.com) | Skills Hub (multi-source) |
| Plugin-shipped skills | Yes (via `openclaw.plugin.json`) | No (external dirs instead) |
| Gating | `metadata.openclaw.requires` | `metadata.hermes.fallback_for_toolsets` |

---

## 9  Sandboxing & Execution Backends

OpenClaw's sandboxing system is the most directly relevant subsystem for
NemoClaw Escapades — it controls where and how agent tools execute.

```
┌───────────────────────────────────────────────────────────────────┐
│                    Sandbox Architecture                           │
│                                                                   │
│  Modes (agents.defaults.sandbox.mode):                            │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────┐     │
│  │   off    │  │  non-main    │  │  all                     │     │
│  │ (host)   │  │ (groups/     │  │ (every session           │     │
│  │          │  │  channels    │  │  sandboxed)              │     │
│  │          │  │  sandboxed)  │  │                          │     │
│  └──────────┘  └──────────────┘  └──────────────────────────┘     │
│                                                                   │
│  Scope (agents.defaults.sandbox.scope):                           │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────┐     │
│  │ session  │  │   agent      │  │  shared                  │     │
│  │ (1 per   │  │ (1 per       │  │ (1 for all               │     │
│  │  session)│  │  agent id)   │  │  sessions)               │     │
│  └──────────┘  └──────────────┘  └──────────────────────────┘     │
│                                                                   │
│  Backends (agents.defaults.sandbox.backend):                      │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │                                                            │   │
│  │  ┌──────────┐  ┌───────────┐  ┌────────────────────────┐   │   │
│  │  │  Docker  │  │   SSH     │  │     OpenShell          │   │   │
│  │  │ (default)│  │ (remote)  │  │   (managed remote)     │   │   │
│  │  │          │  │           │  │                        │   │   │
│  │  │ Local    │  │ Any SSH-  │  │ openshell CLI +        │   │   │
│  │  │ container│  │ accessible│  │ SSH transport          │   │   │
│  │  │          │  │ host      │  │                        │   │   │
│  │  │ Bind     │  │           │  │ Workspace modes:       │   │   │
│  │  │ mounts,  │  │ Remote-   │  │ • mirror (local        │   │   │
│  │  │ network  │  │ canonical │  │   canonical, sync      │   │   │
│  │  │ control, │  │ model     │  │   each exec)           │   │   │
│  │  │ browser  │  │           │  │ • remote (remote       │   │   │
│  │  │ sandbox  │  │ Seed once │  │   canonical, seed      │   │   │
│  │  │          │  │ then exec │  │   once)                │   │   │
│  │  │          │  │ remotely  │  │                        │   │   │
│  │  └──────────┘  └───────────┘  └────────────────────────┘   │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                   │
│  Workspace Access (agents.defaults.sandbox.workspaceAccess):      │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────┐     │
│  │  none    │  │   ro         │  │  rw                      │     │
│  │ (sandbox │  │ (read-only   │  │ (read/write mount at     │     │
│  │  only)   │  │  at /agent)  │  │  /workspace)             │     │
│  └──────────┘  └──────────────┘  └──────────────────────────┘     │
│                                                                   │
│  What gets sandboxed:                                             │
│  ✓ exec, process, read, write, edit, apply_patch                  │
│  ✓ Optional browser (dedicated browser sandbox container)         │
│  ✗ Gateway process itself                                         │
│  ✗ Elevated exec (explicit host escape hatch)                     │
└───────────────────────────────────────────────────────────────────┘
```

### Backend Comparison

| | Docker | SSH | OpenShell |
|---|---|---|---|
| Runs on | Local container | Any SSH host | OpenShell managed sandbox |
| Setup | `scripts/sandbox-setup.sh` | SSH key + target | OpenShell plugin enabled |
| Workspace | Bind-mount or copy | Remote-canonical (seed once) | `mirror` or `remote` |
| Network control | `docker.network` | Depends on host | Depends on OpenShell |
| Browser sandbox | Supported | Not supported | Not supported yet |
| Best for | Local dev, full isolation | Offloading to remote | Managed remote sandboxes |

---

## 10  OpenShell Integration

OpenShell is particularly relevant because NemoClaw uses it as the primary
sandbox backend.

```
┌───────────────────────────────────────────────────────────────────┐
│                    OpenShell Lifecycle                            │
│                                                                   │
│  1. Gateway reads plugin config                                   │
│     plugins.entries.openshell.config                              │
│                                                                   │
│  2. On first agent turn needing sandbox:                          │
│     openshell sandbox create                                      │
│       --from openclaw                                             │
│       --gateway <name>                                            │
│       --policy <id>                                               │
│       --providers <list>                                          │
│       [--gpu]                                                     │
│                                                                   │
│  3. Get SSH config for the sandbox:                               │
│     openshell sandbox ssh-config <sandbox-id>                     │
│     → writes to temp file                                         │
│                                                                   │
│  4. Open SSH session using remote filesystem bridge               │
│                                                                   │
│  5a. Mirror mode:                                                 │
│      sync local→remote BEFORE exec                                │
│      run command in sandbox                                       │
│      sync remote→local AFTER exec                                 │
│                                                                   │
│  5b. Remote mode:                                                 │
│      seed workspace once on create                                │
│      all subsequent ops run directly on remote                    │
│                                                                   │
│  Lifecycle commands:                                              │
│  • openclaw sandbox list     (shows Docker + OpenShell)           │
│  • openclaw sandbox explain  (effective policy)                   │
│  • openclaw sandbox recreate (delete + re-seed on next use)       │
└───────────────────────────────────────────────────────────────────┘
```

### Workspace Modes: Mirror vs Remote

The `mode` setting in the OpenShell plugin config determines where the
canonical copy of the workspace lives and how it stays in sync.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Mirror Mode  (mode: "mirror")                      │
│                                                                       │
│  Canonical workspace: HOST                                            │
│                                                                       │
│  ┌────────────┐                        ┌────────────────────────┐     │
│  │  HOST      │   sync local→remote    │  SANDBOX               │     │
│  │            │   BEFORE every exec    │                        │     │
│  │  /project  │ ─────────────────────▶ │  /sandbox/project      │     │
│  │  (canonical│                        │  (copy)                │     │
│  │   copy)    │   sync remote→local    │                        │     │
│  │            │ ◀───────────────────── │  Agent runs here       │     │
│  │            │   AFTER every exec     │  (makes changes)       │     │
│  └────────────┘                        └────────────────────────┘     │
│                                                                       │
│  Flow per tool execution (exec, write, edit, etc.):                   │
│  1. Sync local workspace → sandbox (push latest host state)           │
│  2. Execute the tool inside the sandbox                               │
│  3. Sync sandbox → local workspace (pull agent's changes)             │
│                                                                       │
│  Pros:                                                                │
│  • IDE on host sees agent's changes in real-time                      │
│  • Agent sees your local edits in real-time                           │
│  • Familiar model — feels like local development                      │
│                                                                       │
│  Cons:                                                                │
│  • Sync overhead on EVERY tool call — latency scales with repo size   │
│  • Large repos (monorepos, node_modules) become painfully slow        │
│  • Two-way sync can cause conflicts if both sides edit simultaneously │
│                                                                       │
│  Best for: development / debugging where you're editing locally       │
│  in your IDE and want the sandbox to see your latest changes.         │
└───────────────────────────────────────────────────────────────────────┘
```

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Remote Mode  (mode: "remote")                      │
│                                                                       │
│  Canonical workspace: SANDBOX                                         │
│                                                                       │
│  ┌────────────┐   seed ONCE            ┌────────────────────────┐     │
│  │  HOST      │   at sandbox create    │  SANDBOX               │     │
│  │            │ ─────────────────────▶ │                        │     │
│  │  /project  │                        │  /sandbox/project      │     │
│  │  (initial  │   NO further sync      │  (canonical copy)      │     │
│  │   source)  │                        │                        │     │
│  │            │                        │  Agent runs here       │     │
│  │            │                        │  (owns all changes)    │     │
│  └────────────┘                        └────────────────────────┘     │
│                                                                       │
│  Flow:                                                                │
│  1. At sandbox creation: copy workspace into sandbox (one-time seed)  │
│  2. All subsequent tool executions run directly in the sandbox        │
│  3. No sync back to host — workspaces drift apart                     │
│  4. To get results: openshell sandbox download <name> /path ./local   │
│                                                                       │
│  Pros:                                                                │
│  • Zero sync overhead — every tool call runs at local disk speed      │
│  • No conflicts — sandbox is the single source of truth               │
│  • Simpler mental model for always-on / ephemeral agents              │
│                                                                       │
│  Cons:                                                                │
│  • Host workspace goes stale immediately                              │
│  • Need explicit upload/download to exchange files                    │
│  • Can't edit in your IDE and have the agent see changes live         │
│                                                                       │
│  Best for: always-on orchestrators, ephemeral sub-agents, and any     │
│  workflow where the agent is the primary actor (not the human).       │
└───────────────────────────────────────────────────────────────────────┘
```

#### Which Mode for NemoClaw Escapades

| Sandbox | Mode | Rationale |
|---------|------|-----------|
| **Orchestrator** (always-on) | `remote` | Owns its own state (skills, memory, config). No local editing needed — it's the canonical source of truth. |
| **Coding sub-agent** (ephemeral) | `remote` | Orchestrator uploads task + source, agent works, orchestrator downloads results. Created, used, destroyed. |
| **Development / debugging** | `mirror` | When iterating on orchestrator code in your IDE and want the sandbox to reflect edits in real-time. Useful during M1, not for production. |

### OpenShell Config

```json5
{
  agents: {
    defaults: {
      sandbox: {
        mode: "all",
        backend: "openshell",
        scope: "session",        // one sandbox per session
        workspaceAccess: "rw",
      },
    },
  },
  plugins: {
    entries: {
      openshell: {
        enabled: true,
        config: {
          from: "openclaw",
          mode: "remote",        // or "mirror"
          remoteWorkspaceDir: "/sandbox",
          remoteAgentWorkspaceDir: "/agent",
          gpu: false,
          timeoutSeconds: 120,
        },
      },
    },
  },
}
```

---

## 11  Sub-Agents & Multi-Agent Routing

OpenClaw supports spawning background sub-agents for parallel work.

```
┌───────────────────────────────────────────────────────────────┐
│                    Sub-Agent System                           │
│                                                               │
│  Main Agent (depth 0)                                         │
│  session: agent::<id>::main                                   │
│  │                                                            │
│  ├── sessions_spawn(task, ...) ──────────────────────────┐    │
│  │   Returns: { status: "accepted", runId, childKey }    │    │
│  │   Non-blocking.                                       │    │
│  │                                                       │    │
│  │   Sub-Agent (depth 1, "orchestrator")                 │    │
│  │   session: agent::<id>::subagent:<runId>              │    │
│  │   │                                                   │    │
│  │   ├── If maxSpawnDepth >= 2:                         │     │
│  │   │   sessions_spawn(task, ...) ───────────────┐     │     │
│  │   │                                             │     │    │
│  │   │   Sub-Sub-Agent (depth 2, "leaf worker")   │     │     │
│  │   │   session: ...::subagent:<runId2>          │     │     │
│  │   │   • Cannot spawn further                   │     │     │
│  │   │   • Announces result back to depth 1       │     │     │
│  │   │                                             │     │    │
│  │   ├── Announces result back to depth 0           │         │
│  │                                                       │    │
│  ├── /subagents list                                     │    │
│  ├── /subagents kill <id>                                │    │
│  ├── /subagents log <id>                                 │    │
│  └── /subagents info <id>                                │    │
│                                                               │
│  Config:                                                      │
│  • maxSpawnDepth: 1-5 (default 1, recommended 2)              │
│  • maxChildrenPerAgent: 1-20 (default 5)                      │
│  • maxConcurrent: global lane cap (default 8)                 │
│  • runTimeoutSeconds: per-spawn timeout                       │
│  • model / thinking: per-spawn overrides                      │
│                                                               │
│  Thread binding (Discord):                                    │
│  • sessions_spawn with thread: true                           │
│  • Persistent thread-bound sessions                           │
│  • /focus, /unfocus, /session idle, /session max-age          │
│                                                               │
│  Announce chain:                                              │
│  depth-2 → announces to depth-1 (orchestrator)                │
│  depth-1 → announces to depth-0 (main agent → user)           │
└───────────────────────────────────────────────────────────────┘
```

### Multi-Agent Routing

OpenClaw can route inbound channels/accounts/peers to **isolated agents**,
each with its own workspace and per-agent sessions. Configure via
`agents.list[]` with per-agent sandbox, tools, and model settings.

---

## 12  Cron & Scheduled Tasks

OpenClaw has a built-in cron system managed through the `cron` tool and
ticked by the Gateway.

```
┌───────────────────────────────────────────────────────────────┐
│                    Cron System                                │
│                                                               │
│  • cron tool available to the agent                           │
│  • Gateway ticks the scheduler periodically                   │
│  • Jobs run in fresh agent sessions                           │
│  • Delivery to any connected channel                          │
│                                                               │
│  Slash commands:                                              │
│  • /cron list, /cron add, /cron remove                        │
│                                                               │
│  Also available via:                                          │
│  • openclaw cron CLI commands                                 │
│  • Natural language ("Every morning at 9am...")               │
└───────────────────────────────────────────────────────────────┘
```

---

## 13  Security Model

```
┌───────────────────────────────────────────────────────────────┐
│                    Security Layers                            │
│                                                               │
│  1. DM Pairing (default)                                      │
│     Unknown senders get a pairing code; bot doesn't respond   │
│     until approved via: openclaw pairing approve <channel>    │
│                                                               │
│  2. Platform Allowlists                                       │
│     channels.<platform>.allowFrom = ["number1", "number2"]    │
│                                                               │
│  3. Tool Allow/Deny                                           │
│     tools.allow / tools.deny (deny wins)                      │
│     tools.profile (base allowlist)                            │
│                                                               │
│  4. Sandbox Isolation                                         │
│     mode: "non-main" → groups/channels sandboxed              │
│     mode: "all" → everything sandboxed                        │
│                                                               │
│  5. Command Approval                                          │
│     Dangerous commands require explicit user approval         │
│                                                               │
│  6. Elevated Exec (escape hatch)                              │
│     /elevated on|off per session                              │
│     Runs on host, bypasses sandbox                            │
│                                                               │
│  7. Gateway Auth                                              │
│     Token-based WS authentication                             │
│     Tailscale identity headers (serve mode)                   │
│                                                               │
│  Design: Treat all inbound DMs as untrusted input.            │
└───────────────────────────────────────────────────────────────┘
```

---

## 14  Context Files & Memory

OpenClaw uses **injected prompt files** rather than Hermes-style bounded
memory files.

```
┌───────────────────────────────────────────────────────────────┐
│                    Context / Memory                           │
│                                                               │
│  Prompt Files (injected into system prompt):                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │  AGENTS.md  │  │  SOUL.md    │  │  TOOLS.md   │            │
│  │  (behavior, │  │  (persona,  │  │  (tool usage│            │
│  │   rules,    │  │   identity) │  │   guidance) │            │
│  │   context)  │  │             │  │             │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐           │
│  │ IDENTITY.md │  │  USER.md    │  │ HEARTBEAT.md │           │
│  │             │  │  (user      │  │ (time/state  │           │
│  │             │  │   profile)  │  │  awareness)  │           │
│  └─────────────┘  └─────────────┘  └──────────────┘           │
│                                                               │
│  Memory Tools:                                                │
│  • memory_search — search stored memories                     │
│  • memory_get — retrieve specific memory entries              │
│                                                               │
│  Key difference from Hermes:                                  │
│  • OpenClaw uses file-based context injection (AGENTS.md etc) │
│  • Hermes uses bounded MEMORY.md/USER.md with active curation │
│  • Both support the AgentSkills standard for skills           │
│  • Hermes adds Honcho for cross-session user modeling         │
│  • OpenClaw does not have a self-learning loop                │
└───────────────────────────────────────────────────────────────┘
```

---

## 15  Companion Apps & Nodes

OpenClaw has a **node model** where companion devices connect to the Gateway
and expose device-native capabilities.

```
┌───────────────────────────────────────────────────────────────┐
│                    Node Architecture                          │
│                                                               │
│  Gateway ◄──── WS (role: "node") ────► Device Nodes           │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  macOS App (menu bar)                                    │ │
│  │  • Voice Wake + push-to-talk + Talk Mode overlay         │ │
│  │  • WebChat + debug tools                                 │ │
│  │  • Remote gateway control over SSH                       │ │
│  │  • Node mode: system.run, system.notify, canvas, camera  │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  iOS Node                                                │ │
│  │  • Canvas surface                                        │ │
│  │  • Voice Wake, Talk Mode                                 │ │
│  │  • Camera snap/clip, screen recording                    │ │
│  │  • Bonjour + device pairing                              │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  Android Node                                            │ │
│  │  • Chat sessions, voice, Canvas                          │ │
│  │  • Camera, screen recording                              │ │
│  │  • Device commands: notifications, location, SMS,        │ │
│  │    photos, contacts, calendar, motion, app update        │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  Node commands (via node.invoke):                             │
│  • canvas.*, camera.*, screen.record, location.get            │
│  • system.run (macOS only), system.notify (macOS only)        │
└───────────────────────────────────────────────────────────────┘
```

---

## 16  Live Canvas (A2UI)

The Canvas is an **agent-driven visual workspace** served by the Gateway.

- Hosted at `/__openclaw__/canvas/` and `/__openclaw__/a2ui/`
- Agent can push HTML/CSS/JS to the Canvas (`canvas` tool)
- Canvas supports eval, snapshot, reset
- Available on iOS/Android nodes + WebChat

---

## 17  Configuration System

```
┌───────────────────────────────────────────────────────────────┐
│                    Configuration Layers                       │
│                                                               │
│  ~/.openclaw/openclaw.json    (primary config file)           │
│  Environment variables        (override config values)        │
│  CLI flags                    (per-command overrides)         │
│                                                               │
│  Key sections:                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  agent:        model, imageModel, thinking level        │  │
│  │  channels:     per-platform config (telegram, slack...) │  │
│  │  agents:       defaults + per-agent overrides           │  │
│  │  tools:        allow/deny, profiles, elevated           │  │
│  │  skills:       entries, load.extraDirs, allowBundled    │  │
│  │  plugins:      entries (openshell, etc.)                │  │
│  │  gateway:      bind, auth, tailscale                    │  │
│  │  browser:      enabled, color, extraArgs                │  │
│  │  session:      threadBindings, defaults                 │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

---

## 18  Setup & Installation

```bash
# Install
npm install -g openclaw@latest

# Guided setup (recommended)
openclaw onboard --install-daemon

# Start the gateway
openclaw gateway --port 18789 --verbose

# Talk to the agent
openclaw agent --message "Hello" --thinking high

# Diagnose issues
openclaw doctor
```

---

## 19  OpenClaw vs Hermes — Feature Comparison

| Feature | OpenClaw | Hermes |
|---------|----------|--------|
| **Language** | TypeScript (89%) | Python (92%) |
| **Runtime** | Node.js | Python (uv) |
| **Stars** | 341k | 17k |
| **Channels** | 25+ (core + plugin) | 6 (Telegram, Discord, Slack, WhatsApp, Signal, CLI) |
| **Companion apps** | macOS, iOS, Android | None (CLI + messaging) |
| **Canvas** | A2UI visual workspace | None |
| **Sandbox backends** | Docker, SSH, OpenShell | Local, Docker, SSH, Daytona, Singularity, Modal |
| **Skills format** | AgentSkills standard | AgentSkills standard |
| **Agent-created skills** | No | Yes (core feature) |
| **Self-learning loop** | No | Yes (defining feature) |
| **Memory** | Context files (AGENTS.md, SOUL.md) + memory tools | Bounded MEMORY.md/USER.md + Honcho + session search |
| **User modeling** | None | Honcho (cross-session dialectic) |
| **Sub-agents** | Yes (depth 1-5, thread binding) | Yes (isolated sessions) |
| **Cron** | Yes (via Gateway) | Yes (via Gateway) |
| **Plugin system** | Full (channels, tools, skills, providers, speech, image) | MCP tools |
| **Multi-agent** | Yes (per-agent workspace, sandbox, tools) | Via sessions |
| **OpenShell** | Native backend | Not integrated |
| **RL/training** | No | Yes (environments, trajectories, Atropos) |
| **Migration** | — | `hermes claw migrate` from OpenClaw |
| **Node model** | Device nodes (camera, voice, screen) | None |

---

## 20  Answers to Design Doc Questions

### Q6: Does NemoClaw provide a harness? Or computer use?

**OpenClaw provides the harness model we're looking for.** The Gateway is the
harness — it's a long-running daemon that:
- Connects to messaging channels (Slack in our case)
- Routes messages to agent sessions
- Manages sandbox lifecycle (OpenShell in our case)
- Ticks cron jobs
- Delivers responses

NemoClaw + OpenShell is the sandbox execution layer, not the harness itself.
**OpenClaw's Gateway is the orchestrator** that wires everything together.

For our project: we need to build a simplified version of this Gateway pattern
— a long-running process that connects Slack, manages sessions, delegates to
the agent runtime, and manages OpenShell sandboxes.

### Q7: What should be the "main brain" — where does the orchestrator run?

**The Gateway pattern is the answer.** In OpenClaw:
- The Gateway runs on a host machine (local, VPS, or cloud)
- It's supervised by launchd (macOS) or systemd (Linux)
- It binds to localhost and optionally exposes via Tailscale/SSH tunnel
- It owns all state: sessions, cron jobs, channel connections

For NemoClaw Escapades: the orchestrator should be a **long-running Gateway
process** on a server (or your own machine), supervised by systemd. It connects
to Slack, manages OpenShell sandboxes, and runs the agent loop.

### Q8: Can existing slackbot workflows convert to NemoClaw policies?

**Yes, through the skills pattern.** In OpenClaw, workflows are encoded as
skills (SKILL.md files) that teach the agent when and how to use tools. Your
existing slackbot workflows can be converted to SKILL.md files with:
- **When to Use** section describing the trigger conditions
- **Procedure** section with step-by-step instructions
- **metadata.openclaw.requires** for gating (required tools, env vars, config)

The NemoClaw policy engine would load these skills and match them to incoming
requests, similar to how OpenClaw's Pi agent scans the skills list.

### Q9: Can we auto-identify a workflow's required permissions?

The real question here is: **given a skill (SKILL.md), can we automatically
generate the OpenShell network policy that the sandbox needs to run it?**

This is a two-layer problem. OpenClaw skills declare what the *agent* needs
(binaries, env vars, config flags). OpenShell policies declare what the
*sandbox* is allowed to do (network endpoints, filesystem paths, binary
access). Today these are written independently — the skill author and the
policy author must coordinate manually. For NemoClaw Escapades, we want to
close this gap.

**Proposed approach: skill-to-policy derivation.**

1. **Skills declare their requirements** (OpenClaw pattern, already standard):

```yaml
metadata:
  openclaw:
    requires:
      bins: ["git", "docker"]
      env: ["GITHUB_TOKEN"]
      config: ["browser.enabled"]
```

2. **Add an `infrastructure` block** to the skill metadata that declares the
   network and filesystem requirements at the OpenShell level:

```yaml
metadata:
  nemoclaw:
    infrastructure:
      network:
        - host: api.github.com
          port: 443
          protocol: rest
          tls: terminate
          rules:
            - allow: { method: GET, path: "/**" }
            - allow: { method: "*", path: "/repos/org/repo/**" }
        - host: messages.local
          port: 9876
      filesystem:
        read_write: [/sandbox/src, /tmp]
      binaries: [/usr/bin/git, /usr/local/bin/gh]
```

3. **A policy generator** reads the skill's `infrastructure` block and
   produces a valid OpenShell policy YAML:

```bash
nemoclaw policy generate --from skills/code-review/SKILL.md
# → outputs policies/code-review-sandbox.yaml
```

4. **The orchestrator auto-applies** the generated policy when creating a
   sandbox for that skill:

```bash
openshell sandbox create --policy policies/code-review-sandbox.yaml -- claude
```

**Fallback for skills without `infrastructure` metadata:** Use OpenShell's
deny-and-approve workflow (see
[OpenShell Deep Dive §16-Q9](openshell_deep_dive.md#q9-can-we-auto-identify-a-workflows-required-permissions)):
run the skill in a sandbox with minimal policy, observe denials in the TUI,
and export the resulting approved set as the skill's policy. This
trial-and-approve output can then be written back into the skill's
`infrastructure` block for future runs.

**Net result:** Skills become self-describing — they declare not just what
tools they need, but what sandbox policy they require. The orchestrator reads
the skill, generates the policy, creates the sandbox, and runs the workflow
with exactly the right permissions. No manual policy authoring for
well-annotated skills.

### Q10: Should each workflow run in its own sandbox container?

**Yes — OpenClaw's `scope: "session"` does exactly this.** Each session gets
its own sandbox container. For NemoClaw:
- Set `sandbox.scope: "session"` (one sandbox per workflow invocation)
- Or `sandbox.scope: "agent"` (one sandbox per agent type — coding agent,
  review agent, etc.)

The session-scoped model provides the best isolation for parallel workflows.

### Q11: How to set up Claude Code in an OpenShell container? Input/output contract?

**First, an important clarification: OpenClaw's Pi agent IS a coding agent.**

Pi is not a thin wrapper — it's a full agentic coding system comparable to
Claude Code, Codex CLI, or Gemini CLI. It has tool execution (`exec`, `read`,
`write`, `edit`, `apply_patch`, `browser`), context management (compression,
prompt caching), sub-agent delegation, and it can call any configured model
provider (OpenAI, Anthropic, Google, OpenRouter, custom endpoints). When
NemoClaw runs OpenClaw inside an OpenShell sandbox, Pi is the coding agent
that executes tasks — backed by whichever LLM the inference routing is
configured to use.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Coding Agent Options in OpenShell                  │
│                                                                       │
│  ┌───────────────────┐  ┌────────────────────┐  ┌───────────────────┐ │
│  │  OpenClaw Pi      │  │  Claude Code       │  │  Codex CLI        │ │
│  │                   │  │                    │  │                   │ │
│  │  Full agentic     │  │  Anthropic's       │  │  OpenAI's         │ │
│  │  runtime with:    │  │  coding agent:     │  │  coding agent:    │ │
│  │  • ANY model via  │  │  • Claude models   │  │  • Codex/GPT      │ │
│  │    provider config│  │    only            │  │    models only    │ │
│  │  • 20+ built-in   │  │  • File editing,   │  │  • File editing,  │ │
│  │    tools          │  │    terminal, search│  │    terminal       │ │
│  │  • Skills system  │  │  • MCP tool support│  │  • Sandboxed      │ │
│  │  • Sub-agents     │  │  • AGENTS.md       │  │    execution      │ │
│  │  • Cron, browser, │  │    context files   │  │                   │ │
│  │    canvas         │  │                    │  │                   │ │
│  │  • Memory tools   │  │                    │  │                   │ │
│  │                   │  │                    │  │                   │ │
│  │  Create sandbox:  │  │  Create sandbox:   │  │  Create sandbox:  │ │
│  │  openshell sandbox│  │  openshell sandbox │  │  openshell sandbox│ │
│  │  create --from    │  │  create -- claude  │  │  create -- codex  │ │
│  │  openclaw         │  │                    │  │                   │ │
│  └───────────────────┘  └────────────────────┘  └───────────────────┘ │
│                                                                       │
│  All three run inside OpenShell sandboxes with the same isolation:    │
│  • Landlock + seccomp + network namespace                             │
│  • Policy-controlled network egress                                   │
│  • Inference routing via inference.local (Pi uses this natively;      │
│    Claude Code and Codex call their own APIs, routed by policy)       │
└───────────────────────────────────────────────────────────────────────┘
```

**For NemoClaw Escapades, this means we have a choice for Milestone 3:**

| Option | Sandbox command | Model flexibility | Tool richness | Self-learning ready |
|--------|----------------|-------------------|---------------|---------------------|
| **Pi (via OpenClaw/NemoClaw)** | `openshell sandbox create --from openclaw` | Any model (via inference routing) | Highest (20+ tools, skills, sub-agents, cron) | Yes (skills system built in) |
| **Claude Code** | `openshell sandbox create -- claude` | Claude models only | Good (file editing, terminal, MCP) | No (no skills system) |
| **Codex CLI** | `openshell sandbox create -- codex` | Codex/GPT models only | Basic (file editing, terminal) | No |

**Recommendation:** Use Pi (via NemoClaw's OpenClaw sandbox) as the primary
coding agent. It's model-agnostic (swap between Nemotron, Claude, GPT via
`openshell inference set`), has the richest tool set, and the skills system
means Milestone 4 (self-improvement loop) builds directly on Milestone 3's
infrastructure. Use Claude Code as an alternative for tasks where Anthropic's
models are specifically preferred.

**The input/output contract is the same regardless of which agent runs:**

1. Create sandbox: `openshell sandbox create --from openclaw` (or `-- claude`)
2. Seed workspace: `openshell sandbox upload <name> ./project /sandbox/src`
   (or use `mode: "remote"` in OpenShell plugin config to seed on creation)
3. Send task: via NMB `task.assign` message (preferred) or SSH exec
4. Monitor progress: via NMB `task.progress` stream (preferred) or `openshell logs`
5. Collect results: via NMB `task.complete` message (preferred) or
   `openshell sandbox download <name> /sandbox/src ./output`
6. Cleanup: `openshell sandbox delete <name>`

### Q14: How to add another server backend to the Slack integration?

**OpenClaw's channel adapter pattern is the blueprint.** Each channel is an
adapter in `src/channels/` (or a plugin) that:
1. Connects to the platform API (Baileys for WhatsApp, grammY for Telegram,
   Bolt for Slack, etc.)
2. Translates incoming events to a common session routing format
3. Handles outgoing delivery in platform-specific format

To add a backend: create a new adapter following the same interface. OpenClaw's
architecture makes channels independent — they share the session router and
delivery engine.

For NemoClaw: build a generic `Connector` base class (as in the design doc),
with Slack as the first implementation. The base class handles session routing
and delivery; the Slack adapter handles Bolt-specific concerns.

---

## 21  What to Lift for NemoClaw Escapades

### Milestone 1 — Foundation

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| Gateway architecture | Build a long-running orchestrator daemon that connects Slack and manages sessions |
| Channel adapter pattern | Implement a generic Connector base class; Slack as first adapter (use Bolt SDK) |
| WS control plane | Consider a lightweight control interface for monitoring (optional for M1) |
| `openclaw onboard` pattern | Create a setup wizard for first-time configuration |

### Milestone 2 — Knowledge Management

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| Context files (AGENTS.md, SOUL.md) | Use similar prompt injection files for agent behavior and context |
| Skills as knowledge carriers | Create skills that teach the agent how to query SecondBrain |

### Milestone 3 — Coding Agent

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| OpenShell backend | Use OpenClaw's exact OpenShell integration pattern: create → ssh-config → exec |
| `sandbox.scope: "session"` | One sandbox per coding task (isolation + auto-cleanup) |
| `mode: "remote"` | Let the sandbox workspace be canonical; extract PR on completion |
| Sub-agent spawning (`sessions_spawn`) | Orchestrator spawns coding agent as a sub-agent |
| Tool profiles ("coding") | Give the coding agent only `group:runtime + group:fs + group:sessions` |

### Milestone 4 — Self-Improvement Loop

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| Skills system (AgentSkills format) | Use the same SKILL.md format (shared standard with Hermes) |
| Skills gating (metadata.openclaw.requires) | Add requirement declarations to skills |
| Context files (AGENTS.md) | Use as the base for self-updating agent instructions |

**Note:** OpenClaw does NOT have a self-learning loop. For this milestone,
**Hermes is the primary reference** (see
[Hermes Deep Dive §14](hermes_deep_dive.md#14--the-self-learning-loop)).

### Milestone 5 — Review Agent

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| Sub-agent orchestrator pattern | Main → orchestrator (depth 1) → coding + review workers (depth 2) |
| `maxSpawnDepth: 2` | Allow orchestrator to manage coding and review as leaf workers |
| Announce chain | Review agent announces feedback to orchestrator, which routes to coding agent |

### Milestone 6 — Professional KB

| OpenClaw Pattern | How to Apply |
|------------------|-------------|
| Cron system | Schedule periodic scraping as cron jobs |
| Slack channel adapter | Read from Slack channels programmatically |
| Skills for scraping workflows | Create skills that define the scraping/summarization procedure |
