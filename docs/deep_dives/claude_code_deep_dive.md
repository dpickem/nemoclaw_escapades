# Claude Code — Deep Dive

> **Source:** Leaked TypeScript source (v2.1.88) via `.map` file in npm registry;
> actual source snapshot at [zackautocracy/claude-code](https://github.com/zackautocracy/claude-code);
> clean-room reimplementation at [instructkr/claw-code](https://github.com/instructkr/claw-code)
> (107k stars, Python + Rust ports);
> static analysis at [thtskaran/claude-code-analysis](https://github.com/thtskaran/claude-code-analysis)
> (1,884 files, 6,552 exports, 99 classes, 1,308 types documented)
>
> **Background:** [Gizmodo — Source Code for Anthropic's Claude Code Leaks](https://gizmodo.com/source-code-for-anthropics-claude-code-leaks-at-the-exact-wrong-time-2000740379)
> (March 31, 2026)
>
> **Last reviewed:** 2026-04-01

---

## Table of Contents

1. [Overview](#1--overview)
2. [How the Leak Happened](#2--how-the-leak-happened)
3. [High-Level Architecture](#3--high-level-architecture)
4. [Original TypeScript Codebase Structure](#4--original-typescript-codebase-structure)
5. [Feature Flags & Hidden Features](#5--feature-flags--hidden-features)
6. [Rust Port Structure (claw-code)](#6--rust-port-structure-claw-code)
7. [The Agent Loop — Async Generator Architecture](#7--the-agent-loop--async-generator-architecture)
8. [Tool System](#8--tool-system)
9. [Slash Commands](#9--slash-commands)
10. [Plugin System](#10--plugin-system)
11. [Permission Model & YOLO Classifier](#11--permission-model--yolo-classifier)
12. [Session Management & Three-Tier Compaction](#12--session-management--three-tier-compaction)
13. [Security Architecture](#13--security-architecture)
14. [MCP Integration](#14--mcp-integration)
15. [Configuration & Memory Files](#15--configuration--memory-files)
16. [OAuth & Authentication](#16--oauth--authentication)
17. [CLI & REPL](#17--cli--repl)
18. [System Prompt Construction](#18--system-prompt-construction)
19. [Sub-Agents, Skills & Coordinator Mode](#19--sub-agents-skills--coordinator-mode)
20. [Daemon Mode & Proactive Agent](#20--daemon-mode--proactive-agent)
21. [Bundled Skills Catalog](#21--bundled-skills-catalog)
22. [Bootstrap Sequence](#22--bootstrap-sequence)
23. [Usage Tracking & Cost](#23--usage-tracking--cost)
24. [Model Behavioral Contract](#24--model-behavioral-contract)
25. [Notable Curiosities from the Leak](#25--notable-curiosities-from-the-leak)
26. [Comparison with Hermes & OpenClaw](#26--comparison-with-hermes--openclaw)
27. [What to Lift for NemoClaw Escapades](#27--what-to-lift-for-nemoclaw-escapades)

---

## 1  Overview

Claude Code is Anthropic's **terminal-native AI coding assistant**. It runs as
an interactive REPL in the user's terminal, connects to Anthropic's API, and
operates on the local filesystem using a defined set of tools (bash, file
read/write/edit, grep, glob, web fetch, etc.). It is the closest commercial
analogue to what NemoClaw Escapades aims to build.

Key facts from the leaked source:
- **Written in TypeScript** — 1,884 source files; 6,552 exported
  functions/constants; 99 classes; 1,308 type definitions; 327 Zod
  validation schemas; 90 feature flags
- **38 distinct services** organized into utils, components, commands, tools,
  services, hooks, ink (UI framework), bridge, CLI, constants, skills, and more
- **Default model:** `claude-opus-4-6` (32k max output tokens); `claude-sonnet-4-6`
  and `claude-haiku-4-5` also supported via `--model` flag
- **Permission modes:** `read-only`, `workspace-write`, `danger-full-access`
  with a two-stage YOLO auto-classifier
- **Closed-source** — not licensed for redistribution; community created
  clean-room reimplementations
- **Core loop:** `async function* query()` — a streaming-first async generator
  architecture that allows tool execution *during* response generation

### Tech stack

| Category | Technology |
|----------|-----------|
| **Runtime** | [Bun](https://bun.sh) |
| **Language** | TypeScript (strict) |
| **Terminal UI** | [React](https://react.dev) + [Ink](https://github.com/vadimdemedes/ink) |
| **CLI parsing** | [Commander.js](https://github.com/tj/commander.js) (extra-typings) |
| **Schema validation** | [Zod v4](https://zod.dev) |
| **Code search** | [ripgrep](https://github.com/BurntSushi/ripgrep) |
| **Protocols** | [MCP SDK](https://modelcontextprotocol.io), LSP |
| **API** | [Anthropic SDK](https://docs.anthropic.com) |
| **Telemetry** | OpenTelemetry + gRPC |
| **Feature flags** | GrowthBook (remote) + `bun:bundle` `feature()` (build-time dead code elimination) |
| **Auth** | OAuth 2.0 + PKCE, JWT, macOS Keychain |

The choice of **Bun** over Node.js is notable — Bun's `bun:bundle`
`feature()` primitive enables build-time dead code elimination for feature
flags, meaning gated code is physically stripped from the binary rather
than checked at runtime. This is why ~15% of the codebase (`USER_TYPE === 'ant'`)
disappears entirely in external builds.

Ten critical architectural patterns identified by the analysis community:
1. Async generator agent loop (`async function* query()`)
2. Three-tier context compaction (micro ~256 tokens, full ~4K tokens, session memory)
3. Two-stage YOLO auto-permission classifier (64-token fast + 4K-token thinking)
4. NO_TOOLS sandwich pattern (safety instructions at start AND end of system prompt)
5. Prompt cache boundary marker (`__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`)
6. Copy-on-write speculation (pre-computed next responses on overlay filesystem)
7. Streaming tool execution (during response, not after)
8. 4,437-line Bash parser for command injection defense
9. Frustration detection (profanity regex → telemetry + feedback surveys)
10. Coordinator mode (multi-agent orchestration with parallel tool execution)

The leaked codebase does **not** reveal Anthropic's underlying model weights or
training procedures — it exposes the *harness* layer: how the agent wires tools,
manages sessions, constructs prompts, handles permissions, and integrates with
external services via MCP.

---

## 2  How the Leak Happened

On March 31, 2026, a `.map` file (source map) was discovered in the npm
registry for Claude Code v2.1.88. Source maps are plaintext files generated
during compilation that map minified/obfuscated code back to readable source.
They are intended for internal debugging and should never ship to production
registries.

The file was found by Chaofan Shou ([@Fried_rice](https://x.com/Fried_rice))
and contained the full unobfuscated TypeScript source for the Claude Code
agent harness.

Anthropic confirmed the authenticity of the leak to Gizmodo:

> "Earlier today, a Claude Code release included some internal source code. No
> sensitive customer data or credentials were involved or exposed. This was a
> release packaging issue caused by human error, not a security breach."

The timing was particularly unfortunate — Anthropic is reportedly preparing
for an IPO and competing directly with OpenAI's Codex on the enterprise
coding-assistant market.

Within hours of the leak:
- The community had parsed the full tool manifest, slash command inventory,
  and bootstrap sequence
- The `instructkr/claw-code` repo appeared with a clean-room Python rewrite,
  followed by a Rust port — it became the fastest GitHub repo to reach 50k
  stars (2 hours)
- "Spinner verbs," system prompt fragments, and the permission model were
  publicly documented

---

## 3  High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            ENTRY POINTS                                      │
│                                                                              │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│  │  REPL    │  │  One-shot    │  │  --resume   │  │  Editor Integration  │   │
│  │  (claw)  │  │  -p "prompt" │  │  SESSION    │  │  (compat-harness)    │   │
│  └────┬─────┘  └──────┬───────┘  └──────┬──────┘  └──────────┬───────────┘   │
│       │               │                │                     │              │
│       └───────────────┴────────┬───────┴─────────────────────┘              │
│                                │                                            │
│                                ▼                                            │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │                ConversationRuntime  (runtime crate)                │      │
│  │                                                                    │      │
│  │  ┌────────────────┐  ┌──────────────┐  ┌───────────────────────┐   │      │
│  │  │ System Prompt   │  │   API Client │  │   Tool Executor       │   │      │
│  │  │ Builder         │  │              │  │   (StreamingTool      │   │      │
│  │  │                 │  │ • Anthropic  │  │    Executor)          │   │      │
│  │  │ • project ctx   │  │   Messages   │  │                       │   │      │
│  │  │ • memory files  │  │ • streaming  │  │ • 40+ built-in tools  │   │      │
│  │  │ • CLAW.md       │  │ • OAuth auth │  │ • MCP tools           │   │      │
│  │  │ • date/OS ctx   │  │ • retries    │  │ • plugin tools        │   │      │
│  │  └────────────────┘  └──────────────┘  └───────────────────────┘   │      │
│  │                                                                    │      │
│  │  ┌────────────────┐  ┌──────────────┐  ┌───────────────────────┐   │      │
│  │  │ Session         │  │   Compaction │  │   Permission          │   │      │
│  │  │ Persistence     │  │   Engine     │  │   Prompter            │   │      │
│  │  │                 │  │              │  │                       │   │      │
│  │  │ • save/load     │  │ • token est  │  │ • read-only           │   │      │
│  │  │ • JSON files    │  │ • threshold  │  │ • workspace-write     │   │      │
│  │  │ • export        │  │ • summary    │  │ • danger-full-access  │   │      │
│  │  └────────────────┘  └──────────────┘  └───────────────────────┘   │      │
│  └────────────────────────────────────────────────────────────────────┘      │
│                                │                                            │
│                ┌───────────────┼────────────────────┐                       │
│                ▼               ▼                    ▼                       │
│  ┌───────────────────┐  ┌──────────────┐  ┌───────────────────────┐         │
│  │  Plugin Manager   │  │   MCP Client │  │   Config Loader       │         │
│  │  (plugins crate)  │  │   (runtime)  │  │   (runtime)           │         │
│  │                   │  │              │  │                       │         │
│  │ • builtin         │  │ • stdio      │  │ • CLAW.md             │         │
│  │ • bundled         │  │ • sdk        │  │ • .claw/settings.json │         │
│  │ • external        │  │ • managed    │  │ • env vars            │         │
│  │ • hooks pipeline  │  │   proxy      │  │ • schema validation   │         │
│  │ • lifecycle       │  │ • websocket  │  │                       │         │
│  └───────────────────┘  └──────────────┘  └───────────────────────┘         │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │                     Commands Layer  (commands crate)               │      │
│  │                                                                    │      │
│  │  /help  /status  /compact  /model  /permissions  /clear  /cost     │      │
│  │  /resume  /config  /memory  /init  /diff  /version  /bughunter    │      │
│  │  /branch  /worktree  /commit  /commit-push-pr  /pr  /issue        │      │
│  │  /ultraplan  /teleport  /export  /session  /plugin  /agents       │      │
│  │  /skills  /debug-tool-call                                        │      │
│  └────────────────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Key Design Themes

- **Streaming-first architecture** — the core `async function* query()`
  is an async generator that yields events as they arrive, enabling tools
  to execute *during* the model's streaming response, not after.
- **Terminal-first with daemon aspirations** — the primary interface is a
  CLI REPL, but the codebase includes daemon mode, background sessions,
  proactive agent ticking, and remote control infrastructure.
- **Tool execution is permission-gated** — every tool has a required
  permission level; a two-stage YOLO classifier can auto-approve in
  "auto mode" before falling back to interactive prompts.
- **Three-tier compaction** — micro-compaction (~256 tokens, no API call),
  full compaction (~4K tokens, API-evaluated), and session memory
  (zero-cost in-memory cache) manage context across different scales.
- **Plugins extend everything** — external plugins can add tools, commands,
  and hooks (pre/post-tool-use) without modifying core code.
- **MCP is the integration protocol** — external tool servers connect via
  stdio, SDK, managed proxy, or WebSocket transports.
- **90 feature flags** via GrowthBook control progressive rollout of
  capabilities (daemon mode, KAIROS assistant, remote triggers, browser
  automation, etc.).

---

## 4  Original TypeScript Codebase Structure

The original leaked source is a TypeScript codebase of 1,884 files. The
[thtskaran/claude-code-analysis](https://github.com/thtskaran/claude-code-analysis)
repo performed systematic AST analysis and documented the full module graph.

### File counts by top-level directory:

| Directory | Files | Role |
|-----------|-------|------|
| `utils/` | ~215 | Foundation layer: config, auth, bash parsing, permissions, git, formatting |
| `commands/` | ~195 | Slash command implementations |
| `components/` | ~193 | Ink (React-based terminal UI) components |
| `tools/` | ~155 | Tool implementations (Bash, File*, Glob, Grep, Agent, etc.) |
| `services/` | ~113 | API client, analytics, MCP, compaction, tool execution |
| `hooks/` | ~104 | React hooks for UI state and tool permissions |
| `ink/` | ~93 | Custom Ink rendering framework |
| `bridge/` | ~31 | Remote bridge infrastructure |
| `constants/` | ~21 | Prompts, thresholds, magic numbers |
| `skills/` | ~20 | Bundled skill definitions |
| `cli/` | ~19 | CLI transport (print.ts alone is 5,595 lines) |
| `state/` | ~6 | Global AppState + AppStateStore |

### Largest files (complexity proxies):

| File | Lines | Purpose |
|------|-------|---------|
| `cli/print.ts` | 5,595 | Non-interactive output rendering |
| `screens/REPL.tsx` | 5,006 | Main interactive UI component |
| `main.tsx` | 4,684 | Entry point, bootstrap, mode routing |
| `services/api/claude.ts` | 3,420 | Anthropic API client, streaming, caching |
| `bridge/bridgeMain.ts` | 3,000 | Remote control bridge |
| `tools/BashTool/bashPermissions.ts` | 2,622 | Bash permission classification |
| `utils/auth.ts` | 2,003 | Authentication and credential management |
| `utils/config.ts` | 1,818 | Configuration loading and merging |
| `query.ts` | 1,730 | Core agent query loop |
| `services/mcp/config.ts` | 1,579 | MCP server configuration |
| `tools/AgentTool/AgentTool.tsx` | 1,398 | Sub-agent orchestration |
| `QueryEngine.ts` | 1,296 | Query engine with compaction |
| `tools/BashTool/BashTool.tsx` | 1,144 | Bash tool execution |

### Hub files (most-imported modules):

The dependency graph reveals a clear backbone — the top 10 most-imported
modules are imported by 134-292 other files:

1. `ink.js` (292 importers) — UI framework re-export
2. `utils/config.js` (268) — configuration management
3. `commands.js` (~204) — command registry
4. `utils/debug.js` (192) — debug logging
5. `Tool.js` (184) — tool type definitions
6. `types/message.js` (181) — core message types
7. `utils/errors.js` (154) — error handling
8. `utils/log.js` (154) — logging
9. `state/AppState.js` (150) — global application state
10. `bootstrap/state.js` (134) — bootstrap state

Analytics is deeply embedded: `services/analytics/growthbook.js` is imported
by 128+ files, indicating pervasive telemetry throughout the codebase.

### Additional directories from the actual source snapshot

The [zackautocracy/claude-code](https://github.com/zackautocracy/claude-code)
repo contains the actual TypeScript source and reveals directories not
visible in the analysis or Rust port:

| Directory | Purpose |
|-----------|---------|
| `bridge/` | IDE integration bridge (VS Code, JetBrains) — bidirectional messaging, JWT auth, permission callbacks |
| `coordinator/` | Multi-agent coordinator mode |
| `voice/` | Voice input (feature-gated) |
| `vim/` | Vim mode for the REPL |
| `buddy/` | **Companion sprite** — the Tamagotchi-style virtual pet |
| `memdir/` | Persistent memory directory |
| `remote/` | Remote session management |
| `server/` | Direct-connect server mode |
| `upstreamproxy/` | Proxy configuration |
| `native-ts/` | Native TypeScript utilities |
| `outputStyles/` | Output styling |
| `query/` | Query pipeline internals |
| `migrations/` | Config migrations (model alias updates, settings migrations) |
| `schemas/` | Config schemas (Zod) |

---

## 5  Feature Flags & Hidden Features

This is one of the most revealing aspects of the leak. Claude Code uses
**Bun's `feature()` build-time flag** for dead code elimination — gated
features are physically stripped from the production binary. The source
snapshot reveals the *full* set of capabilities Anthropic is building,
most of which external users have never seen.

### Feature flag mechanism

```typescript
import { feature } from 'bun:bundle'

// Code inside this branch is entirely removed from external builds
const voiceCommand = feature('VOICE_MODE')
  ? require('./commands/voice/index.js').default
  : null
```

Flags fall into two categories:
1. **`feature('FLAG_NAME')`** — Bun build-time flags; code is stripped
   at compile time
2. **`process.env.USER_TYPE === 'ant'`** — Runtime flags for
   Anthropic-internal ("ant") builds

### Complete feature flag inventory

#### Active / Shipping features

| Flag | What it gates |
|------|---------------|
| — (always-on) | Core tools: Bash, FileRead/Edit/Write, Glob, Grep, WebFetch, WebSearch, Agent, Skill, Todo, Brief, AskUserQuestion, EnterPlanMode, NotebookEdit |

#### Proactive / Assistant mode (KAIROS family)

| Flag | What it gates |
|------|---------------|
| `PROACTIVE` | Proactive tick system — periodic `<proactive_tick>` prompts, `SleepTool` |
| `KAIROS` | Full assistant mode — `SleepTool`, `SendUserFileTool`, `PushNotificationTool`, `/assistant` command, `/brief` command, `/proactive` command, assistant module + gate |
| `KAIROS_BRIEF` | Brief mode command (standalone, without full KAIROS) |
| `KAIROS_PUSH_NOTIFICATION` | Push notification tool (standalone) |
| `KAIROS_GITHUB_WEBHOOKS` | `SubscribePRTool`, `/subscribe-pr` command — watch GitHub PRs for updates |
| `KAIROS_DREAM` | Background "dream" skill — deferred-work / background-memory processing |

#### Automation & triggers

| Flag | What it gates |
|------|---------------|
| `AGENT_TRIGGERS` | Local cron: `CronCreateTool`, `CronDeleteTool`, `CronListTool` — scheduled task execution |
| `AGENT_TRIGGERS_REMOTE` | `RemoteTriggerTool` — remote scheduled agents on Anthropic cloud |
| `MONITOR_TOOL` | `MonitorTool` — background monitoring capability |

#### Multi-agent & orchestration

| Flag | What it gates |
|------|---------------|
| `COORDINATOR_MODE` | Multi-agent coordinator — parallel sub-agent orchestration with result synthesis |
| `FORK_SUBAGENT` | `/fork` command — fork the current session into a sub-agent |
| `UDS_INBOX` | `ListPeersTool`, `/peers` command — Unix domain socket inter-agent messaging |

#### Code & IDE integration

| Flag | What it gates |
|------|---------------|
| `BRIDGE_MODE` | IDE bridge — VS Code / JetBrains integration (`/bridge` command) |
| `DAEMON` | Daemon mode — long-lived supervisor process (requires `BRIDGE_MODE` for `/remoteControlServer`) |
| `TERMINAL_PANEL` | `TerminalCaptureTool` — capture terminal content |
| `WEB_BROWSER_TOOL` | `WebBrowserTool` — programmatic browser automation |

#### Workflow & skill system

| Flag | What it gates |
|------|---------------|
| `WORKFLOW_SCRIPTS` | `WorkflowTool`, `/workflows` command — reusable workflow scripts |
| `EXPERIMENTAL_SKILL_SEARCH` | Skill search index with cache clearing |
| `ULTRAPLAN` | `/ultraplan` command — deep multi-step planning |
| `TORCH` | `/torch` command (purpose unclear — likely a specialized analysis/debugging workflow) |

#### History & context management

| Flag | What it gates |
|------|---------------|
| `HISTORY_SNIP` | `SnipTool`, `/force-snip` command — selective history editing |
| `CONTEXT_COLLAPSE` | `CtxInspectTool` — inspect and debug context window contents |
| `OVERFLOW_TEST_TOOL` | `OverflowTestTool` — test context overflow behavior |
| `TRANSCRIPT_CLASSIFIER` | Auto-mode state management for transcript-based permission classification |

#### Remote & cloud

| Flag | What it gates |
|------|---------------|
| `CCR_REMOTE_SETUP` | `/web` remote setup command |
| `REVIEW_ARTIFACT` | `hunter` skill — deep-review / artifact-review workflow |

#### Fun & experimental

| Flag | What it gates |
|------|---------------|
| `VOICE_MODE` | `/voice` command — voice input for the REPL |
| `BUDDY` | `/buddy` command — **the Tamagotchi companion sprite** (the `buddy/` directory) |
| `BUILDING_CLAUDE_APPS` | `claude-api` skill — built-in developer docs for the Anthropic SDK |
| `RUN_SKILL_GENERATOR` | Automated skill generation |

#### Anthropic-internal only (`USER_TYPE === 'ant'`)

| Feature | What it gates |
|---------|---------------|
| `ConfigTool` | Get/set Claude Code settings as a tool (not exposed to external users) |
| `TungstenTool` | Internal-only tool (purpose unknown — possibly internal testing/metrics) |
| `REPLTool` | REPL wrapper mode (wraps primitive tools in a VM) |
| `SuggestBackgroundPRTool` | Suggest background PR creation |
| `agentsPlatform` | `/agents-platform` command |
| Internal commands | `/backfill-sessions`, `/break-cache`, `/bughunter`, `/commit`, `/commit-push-pr`, `/ctx_viz`, `/good-claude`, `/issue`, `/init-verifiers`, `/mock-limits`, `/bridge-kick`, `/version`, `/ultraplan`, `/subscribe-pr`, `/reset-limits`, `/onboarding`, `/share`, `/summary`, `/teleport`, `/ant-trace`, `/perf-issue`, `/env`, `/oauth-refresh`, `/debug-tool-call`, `/autofix-pr` |

### Hidden commands not in the Rust port

The actual source reveals **80+ slash commands** — far more than the 25
documented in the Rust port:

| Command | Category | Description |
|---------|----------|-------------|
| `/advisor` | AI | AI advisor mode |
| `/agents-platform` | Internal | Agent platform management |
| `/autofix-pr` | Internal | Auto-fix PR issues |
| `/bridge` | IDE | IDE bridge control |
| `/buddy` | Fun | Tamagotchi companion |
| `/btw` | UX | Quick aside/note |
| `/chrome` | Browser | Chrome integration control |
| `/color` | UI | Color scheme |
| `/copy` | UX | Copy last response |
| `/desktop` | Handoff | Desktop app handoff |
| `/effort` | Config | Set reasoning effort level |
| `/env` | Internal | Environment variables |
| `/fast` | Config | Fast mode toggle |
| `/files` | Context | File context management |
| `/fork` | Agent | Fork session into sub-agent |
| `/force-snip` | History | Force snip conversation history |
| `/good-claude` | Internal | Positive feedback (Anthropic-internal) |
| `/heapdump` | Debug | Heap dump for memory debugging |
| `/hooks` | Config | Hook configuration |
| `/ide` | IDE | IDE integration |
| `/insights` | Analytics | Session analytics report (113KB lazy-loaded) |
| `/install-github-app` | Integration | Install GitHub App |
| `/install-slack-app` | Integration | Install Slack App |
| `/keybindings` | Config | Keyboard shortcuts |
| `/mobile` | Handoff | Mobile app handoff |
| `/output-style` | UI | Output style configuration |
| `/passes` | Billing | Usage passes |
| `/peers` | Agent | List peer agents (UDS inbox) |
| `/plan` | Workflow | Enter plan mode |
| `/privacy-settings` | Config | Privacy settings |
| `/proactive` | Assistant | Proactive mode control |
| `/rate-limit-options` | Config | Rate limit options |
| `/release-notes` | Info | Release notes |
| `/remote-env` | Remote | Remote environment |
| `/remoteControlServer` | Daemon | Remote control server |
| `/rename` | Session | Rename session |
| `/review` | Code | Code review |
| `/rewind` | History | Rewind conversation |
| `/sandbox-toggle` | Security | Toggle sandbox mode |
| `/security-review` | Code | Security-focused review |
| `/stickers` | Fun | Stickers/decorations |
| `/stats` | Analytics | Session statistics |
| `/statusline` | UI | Status line configuration |
| `/tag` | Session | Tag session |
| `/tasks` | Workflow | Task management |
| `/terminal-setup` | Config | Terminal setup |
| `/thinkback` | Debug | Review model's thinking |
| `/thinkback-play` | Debug | Replay thinking process |
| `/torch` | Workflow | Specialized analysis |
| `/ultrareview` | Code | Deep code review |
| `/upgrade` | System | Upgrade Claude Code |
| `/usage` | Billing | Usage information |
| `/vim` | UI | Vim mode toggle |
| `/voice` | Input | Voice input |
| `/workflows` | Automation | Workflow scripts |

### Services behind feature flags

The `services/` directory reveals additional capabilities:

| Service | Description |
|---------|-------------|
| `extractMemories/` | **Automatic memory extraction** — Claude Code can auto-extract key facts from conversations |
| `teamMemorySync/` | **Team memory synchronization** — shared memory across agent teams |
| `policyLimits/` | Organization-level policy limits (enterprise) |
| `remoteManagedSettings/` | Remote settings pushed from claude.ai console |
| `PromptSuggestion/` | AI-powered prompt suggestions |
| `tips/tipRegistry.ts` | Contextual tip system |
| `skillSearch/localSearch.js` | Local skill search index |
| `claudeAiLimits.js` | Claude.ai subscription quota checking |

### What this means

The feature flags reveal that Claude Code is evolving into a **full assistant
platform**, not just a coding tool:

1. **Voice input** is under development (`VOICE_MODE`)
2. **Desktop and mobile handoff** commands exist (`/desktop`, `/mobile`)
3. **GitHub and Slack app integrations** are built-in (`/install-github-app`,
   `/install-slack-app`)
4. **Browser automation** goes beyond Chrome MCP to a dedicated tool
   (`WEB_BROWSER_TOOL`)
5. **Multi-agent orchestration** is real with coordinator mode, forking,
   and inter-agent messaging via Unix domain sockets
6. **Remote/cloud execution** with daemon supervisor, remote triggers,
   and the bridge system for IDE integration
7. **Memory is becoming automated** — `extractMemories/` and
   `teamMemorySync/` suggest Claude Code will automatically extract and
   share learned context
8. **The Tamagotchi is real** — `buddy/` is a companion sprite, gated
   behind the `BUDDY` flag

---

## 6  Rust Port Structure (claw-code)

The Rust port at `instructkr/claw-code` mirrors the original TypeScript module
boundaries. The workspace is organized as a Cargo workspace:

```
rust/
├── Cargo.toml                         # Workspace root (resolver v2)
├── crates/
│   ├── api-client/                    # API client with provider abstraction
│   │   └── src/lib.rs                 #   OAuth, streaming, model resolution
│   │
│   ├── runtime/                       # Core session/agent runtime
│   │   └── src/
│   │       ├── lib.rs                 #   Re-exports all subsystems
│   │       ├── bash.rs                #   Shell command execution
│   │       ├── bootstrap.rs           #   Startup phase plan
│   │       ├── compact.rs             #   Session compaction
│   │       ├── config.rs              #   Config loading + schema
│   │       ├── conversation.rs        #   ConversationRuntime (agent loop)
│   │       ├── file_ops.rs            #   read/write/edit/glob/grep
│   │       ├── hooks.rs               #   Hook event pipeline
│   │       ├── mcp.rs                 #   MCP naming + config hashing
│   │       ├── mcp_client.rs          #   MCP transport abstraction
│   │       ├── mcp_stdio.rs           #   MCP JSON-RPC + process mgmt
│   │       ├── oauth.rs               #   PKCE, token exchange, storage
│   │       ├── permissions.rs         #   3-tier permission model
│   │       ├── prompt.rs              #   System prompt construction
│   │       ├── remote.rs              #   Remote/upstream proxy session
│   │       ├── session.rs             #   Session data model
│   │       ├── sandbox.rs             #   Sandbox execution layer
│   │       └── usage.rs               #   Token tracking + cost est
│   │
│   ├── tools/                         # Tool manifest + execution
│   │   └── src/lib.rs                 #   18 built-in tools + plugin tools
│   │
│   ├── commands/                      # Slash commands
│   │   └── src/lib.rs                 #   25 commands, agents/skills discovery
│   │
│   ├── plugins/                       # Plugin model
│   │   └── src/lib.rs                 #   Manifest, hooks, lifecycle, install
│   │
│   ├── compat-harness/                # Editor integration compatibility
│   │
│   ├── server/                        # HTTP/SSE server (axum)
│   │
│   ├── lsp/                           # LSP client integration
│   │
│   └── claw-cli/                      # Interactive REPL binary
│       └── src/
│           ├── main.rs                #   CLI arg parsing, REPL loop
│           ├── init.rs                #   /init (CLAW.md creation)
│           ├── input.rs               #   Line editor + completion
│           └── render.rs              #   Markdown streaming + spinner
```

### Key crate responsibilities:

| Crate | Purpose |
|-------|---------|
| `api-client` | HTTP client for Anthropic Messages API; handles streaming, OAuth, model alias resolution (`opus` → `claude-opus-4-6`) |
| `runtime` | The engine — owns `ConversationRuntime`, session state, compaction, file operations, MCP orchestration, system prompt building, permission enforcement |
| `tools` | Declares `ToolSpec` schemas for all 18 built-in tools; dispatches tool execution via `execute_tool()` match table; integrates plugin tools through `GlobalToolRegistry` |
| `commands` | 25 slash commands (`/help`, `/compact`, `/model`, `/commit-push-pr`, `/ultraplan`, etc.) with parse + execute logic |
| `plugins` | Full plugin lifecycle: manifest parsing, installation, hooks (pre/post-tool-use), tool + command contribution, marketplace (builtin/bundled/external) |
| `claw-cli` | The user-facing binary: argument parsing, REPL loop, spinner/markdown rendering, OAuth login flow, session management |

---

## 7  The Agent Loop — Async Generator Architecture

The core of Claude Code is `async function* query()` in `src/query.ts`
(1,730 lines). This is **not** a simple request-response loop — it is a
streaming-first async generator that yields events as they arrive, allowing
tool execution to begin *during* the model's response generation.

```
┌──────────────┐
│  User Input  │
└──────┬───────┘
       │
       ▼
┌──────────────────────┐
│  Build API Request   │
│  • system prompt     │
│  • session messages  │
│  • tool definitions  │
│  • model + params    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Anthropic API Call  │◄─────────────────────────┐
│  (streaming)         │                          │
└──────────┬───────────┘                          │
           │                                      │
           ▼                                      │
┌──────────────────────┐                          │
│  Parse Response      │                          │
│  • text blocks       │                          │
│  • tool_use blocks   │                          │
└──────────┬───────────┘                          │
           │                                      │
      ┌────┴────┐                                 │
      │         │                                 │
      ▼         ▼                                 │
  [text]    [tool_use]                            │
   done    ┌────────────────┐                     │
           │ Permission     │                     │
           │ Check          │                     │
           └────┬───────────┘                     │
                │                                 │
           ┌────┴────┐                            │
           │         │                            │
          deny    allow                           │
           │         │                            │
           │    ┌────┴──────────┐                 │
           │    │  Execute Tool │                 │
           │    └────┬──────────┘                 │
           │         │                            │
           └────┬────┘                            │
                │                                 │
                ▼                                 │
       ┌────────────────────┐                     │
       │  Append tool_result│                     │
       │  to session        │────────────────────►│
       └────────────────────┘   (loop back to API)
```

### Core types:

- **`Session`** — ordered list of `ConversationMessage` entries, each with a
  `MessageRole` (User, Assistant) and `ContentBlock` variants (text, tool_use,
  tool_result). Sessions serialize to JSON and can be saved/loaded from disk.
- **`ApiClient` / `ApiRequest`** — trait-based abstraction over the Anthropic
  Messages API; the CLI uses `ClawApiClient` which handles streaming via SSE.
- **`ToolExecutor`** — trait for executing tool calls; the `GlobalToolRegistry`
  implements this, dispatching to built-in handlers or plugin tools.
- **`PermissionPolicy` / `PermissionPrompter`** — the runtime calls the
  prompter before executing any tool; in CLI mode this prompts the user
  interactively.

### Streaming tool execution

A critical architectural choice: the `StreamingToolExecutor`
(`services/tools/StreamingToolExecutor.ts`, 530 lines) executes tools
*during* the model's streaming response, not after it completes. This means:

- As soon as a `tool_use` block is complete in the stream, execution begins
- Multiple tool calls in the same response can run concurrently
  (via `isConcurrencySafe()` on each tool)
- Text rendering continues alongside tool execution
- Results are aggregated and appended for the next API turn

### Turn execution (from `LiveCli.run_turn()`):

1. User types input at the REPL prompt
2. A `Spinner` displays "Thinking..." while the API call streams
3. `ConversationRuntime.run_turn()` is called with the input and a
   permission prompter
4. The runtime builds an `ApiRequest` with system prompt, session history,
   and tool definitions
5. Streaming response is parsed — text blocks are rendered via the
   `TerminalRenderer` (markdown to terminal)
6. Tool-use blocks trigger permission checks, then **streaming** tool execution
7. Tool results are appended to the session and the loop continues
8. On completion, the session is persisted to disk

### Recovery and retry

The agent loop handles several failure modes:
- **Token limit cutoff** — if output is truncated, the system retries up to
  3 recovery turns, asking the model to "resume directly mid-thought without
  apology or recap"
- **User cancellation** — rejection messages explicitly tell the model to
  stop and wait, not to pretend the tool ran
- **Permission denial** — the model may try reasonable alternatives but must
  not covertly bypass the denial's intent
- **Malformed tool input** — JSON parse failures fall back to `{}`, then Zod
  validation produces an error `tool_result`

---

## 8  Tool System

The full tool inventory from the original TypeScript source reveals **40+
tool implementations** — far more than the 18 core tools visible in the
Rust port. The tool system has five layers: type-level interface → concrete
registry → environment gating → permission pruning → execution-mode rewriting.

| Tool | Permission | Description |
|------|-----------|-------------|
| `bash` | `danger-full-access` | Execute shell commands with optional timeout, background mode, and sandbox bypass |
| `read_file` | `read-only` | Read a text file with optional offset/limit |
| `write_file` | `workspace-write` | Write a text file |
| `edit_file` | `workspace-write` | String-replace in a file (old_string → new_string), with optional `replace_all` |
| `glob_search` | `read-only` | Find files by glob pattern |
| `grep_search` | `read-only` | Regex search with context lines, output modes, multiline support |
| `WebFetch` | `read-only` | Fetch a URL, convert to readable text |
| `WebSearch` | `read-only` | Web search with domain allow/block lists |
| `TodoWrite` | `workspace-write` | Structured task list for the session (pending/in_progress/completed) |
| `Skill` | `read-only` | Load a local skill definition |
| `Agent` | `danger-full-access` | Launch a sub-agent with handoff metadata |
| `ToolSearch` | `read-only` | Search for deferred/specialized tools by keyword |
| `NotebookEdit` | `workspace-write` | Replace/insert/delete Jupyter notebook cells |
| `Sleep` | `read-only` | Wait for a duration without holding a shell process |
| `SendUserMessage` | `read-only` | Send a message to the user (aliased as `Brief`) |
| `Config` | `workspace-write` | Get/set Claude Code settings |
| `StructuredOutput` | `read-only` | Return structured output in a requested format |
| `REPL` | `danger-full-access` | Execute code in a REPL subprocess |
| `PowerShell` | `danger-full-access` | Execute PowerShell commands (Windows) |

### Full tool inventory (from TypeScript analysis)

Beyond the core tools, the TypeScript source reveals many feature-gated and
conditional tools:

| Category | Tools |
|----------|-------|
| **Shell & file** | `BashTool`, `FileReadTool`, `FileEditTool`, `FileWriteTool`, `GlobTool`, `GrepTool`, `NotebookEditTool`, `PowerShellTool` |
| **Knowledge** | `WebFetchTool`, `WebSearchTool`, `ListMcpResourcesTool`, `ReadMcpResourceTool`, `ToolSearchTool` |
| **Planning & workflow** | `EnterPlanModeTool`, `ExitPlanModeTool`, `BriefTool`, `TaskOutputTool`, `TaskStopTool`, `TaskCreateTool`, `TaskGetTool`, `TaskListTool`, `TaskUpdateTool` |
| **User interaction** | `AskUserQuestionTool`, `ConfigTool`, `TodoWriteTool` |
| **Agents & orchestration** | `AgentTool`, `SkillTool`, `SendMessageTool`, `TeamCreateTool`, `TeamDeleteTool`, `EnterWorktreeTool`, `ExitWorktreeTool` |
| **Automation** | `SleepTool`, `ScheduleCronTool` (create/delete/list), `RemoteTriggerTool`, `MonitorTool`, `WorkflowTool` |
| **Browser & media** | `WebBrowserTool`, `SendUserFileTool`, `TerminalCaptureTool`, `SnipTool` |
| **Notifications** | `PushNotificationTool`, `SubscribePRTool`, `SuggestBackgroundPRTool` |
| **MCP** | `MCPTool`, `McpAuthTool` |
| **Special** | `REPLTool`, `LSPTool`, `SyntheticOutputTool`, `ListPeersTool` |
| **Debug** | `OverflowTestTool`, `CtxInspectTool` |

### Tool exposure filtering

The runtime does **not** expose all tools to the model. Several rewriting
layers filter the tool set:

1. **Blanket deny rules** — MCP server-prefix deny rules can hide entire
   tool families
2. **Simple mode** (`CLAUDE_CODE_SIMPLE`) — exposes only `BashTool`,
   `FileReadTool`, `FileEditTool` (or `REPLTool` in REPL mode)
3. **Special-tool stripping** — `ListMcpResourcesTool`,
   `ReadMcpResourceTool`, and `SyntheticOutputTool` are hidden from the
   prompt-visible tool list
4. **REPL wrapping** — in REPL mode, primitive tools are hidden and accessed
   indirectly through the REPL wrapper
5. **Coordinator mode** — regains orchestration tools even when the rest of
   the surface is narrow

### Tool execution dispatch (Rust port)

The Rust port dispatches built-in tools through a single `execute_tool()` match:

```rust
pub fn execute_tool(name: &str, input: &Value) -> Result<String, String> {
    match name {
        "bash"          => from_value::<BashCommandInput>(input).and_then(run_bash),
        "read_file"     => from_value::<ReadFileInput>(input).and_then(run_read_file),
        "write_file"    => from_value::<WriteFileInput>(input).and_then(run_write_file),
        "edit_file"     => from_value::<EditFileInput>(input).and_then(run_edit_file),
        "glob_search"   => from_value::<GlobSearchInputValue>(input).and_then(run_glob_search),
        "grep_search"   => from_value::<GrepSearchInput>(input).and_then(run_grep_search),
        "WebFetch"      => from_value::<WebFetchInput>(input).and_then(run_web_fetch),
        "WebSearch"     => from_value::<WebSearchInput>(input).and_then(run_web_search),
        // ... 10 more tools ...
        _ => Err(format!("unsupported tool: {name}")),
    }
}
```

### Tool aliasing

The `GlobalToolRegistry` supports short aliases for common tools:

```
read  → read_file
write → write_file
edit  → edit_file
glob  → glob_search
grep  → grep_search
```

### Tool filtering

Users can restrict the available tool set via `--allowedTools`:

```bash
claw --allowedTools "read_file,edit_file,bash" "fix the bug in main.rs"
```

The `GlobalToolRegistry` validates tool names, resolves aliases, and filters
the tool definitions sent to the API.

---

## 9  Slash Commands

Claude Code provides **25 slash commands** accessible in the REPL:

| Command | Summary |
|---------|---------|
| `/help` | Show available slash commands |
| `/status` | Show current session status (model, messages, turns, usage, permissions, git branch, config/memory files) |
| `/compact` | Compact session history (remove old messages, keep a summary) |
| `/model [name]` | Show or switch the active model |
| `/permissions [mode]` | Show or switch the active permission mode |
| `/clear [--confirm]` | Start a fresh session |
| `/cost` | Show cumulative token usage (input, output, cache create, cache read) |
| `/resume <path>` | Load a saved session |
| `/config [section]` | Inspect config files (env, hooks, model, plugins) |
| `/memory` | Inspect loaded CLAW instruction memory files |
| `/init` | Create a starter `CLAW.md` for the current repo |
| `/diff` | Show git diff for workspace changes |
| `/version` | Show CLI version and build info |
| `/bughunter [scope]` | Inspect codebase for likely bugs |
| `/branch [action]` | List, create, or switch git branches |
| `/worktree [action]` | List, add, remove, or prune git worktrees |
| `/commit` | Generate commit message and create git commit |
| `/commit-push-pr [ctx]` | Commit, push, and open a PR — all in one command |
| `/pr [context]` | Draft or create a pull request |
| `/issue [context]` | Draft or create a GitHub issue |
| `/ultraplan [task]` | Deep planning with multi-step reasoning |
| `/teleport <target>` | Jump to a file or symbol by searching workspace |
| `/debug-tool-call` | Replay the last tool call with debug details |
| `/export [file]` | Export conversation to a file |
| `/session [action]` | List or switch managed sessions |
| `/plugin [action]` | Manage plugins (list/install/enable/disable/uninstall/update) |
| `/agents` | List configured agents |
| `/skills` | List available skills |

Commands are parsed via `SlashCommand::parse()` which splits on `/` and
whitespace, then pattern-matches to the appropriate enum variant. Some
commands support `--resume` mode (marked `resume_supported` in the spec).

---

## 10  Plugin System

The plugin system supports three tiers:

| Kind | Source | Description |
|------|--------|-------------|
| **Builtin** | Ships with Claude Code | Core functionality, always available |
| **Bundled** | Ships with Claude Code | Optional, can be disabled |
| **External** | Installed from marketplace | User-managed, from `.claw-plugin/plugin.json` manifests |

### Plugin manifest (`plugin.json`):

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "An example plugin",
  "permissions": ["read", "write", "execute"],
  "defaultEnabled": true,
  "hooks": {
    "PreToolUse": ["./hooks/pre.sh"],
    "PostToolUse": ["./hooks/post.sh"]
  },
  "lifecycle": {
    "Init": ["./setup.sh"],
    "Shutdown": ["./cleanup.sh"]
  },
  "tools": [...],
  "commands": [...]
}
```

### Hook pipeline

Plugins can register **pre-tool-use** and **post-tool-use** hooks. These are
shell commands that run before/after any tool execution. The `HookRunner`
aggregates hooks from all enabled plugins and runs them in order:

```
User prompt → LLM → tool_use → PreToolUse hooks → execute tool → PostToolUse hooks → tool_result
```

Hooks receive context about the tool call (name, input, output) and can
modify behavior or log telemetry.

### Plugin tool integration

Plugins contribute tools via `PluginToolManifest` entries. These are merged
into the `GlobalToolRegistry` alongside built-in tools. Name conflicts with
built-in tools are rejected at registration time. Each plugin tool specifies
its own `required_permission` level.

---

## 11  Permission Model & YOLO Classifier

Claude Code implements a **3-tier permission model** with an automated
**two-stage YOLO classifier** for auto-mode approval:

| Mode | Label | Scope |
|------|-------|-------|
| `ReadOnly` | `read-only` | Read/search tools only |
| `WorkspaceWrite` | `workspace-write` | Edit files inside the workspace |
| `DangerFullAccess` | `danger-full-access` | Unrestricted tool access |

Every tool in the system declares its `required_permission`. At runtime, the
`PermissionPolicy` compares the tool's requirement against the session's
active mode.

### Permission flow:

1. LLM requests a tool call
2. Runtime looks up the tool's `required_permission` from the `ToolSpec`
3. If the session's `permission_mode` is sufficient, the tool executes
4. If insufficient, the `PermissionPrompter` is invoked — in CLI mode,
   this asks the user interactively
5. The user can approve, deny, or escalate the permission mode

### Configuration:

- Default mode is `danger-full-access` (set via `CLAW_PERMISSION_MODE` env
  var or `--permission-mode` flag)
- Can be changed mid-session with `/permissions`
- `--dangerously-skip-permissions` bypasses all checks

The `bash` tool has an additional `dangerouslyDisableSandbox` parameter that
the model can request to bypass sandbox restrictions for specific commands.

### Two-stage YOLO classifier (auto-mode)

In "auto mode" (where the user wants minimal interactive prompts), a
**two-stage classifier** decides whether to auto-approve tool calls:

```
Tool call requested
        │
        ▼
┌───────────────────┐
│  Stage 1: Fast    │
│  64-token budget  │
│  Heuristic rules  │
│  + confidence     │
└────────┬──────────┘
         │
    ┌────┴────┐
    │         │
  approve   uncertain
    │         │
    │    ┌────┴──────────────┐
    │    │  Stage 2: Thinking│
    │    │  4,096-token      │
    │    │  budget           │
    │    │  Deep reasoning   │
    │    └────────┬──────────┘
    │             │
    │        ┌────┴────┐
    │        │         │
    │      approve    deny
    │        │         │
    └────┬───┘    ┌────┘
         │        │
         ▼        ▼
      execute   prompt user
```

Stage 1 uses a 64-token classifier for instant decisions on clearly safe
operations (e.g., reading a file in the workspace). Stage 2 escalates to a
4,096-token "thinking" classifier for edge cases. The XML classifier expects
responses like `<decision>yes...</decision>` or `<decision>no</decision>`
with optional `<thinking>` blocks. Unparseable responses default to **deny**
(fail-closed).

The permission context passed to tools is richer than a simple mode enum:
- Mode + additional working directories
- Always-allow / always-deny / always-ask rules
- Bypass-permissions availability
- Auto-mode availability
- Stripped dangerous rules
- "Avoid permission prompts" flag
- Pre-plan mode state

---

## 12  Session Management & Three-Tier Compaction

### Session model

A session is a `Session` struct containing a `Vec<ConversationMessage>`. Each
message has a `MessageRole` (User or Assistant) and a vector of
`ContentBlock` variants:

```rust
pub enum ContentBlock {
    Text(String),
    ToolUse { id: String, name: String, input: Value },
    ToolResult { tool_use_id: String, content: String },
}
```

Sessions persist as JSON files under a managed directory. The CLI creates a
new session handle (`SessionHandle { id, path }`) at startup and
auto-persists after each turn.

### Three-tier compaction

Context memory is managed across three tiers:

| Tier | Token Budget | API Call? | Purpose |
|------|-------------|-----------|---------|
| **Microcompact** | ~256 tokens | No | Single-message optimization; local heuristic pruning |
| **Full compaction** | ~4K tokens | Yes | API-evaluated summary of conversation history |
| **Session memory** | Zero-cost | No | In-memory cache of key facts extracted from the session |

The compaction pipeline:

1. **Token estimation** — `estimate_session_tokens()` approximates the
   token count from session messages
2. **Threshold check** — `should_compact()` returns true when the estimated
   count exceeds `CompactionConfig.max_estimated_tokens`
3. **Micro-compaction** (`services/compact/microCompact.ts`, 530 lines) —
   tries local, no-API-call pruning first
4. **Full compaction** (`services/compact/compact.ts`, 1,705 lines) —
   calls the API to produce a summary, then replaces older messages
5. **Auto-compaction** (`services/compact/autoCompact.ts`) — triggers
   automatically based on context window pressure
6. **Continuation** — `get_compact_continuation_message()` provides a
   bridge message so the LLM understands the context was truncated

The `/compact` slash command triggers manual compaction. Auto-compaction
runs transparently in the background.

### Prompt cache break detection

`services/api/promptCacheBreakDetection.ts` (728 lines) monitors whether
the prompt cache is being invalidated between turns. If the static system
prompt prefix changes, cache effectiveness drops dramatically — this
diagnostic system detects and reports those break points.

### Session resume

Sessions can be restored with `--resume SESSION.json`. The resumed session
supports a subset of slash commands (those marked `resume_supported`).

### Session export

`/export [file]` writes the full conversation transcript to a text file.

---

## 13  Security Architecture

The security audit from the analysis repo reveals a mature, defense-in-depth
approach with some notable gaps.

### Bash parser (4,437 lines)

The single most security-critical component. `utils/bash/bashParser.ts`
implements a fail-closed parser that:
- Parses shell commands into an AST
- Blocks 15 dangerous AST node types explicitly
- Handles POSIX `--` end-of-options semantics to prevent path-argument
  confusion attacks (e.g., `rm -- -/../...`)
- Implements shell-prefix classification to detect command injection
  patterns

### Path traversal defenses

Multiple layers:
- `realpath` checks before file operations
- `O_NOFOLLOW` on POSIX for shell task output files (prevents symlink attacks)
- Randomized temp-root paths with per-process nonces (defends against
  shared-`/tmp` pre-creation attacks)
- Deny-first permission evaluation (deny rules → internal exceptions →
  safety checks → `acceptEdits` fast paths)

### Prompt injection mitigations

- `<system_reminder>` content from attachments is wrapped in isolation tags
- Transcript search strips reminder content (it's model-facing, not
  user-visible)
- Read-result reconstruction strips reminder blocks before caching
- Tool-result serialization differs from user-visible rendering

### NO_TOOLS sandwich pattern

Security-critical instructions appear at the **start** of the system prompt
AND again at the **end** with explicit rejection consequences. This ensures
tool-calling restrictions are enforced even under adversarial prompt
conditions.

### Known security gaps (from audit)

| Severity | Finding |
|----------|---------|
| **High** | Sub-agent permission widening — a parent agent under a tight mode can spawn a child with `acceptEdits` and broader tool access, bypassing parent-level approval |
| **Medium** | Non-interactive managed-settings acceptance — headless/CI sessions accept remote settings changes silently without the interactive approval dialog |
| **Medium** | WebFetch hostname safety downgrade — when `skipWebFetchPreflight` is enabled, the public-domain safety check is bypassed; numeric IPs and internal hostnames may be reachable |
| **Low** | Silent malformed-JSONL dropping — corrupted transcript lines are silently dropped rather than causing hard failures, weakening audit integrity |
| **Low** | Cross-process OAuth refresh races — multiple Claude Code processes can race on token exchange for shared credentials |

---

## 14  MCP Integration

Claude Code has comprehensive **Model Context Protocol** support with
multiple transport backends:

| Transport | Type | Description |
|-----------|------|-------------|
| `McpStdioTransport` | Local process | Spawns an MCP server as a child process communicating via stdin/stdout |
| `McpSdkTransport` | SDK-based | Uses the MCP SDK for direct integration |
| `McpRemoteTransport` | HTTP | Connects to a remote MCP server |
| `McpManagedProxyTransport` | Proxy | Routes through a managed proxy (for cloud deployments) |
| `McpWebSocketServerConfig` | WebSocket | WebSocket-based MCP transport |

### MCP lifecycle:

1. **Discovery** — config loader reads MCP server definitions from
   `.claw/settings.json` or env vars
2. **Initialization** — `McpServerManager` spawns servers with
   `McpInitializeParams` (client info, capabilities)
3. **Tool listing** — `McpListToolsParams` → `McpListToolsResult` retrieves
   available tools from each server
4. **Tool execution** — `McpToolCallParams` → `McpToolCallResult` invokes
   tools on the server
5. **Resource access** — `McpListResourcesParams` and `McpReadResourceParams`
   for server-provided resources

Tools discovered via MCP are merged into the tool registry alongside
built-in and plugin tools. MCP tool names are normalized and prefixed
to avoid conflicts.

### OAuth for MCP

MCP servers requiring OAuth get full PKCE flow support:
`McpClientAuth` → `McpClientBootstrap` → `McpOAuthConfig`.

---

## 15  Configuration & Memory Files

### Configuration hierarchy

Claude Code loads configuration from multiple sources, merged in priority
order:

| Source | Path | Scope |
|--------|------|-------|
| Project CLAW.md | `./CLAW.md` | Per-repo instructions for the agent |
| Project .claw | `./.claw/settings.json` | Per-repo machine settings |
| User ~/.claw | `~/.claw/settings.json` | Global user settings |
| User ~/.codex | `~/.codex/` | Compatibility with Codex config |
| Project .codex | `./.codex/` | Compatibility with Codex config |
| Environment | `CLAW_*` env vars | Runtime overrides |

The `ConfigLoader` validates against `CLAW_SETTINGS_SCHEMA_NAME` and merges
all sources into a `RuntimeConfig` with sections for:

- `RuntimeFeatureConfig` — feature flags
- `RuntimeHookConfig` — hook definitions
- `RuntimePluginConfig` — plugin settings
- `McpConfigCollection` — MCP server definitions

### CLAW.md

The `CLAW.md` file is the per-project instruction file. It serves the same
purpose as `.cursorrules` in Cursor — providing project-specific context
and instructions that get injected into the system prompt. The `/init`
command creates a starter `CLAW.md`.

### Memory files

The `/memory` command displays loaded instruction files. Memory files are
analogous to Hermes's `MEMORY.md` — persistent context that survives
across sessions.

---

## 16  OAuth & Authentication

Claude Code uses **OAuth 2.0 with PKCE** for authentication:

```
┌──────────┐     ┌────────────────────────────┐     ┌──────────────┐
│  CLI     │     │  platform.claw.dev         │     │  API         │
│  /login  │     │  /oauth/authorize          │     │  /v1/oauth/  │
│          │     │                            │     │  token       │
└────┬─────┘     └─────────────┬──────────────┘     └──────┬───────┘
     │                         │                           │
     │  1. Generate PKCE pair  │                           │
     │  2. Generate state      │                           │
     │  3. Open browser ───────►                           │
     │                         │  4. User authorizes       │
     │  5. Listen on :4545 ◄───┤  6. Redirect w/ code     │
     │                         │                           │
     │  7. Exchange code ──────┼──────────────────────────►│
     │                         │                           │
     │  8. Receive tokens  ◄───┼───────────────────────────┤
     │  9. Save credentials    │                           │
     └────────────────────────────────────────────────────────
```

Key details:
- **Client ID:** `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- **Authorize URL:** `https://platform.claw.dev/oauth/authorize`
- **Token URL:** `https://platform.claw.dev/v1/oauth/token`
- **Default callback port:** `4545`
- **Scopes:** `user:profile`, `user:inference`, `user:sessions:claw_code`
- Credentials saved locally via `save_oauth_credentials()` / `load_oauth_credentials()`
- `/logout` clears stored credentials

The API client resolves auth from multiple sources via
`resolve_startup_auth_source()` — OAuth tokens, API keys from env, or
no auth.

---

## 17  CLI & REPL

### Entry points

The CLI binary (`claw`) supports multiple modes:

| Mode | Invocation | Description |
|------|-----------|-------------|
| REPL | `claw` | Interactive loop with line editor, spinner, markdown rendering |
| One-shot | `claw -p "prompt"` or `claw prompt "fix the bug"` | Single turn, then exit |
| Resume | `claw --resume session.json [/commands...]` | Restore a session, optionally run commands |
| System prompt | `claw system-prompt --cwd . --date 2026-03-31` | Print the constructed system prompt |
| Manifests | `claw dump-manifests` | Print command/tool/bootstrap counts |
| Login/Logout | `claw login` / `claw logout` | OAuth flow |
| Init | `claw init` | Create `CLAW.md` |

### REPL features

- **Line editor** with history and slash-command tab completion
- **Markdown streaming** — assistant responses stream through a
  `MarkdownStreamState` → `TerminalRenderer` pipeline
- **Spinner** — displays animated progress ("Thinking...") during API calls
- **Color theme** — terminal-aware rendering
- **Model switching** — `/model sonnet` during a session
- **Permission switching** — `/permissions workspace-write` during a session
- **Session auto-persist** — session saved after every turn and on exit

### Output formats

Non-interactive mode supports `--output-format text|json` for pipeline
integration.

---

## 18  System Prompt Construction

The system prompt is built by `load_system_prompt()` in the `runtime` crate.
It takes:

- **Current working directory** — used for project context
- **Date** — injected as current date (default: `2026-03-31`)
- **OS** — operating system string
- **Shell** — user's shell

The `SystemPromptBuilder` assembles multiple sections:

1. **Base instructions** — the agent's identity, behavioral guidelines
2. **Project context** — `ProjectContext` struct with project-specific info
3. **CLAW.md content** — per-repo instructions read from disk
4. **Memory files** — any loaded instruction memory
5. **Tool descriptions** — dynamically generated from the tool registry
6. **Date and environment** — current date, OS, shell info

A key design choice: `FRONTIER_MODEL_NAME` and
`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` constants mark where the static
system prompt ends and dynamic content begins. This boundary is
important for **prompt caching** — Anthropic's API can cache the
static prefix across requests, reducing latency and cost.

---

## 19  Sub-Agents, Skills & Coordinator Mode

### Sub-Agents

The `Agent` tool launches sub-agents — autonomous agent instances that
run in isolation with their own context:

```json
{
  "description": "Refactor the auth module",
  "prompt": "Split auth.rs into smaller modules...",
  "subagent_type": "generalPurpose",
  "name": "auth-refactor",
  "model": "sonnet"
}
```

Sub-agents produce:
- An **output file** with the agent's response
- A **manifest file** (`AgentOutput`) with metadata: agent ID, name,
  status, timestamps, model, errors
- Progress is tracked via `AgentJob` which polls for completion

### Skills

Skills are loaded via the `Skill` tool — a way to inject procedural
knowledge:

```json
{
  "skill": "code-review",
  "args": "--strict"
}
```

The tool resolves the skill name to a local definition file (analogous
to Hermes's `SKILL.md`), reads its instructions, and returns them as
context for the LLM.

Skills are discovered from multiple directories:
- `ProjectCodex` (`.codex/`)
- `ProjectClaw` (`.claw/`)
- `UserCodexHome` (`$CODEX_HOME/`)
- `UserCodex` (`~/.codex/`)
- `UserClaw` (`~/.claw/`)

The `/skills` and `/agents` slash commands list available definitions.

### Coordinator Mode

`coordinator/coordinatorMode.ts` (369 lines) implements a **multi-agent
orchestration layer** that synthesizes results from parallel agent threads:

- The coordinator spawns multiple sub-agents working in parallel
- Each sub-agent operates on a different aspect of the task
- Results are aggregated and synthesized by the coordinator
- This enables complex workflows and coordinated reasoning across agents
- In coordinator mode, the tool surface regains `AgentTool`, `TaskStopTool`,
  and `SendMessageTool` even when the rest of the surface is narrowed

---

## 20  Daemon Mode & Proactive Agent

The analysis repo reveals that Claude Code has significant **always-on**
infrastructure that goes well beyond the interactive REPL.

### Daemon mode

A long-lived supervisor process (`claude daemon ...`) that:
- Owns durable infrastructure like scheduling and remote-control connectivity
- Spawns lightweight worker subprocesses as `claude --daemon-worker <kind>`
- Workers only initialize what they need
- The supervisor can respawn crashed child agents

### Proactive agent (KAIROS)

When enabled via `--proactive` or `CLAUDE_CODE_PROACTIVE`, the agent
receives periodic `<proactive_tick>` prompts and should either do useful
work or call `Sleep`:

- First wake-up: greet briefly, ask what the user wants
- Later wake-ups: look for useful work
- If nothing useful: **must** call `Sleep`
- Tick prompts are hidden from the transcript (`isMeta: true`)
- In headless mode, ticks are injected when the queue is empty
- In REPL mode, ticks are queued as hidden meta prompts

### Background sessions

- `claude ps|logs|attach|kill` manage background sessions
- Sessions register in `~/.claude/sessions/<id>.json` with metadata:
  PID, session ID, CWD, start time, kind (`interactive|bg|daemon|daemon-worker`)
- Background sessions detach from tmux instead of terminating on Ctrl+C

### Cron scheduling

- `CronCreate`, `CronDelete`, `CronList` tools manage scheduled tasks
- Durable tasks persist to `.claude/scheduled_tasks.json`
- Session-only tasks live in memory
- One-shot tasks auto-delete after firing
- Recurring tasks auto-expire after 7 days unless marked permanent
- Per-project lock file prevents double-firing across processes

### Remote triggers

- `RemoteTrigger` tool manages scheduled remote agents via the claude.ai API
- Supports: list, get, create, update, run
- OAuth-authenticated calls to `/v1/code/triggers`
- Remote agents run in Anthropic cloud environments

---

## 21  Bundled Skills Catalog

The TypeScript source ships with **15+ bundled skills** — opinionated
workflow prompts that encode the product team's recommended practices.

### Always-available skills

| Skill | Purpose |
|-------|---------|
| `update-config` | Settings expert: explains and modifies `settings.json` safely |
| `verify` | "Prove it works" workflow — pushes model toward real validation, not static reasoning |
| `debug` | Incident triage for Claude Code itself (reads session debug logs) |
| `skillify` | **Workflow capture** — turns a successful session into a reusable custom `SKILL.md` |
| `remember` | Memory curation — reviews auto-learned entries and promotes/reconciles across memory layers |
| `simplify` | Self-review and cleanup — critiques and tightens the model's own code changes |
| `batch` | **Bulk-change orchestrator** — research → decompose → distribute across worktree agents → verify → track |
| `stuck` | Diagnostic skill for frozen/slow Claude Code sessions |
| `keybindings-help` | Hidden helper for keyboard shortcut configuration |
| `lorem-ipsum` | Filler text generation for long-context testing (internal) |

### Feature-gated skills

| Skill | Gate | Purpose |
|-------|------|---------|
| `dream` | KAIROS/background-memory | Background memory / deferred-work processing |
| `hunter` | REVIEW_ARTIFACT | Deep-review / artifact-review workflow |
| `loop` | AGENT_TRIGGERS | Local recurring-work scheduler (prompts/commands on a cron) |
| `schedule` | AGENT_TRIGGERS_REMOTE | Remote counterpart to `loop` — manages recurring remote agents |
| `claude-api` | BUILDING_CLAUDE_APPS | Built-in developer advocate with bundled Anthropic SDK docs |
| `claude-in-chrome` | Chrome integration | Browser automation entry skill (Chrome MCP) |

### Product signals

The bundled skills reveal Anthropic's product bets:
- **Workflow capture is first-class** (`skillify`) — the product can teach
  itself new workflows from real usage
- **Background/scheduled automation is real** (`loop`, `schedule`, `dream`)
- **Browser automation is built-in** (`claude-in-chrome`)
- **Large-scale parallel changes are explicitly encouraged** (`batch`)
- **Memory curation is becoming productized** (`remember`)

---

## 22  Bootstrap Sequence

The startup sequence mirrors the leaked `BootstrapPlan::claw_default()`:

```
1. Top-level prefetch side effects
   • MDM raw-read prefetch (workspace metadata)
   • Keychain prefetch (credential loading)
   • Project scan (file discovery)

2. Warning handler and environment guards
   • Check for supported platform/version

3. CLI parser and pre-action trust gate
   • Parse args, resolve model aliases
   • Determine permission mode

4. setup() + commands/agents parallel load
   • Build WorkspaceSetup (Python version, platform, test command)
   • Load command snapshot + tool snapshot in parallel
   • Prepare parity audit hooks

5. Deferred init after trust
   • Trust-gated initialization steps
   • Plugin manager construction

6. Mode routing
   • local / remote / ssh / teleport / direct-connect / deep-link

7. Query engine submit loop
   • REPL loop or one-shot prompt execution
```

This reveals that Claude Code supports **6 connection modes**, suggesting
planned or existing remote-agent capabilities:

| Mode | Purpose |
|------|---------|
| `local` | Default — run tools on the local machine |
| `remote` | Remote control via upstream proxy |
| `ssh` | SSH proxy to a remote machine |
| `teleport` | Resume/create sessions remotely |
| `direct-connect` | Direct connection to a remote runtime |
| `deep-link` | URI-based session launch |

---

## 23  Usage Tracking & Cost

Token usage is tracked at multiple granularities:

```rust
pub struct TokenUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_creation_input_tokens: u64,
    pub cache_read_input_tokens: u64,
}
```

The `UsageTracker` aggregates per-turn and cumulative usage. Cost estimation
uses `pricing_for_model()` → `ModelPricing` → `UsageCostEstimate`, formatted
via `format_usd()`.

The `/cost` slash command displays:
- Input tokens
- Output tokens
- Cache creation tokens
- Cache read tokens
- Total tokens

This granularity confirms that Claude Code heavily relies on Anthropic's
**prompt caching** feature to reduce costs — the `cache_creation` and
`cache_read` fields track how effectively the system prompt prefix is being
cached across turns.

---

## 24  Model Behavioral Contract

The analysis repo documents 21 behavioral invariants that the Claude Code
harness **implicitly expects** from the model. These are not in the system
prompt — they are enforced by parser expectations, retry/recovery prompts,
and tool-use repair logic.

### Critical invariants

| # | Invariant | Severity |
|---|-----------|----------|
| 1 | Tool calls must be expressed as actual `tool_use` blocks; `stop_reason` is not trusted | Critical |
| 2 | `tool_use.input` must be valid JSON matching the tool schema with correct types | Critical |
| 4 | Every `tool_use` id must be unique and paired with exactly one matching `tool_result` | Critical |
| 11 | Structured-output mode requires one final `StructuredOutput` tool call with schema-valid JSON | Critical |
| 14 | The XML classifier must emit `<decision>` first, optional `<thinking>`, no preamble | Critical |
| 16 | On user rejection/cancellation, the model must stop and wait, not pretend the tool ran | Critical |

### Repair mechanisms

When the model violates invariants, the harness repairs the transcript:
- Orphaned `tool_result` blocks are stripped
- Duplicate tool IDs are deduped
- Missing `tool_result`s get synthetic placeholders injected
- Empty/whitespace-only assistant messages are filtered or replaced
- Trailing thinking blocks are truncated
- Server-side tool orphans are stripped

### Recovery prompts that shape behavior

- **Token limit cutoff:** "resume directly, no apology, no recap, pick up
  mid-thought, break work smaller"
- **Cancel/reject:** "stop and wait"
- **Permission denied:** "do not maliciously bypass; stop and explain if
  essential"
- **Classifier outage:** "wait briefly and retry, or continue with other tasks"
- **Deferred tool:** "load tool schema with ToolSearch and retry"

### Helper prompt format expectations

Several helper systems expect specific output formats:
- **Skill improvement:** JSON array inside `<updates>...</updates>` tags
- **Agent generation:** bare JSON object, no wrapper text
- **Date/time parsing:** ISO-8601 string or exactly `INVALID`
- **Shell prefix:** single prefix token or sentinel like `command_injection_detected`
- **Session title:** `safeParseJSON` + Zod → `{"title": "..."}`

---

## 25  Notable Curiosities from the Leak

The community discovered several interesting details:

### Spinner verbs
Claude Code uses rotating "spinner verbs" — phrases displayed while the LLM
processes. These appear as animated text in the terminal (e.g.
"🦀 Thinking...") and reportedly include various playful phrases.

### Lobster emoji
The REPL banner includes a lobster emoji (🦞) — Claude Code's unofficial
mascot, visible in the ASCII art banner.

### Tamagotchi easter egg
One person reported finding a hidden "Tamagotchi-style" virtual pet in the
code, allegedly scheduled for an April 1 launch. This was likely an April
Fool's joke built into the codebase.

### Frustration detection
`useFrustrationDetection.ts` uses regex patterns to detect profanity and
emotional signals in user input. When triggered, this fires telemetry events,
presents feedback surveys, and auto-files issues for product improvement.
In external (non-Anthropic) builds, this is replaced with a no-op returning
`{ state: 'closed' }`.

### ~15% "ant-only" code
Approximately 15% of the codebase is internal Anthropic code, conditionally
included at build time via `"external" === 'ant' ? require(...)` patterns.
External builds substitute no-op fallbacks. This gates internal telemetry,
A/B testing, and experimental features.

### Copy-on-write speculation
The system pre-computes the next user response on an overlay filesystem,
allowing near-instant switching when users navigate between sessions or
make rapid back-to-back requests.

### Model aliases
Simple aliases are supported: `opus` → `claude-opus-4-6`,
`sonnet` → `claude-sonnet-4-6`, `haiku` → `claude-haiku-4-5-20251213`.

### Token limits
Opus models get 32k max output tokens; all others get 64k. This is
hard-coded in the CLI.

---

## 26  Comparison with Hermes & OpenClaw

| Dimension | Claude Code | Hermes Agent | OpenClaw |
|-----------|-------------|--------------|----------|
| **Language** | TypeScript (npm) | Python | TypeScript (Node) |
| **License** | Proprietary | MIT | MIT |
| **LLM Provider** | Anthropic only | Any (OpenRouter, OpenAI, Anthropic, custom) | Any |
| **Interface** | Terminal REPL | CLI TUI + Gateway + ACP | CLI + Gateway + Native Apps |
| **Tool count** | 40+ built-in + plugins + MCP (18 always-on, rest feature-gated) | 40+ built-in + MCP | 30+ built-in + plugins + MCP |
| **Permission model** | 3-tier (read-only / write / full) | Trust-based | 3-tier + sandbox |
| **Session persistence** | JSON files | SQLite with FTS5 | SQLite |
| **Compaction** | Token-estimated, summary-based | Mid-convo compression | Configurable |
| **Plugin system** | Full (manifest, hooks, lifecycle, marketplace) | Skills-based | Plugin + Node packages |
| **Memory** | CLAW.md + memory files | MEMORY.md + USER.md + Honcho + SecondBrain | Context files |
| **Self-learning** | Partial (`skillify` skill + auto-memory) | Full loop (skills, memory, nudges) | Basic (context files) |
| **Sub-agents** | Yes (Agent tool) | Yes (sessions_send) | Yes (multi-agent routing) |
| **Remote execution** | 6 modes (local/remote/ssh/teleport/direct/deep-link) | Docker, SSH, Daytona, Modal, Singularity | Docker, SSH, OpenShell |
| **Messaging channels** | None (terminal only) | Telegram, Discord, Slack, etc. | 25+ channels |
| **Always-on** | Yes (daemon mode + KAIROS, but feature-gated) | Yes (Gateway) | Yes (Gateway) |

### Key differentiators:

- **Claude Code** is the most polished terminal-only coding experience with
  tight Anthropic API integration, emerging daemon/assistant capabilities,
  and a nascent self-learning system (`skillify` + auto-memory), but it's
  locked to one provider.
- **Hermes** is the most sophisticated self-learning system with
  cross-session user modeling, but has no plugin marketplace.
- **OpenClaw** is the most complete platform with native apps and 25+
  messaging channels, but its self-learning is basic.

---

## 27  What to Lift for NemoClaw Escapades

Based on this deep dive, here are the Claude Code patterns and components
most relevant to NemoClaw milestones:

### Milestone 1 — Foundation

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| Async generator agent loop (`async function* query()`) | Model our loop as a streaming generator: prompt → LLM stream → tool calls during stream → persist |
| System prompt builder with `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` | Separate static prefix (cacheable) from dynamic content for cost efficiency |
| 3-tier permission model + YOLO classifier | Adopt the same tiers; consider a fast-path auto-classifier for common safe operations |
| Tool schema declarations with 5-layer filtering | Define tools with JSON Schema + permission requirements; support simple/full/coordinator modes |
| Session persistence (JSON) + three-tier compaction | Start with JSON-file sessions + micro/full compaction; migrate to SQLite later |
| Model behavioral contract | Implement the same repair mechanisms: orphan stripping, synthetic placeholders, fail-closed parsing |

### Milestone 2 — Knowledge Management

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| CLAW.md project context | Support per-repo instruction files that inject into the system prompt |
| Memory files | Load persistent context from well-known file locations |
| `WebFetch` / `WebSearch` tools | Expose web access for knowledge gathering |

### Milestone 3 — Coding Agent

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| `bash` tool with 4,437-line parser + sandbox | Sandboxed shell execution with fail-closed security parsing |
| `edit_file` (string-replace) | The same old_string/new_string pattern used by Cursor and Claude Code |
| `Agent` tool + Coordinator mode | Delegate coding tasks to parallel sub-agent instances with result synthesis |
| `/commit-push-pr` workflow | One-command git workflow: commit → push → PR creation |
| `/bughunter` command | Automated codebase inspection — worth replicating |
| `batch` skill pattern | Research → decompose → distribute across worktree agents → verify → track |
| Streaming tool execution | Execute tools during response generation, not after — critical for latency |

### Milestone 4 — Self-Improvement Loop

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| `skillify` skill | **Workflow capture** — turn successful sessions into reusable `SKILL.md` files |
| `remember` skill | Memory curation — auto-learned entries promoted across memory layers |
| `Skill` tool + discovery | Support skill definitions that inject procedural knowledge |
| Plugin hooks (pre/post-tool-use) | Allow self-improvement hooks to observe and learn from tool execution |
| Three-tier compaction | Essential for long-running sessions; adopt micro + full + session-memory approach |
| `TodoWrite` structured tasks | Useful for self-directed planning within a session |

### Milestone 5 — Review Agent

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| Permission escalation flow | Review agent should have restricted permissions; escalate only when needed |
| `/diff` command | Quick workspace diff for review context |

### Milestone 6 — Professional KB

| Claude Code Pattern | How to Apply |
|---------------------|-------------|
| MCP integration (4 transports) | MCP is the standard for external tool servers; adopt stdio + remote transports |
| `ConfigLoader` multi-source merge | Configuration from project, user, and environment levels |
| `ToolSearch` for deferred tools | Lazy tool loading for large tool registries |

### Key gaps — things Claude Code lacks that NemoClaw needs:

| Gap | NemoClaw Approach |
|-----|-------------------|
| Daemon mode is feature-gated, not production | Need reliable Gateway architecture (from Hermes/OpenClaw) |
| No messaging channels | Need Slack adapter |
| Self-learning is nascent (`skillify` + `remember`) | Need the full Hermes-style loop with nudges and cross-session learning |
| No cross-session user modeling | Need Honcho or similar |
| No local inference support | Need Inference Hub integration for self-hosted models |
| Single-provider lock-in | Need provider resolver for multiple backends |
| Sub-agent permission widening (security gap) | Must enforce that children cannot exceed parent permission scope |
