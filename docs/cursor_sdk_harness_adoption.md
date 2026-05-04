# Cursor SDK Agent Harness - Adoption Notes

> **Sources:** [Cursor SDK announcement](https://cursor.com/blog/typescript-sdk),
> [Cursor Cookbook](https://github.com/cursor/cookbook/tree/main), and
> [TypeScript SDK docs](https://cursor.com/docs/sdk/typescript)
>
> **Related:** [M2a - Reusable Agent Loop](design_m2a.md),
> [M2b - Multi-Agent Orchestration](design_m2b.md),
> [M3 - Multi-Sandbox Delegation](design_m3.md),
> [OpenShell Runner Reuse Design](openshell_runner_reuse_design.md)
>
> **Last updated:** 2026-04-30

---

## 1  Purpose

Cursor released a public TypeScript SDK for running the same coding agent
runtime used by Cursor Desktop, CLI, Cloud Agents, and the web app.  The SDK is
interesting to NemoClaw less as a dependency and more as a validation point for
the **agent harness** shape we have been converging on: durable agents, prompt
runs, normalized event streams, local/cloud runtime selection, artifacts,
resumability, hooks, skills, subagents, and programmatic PR workflows.

This document records what NemoClaw should adopt from that harness, how those
ideas map onto the existing `AgentLoop` + OpenShell + NMB architecture, and
which parts should be deferred or intentionally not copied.

The short version:

1. **Do not replace NemoClaw's runtime.**  OpenShell remains the security and
   sandbox layer; NMB remains the inter-agent control plane.
2. **Adopt the public API shape.**  Cursor's `Agent` / `Run` split is a clean
   external interface over the capabilities NemoClaw is already building.
3. **Adopt normalized streaming and run inspection.**  Cursor's stable event
   envelope is a strong model for dashboard, Slack, CLI, audit, and training
   flywheel consumers.
4. **Adopt durable run/session management.**  `resume`, `list`, `getRun`,
   `cancel`, `archive`, and artifact APIs are exactly the surfaces an
   always-on agent needs.
5. **Keep provider-specific tool details unstable and internal.**  The stable
   boundary should be the run/event/artifact envelope, not every internal tool
   payload shape.

---

## 2  What Cursor Shipped

Cursor's SDK exposes a scriptable agent harness:

| Concept | Cursor shape | Why it matters to NemoClaw |
|---------|--------------|----------------------------|
| Agent | Durable container holding conversation state, workspace config, and settings. | Maps to an orchestrator or sub-agent session that survives multiple prompts. |
| Run | One prompt submission with status, stream, result, git metadata, cancellation, and conversation inspection. | Maps to a `task.assign` / `AgentLoop.run()` invocation and should become the main observable unit. |
| Runtime | Same API for local, Cursor-hosted cloud, and self-hosted pools. | Matches NemoClaw's desired local dev, same-sandbox M2b, and multi-sandbox M3 paths. |
| Stream events | Discriminated `SDKMessage` events: system, user, assistant, thinking, tool_call, status, task, request. | Good template for NMB `system.events`, dashboard websockets, and audit-derived traces. |
| Resume / inspect | `Agent.resume`, `Agent.list`, `Agent.get`, `Agent.listRuns`, `Agent.getRun`. | Directly relevant for an always-on Slack agent and Mission Control dashboard. |
| Artifacts | List/download files produced by an agent workspace. | Maps to OpenShell `sandbox download`, PR branches, logs, patches, screenshots, and audit bundles. |
| Config surfaces | MCP servers, `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/agents/*.md`, `.cursor/skills/`. | Validates repo-local declarative configuration for tools, hooks, subagents, and skills. |
| Subagents | Named subagents with description, prompt, model, and scoped MCP servers. | Similar to NemoClaw role definitions for coding, review, research, and note-taking agents. |
| Samples | Quickstart, prototyping app, kanban board, coding-agent CLI. | Useful product patterns for NemoClaw CLI and dashboard surfaces. |

Cursor's blog frames this as "coding agents as programmatic
infrastructure": CI/CD can start agents, custom apps can embed them, and cloud
runs can continue after the caller disconnects.  That is also NemoClaw's
direction, except our isolation, credentials, and service routing are centered
on OpenShell and NVIDIA-internal service tools rather than Cursor Cloud.

---

## 3  Fit With NemoClaw

NemoClaw already has the most important internal primitive: a reusable
`AgentLoop` that is independent of Slack, NMB, and OpenShell.  Cursor's SDK
does not argue for replacing it.  It argues for a cleaner **harness API around
it**.

Current NemoClaw mapping:

| Cursor harness idea | Existing NemoClaw piece | Gap |
|---------------------|-------------------------|-----|
| Durable `Agent` | Orchestrator process; coding-agent process; future review agent process. | No first-class session object exposed to callers. |
| `Run` | `AgentLoop.run()` and NMB `task.assign`/`task.complete`. | No persistent run handle with `stream`, `wait`, `cancel`, `conversation`. |
| Runtime choice | CLI mode, same-sandbox NMB mode, future OpenShell multi-sandbox mode. | Runtime selection is spread across entrypoints and Makefile targets. |
| Stream events | Tool callbacks, structured logs, NMB audit DB, planned `system.events`. | No single normalized event envelope for all consumers. |
| Artifact listing | Git diff helpers, OpenShell upload/download, audit DB rows. | No stable artifact manifest or download API. |
| Hooks | Approval gates, audit callbacks, future policy hot-reload. | No repo-local hook manifest equivalent. |
| Subagents | `DelegationManager`, `delegate_task`, role-specific prompts/tools. | Role definitions live in Python/config, not declarative files. |
| Config reload | Config load at startup; some generated policy/config files. | No `reload()` equivalent for hooks/skills/subagent definitions. |

The adoption target should be a **NemoClaw harness layer** above the existing
loop and bus:

```text
src/nemoclaw_escapades/harness/
  __init__.py
  agent.py        # AgentSession: create/resume/send/close/reload
  run.py          # AgentRun: stream/wait/cancel/conversation
  runtime.py      # LocalRuntime, SameSandboxRuntime, OpenShellRuntime
  events.py       # stable event envelope + adapters from AgentLoop/NMB
  artifacts.py    # artifact manifest + OpenShell download bridge
  registry.py     # list/get agents and runs backed by audit DB
```

This layer should be internal Python first.  A TypeScript or REST wrapper can
come later if the dashboard or external automation needs it.

---

## 4  Adoption Recommendations

### 4.1 Adopt `Agent` / `Run` as the public mental model

Cursor's split is right:

- `Agent` is durable and owns identity, conversation state, workspace config,
  model defaults, tools, skills, hooks, and runtime binding.
- `Run` is a single prompt/task attempt with status, stream, result,
  cancellation, duration, git/artifact metadata, and structured conversation.

NemoClaw should mirror this naming in its harness even if the implementation is
Python and backed by `AgentLoop` + NMB:

```python
agent = await AgentSession.create(
    runtime=OpenShellRuntime(policy="policies/coding-agent.yaml"),
    model=ModelSelection(id="nemotron-or-claude", params={}),
    workspace=GitWorkspace(repo_url=repo_url, starting_ref="main"),
)

run = await agent.send("Fix the flaky sync test and add a regression test")

async for event in run.stream():
    ...

result = await run.wait()
```

This gives Slack, CLI, cron, dashboard, and future CI integrations the same
interface instead of each surface learning how to spawn agents directly.

### 4.2 Adopt a stable stream envelope

Cursor's event stream has a stable envelope and deliberately unstable internal
tool payloads.  NemoClaw should do the same.

Recommended event types:

| Event type | Meaning |
|------------|---------|
| `system` | Run initialized; includes model, runtime, available tools, workspace summary. |
| `user` | User/task prompt accepted. |
| `assistant` | Assistant text output, optionally chunked. |
| `thinking` | Reasoning/plan text when the backend exposes it and policy allows display. |
| `tool_call` | Tool lifecycle: started, completed, failed, cancelled. |
| `status` | Run lifecycle: queued, provisioning, running, waiting_for_approval, finished, error, cancelled. |
| `task` | Higher-level milestones: workspace_ready, tests_started, diff_ready, pr_opened. |
| `request` | Human input or approval needed. |

Every event should include:

- `agent_id`
- `run_id`
- `ts`
- `sequence`
- `runtime`
- `event_type`
- `payload`

The envelope should be stable.  `payload` is allowed to evolve by event type.
Tool args/results should be treated as diagnostic data, not a compatibility
contract.  This is especially important for the training flywheel: stable
event ordering and correlation IDs matter more than preserving every tool's
exact internal schema forever.

### 4.3 Adopt runtime abstraction, but map it to OpenShell

Cursor uses one interface for local, hosted cloud, and self-hosted pool runs.
NemoClaw should use the same abstraction but with OpenShell-native runtimes:

| NemoClaw runtime | Use case | Backing implementation |
|------------------|----------|------------------------|
| `LocalRuntime` | Fast unit tests and developer experiments. | Current `python -m nemoclaw_escapades.agent --task ...` path. |
| `SameSandboxRuntime` | M2b co-located sub-agent process. | `DelegationManager._spawn_subprocess` + NMB. |
| `OpenShellRuntime` | M3 production path: one sandbox per task. | `openshell sandbox create/upload/download/delete` + NMB. |
| `RemoteOpenShellRuntime` | Future Brev/DGX Spark or self-hosted pool equivalent. | Same harness contract, remote OpenShell gateway. |

This preserves NemoClaw's security model while giving callers the same
ergonomic benefit Cursor exposes: choose the runtime with one field, not a
different code path.

### 4.4 Adopt durable resume and inspection APIs

Cursor's `resume`, `list`, `get`, `listRuns`, and `getRun` are a good fit for
an always-on agent.  NemoClaw already has the audit DB and NMB state needed to
back most of this.

Recommended API:

```python
agent = await AgentSession.resume(agent_id)
run = await AgentRun.get(run_id)

agents = await AgentRegistry.list(runtime="openshell", include_archived=False)
runs = await agent.list_runs(limit=50)
conversation = await run.conversation()
```

Implementation notes:

- Use the audit DB as the source of truth for historical runs.
- Use NMB broker snapshot / `system.events` for live status.
- Store enough run metadata to reconstruct: prompt, model selection, runtime,
  workspace, branch/PR, artifacts, terminal status, timestamps, cancellation
  reason, and approval requests.
- Keep `conversation()` structured.  It should be renderable by Slack,
  dashboard, CLI, and training-data exporters without reparsing logs.

### 4.5 Adopt cancellation as a first-class operation

Cursor makes cancellation part of `Run`, not an afterthought.  NemoClaw should
add this before multi-sandbox fanout grows:

```python
await run.cancel(reason="superseded by newer user request")
```

For M2b same-sandbox runs, cancellation can signal the subprocess / task
handler and mark the NMB request cancelled.  For M3 OpenShell runs, it should
first ask the agent to stop cleanly over NMB, then enforce a timeout, then
delete the sandbox if needed.

Cancellation events should flow through the same stream:

```json
{"type": "status", "status": "cancelling", "run_id": "..."}
{"type": "status", "status": "cancelled", "run_id": "..."}
```

### 4.6 Adopt artifact manifests

Cursor exposes cloud artifacts with `listArtifacts()` and
`downloadArtifact(path)`.  NemoClaw should introduce the same concept, backed
by OpenShell file download and git metadata.

Initial artifact kinds:

| Artifact kind | Examples |
|---------------|----------|
| `patch` | `git diff`, formatted patch, changed-file summary. |
| `git` | branch name, commit SHA, PR/MR URL. |
| `logs` | agent log, tool log, sandbox boot log, policy denial log. |
| `audit` | per-run NMB/audit bundle for replay and training. |
| `test` | pytest output, coverage report, junit XML. |
| `media` | screenshots or dashboard captures from future browser agents. |

M3's OpenShell artifact transport should produce a manifest before sandbox
cleanup.  The dashboard can render this without knowing where the file lived
inside the sandbox.

### 4.7 Adopt declarative hooks cautiously

Cursor's hooks are file-based policy boundaries rather than per-run callbacks.
That distinction is useful.  NemoClaw should eventually have a repo-local hook
manifest, but not arbitrary host script execution in the first version.

Candidate hooks:

| Hook | Purpose |
|------|---------|
| `pre_run` | Validate workspace, policy, credentials, and repo cleanliness. |
| `post_tool_call` | Emit observability, redact sensitive output, update progress. |
| `pre_artifact_collect` | Run tests or generate a final diff summary. |
| `post_run` | Update dashboard, Slack, training flywheel, and cleanup records. |
| `on_policy_denial` | Convert OpenShell denials into policy proposals. |

Hooks should run inside the relevant sandbox when they touch workspace data.
The orchestrator can own hook approval and policy decisions, but should avoid
reintroducing a privileged host-side runner path.

### 4.8 Adopt declarative subagent definitions

Cursor supports inline subagents and `.cursor/agents/*.md` definitions with a
name, description, prompt, and optional model.  NemoClaw's M3 role definitions
should adopt the same repo-local declarative shape, with OpenShell additions:

```yaml
name: code-reviewer
description: Reviews local diffs for correctness, tests, security, and style.
model: inherit
policy: policies/review-agent.yaml
tools:
  - read_file
  - grep
  - git_diff
  - comment_on_diff
skills:
  - code-review
nemoclaw:
  runtime: openshell
  max_concurrent: 2
  max_turns: 8
```

This should complement, not replace, `DelegationManager`.  The manager still
enforces spawn depth, concurrency limits, NMB routing, and policy.  The
definition file gives the parent agent and operators a stable role catalog.

### 4.9 Adopt model catalog and per-run model parameters

Cursor's SDK separates `model.id` from model-specific `params`, and exposes a
model listing API so callers can discover valid parameters.  NemoClaw already
has per-task `model` in M2b; the missing piece is structured model params.

Recommended shape:

```python
@dataclass(frozen=True)
class ModelSelection:
    id: str
    params: dict[str, str | int | float | bool] = field(default_factory=dict)
```

Store the resolved model selection on every run.  That improves reproducibility
and makes training-flywheel traces more useful.

### 4.10 Use the Cookbook examples as product references

The public cookbook examples are useful as UI/CLI product references:

| Example | NemoClaw use |
|---------|--------------|
| Quickstart | Shape of a minimal `nemoclaw agent prompt ...` CLI. |
| Coding agent CLI | Shape of a scriptable one-shot coding task runner. |
| Kanban board | Direct input for Mission Control task cards and agent grouping. |
| Prototyping tool | Reference for a dashboard flow that creates sandboxes and streams results. |

We should not copy the TypeScript implementation wholesale.  The reusable
piece is the workflow surface: create agent, send prompt, stream events,
inspect result, collect artifacts.

---

## 5  What Not To Adopt Directly

### 5.1 Do not make Cursor Cloud the core runtime

Cursor Cloud is useful for public GitHub repos and teams already bought into
that environment.  NemoClaw's core runtime has different constraints:

- NVIDIA/internal repos and credentials.
- OpenShell policy enforcement and provider routing.
- NMB-based multi-agent collaboration.
- Audit DB and training-flywheel requirements.
- Slack approval gates and enterprise service tools.

Cursor's self-hosted pool concept is directionally similar to a remote
OpenShell gateway.  Adopt the abstraction, not the hosted dependency.

### 5.2 Do not replace NMB with SDK streams

Cursor's stream is client-facing.  NMB is a brokered inter-agent control plane.
They overlap in event shape, not responsibility.

Recommended layering:

```text
AgentLoop/tool callbacks
        |
        v
NMB task protocol + system.events + audit DB
        |
        v
Harness AgentRun.stream()
        |
        +-- Slack renderer
        +-- CLI renderer
        +-- Dashboard websocket
        +-- training/export pipeline
```

The harness stream should be a projection from NMB/audit state, not a second
message bus that agents depend on for coordination.

### 5.3 Do not freeze every tool schema

Cursor explicitly warns that tool-call args/results are internal-facing and
not stable.  NemoClaw should follow that discipline.  Stabilize:

- run identity
- event ordering
- status values
- artifact manifest
- approval request envelope
- task completion envelope

Do not promise long-term stability for the exact JSON emitted by `bash`,
`grep`, `git_diff`, Jira, Slack, Gerrit, or future MCP/nv-tools wrappers.

### 5.4 Do not collapse skills, MCP, and service tools into one mechanism

Cursor can expose many external tools through MCP.  NemoClaw has a stronger
security boundary:

- Skills teach behavior and may contribute policy metadata.
- Service tools wrap approved internal APIs and write gates.
- OpenShell providers handle credential injection and network policy.
- NMB handles agent-to-agent coordination.

MCP can be one integration option later, especially for non-NVIDIA services,
but it should not replace the existing service-tool registry or approval
model.

### 5.5 Do not add arbitrary hooks before policy design

Hooks are powerful and risky.  A `.cursor/hooks.json`-style mechanism that can
run scripts during agent execution is effectively a policy extension language.
NemoClaw should land a constrained, audited hook surface first, then expand.

---

## 6  Proposed NemoClaw Harness API

This is the smallest API that captures the Cursor harness ergonomics while
remaining native to NemoClaw.

```python
class AgentSession:
    agent_id: str
    runtime: RuntimeSpec
    model: ModelSelection | None

    @classmethod
    async def create(cls, options: AgentOptions) -> "AgentSession": ...

    @classmethod
    async def resume(cls, agent_id: str, options: ResumeOptions | None = None) -> "AgentSession": ...

    async def send(self, message: str | UserMessage, options: SendOptions | None = None) -> "AgentRun": ...
    async def reload(self) -> None: ...
    async def close(self) -> None: ...
    async def list_artifacts(self) -> list[Artifact]: ...
    async def download_artifact(self, path: str) -> bytes: ...


class AgentRun:
    run_id: str
    agent_id: str
    status: RunStatus
    result: str | None
    model: ModelSelection | None
    duration_ms: int | None
    git: GitInfo | None

    async def stream(self) -> AsyncIterator[HarnessEvent]: ...
    async def wait(self) -> RunResult: ...
    async def cancel(self, reason: str | None = None) -> None: ...
    async def conversation(self) -> list[ConversationTurn]: ...
```

The first implementation can be thin:

- `AgentSession.create(LocalRuntime)` wraps current CLI-mode stack assembly.
- `AgentSession.create(SameSandboxRuntime)` wraps M2b `DelegationManager`.
- `AgentRun.stream()` tails live in-memory events for local runs and NMB
  `system.events` for delegated runs.
- `AgentRun.wait()` resolves from the local task or NMB `task.complete`.
- `AgentRun.conversation()` reads the working messages captured by
  `AgentLoopResult` and/or audit DB rows.

The API can be private/internal until at least one real consumer exists
outside tests.  The likely first consumers are:

1. `nemoclaw agent prompt ...` CLI
2. Slack task delegation path
3. Mission Control dashboard backend
4. Cron / CI-style scheduled jobs

---

## 7  Implementation Plan

### Phase H0 - Event Envelope

Define `HarnessEvent` and adapters from the existing `AgentLoop` callbacks.
Emit events for local CLI-mode runs first, without changing NMB.

Exit criteria:

- Unit tests cover event ordering for assistant text, tool start, tool finish,
  failure, and terminal run status.
- Tool payloads remain opaque under `payload`.

### Phase H1 - Run Records

Add a `runs` table or extend the existing audit schema so every
`AgentLoop.run()` has a durable row with status, timestamps, model, runtime,
task prompt, and result summary.

Exit criteria:

- `AgentRun.wait()` can be reconstructed from persisted state.
- `AgentRun.conversation()` can return structured turns for completed runs.

### Phase H2 - Local `AgentSession`

Wrap the current coding-agent CLI stack in `AgentSession.create(local=...)`.
This should not change behavior; it only introduces the harness API.

Exit criteria:

- A one-shot prompt path can be written in fewer than ~15 lines of Python.
- Existing coding-agent CLI tests still pass.

### Phase H3 - NMB-backed Runs

Project M2b delegated tasks into the same `AgentRun` abstraction.  Bridge
`task.assign`, `task.progress`, `task.complete`, and `task.error` into
`HarnessEvent` and run records.

Exit criteria:

- The orchestrator can call `agent.send()` instead of directly handling all
  NMB details in the Slack path.
- Dashboard can subscribe to a run stream without knowing whether it is local
  or delegated.

### Phase H4 - Artifacts

Add artifact manifests for diffs, logs, audit bundles, and git metadata.
For local/same-sandbox runs this can start as filesystem paths.  M3 should
swap the backend to OpenShell `sandbox download`.

Exit criteria:

- Every completed coding run has a machine-readable artifact manifest.
- Artifact metadata is visible in the audit DB and dashboard-ready.

### Phase H5 - Resume, List, Cancel

Add registry operations after the event/run model is stable:

- `AgentSession.resume`
- `AgentRegistry.list`
- `AgentRun.get`
- `AgentRun.cancel`
- archive/delete semantics if persistent sessions become numerous

Exit criteria:

- A process restart can reattach to a live or completed run.
- Cancellation is observable in NMB, audit DB, and any active stream.

### Phase H6 - Declarative Agents and Hooks

Introduce repo-local `agents/*.md` and a constrained hook manifest only after
M3 policy generation is in place.

Exit criteria:

- Coding/review/research role definitions move out of scattered Python/config
  constants.
- Hooks run inside a sandbox, are audited, and are policy-governed.

---

## 8  Priority Matrix

| Priority | Adopt | Why now |
|----------|-------|---------|
| P0 | Stable run/event envelope | Unblocks dashboard, Slack rendering, audit, and training data with one schema. |
| P0 | `Agent` / `Run` API shape | Prevents each caller from reinventing spawn/stream/wait/cancel. |
| P1 | Runtime abstraction | M2b to M3 transition becomes a runtime swap instead of a caller rewrite. |
| P1 | Artifact manifest | Needed for coding tasks, PR summaries, dashboard, and postmortems. |
| P1 | Resume/list/get run registry | Important for always-on operation and Mission Control. |
| P2 | Declarative subagents | Useful for M3 role expansion; not required for M2b. |
| P2 | Model catalog + params | Valuable for reproducibility; can land after per-task model plumbing stabilizes. |
| P3 | Hooks | Powerful, but should wait for policy and audit guardrails. |
| P3 | MCP parity | Optional integration layer, not a core architectural requirement. |

---

## 9  Open Questions

1. Should the harness API live entirely inside Python, or should NemoClaw also
   expose a small REST API for dashboard/automation callers?
2. Should run records be stored in the existing audit DB or in a separate
   `runs.db` with foreign keys into audit rows?
3. How much of `AgentLoopResult.working_messages` should be persisted for
   replay/training, given tool outputs may contain secrets?
4. What is the exact cancellation contract for a tool call already executing
   inside an OpenShell sandbox?
5. Should declarative subagent definitions live under `agents/`,
   `.nemoclaw/agents/`, or `.cursor/agents/` for compatibility with Cursor?
6. Should NemoClaw provide a compatibility adapter that can call Cursor's SDK
   for public GitHub repos, or keep Cursor SDK usage as inspiration only?
7. What artifact retention policy is appropriate for logs and audit bundles
   that may include internal repo contents?

---

## 10  Recommendation

Treat Cursor's SDK as the clearest public reference for **agent harness
ergonomics**:

- copy the `Agent` / `Run` mental model;
- copy normalized event streaming and run inspection;
- copy artifact manifests and resumability;
- copy declarative subagent/hook ideas once NemoClaw's policy layer is ready;
- do **not** copy the hosted runtime dependency or collapse OpenShell/NMB into
  a Cursor-specific integration.

The immediate next step is Phase H0/H1: define a stable harness event envelope
and persist run records.  Those two changes pay off across Slack, CLI,
dashboard, multi-agent orchestration, and the training flywheel without forcing
any runtime migration.
