# OpenShell — Deep Dive

> **Source:** [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell)
> (Apache 2.0 license, alpha since March 16, 2026)
>
> **Official docs:** [docs.nvidia.com/openshell](https://docs.nvidia.com/openshell/latest/)
>
> **Official blog:** [Run Autonomous, Self-Evolving Agents More Safely with NVIDIA OpenShell](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/)
>
> **Last reviewed:** 2026-03-29

---

## Table of Contents

1. [Overview](#1--overview)
   - [Five Key Advantages](#five-key-advantages)
2. [The Problem OpenShell Solves](#2--the-problem-openshell-solves)
3. [High-Level Architecture](#3--high-level-architecture)
4. [Core Components](#4--core-components)
5. [Request Flow](#5--request-flow)
6. [Sandbox System](#6--sandbox-system)
7. [Policy Engine](#7--policy-engine)
8. [Privacy Router (Inference Routing)](#8--privacy-router-inference-routing)
9. [Gateway](#9--gateway)
10. [Remote Hosting & Multi-Machine Deployment](#10--remote-hosting--multi-machine-deployment)
11. [CLI Reference](#11--cli-reference)
12. [Community Sandboxes & Agent Support](#12--community-sandboxes--agent-support)
13. [IDE Integration](#13--ide-integration)
14. [Setup & Installation](#14--setup--installation)
15. [Comparison with Hermes Terminal Backends](#15--comparison-with-hermes-terminal-backends)
16. [Answers to Design Doc Questions](#16--answers-to-design-doc-questions)
17. [What to Lift for NemoClaw Escapades](#17--what-to-lift-for-nemoclaw-escapades)

---

## 1  Overview

OpenShell is NVIDIA's open-source secure runtime environment for autonomous AI
agents. Part of the NVIDIA Agent Toolkit, it sits **between the agent and the
infrastructure**, governing how the agent executes, what it can see and do, and
where inference goes.

The core insight: **out-of-process policy enforcement**. Instead of relying on
behavioral prompts or system-level instructions that the agent could override,
OpenShell enforces constraints on the *environment* the agent runs in — the
agent cannot bypass them even if compromised. This is the "browser tab model"
applied to agents: sessions are isolated, and permissions are verified by the
runtime before any action executes.

### What OpenShell Is and Is Not

```
┌──────────────────────────────────────┬──────────────────────────────────────┐
│  OpenShell IS                         │  OpenShell IS NOT                   │
├──────────────────────────────────────┼──────────────────────────────────────┤
│  A secure runtime for AI agents      │  An AI agent itself                  │
│  A policy enforcement layer          │  An inference engine                 │
│  A sandbox creation/management tool  │  A model training platform           │
│  Agent-agnostic (any agent works)    │  Tied to a single agent framework    │
│  A privacy-aware inference router    │  A replacement for Docker            │
│  An audit trail system               │  A monitoring/observability stack    │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### Status

Alpha stage — "single-player mode" for individual developers. Multi-tenant
enterprise deployments planned for future versions.

### Five Key Advantages

1. **Out-of-process policy enforcement** — Guardrails live *outside* the agent
   process, not inside it. Even a compromised agent cannot bypass them. This
   solves the "agent security trilemma": getting safety, capability, and
   autonomy simultaneously instead of picking two.

2. **Defense-in-depth sandbox isolation** — Three enforcement layers stack on
   top of each other: application-level (proxy intercepts all egress),
   infrastructure-level (Docker containers, network namespaces, unprivileged
   user), and kernel-level (Landlock LSM for filesystem, seccomp for syscalls).
   An agent would have to defeat all three.

3. **Privacy-aware inference routing** — The Privacy Router (`inference.local`)
   strips any credentials the agent sends, injects the real backend credentials,
   and routes requests based on operator-defined cost/privacy policy. The agent
   never sees API keys and doesn't even know which model it's talking to. Models
   can be switched at runtime without restarting the sandbox.

4. **Granular, hot-reloadable policy engine** — Policies are defined per-sandbox
   with per-binary, per-endpoint, and per-HTTP-method/path granularity. Network
   policies can be updated on a running sandbox without rebuilding or restarting.
   Every allow/deny decision is logged for a full audit trail.

5. **Agent-agnostic with flexible deployment topology** — Works with any agent
   (Claude Code, OpenClaw, Codex, custom code). The same CLI, policies, and
   sandbox definitions work identically across five deployment modes: local
   Docker, remote SSH, cloud reverse proxy, Brev managed GPU, and DGX Spark —
   just switch the active gateway with `openshell gateway select`.

---

## 2  The Problem OpenShell Solves

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    The Agent Security Trilemma                           │
│                                                                          │
│  Long-running agents need three things simultaneously:                   │
│                                                                          │
│                         SAFETY                                           │
│                        /      \                                          │
│                       /        \                                         │
│                      /          \                                        │
│            Traditional    ←PICK 2→    Traditional                        │
│            sandbox           ↑        agent + human                      │
│            (safe +           │        oversight                          │
│             capable,         │        (safe + autonomous,                │
│             not              │         not capable)                      │
│             autonomous)      │                                           │
│                              │                                           │
│                   ┌─────────────────────┐                                │
│                   │  OpenShell          │                                │
│                   │  (all three)        │                                │
│                   └─────────────────────┘                                │
│                              │                                           │
│                     CAPABILITY ──── AUTONOMY                             │
│                                                                          │
│  Without OpenShell:                                                      │
│  • Safe + autonomous but no tools → agent can't finish the job           │
│  • Capable + safe but gated on approvals → you're babysitting it         │
│  • Capable + autonomous with full access → guardrails live inside        │
│    the same process they guard (critical failure mode)                   │
│                                                                          │
│  With OpenShell:                                                         │
│  • Guardrails live OUTSIDE the agent process                             │
│  • Agent gets tools + autonomy within defined boundaries                 │
│  • Operator retains control without constant intervention                │
└──────────────────────────────────────────────────────────────────────────┘
```

### Why Existing Approaches Fail for Long-Running Agents

| Threat | Without OpenShell | With OpenShell |
|--------|-------------------|----------------|
| Prompt injection → credential leak | Agent polices itself | Credentials injected by runtime, never visible to agent |
| Third-party skill installs unreviewed binary | Agent decides to trust it | Policy engine blocks unreviewed binaries |
| Subagent inherits parent permissions | No isolation between subagents | Each sandbox has independent policy |
| Agent rewrites its own tooling | No external check | Filesystem restrictions prevent modification of system paths |
| 6+ hours of accumulated context with API access | Growing attack surface | Every API call verified by policy engine |

---

## 3  High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       OpenShell Architecture                             │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    HOST / CLOUD MACHINE                             │ │
│  │                                                                     │ │
│  │  ┌───────────────────────┐                                          │ │
│  │  │  OpenShell CLI        │  User / operator interface               │ │
│  │  │  (Python, via pip/uv) │                                          │ │
│  │  └───────────┬───────────┘                                          │ │
│  │              │                                                      │ │
│  │              ▼                                                      │ │
│  │  ┌──────────────────────────────────────────────────────────────┐   │ │
│  │  │  GATEWAY  (control-plane API)                                │   │ │
│  │  │                                                              │   │ │
│  │  │  • Coordinates sandbox lifecycle and state                   │   │ │
│  │  │  • Authentication boundary                                   │   │ │
│  │  │  • Brokers requests across the platform                      │   │ │
│  │  │  • Runs inside Docker on local or remote host                │   │ │
│  │  └───────────────┬──────────────────────────────────────────────┘   │ │
│  │                   │                                                 │ │
│  │        ┌──────────┴──────────┐                                      │ │
│  │        ▼                     ▼                                      │ │
│  │  ┌────────────────────┐  ┌────────────────────┐                     │ │
│  │  │  SANDBOX A         │  │  SANDBOX B         │  (Docker containers)│ │
│  │  │                    │  │                    │                     │ │
│  │  │  ┌───────────────┐ │  │  ┌──────────────┐  │                     │ │
│  │  │  │  Agent        │ │  │  │  Agent       │  │                     │ │
│  │  │  │  (Claude Code,│ │  │  │  (OpenClaw,  │  │                     │ │
│  │  │  │   OpenClaw,   │ │  │  │   Codex,     │  │                     │ │
│  │  │  │   Codex, ...) │ │  │  │   custom)    │  │                     │ │
│  │  │  └──────┬────────┘ │  │  └──────┬───────┘  │                     │ │
│  │  │         │          │  │         │          │                     │ │
│  │  │         ▼          │  │         ▼          │                     │ │
│  │  │  ┌──────────────┐  │  │  ┌──────────────┐  │                     │ │
│  │  │  │  PROXY       │  │  │  │  PROXY       │  │                     │ │
│  │  │  │  (intercepts │  │  │  │  (intercepts │  │                     │ │
│  │  │  │   all egress)│  │  │  │   all egress)│  │                     │ │
│  │  │  └──────┬───────┘  │  │  └──────┬───────┘  │                     │ │
│  │  │         │          │  │         │          │                     │ │
│  │  │         ▼          │  │         ▼          │                     │ │
│  │  │  ┌──────────────┐  │  │  ┌──────────────┐  │                     │ │
│  │  │  │ POLICY ENGINE│  │  │  │ POLICY ENGINE│  │                     │ │
│  │  │  │ (per-sandbox)│  │  │  │ (per-sandbox)│  │                     │ │
│  │  │  └──────────────┘  │  │  └──────────────┘  │                     │ │
│  │  │         │          │  │         │          │                     │ │
│  │  │         ▼          │  │         ▼          │                     │ │
│  │  │  ┌──────────────┐  │  │  ┌──────────────┐  │                     │ │
│  │  │  │PRIVACY ROUTER│  │  │  │PRIVACY ROUTER│  │                     │ │
│  │  │  │(inference    │  │  │  │(inference    │  │                     │ │
│  │  │  │ routing)     │  │  │  │ routing)     │  │                     │ │
│  │  │  └──────────────┘  │  │  └──────────────┘  │                     │ │
│  │  │                    │  │                    │                     │ │
│  │  │  Kernel isolation: │  │  Kernel isolation: │                     │ │
│  │  │  Landlock + seccomp│  │  Landlock + seccomp│                     │ │
│  │  │  + network NS      │  │  + network NS      │                     │ │
│  │  └────────────────────┘  └────────────────────┘                     │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  External Services                                                 │  │
│  │  ┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────────┐   │  │
│  │  │ build.nvidia│ │ Anthropic    │ │ GitHub /     │ │ PyPI,     │   │  │
│  │  │ .com (NIM)  │ │ (Claude)     │ │ GitLab       │ │ npm, etc. │   │  │
│  │  └─────────────┘ └──────────────┘ └──────────────┘ └───────────┘   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Three-Level Hierarchy: Gateway, Orchestrator, Sub-Agents

The diagram above shows "Agent" as a generic box inside each sandbox.
In practice, **not all agent sandboxes are equal.** A real deployment has
a clear hierarchy of intelligence, and it's critical to understand which
layer does the thinking.

```
┌───────────────────────────────────────────────────────────────────────┐
│            Where Intelligence Lives in an OpenShell Deployment        │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  GATEWAY  (no intelligence — pure infrastructure)               │  │
│  │                                                                 │  │
│  │  Manages sandbox lifecycle, credentials, policies, and          │  │
│  │  inference routing config. Does not reason, plan, or decide     │  │
│  │  anything. Cannot run skills, tools, or workflows. Think of it  │  │
│  │  as the building's property manager — it keeps the lights on    │  │
│  │  but doesn't live in any of the apartments.                     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │ manages                                │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  ORCHESTRATOR SANDBOX  (the brain — always-on super-agent)      │  │
│  │                                                                 │  │
│  │  This is where nearly ALL intelligence and reasoning resides.   │  │
│  │  The orchestrator is itself an agent — the "main brain" — that  │  │
│  │  runs persistently in its own sandbox. It owns:                 │  │
│  │                                                                 │  │
│  │  • The agentic loop  (prompt → LLM → tools → loop)              │  │
│  │  • Skills system     (SKILL.md files, auto-creation, recall)    │  │
│  │  • Memory system     (MEMORY.md, USER.md, Honcho, session       │  │
│  │                       search — all three layers)                │  │
│  │  • Self-learning loop (evaluate outcomes, update skills/memory) │  │
│  │  • Cron scheduler    (background tasks, periodic checks)        │  │
│  │  • Messaging         (Slack connector, receives tasks from user)│  │
│  │  • Sub-agent mgmt    (decides when to delegate, spawns workers, │  │
│  │                       collects results, manages budgets)        │  │
│  │  • Workflow logic    (decides WHAT to do and HOW to do it)      │  │
│  │                                                                 │  │
│  │  The orchestrator is the only sandbox that has the full         │  │
│  │  picture. It's the manager who understands the project,         │  │
│  │  remembers past work, and decides what to delegate.             │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │ spawns & manages                       │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  SUB-AGENT SANDBOXES  (the hands — ephemeral, narrow-scope)     │  │
│  │                                                                 │  │
│  │  Leaf workers that execute specific tasks on behalf of the      │  │
│  │  orchestrator. They do NOT have the full picture. Each one:     │  │
│  │                                                                 │  │
│  │  • Receives a scoped task from the orchestrator                 │  │
│  │  • Runs in its own isolated sandbox (own policy, own creds)     │  │
│  │  • Has a narrow toolset (e.g. coding agent gets fs + terminal)  │  │
│  │  • Produces an output (patch, review, summary)                  │  │
│  │  • Is destroyed after completion                                │  │
│  │                                                                 │  │
│  │  Examples:                                                      │  │
│  │  ┌────────────┐  ┌──────────────┐  ┌──────────────────────┐     │  │
│  │  │ Coding     │  │ Review       │  │ Research             │     │  │
│  │  │ Agent      │  │ Agent        │  │ Agent                │     │  │
│  │  │ (Claude    │  │ (reads diff, │  │ (web search,         │     │  │
│  │  │  Code)     │  │  gives       │  │  SecondBrain)        │     │  │
│  │  │            │  │  feedback)   │  │                      │     │  │
│  │  │ Input:     │  │ Input:       │  │ Input:               │     │  │
│  │  │  task +    │  │  diff +      │  │  research query      │     │  │
│  │  │  source    │  │  context     │  │                      │     │  │
│  │  │ Output:    │  │ Output:      │  │ Output:              │     │  │
│  │  │  PR/patch  │  │  comments    │  │  summary + citations │     │  │
│  │  └────────────┘  └──────────────┘  └──────────────────────┘     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  KEY POINT:  OpenShell itself doesn't know or care about this         │
│  hierarchy. To the gateway, the orchestrator sandbox and a coding     │
│  sub-agent sandbox look identical — both are just Docker containers   │
│  with policies. The hierarchy is an APPLICATION-LEVEL concern,        │
│  defined by how YOU wire the agent code, not by OpenShell.            │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 4  Core Components

### Component Overview

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Four Core Components                               │
│                                                                       │
│  ┌──────────────────────┐  ┌──────────────────────────────────────┐   │
│  │  1. GATEWAY          │  │  2. SANDBOX                          │   │
│  │                      │  │                                      │   │
│  │  Role: Control plane │  │  Role: Isolated execution            │   │
│  │                      │  │                                      │   │
│  │  • Lifecycle mgmt    │  │  • Docker container                  │   │
│  │  • Auth boundary     │  │  • Container supervision             │   │
│  │  • Request brokering │  │  • Policy-enforced egress            │   │
│  │  • State coordination│  │  • Kernel isolation (Landlock,       │   │
│  │                      │  │    seccomp, network NS)              │   │
│  └──────────────────────┘  └──────────────────────────────────────┘   │
│                                                                       │
│  ┌────────────────────────┐  ┌──────────────────────────────────────┐ │
│  │  3. POLICY ENGINE      │  │  4. PRIVACY ROUTER                   │ │
│  │                        │  │                                      │ │
│  │  Role: Governance      │  │  Role: Inference routing             │ │
│  │                        │  │                                      │ │
│  │  • Filesystem rules    │  │  • Keeps sensitive context on-device │ │
│  │  • Network rules       │  │  • Routes based on cost + privacy    │ │
│  │  • Process rules       │  │    policy                            │ │
│  │  • Defense in depth:   │  │  • Model-agnostic                    │ │
│  │    app → infra → kernel│  │  • Strips sandbox credentials,       │ │
│  │  • Full audit trail    │  │    injects backend credentials       │ │
│  └────────────────────────┘  └──────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

### Defense-in-Depth Enforcement

```
┌───────────────────────────────────────────────────────────────┐
│              Three Enforcement Layers                         │
│                                                               │
│  Layer 1: APPLICATION                                         │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  • Proxy intercepts every outbound connection           │  │
│  │  • Identifies calling binary                            │  │
│  │  • Queries policy engine (destination + binary)         │  │
│  │  • For REST with TLS terminate: inspects HTTP method,   │  │
│  │    path, headers                                        │  │
│  │  • All decisions logged (allow/deny + metadata)         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                              │                                │
│                              ▼                                │
│  Layer 2: INFRASTRUCTURE                                      │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  • Docker container isolation                           │  │
│  │  • Network namespace separation                         │  │
│  │  • Filesystem mount restrictions                        │  │
│  │  • Unprivileged user (sandbox:sandbox, no root)         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                              │                                │
│                              ▼                                │
│  Layer 3: KERNEL                                              │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  • Landlock LSM — kernel-enforced filesystem access     │  │
│  │    (read_only, read_write lists; everything else denied)│  │
│  │  • seccomp — blocks dangerous system calls              │  │
│  │  • Agent cannot escalate or bypass kernel-level controls│  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

---

## 5  Request Flow

Every outbound connection from agent code follows the same decision path:

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Outbound Request Flow                             │
│                                                                      │
│  ┌──────────────┐                                                    │
│  │  Agent opens │  API call, package install, git clone, etc.        │
│  │  outbound    │                                                    │
│  │  connection  │                                                    │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  Proxy       │  Intercepts connection, identifies calling binary  │
│  │  intercepts  │                                                    │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ├──────────────── Is target inference.local? ──── YES ───┐   │
│  │              │                                                    │
│         │ NO                                                      ▼  │
│         │                                              ┌─────────────┐
│         ▼                                              │ MANAGED     │
│  ┌───────────────┐                                      │ INFERENCE  │
│  │  Policy engine│                                      │            │
│  │  check        │                                      │ • Strip    │
│  │               │                                      │   sandbox  │
│  │  Match dest + │                                      │   creds    │
│  │  port + binary│                                      │ • Inject   │
│  │  against rules│                                      │   backend  │
│  └──────┬────────┘                                      │   creds    │
│         │                                              │ • Forward to│
│    ┌────┴────┐                                         │   model     │
│    │         │                                         │   endpoint  │
│ MATCH    NO MATCH                                      └─────────────┘
│         │                                              │             │
│    ▼         ▼                                                       │
│ ┌────────┐ ┌──────────┐                                              │
│ │ ALLOW  │ │  DENY    │  For REST + TLS terminate:                   │
│ │        │ │          │  also check HTTP method + path rules         │
│ │ Traffic│ │ Block +  │                                              │
│ │ flows  │ │ log +    │                                              │
│ │ to ext │ │ surface  │                                              │
│ │ service│ │ in TUI   │                                              │
│ └────────┘ └──────────┘                                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6  Sandbox System

### Sandbox Lifecycle

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Sandbox Lifecycle                                  │
│                                                                       │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│  │  CREATE  │───▶│ RUNNING  │───▶│  MONITOR │───▶│  DELETE  │         │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘         │
│                                                                       │
│  CREATE                                                               │
│  $ openshell sandbox create -- claude                                 │
│  $ openshell sandbox create --from openclaw                           │
│  $ openshell sandbox create --from ./my-dir                           │
│  $ openshell sandbox create --from registry.io/image:tag              │
│  $ openshell sandbox create --gpu -- claude    # with GPU             │
│  $ openshell sandbox create --policy ./p.yaml  # custom policy        │
│  $ openshell sandbox create --forward 8000     # port forwarding      │
│  $ openshell sandbox create --editor vscode    # IDE integration      │
│                                                                       │
│  CONNECT                                                              │
│  $ openshell sandbox connect my-sandbox        # SSH into sandbox     │
│  $ openshell sandbox connect my-sandbox --editor cursor               │
│                                                                       │
│  MONITOR                                                              │
│  $ openshell sandbox list                      # list all             │
│  $ openshell sandbox get my-sandbox            # detailed info        │
│  $ openshell logs my-sandbox --tail            # stream logs          │
│  $ openshell term                              # live TUI dashboard   │
│                                                                       │
│  TRANSFER                                                             │
│  $ openshell sandbox upload my-sandbox ./src /sandbox/src             │
│  $ openshell sandbox download my-sandbox /sandbox/out ./local         │
│                                                                       │
│  DELETE                                                               │
│  $ openshell sandbox delete my-sandbox         # destroys everything  │
│                                                                       │
│  PORT FORWARDING                                                      │
│  $ openshell forward start 8000 my-sandbox     # foreground           │
│  $ openshell forward start 8000 my-sandbox -d  # background           │
│  $ openshell forward list                                             │
│  $ openshell forward stop 8000 my-sandbox                             │
└───────────────────────────────────────────────────────────────────────┘
```

### What a Sandbox Provides

| Feature | Details |
|---------|---------|
| **Container isolation** | Docker-based, isolated from host and other sandboxes |
| **Filesystem control** | Landlock LSM: explicit read_only and read_write lists; unlisted paths inaccessible |
| **Network control** | All egress via proxy → policy engine; deny by default |
| **Process control** | Runs as unprivileged user (sandbox:sandbox); seccomp filters block dangerous syscalls |
| **Inference routing** | Calls to `inference.local` intercepted and routed to configured provider |
| **Credential management** | Credentials injected by runtime, never exposed to agent |
| **Live policy updates** | Network policies hot-reloadable without restart |
| **Port forwarding** | Forward host ports into sandbox (see [security note](#port-forwarding-security-note)) |
| **File transfer** | Upload/download files to/from sandbox |
| **IDE integration** | Direct VS Code / Cursor access via SSH |
| **Audit trail** | Every allow/deny decision logged |

---

## 7  Policy Engine

### Policy Structure

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Policy YAML Schema                                 │
│                                                                       │
│  version: 1                                                           │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  STATIC SECTIONS (locked at sandbox creation)                   │  │
│  │                                                                 │  │
│  │  filesystem_policy:         Filesystem access control           │  │
│  │    read_only:                                                   │  │
│  │      - /usr                                                     │  │
│  │      - /lib                                                     │  │
│  │      - /etc                                                     │  │
│  │    read_write:                                                  │  │
│  │      - /sandbox                                                 │  │
│  │      - /tmp                                                     │  │
│  │                                                                 │  │
│  │  landlock:                  Kernel-level enforcement config     │  │
│  │    compatibility: best_effort                                   │  │
│  │                                                                 │  │
│  │  process:                   Agent process identity              │  │
│  │    run_as_user: sandbox                                         │  │
│  │    run_as_group: sandbox                                        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  DYNAMIC SECTIONS (hot-reloadable on running sandbox)           │  │
│  │                                                                 │  │
│  │  network_policies:                                              │  │
│  │    my_api:                                                      │  │
│  │      name: my-api                                               │  │
│  │      endpoints:                                                 │  │
│  │        - host: api.example.com                                  │  │
│  │          port: 443                                              │  │
│  │          protocol: rest       # enables HTTP inspection         │  │
│  │          tls: terminate       # decrypts for rule checking      │  │
│  │          enforcement: enforce                                   │  │
│  │          access: full                                           │  │
│  │          rules:               # optional per-method/path rules  │  │
│  │            - allow:                                             │  │
│  │                method: GET                                      │  │
│  │                path: "/**"                                      │  │
│  │      binaries:               # which binaries can use this      │  │
│  │        - path: /usr/bin/curl                                    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### Policy Granularity Levels

```
┌───────────────────────────────────────────────────────────────┐
│                    Policy Granularity                         │
│                                                               │
│  Level 1: HOST-LEVEL                                          │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  endpoints:                                             │  │
│  │    - host: pypi.org                                     │  │
│  │      port: 443                                          │  │
│  │  binaries:                                              │  │
│  │    - path: /usr/bin/pip                                 │  │
│  │                                                         │  │
│  │  → Allow pip to reach pypi.org (TCP passthrough)        │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  Level 2: METHOD + PATH                                       │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  endpoints:                                             │  │
│  │    - host: api.github.com                               │  │
│  │      port: 443                                          │  │
│  │      protocol: rest                                     │  │
│  │      tls: terminate                                     │  │
│  │      rules:                                             │  │
│  │        - allow:                                         │  │
│  │            method: GET                                  │  │
│  │            path: "/**"                                  │  │
│  │        - allow:                                         │  │
│  │            method: "*"                                  │  │
│  │            path: "/repos/org/repo/**"                   │  │
│  │  binaries:                                              │  │
│  │    - path: /usr/local/bin/claude                        │  │
│  │    - path: /usr/bin/gh                                  │  │
│  │                                                         │  │
│  │  → Claude and gh can read all repos but write only to   │  │
│  │    org/repo (HTTP inspection enabled)                   │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

### Policy Iteration Workflow

```
┌──────────┐    ┌────────────┐    ┌──────────┐    ┌────────────┐
│ 1. CREATE│───▶│ 2. MONITOR │───▶│ 3. PULL  │───▶│ 4. EDIT    │
│ sandbox  │    │ for denials│    │ current  │    │ YAML       │
│ + policy │    │ in logs    │    │ policy   │    │            │
└──────────┘    └────────────┘    └──────────┘    └─────┬──────┘
                                                       │
                 ┌──────────┐    ┌──────────┐          │
                 │ 6. VERIFY│◀───│ 5. PUSH  │◀─────────┘
                 │ loaded?  │    │ updated  │
                 │ repeat   │    │ policy   │
                 └──────────┘    └──────────┘
```

### Port Forwarding Security Note

`openshell forward` creates a tunnel from host ports into the sandbox. This
is an **operator-only** action (the agent cannot create forwards), but it
introduces a potential policy bypass vector worth understanding.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Port Forwarding vs Policy Engine                   │
│                                                                       │
│  NORMAL EGRESS (policy-enforced):                                     │
│                                                                       │
│  Agent → outbound connection → Proxy → Policy Engine → Allow/Deny     │
│                                        ▲                              │
│                                        │                              │
│                              All egress funneled through              │
│                              sandbox network namespace                │
│                                                                       │
│  FORWARDED PORT (potentially unmonitored):                            │
│                                                                       │
│  Agent → localhost:<forwarded-port> → tunnel → Host network           │
│                                                  │                    │
│                                                  ▼                    │
│                                         Host has unrestricted         │
│                                         egress (no sandbox proxy)     │
│                                                                       │
│  Risk scenario:                                                       │
│  1. Operator forwards host port 8080 into sandbox                     │
│  2. Host port 8080 runs a proxy/service with unrestricted egress      │
│  3. Agent discovers forwarded port (port scan or config leak)         │
│  4. Agent routes traffic through forwarded port                       │
│  5. Traffic exits via HOST network, bypassing sandbox policy engine   │
│                                                                       │
│  Mitigations:                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  ✓ Operator-only: agent cannot create forwards                  │  │
│  │  ✓ Intentional: operator chose to forward the port              │  │
│  │  ? Unknown: docs don't state whether forwarded traffic is       │  │
│  │    also subject to policy engine inspection                     │  │
│  │                                                                 │  │
│  │  Recommendations:                                               │  │
│  │  • Avoid forwarding ports that expose services with             │  │
│  │    unrestricted network access                                  │  │
│  │  • Prefer forwarding sandbox→host (agent exposes a service)     │  │
│  │    over host→sandbox (agent gains a network path out)           │  │
│  │  • Audit forwarded ports as part of policy review               │  │
│  │  • Test whether forwarded traffic passes through the proxy      │  │
│  │    (if yes, the concern is neutralized)                         │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Status: OPEN QUESTION — needs verification against OpenShell         │
│  implementation. If forwarded connections bypass the proxy, this is a │
│  documented escape hatch that operators must manage consciously.      │
└───────────────────────────────────────────────────────────────────────┘
```

This is analogous to SSH port forwarding bypassing corporate firewalls — a
well-known pattern where a sanctioned tunnel inadvertently undermines network
policy. The key difference is that in OpenShell, only the operator (not the
agent) can create the tunnel. But a misconfigured forward could still weaken
the sandbox's egress guarantees.

---

## 8  Privacy Router (Inference Routing)

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Privacy Router                                     │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Agent calls inference.local                                    │  │
│  │  (standard OpenAI-compatible API)                               │  │
│  └──────────────────────────┬──────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  OpenShell Privacy Router                                       │  │
│  │                                                                 │  │
│  │  1. Intercept inference call                                    │  │
│  │  2. Strip sandbox-supplied credentials                          │  │
│  │  3. Apply cost + privacy policy                                 │  │
│  │  4. Route decision:                                             │  │
│  │                                                                 │  │
│  │     ┌─────────────────┐    ┌──────────────────────────────────┐ │  │
│  │     │  Local Model    │    │  Cloud Model                     │ │  │
│  │     │  (on-device)    │    │  (Anthropic, NVIDIA, OpenAI)     │ │  │
│  │     │                 │    │                                  │ │  │
│  │     │  Sensitive data │    │  Non-sensitive or policy-allowed │ │  │
│  │     │  stays on device│    │  Inject backend credentials      │ │  │
│  │     └─────────────────┘    └──────────────────────────────────┘ │  │
│  │                                                                 │  │
│  │  5. Inject backend-specific credentials                         │  │
│  │  6. Forward request to selected endpoint                        │  │
│  │  7. Return response to agent                                    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Key properties:                                                      │
│  • Model-agnostic — works with any LLM provider                       │
│  • Agent doesn't know which model it's talking to                     │
│  • Routing decisions based on operator policy, not agent preference   │
│  • Switch models at runtime without restarting sandbox                │
│  • Credentials never visible to agent code                            │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 9  Gateway

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Gateway Architecture                               │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Gateway = Control-Plane API (runs inside Docker)               │  │
│  │                                                                 │  │
│  │  Responsibilities:                                              │  │
│  │  • Sandbox lifecycle management (create, delete, list)          │  │
│  │  • State coordination across sandboxes                          │  │
│  │  • Authentication boundary (who can manage sandboxes)           │  │
│  │  • Request brokering (CLI → gateway → sandbox)                  │  │
│  │  • Provider registration and credential management              │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Deployment options:                                                  │
│                                                                       │
│  ┌───────────────┐  ┌───────────────┐  ┌──────────────────────────┐   │
│  │  LOCAL        │  │  REMOTE       │  │  CLOUD                   │   │
│  │               │  │  (SSH)        │  │  (reverse proxy)         │   │
│  │  openshell    │  │  openshell    │  │  openshell gateway add   │   │
│  │  gateway start│  │  gateway start│  │    https://gw.example.com│   │
│  │               │  │  --remote     │  │                          │   │
│  │  Docker on    │  │   user@host   │  │  Already running behind  │   │
│  │  workstation  │  │               │  │  Cloudflare Access etc.  │   │
│  │               │  │  Only Docker  │  │  Register + auth via     │   │
│  │  Auto-        │  │  needed on    │  │  browser                 │   │
│  │  provisioned  │  │  remote       │  │                          │   │
│  └───────────────┘  └───────────────┘  └──────────────────────────┘   │
│                                                                       │
│  Multiple gateways: openshell gateway select <name>                   │
│  Gateway on Brev: brev.nvidia.com/launchable (OpenShell Launchable)   │
│  Gateway on Spark: openshell gateway start --remote user@spark.local  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 10  Remote Hosting & Multi-Machine Deployment

The gateway/sandbox split is the architectural key to remote hosting. The CLI
runs on your laptop; the gateway and its sandboxes run wherever Docker is
available. The two communicate over SSH tunnels, mTLS, or HTTPS through a
reverse proxy. **No code changes, no policy changes, no agent changes** — the
same sandbox definition works identically on localhost, a bare-metal server
across the room, or a Brev cloud instance on the other side of the planet.

### 10.0  Where the Brains Live (Gateway vs Orchestrator vs Sub-Agents)

Before describing the remote hosting topology, it's critical to understand
which layer does the thinking — because the gateway-centric language in the
rest of this section can make it sound like the gateway is the center of the
system. **It is not.** The orchestrator is.

```
┌───────────────────────────────────────────────────────────────────────┐
│         The Three-Level Hierarchy in a Real Deployment                │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  LEVEL 1: GATEWAY  (infrastructure plumbing — zero intelligence)│  │
│  │                                                                 │  │
│  │  The gateway is an infrastructure daemon. It:                   │  │
│  │  • Creates, deletes, and monitors sandbox containers            │  │
│  │  • Stores credentials and injects them into sandboxes           │  │
│  │  • Delivers and hot-reloads policies                            │  │
│  │  • Registers inference providers                                │  │
│  │  • Provides SSH tunnel endpoints                                │  │
│  │                                                                 │  │
│  │  It does NOT:                                                   │  │
│  │  • Reason, plan, decide, or learn anything                      │  │
│  │  • Know what skills, memory, or workflows exist                 │  │
│  │  • Understand the difference between an orchestrator sandbox    │  │
│  │    and a coding sub-agent sandbox — they're all just containers │  │
│  │                                                                 │  │
│  │  When you `openshell gateway select brev-prod`, you're          │  │
│  │  switching infrastructure, not switching brains.                │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │ manages                                │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  LEVEL 2: ORCHESTRATOR  (the super-agent — this is the brain)   │  │
│  │                                                                 │  │
│  │  The orchestrator is an always-on agent process running inside  │  │
│  │  its own OpenShell sandbox. It is the ONLY component that has   │  │
│  │  the full picture. All high-level intelligence lives here:      │  │
│  │                                                                 │  │
│  │  ✓ Agentic loop      — prompt → LLM → tools → loop              │  │
│  │  ✓ Skills system     — SKILL.md files, auto-creation, recall    │  │
│  │  ✓ Memory system     — MEMORY.md, USER.md, Honcho, session      │  │
│  │                        search (all three layers)                │  │
│  │  ✓ Self-learning     — evaluate outcomes, create/update skills, │  │
│  │                        persist knowledge                        │  │
│  │  ✓ Cron scheduler    — background tasks, periodic scans         │  │
│  │  ✓ Messaging         — Slack connector (receives user tasks)    │  │
│  │  ✓ Sub-agent mgmt    — decides WHEN to delegate, WHAT to        │  │
│  │                        delegate, spawns sub-agent sandboxes,    │  │
│  │                        collects results, merges outputs         │  │
│  │  ✓ Workflow logic    — decides what to do and how to do it      │  │
│  │  ✓ Prompt engineering— system prompt, context injection, nudges │  │
│  │                                                                 │  │
│  │  The orchestrator is the manager who understands the project,   │  │
│  │  remembers past work, learns from mistakes, and decides what    │  │
│  │  to hand off to workers. It is itself an agent — the most       │  │
│  │  capable one in the system.                                     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │ spawns & manages via                   │
│                              │ `openshell sandbox create/delete`      │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  LEVEL 3: SUB-AGENTS  (ephemeral workers — limited intelligence)│  │
│  │                                                                 │  │
│  │  Leaf workers in their own sandboxes. They execute a scoped     │  │
│  │  task and return a result. They do NOT have the full picture:   │  │
│  │                                                                 │  │
│  │  ✗ No memory system    ✗ No self-learning    ✗ No cron          │  │
│  │  ✗ No Slack connector  ✗ No sub-agent mgmt   ✗ No skills recall │  │
│  │                                                                 │  │
│  │  They DO have:                                                  │  │
│  │  ✓ A narrow toolset (fs + terminal for coding; read-only for    │  │
│  │    review; web search for research)                             │  │
│  │  ✓ An LLM (via inference routing) for their specific task       │  │
│  │  ✓ Their own sandbox policy (isolated from orchestrator)        │  │
│  │                                                                 │  │
│  │  Lifecycle: created by orchestrator → receives task → executes  │  │
│  │  → returns output → destroyed                                   │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Summary:                                                             │
│                                                                       │
│    Gateway      = the building's property manager (keeps lights on)   │
│    Orchestrator = the CEO in the corner office (makes all decisions)  │
│    Sub-agents   = contractors (do specific jobs, leave when done)     │
│                                                                       │
│  The orchestrator is where you encode workflows, skills, agent        │
│  memory, self-learning, and all reasoning. The gateway just keeps     │
│  its sandbox alive. Sub-agents just execute what it tells them to.    │
└───────────────────────────────────────────────────────────────────────┘
```

**Implication for NemoClaw Escapades:** The orchestrator — with its agentic
loop, skills, memory, self-learning loop, and Slack connector — must be baked
into its sandbox, either as part of the container image or seeded into the
sandbox filesystem at creation time. The gateway knows nothing about any of
it. Sub-agent sandboxes are lightweight and disposable; the orchestrator
sandbox is the one that must persist and be treated with care.

### Where Does the Agent Come From?

None of these three layers — Gateway, OpenShell, or NemoClaw — contain the
agent intelligence. The agent is a **separate project entirely**.

```
┌───────────────────────────────────────────────────────────────────────┐
│         Who Provides What                                             │
│                                                                       │
│  OpenShell  = the runtime ("where it runs")                           │
│               Sandbox containers, policy engine, inference routing,   │
│               credential injection, gateway. Agent-agnostic.          │
│               Contains no skills, no memory, no agent loop.           │
│                                                                       │
│  NemoClaw   = the setup harness ("how it's configured")               │
│               A blueprint (Python) with default policies and a        │
│               plugin (TypeScript) that registers inference providers  │
│               inside the agent. Drives `openshell` CLI to create      │
│               a sandbox, apply policies, and wire up inference.       │
│               Contains no agent logic — it's a setup wizard.          │
│                                                                       │
│  OpenClaw   = the agent ("what runs")                                 │
│               The actual intelligence: agent loop, skills, memory,    │
│               messaging gateway, tools, cron, sub-agents, Canvas.     │
│               This is where skills, workflows, and reasoning live.    │
│               NemoClaw deploys OpenClaw INTO an OpenShell sandbox.    │
│                                                                       │
│  In the NemoClaw repo, you will NOT find:                             │
│  ✗ An agent loop          ✗ Skills            ✗ Memory files          │
│  ✗ A cron scheduler       ✗ Tool definitions  ✗ Prompt templates      │
│                                                                       │
│  You WILL find:                                                       │
│  ✓ A plugin (TypeScript) — registers /nemoclaw slash command          │
│    and inference provider inside OpenClaw                             │
│  ✓ A blueprint (Python) — runner.py (plan/apply/status) and           │
│    openclaw-sandbox.yaml (default policy)                             │
│  ✓ CLI commands (onboard, connect, status, logs, deploy, destroy)     │
│                                                                       │
│  NemoClaw is to OpenClaw what a Dockerfile is to the application:     │
│  it defines HOW to package and deploy it, not WHAT it does.           │
└───────────────────────────────────────────────────────────────────────┘
```

**For NemoClaw Escapades specifically:** Since the project builds a custom
orchestrator (not vanilla OpenClaw), we'll interact with **OpenShell
directly** rather than going through NemoClaw. We'll need to:

1. Build the orchestrator agent (custom code: agent loop, skills, memory,
   Slack connector, self-learning loop)
2. Package it into a container image or seed it into a sandbox filesystem
3. Create an OpenShell sandbox with the right policies and providers
4. Effectively build our own "NemoClaw-like" harness tailored to our
   custom orchestrator instead of OpenClaw

### 10.1  The Gateway as the Remote Anchor

The gateway is the single process that owns everything on the **infrastructure
side** of the remote deployment: sandbox lifecycle, credential storage, policy
delivery, inference configuration, and the SSH tunnel endpoint that lets you
`openshell sandbox connect` from anywhere. It contains no agent logic. Every
CLI command flows through the gateway API (gRPC + HTTP, multiplexed on one
port, mTLS by default).

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Gateway: Local vs Remote vs Cloud                     │
│                                                                          │
│  All three expose the SAME API surface. Sandboxes, policies, providers,  │
│  and inference work identically. Only the transport differs.             │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  LOCAL GATEWAY                                                     │  │
│  │                                                                    │  │
│  │  $ openshell gateway start                                         │  │
│  │                                                                    │  │
│  │  • Runs in Docker on your workstation                              │  │
│  │  • CLI connects via localhost:8080                                 │  │
│  │  • Auto-bootstrapped if you just run `sandbox create`              │  │
│  │  • Best for: development, quick iteration                          │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  REMOTE GATEWAY (SSH)                                              │  │
│  │                                                                    │  │
│  │  $ openshell gateway start --remote user@hostname                  │  │
│  │  $ openshell gateway start --remote user@host --ssh-key ~/.ssh/key │  │
│  │                                                                    │  │
│  │  • Installs and starts gateway in Docker on the remote host        │  │
│  │  • CLI connects over SSH tunnel (automatic, transparent)           │  │
│  │  • Only Docker needed on remote — no OpenShell install required    │  │
│  │  • Sandboxes run on remote hardware (GPU, disk, memory)            │  │
│  │  • Best for: DGX Spark, on-prem servers, any Linux VM              │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  CLOUD GATEWAY (reverse proxy)                                     │  │
│  │                                                                    │  │
│  │  $ openshell gateway add https://gateway.example.com               │  │
│  │  $ openshell gateway add https://gw.example.com --name production  │  │
│  │                                                                    │  │
│  │  • Gateway already running behind Cloudflare Access / similar      │  │
│  │  • CLI authenticates via browser (bearer token stored locally)     │  │
│  │  • Re-authenticate on token expiry: `openshell gateway login`      │  │
│  │  • Best for: cloud VMs, Brev instances, team-accessible gateways   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  REGISTER EXISTING GATEWAY (any type)                              │  │
│  │                                                                    │  │
│  │  # Already-running remote gateway (SSH access)                     │  │
│  │  $ openshell gateway add ssh://user@remote-host:8080               │  │
│  │                                                                    │  │
│  │  # Already-running local gateway (started outside CLI)             │  │
│  │  $ openshell gateway add https://127.0.0.1:8080 --local            │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 10.2  Multi-Gateway Management

You can register **multiple gateways** and switch between them. One gateway is
always the "active" gateway — all CLI commands target it by default.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Multi-Gateway Workflow                             │
│                                                                       │
│  ┌──────────┐                                                         │
│  │  Laptop  │                                                         │
│  │  CLI     │                                                         │
│  └─────┬────┘                                                         │
│        │                                                              │
│        ├── active ──▶  ┌──────────────────────────────┐               │
│        │               │  "dev-local" (local gateway) │               │
│        │               │  Laptop Docker               │               │
│        │               │  Quick iteration             │               │
│        │               └──────────────────────────────┘               │
│        │                                                              │
│        ├── select ──▶  ┌──────────────────────────────┐               │
│        │               │  "spark" (remote via SSH)    │               │
│        │               │  DGX Spark on desk           │               │
│        │               │  Local inference + GPU       │               │
│        │               └──────────────────────────────┘               │
│        │                                                              │
│        └── select ──▶  ┌──────────────────────────────┐               │
│                        │  "brev-prod" (cloud gateway) │               │
│                        │  Brev L4 instance            │               │
│                        │  Always-on production agent  │               │
│                        └──────────────────────────────┘               │
│                                                                       │
│  Commands:                                                            │
│  $ openshell gateway select               # list all, pick active     │
│  $ openshell gateway select brev-prod     # switch active gateway     │
│  $ openshell status -g spark              # one-off on non-active     │
│  $ openshell gateway info                 # endpoint, auth, port      │
│  $ openshell gateway info --name spark    # info for specific gw      │
└───────────────────────────────────────────────────────────────────────┘
```

This means you can develop locally, test on a DGX Spark, and deploy to Brev —
all from the same CLI, same commands, same policy files. Just switch the active
gateway.

### 10.3  Credential & Provider Management Across Machines

Providers are **registered with and stored by the gateway**. They are not
stored on your laptop or inside the sandbox. This has important implications
for remote deployments.

#### Where Providers Physically Live

```
┌───────────────────────────────────────────────────────────────────────┐
│         Provider Storage: It's on the Gateway Host                    │
│                                                                       │
│  When you run:                                                        │
│  $ openshell provider create --name my-claude \                       │
│      --type claude --from-existing                                    │
│                                                                       │
│  The CLI reads ANTHROPIC_API_KEY from YOUR LOCAL shell environment,   │
│  then sends it to the GATEWAY over the CLI→gateway transport.         │
│                                                                       │
│  ┌────────────┐    credential     ┌──────────────────────────────┐    │
│  │  Laptop    │    over SSH/mTLS  │  Gateway Host                │    │
│  │            │───────────────────▶                              │    │
│  │  CLI reads │                   │  Gateway stores credential   │    │
│  │  env var   │                   │  in its internal state       │    │
│  │  locally   │                   │  (Docker volume on the host) │    │
│  └────────────┘                   └──────────────────────────────┘    │
│                                                                       │
│  After this:                                                          │
│  • The credential lives on the GATEWAY HOST, not your laptop          │
│  • Your laptop only has the gateway connection info (address + auth)  │
│  • The gateway persists providers across restarts (Docker volume)     │
│  • Providers are gateway-scoped — different gateways have different   │
│    providers (dev-local might have test keys, brev-prod has real ones)│
└───────────────────────────────────────────────────────────────────────┘
```

#### How Credentials Travel: Laptop → Gateway → Proxy (Not Sandbox)

```
┌───────────────────────────────────────────────────────────────────────┐
│         End-to-End Credential Flow                                    │
│                                                                       │
│  STEP 1: Operator creates provider (one-time setup)                   │
│  ─────────────────────────────────────────────────                    │
│                                                                       │
│  ┌──────────────┐                        ┌────────────────────────┐   │
│  │  Laptop      │                        │  Gateway               │   │
│  │              │   provider create      │  (local, SSH, or cloud)│   │
│  │  CLI reads   │   ──────────────────▶  │                        │   │
│  │  local env   │   credential value     │  Stores credential in  │   │
│  │  vars or     │   sent over encrypted  │  persistent state      │   │
│  │  --credential│ transport (SSH tunnel  │  (Docker volume)       │   │
│  │  flag        │   or mTLS)             │                        │   │
│  └──────────────┘                        └────────────────────────┘   │
│                                                                       │
│  Transport security by gateway type:                                  │
│  • Local gateway:  localhost (no wire exposure)                       │
│  • SSH gateway:    SSH tunnel (encrypted by SSH)                      │
│  • Cloud gateway:  mTLS or HTTPS (encrypted in transit)               │
│                                                                       │
│  STEP 2: Operator creates sandbox with providers attached             │
│  ─────────────────────────────────────────────────────                │
│                                                                       │
│  $ openshell sandbox create \                                         │
│      --provider my-claude \                                           │
│      --provider my-github \                                           │
│      --provider my-slack \                                            │
│      -- claude                                                        │
│                                                                       │
│  STEP 3: Proxy injects credentials at EGRESS, not at boot             │
│  ─────────────────────────────────────────────────────                │
│                                                                       │
│  The agent NEVER receives actual credentials — not as env vars,       │
│  not as files, not as config. Instead, the sandbox proxy (which       │
│  intercepts all outbound traffic) injects the real credentials        │
│  at the point of egress, after stripping anything the agent sent.     │
│                                                                       │
│  For INFERENCE providers (claude, openai, nvidia, codex):             │
│                                                                       │
│  ┌──────────────────┐     ┌──────────────┐     ┌─────────────────┐    │
│  │  Agent           │     │  Proxy       │     │  Cloud API      │    │
│  │                  │     │  (in-sandbox)│     │  (Anthropic,    │    │
│  │  Calls           │     │              │     │   NVIDIA, etc.) │    │
│  │  inference.local │────▶│  1. Intercept│────▶│                 │    │
│  │  with NO real    │     │  2. Strip    │     │  Receives real  │    │
│  │  credentials     │     │     agent's  │     │  API key from   │    │
│  │  (or dummy ones) │     │     creds    │     │  proxy          │    │
│  │                  │     │  3. Inject   │     │                 │    │
│  │                  │     │     REAL     │     │                 │    │
│  │                  │     │     backend  │     │                 │    │
│  │                  │     │     creds    │     │                 │    │
│  │                  │     │  4. Forward  │     │                 │    │
│  └──────────────────┘     └──────────────┘     └─────────────────┘    │
│                                                                       │
│  The agent calls inference.local and gets a response.                 │
│  It has no idea which model it's talking to or what API key was used. │
│  The proxy (configured by the gateway with stored provider creds)     │
│  handles the credential swap transparently.                           │
│                                                                       │
│  This is documented in §5 (Request Flow) and §8 (Privacy Router):     │
│  "Strip sandbox-supplied credentials → Inject backend credentials"    │
│                                                                       │
│  For NON-INFERENCE providers (github, gitlab, generic, slack):        │
│                                                                       │
│  OPEN QUESTION — the docs are not explicit about whether the proxy    │
│  also handles credential injection for non-inference API calls.       │
│  Two possible mechanisms:                                             │
│                                                                       │
│  A) Proxy-injected at egress (like inference):                        │
│     For REST policies with `tls: terminate`, the proxy does TLS       │
│     termination and can inspect/modify HTTP requests. It COULD        │
│     inject Authorization headers for GitHub, Slack, etc.              │
│     → Agent never sees credentials.                                   │
│                                                                       │
│  B) Env-var injected at boot (traditional Docker model):              │
│     The gateway sets env vars (GITHUB_TOKEN, SLACK_BOT_TOKEN) in      │
│     the container at creation time. The agent reads them directly.    │
│     → Agent sees credential values (less secure).                     │
│                                                                       │
│  The inference model (A) is clearly proxy-injected. The non-inference │
│  model needs verification against the OpenShell implementation.       │
│  For this project, assume (A) for inference and verify for others.    │
│                                                                       │
│  Providers cannot be added to a running sandbox — must recreate.      │
└───────────────────────────────────────────────────────────────────────┘
```

#### Provider Creation Commands

```bash
# Auto-detect from your shell environment (reads local env vars,
# sends to gateway — credential leaves your laptop)
$ openshell provider create --name my-claude \
    --type claude --from-existing

# Explicit credential (value passed on command line,
# sent to gateway over encrypted transport)
$ openshell provider create --name my-github \
    --type github --credential GITHUB_TOKEN=ghp_xxx

# Generic service (any env var name)
$ openshell provider create --name my-slack \
    --type generic --credential SLACK_BOT_TOKEN=xoxb-xxx
```

Supported types: `claude`, `codex`, `generic`, `github`, `gitlab`,
`nvidia`, `openai`, `opencode`.

#### Security Implications

```
┌───────────────────────────────────────────────────────────────────────┐
│         Provider Security Considerations                              │
│                                                                       │
│  1. Credentials travel from laptop to gateway over the wire           │
│     • Local gateway: localhost only — no network exposure             │
│     • SSH gateway: encrypted by SSH tunnel                            │
│     • Cloud gateway: encrypted by mTLS/HTTPS                          │
│     → Encrypted in all cases, but credentials DO leave your laptop    │
│       when the gateway is remote.                                     │
│                                                                       │
│  2. Credentials are stored on the gateway host                        │
│     • Persisted in a Docker volume on the machine running the gateway │
│     • If the gateway host is compromised, credentials are exposed     │
│     • You are trusting the gateway host with your API keys            │
│                                                                       │
│  3. Credentials are NOT stored in the sandbox                         │
│     • For inference: proxy injects at egress — agent never sees them  │
│     • For non-inference: mechanism TBD (proxy-injected or env var?)   │
│     • Either way, the agent cannot exfiltrate inference credentials   │
│       because it never has them — the proxy does the swap             │
│                                                                       │
│  4. --from-existing copies your local env vars to the remote host     │
│     • Convenient but means your local secrets are now also remote     │
│     • For production: consider creating providers with different      │
│       credentials per gateway (test keys for dev, prod keys for Brev) │
│                                                                       │
│  5. Different gateways = different credential scopes                  │
│     • dev-local gateway: test/personal API keys                       │
│     • brev-prod gateway: production API keys                          │
│     • spark gateway: on-prem keys                                     │
│     → Credential scoping per gateway means switching gateways         │
│       also switches credential sets.                                  │
│                                                                       │
│  6. Per-sandbox provider attachment = least privilege                 │
│     • Each sandbox only gets the providers specified at creation      │
│     • Coding sub-agent: inference only (no GitHub, no Slack)          │
│     • Orchestrator: inference + Slack + GitHub (broader scope)        │
│     • A compromised coding agent can't access Slack credentials       │
│       because its sandbox was never given that provider               │
└───────────────────────────────────────────────────────────────────────┘
```

### 10.4  NVIDIA Brev Integration (Managed Cloud GPU)

[NVIDIA Brev](https://brev.nvidia.com/) is the most turnkey path to remote
OpenShell hosting. Brev provides GPU-accelerated VMs with Docker, CUDA, and
NVIDIA drivers pre-installed. OpenShell publishes an official
**Launchable** — a one-click deploy template.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    OpenShell on Brev — End-to-End                        │
│                                                                          │
│  OPTION A: Brev Launchable (one-click)                                   │
│  ─────────────────────────────────────                                   │
│  1. Go to brev.nvidia.com/launchable and click "Deploy" on the           │
│     OpenShell Launchable                                                 │
│  2. Wait for instance to start                                           │
│  3. In Brev console → "Using Secure Links" → copy URL for port 8080      │
│  4. Register the gateway on your laptop:                                 │
│                                                                          │
│     $ openshell gateway add https://<port-8080-url>.brevlab.com          │
│     $ openshell status                                                   │
│                                                                          │
│  5. Create sandboxes from your laptop — they run on Brev:                │
│                                                                          │
│     $ openshell sandbox create -- claude                                 │
│     $ openshell sandbox connect my-sandbox                               │
│                                                                          │
│  OPTION B: NemoClaw deploy (automated)                                   │
│  ─────────────────────────────────────                                   │
│  $ nemoclaw deploy my-agent                                              │
│                                                                          │
│  This provisions a Brev instance, installs Docker + NVIDIA Container     │
│  Toolkit + OpenShell, runs the onboard wizard, and connects you to       │
│  the sandbox — all in one command.                                       │
│                                                                          │
│  OPTION C: Manual SSH remote gateway                                     │
│  ─────────────────────────────────────                                   │
│  1. Create a Brev instance manually (any GPU or CPU type)                │
│  2. From your laptop:                                                    │
│                                                                          │
│     $ openshell gateway start --remote ubuntu@<brev-hostname>            │
│     $ openshell sandbox create --gpu -- claude                           │
│                                                                          │
│  All three options result in the same architecture:                      │
│                                                                          │
│  ┌───────────┐         ┌──────────────────────────────────────────────┐  │
│  │  Laptop   │  SSH /  │  Brev Instance                               │  │
│  │           │  HTTPS  │                                              │  │
│  │  openshell│────────▶│  ┌──────────────────┐                        │  │
│  │  CLI      │         │  │  Gateway         │  Port 8080 (mTLS)      │  │
│  │           │         │  └────────┬─────────┘                        │  │
│  │           │         │           │                                  │  │
│  │           │         │  ┌────────┴─────────┐                        │  │
│  │           │         │  │  Sandbox(es)     │  Agents run here       │  │
│  │           │         │  │  (Docker)        │                        │  │
│  │           │         │  └──────────────────┘                        │  │
│  │           │         │                                              │  │
│  │           │         │  Optional: GPU (L4, T4, A100, H100)          │  │
│  │           │         │  Persistent: /home/ubuntu/workspace          │  │
│  └───────────┘         └──────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 10.5  DGX Spark Integration

DGX Spark is a desktop AI supercomputer with the Grace Blackwell GB10 chip
(128 GB unified memory). OpenShell treats it as a remote SSH host.

```bash
# From laptop — deploy gateway to Spark over SSH
openshell gateway start --remote <username>@<spark-ssid>.local

# Create a sandbox on the Spark
openshell sandbox create --gpu --from openclaw

# Sandboxes run on Spark hardware, using its GPU for local inference
```

DGX Spark is ideal for always-on local deployment because it's designed to run
24/7 and has enough memory for large context windows (128K+ tokens) and
multiple concurrent subagents (4–8 with Qwen3 Coder 80B).

### 10.6  Deployment Topology Summary

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Five Deployment Topologies                         │
│                                                                       │
│  Mode 1: EVERYTHING LOCAL                                             │
│  ┌─────────────────────────────────────────┐                          │
│  │  Laptop / Desktop                       │                          │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ │                          │
│  │  │ CLI      │ │ Gateway  │ │ Sandbox  │ │                          │
│  │  │          │→│ (Docker) │→│ (Docker) │ │                          │
│  │  └──────────┘ └──────────┘ └──────────┘ │                          │
│  └─────────────────────────────────────────┘                          │
│  Setup: openshell sandbox create -- claude                            │
│  (auto-bootstraps local gateway)                                      │
│                                                                       │
│  Mode 2: REMOTE SSH (any Linux machine)                               │
│  ┌──────────┐      SSH       ┌──────────────────────┐                 │
│  │  Laptop  │──────────────▶ │  Remote Host         │                 │
│  │  CLI     │                │  Gateway + Sandbox(s)│                 │
│  └──────────┘                └──────────────────────┘                 │
│  Setup: openshell gateway start --remote user@host                    │
│  Prereq: Docker on remote host, SSH access                            │
│                                                                       │
│  Mode 3: CLOUD GATEWAY (reverse proxy)                                │
│  ┌──────────┐    HTTPS     ┌─────────────┐   ┌───────────────────┐    │
│  │  Laptop  │────────────▶ │  Cloudflare │──▶│  Cloud VM         │    │
│  │  CLI     │              │  Access     │   │  Gateway + Sandbox│    │
│  └──────────┘              └─────────────┘   └───────────────────┘    │
│  Setup: openshell gateway add https://gateway.example.com             │
│  Prereq: Gateway pre-deployed behind reverse proxy                    │
│                                                                       │
│  Mode 4: BREV LAUNCHABLE (managed cloud GPU)                          │
│  ┌──────────┐    HTTPS     ┌──────────────────────────────────────┐   │
│  │  Laptop  │────────────▶ │  Brev Instance                       │   │
│  │  CLI     │              │  (OpenShell Launchable)              │   │
│  │          │              │  Gateway + Sandbox + GPU             │   │
│  └──────────┘              └──────────────────────────────────────┘   │
│  Setup: 1-click Launchable → openshell gateway add <url>              │
│  OR: nemoclaw deploy <name>                                           │
│                                                                       │
│  Mode 5: DGX SPARK (desktop AI supercomputer)                         │
│  ┌──────────┐    SSH       ┌──────────────────────────────────────┐   │
│  │  Laptop  │────────────▶ │  DGX Spark                           │   │
│  │  CLI     │              │  Gateway + Sandbox + Local Inference │   │
│  │          │              │  Grace Blackwell GB10 (128 GB)       │   │
│  └──────────┘              └──────────────────────────────────────┘   │
│  Setup: openshell gateway start --remote user@spark.local             │
│                                                                       │
│  KEY INSIGHT: Same CLI, same commands, same policies in all modes.    │
│  Just switch the gateway: openshell gateway select <name>             │
└───────────────────────────────────────────────────────────────────────┘
```

### 10.7  What This Means for NemoClaw Escapades

The remote hosting model directly answers the project's hosting question.
The development workflow would be:

```
┌───────────────────────────────────────────────────────────────────────┐
│  Dev Cycle: Develop Local → Deploy Remote → Monitor from Anywhere     │
│                                                                       │
│  1. Develop locally                                                   │
│     $ openshell gateway start                     # local gateway     │
│     $ openshell sandbox create --from openclaw    # local sandbox     │
│     # iterate on agent code, policies, skills                         │
│                                                                       │
│  2. Deploy to Brev for always-on                                      │
│     $ openshell gateway start --remote ubuntu@brev-host               │
│     # — or —                                                          │
│     $ nemoclaw deploy my-agent                                        │
│     # Same sandbox definition, same policies, just remote hardware    │
│                                                                       │
│  3. Switch between environments                                       │
│     $ openshell gateway select dev-local    # back to laptop          │
│     $ openshell gateway select brev-prod    # back to production      │
│                                                                       │
│  4. Monitor production from anywhere                                  │
│     $ openshell status -g brev-prod                                   │
│     $ openshell logs my-sandbox --tail -g brev-prod                   │
│     $ openshell term -g brev-prod           # live TUI over SSH       │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 11  CLI Reference

### Installation

```bash
# Quick install
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh

# Via uv
uv tool install -U openshell

# Verify
openshell --help
```

### Key Commands

| Category | Command | Description |
|----------|---------|-------------|
| **Gateway** | `openshell gateway start` | Start local gateway |
| | `openshell gateway start --remote user@host` | Start on remote host |
| | `openshell gateway add https://url` | Register cloud gateway |
| | `openshell gateway select <name>` | Switch active gateway |
| | `openshell status` | Show gateway health |
| **Sandbox** | `openshell sandbox create -- <agent>` | Create sandbox |
| | `openshell sandbox create --from <name>` | From community catalog |
| | `openshell sandbox create --gpu -- <agent>` | With GPU access |
| | `openshell sandbox connect <name>` | SSH into sandbox |
| | `openshell sandbox list` | List all sandboxes |
| | `openshell sandbox get <name>` | Detailed info |
| | `openshell sandbox delete <name>` | Destroy sandbox |
| | `openshell sandbox ssh-config <name>` | Generate SSH config |
| **Files** | `openshell sandbox upload <name> <src> <dst>` | Upload to sandbox |
| | `openshell sandbox download <name> <src> <dst>` | Download from sandbox |
| **Policy** | `openshell policy get <name> --full` | Pull current policy |
| | `openshell policy set <name> --policy <file> --wait` | Push updated policy |
| | `openshell policy list <name>` | List policy revisions |
| **Inference** | `openshell inference set --provider <p> --model <m>` | Switch model |
| **Monitoring** | `openshell logs <name> --tail` | Stream logs |
| | `openshell term` | Live TUI dashboard |
| **Ports** | `openshell forward start <port> <name>` | Forward port |
| | `openshell forward list` | List forwards |
| | `openshell forward stop <port> <name>` | Stop forward |

---

## 12  Community Sandboxes & Agent Support

OpenShell is agent-agnostic. The community catalog provides pre-built sandbox
definitions:

```
┌───────────────────────────────────────────────────────────────┐
│                    Supported Agents                           │
│                                                               │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────────┐  │
│  │  Claude Code  │  │  OpenClaw     │  │  OpenCode         │  │
│  │               │  │               │  │                   │  │
│  │  openshell    │  │  openshell    │  │  openshell        │  │
│  │  sandbox      │  │  sandbox      │  │  sandbox          │  │
│  │  create       │  │  create       │  │  create           │  │
│  │  -- claude    │  │  --from       │  │  -- opencode      │  │
│  │               │  │  openclaw     │  │                   │  │
│  └───────────────┘  └───────────────┘  └───────────────────┘  │
│                                                               │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────────┐  │
│  │  Codex        │  │  Base         │  │  Custom Image     │  │
│  │               │  │  (minimal)    │  │                   │  │
│  │  openshell    │  │  openshell    │  │  openshell        │  │
│  │  sandbox      │  │  sandbox      │  │  sandbox          │  │
│  │  create       │  │  create       │  │  create           │  │
│  │  -- codex     │  │  --from base  │  │  --from image:tag │  │
│  └───────────────┘  └───────────────┘  └───────────────────┘  │
│                                                               │
│  Community catalog: github.com/NVIDIA/OpenShell-Community     │
│  Each definition: container image + tailored policy + skills  │
└───────────────────────────────────────────────────────────────┘
```

---

## 13  IDE Integration

OpenShell provides direct IDE access to sandboxes:

```bash
# Create sandbox with VS Code auto-launch
openshell sandbox create --editor vscode --name my-sandbox

# Connect Cursor to existing sandbox
openshell sandbox connect my-sandbox --editor cursor

# Generate SSH config for manual IDE setup
openshell sandbox ssh-config my-sandbox >> ~/.ssh/config
```

When `--editor` is used, OpenShell:
- Keeps the sandbox alive
- Installs an OpenShell-managed SSH include file
- Does not clutter `~/.ssh/config` with generated host blocks

---

## 14  Setup & Installation

### Prerequisites

| Requirement | Details |
|-------------|---------|
| Docker | Docker Desktop running |
| Python | For pip/uv install |

### Platform Support

| Platform | Status |
|----------|--------|
| Linux (Ubuntu 22.04+) | Primary supported path |
| macOS (Colima / Docker Desktop) | Supported |
| Windows WSL (Docker Desktop) | Supported |
| DGX Spark | Supported (with cgroup v2 setup) |
| Brev (cloud GPU) | Supported via Launchable |

---

## 15  Comparison with Hermes Terminal Backends

OpenShell serves the same role as Hermes's terminal backends — providing
isolated execution environments for agent tasks. Here's how they compare:

```
┌───────────────────────────────────────────────────────────────────────┐
│                    OpenShell vs Hermes Terminal Backends              │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  Feature          │  OpenShell         │  Hermes Backends        │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Isolation         │  Kernel-level      │  Container-level       │ │
│  │                    │  (Landlock+seccomp │  (Docker) or none      │ │
│  │                    │  +network NS)      │  (local)               │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Policy engine    │  Yes (granular      │  No (command approval  │ │
│  │                    │  per-binary,        │  lists only)          │ │
│  │                    │  per-endpoint,      │                       │ │
│  │                    │  per-method)        │                       │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Inference routing│  Yes (privacy       │  No (agent manages     │ │
│  │                    │  router, model-     │  its own API calls)   │ │
│  │                    │  agnostic)          │                       │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Agent support    │  Any (Claude Code,  │  Hermes only           │ │
│  │                    │  OpenClaw, Codex,   │                       │ │
│  │                    │  custom)            │                       │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Backend options  │  Local, Remote SSH, │  Local, Docker, SSH,   │ │
│  │                    │  Cloud, Brev, Spark │  Daytona, Singularity,│ │
│  │                    │                    │  Modal                 │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Audit trail      │  Yes (all decisions │  No                    │ │
│  │                    │  logged)            │                       │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  IDE integration  │  Yes (VS Code,      │  No native             │ │
│  │                    │  Cursor)            │  (ACP is separate)    │ │
│  ├───────────────────┼───────────────────┼───────────────────────│    │
│  │  Credential mgmt  │  Runtime-injected,  │  Agent-managed         │ │
│  │                    │  never visible to   │  (config/env vars)    │ │
│  │                    │  agent              │                       │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  Key takeaway: OpenShell is strictly more secure than any Hermes      │
│  terminal backend. It's the right choice for enterprise/always-on     │
│  deployment where trust boundaries matter.                            │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 16  Answers to Design Doc Questions

### Q9: Can we auto-identify a workflow's required permissions?

**Partially, via the deny-and-approve workflow.** OpenShell's operator approval
flow provides a practical way to discover what permissions a workflow needs:

1. Start the workflow in a sandbox with a minimal policy
2. Run `openshell term` to monitor blocked requests
3. As the agent hits denied endpoints, the TUI surfaces them
4. Approve or deny each request
5. Export the resulting policy as the workflow's permission set

This is manual but systematic. For automation, you could:
- Capture denied requests programmatically via `openshell logs --tail`
- Parse the deny log entries to extract required endpoints
- Auto-generate a policy YAML from the deny log

**OpenShell doesn't auto-discover permissions**, but it provides the tooling to
iteratively discover and lock them down.

### Q11: How to set up Claude Code in an OpenShell container?

**One command:**

```bash
openshell sandbox create -- claude
```

The input/output contract for a coding agent workflow:
1. **Create sandbox:** `openshell sandbox create --policy coding-policy.yaml -- claude`
2. **Upload code:** `openshell sandbox upload <name> ./project /sandbox/project`
3. **Send task:** `openshell sandbox connect <name>` then `claude "Implement feature X"`
4. **Download result:** `openshell sandbox download <name> /sandbox/project ./output`
5. **Cleanup:** `openshell sandbox delete <name>`

For the coding agent in NemoClaw Escapades (Milestone 3), this is the
exact pattern to follow. The orchestrator would automate steps 1–5.

---

## 17  What to Lift for NemoClaw Escapades

| Milestone | OpenShell Component | How to Use |
|-----------|-------------------|-----------|
| M1 — Foundation | Gateway + Sandbox | Deploy orchestrator inside an OpenShell sandbox with Slack and inference endpoints in network policy |
| M2 — Knowledge | Network policy | Add SecondBrain API endpoint to policy |
| M3 — Coding | Sandbox creation | `openshell sandbox create -- claude` for ephemeral coding sandboxes; upload/download for file transfer |
| M4 — Self-Improvement | Workspace files | MEMORY.md and USER.md already supported in sandbox filesystem |
| M5 — Review | Multi-sandbox | Review agent in its own sandbox; communicate via shared volume or API |
| M6 — Professional KB | Network policy | Add Slack API, Teams API, and scraping endpoints to policy |

### Architecture Pattern for NemoClaw Escapades

```
┌───────────────────────────────────────────────────────────────────────┐
│                    NemoClaw Escapades on OpenShell                    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  ORCHESTRATOR SANDBOX (always-on)                               │  │
│  │                                                                 │  │
│  │  • Custom agent loop (Hermes-inspired)                          │  │
│  │  • Skills system, memory, self-learning                         │  │
│  │  • Slack connector                                              │  │
│  │  • Sub-agent spawning via openshell sandbox create              │  │
│  │                                                                 │  │
│  │  Network policy: Slack API, inference, SecondBrain, GitHub      │  │
│  └───────────────────────────┬─────────────────────────────────────┘  │
│                               │ spawns                                │
│                 ┌─────────────┼──────────────┐                        │
│                 ▼             ▼              ▼                        │
│  ┌──────────────────┐ ┌───────────────┐ ┌──────────────────────────┐  │
│  │ CODING SANDBOX   │ │ REVIEW SANDBOX│ │ RESEARCH SANDBOX         │  │
│  │ (ephemeral)      │ │ (ephemeral)   │ │ (ephemeral)              │  │
│  │                  │ │               │ │                          │  │
│  │ Claude Code      │ │ Custom agent  │ │ Web + SecondBrain        │  │
│  │ Minimal policy   │ │ Git read-only │ │ Minimal policy           │  │
│  │ GitHub push only │ │               │ │                          │  │
│  └──────────────────┘ └───────────────┘ └──────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

---

### Sources

- [OpenShell Architecture](https://docs.nvidia.com/openshell/latest/about/architecture.html)
- [OpenShell Quickstart](https://docs.nvidia.com/openshell/latest/get-started/quickstart.html)
- [About Gateways and Sandboxes](https://docs.nvidia.com/openshell/latest/sandboxes/index.html)
- [Deploy and Manage Gateways](https://docs.nvidia.com/openshell/latest/sandboxes/manage-gateways.html)
- [Manage Sandboxes](https://docs.nvidia.com/openshell/latest/sandboxes/manage-sandboxes.html)
- [Manage Providers and Credentials](https://docs.nvidia.com/openshell/latest/sandboxes/manage-providers.html)
- [Customize Sandbox Policies](https://docs.nvidia.com/openshell/latest/sandboxes/policies.html)
- [NVIDIA OpenShell Blog Post](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/)
- [NVIDIA OpenShell GitHub](https://github.com/NVIDIA/OpenShell)
- [NVIDIA Brev Launchables](https://docs.nvidia.com/brev/latest/launchables.html)
- [DGX Spark + OpenShell Blog](https://developer.nvidia.com/blog/scaling-autonomous-ai-agents-and-workloads-with-nvidia-dgx-spark/)
