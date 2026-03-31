# NemoClaw — Deep Dive

> **Source:** [NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw)
> (Apache 2.0 license, alpha since March 16, 2026)
>
> **Official docs:** [docs.nvidia.com/nemoclaw](https://docs.nvidia.com/nemoclaw/latest/)
>
> **Last reviewed:** 2026-03-29

---

## Table of Contents

1. [Overview](#1--overview)
2. [High-Level Architecture](#2--high-level-architecture)
3. [Design Principles](#3--design-principles)
4. [Plugin and Blueprint System](#4--plugin-and-blueprint-system)
5. [Sandbox Creation & Lifecycle](#5--sandbox-creation--lifecycle)
6. [Inference Routing](#6--inference-routing)
7. [Network & Filesystem Policy](#7--network--filesystem-policy)
8. [CLI Reference](#8--cli-reference)
9. [Monitoring & Observability](#9--monitoring--observability)
10. [Deployment Modes](#10--deployment-modes)
11. [Workspace Files](#11--workspace-files)
12. [Relationship to OpenShell](#12--relationship-to-openshell)
13. [Relationship to OpenClaw](#13--relationship-to-openclaw)
14. [Setup & Installation](#14--setup--installation)
15. [Answers to Design Doc Questions](#15--answers-to-design-doc-questions)
16. [What NemoClaw Means for This Project](#16--what-nemoclaw-means-for-this-project)

---

## 1  Overview

NemoClaw is NVIDIA's open-source reference stack for running
[OpenClaw](https://openclaw.ai/) always-on assistants with enterprise-grade
security and privacy. Announced at GTC 2026 (March 16, 2026), it wraps
OpenClaw in the NVIDIA [OpenShell](https://github.com/NVIDIA/OpenShell)
runtime, providing sandboxed execution, policy-enforced network access,
managed inference routing, and a single-command setup experience.

**In plain terms:** NemoClaw is the "enterprise harness" for OpenClaw — it
takes the powerful but unconstrained OpenClaw agent and puts it inside a
secure, policy-controlled sandbox where every network request, filesystem
access, and inference call is governed by the operator.

### What NemoClaw Is NOT

NemoClaw is **not an agent**. It contains no agent loop, no skills, no memory
system, no tools, no cron scheduler, no messaging connectors, and no
reasoning of any kind. It is a **setup and deployment harness** — a thin
layer that automates the process of deploying the OpenClaw agent into an
OpenShell sandbox with the right policies and inference providers.

All agent intelligence — the agentic loop, skills, memory, self-learning,
workflows, tools, and sub-agent delegation — comes from **OpenClaw**, which
is a separate project. NemoClaw just makes it easy to get OpenClaw running
inside a secure sandbox. Think of it as a Dockerfile for agents: it defines
HOW to package and deploy, not WHAT runs.

### Key Capabilities

| Capability | Description |
|------------|-------------|
| **Sandbox OpenClaw** | Creates an OpenShell sandbox pre-configured for OpenClaw with filesystem and network policies from first boot |
| **Route inference** | Configures OpenShell inference routing so agent traffic flows through cloud-hosted Nemotron 3 Super 120B via [build.nvidia.com](https://build.nvidia.com/) |
| **Manage lifecycle** | Handles blueprint versioning, digest verification, and sandbox setup |
| **Single CLI** | The `nemoclaw` command orchestrates the full stack: gateway, sandbox, inference provider, and network policy |
| **Remote deployment** | Deploy to NVIDIA Brev GPU instances for persistent always-on operation |

### Status

NemoClaw is in **alpha** (early preview since March 16, 2026). APIs,
configuration schemas, and runtime behavior are subject to breaking changes.
Not production-ready.

---

## 2  High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           NemoClaw Architecture                              │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │                         HOST MACHINE                                 │    │
│  │                                                                      │    │
│  │  ┌────────────────┐        ┌─────────────────────────────────────┐   │    │
│  │  │  nemoclaw CLI  │───────▶│  NemoClaw Blueprint (Python)        │   │    │
│  │  │  (npm package) │        │                                     │   │    │
│  │  │                │        │  • resolve version                  │   │    │
│  │  │  Commands:     │        │  • verify digest                    │   │    │
│  │  │  • onboard     │        │  • plan resources                   │   │    │
│  │  │  • connect     │        │  • apply via openshell CLI          │   │    │
│  │  │  • status      │        │  • report status                    │   │    │
│  │  │  • logs        │        └──────────────────┬──────────────────┘   │    │
│  │  │  • deploy      │                           │                      │    │
│  │  │  • destroy     │                           ▼                      │    │
│  │  └────────────────┘        ┌─────────────────────────────────────┐   │    │
│  │                            │  OpenShell CLI                      │   │    │
│  │                            │  • gateway management               │   │    │
│  │                            │  • sandbox CRUD                     │   │    │
│  │                            │  • policy enforcement               │   │    │
│  │                            │  • inference routing                │   │    │
│  │                             └──────────────────┬──────────────────┘  │    │
│  └─────────────────────────────────────────────────┼────────────────────┘    │
│                                                    │                         │
│                                                    ▼                         │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │                     OpenShell SANDBOX (Docker)                       │    │
│  │                                                                      │    │
│  │  ┌──────────────────────────────────────────────────────────────┐    │    │
│  │  │  OpenClaw Agent                                              │    │    │
│  │  │  + NemoClaw Plugin (TypeScript)                              │    │    │
│  │  │                                                              │    │    │
│  │  │  • /nemoclaw slash command                                   │    │    │
│  │  │  • Registered inference provider                             │    │    │
│  │  │  • Skills, memory, messaging gateway                         │    │    │
│  │  └─────────────────────────┬────────────────────────────────────┘    │    │
│  │                              │                                       │    │
│  │  ┌──────────────┐  ┌────────┴──────────┐  ┌──────────────────────┐   │    │
│  │  │  Filesystem  │  │  Network Policy   │  │  Inference Routing   │   │    │
│  │  │  Isolation   │  │  Engine           │  │                      │   │    │
│  │  │              │  │                   │  │  Agent → OpenShell   │   │    │
│  │  │  /sandbox RW │  │  • deny by default│  │  gateway → NVIDIA    │   │    │
│  │  │  /tmp    RW  │  │  • operator       │  │  Endpoints           │   │    │
│  │  │  /usr    RO  │  │    approval       │  │  (Nemotron 3 120B)   │   │    │
│  │  │  /lib    RO  │  │  • audit trail    │  │                      │   │    │
│  │  └──────────────┘  └───────────────────┘  └──────────────────────┘   │    │
│  │                                                                      │    │
│  │  Kernel Isolation: Landlock LSM + seccomp + network namespaces       │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │                     External Services                                │    │
│  │                                                                      │    │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐    │    │
│  │  │ build.nvidia │  │  Telegram    │  │  Brev (remote GPU        │    │    │
│  │  │ .com         │  │  Bridge      │  │   instances)             │    │    │
│  │  │ (inference)  │  │              │  │                          │    │    │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘    │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Component Interaction Flow

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌────────────────┐
│  User    │    │  nemoclaw    │    │  Blueprint   │    │  OpenShell     │
│  (CLI)   │    │  Plugin      │    │  (Python)    │    │  CLI           │
└─────┬────┘    └──────┬───────┘    └──────┬───────┘    └──────┬─────────┘
      │                │                    │                    │
      │  onboard       │                    │                    │
      ├───────────────▶│                    │                    │
      │                │  resolve + verify  │                    │
      │                ├───────────────────▶│                    │
      │                │                    │  plan resources    │
      │                │                    ├───────────────────▶│
      │                │                    │                    │
      │                │                    │  openshell gateway │
      │                │                    │  openshell sandbox │
      │                │                    │  openshell policy  │
      │                │                    │  openshell inference│
      │                │                    │◀───────────────────┤
      │                │     status         │                    │
      │                │◀──────────────────┤                    │
      │  ✅ sandbox    │                    │                    │
      │◀───────────────┤                    │                    │
      │                │                    │                    │
```

---

## 3  Design Principles

NemoClaw follows five architectural principles:

```
┌───────────────────────────────────────────────────────────────┐
│                    Design Principles                          │
│                                                               │
│  ┌─────────────────────┐  ┌────────────────────────────────┐  │
│  │  1. Thin Plugin,    │  │  2. Respect CLI Boundaries     │  │
│  │     Versioned       │  │                                │  │
│  │     Blueprint       │  │  nemoclaw CLI = primary        │  │
│  │                     │  │  interface for sandbox mgmt.   │  │
│  │  Plugin stays small │  │  No hidden internal APIs.      │  │
│  │  and stable.        │  │                                │  │
│  │  Orchestration      │  │                                │  │
│  │  evolves in the     │  │                                │  │
│  │  blueprint.         │  │                                │  │
│  └─────────────────────┘  └────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────┐  ┌────────────────────────────────┐  │
│  │  3. Supply Chain    │  │  4. OpenShell-Native           │  │
│  │     Safety          │  │                                │  │
│  │                     │  │  For new installs, recommend   │  │
│  │  Blueprints are     │  │  `openshell sandbox create`    │  │
│  │  immutable,         │  │  directly.                     │  │
│  │  versioned, and     │  │                                │  │
│  │  digest-verified.   │  │                                │  │
│  └─────────────────────┘  └────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  5. Reproducible Setup                                  │  │
│  │     Running setup again recreates sandbox from the      │  │
│  │     same blueprint and policy definitions.              │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

---

## 4  Plugin and Blueprint System

NemoClaw is split into two distinct artifacts with separate release cadences:

```
┌───────────────────────────────────────────────────────────────────────┐
│                  Plugin + Blueprint Architecture                      │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  PLUGIN (TypeScript / npm package)                              │  │
│  │                                                                 │  │
│  │  nemoclaw/                                                      │  │
│  │  ├── src/                                                       │  │
│  │  │   ├── index.ts                  Plugin entry                 │  │
│  │  │   ├── cli.ts                    Commander.js wiring          │  │
│  │  │   ├── commands/                                              │  │
│  │  │   │   ├── launch.ts             Fresh install into OpenShell │  │
│  │  │   │   ├── connect.ts            Interactive shell            │  │
│  │  │   │   ├── status.ts             Health + run state           │  │
│  │  │   │   ├── logs.ts               Stream blueprint logs        │  │
│  │  │   │   └── slash.ts              /nemoclaw chat command       │  │
│  │  │   └── blueprint/                                             │  │
│  │  │       ├── resolve.ts            Version resolution           │  │
│  │  │       ├── fetch.ts              OCI registry download        │  │
│  │  │       ├── verify.ts             Digest verification          │  │
│  │  │       ├── exec.ts               Subprocess execution         │  │
│  │  │       └── state.ts              Persistent run IDs           │  │
│  │  ├── openclaw.plugin.json          Plugin manifest              │  │
│  │  └── package.json                                               │  │
│  │                                                                 │  │
│  │  Role: Small, stable. Registers slash command + inference       │  │
│  │  provider inside the sandbox. Delegates to blueprint.           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  BLUEPRINT (Python artifact, own release stream)                │  │
│  │                                                                 │  │
│  │  nemoclaw-blueprint/                                            │  │
│  │  ├── blueprint.yaml           Manifest (version, profiles,      │  │
│  │  │                             compatibility constraints)       │  │
│  │  ├── orchestrator/                                              │  │
│  │  │   └── runner.py            CLI runner (plan / apply / status)│  │
│  │  └── policies/                                                  │  │
│  │      └── openclaw-sandbox.yaml  Default network + filesystem    │  │
│  │                                  policy                         │  │
│  │                                                                 │  │
│  │  Role: Contains all orchestration logic. Drives OpenShell CLI.  │  │
│  │  Evolves independently of the plugin.                           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### Blueprint Lifecycle

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ RESOLVE  │───▶│ VERIFY   │───▶│  PLAN    │───▶│  APPLY   │───▶│ STATUS   │
│          │    │          │    │          │    │          │    │          │
│ Locate   │    │ Check    │    │ Determine│    │ Execute  │    │ Report   │
│ artifact,│    │ artifact │    │ OpenShell│    │ plan via │    │ current  │
│ check    │    │ digest   │    │ resources│    │ openshell│    │ state    │
│ version  │    │          │    │ to create│    │ CLI      │    │          │
│ compat.  │    │          │    │ or update│    │ commands │    │          │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
```

---

## 5  Sandbox Creation & Lifecycle

When you run `nemoclaw onboard`, the following sequence occurs:

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Sandbox Creation Flow                             │
│                                                                      │
│  ┌──────────────┐                                                    │
│  │  1. PREFLIGHT│  • Check Docker running                            │
│  │     CHECKS   │  • Verify cgroup v2 config (DGX Spark / WSL2)      │
│  │              │  • Validate Node.js version                        │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌────────────────┐                                                  │
│  │  2. CREDENTIALS│  • Prompt for NVIDIA API key                     │
│  │                │  • Save to ~/.nemoclaw/credentials.json          │
│  └──────┬─────────┘                                                  │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  3. GATEWAY  │  • Create OpenShell gateway                        │
│  │     SETUP    │  • Register inference providers                    │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  4. BLUEPRINT│  • Download from OCI registry                      │
│  │     RESOLVE  │  • Verify digest                                   │
│  │              │  • Check version compatibility                     │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  5. SANDBOX  │  • Pull container image                            │
│  │     CREATE   │  • Apply filesystem policy                         │
│  │              │  • Apply network policy                            │
│  │              │  • Configure inference routing                     │
│  │              │  • Install NemoClaw plugin in sandbox              │
│  └──────┬───────┘                                                    │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────┐                                                    │
│  │  6. RUNNING  │  • OpenClaw agent active inside sandbox            │
│  │     SANDBOX  │  • All policies enforced                           │
│  │              │  • Inference routed through OpenShell              │
│  └──────────────┘                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

### Sandbox Environment

The sandbox runs the `ghcr.io/nvidia/openshell-community/sandboxes/openclaw`
container image. Inside the sandbox:

- OpenClaw runs with the NemoClaw plugin pre-installed
- Inference calls are routed through OpenShell to the configured provider
- Network egress is restricted by the baseline policy
- Filesystem access: `/sandbox` and `/tmp` = read-write; system paths = read-only
- Kernel isolation: Landlock LSM + seccomp + network namespaces

---

## 6  Inference Routing

Inference requests from the agent never leave the sandbox directly. OpenShell
intercepts every call and routes it to the configured provider.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Inference Routing                                  │
│                                                                       │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────────────┐  │
│  │  OpenClaw   │    │  OpenShell       │    │  NVIDIA Endpoints    │  │
│  │  Agent      │───▶│  Gateway         │───▶│  (build.nvidia.com)  │  │
│  │  (sandbox)  │    │                  │    │                      │  │
│  │             │    │  • strip sandbox │    │  Nemotron 3 Super    │  │
│  │  calls      │    │    credentials   │    │  120B (default)      │  │
│  │  inference  │    │  • inject backend│    │                      │  │
│  │  .local     │    │    credentials   │    │  Also available:     │  │
│  │             │    │  • route to      │    │  • Nemotron Ultra    │  │
│  │             │    │    provider      │    │    253B              │  │
│  │             │    │                  │    │  • Nemotron Super    │  │
│  └─────────────┘    └─────────────────┘    │    49B v1.5          │   │
│                                             │  • Nemotron Nano     │  │
│                                             │    30B               │  │
│                                             └──────────────────────┘  │
│                                                                       │
│  Model switching at runtime (no restart needed):                      │
│  $ openshell inference set --provider nvidia-nim \                    │
│      --model nvidia/nemotron-3-super-120b-a12b                        │
└───────────────────────────────────────────────────────────────────────┘
```

### Available Models (via nvidia-nim provider)

| Model ID | Label | Context Window | Max Output |
|----------|-------|----------------|------------|
| `nvidia/nemotron-3-super-120b-a12b` | Nemotron 3 Super 120B | 131,072 | 8,192 |
| `nvidia/llama-3.1-nemotron-ultra-253b-v1` | Nemotron Ultra 253B | 131,072 | 4,096 |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | Nemotron Super 49B v1.5 | 131,072 | 4,096 |
| `nvidia/nemotron-3-nano-30b-a3b` | Nemotron 3 Nano 30B | 131,072 | 4,096 |

---

## 7  Network & Filesystem Policy

### Policy Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Policy Enforcement Stack                           │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Application Layer                                              │  │
│  │  • OpenShell proxy intercepts every outbound connection         │  │
│  │  • Identifies calling binary                                    │  │
│  │  • For inference.local: strips/injects credentials, routes      │  │
│  │  • For all else: queries policy engine                          │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Infrastructure Layer                                           │  │
│  │  • Docker container isolation                                   │  │
│  │  • Network namespace separation                                 │  │
│  │  • Filesystem mount restrictions                                │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Kernel Layer                                                   │  │
│  │  • Landlock LSM — kernel-enforced filesystem restrictions       │  │
│  │  • seccomp — blocks dangerous system calls                      │  │
│  │  • Agent runs as unprivileged user (sandbox:sandbox)            │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### Policy Types

```
┌───────────────────────────────────────────────────────────────┐
│                    Policy Structure                           │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  STATIC (locked at sandbox creation)                     │ │
│  │                                                          │ │
│  │  filesystem_policy:                                      │ │
│  │    read_only: [/usr, /lib, /etc]                         │ │
│  │    read_write: [/sandbox, /tmp]                          │ │
│  │                                                          │ │
│  │  landlock:                                               │ │
│  │    compatibility: best_effort                            │ │
│  │                                                          │ │
│  │  process:                                                │ │
│  │    run_as_user: sandbox                                  │ │
│  │    run_as_group: sandbox                                 │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  DYNAMIC (hot-reloadable on running sandbox)             │ │
│  │                                                          │ │
│  │  network_policies:                                       │ │
│  │    my_api:                                               │ │
│  │      name: my-api                                        │ │
│  │      endpoints:                                          │ │
│  │        - host: api.example.com                           │ │
│  │          port: 443                                       │ │
│  │          protocol: rest                                  │ │
│  │          tls: terminate                                  │ │
│  │      binaries:                                           │ │
│  │        - path: /usr/bin/curl                             │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  Granularity: per-binary, per-endpoint, per-method, per-path  │
│  Updates: openshell policy set <name> --policy file.yaml      │
└───────────────────────────────────────────────────────────────┘
```

### Operator Approval Flow

```
┌──────────┐    ┌───────────────┐    ┌──────────────┐    ┌───────────────┐
│  Agent   │    │  OpenShell    │    │  Policy      │    │  Operator     │
│  Process │    │  Proxy        │    │  Engine      │    │  (TUI)        │
└─────┬────┘    └──────┬────────┘    └──────┬───────┘    └──────┬────────┘
      │                │                     │                    │
      │  connect to    │                     │                    │
      │  api.new.com   │                     │                    │
      ├───────────────▶│                     │                    │
      │                │  check policy       │                    │
      │                ├────────────────────▶│                    │
      │                │                     │                    │
      │                │  DENY (no match)    │                    │
      │                │◀────────────────────┤                    │
      │                │                     │                    │
      │                │  surface blocked    │                    │
      │                │  request in TUI ────────────────────────▶│
      │                │                     │                    │
      │                │                     │  approve / deny    │
      │                │◀─────────────────────────────────────────┤
      │                │                     │                    │
      │  connection    │                     │                    │
      │  allowed       │                     │                    │
      │◀───────────────┤                     │                    │
```

---

## 8  CLI Reference

### Host Commands

| Command | Description |
|---------|-------------|
| `nemoclaw onboard` | Interactive setup wizard — creates gateway, registers inference providers, builds sandbox image |
| `nemoclaw list` | List all registered sandboxes with model, provider, and policy presets |
| `nemoclaw <name> connect` | Open interactive shell inside sandbox |
| `nemoclaw <name> status` | Show sandbox status, health, and inference config |
| `nemoclaw <name> logs [--follow]` | View/stream sandbox and blueprint logs |
| `nemoclaw <name> destroy` | Stop NIM container and delete sandbox (destructive) |
| `nemoclaw deploy <instance>` | Deploy to remote Brev GPU instance (experimental) |
| `nemoclaw <name> policy-add` | Add policy preset to sandbox |
| `nemoclaw <name> policy-list` | List available and applied policy presets |
| `nemoclaw start` | Start auxiliary services (Telegram bridge, cloudflared) |
| `nemoclaw stop` | Stop auxiliary services |
| `nemoclaw setup-spark` | DGX Spark-specific setup (cgroup v2, Docker fixes) |
| `openshell term` | Open TUI dashboard for monitoring and network approval |

### Slash Command (inside OpenClaw chat)

| Command | Description |
|---------|-------------|
| `/nemoclaw status` | Show sandbox and inference state from inside the agent |

---

## 9  Monitoring & Observability

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Monitoring Stack                                   │
│                                                                       │
│  ┌───────────────────────┐  ┌──────────────────────────────────────┐  │
│  │  nemoclaw status      │  │  nemoclaw logs --follow              │  │
│  │                       │  │                                      │  │
│  │  • Sandbox state      │  │  • Blueprint runner output           │  │
│  │  • Blueprint run ID   │  │  • Sandbox activity                  │  │
│  │  • Inference provider │  │  • Error messages                    │  │
│  │  • Model + endpoint   │  │  • Filter by source, level, time     │  │
│  │  • --json for parsing │  │                                      │  │
│  └───────────────────────┘  └──────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  openshell term  (TUI Dashboard)                                │  │
│  │                                                                 │  │
│  │  ┌──────────────────────┐  ┌─────────────────────────────────┐  │  │
│  │  │  Sandbox Status      │  │  Live Network Activity          │  │  │
│  │  │  • Running / stopped │  │  • Active connections           │  │  │
│  │  │  • Resource usage    │  │  • Blocked requests             │  │  │
│  │  └──────────────────────┘  │  • Approval prompts             │  │  │
│  │                             │  • Inference routing status     │ │  │
│  │                             └─────────────────────────────────┘ │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 10  Deployment Modes

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Deployment Options                                 │
│                                                                       │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐   │
│  │  LOCAL (Development)         │  │  DGX SPARK                   │   │
│  │                              │  │                              │   │
│  │  Host: Laptop / Desktop      │  │  Host: DGX Spark device      │   │
│  │  GPU: Optional               │  │  GPU: Grace Blackwell GB10   │   │
│  │  Setup: nemoclaw onboard     │  │  Setup: sudo nemoclaw        │   │
│  │  Good for: Testing, dev      │  │    setup-spark               │   │
│  │                              │  │  Good for: Always-on,        │   │
│  │  macOS: Colima / Docker      │  │    local inference           │   │
│  │  Linux: Docker               │  │                              │   │
│  │  Windows: Docker Desktop     │  │  128GB unified memory        │   │
│  │    (WSL backend)             │  │  Scales to 4 nodes           │   │
│  └──────────────────────────────┘  └──────────────────────────────┘   │
│                                                                       │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐   │
│  │  BREV (Cloud GPU)            │  │  REMOTE SSH                  │   │
│  │                              │  │                              │   │
│  │  Host: NVIDIA Brev instance  │  │  Host: Any Linux machine     │   │
│  │  GPU: Configurable           │  │  GPU: Optional               │   │
│  │  Setup: nemoclaw deploy      │  │  Setup: openshell gateway    │   │
│  │    <instance-name>           │  │    start --remote user@host  │   │
│  │  Good for: Always-on,        │  │  Good for: Custom infra,     │   │
│  │    persistent agents         │  │    on-prem servers           │   │
│  │                              │  │                              │   │
│  │  Default GPU: A100           │  │                              │   │
│  │  Includes: Docker + NVIDIA   │  │                              │   │
│  │    Container Toolkit         │  │                              │   │
│  └──────────────────────────────┘  └──────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 11  Workspace Files

NemoClaw sandboxes include workspace files that persist agent state:

| File | Purpose |
|------|---------|
| `SOUL.md` | Agent personality and identity |
| `USER.md` | User profile and preferences |
| `IDENTITY.md` | Agent identity configuration |
| `AGENTS.md` | Multi-agent configuration |
| `MEMORY.md` | Persistent memory notes |
| Daily memory notes | Timestamped memory entries |

**Warning:** `nemoclaw destroy` permanently deletes all workspace files.
Back up before destroying a sandbox.

---

## 12  Relationship to OpenShell

```
┌───────────────────────────────────────────────────────────────────────┐
│                    NemoClaw ↔ OpenShell Relationship                  │
│                                                                       │
│                    ┌─────────────────────────────────┐                │
│                    │  NemoClaw                       │                │
│                    │  "The setup harness"            │                │
│                    │                                 │                │
│                    │  • Automates OpenClaw deployment│                │
│                    │  • Configures inference routing │                │
│                    │  • Applies default policies     │                │
│                    │  • Provides setup wizard        │                │
│                    │  • Handles deployment (Brev,    │                │
│                    │    Spark)                       │                │
│                    │                                 │                │
│                    │  NOT an agent. No intelligence. │                │
│                    │  Runs at setup time, not at     │                │
│                    │  agent runtime.                 │                │
│                    └───────────────┬──────────────────┘               │
│                                    │ drives                           │
│                                    ▼                                  │
│                    ┌─────────────────────────────────┐                │
│                    │  OpenShell                      │                │
│                    │  "The runtime"                  │                │
│                    │                                 │                │
│                    │  • Sandbox creation & isolation │                │
│                    │  • Policy engine (enforcement)  │                │
│                    │  • Privacy router (inference)   │                │
│                    │  • Gateway (control plane)      │                │
│                    │  • Supports any agent, not      │                │
│                    │    just OpenClaw                │                │
│                    │                                 │                │
│                    │  NOT an agent. No intelligence. │                │
│                    │  Runs at infrastructure level.  │                │
│                    └─────────────────────────────────┘                │
│                                                                       │
│  Neither NemoClaw nor OpenShell contains agent intelligence.          │
│  The agent (OpenClaw) is a separate project that runs INSIDE the      │
│  sandbox that NemoClaw + OpenShell create.                            │
│                                                                       │
│  Key insight: OpenShell is agent-agnostic. NemoClaw is                │
│  OpenClaw-specific. For NemoClaw Escapades, we'll interact            │
│  primarily with OpenShell directly.                                   │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 13  Relationship to OpenClaw

NemoClaw wraps OpenClaw in a secure runtime. The agent (OpenClaw) runs
unmodified inside the sandbox — NemoClaw adds the security and infrastructure
layer around it. **The agent intelligence comes entirely from OpenClaw.**

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  OpenClaw              NemoClaw              OpenShell           │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐    │
│  │  The Agent   │      │  The Harness │      │  The Runtime │    │
│  │  (THE BRAIN) │      │  (setup only)│      │  (infra only)│    │
│  │              │      │              │      │              │    │
│  │  • AI logic  │      │  • Blueprint │      │  • Sandbox   │    │
│  │  • Skills    │  ──▶ │  • Setup     │  ──▶ │  • Policy    │    │
│  │  • Memory    │      │  • Deploy    │      │  • Privacy   │    │
│  │  • Gateway   │      │  • Monitor   │      │  • Gateway   │    │
│  │  • Tools     │      │              │      │              │    │
│  │  • Cron      │      │              │      │              │    │
│  │  • Sub-agents│      │              │      │              │    │
│  └──────────────┘      └──────────────┘      └──────────────┘    │
│                                                                  │
│  "What runs"           "How it's deployed"   "Where it runs"     │
│  (all intelligence)    (no intelligence)     (no intelligence)   │
└──────────────────────────────────────────────────────────────────┘
```

### What Lives in Each Layer

| Concern | OpenClaw (agent) | NemoClaw (harness) | OpenShell (runtime) |
|---------|:----------------:|:------------------:|:-------------------:|
| Agent loop (prompt → LLM → tools) | **Yes** | No | No |
| Skills system (SKILL.md) | **Yes** | No | No |
| Memory (MEMORY.md, USER.md) | **Yes** | No | No |
| Messaging (Slack, Telegram, etc.) | **Yes** | No | No |
| Cron scheduler | **Yes** | No | No |
| Sub-agent delegation | **Yes** | No | No |
| Tools (fs, terminal, browser, etc.) | **Yes** | No | No |
| Self-learning loop | **No** (Hermes has this) | No | No |
| Blueprint (setup orchestration) | No | **Yes** | No |
| Default policies (YAML) | No | **Yes** | No |
| `/nemoclaw` plugin | No | **Yes** | No |
| Sandbox creation & isolation | No | No | **Yes** |
| Policy engine (enforcement) | No | No | **Yes** |
| Inference routing | No | No | **Yes** |
| Credential injection | No | No | **Yes** |

The NemoClaw repo's `orchestrator/runner.py` is an **infrastructure
orchestrator** — it runs `openshell` CLI commands to create sandboxes and
apply policies. It is not an agent orchestrator. The word "orchestrator" in
that context means "setup script that coordinates infrastructure steps," not
"super-agent that reasons about tasks and delegates to sub-agents."

---

## 14  Setup & Installation

### Prerequisites

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 4 vCPU | 4+ vCPU |
| RAM | 8 GB | 16 GB |
| Disk | 20 GB free | 40 GB free |

| Software | Version |
|----------|---------|
| Linux | Ubuntu 22.04 LTS or later |
| Node.js | 20 or later |
| npm | 10 or later |
| Docker | Running |
| OpenShell | Installed |

### Quick Install

```bash
# One-line installer
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash

# After install
nemoclaw my-assistant connect    # connect to sandbox
nemoclaw my-assistant status     # check health
nemoclaw my-assistant logs -f    # stream logs
```

### Platform Support

| Platform | Supported Runtimes |
|----------|--------------------|
| Linux | Docker |
| macOS (Apple Silicon) | Colima, Docker Desktop |
| macOS (Podman) | Not yet supported |
| Windows WSL | Docker Desktop (WSL backend) |

---

## 15  Answers to Design Doc Questions

### Q6: Does NemoClaw provide a harness? Or computer use?

**NemoClaw provides a harness, not computer use.** NemoClaw is an orchestration
and security layer — it wraps OpenClaw in a sandboxed runtime with policy
controls, inference routing, and lifecycle management. It does not provide
computer-use capabilities (screen control, mouse/keyboard). The "computer use"
comes from the agent itself (OpenClaw) and the tools available to it within the
sandbox.

The harness includes:
- Sandboxed execution (Landlock + seccomp + network namespaces)
- Declarative network policy with operator approval
- Managed inference routing
- Blueprint lifecycle management
- Deployment to remote GPU instances

### Q7: What should be the "main brain" — where does the orchestrator run?

**The orchestrator runs inside the OpenShell sandbox**, managed by NemoClaw.
For NemoClaw Escapades, there are several options:

1. **Local (development):** Run on a laptop/desktop with Docker. Good for
   testing but not always-on.
2. **NVIDIA Brev (recommended for always-on):** Use `nemoclaw deploy` to
   provision a GPU instance that runs persistently. See the
   [Hosting Deep Dive](hosting_deep_dive.md) for details.
3. **DGX Spark:** Ideal for always-on local deployment with GPU inference.
4. **Remote SSH:** Any Linux machine with Docker.

The agent loop itself lives in OpenClaw (or a custom orchestrator). NemoClaw
just provides the managed runtime environment. For a Hermes-style custom
orchestrator, we'd run it inside an OpenShell sandbox with policies that allow
access to the services it needs (Slack, GitLab, inference endpoints, etc.).

### Q8: Can existing slackbot workflows convert to NemoClaw policies?

**Partially.** NemoClaw policies are primarily about security and access control
(which network endpoints, which binaries, which filesystem paths). Slackbot
*workflow logic* would need to be reimplemented as OpenClaw skills or agent
instructions, not as NemoClaw policies.

However, the network policy aspect is relevant: if a slackbot workflow calls
external APIs, those endpoints need to be declared in the NemoClaw network
policy. The conversion would be:

- **Workflow logic** → OpenClaw skills / agent instructions
- **API access requirements** → NemoClaw network policy entries
- **Credentials** → OpenShell provider configuration

### Q10: Should each workflow run in its own sandbox container?

**Yes, this is the recommended pattern.** OpenShell sandboxes are designed to be
lightweight and ephemeral. Each sandbox provides:
- Isolated filesystem
- Independent network policy
- Separate inference routing
- Its own credential scope

For NemoClaw Escapades, the architecture would be:
- One orchestrator sandbox (always-on, broad permissions)
- Per-workflow sandboxes (ephemeral, minimal permissions)
- The orchestrator spawns workflow sandboxes as needed

### Q11: How to set up Claude Code in an OpenShell container?

**OpenShell has first-class Claude Code support:**

```bash
openshell sandbox create -- claude
```

This creates a sandbox with Claude Code running inside it. The CLI auto-detects
`ANTHROPIC_API_KEY` from the environment. The sandbox provides:
- Filesystem isolation
- Network policy (allow Anthropic API endpoint)
- All Claude Code features (file editing, terminal, etc.)

For the coding agent in NemoClaw Escapades, the orchestrator would:
1. Create an OpenShell sandbox: `openshell sandbox create -- claude`
2. Upload the relevant code files
3. Send the coding task via the sandbox shell
4. Download the results (PR/patch)
5. Destroy the sandbox after completion

---

## 16  What NemoClaw Means for This Project

### NemoClaw provides the infrastructure layer

NemoClaw + OpenShell answer the "where does the agent run" and "how do we keep
it safe" questions. They do *not* provide the agentic intelligence (skills,
memory, self-learning). That comes from the agent itself (OpenClaw, Hermes, or
a custom orchestrator).

### Mapping to Milestones

| Milestone | NemoClaw Role |
|-----------|---------------|
| M1 — Foundation | Use NemoClaw to deploy the orchestrator in a sandboxed environment. Configure network policy to allow Slack API and inference endpoints. |
| M2 — Knowledge Mgmt | Add SecondBrain API endpoint to network policy. |
| M3 — Coding Agent | Use `openshell sandbox create -- claude` for coding sub-agent sandboxes. |
| M4 — Self-Improvement | Workspace files (MEMORY.md, USER.md) already supported by NemoClaw. Extend with Hermes-style skills. |
| M5 — Review Agent | Spawn review agent in its own sandbox, communicate via shared filesystem or API. |
| M6 — Professional KB | Add Slack API, Teams API to network policy for scraping. |

### Key Architectural Decision

```
┌───────────────────────────────────────────────────────────────┐
│  Option A: Use NemoClaw as-is (wraps OpenClaw)                │
│                                                               │
│  Pros: Quick setup, batteries included, OpenClaw ecosystem    │
│  Cons: Tied to OpenClaw, limited customization of agent loop  │
│                                                               │
│  Option B: Use OpenShell directly (custom orchestrator)       │
│                                                               │
│  Pros: Full control over agent loop, can implement            │
│    Hermes-style self-learning, custom skills/memory           │
│  Cons: More work, must build orchestrator from scratch        │
│                                                               │
│  Recommended: Start with Option A for Milestone 1             │
│  (fastest path to a working system), then evaluate whether    │
│  to switch to Option B for Milestone 4 when the               │
│  self-learning loop is needed.                                │
└───────────────────────────────────────────────────────────────┘
```

---

### Sources

- [NemoClaw Overview](https://docs.nvidia.com/nemoclaw/latest/about/overview.html)
- [How NemoClaw Works](https://docs.nvidia.com/nemoclaw/latest/about/how-it-works.html)
- [NemoClaw Architecture](https://docs.nvidia.com/nemoclaw/latest/reference/architecture.html)
- [NemoClaw Quickstart](https://docs.nvidia.com/nemoclaw/latest/get-started/quickstart.html)
- [NemoClaw Commands Reference](https://docs.nvidia.com/nemoclaw/latest/reference/commands.html)
- [NemoClaw Remote Deploy](https://docs.nvidia.com/nemoclaw/latest/deployment/deploy-to-remote-gpu.html)
- [Switch Inference Providers](https://docs.nvidia.com/nemoclaw/latest/inference/switch-inference-providers.html)
- [Monitor Sandbox Activity](https://docs.nvidia.com/nemoclaw/latest/monitoring/monitor-sandbox-activity.html)
- [NVIDIA NemoClaw GitHub](https://github.com/NVIDIA/NemoClaw)
- [NVIDIA OpenShell Blog Post](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/)
