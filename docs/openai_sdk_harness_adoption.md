# OpenAI Agents SDK Harness - Adoption Notes

> **Sources:** [OpenAI Agents SDK overview](https://developers.openai.com/api/docs/guides/agents),
> [Agents SDK quickstart](https://developers.openai.com/api/docs/guides/agents/quickstart),
> [NMB Design](nmb_design.md)
>
> **Related:** [Cursor SDK Agent Harness - Adoption Notes](cursor_sdk_harness_adoption.md),
> [Claude Agent SDK Harness - Adoption Notes](claude_sdk_harness_adoption.md),
> [M2a - Reusable Agent Loop](design_m2a.md),
> [M2b - Multi-Agent Orchestration](design_m2b.md),
> [M3 - Multi-Sandbox Delegation](design_m3.md),
> [Sandbox Spawn Design](sandbox_spawn_design.md)
>
> **Last updated:** 2026-05-06

---

## 1  Purpose

The OpenAI Agents SDK is a code-first agent framework for applications that own
orchestration, tool execution, approvals, state, and runtime behavior.  The
Python quickstart shape is deliberately small: define an `Agent`, call
`Runner.run(...)`, add `function_tool`s, and introduce specialists or handoffs
as the workflow grows.

This document records how NemoClaw should use the OpenAI Agents SDK as an
optional **agent runtime adapter** while preserving NemoClaw's existing
architecture:

1. **Do not replace OpenShell.**  OpenShell remains the sandbox, policy, and
   credential-routing boundary.
2. **Do not replace NMB.**  NMB remains the inter-sandbox message bus for task
   assignment, progress, peer review, audit flushes, and final results.
3. **Use OpenAI Agents SDK inside a sandbox process.**  The SDK should execute
   one assigned coding or review task, not own the global workflow.
4. **Expose NMB through narrow function tools.**  Avoid giving the model a raw
   arbitrary message-sending API.
5. **Adopt the useful SDK harness ideas.**  OpenAI's `Agent`, `Runner`,
   function tools, handoffs, run results, continuation state, guardrails, and
   tracing map cleanly to NemoClaw's `AgentRun` model and event stream.

The most important separation:

```text
NemoClaw orchestrator
  owns workflow state, sandbox lifecycle, NMB routing, audit DB, finalization

OpenShell sandbox
  owns process isolation, filesystem scope, network policy, credential routing

OpenAI Agents SDK process
  owns the local agent loop for one coding/review task

NMB client
  owns inter-sandbox messages and peer coordination
```

---

## 2  What OpenAI Shipped

OpenAI's Agents SDK provides the core pieces of a code-first agent application:

| Concept | OpenAI SDK shape | Why it matters to NemoClaw |
|---------|------------------|----------------------------|
| Agent definition | `Agent(name=..., instructions=..., model=...)` | Maps to NemoClaw role definitions for coding, review, research, and finalization workers. |
| Run execution | `await Runner.run(agent, prompt)` | Maps to one NMB `task.assign` handled inside a sandbox process. |
| Final output | `result.final_output` | Maps to `TaskCompletePayload.summary` and finalization input. |
| Function tools | `@function_tool` | Best fit for exposing NMB actions as constrained model-callable tools. |
| Local repo tools | User-supplied function tools or sandbox/hosted tools | Unlike Claude Code, the basic SDK example does not automatically expose local `Read` / `Edit` / `Bash` tools. |
| Specialists | multiple `Agent` instances and handoffs | Useful inside one runtime, but not a substitute for OpenShell-isolated peer agents. |
| State/continuation | run history, sessions, continuation IDs, interruption/approval resume paths | Relevant to future `AgentSession.resume` and `task.redirect`. |
| Guardrails/human review | SDK-level validation and approval surfaces | Useful defense in depth around NemoClaw's Slack/OpenShell approvals. |
| Tracing/evals | SDK traces and evaluation workflow | Useful as an observability input, but not the source of truth for NemoClaw audit. |
| Hosted tools/MCP | tools integration path | Optional future integration layer; NMB function tools should come first. |
| Sandbox agents | container-based environment support | Directionally similar to NemoClaw's OpenShell runtime, but not a replacement for OpenShell policy. |

The SDK overview explicitly positions this path for applications that own
orchestration, tool execution, approvals, and state.  That matches NemoClaw's
shape well, provided the OpenAI SDK is kept inside the sandbox runtime layer and
does not replace NMB or OpenShell.

The main implementation difference from Claude Agent SDK is tool ownership:
OpenAI's SDK is code-first and expects the application to supply the function
tools or hosted tools the agent can use.  NemoClaw therefore needs a repo-tool
surface (`read_file`, `write_file`, `run_command`, git helpers, etc.) in
addition to NMB coordination tools.

---

## 3  Fit With NemoClaw

NemoClaw already has a reusable agent loop, typed NMB protocol, delegation
manager, audit DB, finalization flow, and OpenShell sandbox design.  The OpenAI
Agents SDK should therefore be evaluated as a **runtime implementation** rather
than a replacement architecture.

Current mapping:

| OpenAI SDK idea | Existing NemoClaw piece | Adoption stance |
|-----------------|-------------------------|-----------------|
| `Agent` | Future role/session definitions | Good public mental model for a coding or review worker. |
| `Runner.run` | `_run_assigned_task` after NMB `task.assign` | Good fit for single-task workers. |
| `function_tool` | NemoClaw tools and NMB client operations | Best fit for NMB peer coordination tools. |
| `final_output` | `TaskCompletePayload.summary` | Use as one source for terminal summaries. |
| Handoffs | `DelegationManager`, `delegate_task`, review loop | Useful locally, but do not conflate with NMB peer agents. |
| Guardrails / approvals | Approval gate, Slack buttons, OpenShell policy | Use as defense in depth, not primary policy. |
| Tracing | Audit DB and future harness events | Useful secondary observability, not the authoritative audit log. |
| Sessions / continuation | Future `AgentSession.resume` | Useful after run/session records exist. |

Recommended layering:

```text
Slack / CLI / Dashboard
        |
        v
NemoClaw harness AgentSession / AgentRun
        |
        v
DelegationManager + OpenShellRuntime
        |
        v
NMB task.assign / task.progress / task.complete / audit.flush
        |
        v
Sandbox process running OpenAI Agents SDK
        |
        v
OpenAI model/tools + NemoClaw NMB function tools
```

In this model, a coding sandbox and review sandbox are not just SDK handoff
specialists.  They are separate OS processes, usually separate OpenShell
sandboxes, each running an OpenAI Agents SDK agent and each connected to NMB.

---

## 4  Adoption Recommendations

### 4.1 Use OpenAI Agents SDK as a runtime adapter

Add an `OpenAiAgentsRunner` behind the NemoClaw harness:

```python
class OpenAiAgentsRunner:
    async def run_task(self, assignment: TaskAssignPayload) -> TaskCompletePayload:
        ...
```

The runner should:

- translate `TaskAssignPayload.prompt` into a `Runner.run(...)` input;
- construct an `Agent` from task role, model, tool surface, and workspace root;
- attach sandbox-local repo tools or hosted sandbox tools for file/command work;
- expose only approved NemoClaw coordination tools through `@function_tool`;
- collect `result.final_output`;
- compute the final diff through NemoClaw git helpers;
- return typed `TaskCompletePayload` or `TaskErrorPayload`.

This keeps the rest of the orchestrator stable.  The orchestrator should not
care whether a task was executed by NemoClaw's native `AgentLoop`, Claude Agent
SDK, OpenAI Agents SDK, Cursor SDK, or another future runner.

### 4.2 Use function tools for NMB peer coordination

Peer coordination should be tool-mediated, not raw bus access.  For example,
the coding agent may get:

| Tool | Backing NMB operation |
|------|-----------------------|
| `send_progress` | `bus.send(orchestrator_id, "task.progress", ...)` |
| `request_review` | `bus.request(reviewer_id, "review.request", ...)` |
| `ask_orchestrator` | `bus.request(orchestrator_id, "task.clarify", ...)` |

Avoid exposing a generic `send_message(to, type, payload)` tool at first.  The
model should not be able to invent message types, target arbitrary sandboxes, or
bypass the orchestrator's role policy.  Purpose-built tools are easier to audit
and map directly onto NMB message-type restrictions.

### 4.3 Keep SDK handoffs separate from NemoClaw peer agents

OpenAI's SDK can route between specialist agents with handoffs.  That is useful
inside a single runtime.  It does not provide the isolation NemoClaw needs for
production peer agents.

Use this distinction:

| Kind | Isolation | Transport | Use case |
|------|-----------|-----------|----------|
| OpenAI SDK handoff specialist | Same process / runtime boundary | SDK-internal | Small focused specialization under the same trust boundary. |
| NemoClaw peer agent | Separate process / sandbox | NMB | Coding, review, data, and finalization agents with distinct policies. |

The reviewer in the code example below is a NemoClaw peer agent, not a local
handoff specialist.

### 4.4 Use SDK guardrails as defense in depth

OpenShell remains the real security boundary.  SDK guardrails, structured
outputs, and tool validation should still be used because they reduce accidental
behavior and make local failures clearer.

Good first guardrails:

- type the `review.feedback` payload;
- require final summaries to include tests run and caveats;
- deny or sanitize unexpected tool inputs;
- validate that a review verdict is one of `approve` or `request_changes`;
- convert validation failure into `task.error` instead of hanging the NMB
  request.

### 4.5 Treat SDK traces as observability, not authority

OpenAI traces are useful for debugging model calls, tool calls, handoffs, and
guardrails.  NemoClaw should still treat its audit DB and NMB message log as the
authoritative workflow record because they include sandbox identity, peer
routing, Slack approvals, finalization actions, and OpenShell policy outcomes.

Recommended projection:

```text
OpenAI SDK trace
        |
        v
NemoClaw harness event adapter
        |
        v
audit DB + NMB system.events + dashboard/Slack stream
```

### 4.6 Start with one-shot `Runner.run`

For M2b and early M3, `Runner.run` is enough: each sub-agent process handles
one assignment and exits.  This matches the current `agent.__main__` shape:

```text
connect NMB -> wait for task.assign -> run task -> reply -> close
```

Use sessions, continuation IDs, or interruption/resume paths later when
NemoClaw needs:

- multiple NMB assignments in the same model/session context;
- `task.redirect` / interruption support;
- explicit approval pause/resume;
- durable sessions in a long-running worker sandbox.

---

## 5  What Not To Adopt Directly

### 5.1 Do not replace NMB with SDK handoffs

SDK handoffs route control inside an SDK workflow.  They do not route messages
between isolated OpenShell sandboxes, enforce sandbox identity, persist NMB
audit logs, or provide the brokered request/reply semantics needed by NemoClaw.

NMB remains responsible for:

- peer discovery and routing;
- `task.assign`, `task.progress`, `task.complete`, `task.error`;
- `review.request`, `review.feedback`;
- `audit.flush`;
- future `task.redirect` and `policy.*` messages.

### 5.2 Do not rely on SDK sandboxing as the primary boundary

OpenAI's sandbox agent guidance is relevant, but NemoClaw's core runtime has
specific internal requirements:

- OpenShell network policies;
- provider/credential routing;
- NVIDIA/internal service tools;
- sandbox-to-sandbox NMB routing through the current forward/tunnel workaround;
- audit DB attribution by workflow, parent sandbox, agent ID, and role.

Adopt the runtime abstraction ideas, not a replacement for OpenShell.

### 5.3 Do not expose arbitrary NMB send as a model tool

Raw NMB is too broad for the model-facing surface.  A compromised or confused
agent could target the wrong sandbox, forge application-level intent, spam
channels, or send message types outside its role.

Expose intent-level tools instead:

- `request_review`
- `send_progress`
- `ask_orchestrator`
- `submit_artifact_manifest`

Each tool should hard-code allowed targets and message types from the task
assignment and role definition.

### 5.4 Do not make Agent Builder the default production path

Agent Builder is useful when a team specifically wants the hosted workflow
editor and ChatKit path.  NemoClaw's immediate need is a code-first runner
inside OpenShell-managed infrastructure.  The SDK path is the relevant one.

### 5.5 Do not treat OpenAI hosted traces as the only audit trail

Hosted traces may be incomplete for NemoClaw's needs and may not include
OpenShell policy denials, Slack approvals, NMB routing details, or local
artifact collection.  Keep the NemoClaw audit DB as the source of truth.

---

## 6  Conversation Notes Captured In This Design

This section records the design discussion that led to the proposed shape.

### 6.1 Initial question: how would an Agents SDK runtime connect to NMB?

Question:

```text
how would claude code via agent sdk connect to the nmb bus in @docs/nmb_design.md?
https://developers.openai.com/api/docs/guides/agents
```

OpenAI-specific answer captured:

An SDK agent should not connect to peers directly.  A sandbox process creates a
`MessageBus` with a stable sandbox identity, connects to the broker at
`ws://messages.local:9876` (or the current forwarded/tunneled endpoint), listens
for `task.assign`, runs the SDK agent, and replies with `task.complete` or
`task.error`.

The concrete shape:

```text
Orchestrator sandbox
  NemoClaw orchestrator + MessageBus("orchestrator")

NMB broker
  routes task.assign / task.complete / task.error / audit.flush

Coding sandbox
  OpenAI Agents SDK process + MessageBus("coding-<id>")
```

For the current repo, this maps to `src/nemoclaw_escapades/agent/__main__.py`:
connect to NMB, wait for one assignment, run the task, flush audit, reply, and
exit.  The SDK runner is an implementation choice inside "run the task".

### 6.2 Follow-up: how would peer coordination work?

Question:

```text
how concretely would peer coordination work? write a full code example with agent sdk and the current nmb client implementation
```

OpenAI-specific answer captured:

Use separate SDK processes as peers.  The coding process exposes a function
tool like `request_review`; that tool uses the current NMB client
`bus.request(reviewer_id, "review.request", ...)`.  The review process listens
for `review.request`, runs its own OpenAI SDK review agent, then replies with
`bus.reply(original_msg, "review.feedback", ...)`.

The model sees a narrow semantic tool.  The process wrapper owns the bus,
targets, message types, timeouts, and payload schema.

---

## 7  Full Code Example: OpenAI Agents SDK Peers Over Current NMB Client

This example uses the current async NMB client implementation:

- `MessageBus.connect_with_retry()`
- `MessageBus.listen()`
- `MessageBus.request()`
- `MessageBus.reply()`
- `MessageBus.send()`

It starts three processes:

```bash
ROLE=reviewer SANDBOX_ID=reviewer-1 python nmb_openai_peer_demo.py
ROLE=coding SANDBOX_ID=coding-1 REVIEWER_ID=reviewer-1 python nmb_openai_peer_demo.py
ROLE=orchestrator CODING_ID=coding-1 python nmb_openai_peer_demo.py
```

In OpenShell, use `NMB_URL=ws://messages.local:9876` once native routing exists.
In the current prototype, set `NMB_URL` to the forwarded or tunneled broker
endpoint, for example `ws://host.docker.internal:9876`.

```python
# nmb_openai_peer_demo.py
#
# Run three processes:
#   ROLE=reviewer SANDBOX_ID=reviewer-1 python nmb_openai_peer_demo.py
#   ROLE=coding SANDBOX_ID=coding-1 REVIEWER_ID=reviewer-1 python nmb_openai_peer_demo.py
#   ROLE=orchestrator CODING_ID=coding-1 python nmb_openai_peer_demo.py
#
# In OpenShell use NMB_URL=ws://messages.local:9876.
# In the current forwarded prototype, use the forwarded URL instead.

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agents import Agent, Runner, function_tool

from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.nmb.models import NMBMessage


NMB_URL = os.getenv("NMB_URL", "ws://messages.local:9876")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", os.getcwd()))


def workspace_path(relative_path: str) -> Path:
    root = WORKSPACE_ROOT.resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {relative_path}")
    return target


async def run_git_diff(cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"git diff failed:\n{stderr.decode()}"
    return stdout.decode()


async def run_openai_agent(agent: Agent, prompt: str) -> str:
    result = await Runner.run(agent, prompt)
    return str(result.final_output or "")


async def connect_bus(sandbox_id: str) -> MessageBus:
    bus = MessageBus(
        sandbox_id=sandbox_id,
        broker_url=NMB_URL,
        append_random_suffix=False,
    )
    await bus.connect_with_retry()
    return bus


async def reviewer_loop() -> None:
    sandbox_id = os.getenv("SANDBOX_ID", "reviewer-1")
    bus = await connect_bus(sandbox_id)

    reviewer = Agent(
        name="NemoClaw code reviewer",
        instructions=(
            "You are a strict code reviewer. Review diffs for correctness, "
            "regressions, security issues, and missing tests. Return exactly: "
            "VERDICT: approve | request_changes, SUMMARY: one paragraph, "
            "COMMENTS: bullet list."
        ),
        model=OPENAI_MODEL,
    )

    async for msg in bus.listen():
        if msg.type != "review.request":
            continue

        payload = msg.payload or {}
        diff = payload.get("diff", "")
        original_task = payload.get("original_task", "")

        review_prompt = f"""
Original task:
{original_task}

Diff:
{diff}
"""

        try:
            review_text = await run_openai_agent(reviewer, review_prompt)
            normalized_review = review_text.lower()
            verdict = (
                "request_changes"
                if "verdict: request_changes" in normalized_review
                else "approve"
            )
            await bus.reply(
                msg,
                "review.feedback",
                {
                    "workflow_id": payload.get("workflow_id"),
                    "verdict": verdict,
                    "review": review_text,
                },
            )
        except Exception as exc:
            await bus.reply(
                msg,
                "review.feedback",
                {
                    "workflow_id": payload.get("workflow_id"),
                    "verdict": "request_changes",
                    "review": f"Reviewer failed: {type(exc).__name__}: {exc}",
                },
            )


async def coding_loop() -> None:
    sandbox_id = os.getenv("SANDBOX_ID", "coding-1")
    reviewer_id = os.environ["REVIEWER_ID"]
    bus = await connect_bus(sandbox_id)

    async for msg in bus.listen():
        if msg.type != "task.assign":
            continue

        payload = msg.payload or {}
        workflow_id = payload["workflow_id"]
        original_task = payload["prompt"]
        orchestrator_id = payload["orchestrator_id"]

        @function_tool
        async def read_file(path: str) -> str:
            """Read a UTF-8 text file under the workspace root."""
            target = workspace_path(path)
            return target.read_text(encoding="utf-8")

        @function_tool
        async def write_file(path: str, content: str) -> str:
            """Write a UTF-8 text file under the workspace root."""
            target = workspace_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {path}"

        @function_tool
        async def run_command(args: list[str]) -> str:
            """Run a command under the workspace root without invoking a shell."""
            if not args:
                raise ValueError("args must not be empty")
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(WORKSPACE_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return (
                f"exit_code={proc.returncode}\n"
                f"stdout:\n{stdout.decode()}\n"
                f"stderr:\n{stderr.decode()}"
            )

        @function_tool
        async def send_progress(status: str) -> str:
            """Send a short progress update to the orchestrator over NMB."""
            await bus.send(
                orchestrator_id,
                "task.progress",
                {
                    "workflow_id": workflow_id,
                    "status": status,
                },
            )
            return "Progress sent."

        @function_tool
        async def request_review(summary: str) -> str:
            """Ask the peer review agent to review the current git diff."""
            diff = await run_git_diff(WORKSPACE_ROOT)
            response = await bus.request(
                reviewer_id,
                "review.request",
                {
                    "workflow_id": workflow_id,
                    "original_task": original_task,
                    "summary": summary,
                    "diff": diff,
                    "workspace_root": str(WORKSPACE_ROOT),
                },
                timeout=300,
            )
            return json.dumps(response.payload or {}, indent=2)

        coding_agent = Agent(
            name="NemoClaw coding agent",
            instructions=(
                "Implement the assigned task in the current repository. "
                "Use read_file, write_file, and run_command for local work. "
                "Use send_progress for brief milestones. Before finishing, "
                "call request_review with a summary of your changes. If the "
                "reviewer requests changes, address them and request review "
                "again. Finish with a concise summary including tests run."
            ),
            model=OPENAI_MODEL,
            tools=[
                read_file,
                write_file,
                run_command,
                send_progress,
                request_review,
            ],
        )

        coding_prompt = f"""
Task:
{original_task}

Workspace root:
{WORKSPACE_ROOT}
"""

        try:
            result = await run_openai_agent(coding_agent, coding_prompt)
            final_diff = await run_git_diff(WORKSPACE_ROOT)
            await bus.reply(
                msg,
                "task.complete",
                {
                    "workflow_id": workflow_id,
                    "summary": result,
                    "diff": final_diff,
                    "files_changed": [],
                },
            )
        except Exception as exc:
            await bus.reply(
                msg,
                "task.error",
                {
                    "workflow_id": workflow_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "recoverable": True,
                },
            )


async def orchestrator_once() -> None:
    bus = await connect_bus(os.getenv("SANDBOX_ID", "orchestrator"))
    coding_id = os.getenv("CODING_ID", "coding-1")

    response: NMBMessage = await bus.request(
        coding_id,
        "task.assign",
        {
            "workflow_id": "wf-demo-001",
            "orchestrator_id": bus.sandbox_id,
            "prompt": "Add input validation to the parser and update tests.",
        },
        timeout=1800,
    )

    print(f"Received {response.type} from {response.from_sandbox}")
    print(json.dumps(response.payload or {}, indent=2))


async def main() -> None:
    role = os.environ["ROLE"]
    if role == "reviewer":
        await reviewer_loop()
    elif role == "coding":
        await coding_loop()
    elif role == "orchestrator":
        await orchestrator_once()
    else:
        raise ValueError(f"unknown ROLE={role}")


if __name__ == "__main__":
    asyncio.run(main())
```

### 7.1 Message flow

```text
orchestrator -> NMB request(task.assign) -> coding
coding       -> OpenAI Runner.run(...)
coding       -> function tool send_progress
coding       -> NMB send(task.progress) -> orchestrator
coding       -> function tool request_review
coding       -> NMB request(review.request) -> reviewer
reviewer     -> OpenAI Runner.run(...)
reviewer     -> NMB reply(review.feedback) -> coding
coding       -> OpenAI agent addresses feedback if needed
coding       -> NMB reply(task.complete) -> orchestrator
```

### 7.2 Why this is the right boundary

The code example deliberately keeps NMB outside the model's raw control:

- The wrapper owns the bus connection and sandbox identity.
- The assignment owns `workflow_id`, `orchestrator_id`, and `reviewer_id`.
- The model can only call semantic function tools.
- Each semantic tool maps to exactly one allowed NMB operation.
- Local file and command tools are rooted in `WORKSPACE_ROOT`; OpenShell still
  supplies the hard sandbox boundary.
- The reviewer process owns the `review.feedback` reply path.
- OpenShell still enforces the real filesystem and network policy.

---

## 8  Proposed NemoClaw Harness API Additions

The Cursor and Claude adoption docs propose an internal `AgentSession` /
`AgentRun` harness.  OpenAI SDK support should slot into that API as a runtime
backend:

```python
agent = await AgentSession.create(
    runtime=OpenShellRuntime(
        runner="openai-agents-sdk",
        policy="policies/coding-agent.yaml",
    ),
    workspace=GitWorkspace(repo_url=repo_url, starting_ref="main"),
)

run = await agent.send(
    "Fix the parser validation bug and ask the review peer before finishing",
    options=SendOptions(
        peer_tools=[
            PeerTool.request_review(role="code-reviewer"),
            PeerTool.send_progress(),
        ],
    ),
)

async for event in run.stream():
    ...

result = await run.wait()
```

Internal runner shape:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OpenAiAgentsRunnerOptions:
    model: str = "gpt-5.5"
    function_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    use_session: bool = False
    enable_tracing: bool = True


class OpenAiAgentsRunner:
    async def run(
        self,
        assignment: TaskAssignPayload,
        bus: MessageBus,
        options: OpenAiAgentsRunnerOptions,
    ) -> TaskCompletePayload:
        ...
```

The first version can support only one-shot `Runner.run`.  Later versions can
use SDK sessions, continuation IDs, or approval/interruption resume paths for
durable sessions and `task.redirect`.

---

## 9  Implementation Plan

### Phase O0 - Prototype wrapper

Create a standalone example or test fixture similar to the code in section 7.
Use a local NMB broker and three local processes.

Exit criteria:

- Orchestrator sends `task.assign` via `bus.request`.
- Coding SDK process receives it and calls `Runner.run`.
- Coding process calls reviewer through a `request_review` function tool.
- Reviewer SDK process replies with `review.feedback`.
- Coding process replies with `task.complete`.

### Phase O1 - Runner abstraction

Introduce an `OpenAiAgentsRunner` that can be called from the existing NMB
sub-agent path.

Exit criteria:

- Runner converts `TaskAssignPayload` to `Agent` and `Runner.run` inputs.
- Runner returns typed `TaskCompletePayload` / `TaskErrorPayload`.
- Current native `AgentLoop` path remains available.

### Phase O2 - NMB function tool factory

Create a small factory that builds role-specific NMB function tools:

```python
tools = create_nmb_function_tools(
    bus=bus,
    assignment=task,
    peers=peer_manifest,
    allowed_actions=["send_progress", "request_review"],
)
```

Exit criteria:

- Tools hard-code allowed targets and message types.
- Tool calls emit audit rows.
- Tests cover offline peer, timeout, malformed reviewer payload, and policy
  denial.

### Phase O3 - Guardrail and audit integration

Use SDK guardrails and result validation to feed NemoClaw's audit sink and
event stream.

Exit criteria:

- Tool starts and finishes are visible as harness events.
- Function-tool audit rows include workflow ID, parent sandbox ID, agent ID,
  and role.
- Guardrail failures do not prevent `task.error` replies.

### Phase O4 - Durable sessions and interruptions

Evaluate SDK sessions, server-managed continuation state, approval resume
surfaces, and interruption handling for long-lived workers.

Exit criteria:

- `task.redirect` has a documented mapping.
- Session/continuation IDs are persisted on the run record.
- Reconnect/resume behavior is documented and tested.

### Phase O5 - OpenShell integration

Run the same peer-coordination flow across separate OpenShell sandboxes.

Exit criteria:

- Coding and review sandboxes connect to NMB through the current forward/tunnel
  workaround.
- Policies only allow the configured NMB endpoint and required inference/tools.
- Cleanup always tears down sandboxes and forwarded endpoints.

---

## 10  Priority Matrix

| Priority | Adopt | Why now |
|----------|-------|---------|
| P0 | OpenAI SDK as runtime adapter | Lets NemoClaw evaluate OpenAI's code-first agent loop without replacing orchestration. |
| P0 | NMB function-tool surface | Enables peer coordination while preserving message policy and auditability. |
| P0 | One-shot `Runner.run` worker | Matches current single-assignment sub-agent lifecycle. |
| P1 | Runner abstraction | Allows native, Claude SDK, OpenAI SDK, and future SDK runners behind one harness API. |
| P1 | Guardrail/result validation | Improves typed failure handling before finalization. |
| P1 | Trace adapter | Useful observability if projected into NemoClaw events and audit. |
| P2 | Sessions/continuations | Needed for redirect/resume, but not for first peer-review prototype. |
| P2 | Local SDK handoffs | Useful local specialization, but not required for OpenShell-isolated peers. |
| P3 | Agent Builder path | Not aligned with current internal sandbox and credential requirements. |

---

## 11  Open Questions

1. Should OpenAI SDK support be a first-class runtime in `AgentSession`, or an
   experimental runner behind a feature flag?
2. How should `TaskAssignPayload.tool_surface` map to OpenAI function tools,
   hosted tools, MCP tools, and OpenShell policy?
3. Should NMB function tools live in a generic factory or in role-specific
   runner modules?
4. How much of OpenAI SDK run history and traces should be persisted, given tool
   outputs may contain sensitive repo or credential-derived data?
5. Should `review.feedback` be free-form text, a typed Pydantic payload, or both?
6. How should OpenAI session/continuation IDs map to NemoClaw `agent_id`,
   `workflow_id`, and future `run_id`?
7. What is the exact mapping from SDK approval/interruption state to NMB
   `task.redirect`, `task.error`, or future `approval.*` messages?
8. Should reviewer peers receive only diffs over NMB, or also a read-only
   workspace clone/snapshot?
9. Should OpenAI traces be exported into NemoClaw audit DB rows, or referenced
   by trace ID only?

---

## 12  Recommendation

Treat the OpenAI Agents SDK as a strong candidate for a **sandbox-local
code-first agent runtime**, not as the NemoClaw orchestrator.

Adopt these pieces first:

- one-shot `Runner.run` execution behind an `OpenAiAgentsRunner`;
- function tools that wrap narrow NMB actions;
- SDK guardrails and result validation as defense in depth;
- trace projection into NemoClaw harness events;
- `final_output` extraction into typed `TaskCompletePayload`.

Keep these outside the SDK:

- sandbox creation and deletion;
- network and filesystem policy;
- workflow state and finalization;
- peer identity and routing;
- audit DB authority;
- Slack/dashboard rendering.

The immediate next step is a local three-process prototype using the code in
section 7, then an `OpenAiAgentsRunner` that can be selected by the existing
delegation path without changing the orchestrator's NMB protocol.
