# Hermes vs OpenClaw vs Claude Code — Comparative Deep Dive

> **Sources:**
> [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) (17k stars, MIT, v0.5.0) &nbsp;|&nbsp;
> [openclaw/openclaw](https://github.com/openclaw/openclaw) (341k stars, MIT, 2026.3.28) &nbsp;|&nbsp;
> Claude Code v2.1.88 (proprietary, leaked TypeScript source via `.map` file; Rust port at [instructkr/claw-code](https://github.com/instructkr/claw-code))
>
> **Last reviewed:** 2026-04-04
>
> **Companion docs:**
> [Hermes Deep Dive](hermes_deep_dive.md) &nbsp;|&nbsp;
> [OpenClaw Deep Dive](openclaw_deep_dive.md) &nbsp;|&nbsp;
> [Claude Code Deep Dive](claude_code_deep_dive.md)

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

Hermes, OpenClaw, and Claude Code occupy the same category — AI assistants
with sandbox execution and tool-calling loops — but they optimize for
fundamentally different things.

### Core Identity

| | Hermes | OpenClaw | Claude Code |
|---|--------|----------|-------------|
| **Tagline** | "The self-improving agent" | "The complete assistant platform" | "The polished coding terminal" |
| **Optimizes for** | Learning from experience; memory that deepens; skills created at runtime; flexible inference backends | Breadth of integrations; product polish (native apps, Canvas, voice); plugin ecosystem; multi-device experience | Developer UX in the terminal; streaming-first architecture; security-in-depth (permission model, bash parser); tight model-harness co-design |
| **Written in** | Python (92%) | TypeScript (89%) | TypeScript (Bun) |
| **Stars** | 17k | 341k | Proprietary (leaked) |
| **License** | MIT | MIT | Proprietary |

### Where all three converge

- AgentSkills standard (SKILL.md)
- Prompt → LLM → tool calls → loop architecture
- MCP integration
- Sub-agent delegation
- Session persistence with compaction
- Plugin/hook extensibility

### Where they diverge

| Dimension | Hermes | OpenClaw | Claude Code |
|-----------|--------|----------|-------------|
| **Self-learning** | ★★★ | ★ | ★★ |
| **Native apps** | — | ★★★ | — |
| **Messaging** | ★★ | ★★★ | — |
| **Memory depth** | ★★★ | ★ | ★★ |
| **Security model** | ★ | ★★★ | ★★★ |
| **Provider freedom** | ★★★ | ★★★ | — |
| **Terminal UX** | ★★ | ★★ | ★★★ |
| **Daemon / always-on** | ★★ | ★★★ | ★★ (gated) |

**Bottom line for NemoClaw Escapades:** Use OpenClaw as the structural
reference (Gateway pattern, OpenShell integration, plugin architecture),
Hermes as the intelligence reference (self-learning loop, memory system,
skills auto-creation), and Claude Code as the engineering-quality reference
(streaming architecture, three-tier compaction, permission model, bash
security parser, prompt caching). The three are complementary.

---

## 2  Philosophy & Design Goals

| Dimension | Hermes | OpenClaw | Claude Code |
|-----------|--------|----------|-------------|
| **Primary audience** | Power users, researchers, self-hosters | Broad consumer + developer audience | Professional developers (terminal-native) |
| **Core thesis** | An agent should learn from experience and get better over time | An agent should be a complete personal assistant platform | An agent should be the best coding companion in the terminal |
| **Design surface** | CLI-first, messaging as extension | Multi-surface: CLI, native apps, Canvas, WebChat, 25+ channels | Terminal REPL only (daemon/IDE bridge feature-gated) |
| **Extensibility model** | External skill dirs, MCP tools | Full plugin architecture (channels, tools, skills, providers, speech, image) | Plugins (manifest + hooks + lifecycle) + MCP + feature flags |
| **Deployment model** | Single Python process, any $5 VPS | Node.js daemon, supervised by launchd/systemd | Bun binary, single-user CLI (daemon mode feature-gated) |
| **RL / training** | First-class (environments, trajectories, Atropos integration) | None | None |
| **User modeling** | Deep (Honcho dialectic reasoning across sessions) | Minimal (context file injection) | Moderate (CLAW.md + memory files + nascent auto-extraction) |
| **OpenShell support** | Not integrated (uses own terminal backends) | Native backend since v2026.3 | Not integrated |
| **Provider lock-in** | None (any OpenAI-compatible endpoint) | None (any provider) | Anthropic only |

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

**Claude Code excels at:**
- The most polished terminal coding experience (streaming markdown, spinner verbs, React+Ink UI)
- Security engineering depth (4,437-line bash parser, 3-tier permissions, YOLO classifier, NO_TOOLS sandwich)
- Streaming-first architecture (tools execute *during* response generation, not after)
- Three-tier compaction system (micro → full → session memory)
- Prompt caching with cache-break detection for cost optimization
- Model behavioral contract with repair mechanisms (21 invariants, fail-closed parsing)

---

## 3  Architecture Comparison

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Architecture Comparison                                │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐     │
│  │  Entry Points    │      │  User Interfaces │      │  Entry Points    │     │
│  │  • CLI TUI       │      │  • 25+ channels  │      │  • REPL (claw)   │     │
│  │  • Gateway       │      │  • macOS/iOS/    │      │  • One-shot (-p) │     │
│  │    (6 platforms)  │      │    Android       │      │  • --resume      │     │
│  │  • ACP (editor)  │      │  • WebChat       │      │  • Editor bridge │     │
│  │  • Batch Runner  │      │  • Canvas (A2UI) │      │  • Daemon mode   │     │
│  └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────┘     │
│           │                         │                         │               │
│           ▼                         ▼                         ▼               │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────────┐ │
│  │  AIAgent (Python)│      │  Gateway Daemon  │      │  ConversationRuntime │ │
│  │  • Single class  │      │  (Node.js, WS    │      │  (TypeScript/Bun)    │ │
│  │  • Prompt builder│      │   control plane) │      │  • System prompt     │ │
│  │  • Provider      │      │  • Channel       │      │    builder           │ │
│  │    resolver      │      │    adapters      │      │  • API client        │ │
│  │  • Tool dispatch │      │  • Session router│      │  • StreamingTool     │ │
│  │  • Context       │      │  • Plugin loader │      │    Executor          │ │
│  │    compressor    │      │  • Node manager  │      │  • Compaction engine │ │
│  │  • Session       │      │                  │      │  • Permission        │ │
│  │    persistence   │      │                  │      │    prompter          │ │
│  └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────────┘ │
│           │                         │                         │               │
│           ▼                         ▼                         ▼               │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────────┐ │
│  │  Tools Runtime   │      │  Pi Agent Runtime│      │  Tool Registry       │ │
│  │  • 40+ built-in  │      │  (RPC mode)      │      │  • 40+ built-in      │ │
│  │  • MCP tools     │      │  • System prompt │      │  • MCP tools         │ │
│  │  • Skills tools  │      │    builder       │      │  • Plugin tools      │ │
│  │  • Memory tools  │      │  • Provider      │      │  • Feature-gated     │ │
│  │  • Honcho tools  │      │    selection     │      │    tools             │ │
│  └────────┬─────────┘      │  • Tool executor │      │  • 5-layer filtering │ │
│           │                └────────┬─────────┘      └────────┬─────────────┘ │
│           ▼                         ▼                         ▼               │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────────┐ │
│  │  Terminal        │      │  Sandbox         │      │  Sandbox / Bash      │ │
│  │  Backends        │      │  Backends        │      │  • Host (default)    │ │
│  │  • local         │      │  • Docker        │      │  • 4,437-line bash   │ │
│  │  • docker        │      │  • SSH           │      │    parser            │ │
│  │  • ssh           │      │  • OpenShell     │      │  • Permission-gated  │ │
│  │  • daytona       │      │                  │      │    execution         │ │
│  │  • singularity   │      │  Workspace modes:│      │  • 6 connection modes│ │
│  │  • modal         │      │  • mirror        │      │    (local/remote/ssh/│ │
│  └──────────────────┘      │  • remote        │      │    teleport/direct/  │ │
│                            └──────────────────┘      │    deep-link)        │ │
│                                                      └──────────────────────┘ │
│                                                                                │
│  KEY STRUCTURAL DIFFERENCES:                                                   │
│                                                                                │
│  Hermes: AIAgent is a self-contained Python class. Every entry point           │
│  instantiates one. The Gateway is one of several entry points.                 │
│                                                                                │
│  OpenClaw: The Gateway IS the system. It's a long-lived daemon that owns       │
│  all state, messaging, and control plane. Pi is invoked via RPC.               │
│                                                                                │
│  Claude Code: ConversationRuntime is a TypeScript engine designed for a        │
│  single user in a terminal. The REPL owns the UI; daemon mode and the          │
│  IDE bridge are feature-gated extensions. Streaming-first async generator      │
│  architecture (tools execute during response, not after).                      │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Architectural Trade-offs

| Concern | Hermes | OpenClaw | Claude Code | Implication |
|---------|--------|----------|-------------|-------------|
| **Coupling** | Agent loosely coupled to entry points. Can run standalone. | Pi tightly coupled to Gateway. Cannot run without it. | Runtime designed for single-user REPL. Daemon mode bolted on. | Hermes is easiest to embed; OpenClaw most cohesive as product; Claude Code most polished for terminal use. |
| **State ownership** | Agent owns SQLite + local files | Gateway owns sessions + WS state | JSON files per session, managed directory | Hermes state is portable; OpenClaw is centralized; Claude Code is file-based and simple. |
| **Scaling** | Single process, in-process sub-agents | Single daemon, sub-agents via session spawning | Single process, sub-agents via Agent tool | None scales horizontally out of the box. |
| **Protocol** | Direct function calls + callbacks | Typed WebSocket JSON-RPC | Direct function calls + streaming generator | OpenClaw's protocol enables remote clients. Claude Code's generator enables mid-stream tool execution. |
| **Extensibility** | MCP tools, external skill dirs | Full plugin system (channels, tools, skills, providers) | Plugins (manifest + hooks) + MCP (4 transports) + feature flags | OpenClaw has richest extension surface; Claude Code has most mature plugin lifecycle. |
| **Runtime** | CPython | Node.js | Bun (with build-time dead code elimination) | Bun enables feature-flag stripping at compile time — code for gated features is physically absent from builds. |

---

## 4  Agent Loop / Runtime

All three implement a similar core loop (prompt → LLM → tool calls →
loop), but with important differences in streaming, context management,
and state.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Agent Loop Comparison                                  │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│  (run_agent.py::AIAgent)   (Pi agent runtime)         (query.ts generator)     │
│                                                                                │
│  1. Generate task ID       1. Receive message from    1. User types at REPL    │
│  2. Append user message       Gateway                 2. Build API request     │
│  3. Load/build cached      2. Build system prompt        (system prompt +      │
│     system prompt             (AGENTS.md + SOUL.md +     session + tools)      │
│     (frozen at session        TOOLS.md + IDENTITY.md  3. Streaming API call    │
│      start)                   + USER.md + HEARTBEAT   4. Parse response stream │
│  4. Maybe preflight-          + skills XML +          5. On tool_use block:    │
│     compress                  context files)             permission check →    │
│  5. Build API messages +   3. Resolve model provider     execute DURING stream │
│     ephemeral prompt          + fallback chain         6. Append tool_result   │
│     layers                 4. Stream to LLM           7. Loop until text-only  │
│  6. Apply prompt caching      (block streaming for    8. Persist session       │
│     (provider-specific)       tools)                  9. Auto-compact if       │
│  7. Interruptible API      5. Execute tool calls         context pressure      │
│     call                   6. Loop until final                                 │
│  8. Execute tools (seq        response                Unique to Claude Code:   │
│     or concurrent)         7. Deliver response to     • Streaming tool exec    │
│  9. Loop until final text     Gateway                   (during response, not  │
│  10. Persist session +                                  after)                 │
│      cleanup               Differences:               • 3-tier compaction      │
│                            • No context compression     (micro/full/session    │
│  Unique to Hermes:         • No prompt caching          memory)               │
│  • Context compression     • No iteration budget      • YOLO auto-classifier  │
│    mid-convo               • Block streaming (not       (2-stage permission)   │
│  • Provider-specific         just text streaming)     • Prompt cache boundary  │
│    prompt caching          • Thinking level control     marker for cost        │
│  • Budget tracking across    (off → xhigh)            • Copy-on-write         │
│    agents                  • Media pipeline (images,    speculation            │
│  • Fallback model on         audio, video)            • Token limit recovery   │
│    primary fail                                         (3 retry turns)        │
│  • Session lineage across                             • Model behavioral       │
│    splits                                               contract (21           │
│                                                         invariants + repair)   │
└────────────────────────────────────────────────────────────────────────────────┘
```

### System Prompt Assembly

| Component | Hermes | OpenClaw | Claude Code |
|-----------|--------|----------|-------------|
| Agent instructions | System prompt (built by `prompt_builder.py`) | `AGENTS.md` | Base instructions (identity + behavioral guidelines) |
| Persona / identity | Embedded in system prompt | `SOUL.md` + `IDENTITY.md` | Embedded in base instructions |
| User profile | `USER.md` (bounded, ~500 tokens) | `USER.md` (unbounded) | — (no user profile file) |
| Agent memory | `MEMORY.md` (bounded, ~800 tokens) | — (no equivalent) | Memory files (loaded via `/memory`) |
| Tool guidance | Inline in system prompt | `TOOLS.md` | Dynamic tool descriptions from registry |
| Time awareness | — | `HEARTBEAT.md` | Date/OS/shell injected as context |
| First-run bootstrap | — | `BOOTSTRAP.md` | `BootstrapPlan::claw_default()` (6-phase startup) |
| Skills | Progressive disclosure (list only in prompt, load on demand) | Full compact XML list injected into prompt | Skill tool loads on demand; `/skills` lists available |
| Cross-session context | Honcho dialectic summary (auto-injected) | — | — (no cross-session context) |
| Project context | — | — | `CLAW.md` (per-repo instructions) + `ProjectContext` |
| Cache boundary | — | — | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` (static/dynamic split for prompt caching) |

**Key insight:** Hermes is most token-efficient (bounded memory files,
progressive skill disclosure, context compression). OpenClaw is most explicit
(everything injected upfront, no compression). Claude Code is most
cache-optimized (static prefix cached via boundary marker, three-tier
compaction keeps context under control).

---

## 5  Inference & Provider System

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Provider Comparison                                    │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  Config: config.yaml       Config: openclaw.json      Config: env / OAuth      │
│                                                                                │
│  providers:                agent:                     Models:                  │
│    openrouter:               model: "anthropic:..."   • claude-opus-4-6        │
│      api_key: ...            imageModel: "..."          (default, 32k output)  │
│    openai:                                            • claude-sonnet-4-6      │
│      api_key: ...          + per-agent model           • claude-haiku-4-5      │
│    anthropic:                overrides                                         │
│      api_key: ...          + per-spawn model          Auth:                    │
│    custom:                   overrides                • OAuth 2.0 + PKCE       │
│      base_url: https://    + auth profile rotation    • API key (env var)      │
│      api_key: ...          + subscription model       • macOS Keychain         │
│                                                                                │
│  Resolution chain:         Resolution chain:          Resolution:              │
│  1. Explicit provider      1. Per-spawn override      1. OAuth token           │
│     prefix                 2. Per-agent override      2. API key from env      │
│  2. Model → provider       3. Global agent.model      3. No auth              │
│     mapping                4. Failover chain                                   │
│  3. Base URL heuristics                               Model aliases:           │
│  4. Fallback to default                               opus → claude-opus-4-6   │
│                                                       sonnet → claude-sonnet-  │
│  API modes:                API modes:                   4-6                    │
│  • chat_completions        • OpenAI                   haiku → claude-haiku-    │
│    (OpenAI compat)         • Anthropic (native)         4-5                    │
│  • codex_responses         • Google (native)                                   │
│    (Codex/Responses)       • OpenRouter               API mode:                │
│  • anthropic_messages      • Custom endpoint           • Anthropic Messages    │
│    (native Claude)                                      API only               │
│                                                                                │
│  Unique features:          Unique features:           Unique features:         │
│  • Any base_url            • Subscription model       • Prompt caching with    │
│  • Provider-specific         support                    cache-break detection  │
│    prompt caching          • Auth profile rotation    • Token usage tracking    │
│  • Fallback model on       • Per-agent + per-spawn      (input/output/cache-  │
│    primary fail              models                     create/cache-read)     │
│  • Budget tracking         • Thinking level control   • Hard-coded token       │
│    across agents             (off → xhigh)              limits per model       │
│                                                       • /model switch mid-     │
│                                                         session                │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Inference Hub Compatibility

| | Hermes | OpenClaw | Claude Code |
|---|--------|----------|-------------|
| **How** | Set `base_url` to inference hub endpoint under `providers.custom` | Register as a custom provider in `openclaw.json` | Not supported — Anthropic API only |
| **Effort** | Zero code changes — config only | Zero code changes — config only | Would require forking; API client is Anthropic-specific |
| **Caveat** | Non-standard auth headers or tool formats need a thin adapter | Same | Single-provider by design |

Hermes and OpenClaw are compatible with any OpenAI-compatible endpoint.
Claude Code is locked to the Anthropic API — this is the most significant
architectural limitation for NemoClaw Escapades.

---

## 6  Tools & Plugins

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Tools Comparison                                       │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  Registration:             Registration:              Registration:            │
│  • Python functions with   • TypeScript, typed        • TypeScript ToolSpec    │
│    decorators                schemas                    with JSON Schema       │
│  • Self-registering at     • Built-in + plugin-       • GlobalToolRegistry     │
│    import time               provided                   (built-in + plugin +   │
│  • Central registry        • Central executor in Pi     MCP)                   │
│    (registry.py)                                      • 5-layer filtering      │
│                                                                                │
│  Built-in tools (~40+):   Built-in tools:             Built-in tools (40+):   │
│  • bash, process mgmt     • exec, bash, process       • bash (+ 4,437-line    │
│  • read, write, edit,     • read, write, edit,          parser)               │
│    search                   apply_patch               • read_file, write_file,│
│  • memory (MEMORY.md/     • memory_search,              edit_file             │
│    USER.md CRUD)            memory_get                • glob_search,           │
│  • skills_list,           • browser (CDP)               grep_search           │
│    skill_view,            • web_search, x_search,     • WebFetch, WebSearch   │
│    skill_manage             web_fetch                 • Agent (sub-agents)     │
│  • honcho_* (profile/     • canvas (A2UI)             • Skill, TodoWrite      │
│    search/context/        • message (cross-channel)   • NotebookEdit           │
│    conclude)              • sessions_list/history/    • Sleep, Config          │
│  • sessions tools           send/spawn/yield          • + ~20 feature-gated   │
│  • browser (CDP)          • image, image_generate       tools (cron, monitor,  │
│  • web_search, scrape     • cron, gateway               browser, workflow,    │
│  • cron tools             • nodes (device control)      push notification,    │
│  • gateway tools          • agents_list                 etc.)                  │
│                                                                                │
│  Grouping:                Grouping:                   Filtering:               │
│  • Named toolsets         • Tool profiles (full,      • Blanket deny rules     │
│    (terminal, files,        coding, messaging,        • Simple mode (3 tools   │
│    memory, skills, web,     minimal)                    only)                  │
│    browser, honcho,       • Tool groups (runtime,     • Special-tool stripping │
│    cron, mcp)               fs, web, ui, sessions,    • REPL wrapping          │
│  • Platform presets         memory, messaging,        • Coordinator mode       │
│    (cli, telegram,          automation, nodes)          regains agent tools    │
│    discord)                                           • --allowedTools flag    │
│                                                                                │
│  Access control:          Access control:             Access control:          │
│  • Toolsets enabled/      • tools.allow / deny        • 3-tier permission      │
│    disabled               • tools.profile (base)        model per tool         │
│  • Per-platform presets   • tools.byProvider          • YOLO auto-classifier   │
│  • MCP tools dynamically    (per-LLM)                  (2-stage)              │
│    loaded                 • Per-agent tool overrides  • Interactive approval    │
│                           • Sub-agent tool policies     prompts                │
│                                                       • Always-allow/deny      │
│                                                         rules                  │
│                                                                                │
│  Extensions:              Extensions:                 Extensions:              │
│  • MCP tools (dynamic)    • Full plugin system        • Plugin system           │
│  • External skill dirs    • Lobster (typed workflows)   (manifest + hooks +    │
│                           • LLM Task (structured        lifecycle)             │
│                             output)                   • MCP (4 transports:     │
│                           • OpenProse (markdown          stdio, SDK, managed   │
│                             workflows)                   proxy, WebSocket)     │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Key Differences

| Aspect | Hermes | OpenClaw | Claude Code |
|--------|--------|----------|-------------|
| **Tool creation at runtime** | Agent can create skills (which contain tool-usage procedures) | Not supported | `skillify` skill captures workflows; `RUN_SKILL_GENERATOR` flag for auto-gen |
| **MCP support** | Yes (dynamically loaded MCP tools) | Via plugins | Yes (4 transports: stdio, SDK, managed proxy, WebSocket) |
| **Device control** | None | `nodes` tool — camera, voice, screen, location, SMS, contacts | None |
| **Canvas / UI generation** | None | `canvas` tool — push HTML/CSS/JS to visual workspace | None |
| **Execution modes** | Sequential or concurrent | Block streaming (tool output streams as generated) | Streaming (tools execute *during* response generation) |
| **Approval gates** | Configurable allowlists | Configurable allow/deny + elevated exec escape hatch | 3-tier permissions + two-stage YOLO auto-classifier |
| **Tool count** | ~40+ | ~30+ | 18 always-on + ~20 feature-gated = ~40+ |
| **Tool aliasing** | No | No | Yes (read → read_file, write → write_file, etc.) |

---

## 7  Skills System

All three use the [AgentSkills](https://agentskills.io/) standard (`SKILL.md`
format), making skills theoretically interchangeable. The differences lie in
how skills are discovered, loaded, and — crucially — created.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    Skills Lifecycle Comparison                                  │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐     │
│  │  1. DISCOVERY    │      │  1. RESOLUTION   │      │  1. DISCOVERY    │     │
│  │  skills_list()   │      │  Precedence:     │      │  /skills command  │     │
│  │  → compact list  │      │  workspace >     │      │  or Skill tool    │     │
│  │  (~3k tokens)    │      │  project >       │      │  resolves from:   │     │
│  │  Names + descs   │      │  personal >      │      │  • .claw/         │     │
│  │  only (Level 0)  │      │  managed >       │      │  • .codex/        │     │
│  └────────┬─────────┘      │  bundled >       │      │  • ~/.claw/       │     │
│           ▼                │  extraDirs >     │      │  • ~/.codex/      │     │
│  ┌──────────────────┐      │  plugin          │      │  • $CODEX_HOME/   │     │
│  │  2. LOADING      │      │                  │      │  + 15+ bundled    │     │
│  │  skill_view()    │      │  All skills      │      │    skills shipped │     │
│  │  → full SKILL.md │      │  injected as XML │      │    with the binary│     │
│  │  (Level 1)       │      │  into prompt at  │      └────────┬─────────┘     │
│  │  skill_view(path)│      │  session start.  │               ▼               │
│  │  → reference file│      └────────┬─────────┘      ┌──────────────────┐     │
│  │  (Level 2)       │               │                │  2. LOADING      │     │
│  └────────┬─────────┘               ▼                │  Skill tool reads│     │
│           ▼                ┌──────────────────┐      │  SKILL.md on     │     │
│  ┌──────────────────┐      │  2. EXECUTION   │      │  demand.         │     │
│  │  3. EXECUTION   │      │  Agent follows   │      │  Instructions    │     │
│  │  Agent follows   │      │  skill          │      │  returned as     │     │
│  │  skill           │      │  instructions.  │      │  context for LLM.│     │
│  │  instructions.   │      │                  │      └────────┬─────────┘     │
│  └────────┬─────────┘      │  Available as   │               ▼               │
│           ▼                │  /slash commands │      ┌──────────────────┐     │
│  ┌──────────────────┐      │  or NL.         │      │  3. EXECUTION   │     │
│  │  4. SELF-        │      └──────────────────┘      │  Agent follows   │     │
│  │  IMPROVEMENT     │                                │  skill           │     │
│  │  After complex   │      ┌──────────────────┐      │  instructions.   │     │
│  │  tasks:          │      │  ❌ NOT AVAILABLE│      └────────┬─────────┘     │
│  │  • skill_manage( │      │                  │               ▼               │
│  │    create)       │      │  OpenClaw does   │      ┌──────────────────┐     │
│  │  • skill_manage( │      │  not auto-create │      │  4. WORKFLOW     │     │
│  │    patch)        │      │  or auto-improve │      │  CAPTURE         │     │
│  │  • skill_manage( │      │  skills.         │      │  (nascent)       │     │
│  │    edit)         │      └──────────────────┘      │                  │     │
│  │                  │                                │  `skillify` skill│     │
│  │  Triggers:       │                                │  turns a session │     │
│  │  • 5+ tool calls │                                │  into a SKILL.md │     │
│  │  • Errors → fix  │                                │  (user-invoked,  │     │
│  │  • User correct  │                                │  not automatic)  │     │
│  │  • Novel workflow│                                │                  │     │
│  └──────────────────┘                                │  `remember` skill│     │
│                                                      │  curates memory  │     │
│                                                      │  entries.        │     │
│                                                      └──────────────────┘     │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **Format** | SKILL.md (AgentSkills) | SKILL.md (AgentSkills) | SKILL.md (AgentSkills) |
| **Agent-created skills** | Yes — `skill_manage` tool (create, patch, edit) | No | Partial — `skillify` bundled skill (user-invoked, not automatic) |
| **Progressive disclosure** | Yes — 3 levels (list → view → detail) | No — all skills injected at session start | Partial — Skill tool loads on demand; `/skills` lists |
| **Token cost** | ~3k tokens for the list; details loaded on demand | ~195 chars base + ~97 chars per skill; all upfront | On-demand loading; only loaded skill costs tokens |
| **Gating** | `metadata.hermes.fallback_for_toolsets`, `requires_toolsets` | `metadata.openclaw.requires` (bins, env, config, os) | Feature flags gate bundled skills |
| **Hub** | Skills Hub (multi-source: official, skills.sh, GitHub, ClawHub, LobeHub) | ClawHub (clawhub.com) | None (bundled only) |
| **Bundled skills** | — | Via plugins | 15+ (verify, debug, skillify, remember, simplify, batch, dream, hunter, etc.) |
| **Trust levels** | builtin > official > trusted > community (security scanning) | Not formalized | builtin > bundled > external |
| **Skill sharing standard** | agentskills.io | agentskills.io (same) | agentskills.io (same) |

**Key takeaway:** The formats are interchangeable. Hermes has the most
mature automatic skill creation; Claude Code has the beginnings of workflow
capture via `skillify` but requires manual invocation. OpenClaw has neither.

---

## 8  Memory & Context

This is the area of greatest divergence. Hermes has a 3-layer memory
architecture with active curation; Claude Code has an emerging auto-memory
system; OpenClaw relies on static file injection.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    Memory Architecture Comparison                              │
│                                                                                │
│  HERMES (3 layers)         OPENCLAW (file inject)    CLAUDE CODE              │
│                                                                                │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐     │
│  │  Layer 1: Working│      │  Prompt Files:   │      │  Config + Memory │     │
│  │  Memory          │      │  • AGENTS.md     │      │                  │     │
│  │                  │      │  • SOUL.md       │      │  CLAW.md         │     │
│  │  MEMORY.md       │      │  • TOOLS.md      │      │  (per-repo       │     │
│  │  (~800 tokens,   │      │  • IDENTITY.md   │      │   instructions,  │     │
│  │   bounded)       │      │  • USER.md       │      │   unbounded)     │     │
│  │                  │      │  • HEARTBEAT.md  │      │                  │     │
│  │  USER.md         │      │  • BOOTSTRAP.md  │      │  Memory files    │     │
│  │  (~500 tokens,   │      │                  │      │  (persistent     │     │
│  │   bounded)       │      │  All injected.   │      │   context, loaded│     │
│  │                  │      │  Not bounded.    │      │   via /memory)   │     │
│  │  Injected: frozen│      │  Not actively    │      │                  │     │
│  │  at session start│      │  curated.        │      │  extractMemories/│     │
│  │  Auto-consolidate│      │                  │      │  (auto-extract   │     │
│  │  at 80%.         │      │  Memory tools:   │      │   key facts,     │     │
│  └────────┬─────────┘      │  • memory_search │      │   feature-gated) │     │
│           │                │  • memory_get    │      │                  │     │
│           ▼                └──────────────────┘      │  teamMemorySync/ │     │
│  ┌──────────────────┐                                │  (shared team    │     │
│  │  Layer 2: Honcho │                                │   memory, gated) │     │
│  │  (Cross-Session  │      ┌──────────────────┐      │                  │     │
│  │   User Modeling) │      │  ❌ NO EQUIVALENT│      │  `remember` skill│     │
│  │                  │      │                  │      │  (reviews +      │     │
│  │  • Dual-peer     │      │  OpenClaw does   │      │   promotes across│     │
│  │    model         │      │  not have cross- │      │   layers, user-  │     │
│  │  • Dialectic     │      │  session user    │      │   invoked)       │     │
│  │    reasoning     │      │  modeling.       │      └────────┬─────────┘     │
│  │  • Auto-learned  │      └──────────────────┘               │               │
│  │  • Cloud or      │                                         ▼               │
│  │    self-host     │                                ┌──────────────────┐     │
│  └────────┬─────────┘                                │  Session Memory  │     │
│           │                                          │  (3rd compaction │     │
│           ▼                                          │   tier)          │     │
│  ┌──────────────────┐                                │                  │     │
│  │  Layer 3: Session│      ┌──────────────────┐      │  Zero-cost       │     │
│  │  Search          │      │  ❌ NO EQUIVALENT│      │  in-memory cache │     │
│  │  (Episodic)      │      │                  │      │  of key facts    │     │
│  │                  │      │  OpenClaw        │      │  extracted during│     │
│  │  • All sessions  │      │  sessions are    │      │  compaction.     │     │
│  │    in SQLite     │      │  not searchable  │      │                  │     │
│  │    with FTS5     │      │  across convos.  │      │  NOT cross-      │     │
│  │  • session_search│      └──────────────────┘      │  session.        │     │
│  │  • LLM summary   │                                └──────────────────┘     │
│  └──────────────────┘                                                          │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Memory Comparison Table

| Feature | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **Working memory** | MEMORY.md + USER.md (bounded, actively curated by agent) | AGENTS.md + SOUL.md + USER.md + TOOLS.md + IDENTITY.md (unbounded, human-curated) | CLAW.md + memory files (unbounded, human-curated; nascent auto-extraction) |
| **Cross-session memory** | Honcho (auto-learned user model) + session search (FTS5) | None (context resets per session) | None (sessions independent; `teamMemorySync` feature-gated) |
| **Active memory curation** | Agent adds/replaces/removes entries via `memory` tool | Agent does not modify context files | `remember` skill (user-invoked); `extractMemories` service (feature-gated) |
| **Memory nudges** | System prompt reminds agent to persist knowledge | Not present | Not present |
| **Capacity management** | Auto-consolidates at 80% capacity | No capacity management (unbounded files) | Three-tier compaction manages context window pressure |
| **Session search** | SQLite FTS5 — query all past sessions | Not available | Not available (sessions are independent JSON files) |
| **User modeling** | Honcho dialectic reasoning (learns from both user and AI messages) | Static USER.md file | No equivalent |
| **Context compression** | Mid-conversation compression when context grows too large | `/compact` slash command (manual) | Three-tier: micro (~256 tokens, no API), full (~4K, API-evaluated), session memory |
| **Token overhead** | ~1,300 tokens (fixed, bounded) | Varies (depends on file sizes, not bounded) | Varies; prompt caching reduces effective cost of static prefix |

---

## 9  Sandboxing & Execution

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    Sandbox Comparison                                           │
│                                                                                │
│  HERMES (6 backends)       OPENCLAW (3 backends)     CLAUDE CODE              │
│                                                                                │
│  ┌──────────┐              ┌──────────┐              ┌──────────────────┐     │
│  │  local   │ No isolation │  Docker  │ Container   │  Host execution   │     │
│  └──────────┘              └──────────┘ (default)    │  (default)        │     │
│  ┌──────────┐              ┌──────────┐              │                   │     │
│  │  docker  │ Container    │   SSH    │ Remote      │  4,437-line bash  │     │
│  └──────────┘              └──────────┘              │  parser for       │     │
│  ┌──────────┐              ┌──────────┐              │  command-injection│     │
│  │   ssh    │ Remote       │ OpenShell│ Managed     │  defense          │     │
│  └──────────┘              └──────────┘ + policies   │                   │     │
│  ┌──────────┐                                        │  3-tier permission│     │
│  │ daytona  │ Serverless                             │  model gating     │     │
│  └──────────┘                                        │                   │     │
│  ┌───────────┐                                       │  6 connection     │     │
│  │singularity│ HPC                                   │  modes (local/    │     │
│  └───────────┘                                       │  remote/ssh/      │     │
│  ┌──────────┐                                        │  teleport/direct/ │     │
│  │  modal   │ Serverless                             │  deep-link)       │     │
│  └──────────┘                                        └──────────────────┘     │
│                                                                                │
│  Configuration:            Configuration:             Configuration:           │
│  hermes config set         agents.defaults.sandbox    --permission-mode flag   │
│    terminal.backend        in openclaw.json           or CLAW_PERMISSION_MODE  │
│    <name>                                             env var                  │
│                            Scope options:                                      │
│                            • session / agent / shared                          │
│                                                                                │
│                            Mode options:                                       │
│                            • off / non-main / all                              │
│                                                                                │
│                            OpenShell workspace modes:                          │
│                            • mirror (sync local↔remote)                        │
│                            • remote (remote canonical)                         │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **Backends** | 6 (local, docker, ssh, daytona, singularity, modal) | 3 (docker, ssh, openshell) | Host execution (default); 6 connection modes for remote |
| **Serverless options** | Modal, Daytona | None (but OpenShell could be extended) | None |
| **HPC support** | Singularity | None | None |
| **OpenShell integration** | None | Native (first-class backend) | None |
| **Sandbox scope** | Per-session (implicit) | Configurable (session / agent / shared) | Per-tool (permission-level gating) |
| **Workspace modes** | N/A (terminal-based, no workspace sync) | mirror (sync each exec) / remote (seed once) | N/A (operates on local filesystem) |
| **Browser sandbox** | None | Dedicated browser sandbox container | None (WebBrowserTool feature-gated) |
| **Kernel isolation** | Depends on backend | Landlock + seccomp + network namespaces (via OpenShell) | None (application-level bash parsing) |
| **Network policy** | None | OpenShell policy engine (per-binary, per-endpoint) | None |
| **Bash security** | None | N/A | 4,437-line fail-closed bash parser (AST analysis, 15 blocked node types, `--` handling) |
| **Elevated exec** | N/A | `/elevated on` (escape hatch to host) | `--dangerously-skip-permissions` flag |

**Key takeaway:** Hermes has the most backend variety (especially for
research/HPC and serverless). OpenClaw's OpenShell provides the strongest
*kernel-level* isolation. Claude Code compensates for lack of sandbox
isolation with the most sophisticated *application-level* command-injection
defense (the bash parser). For NemoClaw Escapades, combine OpenClaw's
OpenShell pattern with Claude Code's bash parser approach.

---

## 10  Sub-Agents & Multi-Agent

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Sub-Agent Comparison                                   │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  Model: Isolated AIAgent   Model: Spawned sessions    Model: Agent tool +      │
│  sessions within a single  via Gateway WS protocol.   Coordinator mode.        │
│  process.                                                                      │
│                                                                                │
│  Spawning:                 Spawning:                  Spawning:                │
│  • sessions_send(target,   • sessions_spawn(task,     • Agent tool with:       │
│    message,                  model, thinking,           description, prompt,   │
│    reply_back=True)          tools, ...)                subagent_type, model   │
│  • Each sub-agent is a     • Non-blocking (runId)     • Returns output file +  │
│    separate AIAgent        • Announces when done        AgentOutput manifest   │
│    instance                                           • Coordinator mode for   │
│                                                         parallel orchestration │
│                                                                                │
│  Coordination:             Coordination:              Coordination:            │
│  • sessions_list           • /subagents               • Background: claude ps/ │
│    (discover)                list/kill/log/info          logs/attach/kill       │
│  • sessions_history        • Announce chain            • Coordinator: parallel  │
│    (fetch)                   (depth-2→1→0→user)         sub-agents + result    │
│  • sessions_send           • sessions_yield              synthesis             │
│    (message)                 (pause self, msg user)   • /fork (fork session    │
│  • REPLY_SKIP /                                         into sub-agent)       │
│    ANNOUNCE_SKIP                                      • UDS inbox (inter-agent │
│    (flow control)                                       messaging via Unix     │
│                                                         domain sockets)        │
│                                                                                │
│  Depth limits:             Depth limits:              Depth limits:            │
│  • No formal depth limit   • maxSpawnDepth: 1-5       • Not formally          │
│  • Shared iteration          (default 1)                documented; sub-agents │
│    budget across parent    • maxChildrenPerAgent:       can spawn further      │
│    + children                1-20                       agents                 │
│                            • maxConcurrent: global                             │
│                              cap                                               │
│                            • runTimeoutSeconds:                                │
│                              per-spawn                                         │
│                                                                                │
│  Multi-agent routing:      Multi-agent routing:       Multi-agent:             │
│  • Via session targeting   • agents.list[] (per-      • /agents command lists  │
│                              agent workspace,           configured agents      │
│                              sandbox, tools, model)   • Coordinator mode       │
│                            • Channel → agent routing    orchestrates parallel  │
│                            • Thread binding              sub-agents            │
│                              (Discord)                • SendMessage tool for   │
│                                                         inter-agent messaging  │
│                                                                                │
│  Isolation:                Isolation:                 Isolation:               │
│  • Same process, same      • Separate sessions, can   • Each sub-agent gets   │
│    permissions               have different sandbox     own context            │
│  • No credential             scope + tools + model    • ⚠️ Known gap: child   │
│    isolation                 per agent                  can widen parent       │
│  • Shared failure domain   • Thread-bound sessions      permissions via        │
│                                                         acceptEdits            │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Feature | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **Spawn mechanism** | `sessions_send` (in-process) | `sessions_spawn` (via Gateway RPC) | `Agent` tool (launches sub-process) |
| **Depth control** | No formal limit (shared budget) | `maxSpawnDepth` (1-5), `maxChildrenPerAgent` (1-20) | No formal limit (no budget sharing) |
| **Concurrency cap** | No global cap | `maxConcurrent` (default 8) | No global cap (coordinator handles parallelism) |
| **Per-spawn overrides** | No (all sub-agents share parent config) | Yes (model, thinking, tools per spawn) | Yes (model, subagent_type, name per agent) |
| **Thread binding** | No | Yes (Discord threads → persistent sessions) | No |
| **Isolation level** | Same process | Per-session sandbox possible | Per-agent context isolation (⚠️ permission widening gap) |
| **Coordination latency** | Zero (in-process) | Low (WS message passing) | Low (file-based output) |
| **Coordinator mode** | No | No | Yes — parallel sub-agent orchestration with result synthesis |
| **Inter-agent messaging** | Via sessions | Via Gateway | UDS inbox (Unix domain sockets, feature-gated) |

**Key takeaway:** OpenClaw has the most sophisticated multi-agent management
(depth limits, concurrency caps, per-spawn overrides). Claude Code's
Coordinator mode enables parallel sub-agent orchestration with synthesis,
and the UDS inbox provides inter-agent messaging. Hermes has the simplest
coordination but a shared budget model.

---

## 11  Cron & Scheduling

All three provide cron-style scheduling where jobs run in fresh agent sessions.

| Feature | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **Schedule formats** | Relative (30m), intervals (every 2h), cron syntax, ISO timestamps | Cron syntax, natural language (both support NL) | Cron syntax via `CronCreate/Delete/List` tools (feature-gated) |
| **Storage** | `~/.hermes/cron/jobs.json` | Managed by Gateway | `.claude/scheduled_tasks.json` (persistent) or in-memory (session-only) |
| **Output** | `~/.hermes/cron/output/{job_id}/{timestamp}.md` | Delivered to connected channel | Delivered within daemon process |
| **Tick mechanism** | Gateway scheduler (every 60s) | Gateway scheduler (periodic) | Daemon supervisor |
| **Skill attachment** | Yes (optionally inject skills per job) | Yes (skills matched at runtime) | Not documented |
| **Delivery targets** | Specific: origin, local, telegram:id, discord:id | Any connected channel | Local daemon |
| **Safety** | Cron sessions cannot create more cron jobs | No explicit restriction | One-shot tasks auto-delete; recurring auto-expire after 7 days |
| **Remote scheduling** | No | No | Yes — `RemoteTrigger` tool manages cloud-hosted agents (feature-gated) |
| **Management** | CLI (`hermes cron`) + `/cron` slash + NL | CLI (`openclaw cron`) + `/cron` slash + NL + agent tool | Feature-gated `CronCreate/Delete/List` tools + daemon supervisor |

Hermes and OpenClaw cron systems are production-ready. Claude Code's is
feature-gated behind `AGENT_TRIGGERS` but adds remote trigger support via
the Anthropic cloud — a pattern NemoClaw should watch.

---

## 12  Messaging & Channels

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                    Channel Coverage                                             │
│                                                                                │
│  HERMES (6 platforms)      OPENCLAW (25+ platforms)   CLAUDE CODE (0 channels) │
│                                                                                │
│  Built-in:                 Core:                      ┌──────────────────┐     │
│  ☑ Telegram (grammY)      ☑ WhatsApp (Baileys)        │  ❌ NO MESSAGING  │     │
│  ☑ Discord (discord.js)   ☑ Telegram (grammY)         │  CHANNELS         │     │
│  ☑ Slack                  ☑ Slack (Bolt)               │                  │     │
│  ☑ WhatsApp (Baileys)     ☑ Discord (discord.js)       │  Terminal REPL   │     │
│  ☑ Signal (signal-cli)    ☑ Signal (signal-cli)        │  only.           │     │
│  ☑ CLI TUI                ☑ BlueBubbles (iMessage)     │                  │     │
│                            ☑ Google Chat               │  IDE bridge      │     │
│  Also mentioned:           ☑ IRC                       │  (VS Code,       │     │
│  ☐ iMessage                ☑ WebChat (WS UI)           │   JetBrains)     │     │
│  ☐ IRC                                                │  is feature-gated│     │
│  ☐ Teams                  Plugin:                      │                  │     │
│  ☐ Matrix                 ☑ Teams, Matrix, Feishu,     │  /install-slack- │     │
│  ☐ Feishu                   LINE, Mattermost,          │  app exists but  │     │
│  ☐ LINE                     Nextcloud Talk, Nostr,     │  is internal.    │     │
│  ☐ Mattermost               Synology Chat, Tlon,      └──────────────────┘     │
│  ☐ Nextcloud Talk            Twitch, WeChat, Zalo,                             │
│  ☐ Nostr                     Voice Call, iMessage                              │
│  ☐ Twitch                                                                      │
│  ☐ WeChat                                                                      │
│  ☐ WebChat                                                                     │
│                                                                                │
│  Architecture:             Architecture:              Architecture:            │
│  • Platform adapters in    • Channel adapters in      • N/A (no channels)      │
│    gateway/platforms/        src/channels/ (core)     • IDE bridge uses         │
│  • Session routing per       + plugins                  bidirectional JSON-    │
│    platform + chat ID      • All channels run           RPC + JWT auth         │
│  • Cross-platform            simultaneously                                    │
│    mirroring               • Shared session router                             │
│  • DM pairing (code auth)  • DM pairing + allowlists                           │
└────────────────────────────────────────────────────────────────────────────────┘
```

OpenClaw has a 4:1 advantage in channel coverage over Hermes. Claude Code has
zero messaging channels — it is terminal-only. For NemoClaw Escapades,
Slack is the primary channel (both Hermes and OpenClaw support it), so
Claude Code's patterns are irrelevant for this dimension.

---

## 13  Security Model

| Layer | Hermes | OpenClaw | Claude Code |
|-------|--------|----------|-------------|
| **Auth** | DM pairing (code-based) | DM pairing + platform allowlists + gateway token | OAuth 2.0 + PKCE, JWT, macOS Keychain |
| **Tool gating** | Toolset enable/disable | Allow/deny lists, profiles, per-provider, per-agent | 3-tier permission model (read-only / workspace-write / danger-full-access) |
| **Command approval** | Configurable allowlists | Approval callbacks + elevated exec escape hatch | Two-stage YOLO auto-classifier (64-token fast + 4K-token thinking) + interactive prompts |
| **Sandbox isolation** | Depends on backend (docker = container, local = none) | Docker + OpenShell (Landlock + seccomp + namespaces) | None (host execution); 4,437-line bash parser as defense layer |
| **Network policy** | None (agent has full network access) | OpenShell policy engine (per-binary, per-endpoint) | WebFetch hostname safety check (bypassable via flag) |
| **Credential handling** | Config file / env vars | Config file / env vars + OpenShell credential injection | OAuth + Keychain + env vars; API key rotation |
| **Prompt injection defense** | Skills scanned for injection/exfiltration; memory scanned | Gateway treats all inbound DMs as untrusted input | NO_TOOLS sandwich pattern (instructions at start AND end of system prompt); `<system_reminder>` isolation; transcript stripping |
| **Bash command security** | None | N/A | Fail-closed AST parser: 15 blocked node types, POSIX `--` handling, path traversal prevention |
| **Sub-agent security** | Same permissions as parent | Per-agent sandbox + tool policies | ⚠️ Known gap: child can widen parent permissions via `acceptEdits` |
| **Path traversal defense** | None documented | N/A | `realpath` checks, `O_NOFOLLOW`, randomized temp roots with per-process nonces |

**Key takeaway:** OpenClaw has the strongest *infrastructure-level* security
(kernel isolation, network policy). Claude Code has the strongest
*application-level* security (bash parser, permission model, prompt injection
mitigations, path traversal defenses). Hermes relies most on
application-level trust. For NemoClaw Escapades, combine OpenClaw's kernel
isolation with Claude Code's application-level defense patterns.

---

## 14  Self-Learning Loop

This is where the three systems diverge most sharply.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Self-Learning Loop                                     │
│                                                                                │
│  HERMES                    OPENCLAW                   CLAUDE CODE              │
│                                                                                │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐     │
│  │  1. TASK INTAKE  │      │  1. TASK INTAKE  │      │  1. TASK INTAKE  │     │
│  └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────┘     │
│           ▼                         ▼                         ▼               │
│  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐     │
│  │  2. SKILL RECALL │      │  2. SKILLS       │      │  2. SKILL LOAD   │     │
│  │  Check           │      │  MATCHED         │      │  Skill tool loads│     │
│  │  skills_list()   │      │  From XML list   │      │  on demand.      │     │
│  │  Load if relevant│      │  in system prompt│      │  15+ bundled     │     │
│  └────────┬─────────┘      └────────┬─────────┘      │  skills available│     │
│           ▼                         ▼                └────────┬─────────┘     │
│  ┌──────────────────┐      ┌──────────────────┐               ▼               │
│  │  3. EXECUTION   │      │  3. EXECUTION   │      ┌──────────────────┐     │
│  │  Track what works│      │  Execute task   │      │  3. EXECUTION   │     │
│  └────────┬─────────┘      └────────┬─────────┘      │  Execute task   │     │
│           ▼                         ▼                └────────┬─────────┘     │
│  ┌──────────────────┐      ┌──────────────────┐               ▼               │
│  │  4. MEMORY       │      │  4. DONE        │      ┌──────────────────┐     │
│  │  PERSIST         │      │                  │      │  4. COMPACTION   │     │
│  │  Save discoveries│      │  No learning     │      │  Three-tier      │     │
│  │  to MEMORY.md/   │      │  step.           │      │  compaction      │     │
│  │  USER.md/Honcho  │      │  No memory       │      │  extracts session│     │
│  │                  │      │  update.         │      │  memory (key     │     │
│  │  Memory nudges   │      │  No skill        │      │  facts).         │     │
│  │  remind agent to │      │  creation.       │      │                  │     │
│  │  persist.        │      │                  │      │  extractMemories │     │
│  └────────┬─────────┘      │  Context resets  │      │  service (auto-  │     │
│           ▼                │  at next session.│      │  extract, gated) │     │
│  ┌──────────────────┐      └──────────────────┘      └────────┬─────────┘     │
│  │  5. SKILL        │                                         ▼               │
│  │  CREATION/UPDATE │                                ┌──────────────────┐     │
│  │  After 5+ tool   │                                │  5. OPTIONAL     │     │
│  │  calls:          │                                │  WORKFLOW CAPTURE│     │
│  │  • Create new    │                                │  (user-invoked)  │     │
│  │    skill         │                                │                  │     │
│  │  • Patch existing│                                │  `skillify` turns│     │
│  │  • Rewrite skill │                                │  session into    │     │
│  │                  │                                │  SKILL.md.       │     │
│  └────────┬─────────┘                                │                  │     │
│           ▼                                          │  `remember`      │     │
│  ┌──────────────────┐                                │  reviews + curates│     │
│  │  6. SESSION      │                                │  memory entries. │     │
│  │  ARCHIVE         │                                │                  │     │
│  │  All history →   │                                │  Both require    │     │
│  │  SQLite FTS5.    │                                │  manual invocation│     │
│  │  Searchable via  │                                │  (not automatic).│     │
│  │  session_search. │                                └──────────────────┘     │
│  └────────┬─────────┘                                                          │
│           │                                                                    │
│           └── Next similar task → skill is now available ──►                   │
│                                                                                │
│  HERMES HAS:               OPENCLAW LACKS:           CLAUDE CODE HAS:         │
│  ☑ Agent-created skills    ☒ Agent-created skills    ⚠ skillify (manual)      │
│  ☑ Active memory curation  ☒ Active memory curation  ⚠ remember (manual)      │
│  ☑ Memory nudges           ☒ Memory nudges           ☒ Memory nudges          │
│  ☑ Cross-session modeling  ☒ Cross-session modeling  ☒ Cross-session modeling  │
│  ☑ Session search          ☒ Session search          ☒ Session search          │
│  ☑ Self-reflection         ☒ Self-reflection         ☒ Self-reflection        │
│  ☑ RL infrastructure       ☒ RL infrastructure       ☒ RL infrastructure      │
│  —                         —                         ☑ extractMemories (gated)│
│  —                         —                         ☑ teamMemorySync (gated) │
└────────────────────────────────────────────────────────────────────────────────┘
```

**Hermes remains the primary reference for self-learning.** Claude Code is
building toward a similar vision (`skillify`, `remember`, `extractMemories`,
`teamMemorySync`) but these features are nascent and partially
feature-gated. OpenClaw provides none of these learning components.

The key difference: Hermes's self-learning is **automatic** (triggered after
5+ tool calls, errors, user corrections, novel workflows). Claude Code's is
**manual** (user must invoke `skillify` or `remember` skills explicitly).

---

## 15  Companion Apps & UI

| Surface | Hermes | OpenClaw | Claude Code |
|---------|--------|----------|-------------|
| **CLI** | Rich TUI (`cli.py`) | `openclaw` CLI with sub-commands | React + Ink REPL with markdown streaming, spinner verbs, line editor, tab completion |
| **macOS app** | None | Menu bar app (Swift): voice wake, WebChat, debug, remote gateway | None |
| **iOS app** | None | Node: Canvas, voice, camera, screen recording, Bonjour pairing | None |
| **Android app** | None | Node: chat, voice, Canvas, camera, screen, device commands | None |
| **WebChat** | None | Gateway-hosted static UI over WS | None |
| **Canvas** | None | A2UI visual workspace (push HTML/CSS/JS from agent) | None |
| **Editor integration** | ACP adapter (Cursor/VS Code JSON-RPC) | Via plugins | IDE bridge (VS Code, JetBrains) — bidirectional messaging, JWT auth (feature-gated) |
| **Batch/training UI** | `batch_runner.py` + `trajectory_compressor.py` | None | `batch` skill (research → decompose → distribute across worktree agents → verify) |
| **Daemon / always-on** | Gateway (production) | Gateway daemon (production) | Daemon mode (feature-gated) — supervisor + daemon-worker architecture |
| **Voice input** | None | Voice wake, push-to-talk | `/voice` command (feature-gated) |
| **Fun** | None | None | `/buddy` — Tamagotchi companion sprite (feature-gated) |

OpenClaw is a full product with native apps and visual surfaces. Claude Code
is the most polished terminal experience. Hermes is developer-focused with
an editor integration.

---

## 16  Ecosystem & Community

| Metric | Hermes | OpenClaw | Claude Code |
|--------|--------|----------|-------------|
| **GitHub stars** | 17k | 341k | N/A (proprietary); claw-code Rust port: 107k |
| **Primary language** | Python (92%) | TypeScript (89%) | TypeScript (Bun); Rust port available |
| **License** | MIT | MIT | Proprietary (leaked, not licensed for redistribution) |
| **Org** | Nous Research | OpenClaw Foundation | Anthropic |
| **Release cadence** | Frequent (v0.5.0 as of 2026-03-28) | Rapid (2026.3.28 latest) | v2.1.88 as of leak (2026-03-31) |
| **Plugin ecosystem** | MCP tools, external skill dirs | ClawHub, plugin packages, channel plugins | Plugins (manifest + hooks + lifecycle) + MCP; no public marketplace |
| **Migration path** | `hermes claw migrate` (imports from OpenClaw) | — (OpenClaw is the "upstream") | — |
| **NemoClaw integration** | Not integrated | Native (NemoClaw wraps OpenClaw) | Not integrated |
| **RL community** | Atropos, environments, batch runner | Not applicable | Not applicable |
| **Feature flags** | None | None | 90 GrowthBook flags (build-time dead code elimination) |
| **Codebase size** | — | — | 1,884 files; 6,552 exports; 99 classes; 1,308 types |

---

## 17  Feature Matrix

| Feature | Hermes | OpenClaw | Claude Code | Notes |
|---------|:------:|:--------:|:-----------:|-------|
| **Core Agent Loop** | ✅ | ✅ | ✅ | All implement prompt → LLM → tools → loop |
| **Multi-provider support** | ✅ | ✅ | ❌ | Claude Code is Anthropic-only |
| **Custom endpoint (`base_url`)** | ✅ | ✅ | ❌ | Both open-source agents compatible with inference hub |
| **Provider fallback** | ✅ | ✅ | ❌ | Claude Code has no provider diversity |
| **Context compression** | ✅ | ⚠️ | ✅ | Hermes automatic; OpenClaw manual; Claude Code three-tier |
| **Prompt caching** | ✅ | ❌ | ✅ | Hermes (provider-specific); Claude Code (Anthropic cache boundary + break detection) |
| **Skills (AgentSkills)** | ✅ | ✅ | ✅ | Same standard, interchangeable format |
| **Agent-created skills** | ✅ | ❌ | ⚠️ | Hermes automatic; Claude Code has `skillify` (manual) |
| **Progressive skill disclosure** | ✅ | ❌ | ⚠️ | Hermes 3-level; Claude Code on-demand via Skill tool |
| **Self-learning loop** | ✅ | ❌ | ⚠️ | Hermes full loop; Claude Code nascent (manual skillify + remember) |
| **Active memory curation** | ✅ | ❌ | ⚠️ | Hermes bounded MEMORY.md; Claude Code has gated extractMemories |
| **Cross-session user modeling** | ✅ | ❌ | ❌ | Hermes via Honcho; others have no equivalent |
| **Session search (episodic)** | ✅ | ❌ | ❌ | Hermes via SQLite FTS5 |
| **Memory nudges** | ✅ | ❌ | ❌ | Hermes prompt engineering |
| **Sub-agent delegation** | ✅ | ✅ | ✅ | OpenClaw richest controls; Claude Code has Coordinator mode |
| **Coordinator / orchestration** | ❌ | ❌ | ✅ | Claude Code only — parallel sub-agents with result synthesis |
| **Cron scheduling** | ✅ | ✅ | ⚠️ | Feature-gated in Claude Code |
| **Remote scheduling** | ❌ | ❌ | ⚠️ | Claude Code has RemoteTrigger (feature-gated) |
| **Messaging channels** | 6 | 25+ | 0 | Claude Code is terminal-only |
| **Sandbox backends** | 6 | 3 | 0 | Claude Code runs on host; others have container/remote options |
| **OpenShell integration** | ❌ | ✅ | ❌ | OpenClaw native |
| **Kernel-level isolation** | ❌ | ✅ | ❌ | Via OpenShell (Landlock + seccomp) |
| **Bash security parser** | ❌ | ❌ | ✅ | 4,437-line fail-closed AST parser |
| **Permission model** | ⚠️ | ✅ | ✅ | Claude Code's 3-tier + YOLO classifier is most sophisticated app-level model |
| **Network policy** | ❌ | ✅ | ❌ | Via OpenShell policy engine |
| **Native apps** | ❌ | ✅ | ❌ | macOS, iOS, Android |
| **Canvas / visual workspace** | ❌ | ✅ | ❌ | A2UI |
| **Device node model** | ❌ | ✅ | ❌ | Camera, voice, screen, location, SMS |
| **Plugin architecture** | ⚠️ | ✅ | ✅ | Hermes MCP-only; OpenClaw full plugins; Claude Code manifest+hooks+lifecycle |
| **Editor integration** | ✅ | ⚠️ | ✅ | Hermes ACP; Claude Code IDE bridge (feature-gated) |
| **Streaming tool execution** | ❌ | ❌ | ✅ | Tools execute during response generation |
| **Daemon mode** | ✅ | ✅ | ⚠️ | Feature-gated in Claude Code |
| **RL / training pipeline** | ✅ | ❌ | ❌ | Hermes: environments, trajectories, Atropos |
| **Migration tool** | ✅ | — | — | `hermes claw migrate` imports from OpenClaw |
| **Feature flag system** | ❌ | ❌ | ✅ | 90 flags with build-time dead code elimination |

---

## 18  Implications for NemoClaw Escapades

### Which to Lift, and When

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Lift Strategy                                          │
│                                                                                │
│  MILESTONE           PRIMARY REF       SECONDARY REF     TERTIARY REF         │
│                                                                                │
│  M1 — Foundation     OpenClaw          Claude Code        Hermes              │
│  (Slack + inference  • Gateway pattern  • Streaming        • Provider          │
│   + orchestrator)    • Channel adapter    generator loop     resolver          │
│                        pattern          • 3-tier              (base_url)       │
│                      • Session routing    compaction                           │
│                                         • Prompt cache                         │
│                                           boundary                             │
│                                         • Permission                           │
│                                           model                                │
│                                                                                │
│  M2 — Knowledge      Equal weight                         Claude Code         │
│  Management          • OpenClaw: context files            • CLAW.md pattern   │
│  (SecondBrain)         (AGENTS.md pattern)                • Memory files      │
│                      • Hermes: memory system              • extractMemories   │
│                        (for future active curation)         (inspiration)     │
│                                                                                │
│  M3 — Coding Agent   OpenClaw          Claude Code        Hermes              │
│  (OpenShell)         • OpenShell        • 4,437-line       • Terminal          │
│                        backend            bash parser        backend model     │
│                      • sandbox.scope:   • edit_file                            │
│                        session            (string-replace)                     │
│                      • mirror/remote    • /commit-push-pr                      │
│                        modes            • batch skill                          │
│                      • sessions_spawn   • Streaming tool                       │
│                                           execution                            │
│                                                                                │
│  M4 — Self-Learning  ★ HERMES ★        Claude Code        OpenClaw            │
│  Loop                • Skill auto-      • skillify          • Skills format   │
│                        creation           (workflow            (shared         │
│                      • MEMORY.md/          capture)            standard)       │
│                        USER.md          • remember                             │
│                      • Session search     (memory                              │
│                      • Honcho             curation)                            │
│                        integration      • extractMemories                      │
│                      • Memory nudges      (auto-extraction                     │
│                      • Self-reflection    inspiration)                         │
│                                                                                │
│  M5 — Review Agent   OpenClaw          Claude Code        Hermes              │
│                      • Orchestrator     • Permission        • Sub-agent       │
│                        pattern            escalation          sessions         │
│                        (maxSpawnDepth:    flow                                 │
│                         2)              • /diff command                         │
│                      • Announce chain   • Coordinator                           │
│                                           mode                                 │
│                                                                                │
│  M6 — Professional   Equal weight                         Claude Code         │
│  KB                  • OpenClaw: cron + Slack adapter      • MCP (4           │
│                      • Hermes: cron + skill-backed jobs      transports)      │
│                                                            • Plugin hooks     │
│                                                              (pre/post)       │
│                                                            • ConfigLoader     │
│                                                              multi-source     │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Component-Level Lift Recommendations

| Component | Lift From | Rationale |
|-----------|-----------|-----------|
| **Gateway / daemon pattern** | OpenClaw | Typed WS protocol, session routing, plugin loader — most production-ready |
| **Channel adapters** | OpenClaw | Broadest coverage, cleanest interface pattern |
| **Provider resolver** | Hermes | Most flexible (`base_url` for any endpoint), prompt caching, budget tracking |
| **Streaming agent loop** | Claude Code | Async generator architecture; tools execute during response for lower latency |
| **Three-tier compaction** | Claude Code | Micro (no-API) + full (API-evaluated) + session memory — most sophisticated |
| **Prompt cache boundary** | Claude Code | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` pattern for cost optimization |
| **Permission model** | Claude Code | 3-tier model + two-stage YOLO auto-classifier — most sophisticated app-level security |
| **Bash security parser** | Claude Code | 4,437-line fail-closed AST parser — essential for host execution safety |
| **Skills format** | All (same standard) | AgentSkills SKILL.md — interchangeable |
| **Skill auto-creation** | Hermes | `skill_manage` tool — OpenClaw doesn't have this; Claude Code's `skillify` is manual |
| **Memory system** | Hermes | 3-layer bounded memory with active curation — most mature |
| **User modeling** | Hermes | Honcho — others have nothing comparable |
| **Session search** | Hermes | SQLite FTS5 — others have no cross-session search |
| **Self-learning loop** | Hermes | Only Hermes has automatic self-learning |
| **Sandbox backend** | OpenClaw | OpenShell-native integration (mirror/remote modes) |
| **Kernel-level security** | OpenClaw | Landlock + seccomp + namespaces via OpenShell |
| **Sub-agent management** | OpenClaw + Claude Code | OpenClaw's depth limits + concurrency caps; Claude Code's Coordinator mode for parallel orchestration |
| **Context compression** | Hermes + Claude Code | Hermes for mid-convo compression; Claude Code for three-tier architecture |
| **Prompt caching** | Claude Code | Cache boundary marker + break detection for cost optimization |
| **Plugin system** | Claude Code + OpenClaw | Claude Code's hook lifecycle; OpenClaw's plugin registry |
| **Model behavioral contract** | Claude Code | 21 invariants + repair mechanisms — essential harness engineering |

### The Three-Way Hybrid Strategy

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

5. **Adopt Claude Code's harness engineering** for production quality —
   streaming generator architecture, three-tier compaction, prompt cache
   boundary, 3-tier permission model with YOLO auto-classifier, bash
   security parser, and model behavioral contract with repair mechanisms.

6. **Watch Claude Code's evolving features** — `extractMemories`,
   `teamMemorySync`, Coordinator mode, remote triggers, and daemon mode
   represent Anthropic's roadmap for always-on intelligent agents. As these
   mature (via the claw-code open-source port), they may become additional
   reference points.

This gives you OpenClaw's product maturity and security posture, Hermes's
self-improving intelligence, and Claude Code's engineering rigor — exactly
the combination the design document calls for.

---

### Sources

- [Hermes Agent Deep Dive](hermes_deep_dive.md)
- [OpenClaw Deep Dive](openclaw_deep_dive.md)
- [Claude Code Deep Dive](claude_code_deep_dive.md)
- [NemoClaw Escapades Design Document](../design.md)
- [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)
- [openclaw/openclaw](https://github.com/openclaw/openclaw)
- [Claude Code Analysis](https://github.com/thtskaran/claude-code-analysis)
- [claw-code (Rust port)](https://github.com/instructkr/claw-code)
- [AgentSkills Standard](https://agentskills.io/)
