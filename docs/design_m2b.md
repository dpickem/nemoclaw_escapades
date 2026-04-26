# Milestone 2b — Multi-Agent Orchestration: Delegation, NMB & Concurrency

> **Split from:** [Milestone 2 (original)](design_m2.md)
>
> **Predecessor:** [Milestone 2a — Reusable Agent Loop](design_m2a.md)
>
> **Successor:** [Milestone 3 — Review Agent](design.md#milestone-3--review-agent)
>
> **Last updated:** 2026-04-24

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [Architecture](#3--architecture)
4. [Sandbox Lifecycle & Workspace Setup](#4--sandbox-lifecycle--workspace-setup)
5. [Agent Setup: Policy, Tools, and Comms](#5--agent-setup-policy-tools-and-comms)
6. [Orchestrator ↔ Sub-Agent Protocol](#6--orchestrator--sub-agent-protocol)
7. [Work Collection and Finalization](#7--work-collection-and-finalization)
8. [NMB Event Loop and Concurrency Model](#8--nmb-event-loop-and-concurrency-model)
9. [At-Least-Once NMB Delivery](#9--at-least-once-nmb-delivery)
10. [`ToolSearch` Meta-Tool](#10--toolsearch-meta-tool)
11. [Basic Cron](#11--basic-cron)
12. [Audit and Observability](#12--audit-and-observability)
13. [End-to-End Walkthrough](#13--end-to-end-walkthrough)
14. [Implementation Plan](#14--implementation-plan)
15. [Testing Plan](#15--testing-plan)
16. [Risks and Mitigations](#16--risks-and-mitigations)
17. [Open Questions](#17--open-questions)

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

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Implement the **sub-agent coding process** that uses M2a's `AgentLoop` +
   file tools to execute coding tasks.
2. Define the full **sandbox setup sequence**: workspace, tools, comms, policy.
3. Implement **orchestrator → sub-agent delegation** via NMB `task.assign` and
   result collection via `task.complete`.
4. Build the **work collection and finalization** flow: collect sub-agent
   results, present to user, commit/push/create PR on approval.
5. Implement **per-agent concurrency caps** via `asyncio.Semaphore` and
   **spawn depth limits** (`max_spawn_depth`, `max_children_per_agent`).
6. Implement **at-least-once NMB delivery** for critical messages
   (`task.complete`, `audit.flush`).
7. Implement **`ToolSearch` meta-tool** for progressive tool loading.
9. Implement **basic operational cron**: sandbox TTL watchdog, stale-session
   cleanup, health checks.
10. Maintain audit, approval, and safety guarantees from M1.

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
4. Workspace directory created and seeded with task context.
5. Sub-agent connects to NMB broker, sends `sandbox.ready`.
6. Orchestrator sends `task.assign` with task description and workspace path.
7. Sub-agent runs `AgentLoop` with coding file tools.
8. Sub-agent sends `task.complete` with result, diff, and any notes file
   the agent created in its workspace.
9. Orchestrator runs model-driven finalization (§7).

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
> `fatal: unable to access '…': CONNECT tunnel failed, response 403` even with
> the `gitlab` network policy correctly applied (host allowed, port 443 full
> access, `/usr/bin/git` on the binary allowlist); `curl` against the same
> host returned 200.  This is the concrete failure mode §4.4.1 describes.

#### 4.4.1 Problem Statement

M2a ships `git_clone` as part of the coding tool suite ([M2a §4 tools](design_m2a.md#4--coding-agent-tools)), scoped to a fail-closed host allowlist.  After this milestone's sandbox-image and policy delta (`git` binary installed, `/usr/bin/git` whitelisted for the `github` / `gitlab` / `gerrit` network policies) *unauthenticated* HTTPS clones work out of the box — the obvious case is a public `github.com` repo.

Private hosts are the problem.  An internal GitLab (`gitlab.example.com`), a private Gerrit (`gerrit.example.com`), and a private GitHub repo all reject the clone with an HTTP 401 because git has no credentials to present.  We already hold the right credentials — `GITLAB_TOKEN`, `GERRIT_USERNAME`, and `GERRIT_HTTP_PASSWORD` are OpenShell providers, injected into the sandbox as *placeholder* env vars (e.g. `GITLAB_TOKEN=openshell:resolve:env:GITLAB_TOKEN`) that the L7 proxy substitutes for real values in the `Authorization` header at TLS termination.  Our REST tools consume those placeholders for free because *we* wrote the code that sets `Authorization: Bearer $GITLAB_TOKEN` on every request (`GitLabClient`, `JiraClient`, `GerritClient`, `ConfluenceClient`).

Git doesn't have that hook.  Its credential sources are URL userinfo, credential helpers, `~/.git-credentials` / `.netrc`, and interactive prompts — none of which natively read an env var.  The placeholder sits in `$GITLAB_TOKEN`; git just doesn't know to look.  The four options below are three different *bridges* from "placeholder in env" → "`Authorization` header the proxy can substitute" (URL embedding, credential helper, SSH sidestep), plus a fourth that avoids the clone entirely.  Every option except C (SSH) composes with the existing provider flow; C is the only one that introduces a parallel credential channel.

Two properties matter regardless of which bridge we pick:

1. **No secret leakage into the filesystem image or the cloned repo's `.git/config`.**  Tokens baked into files survive container rebuilds, operator `git push`es, and audit-log captures.  The same URL that holds a harmless placeholder inside the sandbox holds the *real* token on the operator's host (where `$GITLAB_TOKEN` is the unwrapped value from `.env`, read by host-side tooling like `scripts/test_auth.py`), so the leakage is not purely theoretical.
2. **Reuse the existing provider / placeholder system where possible.**  We already hold `GITLAB_TOKEN` / `GERRIT_USERNAME` / `GERRIT_HTTP_PASSWORD` as OpenShell providers.  Adding a parallel credential channel for git — SSH keys mounted into the sandbox, a new secret store, a parallel rotation story — doubles the surface.  Options A and B satisfy this; C does not.

#### 4.4.2 Option A — URL-embedded token

Embed the token in the clone URL:

```
https://oauth2:${GITLAB_TOKEN}@gitlab.example.com/acme/demo-repo
```

**Pros.**  Zero new infrastructure — works today with the existing `GITLAB_TOKEN` provider.  Operator documentation only; no image changes.

**Cons.**  Token leaks into:

- The sandbox's process listing (`ps -ef` shows the full `git clone` command line).
- The cloned repo's `.git/config` (git stores the auth URL verbatim as the remote `origin.url`).
- Any error message or log line that echoes the URL.

Leaked tokens are cheap to rotate, but the workflow is error-prone: every future `git pull` / `git push` inside the clone re-uses the embedded credential, so the exposure persists until the operator manually rewrites the remote URL.

**Use case.**  A short-lived workaround.  `.env.example` can document it for developers who need to clone a specific private repo today; a `scratchpad`-style skill can surface it when the agent hits a 401.  Not the long-term answer.

#### 4.4.3 Option B — Git credential helper (recommended long-term)

Option B uses git's built-in [credential-helper protocol](https://git-scm.com/docs/gitcredentials) to hand the provider-injected placeholder to git.  Git then lands it in the `Authorization` header, where the L7 proxy can substitute the real value — *exactly the same substitution path* the REST tools use, just with a custom helper replacing the `Authorization: Bearer $TOKEN` line our Python clients set explicitly.

**End-to-end flow.**  At image-build time, a short helper script (`git-credential-from-env`) lands in `/usr/local/bin` and `/etc/gitconfig` is configured to invoke it for every private host.  At runtime:

1. Agent runs `git clone https://<host>/…`.
2. git reads `/etc/gitconfig`, finds `credential.https://<host>.helper = !/usr/local/bin/git-credential-from-env`, execs the helper, and writes `host=<host>\n` (plus other request fields) to the helper's stdin.
3. The helper reads `$GITLAB_TOKEN` / `$GERRIT_*` from its environment — which inside the sandbox hold the OpenShell placeholder strings (e.g. `openshell:resolve:env:GITLAB_TOKEN`), **not** the real secrets — and prints `username=…\npassword=<placeholder>\n` on stdout.
4. git composes the HTTPS request with `Authorization: Basic base64("<user>:<placeholder>")` and sends it through `HTTPS_PROXY`.
5. The L7 proxy terminates TLS, decodes the Basic header, matches the placeholder, substitutes the real token, re-encodes, and forwards the request to the origin.

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

The real PAT only ever lives on the OpenShell host (in the provider vault) and briefly inside the L7 proxy during substitution.  The sandbox — the helper, git, the Authorization header git composes — only ever handles the placeholder string.

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

- Token never lands in URLs, process listings, or repo configs — git asks the helper on demand, the helper reads `os.environ[…]`, the value exists in memory for one invocation.
- Composes cleanly with OpenShell's provider flow: the proxy-injected `$GITLAB_TOKEN` is already in the sandbox env; the helper just reads it there.
- Handles `git pull` / `git push` inside the cloned repo automatically once installed — no per-invocation plumbing from the agent.
- Adding a new private host is a two-line change (one `case` arm in the helper + one `git config` line in the Dockerfile).

**Cons.**

- Requires a new executable in the image + a `RUN git config --system` block.  The helper path must be on the policy's `binaries` allowlist for the git-host network policies (git execs the helper to resolve credentials *before* it starts the HTTPS request).
- Secret-exposure caveat: `printf` writes the password to the helper's stdout, which lives in pipe buffers and could in principle be captured.  This is strictly better than the URL-embedding case — the exposure is process-local, in-memory, and short-lived — but it's not zero.  Rotating providers still rotates all downstream usage cleanly.

**Use case.**  The recommended long-term path for any host whose credentials already flow through an OpenShell provider.  Phase 3 implementation target.

#### 4.4.4 Option C — SSH keys

Install `openssh-client` in the image, mount an SSH private key plus a `known_hosts` file into `/sandbox/.ssh`, allow SSH egress to the git hosts (or leave it unmediated by the L7 proxy, since OpenShell doesn't do protocol-level SSH interception).

**Pros.**  Matches most developers' intuition for "how do I clone a private repo."  SSH's authentication is independent of the HTTPS interception path, so TLS-termination quirks don't affect it.

**Cons.**

- SSH keys are coarser-grained than HTTPS tokens — most deployments don't scope SSH keys per-repo, so a leaked key exposes everything the key-owner can reach.
- `known_hosts` pinning is annoying to maintain: the image either ships a static copy (brittle when the host rotates its host key) or disables host-key verification (undermines the point of pinning).
- OpenShell's L7 proxy doesn't intercept SSH, so we lose the observability the HTTPS path gives us (no per-request audit, no credential-placeholder resolution).
- Doesn't compose with the `GITLAB_TOKEN` provider — it's a second credential channel to provision, rotate, and audit.

**Use case.**  Only pick this up if a host *requires* SSH (push restrictions that reject HTTPS tokens, repo mirrors that only expose SSH endpoints).  Not a general-purpose answer.

#### 4.4.5 Option D — Host-side seed (no in-sandbox clone)

For repos the operator already has checked out on their host, skip the clone entirely: copy the local working tree into the sandbox workspace at task-spawn time.  Conceptually:

```make
# Not yet implemented — sketch only.
seed-workspace:
    tar -c -C "$(HOST_REPO)" . \
      | openshell sandbox exec -n $(SANDBOX_NAME) --no-tty -- \
        tar -x -C /sandbox/workspace/$(notdir $(HOST_REPO))
```

The orchestrator / sub-agent then operates on the pre-populated workspace; it never needs to fetch or push if the task is strictly-local (code review, refactor, debugging).

**Pros.**

- Zero credential plumbing in the sandbox: the host already has whatever git / SSH / token configuration works there.  The sandbox doesn't need to know about authentication at all.
- Works for any repo the operator can access on their machine — private mirrors, partially-merged branches, repos behind 2FA-protected hosts that don't tolerate automated tokens.
- Snapshots the operator's exact working state (uncommitted edits, untracked files, stashes applied).  Useful for "review my current changes" workflows where the point *is* the uncommitted state.

**Cons.**

- **One-shot snapshot.**  `git pull` / `git push` inside the sandbox still need credentials; this option doesn't solve that, it only sidesteps the initial clone.  Paired with Option B when both are in play.
- Doesn't compose with autonomous delegation.  The orchestrator would need a way to ask the host "do you have this repo locally? please tar it over," which is outside the current NMB protocol (host ↔ sandbox, not broker ↔ broker).
- Large repos upload slowly and cost sandbox-side disk.  For tasks that only need a subset of the tree, a full copy is wasteful.
- Host-side staleness: if the operator hasn't `git pull`'d lately, the sub-agent sees the state as of the last local pull, not HEAD on the remote.

**Use case.**  Interactive developer workflows — "look at my current branch and suggest a refactor", "review the diff I have staged right now."  Complements Option B rather than replacing it: B handles the fetch path for autonomous / CI-driven work, D handles the "I already have this locally, use that" path for interactive work.

#### 4.4.6 Comparison

| Option | Secret-leak risk | Operator effort | Works for `git pull` / `push`? | Composes with provider system? |
|--------|------------------|-----------------|-------------------------------|-------------------------------|
| **A — URL token** | High — token in process listing, URL, and repo `.git/config` | Zero | Yes, but re-leaks on every fetch | Yes (reads `$GITLAB_TOKEN`) |
| **B — Credential helper** | Low — in-memory, per-invocation | Image + Dockerfile + policy allowlist line | Yes, transparently | Yes (reads `$GITLAB_TOKEN` / `$GERRIT_*`) |
| **C — SSH key** | Medium — key broader than per-repo tokens; `known_hosts` pinning fragile | Key provisioning + `openssh-client` + policy egress | Yes | No — separate credential channel |
| **D — Host seed** | None (secrets stay on host) | Makefile target + sandbox-upload plumbing | **No** — pull/push still need auth | N/A |

#### 4.4.7 Recommended Phasing

- **Phase 1 (landed)** — document the problem and ship the unblocker.  Public-host clones work after the image + policy delta; private-host clones return a clear HTTP 401 instead of a confusing "tool missing" refusal.  `.env.example` documents Option A as a short-term workaround with the leakage caveat called out explicitly.
- **Phase 3** — implement Option B (credential helper).  One shell script, two `git config --system` lines per host in the Dockerfile, one policy allowlist entry.  Handles the 90% case (authenticated clone + subsequent `pull` / `push`) without redesigning secret management.
- **Phase 4+** — evaluate Option D (host seed) if interactive Slack workflows frequently reference an operator's local checkout.  Useful for developer UX, not urgent for autonomous delegation.
- **Deferred** — Option C (SSH).  Only pick it up when a host surfaces that specifically *requires* SSH.

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

### 5.2 NMB Setup in Sub-Agents

Sub-agents connect to the NMB broker at startup. The broker URL is provided
via environment variable. Authentication is by sandbox identity.

### 5.3 Sandbox Configuration Layer (`/app/config.yaml`)

#### 5.3.1 Problem Statement

Today the sandbox receives configuration via three incomplete mechanisms:

1. **Provider-injected env vars** — OpenShell attaches providers (`make setup-providers`)
   and injects placeholder env vars that the L7 proxy resolves at HTTP request
   time. Only works for values that flow through HTTP headers (auth tokens).
2. **Dockerfile `ENV` directives** — static, baked at image build time. No
   per-deployment override.
3. **`in_sandbox` branches in `config.py`** — `_SANDBOX_CODING_WORKSPACE_ROOT`,
   `_SANDBOX_SKILLS_DIR`, and similar constants are selected when
   `OPENSHELL_SANDBOX` is set. Every new knob adds another `if in_sandbox:`
   branch, and two sandboxes running from the same image cannot have different
   settings.

This means **non-secret configuration** (log level, workspace root, feature
flags, tuning parameters) cannot be overridden per sandbox without rebuilding
the image. `openshell sandbox create` does not support `--build-arg` or a
general `--env KEY=VALUE` flag, so there is no clean escape hatch today.

#### 5.3.2 Three Categories of Configuration

The config problem is not a clean secret-vs-non-secret split. We actually have
**three** categories of values:

| Category | Example fields | Where it lives today | Problem |
|----------|----------------|----------------------|---------|
| **A — Public non-secret** | `log.level`, `orchestrator.model`, `audit.db_path`, `coding.workspace_root`, tuning knobs | Dataclass defaults + `in_sandbox` branches in `config.py`, Dockerfile `ENV`, scattered | Not in sandbox env; every new knob needs another `if in_sandbox:` branch |
| **B — Private non-secret** | `coding.git_clone_allowed_hosts`, `ENDPOINTS_NEEDING_ALLOWED_IPS`, `ALLOWED_IPS`, internal URLs | `.env` (gitignored) + `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS` constant in `config.py` | **`_SANDBOX_GIT_CLONE_ALLOWED_HOSTS` currently leaks internal NVIDIA hostnames into the public repo** |
| **C — Secret** | `SLACK_BOT_TOKEN`, `JIRA_AUTH`, `GITLAB_TOKEN`, inference API key | `.env` (gitignored) → OpenShell providers → proxy placeholders | Works fine — nothing to change |

Category B is the one your question flags. These values are not credentials,
so the provider/placeholder flow doesn't apply (the app reads them directly,
not via HTTP headers). But they *also* shouldn't ship in the public repo.
Today we work around this by inlining them as `_SANDBOX_*` constants in
`config.py`, which leaks them into every git push.

#### 5.3.3 Proposal: Public Base + Private Overlay + Build-Time Resolver

Mirror the existing `scripts/gen_policy.py` pattern (which already solves the
same problem for network-policy `allowed_ips` CIDRs):

| File | Purpose | Committed? | Shipped in image? |
|------|---------|------------|-------------------|
| `config/defaults.yaml` | Public non-secret defaults (category A) + fail-closed placeholders for category B | ✅ committed | ✅ (intermediate build input) |
| `.env` | Category B overrides + category C secrets | ❌ gitignored | ❌ never |
| `config/orchestrator.resolved.yaml` | Merged: defaults + category B values from `.env` | ❌ gitignored | ✅ as `/app/config.yaml` |
| `scripts/gen_config.py` | Reads `defaults.yaml` + `.env` → writes `resolved.yaml` | ✅ committed | — |

Build flow:

1. `make setup-sandbox` depends on `make gen-config` (new target, mirrors
   `gen-policy`).
2. `gen_config.py` reads `config/defaults.yaml`, applies overrides from `.env`
   for a *named allowlist* of category-B keys, writes
   `config/orchestrator.resolved.yaml`.
3. `docker/Dockerfile.orchestrator` has
   `COPY --chown=root:root config/orchestrator.resolved.yaml /app/config.yaml`.
4. The resolved file is gitignored (alongside `policies/orchestrator.resolved.yaml`).

Runtime flow:

1. App reads `/app/config.yaml` at startup — it already contains the merged
   category-A + category-B values baked in at build time.
2. Category-C secrets come in via env vars (OpenShell provider
   placeholders in the sandbox, `.env` on the host for tooling).
   Non-secret knobs do **not** have env-var overrides anymore — the
   YAML is the single source of truth.

OSS / CI consumers without a `.env`: `gen_config.py` emits a resolved file
that's byte-identical to `defaults.yaml`. All category-B fields hold their
fail-closed values (e.g. `git_clone_allowed_hosts: ""` → `git_clone` tool
refuses every URL), which is the correct security posture for a fresh clone.

#### 5.3.4 Schema

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

`scripts/gen_config.py` — explicit allowlist of the keys it will fill from
`.env`. Any other `.env` key is ignored (so an operator can't accidentally
inject arbitrary config):

```python
# Category-B keys: .env var name → YAML dotted path
_CATEGORY_B_KEYS: dict[str, str] = {
    "GIT_CLONE_ALLOWED_HOSTS":    "coding.git_clone_allowed_hosts",
    "GITLAB_URL":                 "toolsets.gitlab.url",
    "GERRIT_URL":                 "toolsets.gerrit.url",
    # Future: any other host/IP-flavoured override.
}
```

**Explicit non-goal**: secret-like fields (`auth_header`, `token`,
`api_token`, `username`, `http_password`, `bot_token`, `app_token`,
`user_token`, `api_key`, `jina_api_key`) are **deliberately** excluded from
both the YAML schema and the `gen_config.py` allowlist. They continue to
load from env vars — which, in the sandbox, are provider-injected
placeholders resolved by the L7 proxy. Mixing them into a file-based config
would either expose secrets in images or require yet another resolution
layer. `gen_config.py` refuses to write any key whose name matches the
forbidden suffix list.

#### 5.3.5 Configuration Precedence

Load order at runtime, lowest to highest precedence:

| # | Source | Scope | Notes |
|---|--------|-------|-------|
| 1 | Dataclass field defaults | Process-wide | Hardcoded in `config.py` |
| 2 | `/app/config.yaml` (the resolved file baked into the image) | Per-deployment | Missing file is not an error — treated as empty overlay |
| 3 | Environment variables — **secrets only** | Per-run | Credential env vars (`SLACK_BOT_TOKEN`, `JIRA_AUTH`, `GITLAB_TOKEN`, …) read by `_load_secrets_from_env`; `NEMOCLAW_CONFIG_PATH` selects the alternate YAML |
| 4 | Provider-injected env vars (secret placeholders) | Per-run | OpenShell-provider values resolved by the L7 proxy at HTTP-request time |

Rationale:

- **YAML is the only source for non-secret knobs.** Model names, URLs,
  paths, feature flags, and agent-loop parameters live in
  `config/defaults.yaml` (or a per-deployment YAML via
  `NEMOCLAW_CONFIG_PATH`).  No env-var overrides; operators who want
  to change a knob edit the YAML.
- **Secrets stay in env.** Tokens / API keys / usernames / passwords
  flow through the OpenShell provider → L7 proxy → process env path,
  unchanged.
- **Category-B values flow defaults → resolver → YAML → runtime** without
  ever touching `os.environ` inside the sandbox — so the L7 proxy's
  placeholder substitution (which only applies to HTTP headers) is never
  relevant for them.

#### 5.3.6 Loader Implementation

`AppConfig.load()` (classmethod) replaces the legacy `load_config()`.
Production entrypoints call one of two thin builders that name the
intent at the call site:

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
- `_load_secrets_from_env` reads the credential env vars (see
  §5.3.5 row 3) into the typed config.  In the sandbox these are
  OpenShell-provider-injected placeholders; on the host they come
  from `.env` via `load_dotenv_if_present`.
- `_sync_agent_loop_prompting_fields` propagates
  `orchestrator.model` / `temperature` / `max_tokens` into
  `agent_loop.*` when YAML hasn't pinned the latter.
- `_check_required_config` enforces Slack tokens (when
  `require_slack=True`) and `inference.base_url`.  Deliberately
  **not** checked: `INFERENCE_HUB_API_KEY` — the sandbox's inference
  provider is registered under a different credential name
  (`OPENAI_API_KEY`), and the L7 proxy injects the real key at
  HTTP-request time; the app never reads it.

#### 5.3.7 Migration (Landed in Phase 1)

The migration described below shipped in Phase 1 (PR #13) and the
Phase 1 follow-up (PR #14).

1. **Removed the leak**: `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS`,
   `_SANDBOX_CODING_WORKSPACE_ROOT`, and `_SANDBOX_SKILLS_DIR` constants
   deleted from `config.py`.  Values now live in `config/defaults.yaml`
   (public-safe versions) and — for the host-allowlist case — in
   `.env` via the resolver.
2. **`config/defaults.yaml`** ships with fail-closed / empty-string
   values for all category-B fields.
3. **`scripts/gen_config.py`** mirrors `scripts/gen_policy.py` and
   writes `config/orchestrator.resolved.yaml`; the resolved file is
   gitignored.
4. **Makefile** gained `gen-config`; `setup-sandbox` depends on both
   `gen-config` and the pre-existing `gen-policy`.
5. **`docker/Dockerfile.orchestrator`** copies the resolved file in as
   `/app/config.yaml` and the filesystem policy makes it read-only.
6. **`AppConfig.load()`** replaces the legacy `load_config()`; the
   `in_sandbox` branches for paths / allowlists are gone.  Non-secret
   env-var overrides (`LOG_LEVEL`, `AGENT_LOOP_*`, `NMB_URL`, etc.) were
   also removed — YAML is now the single source of truth for
   non-secret knobs.  New builders `create_orchestrator_config()` /
   `create_coding_agent_config()` name intent at the call site.
7. **Runtime detector** (`runtime.py`) produces `SANDBOX` /
   `INCONSISTENT` only — the `LOCAL_DEV` classification and the
   associated escape hatches were dropped; bare-process execution
   outside a sandbox is no longer supported.
8. **`.env.example`** is now a secrets-only + category-B-only document;
   stale `_SANDBOX_*` references and the `LOG_LEVEL` / `AGENT_LOOP_*` /
   `NMB_URL` / `AUDIT_*` / `CODING_*` / `SKILLS_*` optional-override
   blocks were removed.

#### 5.3.8 What Changes for the Operator

Before (pre-M2b P1):

```bash
# .env
GIT_CLONE_ALLOWED_HOSTS=url1.nvidia.com,url2.nvidia.com,github.com
```
- Worked locally (Makefile exports → `os.environ` → `load_config`).
- Did **not** work in the sandbox — the `.env` wasn't read inside;
  `_SANDBOX_GIT_CLONE_ALLOWED_HOSTS` constant was used instead, silently
  ignoring `.env`.

Now (post-M2b P1):

```bash
# .env — unchanged
GIT_CLONE_ALLOWED_HOSTS=url1.nvidia.com,url2.nvidia.com,github.com
```

- `make gen-config` reads `.env`, writes
  `config/orchestrator.resolved.yaml:coding.git_clone_allowed_hosts`.
- `make setup-sandbox` COPYs the resolved file into the image → the
  sandbox starts with the allowlist honoured.  No `_SANDBOX_*` constant
  in public code, no silent drift between host and sandbox.

#### 5.3.9 Policy-file Resolver (same pattern, different file)

The committed `policies/orchestrator.yaml` used to carry internal
hostnames (`gitlab-master.nvidia.com`, `git-av.nvidia.com`) inline
under `network_policies.gitlab.endpoints[0].host` and
`network_policies.gerrit.endpoints[0].host`.  Same category-B leak
class as the `config.py` constants above — different file, identical
resolver pattern:

- Base policy ships with `host: ""` placeholders (public-safe).
- `scripts/gen_policy.py` reads `.env`'s `GITLAB_URL` / `GERRIT_URL`,
  extracts the hostname via `urllib.parse.urlparse`, and substitutes
  into the matching network_policy at build time. The same env vars
  already drive `gen_config.py`'s `toolsets.{gitlab,gerrit}.url`, so
  operators don't maintain the hostname twice.
- Substitution runs *before* the existing `allowed_ips` injection so
  the SSRF-bypass matcher sees the final host.
- An empty `.env` leaves the placeholders in place — OpenShell
  rejects an empty-host endpoint at apply time, which is the correct
  fail-closed posture for an OSS consumer.  Malformed URLs without a
  scheme also fall through (detected via `urlparse`) and are treated
  as missing.

Mapping table lives next to `_CATEGORY_B_KEYS` in `gen_config.py` for
symmetry:

```python
# scripts/gen_policy.py
_POLICY_HOST_SUBSTITUTIONS: dict[str, str] = {
    "GITLAB_URL": "gitlab",
    "GERRIT_URL": "gerrit",
}
```

### 5.4 Sandbox Detection and Startup Self-Check

#### 5.4.1 Problem Statement

Before M2b P1, `in_sandbox` depended on a single signal:
`os.environ.get("OPENSHELL_SANDBOX")`.  The value is *never set by this
repo* — the code assumed OpenShell injects it automatically.  If the
convention changed (new OpenShell version, custom image, misconfigured
profile), the app silently fell back to local-dev defaults.  Symptoms
were all downstream and non-obvious:

- Inference calls failed (`INFERENCE_HUB_BASE_URL` required but empty).
- Audit DB written to `~/.nemoclaw/audit.db` inside a container with no
  persistent `~` directory.
- File tools rooted at `~/.nemoclaw/workspace` which doesn't exist in
  the sandbox filesystem policy.

§5.3 landed the YAML overlay so paths come from the resolved config
directly.  The runtime self-check below catches the residual failure
mode: "the YAML says one thing, but the environment the process is
actually running in looks nothing like a sandbox".

#### 5.4.2 Multi-Signal Detection

A new helper `detect_runtime_environment()` evaluates independent signals and
classifies the environment:

| # | Signal | Meaning |
|---|--------|---------|
| 1 | `OPENSHELL_SANDBOX` env var set | OpenShell's self-identification |
| 2 | `/sandbox` directory exists and is writable | PVC mount point present |
| 3 | `/app/src` exists and is read-only | Dockerfile-copied app source |
| 4 | `HTTPS_PROXY` env var set | L7 proxy is active |
| 5 | `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` set | OpenShell CA installed |
| 6 | `inference.local` DNS resolves | Inference proxy reachable |

Result is one of:

- `SANDBOX` — at least `_DEFAULT_SANDBOX_SIGNAL_THRESHOLD` (4 of 6)
  signals present.
- `INCONSISTENT` — below threshold.  Almost always a deployment bug
  (policy drift, OpenShell version skew, bare-process execution).
  `main.py` / `agent/__main__.py` refuse to start in this state.

The threshold lets us tolerate minor signal drift (e.g. the
`inference.local` DNS lookup is a network op and can be slow or
flaky) without weakening the check.  A test-only escape hatch
(`NEMOCLAW_SANDBOX_SIGNAL_THRESHOLD` env var) lowers the threshold
for subprocess integration tests that can only fake the three
env-based signals from a dev laptop; prod MUST NOT set it.

The earlier `LOCAL_DEV` classification and the associated escape
hatches (`_BENIGN_LOCAL_ENV_SIGNALS`, `_LOCAL_DEV_SIGNAL_CEILING`)
were removed in Phase 1 follow-up: the OpenShell sandbox is the only
supported runtime, so "not in a sandbox" is a deployment bug, not a
legitimate mode.

#### 5.4.3 Startup Self-Check

`main.py` and `agent/__main__.py` call `detect_runtime_environment()`
immediately after logging setup and before `AppConfig.load()`.  On
`INCONSISTENT`, they raise `SandboxConfigurationError` with a structured
log entry showing which signals were present and which were missing,
plus a suggested fix.  Example:

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

On `SANDBOX` the same signals are logged at INFO so operators can
inspect them in the running log without special tooling.

#### 5.4.4 Interaction with Config Loading

The self-check runs *before* `AppConfig.load()`, so the loader can
trust that it's inside a healthy sandbox without re-deriving the
classification.  There's no `env=` kwarg to thread through — the
`RuntimeEnvironment` never reaches the loader.  What the loader sees
is the resolved YAML at `/app/config.yaml` plus the secret env vars
the proxy injected; both assume the self-check already passed.

#### 5.4.5 Why Not Trust `OPENSHELL_SANDBOX` Alone?

A multi-signal check costs roughly 50 lines of Python and one extra
log line per startup.  The alternative — trusting a single env var —
has already caused one class of silent failure (pre-M2b `load_config()`
happily producing a config with an empty `INFERENCE_HUB_BASE_URL` if
that env var was missing while `OPENSHELL_SANDBOX` was set, or vice
versa).  The self-check is cheap insurance against the full family of
"I thought I was in a sandbox" bugs, not just the specific
`OPENSHELL_SANDBOX` case.

---

## 6  Orchestrator ↔ Sub-Agent Protocol

*Full specification: [original §9](design_m2.md#9--orchestrator--sub-agent-protocol)*

### 6.1 Message Flow

```
Orchestrator                    NMB Broker                    Coding Agent
    │                              │                              │
    │── task.assign ──────────────▶│──────────────────────────────▶│
    │                              │                              │
    │                              │◀─── task.progress (opt) ─────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │                              │◀─── audit.flush ─────────────│
    │◀─────────────────────────────│                              │
    │                              │                              │
    │                              │◀─── task.complete ───────────│
    │◀─────────────────────────────│                              │
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

---

## 7  Work Collection and Finalization

*Full specification: [original §10](design_m2.md#10--work-collection-and-finalization)*

### 7.1 Model-Driven Finalization

After receiving `task.complete`, the orchestrator runs a second `AgentLoop`
invocation with **finalization tools**. The model sees the sub-agent's result
(diff, notes file, summary, test output) and **synthesizes** it before deciding
what to do:

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
  directly (see §5.3.6 discussion).
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
| Add `ToolSpec.is_core` flag; default `True` (no behaviour change) | `tools/registry.py` | ✅ |
| Extend `ToolRegistry`: `search()`, `mark_surfaced()`, `reset_tool_surface()`, `core_names` / `non_core_names` / `surfaced_non_core` properties; make `tool_definitions()` core-only by default with surfaced non-core tools opted back in | `tools/registry.py` | ✅ |
| Implement `tool_search` meta-tool (keyword search over all registered specs; marks matches as surfaced so the next inference round sees them) | `tools/tool_search.py` | ✅ |
| `AgentLoop`: refresh `tool_defs` per round (drop the once-per-run snapshot) and call `reset_tool_surface()` at the start of `run()` | `agent/loop.py` | ✅ |
| Integrate into orchestrator: register `tool_search`; mark every service tool (Jira / GitLab / Gerrit / Confluence / Slack search / web search) `is_core=False` at its `@tool` definition site so prompt visibility is declared alongside the tool itself | `tools/{jira,gitlab,gerrit,confluence,slack_search,web_search}.py`, `tools/tool_registry_factory.py` | ✅ |
| Integrate into coding sub-agent: register `tool_search` (coding + skill tools stay `is_core=True`; the meta-tool is a no-op until non-core tools are added later) | `tools/tool_registry_factory.py` | ✅ |
| Unit tests: registry surface state, `search()` relevance, `tool_search` handler (limit floor/ceiling, surfacing side-effect, no-match no-op) | `tests/test_tool_search.py` — `TestIsCoreDefault`, `TestRegistrySurface`, `TestRegistrySearch`, `TestToolSearchTool`, `TestNonCoreServiceToolsetsList` | ✅ |
| Integration tests: factory flips service toolsets non-core, default tool-defs exclude them, `tool_search` surfaces service tools end-to-end, core surface is strictly smaller than full surface | `tests/test_tool_search.py::TestFullToolRegistryIntegration`, `tests/test_tool_search.py::TestCodingToolRegistryRegistersToolSearch` | ✅ Registry-level.  A subprocess-level turn (mock inference emits `tool_search` → `search_jira` → text reply) is deferred to Phase 3, where the orchestrator's `AgentLoop`-driven delegation flow comes online. |

**Exit criteria:**

- ✅ `ToolSpec.is_core=False` tools are excluded from the default
  `tool_definitions()` output
  (`tests/test_tool_search.py::TestRegistrySurface::test_default_tool_definitions_excludes_non_core`).
- ✅ `tool_search` takes a natural-language query, returns matching
  non-core specs, and marks them as surfaced so they appear in the
  next inference round's `tools` list
  (`tests/test_tool_search.py::TestToolSearchTool::test_tool_search_returns_matches_and_surfaces_them`).
- ✅ Orchestrator's service tools (Jira / GitLab / Gerrit / Confluence /
  Slack search / web search) default to non-core; the default
  orchestrator prompt's tool block is strictly smaller than the full
  surface whenever a service is enabled
  (`tests/test_tool_search.py::TestFullToolRegistryIntegration::{test_service_tools_are_non_core_after_factory,test_default_prompt_surface_shrinks}`).
  Structural invariant (core-only count < 0.75 × full count) is
  tested here; a concrete model-specific token measurement against a
  fixture prompt is deferred to Phase 3 alongside the delegation
  test harness.
- ✅ Coding sub-agent registers `tool_search` for future-compat; its
  coding tool suite stays fully core so current sub-agent flows are
  unchanged
  (`tests/test_tool_search.py::TestCodingToolRegistryRegistersToolSearch`).
- 🟡 A full delegation turn (orchestrator calls `tool_search("jira")`
  → receives `search_jira` → calls `search_jira(...)` → gets a result)
  completes end-to-end in an integration test against the mock
  inference server.  **Deferred to Phase 3** — needs the
  orchestrator-side `AgentLoop` driver that Phase 3 wires up; the
  per-round refresh + surface state plumbing it relies on is already
  in place and covered at the unit / registry-integration level.

### Phase 3 — Orchestrator delegation, NMB event loop, concurrency caps, and finalization

| Task | Files | Status |
|------|-------|--------|
| Create `delegate_task` tool for the orchestrator | `tools/delegation.py` | ⏳ Pending |
| Implement spawn → workspace setup → `task.assign` flow | `orchestrator/delegation.py` | ⏳ Pending |
| Implement per-agent `asyncio.Semaphore` concurrency control | `orchestrator/delegation.py` | ⏳ Pending |
| Implement `max_spawn_depth` and `max_children_per_agent` limits | `orchestrator/delegation.py` | ⏳ Pending |
| Implement NMB event loop (`start_nmb_listener`, event dispatch) | `orchestrator/orchestrator.py` | ⏳ Pending |
| Implement finalization tools (`present_work_to_user`, `push_and_create_pr`, `discard_work`, `re_delegate`, `destroy_sandbox`) | `tools/finalization.py` | ⏳ Pending |
| Implement `_finalize_workflow` (build context, run `AgentLoop` with finalization tools) | `orchestrator/orchestrator.py` | ⏳ Pending |
| Implement `AuditBuffer` with NMB-batched flush + JSONL fallback | `agent/audit_buffer.py` | ⏳ Pending |
| Integration test: orchestrator → coding agent → result → finalize | `tests/integration/test_delegation.py` | ⏳ Pending |

**Exit criteria:** Orchestrator delegates coding tasks, collects results, runs
finalization. Concurrency caps enforced. Audit flush works via NMB and fallback.

### Phase 4 — At-least-once NMB delivery

| Task | Files | Status |
|------|-------|--------|
| Implement reliable send (persist → send → ack → delete) | `nmb/reliable_send.py` | ⏳ Pending |
| Implement NMB crash recovery (replay pending on startup) | `nmb/broker.py` | ⏳ Pending |
| Tests for reliable send and crash recovery | `tests/test_reliable_send.py` | ⏳ Pending |

**Exit criteria:** Critical messages (`task.complete`, `audit.flush`) survive
broker crashes and are replayed on restart.

### Phase 5 — Basic operational cron

| Task | Files | Status |
|------|-------|--------|
| Implement `CronWorker` with hardcoded operational jobs | `orchestrator/cron.py` | ⏳ Pending |
| Implement TTL watchdog, stale-session cleanup, health check jobs | `orchestrator/cron.py` | ⏳ Pending |
| Tests for cron execution | `tests/test_cron.py` | ⏳ Pending |

**Exit criteria:** Operational cron jobs run on schedule.

### Phase 6 — Polish, hardening, and gaps document

| Task | Files | Status |
|------|-------|--------|
| Progress relaying to Slack | `orchestrator/delegation.py` | ⏳ Pending |
| File tool edge case hardening (symlinks, binary files, encoding) | `tools/files.py` | ⏳ Pending |
| Create `docs/DEFERRED.md` — features punted from M2b | `docs/DEFERRED.md` | ⏳ Pending |

**Exit criteria:** Production-quality delegation with cleanup guarantees,
progress reporting, and robust handling.

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
| Delegation concurrency cap | Semaphore blocks at `max_concurrent_tasks`; unblocks on completion | ⏳ Pending (Phase 3) |
| Delegation spawn depth cap | `max_spawn_depth` exceeded → delegation rejected with error | ⏳ Pending (Phase 3) |
| NMB reliable send | Message persisted to disk before send; deleted after ack | ⏳ Pending (Phase 4) |
| NMB crash recovery | Pending messages replayed on broker startup | ⏳ Pending (Phase 4) |
| `ToolSearch` meta-tool | Returns correct tools for keyword queries; non-core excluded from prompt | ✅ `tests/test_tool_search.py` — `TestRegistrySearch` (scoring), `TestRegistrySurface::test_default_tool_definitions_excludes_non_core`, `TestToolSearchTool::test_tool_search_returns_matches_and_surfaces_them`, `TestFullToolRegistryIntegration::test_default_prompt_surface_shrinks` |
| Finalization tools | Each tool produces correct output with mock sandbox/git | ⏳ Pending (Phase 3) |
| Cron scheduling | Jobs fire at correct intervals; missed jobs caught up | ⏳ Pending (Phase 5) |

### 16.2 Integration Tests

| Test | What it verifies | Status |
|------|-----------------|--------|
| Sandbox boot — happy path | `make run-local-sandbox` → log shows `classification: SANDBOX` with all 6 signals present | 🟡 Manual smoke only — the positive path needs a real OpenShell sandbox so `/sandbox`, `/app/src`, and `inference.local` DNS resolve.  Automation would require mocking OpenShell itself, out of scope for unit / integration tests.  The runtime classifier logic is fully covered at the unit level (`tests/test_runtime.py::TestClassification`); operators verify the end-to-end wiring with `make run-local-sandbox`. |
| Sandbox boot — broken env | Manually unset `OPENSHELL_SANDBOX` in a test image → self-check fails with `INCONSISTENT` and the process exits nonzero before Slack connects | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_inconsistent_runtime_fails_fast` — spawns `python -m nemoclaw_escapades.agent` with an env mix the multi-signal detector classifies `INCONSISTENT`; asserts non-zero exit and `SandboxConfigurationError` + `refusing to start` on stderr, before any config load or I/O. |
| Config YAML — deployment override | Mount a custom `config.yaml` over the default → `coding.workspace_root` picks up the override without a rebuild | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_honours_yaml_deployment_override` — custom YAML via `NEMOCLAW_CONFIG_PATH` directs the sub-agent's workspace root without a rebuild; asserts the per-agent subdir lands under the YAML-supplied path. |
| Sub-agent NMB lifecycle | Connect, `sandbox.ready`, `task.assign`, `task.complete` | 🟡 Partial — (a) NMB wire-level transport covered by `tests/integration/test_lifecycle.py::{TestSandboxConnect,TestSandboxDisconnect,TestSandboxReconnect}`; (b) sub-agent-side connect / close wiring (reads broker URL + sandbox id from `config.nmb`, calls `connect_with_retry`, closes on shutdown) covered by `tests/test_coding_agent_main.py::TestNmbMode`; (c) `task.assign` / `task.complete` protocol body awaits Phase 3 |
| Coding agent end-to-end | Agent receives task, uses file tools, returns diff | ✅ `tests/test_integration_coding_agent.py::test_agent_subprocess_executes_file_tool_call` — subprocess + stateful OpenAI-format mock serves a `write_file` tool_call then a terminating reply; assertions: file lands on disk inside the per-agent workspace, final reply reaches stdout. |
| Orchestrator delegation full flow | Spawn → assign → complete → finalize → cleanup | ⏳ Pending (Phase 3) |
| Delegation concurrency enforcement | Third delegation waits when `max_concurrent_tasks=2` | ⏳ Pending (Phase 3) |
| Model-driven finalization | `task.complete` → model calls `present_work_to_user` → user clicks [Push & PR] | ⏳ Pending (Phase 3) |
| Iteration flow | User feedback → `re_delegate` → same agent → updated result | ⏳ Pending (Phase 3) |
| Concurrent finalization | Two sub-agents complete simultaneously; both finalize concurrently | ⏳ Pending (Phase 3) |
| NMB at-least-once delivery | Kill broker after persist, restart, verify replay | ⏳ Pending (Phase 4) |
| Audit NMB flush + fallback | Tool calls arrive via NMB batch and/or JSONL fallback | ⏳ Pending (Phase 3) |
| TTL watchdog | Watchdog fires → sub-agent process killed → workspace cleaned | ⏳ Pending (Phase 5) |

### 16.3 Safety Tests

| Test | What it verifies | Status |
|------|-----------------|--------|
| Tool surface enforcement | Sub-agent cannot use tools not in its `tool_surface` | 🟡 Partial — Phase 1 enforces by *construction*: `create_coding_tool_registry` deliberately omits `git_commit` (orchestrator-only, per §7.1), so the sub-agent's `ToolRegistry` has no entry the model could invoke.  Covered by `tests/test_git_tools.py::TestGitToolRegistration::test_include_commit_false_omits_git_commit` and `tests/test_file_tools.py::TestCodingToolRegistry::test_factory_creates_sub_agent_tool_surface`.  Phase 3 will add a runtime allow-list check for the broader "tool_surface"-as-policy story (e.g. orchestrator-granted per-task tool restrictions). |
| Workspace path sandboxing | File tools cannot access outside `/sandbox/workspace/` | ✅ `tests/test_file_tools.py::TestSafeResolve`, `TestReadFile::test_read_path_escape_blocked`, `::test_read_absolute_path_blocked`, `TestWriteFile::test_write_path_escape_blocked` (M2a) |
| No recursive delegation | Coding agent cannot spawn sub-agents | ⏳ Pending (Phase 3) |
| Notes file size cap | Enforced at the orchestrator when reading back the notes file — large writes are truncated before being fed into finalization context | ⏳ Pending (Phase 3) |

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

---

## 17  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should sub-agent system prompts be generated by the orchestrator or stored as static templates? | Start with templates, evolve to generation. |
| 2 | How should the orchestrator decide when to delegate vs. handle itself? | Start with explicit user intent; evolve to model-driven routing. |
| 3 | Git finalization: orchestrator pull+push (Strategy A) or sub-agent push directly (Strategy B)? | Strategy A maximizes isolation; Strategy B is simpler. See [original §10.4](design_m2.md#104--git-operations-for-finalization). |

---

### Sources

- [Original M2 Design Document](design_m2.md)
- [M2a Design Document](design_m2a.md) — reusable `AgentLoop`, file tools, compaction, skills
- [NMB Design](nmb_design.md) — inter-sandbox messaging protocol
- [Sandbox Spawn Design](sandbox_spawn_design.md) — OpenShell sandbox lifecycle
- [Audit DB Design](audit_db_design.md) — tool-call auditing, NMB-batched flush
- [Build Your Own OpenClaw Deep Dive](deep_dives/build_your_own_openclaw_deep_dive.md) — §4 (event bus), §7 (routing), §8 (dispatch), §9 (concurrency), §13 (cron)
- [GAPS.md](../GAPS.md) — feature tracking
