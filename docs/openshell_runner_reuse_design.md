# OpenShell Runner Reuse Design

> **Source MR:** [aire/scratch/nemo-agent-hub!552](https://gitlab-master.nvidia.com/aire/scratch/nemo-agent-hub/-/merge_requests/552)
>
> **Source title:** Add OpenShell-backed coding runner
>
> **Last updated:** 2026-04-30

---

## 1  Purpose

AgentHub MR !552 adds an OpenShell-backed coding runner that runs
OpenCode inside a fresh OpenShell sandbox, with per-repository policy
storage and denial-driven policy proposals.  This document records what
NemoClaw should reuse from that MR, what should be adapted, and what
should not be copied directly.

The most relevant reusable pieces are:

1. Policy generation and normalization for repository-scoped OpenShell
   policies.
2. Policy denial parsing into structured proposals.
3. The external "runner worker" pattern that owns OpenShell control-plane
   access.
4. The code-runner script shape: upload job payload, clone repo inside the
   sandbox, configure TLS, run the coding tool, commit/push, and return
   structured output.
5. Operational learnings around Docker socket access, private GitLab
   `allowed_ips`, OpenShell TLS CA, and nonblocking startup denials.

---

## 2  MR Summary

MR !552 adds a parallel OpenShell runner path beside AgentHub's existing
Squid-backed OpenCode worker:

| Area | MR implementation | Reuse value for NemoClaw |
|------|-------------------|--------------------------|
| Worker | `openshell-worker` Docker Compose service consuming Redis queue `openshell:jobs`. | Useful as a host/control-plane-side runner pattern for M3 sandbox spawning. |
| Policy model | `OpenShellRepoPolicy` DB table with `repo_url`, JSONB `policy`, `revision`, timestamps. | Useful conceptually; NemoClaw can start with file-backed or SQLite-backed policy records. |
| Policy helpers | `default_policy_for_repo`, `normalize_policy`, `render_policy_yaml`, `parse_policy_denials`. | Directly reusable with local naming and tests. |
| API | `/api/openshell/policies/repo` GET/PUT endpoints. | Useful later for dashboard/admin UI; not needed for first CLI-driven prototype. |
| Runner library | `run_openshell_agent(...)` queues jobs and polls `openshell:result:{job_id}`. | Reusable if NemoClaw adopts an async worker; otherwise adapt to direct function calls. |
| Code runner | Worker starts gateway, creates providers, sets inference, uploads job files, creates sandbox, runs a generated shell script, captures logs, parses denials. | Strong template for NemoClaw's OpenShell coding runner. |
| Validation | Live issue-to-MR run created a branch, committed, pushed, and opened MR !555. | Confirms OpenShell can support a full GitLab code-writing flow. |

---

## 3  Policy Generation Mechanism

### 3.1 What AgentHub Built

The MR adds `src/platform/openshell_runner/policy.py` with four core
capabilities:

1. **Default policy for a repo.**
   `default_policy_for_repo(repo_url)` extracts the repo host and builds
   a minimal OpenShell policy:

   - `version: 1`
   - static `filesystem_policy`
   - `landlock.compatibility: best_effort`
   - `process.run_as_user/group: sandbox`
   - `network_policies.gitlab_git_https`
   - Git binaries only: `/usr/bin/git`, git remote helpers
   - HTTPS endpoint for the repo host on port `443`
   - `protocol: rest`, `enforcement: enforce`, `access: read-write`

2. **Private IP allow-listing.**
   `private_allowed_ips_for_host(host)` resolves the Git host and includes
   concrete private IP addresses when they fall in:

   - `10.0.0.0/8`
   - `172.16.0.0/12`
   - `192.168.0.0/16`
   - `fc00::/7`

   This is specifically to satisfy OpenShell SSRF protection for internal
   GitLab hosts like `gitlab-master.nvidia.com`.

3. **Normalization.**
   `normalize_policy(policy)` fills missing static sections before
   rendering, so a user-edited policy fragment does not accidentally omit
   `filesystem_policy`, `landlock`, or `process`.

4. **Stable rendering.**
   `render_policy_yaml(policy)` uses YAML safe dump with stable ordering
   so the worker can write a policy file and pass it to
   `openshell sandbox create --policy`.

### 3.2 What NemoClaw Should Reuse

NemoClaw should reuse the policy helper pattern, but adapt persistence to
this repo's simpler runtime:

| Mechanism | Reuse recommendation |
|-----------|----------------------|
| Repo host extraction | Reuse directly.  It is cleaner than string-splitting clone URLs. |
| Private IP resolution | Reuse directly, but keep it behind an explicit resolver step so generated policies remain public-safe. |
| Static-section normalization | Reuse directly.  It solves the same problem as our current `policies/orchestrator.yaml` base policy and `scripts/gen_policy.py` overlay. |
| YAML rendering | Reuse directly.  NemoClaw already depends on PyYAML. |
| Postgres JSONB policy table | Do not copy initially.  Start with file-backed policy records or the existing SQLite audit DB if persistence becomes necessary. |
| Admin REST API | Defer until the dashboard/admin UI exists.  It is a later surface, not needed for the first runner. |

### 3.3 Proposed NemoClaw Shape

Add a small package:

```text
src/nemoclaw_escapades/openshell/
  __init__.py
  policy.py
  policy_store.py
  denials.py
```

Initial responsibilities:

- `policy.py`
  - `default_policy_for_repo(repo_url: str) -> dict[str, Any]`
  - `normalize_policy(policy: dict[str, Any]) -> dict[str, Any]`
  - `render_policy_yaml(policy: dict[str, Any]) -> str`
  - `private_allowed_ips_for_host(host: str) -> list[str]`

- `policy_store.py`
  - MVP: file-backed store under `policies/repos/*.yaml`
  - Later: SQLite-backed revisions if the dashboard needs policy history

- `denials.py`
  - `PolicyProposal`
  - `parse_policy_denials(log_text: str) -> list[PolicyProposal]`

For this repo, the policy store should be conservative:

- Keep committed defaults public-safe.
- Keep internal resolved hosts/IPs in generated or ignored files.
- Never write internal `allowed_ips` into committed baseline policy files.
- Treat generated policy artifacts the same way `policies/orchestrator.resolved.yaml`
  is treated today.

---

## 4  Denial Parsing and Policy Proposals

### 4.1 What AgentHub Built

AgentHub parses OpenShell logs into structured `PolicyProposal` records.
It recognizes at least three denial shapes:

1. OCSF network denials:

```text
DENIED /usr/bin/curl(64) -> httpbin.org:443 [reason:no matching policy]
```

2. HTTP denials:

```text
HTTP:POST ... DENIED POST https://api.github.com/user/repos
```

3. L7 proxy denials:

```text
l7_decision=deny dst_host=... l7_action=GET l7_target=...
```

The proposal captures:

- `host`
- `port`
- `binary`
- `reason`
- optional HTTP `method`
- optional HTTP `path`

It can render an `openshell policy update ...` command for human
approval.  This is explicitly not auto-applied in the MR.

### 4.2 What NemoClaw Should Reuse

This is one of the strongest reuse candidates.  NemoClaw's dashboard and
Slack approval flow both need a structured representation of "the sandbox
tried to reach X and policy blocked it."

Reuse with local adjustments:

- Keep deduplication by `(host, port, binary, method, path)`.
- Preserve the raw denial line alongside the parsed fields.  The MR does
  not keep raw text in `PolicyProposal`, but NemoClaw should, because it
  helps explain approvals in Slack and dashboard UI.
- Add a `scope` field for proposed policy updates:
  - `repo`
  - `agent_role`
  - `one_run`
  - `global`
- Add a `risk_level` heuristic:
  - Git host for assigned repo: low/medium
  - package registries: medium
  - arbitrary internet host: high
  - private IP/CIDR expansion: high

### 4.3 Approval Integration

The MR returns proposals from the worker result and leaves runtime Slack
approval for future work.  NemoClaw already has approval gating patterns,
so the integration should be:

1. Runner captures OpenShell logs.
2. `parse_policy_denials()` returns proposals.
3. Orchestrator posts proposed policy changes to Slack/dashboard.
4. User approves a bounded policy update.
5. Trusted host-side runner applies policy update.
6. Job is retried or re-delegated.

Important: the sandboxed agent should not apply its own policy updates.
Policy mutation remains a control-plane operation.

---

## 5  Code Runner Pattern

### 5.1 What AgentHub Built

AgentHub's worker is an external control-plane process.  It performs the
operations that should not live inside an LLM-steered sandbox:

1. Poll Redis for jobs.
2. Create a per-job OpenShell gateway:

```text
openshell gateway start --name agenthub-{job_id} --port ... --gateway-host ... --recreate
```

3. Create providers:

```text
openshell provider create --name ... --type gitlab --credential GITLAB_TOKEN
openshell provider create --name ... --type openai --credential OPENAI_API_KEY
```

4. Configure inference:

```text
openshell inference set --provider ... --model ... --timeout ...
```

5. Write a job directory:

```text
policy.yaml
prompt.md
opencode.json
run_job.sh
preloaded/
```

6. Upload that directory into the sandbox with:

```text
--upload <job_dir>:/sandbox/job
```

7. Run a sandbox command:

```text
bash -lc "<job env> bash /sandbox/job/run_job.sh"
```

8. Fetch sandbox logs and parse denial proposals.
9. Return a structured result:

```json
{
  "success": true,
  "output": "...",
  "error": "",
  "branch_name": "...",
  "policy_proposals": [...]
}
```

10. Clean up sandbox, gateway, and job files unless debugging is enabled.

### 5.2 Runner Script Learnings

The generated `run_job.sh` has several details worth reusing:

- Set `HOME` and create the tool config directory.
- Copy tool config from `/sandbox/job`.
- If `SSL_CERT_FILE` is set, configure Git:

```sh
export GIT_SSL_CAINFO="${SSL_CERT_FILE}"
git config --global http.sslCAInfo "${SSL_CERT_FILE}"
```

- Clone over HTTPS with a token-bearing URL.
- Use different token username conventions:
  - GitLab: `oauth2:<token>`
  - GitHub: `x-access-token:<token>`
- Create or check out the requested branch.
- Copy preloaded files into the workspace.
- Run the coding tool.
- Let the coding tool override commit message via `.commit_message`.
- Remove `.commit_message` before `git add -A` so it is not committed.
- Print the current branch so the worker can extract it.
- Push only when explicitly requested.

### 5.3 What NemoClaw Should Reuse

NemoClaw should reuse the "external runner owns OpenShell" shape, but
the command inside the sandbox should run NemoClaw's coding agent rather
than OpenCode once M3 moves sub-agents into separate sandboxes.

Proposed runner abstraction:

```python
class OpenShellCodeRunner:
    async def run(self, job: CodeRunnerJob) -> CodeRunnerResult:
        ...
```

Initial `CodeRunnerJob` fields:

- `job_id`
- `prompt`
- `repo_url`
- `branch_name`
- `commit_message`
- `workspace_baseline`
- `model`
- `timeout_s`
- `preloaded_files`
- `push_branch`
- `policy_ref`

Initial `CodeRunnerResult` fields:

- `success`
- `output`
- `error`
- `branch_name`
- `diff`
- `policy_proposals`
- `audit_paths`
- `sandbox_name`
- `gateway_name`

For M3 NMB-backed sub-agents, the runner can either:

- run a one-shot coding command inside the sandbox and return output, or
- start `python -m nemoclaw_escapades.agent --nmb` inside the sandbox and
  let the orchestrator communicate through NMB.

The second path better matches NemoClaw's architecture, but it still
needs the cross-sandbox NMB reachability proof from `docs/design_dashboard.md`.

---

## 6  Operational Learnings to Carry Forward

### 6.1 Docker Socket and Privilege

OpenShell can run from Docker Compose, but the runner needs access to the
host Docker socket because the OpenShell CLI starts its own gateway and
K3s/container stack.  AgentHub's compose service mounts:

```text
${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock
```

and parameterizes `DOCKER_SOCKET` for rootless Docker or nonstandard
runtimes.

For NemoClaw, this means:

- Do not put this capability in a model-controlled sandbox.
- Keep the runner as an operator-owned host process or dedicated trusted
  worker.
- Treat Docker socket access as high blast radius.
- Document that it is not equivalent to a normal unprivileged agent.

### 6.2 `host.docker.internal`

AgentHub sets:

```text
extra_hosts:
  - "host.docker.internal:${DOCKER_HOST_GATEWAY:-host-gateway}"
```

and passes `OPENSHELL_GATEWAY_HOST=host.docker.internal`.

This mirrors NemoClaw's own OpenShell prototypes: nested Docker/OpenShell
topologies often need an explicit host-gateway rendezvous.  NemoClaw
should keep this configurable rather than hardcoding one gateway address.

### 6.3 Internal GitLab Requires `allowed_ips`

Internal GitLab resolves to private IPs.  OpenShell SSRF protection
rejects private-address targets unless concrete IPs are explicitly
allowed.  AgentHub resolves private IPs during policy generation and adds
them to the endpoint.

NemoClaw should reuse that behavior, but only in generated/private
policy files.  The committed base policy should keep hostnames and
private IPs out of the repo.

### 6.4 TLS CA for Inspected Git

When OpenShell inspects HTTPS Git traffic, Git needs to trust the
OpenShell CA.  AgentHub's runner uses:

```sh
export GIT_SSL_CAINFO="${SSL_CERT_FILE}"
git config --global http.sslCAInfo "${SSL_CERT_FILE}"
```

NemoClaw should copy this into any Git-capable OpenShell runner script.
Without it, policy-correct Git clone/fetch/push can still fail at TLS
verification time.

### 6.5 Nonblocking Startup Denials

OpenCode attempted calls to:

- `models.dev`
- `registry.npmjs.org`
- `github.com`

Those calls were denied and did not block the proof, so AgentHub kept
them out of the baseline allow-list.

NemoClaw should preserve this discipline:

- Do not widen policy just because a tool probes optional services.
- Parse and report denials.
- Only approve endpoints that are required for the task or role.

### 6.6 Providers Keep Secrets Out of the Agent

The live proof validated that GitLab and Inference Hub credentials can be
provided through OpenShell providers rather than plaintext environment
variables visible to the coding agent.

NemoClaw should keep this invariant:

- Git credentials are provider-injected or proxy-mediated.
- Inference goes through `inference.local`.
- The agent process should not receive raw tokens unless no OpenShell
  provider path exists.

---

## 7  Design Impact for NemoClaw

### 7.1 Dashboard

The dashboard design should eventually expose policy proposals as a
read-only observability surface and, later, as an approval surface.
Reusable MR concepts:

- policy proposal schema,
- denial parsing,
- policy revision history,
- per-repo policy display,
- result payload fields (`policy_proposals`, `output`, `error`).

Do not add dashboard write controls until:

- browser-to-dashboard auth is decided,
- broker/dashboard ACLs exist,
- policy mutation is routed through a trusted runner or host-side
  approval service.

### 7.2 M3 Sub-Agent Sandboxes

The MR's worker pattern is the cleanest answer to "who is allowed to call
OpenShell control-plane commands?"

Recommended M3 shape:

```text
Orchestrator sandbox
  -> NMB request / approved tool call
  -> trusted OpenShell runner process
  -> openshell sandbox create
  -> sub-agent sandbox
  -> NMB broker
```

This avoids giving the LLM-controlled orchestrator broad Docker socket or
gateway control-plane access.

### 7.3 Policy Storage

Start with file-backed policy profiles:

```text
policies/repos/
  gitlab-master.nvidia.com__group__repo.yaml      # gitignored resolved policy
  examples/
    public-github.yaml                            # committed example
```

Later, if dashboard editing is needed, add a SQLite table:

```text
openshell_repo_policies
  id
  repo_url
  policy_json
  revision
  created_at
  updated_at
```

That mirrors AgentHub's schema without forcing a database dependency into
the first prototype.

---

## 8  Reuse Plan

### Phase R0 - Capture Helpers

- Add `openshell/policy.py` and `openshell/denials.py`.
- Port tests from `tests/test_openshell_runner_policy.py`.
- Keep storage out of scope.

Exit criterion: policy generation, normalization, rendering, and denial
parsing are unit-tested locally.

### Phase R1 - File-Backed Policy Store

- Add a small file-backed policy store.
- Generate repo-specific resolved policies from `.env`/operator input.
- Keep generated private policies gitignored.

Exit criterion: a repo URL can produce a runnable policy file with
private `allowed_ips` only in ignored output.

### Phase R2 - Runner Prototype

- Adapt AgentHub's worker flow into a NemoClaw prototype under
  `prototypes/openshell_code_runner/`.
- Start OpenShell gateway.
- Create providers.
- Set inference.
- Upload job payload.
- Create sandbox.
- Run a simple coding command.
- Capture logs and denial proposals.

Exit criterion: a local smoke test clones a repo, makes a trivial change,
and returns structured output plus denials.

### Phase R3 - NMB Sub-Agent Integration

- Replace the one-shot command with `python -m nemoclaw_escapades.agent --nmb`.
- Connect the spawned sandbox to NMB.
- Route `task.assign` / `task.complete` through the existing protocol.

Exit criterion: production delegation no longer uses `subprocess`; it
spawns an OpenShell sandbox through the trusted runner and passes the
existing delegation tests plus a live smoke test.

### Phase R4 - Approval Loop

- Surface `PolicyProposal` objects in Slack and dashboard.
- Require human approval before applying proposed policy changes.
- Record approved/rejected proposals in audit.

Exit criterion: a blocked endpoint produces a reviewable proposal, the
operator approves it, the runner applies it, and the job can be retried.

---

## 9  What Not to Copy Directly

- **Do not copy Redis as a hard dependency.**  NemoClaw can use a direct
  host-side runner call first.  Add a queue only if we need concurrent,
  persistent job processing.
- **Do not copy the Postgres policy table first.**  File-backed or
  SQLite-backed policy storage fits this repo better initially.
- **Do not let the sandbox mutate OpenShell policy.**  Keep policy update
  commands outside the constrained agent.
- **Do not auto-approve parsed denials.**  Denials are evidence, not
  authorization.
- **Do not put internal resolved IPs in committed policy files.**  Keep
  them generated and ignored.
- **Do not broaden baseline policy for optional tool probes.**  The MR's
  `models.dev`, npm, and GitHub finding is a reminder to distinguish
  optional startup chatter from required task egress.

---

## 10  Open Questions

| # | Question | Notes |
|---|----------|-------|
| Q1 | Should the first NemoClaw runner be direct-call or queue-backed? | Direct-call is simpler and matches current Makefile/prototype flow. Queue-backed is better for long-running dashboard jobs. |
| Q2 | Should policy revisions live in files, audit DB, or a new DB table? | Start with files; move to SQLite/table only when UI editing or revision history is needed. |
| Q3 | How do we map policy proposal scopes to approval UX? | Need separate treatment for one-run, repo, role, and global policy updates. |
| Q4 | Can the runner be reused for dashboard sandbox creation? | Probably yes for image/policy creation, but dashboard lifecycle is long-running while coding jobs are per-task. |
| Q5 | Which OpenShell denial log formats are stable? | AgentHub regexes cover observed formats, but NemoClaw should keep raw log lines and test against our own prototypes. |
| Q6 | Where should Docker socket access live in production? | It should remain with a trusted operator-side runner, never inside a model-controlled sandbox. |
