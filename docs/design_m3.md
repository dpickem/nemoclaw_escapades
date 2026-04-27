# Milestone 3 — Multi-Sandbox Delegation, Review Agent & Skill Auto-Policy

> **Predecessor:** [Milestone 2b — Multi-Agent Orchestration](design_m2b.md)
>
> **Successor:** [Milestone 4 — Note-Taking & Professional KB](design.md#milestone-4--note-taking--professional-knowledge-base)
>
> **Last updated:** 2026-04-25

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [Architecture](#3--architecture)
4. [Sandbox Lifecycle (Multi-Sandbox)](#4--sandbox-lifecycle-multi-sandbox)
5. [Manifest — Declarative Workspace Contract](#5--manifest--declarative-workspace-contract)
6. [Per-Role Policies and Skill Auto-Policy](#6--per-role-policies-and-skill-auto-policy)
7. [Policy Hot-Reload](#7--policy-hot-reload)
8. [Artifact Transport: Upload, Download, Snapshots](#8--artifact-transport-upload-download-snapshots)
9. [Review Agent](#9--review-agent)
10. [Coding ↔ Review Local-Collaboration Loop](#10--coding--review-local-collaboration-loop)
11. [Lazy Skills (`load_skill`)](#11--lazy-skills-load_skill)
12. [Audit and Observability (Multi-Sandbox)](#12--audit-and-observability-multi-sandbox)
13. [End-to-End Walkthrough](#13--end-to-end-walkthrough)
14. [Implementation Plan](#14--implementation-plan)
15. [Testing Plan](#15--testing-plan)
16. [Risks and Mitigations](#16--risks-and-mitigations)
17. [Open Questions](#17--open-questions)

---

## 1  Overview

Milestone 3 generalises the M2b protocol to **per-sub-agent sandboxes**:
each coding or review task spawns its own OpenShell sandbox via
`openshell sandbox create`, with a role-specific policy and an isolated
filesystem.  The NMB protocol (`task.assign`, `task.progress`,
`task.complete`) is **unchanged** — only the spawn mechanism, the
artifact-transport layer, and the per-sandbox policy story are new.

Three new capabilities land alongside the multi-sandbox spawn:

1. **Skill-driven auto-policy.**  Skills declare a
   `nemoclaw.infrastructure` block (network endpoints, filesystem
   paths, binaries) in their `SKILL.md` frontmatter; a generator
   produces the per-sandbox OpenShell policy automatically.  Fallback:
   deny-and-approve discovery via the OpenShell TUI.
2. **Policy hot-reload.**  Sub-agents request additional permissions
   mid-task via `policy.request` NMB messages; the orchestrator
   auto-approves known-safe endpoints (PyPI, npm registry) or
   escalates to the user via Slack.  No sandbox teardown required.
3. **Review agent.**  A second sub-agent type that consumes the
   coding agent's diffs and emits structured feedback.  Coding and
   review iterate via NMB diff exchange — no Git round-trips until
   the orchestrator decides to push.

M3 also adopts two design primitives from the
[OpenAI Agents SDK](deep_dives/openai_agents_sdk_deep_dive.md):

- **`Manifest`** — a Pydantic-validated dataclass that declares a
  fresh sandbox's workspace contract (entries, env vars, users,
  permissions).  Replaces M2b's ad-hoc `setup-workspace.sh` + scattered
  `coding.*` config.
- **Lazy `load_skill`** — skill metadata in the system prompt; bodies
  + scripts/references/assets copied into the workspace only when the
  model calls `load_skill(skill_name)`.

> **Scope note:** M3 keeps the NMB protocol and the `AgentLoop` from
> M2a/M2b unchanged.  The work is concentrated in (a) the sandbox
> lifecycle (M2b's `subprocess.spawn` becomes `openshell sandbox
> create`), (b) per-sandbox policy and credential management, and
> (c) the review agent's role-specific tool surface and its
> coordination protocol with the coding agent.

### What was promoted into M3

| Feature | Original Target | Rationale |
|---------|-----------------|-----------|
| `Manifest`-style workspace contract | M5+ (informally) | The OpenAI Agents SDK validated the primitive; doing it inline with M3 multi-sandbox is cheaper than retrofitting later. |
| Lazy `load_skill` capability | M6 | Cost is ~1 day; pays off the first time a sub-agent touches a skill bundle larger than ~500 lines. M3 introduces multi-sandbox sub-agents whose token budget is more sensitive than the orchestrator's. |
| Skill auto-policy (`nemoclaw.infrastructure` blocks) | M6 (self-learning loop) | The mechanism is independent of self-learning. M3 needs per-sandbox policies anyway; auto-generating them from skill metadata is the right time to land it. |

### What stays deferred

- Skill auto-creation (`skillify`) — M6, with the rest of the
  self-learning loop.
- Auto-policy refinement based on observed denials — M6.
- Multi-host sandbox deployment (cross-machine NMB routing) — M5+.
- Snapshots for crash recovery — M3 introduces the `Snapshot`
  primitive but uses it only for restartable session resume; full
  workspace persistence and remote (S3) snapshots are M5+.

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. **Multi-sandbox spawn**: orchestrator delegates by calling
   `openshell sandbox create` instead of `subprocess.Popen`. Each
   sub-agent runs in its own filesystem and policy boundary.
2. **Per-role policy files**: `policies/coding-agent.yaml`,
   `policies/review-agent.yaml`. The orchestrator selects and
   merges policy at spawn time.
3. **Skill auto-policy**: skills with a `nemoclaw.infrastructure`
   block in their frontmatter contribute to the spawned sandbox's
   policy.  Fallback: OpenShell deny-and-approve discovery.
4. **Policy hot-reload**: `policy.request` / `policy.updated` /
   `policy.denied` NMB message types; auto-approval allowlist with
   user escalation.
5. **Artifact transport**: replace M2b's same-sandbox filesystem
   reads with `openshell sandbox upload` (workspace seeding) and
   `openshell sandbox download` (result collection, audit fallback,
   git state).
6. **`Manifest`-style workspace contract**: a Pydantic dataclass
   bundling entries (Files, LocalDirs, GitRepos), environment vars,
   sandbox users, and `extra_path_grants`. Replaces
   `setup-workspace.sh` + scattered `coding.*` config.
7. **Review agent**: a second sub-agent type with a read-only-on-the-
   coding-workspace tool surface. Consumes diffs, emits structured
   feedback.
8. **Coding ↔ Review collaboration loop**: NMB-mediated diff
   exchange. Two sandboxes iterate without Git round-trips until the
   orchestrator decides to push.
9. **Post-push review integration**: after `push_and_create_pr`, the
   review agent posts review comments on the resulting Gerrit/GitLab
   PR via nv-tools.
10. **Lazy `load_skill`**: skill metadata in the system prompt; bodies
    materialised on demand into the sub-agent's sandbox.
11. **Maintain audit, approval, and safety guarantees** from M1/M2.

### 2.2 Non-Goals

1. **Multi-host NMB** — single-host only. Cross-machine routing is M5+.
2. **Skill auto-creation** (`skillify`) — M6 with the self-learning loop.
3. **Auto-policy refinement** — M6.
4. **Memory system** — full Honcho/SecondBrain integration is M5.
5. **Web UI updates** — incremental; M3 lands per-sandbox panels and
   policy-request UI but the kanban/diff/cron surfaces stay as M2b
   shipped them.
6. **Remote snapshots (S3, Azure)** — M5 alongside the long-term
   memory layer.

---

## 3  Architecture

### 3.1 Process and Sandbox Topology (M3)

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Host / Brev / DGX Spark                                                  │
│                                                                           │
│  ┌─────────────────────────────────────┐                                  │
│  │  OpenShell Gateway (control plane)  │                                  │
│  │  - openshell sandbox create/delete  │                                  │
│  │  - openshell sandbox upload/download│                                  │
│  │  - openshell policy set --wait      │                                  │
│  └─────────────────────────────────────┘                                  │
│           │           │           │           │                           │
│           ▼           ▼           ▼           ▼                           │
│  ┌───────────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐              │
│  │ Orchestrator  │ │  Coding   │ │  Review   │ │ NMB       │              │
│  │ Sandbox       │ │  Sandbox  │ │  Sandbox  │ │ Broker    │              │
│  │ (always-on)   │ │  (task A) │ │  (task A) │ │ Sandbox   │              │
│  │               │ │           │ │           │ │           │              │
│  │ Slack         │ │ AgentLoop │ │ AgentLoop │ │ ws server │              │
│  │ AgentLoop     │ │  + coding │ │  + review │ │ at        │              │
│  │ Finalization  │ │   tools   │ │   tools   │ │ messages  │              │
│  │ DelegationMgr │ │           │ │           │ │ .local    │              │
│  │ AuditDB       │ │ AuditBuf  │ │ AuditBuf  │ │           │              │
│  └───────┬───────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘              │
│          │               │             │             │                    │
│          └───────────────┴─────────────┴─────────────┘                    │
│                          NMB (ws://messages.local:9876)                   │
│                          - task.assign / .complete / .progress            │
│                          - policy.request / .updated / .denied            │
│                          - audit.flush                                    │
│                          - diff.exchange (coding ↔ review, §10)           │
└───────────────────────────────────────────────────────────────────────────┘
```

Three things to internalize about the topology:

1. **Sandboxes are siblings, not nested.**  OpenShell does not support
   nested sandboxes; parent-child relationships live at the
   application layer in NMB metadata (`parent_sandbox_id`,
   `workflow_id`).  The orchestrator's sandbox is just another peer
   on the bus.
2. **Each sub-agent's sandbox is ephemeral.**  Coding and review
   sandboxes live for the duration of one task (or a few iterations);
   a TTL watchdog cleans them up if `task.complete` is missed.
3. **NMB is the only inter-sandbox channel.**  No shared filesystem,
   no `pipe()` between processes — just the WebSocket broker.  This
   is what makes the "same protocol, different spawn mechanism"
   property hold across M2b → M3.

### 3.2 Component Map

Components introduced or extended in M3:

| Component | Owned by | Extends | Description |
|-----------|----------|---------|-------------|
| `SandboxClient` | M3 | new | Wrapper around `openshell sandbox` CLI commands. `create()`, `start()`, `delete()`, `upload()`, `download()`, `policy_set()`. |
| `Manifest` | M3 | new | Pydantic dataclass. Workspace contract: entries, env, users, `extra_path_grants`. |
| `PolicyBuilder` | M3 | new | Merges per-role base policy + skill `nemoclaw.infrastructure` blocks + per-task overlay → final OpenShell YAML. |
| `PolicyOverlay` | M3 | new | Per-sandbox runtime additions from `policy.request`. Hot-reloaded via `openshell policy set --wait`. |
| `ReviewAgent` | M3 | M2a `AgentLoop` | New agent role. Read-only tool surface against the coding workspace. |
| `DelegationManager` | M2b | extended | Spawn path now branches: same-sandbox subprocess (M2b mode, kept for tests) vs. multi-sandbox CLI (M3 prod). |
| `AuditFlush` | M2b | extended | Adds `openshell sandbox download` fallback for the audit JSONL when NMB-batched flush misses records. |
| `Skills` capability | M2a | extended | Adds lazy `load_skill` tool; `nemoclaw.infrastructure` block parsing for auto-policy. |

Components **unchanged** from M2b:

- `AgentLoop`
- NMB protocol (the message types `policy.*` and `diff.*` are new
  but they're additions, not protocol changes)
- `LayeredPromptBuilder`
- `ToolRegistry` and `tool_search`
- Finalization tools

---

## 4  Sandbox Lifecycle (Multi-Sandbox)

*Original specification: [design_m2.md §5.1](design_m2.md#51--spawn-sequence).
Reproduced and extended here for the M3 record.*

### 4.1 Spawn Sequence

When the orchestrator delegates to a coding agent, it runs:

```
 Orchestrator                  Gateway              Coding Sandbox       NMB Broker
     │                             │                     │                  │
     │ 1. Build Manifest + policy  │                     │                  │
     │   from base + skills + task │                     │                  │
     │                             │                     │                  │
     │ 2. openshell sandbox create │                     │                  │
     │   --name coding-<wf_id>     │                     │                  │
     │   --policy <generated>.yaml │                     │                  │
     │   --from <coding-image>     │                     │                  │
     │────────────────────────────▶│ provision sandbox ─▶│                  │
     │                             │                     │                  │
     │ 3. openshell sandbox upload │                     │                  │
     │   <generated_manifest_dir>  │                     │                  │
     │   /sandbox/.config/         │                     │                  │
     │────────────────────────────▶│ write files ───────▶│                  │
     │                             │                     │                  │
     │ 4. openshell sandbox exec   │                     │                  │
     │   coding-<wf_id>            │                     │                  │
     │   /app/setup-workspace.sh   │                     │                  │
     │────────────────────────────▶│ run script ────────▶│                  │
     │                             │                     │ connects to NMB  │
     │                             │                     │─────────────────▶│
     │                             │                     │ sandbox.ready    │
     │                             │                     │─────────────────▶│
     │                             │                     │                  │
     │◀────────────────────────────────────────────────────────────────────│
     │              sandbox.ready received                                  │
     │                                                                      │
     │ 5. NMB: task.assign         │                     │                  │
     │   { prompt, max_turns, ... }│                     │                  │
     │─────────────────────────────────────────────────────────────────────▶│
     │                             │                     │◀─────────────────│
     │                             │                     │                  │
     │      ... agent works ...    │                     │                  │
     │                             │                     │                  │
     │ 6. NMB: task.complete       │                     │                  │
     │◀─────────────────────────────────────────────────────────────────────│
     │                                                                      │
     │ 7. download artifacts, ingest audit fallback, delete sandbox        │
```

Steps 1, 2, 3, 4, 7 are the M3-specific delta from M2b.  Steps 5 and 6
are unchanged.

### 4.2 Sandbox Identity and Naming

| Sandbox | Naming pattern | Lifetime |
|---|---|---|
| Orchestrator | `orchestrator` (singleton) | Long-lived; restarts only on deploy |
| Coding | `coding-<workflow_id>` | One workflow's worth of iterations (until `discard_work` or `push_and_create_pr`) |
| Review | `review-<workflow_id>` | Lifetime tied to the paired coding sandbox |
| NMB Broker | `nmb-broker` | Long-lived |

The `<workflow_id>` is a UUID generated by the orchestrator at task
delegation time; it threads through every NMB message and audit record
for the workflow.

### 4.3 Cleanup

On `task.complete`, error, or TTL expiry the orchestrator:

1. Calls `openshell sandbox download <child> /sandbox/audit_fallback.jsonl`
   and ingests records the NMB-batched flush missed.
2. Calls `openshell sandbox download <child> /sandbox/artifacts/`
   for diffs, summaries, and git state (Strategy A in §10.4 of M2 doc).
3. Optionally captures a `LocalSnapshot` (§8.3) before destroying the
   sandbox, so the next iteration can resume from the saved workspace.
4. Calls `openshell sandbox delete <child>`.

The TTL watchdog (M2b operational cron) reaches the same path if
`task.complete` is never received.

---

## 5  Manifest — Declarative Workspace Contract

### 5.1 Motivation

M2b ships workspace seeding as a shell script (`setup-workspace.sh`)
plus three scattered config fields (`coding.workspace_root`,
`coding.git_clone_allowed_hosts`, `skills.skills_dir`).  This works for
the same-sandbox case because the orchestrator's already-set-up
filesystem is reused.  Multi-sandbox doesn't have that luxury — each
sub-agent's sandbox starts empty, and "what should be in the
filesystem at task-start time" needs a single source of truth.

The OpenAI Agents SDK's `Manifest` is the right primitive (see
[deep dive §5](deep_dives/openai_agents_sdk_deep_dive.md#5--manifests-a-first-class-workspace-contract)).
M3 adopts it.

### 5.2 The `Manifest` Dataclass

```python
# src/nemoclaw_escapades/sandbox/manifest.py
from pydantic import BaseModel, Field

class Manifest(BaseModel):
    """Declarative contract for a fresh sandbox's workspace.

    Workspace-relative paths only; absolute paths and ``..`` escapes
    are rejected by the validator.
    """
    version: int = 1
    root: str = "/sandbox/workspace"
    entries: dict[str, BaseEntry] = Field(default_factory=dict)
    environment: dict[str, str | EnvPlaceholder] = Field(default_factory=dict)
    users: list[User] = Field(default_factory=list)
    extra_path_grants: tuple[PathGrant, ...] = Field(default_factory=tuple)
```

`BaseEntry` is a tagged union — exactly the SDK's split:

| Entry type | Materializes |
|---|---|
| `File(content=bytes)` | Synthetic file with literal bytes |
| `Dir()` | Empty directory (e.g., `output/`, `artifacts/`) |
| `LocalFile(src=Path)` | Copy a host file into the sandbox |
| `LocalDir(src=Path)` | Copy a host directory subtree |
| `GitRepo(repo, ref, allowed_hosts)` | `git clone` inside the sandbox; uses M2b's `git_clone` allowlist |

`EnvPlaceholder` flags an env var as an OpenShell-provider placeholder
that the L7 proxy resolves at HTTP-request time (i.e., do **not**
substitute on the host before upload).  Concrete value strings are
substituted as-is.

### 5.3 Example: Coding Agent Manifest

```python
manifest = Manifest(
    root="/sandbox/workspace",
    entries={
        "repo": GitRepo(
            repo=task.repo_url,
            ref=task.branch,
            allowed_hosts=cfg.coding.git_clone_allowed_hosts,
        ),
        ".agents/skills": LocalDir(src=cfg.skills.skills_dir),
        "artifacts": Dir(),
        "audit_fallback.jsonl": File(content=b""),
    },
    environment={
        "GITLAB_TOKEN": EnvPlaceholder("GITLAB_TOKEN"),
        "WORKFLOW_ID": task.workflow_id,
    },
    users=[User(name="agent")],
    extra_path_grants=(PathGrant(path="/tmp"),),
)
```

The orchestrator builds this dataclass in Python, calls
`materialize(manifest, sandbox_id)`, and the helper translates each
entry into the corresponding `openshell sandbox upload` /
`openshell sandbox exec git clone` calls.

### 5.4 Validation

Pydantic enforces:

- `entries` keys are workspace-relative POSIX paths; absolute paths
  raise `InvalidManifestPathError(reason="absolute")`.
- Path components do not include `..`; raises
  `reason="escape_root"`.
- `EnvPlaceholder` names match a registered OpenShell provider; raises
  `UnknownProviderError` at build time, not when the sub-agent first
  tries to use the credential.
- `extra_path_grants` paths are absolute and outside the workspace
  root (the whole point of `extra_path_grants` is paths the workspace
  can't otherwise reach).

These checks move bug-detection from "first failed tool call inside
the sandbox" to "orchestrator startup or task delegation".

### 5.5 Relationship to the OpenShell Policy

Important distinction (also called out in the SDK deep dive):

| Layer | What it expresses | Enforced by |
|---|---|---|
| `Manifest` | **Intent** — what files/env/users should be present | Orchestrator at sandbox-create time |
| OpenShell policy | **Enforcement** — what network/filesystem the sandbox process can touch | OpenShell out-of-process policy engine |

The two **must agree**.  A startup check diffs them:

- For each `extra_path_grant` in the manifest, verify the policy
  allows that path (and at the right read/write level).
- For each `GitRepo.repo` host, verify the `git_clone_allowed_hosts`
  list and the network policy match.

A mismatch logs a structured warning at sandbox-create time.  Hard
failure modes (e.g., manifest grants `/etc` write but policy denies
it) raise before the sandbox is even created.

---

## 6  Per-Role Policies and Skill Auto-Policy

### 6.1 Per-Role Base Policies

*Original specification: [design_m2.md §6.2](design_m2.md#62--policy-openshell-sandbox-policy-per-agent-role).*

Each agent role has a policy file in `policies/`:

- `policies/orchestrator.yaml` (M2b shipped this)
- `policies/coding-agent.yaml` (new in M3)
- `policies/review-agent.yaml` (new in M3)
- `policies/nmb-broker.yaml` (M2b shipped this)

The coding-agent policy declares: `inference.local`, `messages.local`,
`/usr/bin/python3`, and **no external network access by default**.
External access is added via auto-policy (skills) or hot-reload (§7).

The review-agent policy is even tighter — it does **not** need shell
or write access to the coding workspace.  It needs:

- `inference.local` (model calls)
- `messages.local` (NMB)
- Read-only access to the coding sandbox's diff (delivered via NMB,
  not filesystem mount)

### 6.2 Skill Auto-Policy

A skill can declare a `nemoclaw.infrastructure` block in its
`SKILL.md` frontmatter:

```yaml
---
name: pip-install
description: Install Python packages from PyPI
nemoclaw.infrastructure:
  network:
    - host: pypi.org
      port: 443
      protocol: rest
      tls: terminate
      access: full
    - host: files.pythonhosted.org
      port: 443
      protocol: rest
      tls: terminate
      access: full
  filesystem:
    - path: /tmp
      access: rw
  binaries:
    - /usr/bin/pip3
---

# Pip install skill body...
```

When the orchestrator delegates a task that surfaces this skill (via
`load_skill`), the `PolicyBuilder` merges the skill's block into the
sandbox's policy at spawn time.

### 6.3 The `PolicyBuilder`

```python
# src/nemoclaw_escapades/sandbox/policy_builder.py

@dataclass
class PolicyBuilder:
    base_policy: Path                       # policies/coding-agent.yaml
    skill_blocks: list[InfrastructureBlock]  # collected from surfaced skills
    overlay: PolicyOverlay = field(default_factory=PolicyOverlay)

    def render(self) -> str:
        """Merge base + skill blocks + overlay into a single YAML string.

        Conflict policy:
        - Network endpoints: union (most permissive wins).
        - Filesystem paths: union; on access conflicts, the strictest
          wins (denying writes always beats allowing them).
        - Binaries: union.
        """
```

The orchestrator passes the rendered YAML to
`openshell sandbox create --policy <path>`.

### 6.4 Auto-Policy Approval Boundary

Not every skill should be allowed to broaden the policy on its own.
A two-tier model:

| Tier | Behaviour |
|---|---|
| **Auto-allowed** | Skills whose `nemoclaw.infrastructure` block is in the orchestrator's `auto_policy_allowlist` (e.g., the bundled `pip-install`, `npm-install`, `cargo-build` skills) merge silently. |
| **Approval-required** | All other skills surface the proposed policy delta to the user via Slack at spawn time. The user can approve, deny, or "always allow this skill". Approvals are remembered in `~/.nemoclaw/skill_approvals.json`. |

This is the same Approve/Deny pattern M1 ships for write tools,
applied at policy granularity.

### 6.5 Fallback: Deny-and-Approve Discovery

For a skill **without** an `nemoclaw.infrastructure` block, the
sub-agent's first tool call that needs the missing permission fails
with the OpenShell deny error.  The agent emits a `policy.request`
NMB message (§7), which is the same flow as runtime hot-reload.  If
the user approves, the orchestrator can optionally **write the
discovered permissions back into the skill's frontmatter** so the
next run skips the discovery loop.

This puts the M6 self-learning loop's "auto-skill creation" on a
gentle ramp: M3 ships the data plane (capture what was approved); M6
ships the control plane (write it back automatically).

---

## 7  Policy Hot-Reload

*Original specification: [design_m2.md §6.3](design_m2.md#63--policy-hot-reload).*

### 7.1 Why It Matters in M3

In M2b the sub-agent shares the orchestrator's sandbox and inherits
its policy; there's nothing to hot-reload.  In M3 the sub-agent has
its own policy boundary, and real coding tasks frequently discover
mid-task that they need additional access:

- `pip install` against PyPI for a library not in the base image.
- A schema fetch from an internal API not anticipated at spawn time.
- A new git remote for an unfamiliar fork.

The alternative — failing the task and asking the user to retry with
a tweaked policy — is brutal UX.  Hot-reload keeps the sandbox alive
and adds the missing permission without losing context.

### 7.2 Request Flow

```
Sub-Agent                   NMB                Orchestrator           Gateway
   │                         │                       │                   │
   │  bash: pip install foo  │                       │                   │
   │  → policy denies pypi   │                       │                   │
   │                         │                       │                   │
   │  policy.request         │                       │                   │
   │  { reason, endpoint,    │                       │                   │
   │    tool, error_msg }    │                       │                   │
   │────────────────────────▶│──────────────────────▶│                   │
   │                         │                       │                   │
   │                         │       evaluates: auto-approve, ask, deny  │
   │                         │                       │                   │
   │                         │                       │ openshell policy  │
   │                         │                       │ set --wait        │
   │                         │                       │──────────────────▶│
   │                         │                       │◀──────────────────│
   │                         │                       │ applied           │
   │                         │                       │                   │
   │  policy.updated         │                       │                   │
   │  { added: [...] }       │                       │                   │
   │◀────────────────────────│◀──────────────────────│                   │
   │                         │                       │                   │
   │  retry: pip install foo │                       │                   │
   │  → succeeds             │                       │                   │
```

### 7.3 Message Types

| Type | Direction | Payload |
|---|---|---|
| `policy.request` | Sub-Agent → Orchestrator | `{reason, endpoint?, tool?, error_message?}` |
| `policy.updated` | Orchestrator → Sub-Agent | `{added_endpoints[], removed_endpoints[]?, revision}` |
| `policy.denied` | Orchestrator → Sub-Agent | `{reason}` |

### 7.4 Auto-Approval Allowlist

The orchestrator's auto-approval list is config-driven:

```yaml
# config/defaults.yaml
auto_policy_approval:
  endpoints:
    - { host: pypi.org, port: 443, condition: "task.lang == 'python'" }
    - { host: files.pythonhosted.org, port: 443, condition: "task.lang == 'python'" }
    - { host: registry.npmjs.org, port: 443, condition: "task.lang == 'node'" }
    - { host: index.crates.io, port: 443, condition: "task.lang == 'rust'" }
  ttl_seconds: 3600
```

Conditions are evaluated against the task payload; the
`task.lang` field is set by the orchestrator at delegation time
based on the workflow's repo metadata.  Endpoints not on the list
escalate to the user.

### 7.5 The `PolicyOverlay`

Rather than rewriting the whole policy file on every hot-reload, the
orchestrator maintains a per-sandbox **overlay**:

```python
@dataclass
class PolicyOverlay:
    sandbox_id: str
    base_policy_path: Path
    overlays: list[PolicyEntry] = field(default_factory=list)

    def add_endpoint(self, name: str, host: str, port: int, **kwargs) -> None:
        ...

    def render(self) -> str:
        """Merge base policy + overlays → final YAML string."""
        ...

    async def apply(self) -> None:
        """Write merged policy to a temp file and call openshell policy set --wait."""
        merged_path = self._write_temp_policy()
        await openshell_policy_set(self.sandbox_id, merged_path)
```

The overlay is preserved across `re_delegate` iterations within the
same sandbox, so the user only approves a permission once per
workflow.

### 7.6 Audit Trail

Every `policy.request` / `policy.updated` / `policy.denied` lands in
the audit DB with the originating workflow id, the endpoint, the
approval decision, and the user (if any) who approved it.  The same
table powers M6's auto-policy refinement (which skill descriptions
should grow which `nemoclaw.infrastructure` blocks).

---

## 8  Artifact Transport: Upload, Download, Snapshots

### 8.1 Workspace Seeding (Upload)

The orchestrator stages the manifest inside the new sandbox via:

| What | Mechanism | When |
|---|---|---|
| Skill index (lazy) | `openshell sandbox upload <child> ./skills/index.json /sandbox/.agents/index.json` | At sandbox-create time |
| Pre-loaded skill bodies | `openshell sandbox upload` per skill | When the skill is in the auto-load list |
| Lazy skills | not staged; loaded on demand via `load_skill` (§11) | Lazy |
| System prompt | `openshell sandbox upload` | At sandbox-create time |
| Agent runtime config | `openshell sandbox upload` | At sandbox-create time |
| Repo content | `git clone` inside sandbox (`GitRepo` entry) | Inside `setup-workspace.sh` |
| User-provided context files | `openshell sandbox upload` | At sandbox-create time |

The materializer (`materialize(manifest, sandbox_id)`) wraps these
calls and returns a list of operations applied (for audit /
debugging).

### 8.2 Result Collection (Download)

After `task.complete` (or timeout), the orchestrator downloads:

| What | From | To |
|---|---|---|
| Final diff | `<child>:/sandbox/artifacts/final.diff` | orchestrator's filesystem (kept until the workflow ends) |
| Notes file | `<child>:/sandbox/notes-<slug>-<agent_id>.md` | orchestrator's filesystem |
| Audit fallback JSONL | `<child>:/sandbox/audit_fallback.jsonl` | ingested into orchestrator's `audit.db` (§12) |
| Git state (Strategy A) | `<child>:/sandbox/workspace/.git` | orchestrator's repo working copy, then `git push` |

Strategy A vs. B (sub-agent push direct) is unchanged from the M2
discussion ([design_m2.md §10.4](design_m2.md#104--git-operations-for-finalization));
M3 sticks with **Strategy A** by default — keep the coding sandbox
network-isolated, push from the orchestrator.

### 8.3 Snapshots (`LocalSnapshot`)

For sub-agent crash recovery and `re_delegate` continuity, M3
introduces a `LocalSnapshot` primitive — adapted from the
[OpenAI Agents SDK §7](deep_dives/openai_agents_sdk_deep_dive.md#7--snapshots-and-session-state).

```python
class LocalSnapshot:
    base_path: Path           # e.g. ~/.nemoclaw/snapshots/<workflow_id>.tar
    sandbox_id: str

    async def persist(self) -> None:
        """openshell sandbox download → tar → atomic rename to base_path."""

    async def restore(self) -> bytes:
        """Read base_path; the caller materializes it back into a fresh sandbox."""
```

Use cases:

- **`re_delegate` continuity** — the user clicks `[Iterate]` on a
  finalization message, the orchestrator snapshots the coding
  sandbox before sending the new `task.assign`, so `re_delegate` is
  recoverable if the sandbox crashes mid-iteration.
- **Crash recovery** — if the orchestrator restarts between
  `task.complete` and the audit ingest, it can restore the snapshot
  and re-collect artifacts rather than re-running the task.

`RemoteSnapshot` (S3, Azure) and the larger durable-memory story
stay deferred to M5.

---

## 9  Review Agent

### 9.1 Role and Tool Surface

The review agent is the second sub-agent type (after the coding
agent).  It runs `AgentLoop` from M2a with a **different tool
registry**:

| Toolset | Tools | Notes |
|---|---|---|
| Diff inspection | `read_diff`, `read_file_at_diff_target` | Read-only access to the coding sandbox's workspace via NMB-mediated filesystem proxy |
| Comment authoring | `add_review_comment` | Emits structured `ReviewComment` records via NMB |
| Decision | `approve_diff`, `request_changes` | Final review verdict |
| `tool_search` | (carry-over) | M2b meta-tool |
| Skill | `skill`, `load_skill` | Lazy skill loading (§11) |

Notably **absent**: no `bash`, no `write_file`, no `git_*` write
tools.  The review agent is read-only by construction.

### 9.2 System Prompt

The review-agent system prompt is a separate template at
`prompts/review_agent.md`.  Key rules (paraphrased):

- "Your goal is to identify issues before push.  You read diffs,
  flag problems, and either approve or request changes.  You do
  not write code yourself."
- "When you request changes, your comments must include a file
  path, a line range, a category (bug / style / test-coverage /
  perf / security), and a one-paragraph explanation.  The coding
  agent will iterate based on these comments."
- "If the diff is clean, call `approve_diff` and the orchestrator
  will hand off to push."

### 9.3 Sandbox Manifest

The review agent's `Manifest` is *minimal* — it doesn't need a
working repo, just access to the diff stream:

```python
review_manifest = Manifest(
    root="/sandbox/workspace",
    entries={
        ".agents/skills": LocalDir(src=cfg.skills.skills_dir),
        "artifacts": Dir(),                    # for review-output JSON
    },
    users=[User(name="reviewer")],
)
```

No `GitRepo`, no host filesystem mount.  The diff content arrives
via NMB messages.

---

## 10  Coding ↔ Review Local-Collaboration Loop

### 10.1 Why Local Collaboration

The traditional code-review loop is push → CI → reviewer → comments
→ developer → push → repeat.  Each round-trip costs minutes (CI) to
hours (reviewer availability).  When the "developer" and "reviewer"
are both LLM agents, the round-trip can collapse to seconds — but
only if neither has to wait on Git.

The local-collaboration loop avoids Git until the orchestrator
decides to push:

```
Coding Sandbox          NMB Broker          Review Sandbox          Orchestrator
     │                        │                     │                     │
     │ task.complete (draft)  │                     │                     │
     │  + diff payload        │                     │                     │
     │───────────────────────▶│                     │                     │
     │                        │  (orchestrator decides to invoke review)  │
     │                        │◀────────────────────────────────────────│
     │                        │                     │                     │
     │                        │ review.assign       │                     │
     │                        │  + diff payload     │                     │
     │                        │────────────────────▶│                     │
     │                        │                     │                     │
     │                        │                     │  ... reviewer reads │
     │                        │                     │      diff, runs      │
     │                        │                     │      AgentLoop ...   │
     │                        │                     │                     │
     │                        │ review.complete     │                     │
     │                        │  { verdict, comments[]}                   │
     │                        │◀────────────────────│                     │
     │                        │                                           │
     │                        │  if verdict == 'approve' → push           │
     │                        │  if verdict == 'changes_requested' →      │
     │                        │      build re_delegate prompt and send    │
     │                        │      it back to coding sandbox            │
     │                        │                                           │
     │ re_delegate (with comments)                                        │
     │◀────────────────────────                                           │
     │                                                                    │
     │  ... iterate ...                                                   │
```

Three iteration rounds typically converge on a clean diff; if the
agents fail to converge after `max_review_rounds` (default 3), the
orchestrator surfaces the diff + comments to the user with the same
finalization buttons.

### 10.2 Diff Exchange Protocol

| Type | Direction | Payload |
|---|---|---|
| `review.assign` | Orchestrator → Review | `{diff, files_changed, summary, max_turns}` |
| `review.complete` | Review → Orchestrator | `{verdict: "approve" \| "changes_requested", comments: [ReviewComment, ...]}` |

`ReviewComment` is a typed Pydantic dataclass:

```python
class ReviewComment(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    category: Literal["bug", "style", "test", "perf", "security", "other"]
    severity: Literal["blocker", "high", "medium", "low", "info"]
    message: str
    suggested_change: str | None = None
```

Typed output (vs. free-form text in `task.complete`) means the
orchestrator's `re_delegate` prompt assembly is mechanical, not
LLM-driven — the orchestrator builds the prompt from the
`ReviewComment` list directly.  This is one of the
[OpenAI Agents SDK adoption recommendations](deep_dives/openai_agents_sdk_deep_dive.md#11--what-to-adopt--prioritized) — typed sub-agent
output catches bad payloads at the protocol layer.

### 10.3 Concurrency

For a single workflow:

- One coding sandbox + one review sandbox.
- The two are **siblings**; spawn order is coding first, review on
  demand when the orchestrator decides to invoke review.
- Per-workflow `asyncio.Semaphore(value=1)` prevents two reviews of
  the same diff running in parallel.

For multiple workflows in flight, each gets its own coding+review
pair; the orchestrator's existing per-agent semaphores (M2b §8.1)
apply at the role level.

### 10.4 Post-Push Review Integration

When the orchestrator pushes (via `push_and_create_pr`), it can
also re-use the in-flight `ReviewComment` list to post comments on
the resulting Gerrit/GitLab PR via nv-tools.  This gives a single
review surface across "local" and "remote" review channels.

---

## 11  Lazy Skills (`load_skill`)

### 11.1 Motivation

M2a's `SkillLoader` exposes every `SKILL.md` via the `skill` tool
with the **full body** in the response.  For small skills this is
fine; for skills with large reference bundles (a 50-page style
guide, a directory of test-corpus examples) it dominates the prompt
the moment the model loads them.

The OpenAI Agents SDK pattern (see
[deep dive §8](deep_dives/openai_agents_sdk_deep_dive.md#8--lazy-skills-with-load_skill))
is metadata-first, body-on-demand:

1. The system prompt lists each skill's name + description (cheap).
2. A `load_skill(skill_name)` tool copies that skill's directory
   (`SKILL.md` + `scripts/` + `references/` + `assets/`) into the
   sub-agent's workspace.
3. The agent reads the body via the ordinary `read_file` tool.
4. Subsequent calls to `load_skill` for the same skill are no-ops
   (the directory already exists).

### 11.2 SKILL Layout (Extended)

M2a's skills live as `skills/<name>/SKILL.md`.  M3 extends the
layout to support reference material:

```
skills/
└── credit-note-fixer/
    ├── SKILL.md                # frontmatter + body
    ├── scripts/                # executable helpers
    │   └── verify.sh
    ├── references/             # extra reading
    │   ├── grammar.md
    │   └── style-guide.md
    └── assets/                 # static resources
        └── template.json
```

Skills without these subdirectories work unchanged — they just
materialize as `<name>/SKILL.md` in the workspace.

### 11.3 The `load_skill` Tool

```python
@tool(
    name="load_skill",
    description="Materialize a skill (SKILL.md + scripts/references/assets) into the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
        },
        "required": ["skill_name"],
    },
    is_concurrency_safe=False,  # writes to workspace
)
async def load_skill(skill_name: str) -> str:
    ...
```

The handler resolves `skill_name` against the orchestrator's skill
index, copies the skill directory into `<workspace>/.agents/<name>/`,
and returns a JSON payload listing the materialized files.

### 11.4 Relationship to the Orchestrator

The orchestrator (which has no per-task workspace) keeps using the
M2a `SkillLoader` to produce metadata for the system prompt.  Only
the sub-agents (which have workspaces) get the `load_skill` tool.
The `SkillLoader` is the single metadata source for both — no code
duplication.

---

## 12  Audit and Observability (Multi-Sandbox)

### 12.1 Audit Aggregation

Each sub-agent runs an `AuditBuffer` (M2b §4.9 of design_m2.md) that
accumulates tool-call records and flushes them to the orchestrator
via NMB-batched `audit.flush` messages every N seconds or M records.

In M3 this is unchanged.  What changes:

- The audit JSONL fallback file lives at `/sandbox/audit_fallback.jsonl`
  *inside the sub-agent's sandbox*, not on a shared host filesystem.
- On `task.complete` (or TTL cleanup), the orchestrator
  `openshell sandbox download`s that file and ingests any records
  the NMB flush missed.
- Every audit record carries `workflow_id`, `parent_sandbox_id`, and
  `agent_role` so the orchestrator's central `audit.db` can render
  per-workflow timelines across the orchestrator + coding + review
  triple.

### 12.2 Per-Sandbox Cost Tracking

Each sub-agent reports its inference token usage in `task.complete`.
The orchestrator aggregates per-workflow:

```python
@dataclass
class WorkflowCost:
    workflow_id: str
    orchestrator_tokens: dict[str, int]   # {prompt: ..., completion: ...}
    coding_tokens: dict[str, int]
    review_tokens: dict[str, int]
    total_usd: Decimal                    # rate-card lookup
```

This rolls into the M6 budget enforcement story but is observable
already in M3.

### 12.3 Per-Sandbox Logs

OpenShell streams each sandbox's stdout/stderr to the gateway, which
the orchestrator can query via `openshell sandbox logs <name>`.
M3 adds a thin wrapper that inlines per-sandbox tail output into the
finalization Slack message and the (forthcoming) Web UI per-sandbox
panel.

---

## 13  End-to-End Walkthrough

A user asks the orchestrator: "Add `/api/health` to our service and
make sure tests pass."

1. **Orchestrator parses, plans.**  The orchestrator calls
   `delegate_task(role="coding", repo=..., prompt=...)`.
2. **PolicyBuilder + Manifest.**  The orchestrator picks
   `policies/coding-agent.yaml` as the base, finds two matching
   skills (`pip-install` and a project-specific
   `service-skeleton`), merges their `nemoclaw.infrastructure`
   blocks, and generates the per-sandbox policy.  It also builds the
   `Manifest` with the repo `GitRepo` entry, the skill index, and
   the env-var placeholders.
3. **Sandbox spawn.**  `openshell sandbox create --name
   coding-<wf> --policy <generated>.yaml --from <coding-image>`.
4. **Workspace materialization.**  The orchestrator runs the
   manifest materializer: `openshell sandbox upload` for static
   files, `openshell sandbox exec /app/setup-workspace.sh` to clone
   the repo with the L7-proxy-aware credential helper.
5. **Sub-agent ready.**  The sub-agent process starts, connects to
   NMB, sends `sandbox.ready`.
6. **Task assignment.**  Orchestrator sends `task.assign` with
   `prompt`, `max_turns=20`, the surfaced skill list.
7. **Coding agent runs.**  M2a `AgentLoop` cycles through tool
   calls: `read_file`, `grep`, `write_file`, `bash` (`pytest -k
   health`), `git_commit`.  At one point `pip install fastapi`
   fails (PyPI not in policy); the agent emits a `policy.request`,
   the orchestrator auto-approves (`task.lang == "python"`), the
   sandbox's policy is hot-reloaded, the agent retries and
   succeeds.  Tests pass.
8. **Coding `task.complete`.**  Coding sub-agent sends `task.complete`
   with the diff, summary, and notes file path.
9. **Orchestrator decides to invoke review.**  Spawns the review
   sandbox: `openshell sandbox create --name review-<wf> --policy
   policies/review-agent.yaml`.  `review.assign` with the diff
   payload.
10. **Review agent runs.**  Reads the diff via `read_diff`, calls
    `add_review_comment` once ("missing test for unhealthy state"),
    then `request_changes`.
11. **Orchestrator builds re_delegate prompt.**  From the typed
    `ReviewComment` list — no LLM in the loop here, just string
    templating.
12. **Coding agent iterates.**  Receives `re_delegate`, adds the
    test, commits, sends a fresh `task.complete`.
13. **Review round 2.**  Review agent reads the new diff, calls
    `approve_diff`.
14. **Finalization.**  Orchestrator's main `AgentLoop` sees
    `review.complete{verdict=approve}`, calls
    `present_work_to_user` with the diff + summary + review trail.
15. **User clicks `[Push & PR]`.**  `push_and_create_pr` downloads
    git state from the coding sandbox, pushes, opens the PR, posts
    the `ReviewComment` list as inline review comments via
    nv-tools.
16. **Cleanup.**  Both sandboxes are destroyed; audit fallback
    files are downloaded and ingested; per-workflow cost is
    recorded.

End-to-end wall-clock, two iterations, against a small repo:
~5–8 minutes of which ~3 minutes is real LLM inference time.

---

## 14  Implementation Plan

### Phase 1 — Sandbox client + Manifest + per-role policies

| Task | Files | Status |
|---|---|---|
| `SandboxClient` wrapping `openshell sandbox` CLI | `src/nemoclaw_escapades/sandbox/client.py` (new) | ⏳ |
| `Manifest` Pydantic dataclass + entry types | `src/nemoclaw_escapades/sandbox/manifest.py` (new) | ⏳ |
| `materialize(manifest, sandbox_id)` helper | `src/nemoclaw_escapades/sandbox/materialize.py` (new) | ⏳ |
| `policies/coding-agent.yaml` + base `policies/review-agent.yaml` | `policies/` | ⏳ |
| Manifest ↔ policy diff check at startup | `src/nemoclaw_escapades/sandbox/check.py` (new) | ⏳ |
| Unit tests for manifest validation, materialization, diff check | `tests/test_sandbox_manifest.py`, `tests/test_sandbox_materialize.py` | ⏳ |

**Exit criteria:** `SandboxClient.create_with_manifest(...)` produces
a coding-agent sandbox whose filesystem matches the declared manifest
and whose policy file matches the role base.  No skill auto-policy
yet.  Same-sandbox subprocess path from M2b still works (M3 spawn is
a new code path, not a replacement).

### Phase 2 — Skill auto-policy + policy hot-reload

| Task | Files | Status |
|---|---|---|
| `PolicyBuilder` (base + skills + overlay → YAML) | `src/nemoclaw_escapades/sandbox/policy_builder.py` (new) | ⏳ |
| `nemoclaw.infrastructure` parsing in `SkillLoader` | `src/nemoclaw_escapades/agent/skill_loader.py` | ⏳ |
| `auto_policy_approval` config + per-skill approval store | `src/nemoclaw_escapades/config.py`, `~/.nemoclaw/skill_approvals.json` | ⏳ |
| `policy.request` / `.updated` / `.denied` NMB types | `src/nemoclaw_escapades/nmb/types.py`, broker | ⏳ |
| `PolicyOverlay.apply()` calling `openshell policy set --wait` | `src/nemoclaw_escapades/sandbox/policy_overlay.py` (new) | ⏳ |
| Auto-approval evaluator with per-task `task.lang` rule | `src/nemoclaw_escapades/orchestrator/policy_handler.py` (new) | ⏳ |
| Slack escalation for unknown endpoints | `src/nemoclaw_escapades/connectors/slack/policy_approval.py` (new) | ⏳ |
| Audit table for `policy.*` events | `src/nemoclaw_escapades/audit/db.py` | ⏳ |
| Tests: builder, request flow end-to-end (mock OpenShell), Slack escalation UI | `tests/test_policy_builder.py`, `tests/test_policy_hot_reload.py` | ⏳ |

**Exit criteria:** A coding agent that hits a denied endpoint emits
a `policy.request`, the orchestrator auto-approves a known-safe
endpoint (PyPI), the sandbox's policy is hot-reloaded, the agent
retries and succeeds.  An unknown endpoint correctly escalates to
Slack with Approve/Deny buttons.  Per-skill `nemoclaw.infrastructure`
blocks merge into the spawn-time policy without manual editing.

### Phase 3 — Multi-sandbox spawn replaces M2b's subprocess path

| Task | Files | Status |
|---|---|---|
| `DelegationManager` branches on `cfg.delegation.mode` (`subprocess` vs `openshell`) | `src/nemoclaw_escapades/orchestrator/delegation.py` | ⏳ |
| `setup-workspace.sh` simplified — manifest does most of the work; script just runs `git clone` and starts the agent | `setup-workspace.sh` | ⏳ |
| Audit fallback download via `openshell sandbox download` | `src/nemoclaw_escapades/audit/flush.py` | ⏳ |
| Artifact download (diff, notes) via `openshell sandbox download` | `src/nemoclaw_escapades/orchestrator/finalization.py` | ⏳ |
| TTL watchdog calls `openshell sandbox delete` | `src/nemoclaw_escapades/orchestrator/cron.py` | ⏳ |
| End-to-end test: orchestrator spawns coding sandbox, sub-agent receives `task.assign`, returns `task.complete`, sandbox is cleaned up | `tests/test_integration_multi_sandbox.py` (new) | ⏳ |

**Exit criteria:** `cfg.delegation.mode = "openshell"` works in
production: a coding task spawns a fresh sandbox, runs to
completion, and is cleaned up.  Subprocess mode still works for
unit tests so we don't gate CI on a real sandbox runtime.

### Phase 4 — Review agent + collaboration loop

| Task | Files | Status |
|---|---|---|
| Review-agent role config + system prompt template | `prompts/review_agent.md`, `src/nemoclaw_escapades/agent/review_agent.py` (new) | ⏳ |
| Review-specific tool registry (`read_diff`, `add_review_comment`, `approve_diff`, `request_changes`) | `src/nemoclaw_escapades/tools/review.py` (new) | ⏳ |
| `ReviewComment` Pydantic model | `src/nemoclaw_escapades/models/review.py` (new) | ⏳ |
| `review.assign` / `review.complete` NMB types | `src/nemoclaw_escapades/nmb/types.py`, broker | ⏳ |
| Orchestrator's invoke-review decision logic in finalization | `src/nemoclaw_escapades/orchestrator/finalization.py` | ⏳ |
| `re_delegate` prompt synthesis from `ReviewComment` list | `src/nemoclaw_escapades/orchestrator/re_delegate.py` (new) | ⏳ |
| Post-push: `ReviewComment` → Gerrit/GitLab inline comments via nv-tools | `src/nemoclaw_escapades/tools/gerrit.py`, `tools/gitlab.py` | ⏳ |
| Tests: review agent end-to-end against a fixture diff, iteration convergence within `max_review_rounds`, post-push comment posting | `tests/test_review_agent.py`, `tests/test_review_loop.py` | ⏳ |

**Exit criteria:** A coding task that produces a flawed diff is
caught by the review agent, the coding agent iterates based on the
typed comments, the second iteration is approved, and the resulting
PR carries the same review comments inline.

### Phase 5 — Lazy skills + snapshots + polish

| Task | Files | Status |
|---|---|---|
| `load_skill` tool | `src/nemoclaw_escapades/tools/load_skill.py` (new) | ⏳ |
| Skill-layout extension (`scripts/`, `references/`, `assets/`) | `skills/`, `agent/skill_loader.py` | ⏳ |
| System-prompt fragment teaching progressive disclosure | `prompts/coding_agent.md`, `prompts/review_agent.md` | ⏳ |
| `LocalSnapshot` for `re_delegate` continuity | `src/nemoclaw_escapades/sandbox/snapshot.py` (new) | ⏳ |
| Web UI: per-sandbox panel + policy-request approval surface | `web_ui/` (TBD) | ⏳ |
| Documentation update + M3 blog post draft | `docs/blog_posts/m3/` | ⏳ |

**Exit criteria:** Skills with reference bundles load on demand
without bloating the prompt.  `re_delegate` survives an orchestrator
restart by restoring from a `LocalSnapshot`.  The Web UI shows a
per-sub-sandbox card for each in-flight workflow.

---

## 15  Testing Plan

### 15.1 Unit Tests

| Component | Test |
|---|---|
| `Manifest` validation | Reject absolute paths, `..` escapes, Windows-absolute paths, unknown env-var providers. |
| `PolicyBuilder` | Base + skill blocks + overlay merge produces expected YAML; conflicts (writes-vs-no-writes) resolve to strictest. |
| `PolicyOverlay.apply()` | Calls `openshell policy set --wait` with the merged YAML; returns control after the gateway acks. |
| Auto-approval evaluator | Allowlist matches; condition expressions (`task.lang == "python"`) work; deny→escalate fallback. |
| `materialize` | Each entry type produces the expected `openshell sandbox upload`/`exec` calls; mounts apply correct user/group/permissions. |
| `LocalSnapshot.persist` / `.restore` | Atomic write; restore works after an orchestrator restart. |
| `load_skill` | Materializes the right directory; second call is a no-op; missing skill returns a clean error. |
| `ReviewComment` parsing | All required fields present; bad enum values rejected; unknown categories logged but not crashed. |

### 15.2 Integration Tests

| Test | What it verifies |
|---|---|
| `test_integration_multi_sandbox.py::test_coding_task_end_to_end` | Spawn coding sandbox, deliver `task.assign`, receive `task.complete`, cleanup.  Real OpenShell runtime; gated behind `OPENSHELL_AVAILABLE` env var so it doesn't block CI. |
| `::test_policy_hot_reload_pypi` | Coding agent that needs PyPI sees the auto-approval flow end-to-end. |
| `::test_policy_hot_reload_unknown_endpoint` | Coding agent that needs an unknown endpoint sees the Slack escalation flow with a fake-Slack connector. |
| `::test_review_loop_converges` | Coding + review iterate; after `max_review_rounds=3`, either approve or escalate.  Tests both convergent and divergent cases. |
| `::test_re_delegate_with_snapshot` | `re_delegate` after a forced orchestrator restart restores the coding sandbox from snapshot and continues. |
| `::test_lazy_load_skill_materialization` | Sub-agent calls `load_skill`, the skill directory appears in its workspace, subsequent file reads work. |

### 15.3 Safety Tests

| Test | What it verifies |
|---|---|
| `test_no_secret_leakage` | A `.env` value never appears in any rendered policy YAML or `Manifest` JSON dump.  Regression test for the M2b leak fix. |
| `test_manifest_policy_consistency` | Manifest `extra_path_grants` and `GitRepo.repo` hosts match the rendered policy's filesystem and network sections.  Drift surfaces as a hard failure. |
| `test_review_agent_cannot_write` | The review agent's tool registry literally has no write tools; attempting to register one raises. |
| `test_coding_sandbox_no_default_external_network` | Without skill auto-policy or hot-reload, the coding agent's first `pip install` fails with a deny error (i.e., we don't silently widen the policy). |

---

## 16  Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `openshell sandbox create` is slow (~5-15s in practice) and blocks every delegation. | Pre-warmed sandbox pool: keep N idle coding sandboxes ready, hot-reload their policy and reset their workspace per task.  Pool size capped at `cfg.delegation.warm_pool_size`.  Falls back to cold spawn if the pool is empty.  Land in Phase 5 if cold spawn proves too slow in practice. |
| Skill auto-policy widens the attack surface — a malicious / mis-authored skill can request arbitrary network access. | Two-tier approval: only skills in `auto_policy_allowlist` merge silently; everything else surfaces to the user.  Skills in the allowlist are a small, audited set (`pip-install`, `npm-install`, etc.), shipped in-repo, reviewed at PR time. |
| Policy hot-reload latency is non-trivial (~1-3s for `policy set --wait`), and a failed hot-reload mid-run leaves the sandbox in an inconsistent state. | The `--wait` flag blocks until the policy is applied; the `PolicyOverlay.apply()` retries with exponential backoff up to 30s.  On total failure, the orchestrator sends `policy.denied` and the sub-agent treats the original tool call as a hard error.  No silent partial application. |
| Review agent loops indefinitely on subjective issues (style preferences). | `max_review_rounds=3` (config-tunable) hard cap; on hitting the cap, the diff + comments are surfaced to the user for human override.  Severity gating: only `blocker` and `high` comments trigger another iteration; `low` and `info` are reported but don't block approval. |
| `Manifest` adoption requires touching workspace seeding everywhere it lives (M2b's `setup-workspace.sh`, the audit fallback path, the skills directory). | Phase 1 keeps `setup-workspace.sh` working alongside the new manifest path; phase 3 retires the old path.  Migration is incremental, not flag-day. |
| Per-sandbox cost grows linearly with concurrent workflows; an unbounded `delegate_task` storm could blow the budget. | Per-agent semaphores from M2b extend to the role level (one cap for coding, one for review), and `cfg.budget.daily_usd` enforces a hard daily ceiling — over-budget delegations queue or fail with a clear user message. |
| Auto-policy approvals leak across users in a multi-tenant deployment. | M3 is single-user (matching the rest of NemoClaw); per-user approval stores are a separate concern when multi-tenancy lands (M5+). |

---

## 17  Open Questions

1. **Pre-warmed sandbox pool — Phase 5 or M3.5?**  Cold spawn might
   be acceptable in practice (most tasks take minutes; a 10s spawn
   delay is noise).  Wait for real usage data before optimising.
2. **Should the review agent's diff access go through NMB or
   directly via `openshell sandbox download` from the coding
   sandbox?**  NMB is simpler (no extra cross-sandbox download path)
   but caps diff size at NMB's per-message limit (typically a few
   MB).  For most tasks this is fine; for huge refactors it isn't.
   Default to NMB; fall back to download for diffs over a
   configurable size.
3. **Does the review agent need its own sandbox at all?**  An
   alternative: the orchestrator runs the review-agent `AgentLoop`
   inline.  Pros: one less sandbox per workflow.  Cons: the review
   agent's tool calls are then audited under the orchestrator's
   identity, mixing concerns.  Default: separate sandbox.
4. **How does `Manifest` interact with M5's memory layer?**  The
   memory layer (Honcho user memory + SecondBrain knowledge) needs
   to seed sub-agent workspaces with relevant context.  A
   `MemorySeed` `Manifest` entry type that looks up relevant
   memories and writes them as files is one option; injecting the
   memory directly into the system prompt is another.  Defer the
   choice to M5.
5. **When the auto-policy allowlist disagrees with what the user
   has manually approved before, who wins?**  Manual approvals
   should be sticky (the user's intent overrides the allowlist
   default).  A persistent per-skill, per-user approval store
   handles this; lifetime is "until the user explicitly revokes" or
   "until the skill's `nemoclaw.infrastructure` block changes
   materially."
6. **Should `policy.request` be rate-limited?**  A buggy agent
   could storm the orchestrator with requests.  Default: 10/min
   per sandbox, hard-capped; over the cap, the sandbox is killed
   with an `excessive_policy_requests` audit event.
7. **Snapshots for crash recovery vs. snapshots for memory.**  M3
   ships `LocalSnapshot` for `re_delegate` continuity.  M5 will need
   a similar primitive for the long-term memory layer.  Are they
   the same primitive or different?  Provisional answer: same
   `LocalSnapshot` / `RemoteSnapshot` interface, different ID
   conventions and TTLs.  Decide concretely in M5 design.

---

### Sources

- [M2 design (original)](design_m2.md) — multi-sandbox flows
  preserved here as the M3 foundation.
- [M2b design](design_m2b.md) — same-sandbox subprocess delegation,
  NMB protocol, and the `task.assign` / `task.complete` shape that
  M3 inherits unchanged.
- [OpenAI Agents SDK deep dive](deep_dives/openai_agents_sdk_deep_dive.md) —
  `Manifest`, `Capability`, lazy `Skills`, snapshots, and the
  composition patterns this milestone borrows from.
- [OpenShell deep dive](deep_dives/openshell_deep_dive.md) — sandbox
  CLI surface (`create`, `delete`, `upload`, `download`,
  `policy set --wait`), policy enforcement model, network policy
  schema.
- [BYOO tutorial deep dive](deep_dives/build_your_own_openclaw_deep_dive.md) —
  per-agent semaphores (§9), at-least-once outbound delivery (§4),
  and the sub-agent dispatch / concurrency-cap patterns referenced
  in §16.
- [Build your own OpenClaw](https://github.com/czl9707/build-your-own-openclaw)
  Step 13 (sub-agent dispatch) and Step 16 (concurrency control) for
  the dispatch primitives M3 builds on.
- [OpenAI Agents SDK Sandbox docs](https://openai.github.io/openai-agents-python/sandbox/) —
  the official guide for the SDK primitives M3 borrows from.
