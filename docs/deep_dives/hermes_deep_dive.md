# Hermes Agent — Deep Dive

> **Source:** [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
> (17k stars, MIT license, v0.5.0 as of 2026-03-28)
>
> **Official docs:** [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs)
>
> **Last reviewed:** 2026-03-29

---

## Table of Contents

1. [Overview](#1--overview)
2. [High-Level Architecture](#2--high-level-architecture)
3. [Repository Structure](#3--repository-structure)
4. [The Agent Loop](#4--the-agent-loop)
5. [Provider / Inference Backend System](#5--provider--inference-backend-system)
6. [Tools Runtime](#6--tools-runtime)
7. [Terminal Backends & Sandbox Execution](#7--terminal-backends--sandbox-execution)
8. [Skills System (Procedural Memory)](#8--skills-system-procedural-memory)
9. [Memory System](#9--memory-system)
10. [Honcho Integration (Cross-Session User Modeling)](#10--honcho-integration-cross-session-user-modeling)
11. [Messaging Gateway](#11--messaging-gateway)
12. [Cron Scheduling](#12--cron-scheduling)
13. [Sub-Agent Delegation](#13--sub-agent-delegation)
14. [The Self-Learning Loop](#14--the-self-learning-loop)
15. [RL / Environments / Training](#15--rl--environments--training)
16. [ACP Editor Integration](#16--acp-editor-integration)
17. [Setup & Installation](#17--setup--installation)
18. [Answers to Design Doc Questions](#18--answers-to-design-doc-questions)
19. [What to Lift for NemoClaw Escapades](#19--what-to-lift-for-nemoclaw-escapades)

---

## 1  Overview

Hermes Agent is a self-improving AI agent built by Nous Research. Its defining
feature — and the reason it's a key reference for this project — is the
**closed learning loop**: the agent creates skills from experience, improves
them during use, nudges itself to persist knowledge, searches its own past
conversations, and builds a deepening model of who the user is across sessions.

It runs on anything from a $5 VPS to a GPU cluster, is not tied to a laptop
(talk to it from Telegram while it works on a cloud VM), and supports any LLM
provider (OpenRouter, OpenAI, Anthropic, custom endpoints) with zero code
changes.

---

## 2  High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          ENTRY POINTS                                   │
│                                                                         │
│  ┌──────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────────┐   │
│  │  CLI TUI │  │   Gateway    │  │  ACP/RPC   │  │  Batch Runner    │   │
│  │ (cli.py) │  │ (gateway/)   │  │ (acp_      │  │ (batch_runner.py)│   │
│  │          │  │              │  │  adapter/) │  │                  │   │
│  └────┬─────┘  └──────┬───────┘  └─────┬──────┘  └────────┬─────────┘   │
│       │               │                │                   │            │
│       └───────────────┴────────┬───────┴───────────────────┘            │
│                                │                                        │
│                                ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    AIAgent  (run_agent.py)                      │    │
│  │                                                                 │    │
│  │  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │    │
│  │  │   Prompt     │  │   Provider    │  │   Tool Dispatcher    │  │    │
│  │  │   Builder    │  │   Resolver    │  │   (model_tools.py)   │  │    │
│  │  │              │  │               │  │                      │  │    │
│  │  │ • system     │  │ • chat_comp   │  │ • 40+ built-in       │  │    │
│  │  │   prompt     │  │ • codex_resp  │  │ • MCP tools          │  │    │
│  │  │ • memory     │  │ • anthropic   │  │ • skills tools       │  │    │
│  │  │ • skills     │  │   _messages   │  │ • cron tools         │  │    │
│  │  │ • context    │  │               │  │ • memory tools       │  │    │
│  │  │   files      │  │ Provider      │  │ • honcho tools       │  │    │
│  │  │ • honcho     │  │ fallback +    │  │ • session tools      │  │    │
│  │  │   context    │  │ retry logic   │  │ • terminal tools     │  │    │
│  │  └──────────────┘  └───────────────┘  └──────────────────────┘  │    │
│  │                                                                 │    │
│  │  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │    │
│  │  │   Context    │  │   Session     │  │   Callback           │  │    │
│  │  │   Compressor │  │   Persistence │  │   System             │  │    │
│  │  │              │  │   (SQLite)    │  │                      │  │    │
│  │  │ • mid-convo  │  │               │  │ • tool_progress      │  │    │
│  │  │   compress   │  │ • lineage     │  │ • thinking           │  │    │
│  │  │ • prompt     │  │ • FTS5        │  │ • stream_delta       │  │    │
│  │  │   caching    │  │   search      │  │ • clarify            │  │    │
│  │  └──────────────┘  └───────────────┘  └──────────────────────┘  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                │                                        │
│                ┌───────────────┼───────────────────┐                    │
│                ▼               ▼                   ▼                    │
│  ┌──────────────────┐  ┌─────────────┐  ┌──────────────────────┐        │
│  │  Terminal Backend│  │   Honcho    │  │   Skills Store       │        │
│  │                  │  │   Memory    │  │   (~/.hermes/skills/)│        │
│  │ • local          │  │   API       │  │                      │        │
│  │ • docker         │  │             │  │ • bundled            │        │
│  │ • ssh            │  │ • user peer │  │ • hub-installed      │        │
│  │ • daytona        │  │ • AI peer   │  │ • agent-created      │        │
│  │ • singularity    │  │ • dialectic │  │ • external dirs      │        │
│  │ • modal          │  │   reasoning │  │                      │        │
│  └──────────────────┘  └─────────────┘  └──────────────────────┘        │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                     Persistence Layer                           │    │
│  │                                                                 │    │
│  │  ┌────────────────┐  ┌───────────────────┐  ┌────────────────┐  │    │
│  │  │ ~/.hermes/     │  │ state.db (SQLite) │  │ cron/          │  │    │
│  │  │  memories/     │  │ • sessions        │  │  jobs.json     │  │    │
│  │  │  MEMORY.md     │  │ • conversation    │  │  output/       │  │    │
│  │  │  USER.md       │  │   history         │  │                │  │    │
│  │  │  config.yaml   │  │ • FTS5 index      │  │                │  │    │
│  │  └────────────────┘  └───────────────────┘  └────────────────┘  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Design Themes

- **Prompt stability matters** — the system prompt is a frozen snapshot at
  session start; changes take effect next session (preserves LLM prefix cache).
- **Tool execution is observable and interruptible** — Ctrl+C or a new message
  cancels the current tool call.
- **Session persistence survives long-running use** — SQLite with lineage
  tracking across compression splits.
- **All frontends share one agent core** — CLI, Gateway, ACP, and Batch Runner
  all instantiate the same `AIAgent`.
- **Optional subsystems are loosely coupled** — Honcho, cron, skills, MCP are
  all independently toggleable.

---

## 3  Repository Structure

```
hermes-agent/
├── run_agent.py              # AIAgent — the core orchestration engine
├── cli.py                    # Interactive terminal UI (TUI)
├── model_tools.py            # Tool discovery, schema building, dispatch
├── toolsets.py               # Named tool bundles and presets
├── hermes_state.py           # SQLite session/state database
├── hermes_constants.py       # Shared constants
├── hermes_time.py            # Time utilities
├── batch_runner.py           # Batch trajectory generation for RL/SFT
├── trajectory_compressor.py  # Compress trajectories for training
├── toolset_distributions.py  # Toolset sampling for data generation
│
├── agent/                    # Prompt building, compression, caching
│   ├── prompt_builder.py     #   System prompt assembly
│   ├── prompt_caching.py     #   Provider-specific cache hints
│   ├── context_compressor.py #   Mid-conversation compression
│   └── ...
│
├── hermes_cli/               # CLI entrypoints
│   ├── auth.py               #   API key management
│   ├── setup.py              #   Setup wizard
│   ├── models.py             #   Model selection
│   ├── config.py             #   Config get/set
│   ├── doctor.py             #   Diagnostic checks
│   └── ...
│
├── tools/                    # Tool implementations
│   ├── registry.py           #   Central tool registry
│   ├── terminal_tool.py      #   Terminal execution
│   ├── memory_tools.py       #   MEMORY.md / USER.md management
│   ├── skill_tools.py        #   Skill CRUD
│   ├── honcho_tools.py       #   Honcho memory tools
│   ├── session_tools.py      #   Cross-session communication
│   ├── browser_tool.py       #   Browser control
│   └── environments/         #   Terminal backend implementations
│       ├── local.py
│       ├── docker.py
│       ├── ssh.py
│       ├── daytona.py
│       ├── singularity.py
│       └── modal.py
│
├── gateway/                  # Messaging gateway
│   ├── run.py                #   Main gateway process
│   ├── session.py            #   Session routing and lifecycle
│   ├── delivery.py           #   Output delivery to platforms
│   ├── pairing.py            #   DM pairing security
│   ├── hooks.py              #   Event hook system
│   ├── mirror.py             #   Cross-platform mirroring
│   └── platforms/            #   Platform adapters
│       ├── telegram.py
│       ├── discord.py
│       ├── slack.py
│       ├── whatsapp.py
│       ├── signal.py
│       └── ...
│
├── cron/                     # Scheduled task system
│   ├── scheduler.py          #   Job scheduling and ticking
│   └── storage.py            #   jobs.json persistence
│
├── honcho_integration/       # Honcho memory integration
│
├── skills/                   # Bundled skills
├── optional-skills/          # Official optional skills
│
├── acp_adapter/              # ACP editor integration (JSON-RPC)
├── acp_registry/             # ACP manifest
│
├── environments/             # RL / benchmark environments
├── tinker-atropos/           # Atropos RL submodule
│
├── docker/                   # Docker configs
├── scripts/                  # Install/setup scripts
├── tests/                    # Test suite
└── docs/                     # Documentation source
```

---

## 4  The Agent Loop

The agent loop is the heart of Hermes. It lives in `run_agent.py` as the
`AIAgent` class. Every entry point (CLI, Gateway, ACP, Batch Runner)
instantiates an `AIAgent` and calls `run_conversation()`.

### Turn Lifecycle

```
┌──────────────────────────────────────────────────────────────────────┐
│                     run_conversation()                               │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │  Generate effective      │
                    │  task_id                 │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Append user message     │
                    │  to conversation         │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Load / build cached     │
                    │  system prompt           │
                    │  (frozen snapshot)       │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Maybe preflight-compress│
                    │  (if context is large)   │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Build api_messages      │
                    │  + inject ephemeral      │
                    │  prompt layers           │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │  Apply prompt caching    │
                    │  (provider-specific)     │
                    └─────────────┬────────────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │  Make interruptible API call          │
              │  (selected via provider resolver)     │
              │                                       │
              │  Modes:                               │
              │  • chat_completions (OpenAI-compat)   │
              │  • codex_responses  (Codex/Responses) │
              │  • anthropic_messages (native Claude) │
              └───────────────────┬───────────────────┘
                                  │
                         ┌────────┴────────┐
                         │                 │
                    Tool calls?       Final text?
                         │                 │
                         ▼                 ▼
              ┌─────────────────┐  ┌──────────────────┐
              │  Execute tools  │  │  Persist session │
              │  (seq or conc)  │  │  Cleanup, return │
              │  Append results │  │  response        │
              │  to history     │  └──────────────────┘
                         │  ↩ LOOP back    │
              └─────────────────┘
```

### API Modes

| API Mode | Used For |
|----------|----------|
| `chat_completions` | OpenAI-compatible endpoints (OpenRouter, custom) |
| `codex_responses` | OpenAI Codex / Responses API |
| `anthropic_messages` | Native Anthropic Messages API |

The mode is resolved from explicit args, provider selection, and base URL
heuristics. Switching providers requires no code changes — just
`hermes model <provider:model>`.

### Budget & Fallback

- Hermes tracks a shared iteration budget across parent and sub-agents.
- Budget pressure hints are injected near the end of the iteration window.
- Fallback model support lets the agent switch providers when the primary fails.

---

## 5  Provider / Inference Backend System

Hermes has a **shared runtime provider resolver** used by CLI, gateway, cron,
ACP, and auxiliary calls. This is the subsystem most relevant to the
NemoClaw question of "is Hermes compatible with inference hub?"

```
┌───────────────────────────────────────────────────────────────┐
│                 Provider Configuration                        │
│                                                               │
│  config.yaml / env vars / CLI flags                           │
│                                                               │
│  providers:                                                   │
│    openrouter:                                                │
│      api_key: ...                                             │
│      base_url: https://openrouter.ai/api/v1                   │
│    openai:                                                    │
│      api_key: ...                                             │
│    anthropic:                                                 │
│      api_key: ...                                             │
│    custom:                                                    │
│      api_key: ...                                             │
│      base_url: https://your-endpoint.com/v1  ◄── ANY          │
│                                                  OpenAI-      │
│                                                  compatible   │
│                                                  endpoint     │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│              Provider Runtime Resolver                        │
│                                                               │
│  Input:  model string (e.g. "openrouter:deepseek/...")        │
│  Output: (api_mode, base_url, api_key, model_id)              │
│                                                               │
│  Resolution chain:                                            │
│    1. Explicit CLI/config provider prefix                     │
│    2. Model → provider mapping table                          │
│    3. Base URL heuristics                                     │
│    4. Fallback to default provider                            │
└───────────────────────────────────────────────────────────────┘
```

### Supported Providers (non-exhaustive)

| Provider | Notes |
|----------|-------|
| Nous Portal | Nous Research's own endpoint |
| OpenRouter | 200+ models, single API key |
| OpenAI | GPT-4, Codex, etc. |
| Anthropic | Native Messages API |
| z.ai / GLM | |
| Kimi / Moonshot | |
| MiniMax | |
| **Any OpenAI-compatible endpoint** | Set `base_url` to your endpoint |

**Key insight for NemoClaw:** Because Hermes supports any OpenAI-compatible
endpoint via `base_url`, it can point at an NVIDIA Inference Hub endpoint
without modification, as long as that endpoint speaks the OpenAI chat
completions API. See [Q3 in §18](#q3-is-hermes-compatible-with-inference-hub).

---

## 6  Tools Runtime

Tools are self-registering Python functions grouped into **toolsets**.

```
┌───────────────────────────────────────────────────────────────┐
│                    Tool Registration                          │
│                                                               │
│  tools/                                                       │
│  ├── registry.py      ← Central registry (register/dispatch)  │
│  ├── terminal_tool.py ← bash, process management              │
│  ├── memory_tools.py  ← MEMORY.md / USER.md CRUD              │
│  ├── skill_tools.py   ← skills_list, skill_view, skill_manage │
│  ├── honcho_tools.py  ← honcho_profile/search/context/conclude│
│  ├── session_tools.py ← sessions_list/history/send            │
│  ├── browser_tool.py  ← CDP browser control                   │
│  ├── cron_tools.py    ← cronjob create/list/update/remove     │
│  └── ...              ← read, write, edit, search, etc.       │
│                                                               │
│  Each module calls registry.register() at import time.        │
│  model_tools.py discovers and builds the schema for the LLM.  │
└───────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                    Toolset System                             │
│                                                               │
│  Toolsets = named bundles of tools                            │
│                                                               │
│  Resolution:                                                  │
│    explicit enabled/disabled → platform presets →             │
│    dynamic MCP toolsets → curated special-purpose sets        │
│                                                               │
│  Examples:                                                    │
│    "terminal"    → bash, process_*                            │
│    "files"       → read, write, edit, search                  │
│    "memory"      → memory, session_search                     │
│    "skills"      → skills_list, skill_view, skill_manage      │
│    "web"         → web_search, scrape (needs API key)         │
│    "browser"     → browser_* (CDP-based)                      │
│    "honcho"      → honcho_profile/search/context/conclude     │
│    "cron"        → cronjob                                    │
│    "mcp"         → dynamically loaded from MCP servers        │
│                                                               │
│  Platform presets:                                            │
│    hermes-cli, hermes-telegram, hermes-discord, etc.          │
└───────────────────────────────────────────────────────────────┘
```

### Execution Modes

- **Sequential:** for single or interactive tools.
- **Concurrent:** for multiple non-interactive tools (preserves ordering when
  reinserting results).

### Command Approval

Dangerous commands can be gated behind approval callbacks — the user must
confirm before execution proceeds. This is configurable via allowlists.

---

## 7  Terminal Backends & Sandbox Execution

Hermes supports **six terminal backends**, making it one of the most flexible
agent runtimes for sandboxed execution.

```
┌───────────────────────────────────────────────────────────┐
│                Terminal Backend Selection                 │
│                                                           │
│  hermes config set terminal.backend <backend>             │
│                                                           │
│  ┌─────────┐  ┌─────────┐  ┌──────┐  ┌──────────────┐     │
│  │  local  │  │  docker │  │  ssh │  │  daytona     │     │
│  │         │  │         │  │      │  │  (serverless │     │
│  │ Host OS │  │Container│  │Remote│  │   persist)   │     │
│  │ direct  │  │isolated │  │ host │  │              │     │
│  └─────────┘  └─────────┘  └──────┘  └──────────────┘     │
│                                                           │
│  ┌─────────────┐  ┌──────────────────────────────────┐    │
│  │ singularity │  │  modal (serverless, pay-per-use) │    │
│  │ (HPC)       │  │  hibernates when idle            │    │
│  └─────────────┘  └──────────────────────────────────┘    │
└───────────────────────────────────────────────────────────┘
```

| Backend | Isolation | Persistence | Cost Model |
|---------|-----------|-------------|------------|
| `local` | None (host OS) | Always-on | Free |
| `docker` | Container | Ephemeral or volume-mounted | Free (self-hosted) |
| `ssh` | Remote host | Depends on host | VPS cost |
| `daytona` | Serverless container | Hibernates when idle, wakes on demand | Pay-per-use |
| `singularity` | HPC container | Depends on cluster | HPC allocation |
| `modal` | Serverless container | Hibernates when idle, wakes on demand | Pay-per-use |

### Features

- Per-task CWD overrides
- Background process management
- PTY mode for interactive tools
- Approval callbacks for dangerous commands
- Environment variable passthrough to sandboxes

**Relevance to NemoClaw:** OpenShell is NemoClaw's analog of these terminal
backends. The design pattern is the same — a configurable backend that
provides isolated execution environments.

---

## 8  Skills System (Procedural Memory)

The skills system is Hermes's **procedural memory** — the agent learns
workflows from experience and saves them as reusable skills.

```
┌───────────────────────────────────────────────────────────────┐
│                   Skills Lifecycle                            │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  1. DISCOVERY                                           │  │
│  │     skills_list() → [{name, desc, category}, ...]       │  │
│  │     (~3k tokens — progressive disclosure Level 0)       │  │
│  └──────────────────────────┬──────────────────────────────┘  │
│                              │                                │
│                              ▼                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  2. LOADING                                             │  │
│  │     skill_view(name) → Full SKILL.md + metadata         │  │
│  │     skill_view(name, path) → specific reference file    │  │
│  │     (Level 1 / Level 2 — loaded on demand)              │  │
│  └──────────────────────────┬──────────────────────────────┘  │
│                              │                                │
│                              ▼                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  3. EXECUTION                                           │  │
│  │     Agent follows the skill's procedure                 │  │
│  │     Available as /slash-commands or via natural language│  │
│  └──────────────────────────┬──────────────────────────────┘  │
│                              │                                │
│                              ▼                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  4. SELF-IMPROVEMENT                                    │  │
│  │     After complex tasks (5+ tool calls):                │  │
│  │     • Agent creates new skills (skill_manage create)    │  │
│  │     • Agent patches existing skills (skill_manage patch)│  │
│  │     • Agent rewrites skills (skill_manage edit)         │  │
│  │                                                         │  │
│  │     Triggers:                                           │  │
│  │     • Hit errors/dead-ends, found the working path      │  │
│  │     • User corrected the agent's approach               │  │
│  │     • Discovered a non-trivial workflow                 │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

### Skill File Format (SKILL.md)

```markdown
---
name: my-skill
description: Brief description
version: 1.0.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [python, automation]
    category: devops
    fallback_for_toolsets: [web]
    requires_toolsets: [terminal]
---

# Skill Title

## When to Use
Trigger conditions.

## Procedure
1. Step one
2. Step two

## Pitfalls
- Known failure modes.

## Verification
How to confirm it worked.
```

### Skill Sources

```
┌───────────────────────────────────────────────────────┐
│                  Skill Sources                        │
│                                                       │
│  ┌───────────────────┐  ┌──────────────────────────┐  │
│  │  Bundled          │  │  Agent-Created           │  │
│  │  (ships w/ Hermes)│  │  (auto after complex     │  │
│  │  builtin trust    │  │   tasks — procedural     │  │
│  │                   │  │   memory)                │  │
│  └───────────────────┘  └──────────────────────────┘  │
│                                                       │
│  ┌───────────────────┐  ┌──────────────────────────┐  │
│  │  Skills Hub       │  │  External Directories    │  │
│  │  • official       │  │  (shared across tools)   │  │
│  │  • skills.sh      │  │                          │  │
│  │  • well-known     │  │  skills:                 │  │
│  │  • GitHub direct  │  │    external_dirs:        │  │
│  │  • ClawHub        │  │      - ~/.agents/skills  │  │
│  │  • LobeHub        │  │                          │  │
│  └───────────────────┘  └──────────────────────────┘  │
│                                                       │
│  Trust levels:                                        │
│    builtin > official > trusted > community           │
│  Security: all hub skills are scanned for injection,  │
│    exfiltration, destructive commands                 │
└───────────────────────────────────────────────────────┘
```

### Progressive Disclosure (Token Efficiency)

| Level | Call | Tokens |
|-------|------|--------|
| 0 | `skills_list()` | ~3k (names + descriptions only) |
| 1 | `skill_view(name)` | Varies (full SKILL.md) |
| 2 | `skill_view(name, path)` | Varies (specific reference file) |

The agent only loads what it needs, keeping token costs bounded.

---

## 9  Memory System

Hermes has a **two-file bounded memory** system that persists across sessions.

```
┌───────────────────────────────────────────────────────────────┐
│                  Memory Architecture                          │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  MEMORY.md  (2,200 chars / ~800 tokens)                 │  │
│  │  Agent's personal notes:                                │  │
│  │  • Environment facts (OS, tools, project structure)     │  │
│  │  • Project conventions                                  │  │
│  │  • Tool quirks and workarounds                          │  │
│  │  • Completed task diary entries                         │  │
│  │  • Lessons learned                                      │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  USER.md  (1,375 chars / ~500 tokens)                   │  │
│  │  User profile:                                          │  │
│  │  • Name, role, timezone                                 │  │
│  │  • Communication preferences                            │  │
│  │  • Pet peeves and things to avoid                       │  │
│  │  • Workflow habits                                      │  │
│  │  • Technical skill level                                │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Injection: Frozen snapshot into system prompt at session     │
│  start. Changes during session persist to disk immediately    │
│  but appear in prompt only on next session.                   │
│                                                               │
│  Tools: memory(action="add|replace|remove", ...)              │
│                                                               │
│  Capacity: Agent auto-consolidates when >80% full.            │
│  Security: Entries scanned for injection / exfiltration.      │
└───────────────────────────────────────────────────────────────┘
                               │
                               │  Supplemented by:
                               ▼
┌───────────────────────────────────────────────────────────────┐
│  Session Search  (session_search tool)                        │
│                                                               │
│  • All sessions stored in SQLite with FTS5 full-text search   │
│  • Queries return relevant past conversations                 │
│  • LLM summarization (Gemini Flash) of search results         │
│  • Unlimited capacity (all sessions ever)                     │
│  • On-demand — costs tokens only when searched                │
│                                                               │
│  Memory = key facts always in context (~1,300 tokens fixed)   │
│  Session search = "did we discuss X last week?"               │
└───────────────────────────────────────────────────────────────┘
```

---

## 10  Honcho Integration (Cross-Session User Modeling)

[Honcho](https://honcho.dev/) adds a deeper AI-native memory layer on top of
the built-in MEMORY.md/USER.md system.

```
┌───────────────────────────────────────────────────────────────────┐
│                   Memory Stack (Hybrid Mode)                      │
│                                                                   │
│  ┌─────────────────────────────────────┐                          │
│  │  System Prompt (frozen at session   │                          │
│  │  start)                             │                          │
│  │                                     │                          │
│  │  ┌──────────────┐ ┌──────────────┐  │                          │
│  │  │  MEMORY.md   │ │  USER.md     │  │  Built-in memory         │
│  │  │  (agent's    │ │  (user       │  │  (~1,300 tokens fixed)   │
│  │  │   notes)     │ │   profile)   │  │                          │
│  │  └──────────────┘ └──────────────┘  │                          │
│  │                                     │                          │
│  │  ┌──────────────────────────────┐   │                          │
│  │  │  Honcho Context              │   │  Cross-session user      │
│  │  │  (auto-injected dialectic    │   │  modeling (cloud or      │
│  │  │   summary of user model)     │   │  self-hosted)            │
│  │  └──────────────────────────────┘   │                          │
│  └─────────────────────────────────────┘                          │
│                                                                   │
│  ┌──────────────────────────────────────┐                         │
│  │  On-Demand (tool calls)              │                         │
│  │                                      │                         │
│  │  • honcho_profile  (fast, no LLM)    │                         │
│  │  • honcho_search   (semantic, no LLM)│                         │
│  │  • honcho_context  (dialectic Q&A,   │                         │
│  │                      uses LLM)       │                         │
│  │  • honcho_conclude (persist a fact)  │                         │
│  │  • session_search  (local FTS5)      │                         │
│  └──────────────────────────────────────┘                         │
└───────────────────────────────────────────────────────────────────┘
```

### Honcho's Dual-Peer Architecture

```
┌─────────────────────┐          ┌─────────────────────┐
│   User Peer         │          │   AI Peer           │
│                     │          │                     │
│ Observed from user  │          │ Observed from       │
│ messages:           │          │ assistant messages: │
│ • preferences       │          │ • knowledge         │
│ • goals             │          │ • behavior          │
│ • communication     │          │ • identity          │
│   style             │          │   (SOUL.md seed)    │
│ • expertise level   │          │                     │
└─────────────────────┘          └─────────────────────┘
           │                              │
           └─────────┬────────────────────┘
                     │
                     ▼
        ┌───────────────────────┐
        │  Dialectic Reasoning  │
        │  (cross-references    │
        │   both peers)         │
        │                       │
        │  Reasoning level      │
        │  scales with message  │
        │  complexity           │
        └───────────────────────┘
```

### Honcho vs Built-in Memory

| Feature | Built-in Memory | Honcho Memory |
|---------|----------------|---------------|
| Storage | Local files (`~/.hermes/memories/`) | Cloud API or self-hosted Docker |
| Scope | Agent-level notes + user profile | Deep user modeling via dialectic reasoning |
| Persistence | Same machine | Across machines and platforms |
| Query | Injected into system prompt | Prefetched + on-demand via tools |
| Content | Manually curated by agent | Automatically learned from conversations |
| Write | `memory` tool (add/replace/remove) | `honcho_conclude` tool |

### Memory Modes

| Mode | Effect |
|------|--------|
| `hybrid` (default) | Both Honcho and local files |
| `honcho` | Honcho only — skip local file writes |

---

## 11  Messaging Gateway

The gateway is a long-running process that connects Hermes to external
messaging platforms.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Messaging Gateway                            │
│                    (gateway/run.py)                             │
│                                                                 │
│  Config sources:                                                │
│    .env → config.yaml → gateway.json                            │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Platform Adapters  (gateway/platforms/)                   │ │
│  │                                                            │ │
│  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────────┐ │ │
│  │  │ Telegram │ │  Discord   │ │  Slack   │ │  WhatsApp    │ │ │
│  │  │ (grammY) │ │ (discord   │ │ (Bolt)   │ │ (Baileys)    │ │ │
│  │  │          │ │   .js)     │ │          │ │              │ │ │
│  │  └──────────┘ └────────────┘ └──────────┘ └──────────────┘ │ │
│  │                                                            │ │
│  │  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌──────────────┐ │ │
│  │  │  Signal  │ │  iMessage  │ │   IRC    │ │   Teams      │ │ │
│  │  │(signal-  │ │(BlueBubbles│ │          │ │ (Bot         │ │ │
│  │  │ cli)     │ │  / imsg)   │ │          │ │  Framework)  │ │ │
│  │  └──────────┘ └────────────┘ └──────────┘ └──────────────┘ │ │
│  │                                                            │ │
│  │  + Matrix, Feishu, LINE, Mattermost, Nextcloud Talk,       │ │
│  │    Nostr, Twitch, WeChat, WebChat, ...                     │ │
│  └────────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Session Router  (gateway/session.py)                      │ │
│  │                                                            │ │
│  │  Incoming message → session key (platform + chat ID)       │ │
│  │  → route to existing or new AIAgent session                │ │
│  │  → maintain per-chat continuity                            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Authorization                                             │ │
│  │  • Platform allowlists                                     │ │
│  │  • DM pairing (pairing.py) — code-based auth               │ │
│  │  • Gateway-wide allowlists                                 │ │
│  └────────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Delivery  (gateway/delivery.py)                           │ │
│  │  • Deliver to origin chat                                  │ │
│  │  • Deliver to home channel                                 │ │
│  │  • Deliver to explicit targets                             │ │
│  │  • Mirror to local history                                 │ │
│  └────────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Background Tasks                                          │ │
│  │  • Cron ticking (every 60s)                                │ │
│  │  • Session expiry checks                                   │ │
│  │  • Proactive memory flush before reset/expiry              │ │
│  │  • Cache refreshes                                         │ │
│  │  • Honcho manager lifecycle                                │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12  Cron Scheduling

Cron jobs are **first-class agent tasks**, not just shell commands. Each
scheduled job runs in a fresh `AIAgent` session.

```
┌───────────────────────────────────────────────────────────────┐
│                    Cron System                                │
│                                                               │
│  Creation:                                                    │
│  • /cron add "every 2h" "Check server status"                 │
│  • Natural language: "Every morning at 9am, summarize..."     │
│  • CLI: hermes cron create "0 9 * * *" "Daily report"         │
│                                                               │
│  Schedule formats:                                            │
│  • Relative:  30m, 2h, 1d         (one-shot)                  │
│  • Intervals: every 30m, every 2h  (recurring)                │
│  • Cron:      0 9 * * *            (recurring)                │
│  • ISO:       2026-03-15T09:00:00  (one-shot)                 │
│                                                               │
│  Storage: ~/.hermes/cron/jobs.json                            │
│  Output:  ~/.hermes/cron/output/{job_id}/{timestamp}.md       │
│  Lock:    .tick.lock prevents overlapping scheduler ticks     │
└──────────────────────────────────┬────────────────────────────┘
                                   │
                                   ▼
┌───────────────────────────────────────────────────────────────┐
│               Gateway Scheduler (every 60s)                   │
│                                                               │
│   1. Load jobs from jobs.json                                 │
│   2. Check next_run_at vs current time                        │
│   3. For each due job:                                        │
│      a. Start a fresh AIAgent session                         │
│      b. Optionally inject attached skill(s)                   │
│      c. Run the prompt to completion                          │
│      d. Deliver response to target                            │
│      e. Update run metadata + next scheduled time             │
│                                                               │
│  Delivery targets:                                            │
│  • "origin"          → back to where job was created          │
│  • "local"           → save to local files                    │
│  • "telegram"        → Telegram home channel                  │
│  • "discord"         → Discord home channel                   │
│  • "telegram:12345"  → specific Telegram chat                 │
│  • "discord:67890"   → specific Discord channel               │
│                                                               │
│  Safety:                                                      │
│  • Cron sessions CANNOT create more cron jobs                 │
│  • Prompts scanned for injection / exfiltration               │
└───────────────────────────────────────────────────────────────┘
```

---

## 13  Sub-Agent Delegation

Hermes can spawn **isolated sub-agents** for parallel workstreams.

```
┌───────────────────────────────────────────────────────────────┐
│                    Parent Agent                               │
│                                                               │
│  run_conversation() decides to delegate                       │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  sessions_send(target_session, message, reply_back=True)│  │
│  │  → sends a message to another session                   │  │
│  │  → optional reply-back for ping-pong coordination       │  │
│  │  → REPLY_SKIP / ANNOUNCE_SKIP for flow control          │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  Sub-agent spawning                                     │  │
│  │  • Each sub-agent is an isolated AIAgent session        │  │
│  │  • Shared iteration budget across parent + children     │  │
│  │  • Can write Python scripts that call tools via RPC     │  │
│  │  • Zero context cost for delegated work                 │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ Sub-agent A  │  │ Sub-agent B  │  │ Sub-agent C  │         │
│  │ (coding)     │  │ (research)   │  │ (testing)    │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│                                                               │
│  Coordination tools:                                          │
│  • sessions_list    → discover active sessions                │
│  • sessions_history → fetch transcript of another session     │
│  • sessions_send    → message another session                 │
└───────────────────────────────────────────────────────────────┘
```

### 13.1  How Sub-Agent Delegation Would Work in NemoClaw

In Hermes, sub-agents are isolated `AIAgent` sessions within a **single
process**. In NemoClaw Escapades, the equivalent is **separate OpenShell
sandboxes** — each sub-agent runs in its own policy-controlled container with
independent filesystem, network, credentials, and inference routing. The
orchestrator (an always-on sandbox) spawns ephemeral sandboxes for each task
via the `openshell` CLI.

This is architecturally more powerful than Hermes's in-process model: each
sub-agent gets kernel-level isolation (Landlock + seccomp + network
namespaces), and the orchestrator doesn't share a context window or iteration
budget with its children.

```
┌──────────────────────────────────────────────────────────────────────────┐
│           NemoClaw Sub-Agent Delegation (via OpenShell)                  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  ORCHESTRATOR SANDBOX  (always-on, broad policy)                   │  │
│  │  openshell sandbox create --from nemoclaw-orchestrator             │  │
│  │                                                                    │  │
│  │  Agent loop receives task from Slack                               │  │
│  │         │                                                          │  │
│  │         ├── Skill recall: is there a skill for this task?          │  │
│  │         │                                                          │  │
│  │         ├── Decision: delegate to sub-agent?                       │  │
│  │         │   (complex task, requires isolation, or parallelizable)  │  │
│  │         │                                                          │  │
│  │         ▼                                                          │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  1. CREATE sandbox for the sub-agent                         │  │  │
│  │  │     $ openshell sandbox create                               │  │  │
│  │  │         --policy coding-policy.yaml                          │  │  │
│  │  │         -- claude                                            │  │  │
│  │  │                                                              │  │  │
│  │  │  2. UPLOAD workspace / context                               │  │  │
│  │  │     $ openshell sandbox upload <name> ./project /sandbox/src │  │  │
│  │  │                                                              │  │  │
│  │  │  3. SEND task (via SSH exec)                                 │  │  │
│  │  │     $ openshell sandbox connect <name>                       │  │  │
│  │  │       "claude 'Implement feature X per the spec in /sandbox'"│  │  │
│  │  │                                                              │  │  │
│  │  │  4. POLL / WAIT for completion                               │  │  │
│  │  │     $ openshell logs <name> --tail                           │  │  │
│  │  │     (or poll via sandbox status)                             │  │  │
│  │  │                                                              │  │  │
│  │  │  5. DOWNLOAD results                                         │  │  │
│  │  │     $ openshell sandbox download <name> /sandbox/src ./output│  │  │
│  │  │                                                              │  │  │
│  │  │  6. CLEANUP                                                  │  │  │
│  │  │     $ openshell sandbox delete <name>                        │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │         │                                                          │  │
│  │         ▼                                                          │  │
│  │  Process results, update memory, optionally create/update skill    │  │
│  │  Announce result back to Slack                                     │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  Parallel sub-agents (the orchestrator can spawn multiple):              │
│                                                                          │
│  ┌────────────────────┐  ┌───────────────────┐  ┌──────────────────────┐ │
│  │  CODING SANDBOX    │  │  REVIEW SANDBOX   │  │  RESEARCH SANDBOX    │ │
│  │  (ephemeral)       │  │  (ephemeral)      │  │  (ephemeral)         │ │
│  │                    │  │                   │  │                      │ │
│  │  Agent: Claude     │  │  Agent: custom    │  │  Agent: custom       │ │
│  │  Policy: GitHub    │  │  Policy: read-only│  │  Policy: web +       │ │
│  │    push + Anthropic│  │   FS, inference   │  │   SecondBrain API    │ │
│  │  Lifecycle: create │  │  only             │  │  Lifecycle: create   │ │
│  │   → task → destroy │  │  Lifecycle: create│  │   → task → destroy   │ │
│  │                    │  │   → task → destroy│  │                      │ │
│  │  Input: prompt +   │  │  Input: diff from │  │  Input: research     │ │
│  │   source code      │  │   coding sandbox  │  │   query              │ │
│  │  Output: PR/patch  │  │  Output: review   │  │  Output: summary     │ │
│  │                    │  │   comments        │  │   + citations        │ │
│  └────────────────────┘  └───────────────────┘  └──────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### 13.2  Coordination Model: Hermes vs NemoClaw

In Hermes, sub-agents coordinate via `sessions_send` (in-process message
passing). In NemoClaw, the primary coordination mechanism is the **NemoClaw
Message Bus (NMB)** — a lightweight WebSocket-based broker that provides
real-time inter-sandbox messaging while preserving kernel-level isolation.
The NMB works transparently across host boundaries: sandboxes on the same
machine, on a remote DGX Spark, or on a Brev cloud instance all call the same
`messages.local` endpoint — the OpenShell proxy routes each connection to the
central broker over Tailscale, SSH tunnel, or TLS. File-based coordination via
`openshell sandbox upload/download` is retained as a fallback for
broker-unavailable scenarios and for bulk transfers exceeding 10 MB.

See [NMB Design Document](../nmb_design.md) for the full specification, including
[multi-host topology](../nmb_design.md#32--multi-host-sandboxes-distributed-across-machines)
and [remote transport options](../nmb_design.md#33--how-remote-sandboxes-reach-the-broker).

```
┌───────────────────────────────────────────────────────────────────────┐
│                   Coordination Comparison (3 Models)                  │
│                                                                       │
│  HERMES              NMB (primary)          FILE-BASED (fallback)     │
│  (in-process)        (cross-sandbox IPC)    (cross-sandbox bulk)      │
│                                                                       │
│  sessions_send(      bus.send(              openshell sandbox         │
│    target, msg,        "coding-sb-1",         upload <name>           │
│    reply_back=True)    "task.assign",         ./ctx /sandbox/in       │
│                        payload)                                       │
│       │                    │                      │                   │
│       ▼                    ▼                      ▼                   │
│  Sub-agent gets msg  Broker routes via      Sub-agent reads           │
│  in conversation     WebSocket; sub-agent   /sandbox/input            │
│  context (instant)   receives in <50ms      (2-5s round-trip)         │
│       │                    │                      │                   │
│       ▼                    ▼                      ▼                   │
│  Sub-agent replies   Sub-agent calls        Sub-agent writes to       │
│  (same process)      bus.reply() or         /sandbox/output           │
│                      bus.send() back                                  │
│       │                    │                      │                   │
│       ▼                    ▼                      ▼                   │
│  Parent gets reply   Orchestrator receives  Orchestrator downloads    │
│  via announce        delivery in <50ms      results (2-5s)            │
│                                                                       │
│  Latency: <1ms      Latency: ~20-50ms      Latency: ~2-5s             │
│                      (same host) or                                   │
│                      ~25-150ms (cross-host                            │
│                      via Tailscale/SSH/TLS)                           │
│                                                                       │
│  Multi-host: No      Multi-host: Yes        Multi-host: Yes           │
│  (single process)    (central broker via     (via SSH to each         │
│                       Tailscale, SSH tunnel,  sandbox host)           │
│                       or TLS; transparent                             │
│                       to client — still                               │
│                       calls messages.local)                           │
│                                                                       │
│  Isolation: None     Isolation: Full         Isolation: Full          │
│  (shared process)    (kernel-level,          (kernel-level,           │
│                       proxy-enforced          separate containers)    │
│                       identity)                                       │
│                                                                       │
│  Streaming: Yes      Streaming: Yes          Streaming: No            │
│  (callbacks)         (pub/sub channels)                               │
│                                                                       │
│  Interrupt: Yes      Interrupt: Yes          Interrupt: No            │
│  (cancel + resend)   (task.redirect msg)                              │
│                                                                       │
│  Audit trail: No     Audit trail: Yes        Audit trail: No          │
│                      (all msgs logged)                                │
└───────────────────────────────────────────────────────────────────────┘
```

The NMB follows the same `inference.local` pattern already used by OpenShell
for inference routing: sandboxes call `messages.local:9876`, the proxy
intercepts the connection, injects the sandbox identity via `X-Sandbox-ID`,
and routes to the broker on the host. The agent cannot forge its identity or
reach the broker outside the policy-allowed path.

### 13.3  Coding + Review Agent Collaboration (Pre-Push Loop)

This is the pattern for Design Doc Q12 ("Can the review agent and coding
agent collaborate locally without Git in the loop?"). The answer is **yes**
— the orchestrator mediates a real-time review loop between two sandboxes
using the NMB, avoiding any Git round-trip.

```
┌──────────────────────────────────────────────────────────────────────────┐
│      Pre-Push Coding + Review Loop (NemoClaw, via NMB)                   │
│                                                                          │
│  Orchestrator          NMB           Coding SB       Review SB           │
│       │                 │                │                │              │
│  1. Receive task from Slack                                              │
│  2. Create sandboxes (openshell sandbox create)                          │
│       │                 │                │                │              │
│  3.   │  task.assign    │                │                │              │
│       ├────────────────▶├───────────────▶│                │              │
│       │                 │                │ (coding...)    │              │
│       │                 │  progress 25%  │                │              │
│       │◀────────────────┤◀───────────────┤                │              │
│       │                 │  progress 75%  │                │              │
│       │◀────────────────┤◀───────────────┤                │              │
│       │                 │  task.complete  │                │             │
│       │◀────────────────┤◀───(diff)──────┤                │              │
│       │                 │                │                │              │
│  4.   │  review.request │                │                │              │
│       ├────────────────▶├──────────────────────────(diff)▶│              │
│       │                 │                │                │ (reviewing)  │
│       │                 │               review.feedback   │              │
│       │◀────────────────┤◀──────────────────────────(comments)           │
│       │                 │                │                │              │
│  5.   │ Changes needed? │                │                │              │
│       │                 │                │                │              │
│  YES: │  task.assign    │                │                │              │
│       │  (w/ feedback)  │                │                │              │
│       ├────────────────▶├───────────────▶│                │              │
│       │                 │  task.complete  │                │             │
│       │◀────────────────┤◀───(new diff)──┤                │              │
│       │                 │                │                │              │
│       │  review.request │                │                │              │
│       ├────────────────▶├──────────────────────────(diff)▶│              │
│       │                 │               review.lgtm       │              │
│       │◀────────────────┤◀──────────────────────────(ok)──┤              │
│       │                 │                │                │              │
│  6. git push (orchestrator has Git policy)                               │
│  7. Destroy both sandboxes                                               │
│  8. Report result to Slack                                               │
│  9. Save skill if workflow was novel (self-learning loop)                │
│                                                                          │
│  Overhead per review iteration:                                          │
│    NMB (same host):   ~200ms  (4x WebSocket through loopback proxy)      │
│    NMB (cross-host):  ~200ms-600ms (4x WebSocket via Tailscale/SSH)      │
│    File-based:        ~10-20s (4x SSH upload/download round-trips)       │
│    Improvement: 15-100x faster (even across hosts)                       │
│                                                                          │
│  Policy isolation (unchanged):                                           │
│  • Coding sandbox: inference + messages.local only                       │
│  • Review sandbox: inference + messages.local only                       │
│  • Orchestrator: Slack, Git, inference, messages.local, SecondBrain      │
│  • Neither sub-agent has credentials for Git or Slack                    │
│  • Sandbox identities are proxy-enforced (agents can't impersonate)      │
└──────────────────────────────────────────────────────────────────────────┘
```

**Fallback:** If the NMB broker is unavailable, the orchestrator falls back to
file-based coordination (`openshell sandbox upload/download`). The same logical
flow applies, just at higher latency. See
[NMB Design §12](../nmb_design.md#12--failure-modes--recovery) for graceful
degradation details.

### 13.4  Implementation Considerations

| Concern | Hermes Approach | NemoClaw Approach (with NMB) |
|---------|----------------|-------------------------------|
| **Spawning latency** | Instant (in-process) | ~10-30s (container creation). Mitigate with pre-warmed sandbox pools or `sandbox.scope: "agent"` (reuse container). |
| **Communication** | `sessions_send` / `sessions_history` (in-memory, <1ms) | NMB `bus.send()` / `bus.request()` (WebSocket via proxy, ~20-50ms same-host, ~25-150ms cross-host). Fallback: `openshell sandbox upload/download` (file-based, ~2-5s). |
| **Cross-host** | Not supported (single process) | Transparent — sandboxes on different hosts all call `messages.local`; the proxy routes to the central broker via Tailscale, SSH tunnel, or TLS. Client API is identical. |
| **Streaming** | Callback system (`tool_progress_callback`, etc.) | NMB pub/sub channels (`bus.subscribe("progress.coding-1")`). Sub-agent publishes `task.progress` messages in real-time. |
| **Interruption** | Cancel + resend in same process | NMB `task.redirect` message. Sub-agent receives interrupt and restarts with new instructions. |
| **Result collection** | Announce callback (parent receives sub-agent's final text) | NMB `task.complete` message with inline result. For large payloads (>10 MB): file transfer + NMB signaling. |
| **Budget control** | Shared iteration budget across parent + children | Independent budgets per sandbox. Orchestrator tracks total cost across sub-agents via inference routing logs. |
| **Error handling** | Sub-agent exceptions caught by parent loop | NMB `task.error` message + broker `sandbox.shutdown` event on crash. Orchestrator can auto-retry or escalate. |
| **Parallelism** | Concurrent tool execution within shared context | True parallelism — each sandbox is an independent process. Orchestrator subscribes to all sub-agent progress channels concurrently. |
| **Git worktree** | Hermes supports git-worktree for parallel coding | Each sandbox gets its own filesystem — no worktrees needed. Each coding sandbox works on the same codebase independently. Orchestrator merges results. |
| **Audit** | No built-in audit trail | NMB broker logs every message (from, to, type, timestamp, payload size) to an append-only audit file. |

For the full NMB specification — wire protocol, client library API, security
model, failure modes, and deployment — see the
[NMB Design Document](../nmb_design.md).

---

## 14  The Self-Learning Loop

This is the defining feature of Hermes and the primary reason it's a reference
for NemoClaw Escapades. The learning loop is **not a single subsystem** — it
emerges from the interaction of several components.

```
┌──────────────────────────────────────────────────────────────────────┐
│                    THE SELF-LEARNING LOOP                            │
│                                                                      │
│  ┌──────────────┐                                                    │
│  │  1. TASK     │  User sends a task or cron triggers one            │
│  │     INTAKE   │                                                    │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  2. SKILL    │  Agent checks skills_list(). If a relevant         │
│  │     RECALL   │  skill exists, it loads it via skill_view().       │
│  │              │  If not, it proceeds from scratch.                 │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  3. EXECUTION│  Agent executes the task using tools.              │
│  │     (tools,  │  May hit errors, dead ends, or need to             │
│  │     terminal,│  improvise. Tracks what worked and what didn't.    │
│  │     browser) │                                                    │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  4. MEMORY   │  Agent proactively saves key facts:                │
│  │     PERSIST  │  • Environment discoveries → MEMORY.md             │
│  │              │  • User preferences → USER.md                      │
│  │              │  • Deeper patterns → honcho_conclude               │
│  │              │  Memory nudges remind agent to persist.            │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  5. SKILL    │  After complex tasks (5+ tool calls):              │
│  │     CREATION │  • Was it successful? → skill_manage(create)       │
│  │     / UPDATE │  • Did existing skill fail? → skill_manage(patch)  │
│  │              │  • User corrected approach? → update the skill     │
│  │              │                                                    │
│  │              │  The skill captures the WORKING path, not the      │
│  │              │  failed attempts.                                  │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  6. SESSION  │  All conversation history → SQLite with FTS5.      │
│  │     ARCHIVE  │  Searchable via session_search tool.               │
│  │              │  Future tasks can recall past approaches.          │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         │  Next time a similar task arrives...                       │
│         │                                                            │
│         └─────────────────────────────►  Back to Step 2              │
│                                          (skill is now available)    │
└──────────────────────────────────────────────────────────────────────┘
```

### What Makes the Loop Work

| Component | Role in the Loop |
|-----------|-----------------|
| **Skills system** | Procedural memory — saves successful workflows for reuse |
| **MEMORY.md / USER.md** | Semantic memory — stores facts about the environment and user |
| **Honcho** | Long-term user modeling — learns communication patterns across sessions |
| **Session search (FTS5)** | Episodic memory — recalls specific past conversations on demand |
| **Cron** | Autonomous trigger — the agent doesn't wait for the user to learn |
| **Memory nudges** | Prompt engineering — the system prompt reminds the agent to persist knowledge |

### Learning Loop Contrasted: Hermes vs Traditional Agents

| Aspect | Traditional Agent | Hermes |
|--------|-------------------|--------|
| Memory | Conversation context only | Persistent across sessions (3 layers) |
| Skills | Static tool set | Self-creating, self-improving |
| Triggers | User-initiated only | User + cron + background |
| Self-reflection | None | Evaluates outcomes, updates skills |
| User model | Stateless | Deepening model across sessions (Honcho) |

---

## 15  RL / Environments / Training

Hermes ships a full **environment framework** for evaluation, RL integration,
and SFT data generation. This is research-oriented — useful for training the
next generation of tool-calling models.

```
┌───────────────────────────────────────────────────────────────┐
│  environments/        Evaluation & RL environments            │
│  tinker-atropos/      Atropos RL integration (submodule)      │
│  batch_runner.py      Batch trajectory generation             │
│  trajectory_          Compress trajectories for training      │
│    compressor.py                                              │
│  toolset_             Toolset sampling for data generation    │
│    distributions.py                                           │
│                                                               │
│  Pipeline:                                                    │
│    environments → batch_runner → trajectories → compressor    │
│    → training data (SFT / RL)                                 │
└───────────────────────────────────────────────────────────────┘
```

---

## 16  ACP Editor Integration

ACP (Agent Communication Protocol) exposes Hermes as an **editor-native agent**
over stdio/JSON-RPC. This lets IDEs like Cursor or VS Code communicate with
Hermes directly.

| Component | File |
|-----------|------|
| ACP server | `acp_adapter/` |
| ACP manifest | `acp_registry/` |
| Internals doc | [ACP Internals](https://hermes-agent.nousresearch.com/docs/developer-guide/acp-internals) |

---

## 17  Setup & Installation

### Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc
hermes              # start chatting
hermes model        # choose provider/model
hermes setup        # full setup wizard
hermes doctor       # diagnose issues
```

### Key Configuration

```yaml
# ~/.hermes/config.yaml
model: "openrouter:anthropic/claude-opus-4-6"

terminal:
  backend: local          # local|docker|ssh|daytona|singularity|modal

memory:
  memory_enabled: true
  user_profile_enabled: true

skills:
  external_dirs:
    - ~/.agents/skills

# Provider config (or use env vars)
providers:
  openrouter:
    api_key: ...
  custom:
    base_url: https://your-inference-hub/v1   # ← inference hub goes here
    api_key: ...
```

### Migration from OpenClaw

```bash
hermes claw migrate              # interactive migration
hermes claw migrate --dry-run    # preview only
```

Imports: SOUL.md, memories, skills, command allowlist, messaging settings, API
keys, workspace instructions.

---

## 18  Answers to Design Doc Questions

### Q1: What is the exact Hermes agent architecture? Can we duplicate it or lift applicable parts?

**Answered above in [§2](#2--high-level-architecture) and [§4](#4--the-agent-loop).**

The architecture is modular and the codebase is MIT-licensed. The components
most worth lifting for NemoClaw Escapades:

| Component | Liftability | Notes |
|-----------|-------------|-------|
| Skills system (SKILL.md format) | High | File-based, standard format ([agentskills.io](https://agentskills.io)), easily replicated |
| Memory system (MEMORY.md/USER.md) | High | Simple file-based, bounded, well-designed |
| Provider resolver pattern | High | Pluggable backends, supports any OpenAI-compatible endpoint |
| Self-learning loop logic | Medium | Emergent from skills + memory + nudges; need prompt engineering |
| Agent loop (AIAgent) | Medium | Tightly coupled to Hermes internals, but the pattern is replicable |
| Gateway platform adapters | Low | Heavily platform-specific; better to build our own Slack adapter |

### Q2: What is the block diagram for Hermes?

**Answered in [§2](#2--high-level-architecture)** — full block diagram with all
major subsystems, entry points, the agent loop, persistence layer, and
external integrations.

### Q3: Is Hermes compatible with inference hub, or can it be made compatible?

**Yes, with minimal effort.** Hermes's provider system supports any
OpenAI-compatible endpoint via the `base_url` configuration. To point Hermes
at an NVIDIA Inference Hub endpoint:

```yaml
# ~/.hermes/config.yaml
providers:
  custom:
    base_url: https://your-inference-hub.nvidia.com/v1
    api_key: your-api-key
model: "custom:model-name"
```

This works because:
1. Inference Hub exposes an OpenAI-compatible chat completions API.
2. Hermes's `chat_completions` API mode is the default for any
   OpenAI-compatible endpoint.
3. No code changes required — just configuration.

**Caveat:** If the Inference Hub endpoint has non-standard behavior (e.g.,
custom authentication headers, non-standard tool call format), a thin adapter
may be needed. But for standard OpenAI-compatible endpoints, it's
plug-and-play.

### Q4: How can we port Hermes to NemoClaw while preserving the self-learning loop?

The self-learning loop is **not a single module** — it's an emergent behavior
from the interaction of skills, memory, session search, and prompt engineering
(see [§14](#14--the-self-learning-loop)). To port it:

1. **Replicate the skills system.** Adopt the SKILL.md format
   ([agentskills.io standard](https://agentskills.io)). Implement
   `skills_list`, `skill_view`, and `skill_manage` as tools available to the
   NemoClaw orchestrator.

2. **Replicate the memory system.** Implement bounded MEMORY.md / USER.md with
   the same add/replace/remove semantics. Inject as a frozen snapshot in the
   system prompt.

3. **Add session search.** Store all conversations in a searchable store (SQLite
   FTS5 or equivalent). Expose as a `session_search` tool.

4. **Prompt-engineer the nudges.** Hermes's system prompt includes nudges that
   remind the agent to:
   - Save important discoveries to memory
   - Create skills after complex tasks
   - Search past sessions when relevant
   - Consolidate memory when capacity is low

5. **Optionally integrate Honcho.** Honcho is cloud-hosted or self-hostable via
   Docker. It can be used independently of Hermes — just point it at the same
   user/workspace config.

6. **Wire it to NemoClaw policies.** The NemoClaw orchestrator should trigger
   the learning steps as part of its post-task cleanup, similar to how Hermes
   does it within the agent loop.

### Q5: How does Honcho combine with SecondBrain? Should we use one or both?

**Use both — they serve different purposes.**

```
┌───────────────────────────────────────────────────────────────┐
│                   Memory Architecture (Proposed)              │
│                                                               │
│  ┌──────────────────────────────┐                             │
│  │  Honcho                      │                             │
│  │  Purpose: User modeling      │                             │
│  │  • Who is the user?          │                             │
│  │  • Preferences, goals, style │                             │
│  │  • Cross-session continuity  │                             │
│  │  • Auto-learned from convos  │                             │
│  │                              │                             │
│  │  Scope: Agent ↔ User         │                             │
│  │  relationship                │                             │
│  └──────────────────────────────┘                             │
│                                                               │
│  ┌──────────────────────────────┐                             │
│  │  SecondBrain                 │                             │
│  │  Purpose: Knowledge base     │                             │
│  │  • Domain knowledge          │                             │
│  │  • Paper summaries           │                             │
│  │  • Project documentation     │                             │
│  │  • Code analyses             │                             │
│  │  • Structured learning       │                             │
│  │                              │                             │
│  │  Scope: World knowledge      │                             │
│  │  (personal + professional)   │                             │
│  └──────────────────────────────┘                             │
│                                                               │
│  ┌──────────────────────────────┐                             │
│  │  Built-in Memory             │                             │
│  │  (MEMORY.md / USER.md)       │                             │
│  │  Purpose: Working memory     │                             │
│  │  • Current session context   │                             │
│  │  • Environment facts         │                             │
│  │  • Active project state      │                             │
│  │                              │                             │
│  │  Scope: Immediate agent      │                             │
│  │  context (~1,300 tokens)     │                             │
│  └──────────────────────────────┘                             │
│                                                               │
│  Hierarchy:                                                   │
│    Working memory (always in prompt, ~1,300 tokens)           │
│      ↕ syncs key facts from Honcho and SecondBrain            │
│    Honcho (deep user model, queried on demand)                │
│    SecondBrain (domain knowledge, queried on demand)          │
│                                                               │
│  Separation:                                                  │
│    SecondBrain (personal KB) = academic, learning, personal   │
│    Professional KB           = work Slack, Teams, Jira        │
│    Honcho                    = user identity + agent identity │
└───────────────────────────────────────────────────────────────┘
```

**Recommendation:** Honcho handles the *who* (user modeling). SecondBrain
handles the *what* (domain knowledge). Built-in memory handles the *now*
(working context). The three layers complement each other.

---

## 19  What to Lift for NemoClaw Escapades

Based on this deep dive, here are the Hermes patterns and components most
relevant to each NemoClaw milestone:

### Milestone 1 — Foundation

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Provider resolver (`base_url` config) | Point at Inference Hub as a custom OpenAI-compatible endpoint |
| Gateway platform adapters | Build a Slack adapter following the same pattern (but simpler — single platform) |
| `AIAgent` loop pattern | Implement a similar orchestrator loop: prompt → LLM → tool calls → loop |

### Milestone 2 — Knowledge Management

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Toolset system | Expose SecondBrain CLI as a tool available to the orchestrator |
| Memory nudges | Prompt-engineer the agent to save important findings to SecondBrain |

### Milestone 3 — Coding Agent

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Terminal backends (docker, modal, daytona) | Model OpenShell integration after these — same pattern, different runtime |
| Sub-agent delegation (`sessions_send`) | Orchestrator delegates coding tasks to sub-agent sessions |
| Toolset isolation | Give coding agent a restricted toolset (terminal, files, git) |

### Milestone 4 — Self-Improvement Loop

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Skills system (SKILL.md, `skill_manage`) | Replicate the format and CRUD tools |
| Memory system (MEMORY.md, USER.md, `memory` tool) | Replicate bounded memory with add/replace/remove |
| Session search (SQLite FTS5) | Store all conversations searchably |
| Honcho integration | Use for cross-session user modeling |
| Cron scheduler | Adapt for background autonomous tasks |
| Self-learning loop (§14) | Replicate the 6-step loop with prompt nudges |

### Milestone 5 — Review Agent

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Sub-agent coordination (`sessions_*` tools) | Coding agent ↔ review agent communicate via session tools |
| Callback system (tool_progress, clarify) | Review agent provides feedback to coding agent within the same loop |

### Milestone 6 — Professional KB

| Hermes Pattern | How to Apply |
|----------------|-------------|
| Cron + skill-backed jobs | Schedule periodic Slack/Teams scraping as cron jobs with attached skills |
| Session search for recall | Query past scraping results across sessions |
