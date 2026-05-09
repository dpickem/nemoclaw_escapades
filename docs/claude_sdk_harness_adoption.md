# Claude Agent SDK Harness - Adoption Notes

> **Sources:** [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview),
> [Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python),
> [MCP integration](https://code.claude.com/docs/en/agent-sdk/mcp), and
> [NMB Design](nmb_design.md)
>
> **Related:** [Cursor SDK Agent Harness - Adoption Notes](cursor_sdk_harness_adoption.md),
> [M2a - Reusable Agent Loop](design_m2a.md),
> [M2b - Multi-Agent Orchestration](design_m2b.md),
> [M3 - Multi-Sandbox Delegation](design_m3.md),
> [Sandbox Spawn Design](sandbox_spawn_design.md)
>
> **Last updated:** 2026-05-06

---

## Table of Contents

1. [Purpose](#1--purpose)
2. [What Claude Shipped](#2--what-claude-shipped)
3. [Fit With NemoClaw](#3--fit-with-nemoclaw)
4. [Adoption Recommendations](#4--adoption-recommendations)
   - [4.1 Use Claude Agent SDK as a runtime adapter](#41-use-claude-agent-sdk-as-a-runtime-adapter)
   - [4.2 Use NMB MCP tools for peer coordination](#42-use-nmb-mcp-tools-for-peer-coordination)
   - [4.3 Keep Claude SDK subagents separate from NemoClaw peer agents](#43-keep-claude-sdk-subagents-separate-from-nemoclaw-peer-agents)
   - [4.4 Use SDK permissions as defense in depth](#44-use-sdk-permissions-as-defense-in-depth)
   - [4.5 Use hooks for audit and progress, cautiously](#45-use-hooks-for-audit-and-progress-cautiously)
   - [4.6 Prefer `query()` first, then `ClaudeSDKClient`](#46-prefer-query-first-then-claudesdkclient)
   - [4.7 Integrate nv-tools as constrained Claude MCP tools](#47-integrate-nv-tools-as-constrained-claude-mcp-tools)
   - [4.8 Use OpenShell inference routing for Claude credentials](#48-use-openshell-inference-routing-for-claude-credentials)
5. [What Not To Adopt Directly](#5--what-not-to-adopt-directly)
   - [5.1 Do not replace NMB with SDK sessions](#51-do-not-replace-nmb-with-sdk-sessions)
   - [5.2 Do not rely on SDK subagents for security isolation](#52-do-not-rely-on-sdk-subagents-for-security-isolation)
   - [5.3 Do not expose arbitrary NMB send as a model tool](#53-do-not-expose-arbitrary-nmb-send-as-a-model-tool)
   - [5.4 Do not treat SDK permissions as the main policy boundary](#54-do-not-treat-sdk-permissions-as-the-main-policy-boundary)
   - [5.5 Do not make Managed Agents the default production path](#55-do-not-make-managed-agents-the-default-production-path)
   - [5.6 Do not mount real provider API keys into Claude sandboxes](#56-do-not-mount-real-provider-api-keys-into-claude-sandboxes)
6. [Conversation Notes Captured In This Design](#6--conversation-notes-captured-in-this-design)
7. [Full Code Example: Claude SDK Peers Over Current NMB Client](#7--full-code-example-claude-sdk-peers-over-current-nmb-client)
8. [Proposed NemoClaw Harness API Additions](#8--proposed-nemoclaw-harness-api-additions)
9. [Implementation Plan](#9--implementation-plan)
10. [Priority Matrix](#10--priority-matrix)
11. [Open Questions](#11--open-questions)
12. [Recommendation](#12--recommendation)

---

## 1  Purpose

The Claude Agent SDK exposes Claude Code's agent loop as a Python or TypeScript
library.  It can read files, run shell commands, edit code, use MCP tools,
install custom hooks, maintain sessions, and run specialized subagents in the
same process model as the Claude coding runtime.

This document records how NemoClaw should use the Claude Agent SDK as an
optional **agent runtime adapter** while preserving NemoClaw's existing harness
architecture:

1. **Do not replace OpenShell.**  OpenShell remains the sandbox, policy, and
   credential-routing boundary.
2. **Do not replace NMB.**  NMB remains the inter-sandbox message bus for
   task assignment, progress, peer review, audit flushes, and final results.
3. **Use the Claude Agent SDK inside each sandbox.**  The SDK should be one
   way to execute an assigned coding or review task, not the global control
   plane.
4. **Expose NMB to Claude through narrow in-process MCP tools.**  Avoid giving
   the model a raw arbitrary message-sending API.
5. **Adopt the useful SDK harness ideas.**  Claude's `query()` /
   `ClaudeSDKClient`, sessions, hooks, custom MCP tools, permission callbacks,
   and result messages map cleanly to NemoClaw's `AgentRun` and event stream.

The most important architectural point is the separation of responsibilities:

```text
NemoClaw orchestrator
  owns workflow state, sandbox lifecycle, NMB routing, audit DB, finalization

OpenShell sandbox
  owns process isolation, filesystem scope, network policy, credential routing

Claude Agent SDK process
  owns the local agent loop for one coding/review task

NMB client
  owns inter-sandbox messages and peer coordination
```

---

## 2  What Claude Shipped

The Claude Agent SDK provides a programmatic form of the Claude coding agent:

| Concept | Claude SDK shape | Why it matters to NemoClaw |
|---------|------------------|----------------------------|
| One-shot run | `query(prompt, options=ClaudeAgentOptions(...))` | Maps to a single NMB `task.assign` handled by a sandbox process. |
| Continuous session | `ClaudeSDKClient` with `connect`, `query`, `receive_response`, `interrupt` | Maps to a durable sub-agent session that can receive multiple assignments or redirects. |
| Built-in tools | `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, `Monitor` | Provides a mature coding tool loop without NemoClaw implementing each tool itself. |
| Custom MCP tools | `@tool(...)` plus `create_sdk_mcp_server(...)` | Best fit for exposing NMB operations to the model in a constrained way. |
| Permissions | `allowed_tools`, `disallowed_tools`, `permission_mode`, `can_use_tool` | Useful local guardrail layer inside the stronger OpenShell policy boundary. |
| Hooks | `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SessionEnd`, etc. | Maps to audit, progress updates, redaction, and policy enforcement hooks. |
| Sessions | SDK session IDs and `resume` | Useful for long-lived same-sandbox agents and task iterations. |
| SDK subagents | `AgentDefinition` plus the `Agent` tool | Useful inside a sandbox, but not a substitute for OpenShell-isolated NemoClaw peer agents. |
| Managed Agents comparison | SDK runs in "your process, your infrastructure" while Managed Agents are hosted | Confirms that the SDK path fits NemoClaw's self-managed OpenShell runtime. |

The SDK's MCP support is the key integration point.  NemoClaw can create an
in-process MCP server whose tool handlers close over a live `MessageBus`
instance.  Claude sees stable tools such as `mcp__nmb__request_review`; the
handler uses NMB `request()` / `reply()` under the hood.

---

## 3  Fit With NemoClaw

NemoClaw already has a reusable agent loop, typed NMB protocol, delegation
manager, audit DB, finalization flow, and OpenShell sandbox design.  The Claude
Agent SDK should therefore be evaluated as a **runtime implementation** rather
than a replacement architecture.

Current mapping:

| Claude SDK idea | Existing NemoClaw piece | Adoption stance |
|-----------------|-------------------------|-----------------|
| `query()` one-shot run | `_run_assigned_task` after NMB `task.assign` | Good fit for single-task coding and review workers. |
| `ClaudeSDKClient` | Future durable `AgentSession` | Good fit after NemoClaw has resumable run/session records. |
| `ResultMessage` | `TaskCompletePayload.summary` and finalization input | Use as one source for terminal task summaries. |
| `allowed_tools` / `permission_mode` | OpenShell policy plus approval gate | Use as local defense in depth, not as the primary sandbox boundary. |
| SDK MCP tools | NemoClaw tool registry and NMB client | Best fit for peer coordination and orchestration callbacks. |
| SDK hooks | Audit sinks, progress callbacks, future harness hooks | Useful, but should be constrained and audited. |
| SDK subagents | `DelegationManager`, `delegate_task`, NMB peer agents | Do not conflate; SDK subagents share a process/sandbox. |
| SDK session resume | Future `AgentSession.resume` | Useful for same-sandbox and local runtimes. |

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
Sandbox process running Claude Agent SDK
        |
        v
Claude built-in tools + NemoClaw NMB MCP tools
```

In this model, a coding sandbox and review sandbox are not Claude SDK
subagents.  They are separate OS processes, usually separate OpenShell
sandboxes, each running the Claude Agent SDK and each connected to NMB.

---

## 4  Adoption Recommendations

### 4.1 Use Claude Agent SDK as a runtime adapter

Add a `ClaudeSdkRuntime` or `ClaudeAgentSdkRunner` behind the NemoClaw harness:

```python
class ClaudeAgentSdkRunner:
    async def run_task(self, assignment: TaskAssignPayload) -> TaskCompletePayload:
        ...
```

The runner should:

- translate `TaskAssignPayload.prompt` into a Claude SDK prompt;
- configure `ClaudeAgentOptions` from task role, tool surface, max turns, model,
  and workspace root;
- expose only approved NemoClaw coordination tools through MCP;
- collect `ResultMessage.result`;
- compute the final diff through NemoClaw git helpers;
- return typed `TaskCompletePayload` or `TaskErrorPayload`.

This keeps the rest of the orchestrator stable.  The orchestrator should not
care whether a task was executed by NemoClaw's native `AgentLoop`, Claude Agent
SDK, Cursor SDK, or another future runner.

### 4.2 Use NMB MCP tools for peer coordination

Peer coordination should be tool-mediated, not raw bus access.  For example,
the coding agent may get:

| Tool | Backing NMB operation |
|------|-----------------------|
| `mcp__nmb__send_progress` | `bus.send(orchestrator_id, "task.progress", ...)` |
| `mcp__nmb__request_review` | `bus.request(reviewer_id, "review.request", ...)` |
| `mcp__nmb__ask_orchestrator` | `bus.request(orchestrator_id, "task.clarify", ...)` |

Avoid exposing a generic `send_message(to, type, payload)` tool at first.  The
model should not be able to invent message types, target arbitrary sandboxes, or
bypass the orchestrator's role policy.  Purpose-built tools are easier to audit
and map directly onto NMB message-type restrictions.

### 4.3 Keep Claude SDK subagents separate from NemoClaw peer agents

The Claude SDK supports `AgentDefinition` and an `Agent` tool.  That is useful
for local specialization inside a single sandbox.  It does not provide the
isolation NemoClaw needs for production peer agents.

Use this distinction:

| Kind | Isolation | Transport | Use case |
|------|-----------|-----------|----------|
| Claude SDK subagent | Same process / same sandbox | SDK-internal | Small focused analysis under the same trust boundary. |
| NemoClaw peer agent | Separate process / sandbox | NMB | Coding, review, data, and finalization agents with distinct policies. |

The reviewer in the code example below is a NemoClaw peer agent, not a Claude
SDK subagent.

### 4.4 Use SDK permissions as defense in depth

OpenShell remains the real security boundary.  Claude SDK permissions should
still be configured because they reduce accidental tool use and make local
behavior clearer:

```python
ClaudeAgentOptions(
    cwd=workspace_root,
    permission_mode="acceptEdits",
    allowed_tools=[
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Glob",
        "Grep",
        "mcp__nmb__send_progress",
        "mcp__nmb__request_review",
    ],
)
```

For read-only review workers, use only `Read`, `Glob`, and `Grep` plus the
single NMB reply path owned by the process wrapper.

### 4.5 Use hooks for audit and progress, cautiously

Claude SDK hooks can help bridge local SDK activity into NemoClaw's audit and
event model:

| Hook | NemoClaw use |
|------|--------------|
| `SessionStart` | Emit `system` / `run.started` event. |
| `PreToolUse` | Enforce additional local policy or redact arguments before audit. |
| `PostToolUse` | Buffer tool-call audit rows; emit `task.progress`. |
| `Stop` | Compute final diff, collect artifacts, emit terminal status. |

Do not let arbitrary repo-local hooks run privileged host commands.  Hooks that
touch workspace data should run inside the same OpenShell sandbox as the agent.

### 4.6 Prefer `query()` first, then `ClaudeSDKClient`

For M2b and early M3, `query()` is enough: each sub-agent process handles one
assignment and exits.  This matches the current `agent.__main__` shape:

```text
connect NMB -> wait for task.assign -> run task -> reply -> close
```

Use `ClaudeSDKClient` when NemoClaw needs:

- multiple NMB assignments in the same Claude session;
- `task.redirect` / interrupt support;
- explicit `resume` semantics;
- live model changes through orchestration;
- durable sessions in a long-running worker sandbox.

### 4.7 Integrate nv-tools as constrained Claude MCP tools

The Anthropic research-agent demo is a good template for `nv-tools` integration:
the lead Claude agent owns the conversation, delegates to specialized SDK
subagents, and uses hooks to track tool calls.  NemoClaw can add NVIDIA-internal
service access by exposing `nv-tools` through an in-process Claude SDK MCP
server.

There are three reasonable integration levels:

| Option | Shape | When to use |
|--------|-------|-------------|
| 1. Bash-only | Let the agent call `nv-tools ...` through `Bash`. | Fast local experiments; weakest policy and schema control. |
| 2. Generic read wrapper | One `nv_tools_read(args: list[str])` MCP tool that runs read-only CLI commands. | Best first version; keeps service coverage broad while blocking writes. |
| 3. Service-specific tools | Typed tools like `glean_search`, `jira_get_issue`, `gerrit_get_change`, each constructing safe CLI argv internally. | Best production shape; strongest prompts, validation, audit, and least room for model-invented commands. |

Option 2 is the fastest useful bridge:

```python
import asyncio
import json
import shlex
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool


SAFE_NV_TOOLS_SERVICES = {
    "jira",
    "confluence",
    "slack",
    "gitlab",
    "gerrit",
    "helios",
    "glean",
    "jenkins",
    "testbot",
    "linear",
}


@tool(
    "nv_tools_read",
    "Run a read-only nv-tools command and return its JSON envelope.",
    {"args": list[str]},
)
async def nv_tools_read(args: dict[str, Any]) -> dict[str, Any]:
    cmd_args = args["args"]
    if not cmd_args:
        return {
            "content": [{"type": "text", "text": "Missing nv-tools arguments."}],
            "is_error": True,
        }

    service = cmd_args[0]
    if service not in SAFE_NV_TOOLS_SERVICES:
        return {
            "content": [{"type": "text", "text": f"Service not allowed: {service}"}],
            "is_error": True,
        }

    if "--write" in cmd_args or "-w" in cmd_args:
        return {
            "content": [{"type": "text", "text": "WRITE operations are not allowed."}],
            "is_error": True,
        }

    if "--limit" not in cmd_args and service in {
        "jira",
        "slack",
        "gitlab",
        "gerrit",
        "glean",
        "linear",
    }:
        cmd_args.extend(["--limit", "10"])

    proc = await asyncio.create_subprocess_exec(
        "nv-tools",
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

    text = stdout.decode().strip() or stderr.decode().strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = {
            "success": proc.returncode == 0,
            "raw_output": text,
            "command": " ".join(["nv-tools", *map(shlex.quote, cmd_args)]),
        }

    return {"content": [{"type": "text", "text": json.dumps(envelope, indent=2)}]}


nv_tools_server = create_sdk_mcp_server(
    name="nv_tools",
    version="1.0.0",
    tools=[nv_tools_read],
)
```

Then wire it into the Claude research-agent pattern:

```python
agents = {
    "researcher": AgentDefinition(
        description=(
            "Use this agent to gather internal NVIDIA context using Glean, "
            "Confluence, Jira, Slack, Gerrit, GitLab, and Helios through nv-tools."
        ),
        tools=[
            "mcp__nv_tools__nv_tools_read",
            "Write",
            "Read",
            "Glob",
        ],
        prompt=researcher_prompt,
        model="haiku",
    ),
    "report-writer": AgentDefinition(
        description="Synthesize saved notes into a report. Does not call external services.",
        tools=["Skill", "Write", "Glob", "Read", "Bash"],
        prompt=report_writer_prompt,
        model="haiku",
    ),
}

options = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    setting_sources=["project"],
    system_prompt=lead_agent_prompt,
    allowed_tools=["Task", "mcp__nv_tools__nv_tools_read"],
    agents=agents,
    mcp_servers={"nv_tools": nv_tools_server},
    hooks=hooks,
    model="haiku",
)
```

Prompt guidance should make the wrapper's contract explicit:

```text
For NVIDIA-internal lookups, use mcp__nv_tools__nv_tools_read.
Pass arguments exactly as nv-tools CLI arguments, for example:
- ["glean", "search", "NMB sandbox design", "--limit", "5"]
- ["jira", "get-issue", "AVPC-12345"]
- ["gerrit", "get-change", "123456"]
Never use --write. Ask the user before any write operation.
```

Option 3 turns common commands into typed, service-specific tools.  The model no
longer passes arbitrary CLI arguments; each tool validates a narrow input schema
and constructs the `nv-tools` command itself.  That gives better descriptions,
safer defaults, per-tool audit metadata, and cleaner prompts:

```python
async def run_nv_tools_json(args: list[str], *, timeout: float = 60.0) -> dict[str, Any]:
    if "--write" in args or "-w" in args:
        raise ValueError("WRITE operations require an explicit user-approved tool path")

    proc = await asyncio.create_subprocess_exec(
        "nv-tools",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    text = stdout.decode().strip() or stderr.decode().strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"nv-tools returned non-JSON output: {text[:500]}") from exc

    if not envelope.get("success", False):
        error = envelope.get("error", {})
        code = error.get("code", "UNKNOWN")
        message = error.get("message", text[:500])
        raise RuntimeError(f"nv-tools failed [{code}]: {message}")

    return envelope


@tool(
    "glean_search",
    "Search NVIDIA enterprise content through nv-tools Glean.",
    {"query": str, "limit": int},
)
async def glean_search(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit", 5)), 10))
    envelope = await run_nv_tools_json(
        ["glean", "search", args["query"], "--limit", str(limit)]
    )
    return {"content": [{"type": "text", "text": json.dumps(envelope["data"], indent=2)}]}


@tool(
    "jira_get_issue",
    "Fetch one Jira issue through nv-tools.",
    {"issue_key": str},
)
async def jira_get_issue(args: dict[str, Any]) -> dict[str, Any]:
    envelope = await run_nv_tools_json(["jira", "get-issue", args["issue_key"]])
    return {"content": [{"type": "text", "text": json.dumps(envelope["data"], indent=2)}]}


@tool(
    "gerrit_get_change",
    "Fetch one Gerrit change through nv-tools.",
    {"change": str},
)
async def gerrit_get_change(args: dict[str, Any]) -> dict[str, Any]:
    envelope = await run_nv_tools_json(["gerrit", "get-change", args["change"]])
    return {"content": [{"type": "text", "text": json.dumps(envelope["data"], indent=2)}]}


nv_tools_research_server = create_sdk_mcp_server(
    name="nv_tools_research",
    version="1.0.0",
    tools=[glean_search, jira_get_issue, gerrit_get_change],
)
```

With option 3, role definitions can be much tighter:

```python
agents = {
    "internal-researcher": AgentDefinition(
        description=(
            "Use this agent for NVIDIA-internal research across Glean, Jira, "
            "and Gerrit. It can read enterprise context but cannot write."
        ),
        tools=[
            "mcp__nv_tools_research__glean_search",
            "mcp__nv_tools_research__jira_get_issue",
            "mcp__nv_tools_research__gerrit_get_change",
            "Write",
            "Read",
            "Glob",
        ],
        prompt=internal_researcher_prompt,
        model="haiku",
    )
}
```

For write operations, do not add a generic write-capable wrapper.  Create
separate user-approved tools such as `jira_add_comment_approved` or
`slack_send_approved` that only run after the orchestrator has recorded explicit
approval, always pass `--write`, and emit a NemoClaw audit event.  This mirrors
`nv-tools`' own READ/WRITE model while keeping the Claude-facing tool surface
role-specific.

Operational requirements:

- Install `nv-tools` from `~/workspace/nv_tools` with `make install` so the
  `nv-tools` shim is on `PATH`.
- The `nv_tools` package currently requires Python 3.12+, so the Claude SDK
  sandbox image must either use Python 3.12 or call the installed CLI shim from
  a compatible environment.
- Run `nv-tools health` during sandbox bootstrap.  It is offline and diagnoses
  configured services.
- For API-backed commands, OpenShell policy must allow the relevant service
  endpoints or an egress path that the `nv-tools` command can use.
- `nv-tools` loads environment from `~/.env`, `~/workspace/nv_tools/.env`, or
  process environment; secrets should come from OpenShell/provider injection,
  not from prompts.
- Default output is a JSON envelope.  Always parse `success` first; if false,
  inspect `error.code` and `error.message`.
- Cap search/list commands with `--limit` to avoid flooding the model context.
- Keep `Bash` and write-capable `nv-tools` separated unless the role is
  explicitly trusted and approval-gated.

### 4.8 Use OpenShell inference routing for Claude credentials

The Claude Agent SDK examples normally check for `ANTHROPIC_API_KEY` in the
process environment.  That is not the right production shape inside an
OpenShell sandbox.  The sandbox should not contain the real Anthropic key, and
the model should never be able to read it through `Bash`, `Read`, environment
inspection, crash logs, or tool output.

Instead, Claude SDK workers should use the same OpenShell credential-injection
pattern as NemoClaw's native agent loop:

```text
Claude SDK process inside sandbox
  |
  | HTTPS request to inference.local (Anthropic-compatible API)
  | sandbox env contains only placeholder / non-secret client config
  v
OpenShell proxy / inference router
  |
  | validates sandbox policy and calling binary
  | strips sandbox-supplied auth headers
  | injects real provider credential outside the sandbox
  v
Anthropic / NIM / model provider endpoint
```

Operationally, the sandbox gets routing configuration, not credentials:

```bash
# Non-secret values inside the sandbox.
export ANTHROPIC_BASE_URL="https://inference.local/anthropic"
export ANTHROPIC_API_KEY="openshell-placeholder"

# Optional: keep provider choice in NemoClaw/OpenShell config, not in prompts.
export CLAUDE_CODE_USE_BEDROCK=
export CLAUDE_CODE_USE_VERTEX=
```

The exact base-URL variable should be verified against the pinned Claude Agent
SDK / Claude Code version during implementation.  The important invariant is
not the env var name; it is that the SDK's HTTP traffic targets an
OpenShell-routed Anthropic-compatible endpoint and that any sandbox-visible key
is a dummy value.  Some clients require a syntactically present API key before
they will start, so the placeholder is allowed only because the OpenShell proxy
strips and replaces auth before egress.

OpenShell policy then grants the Claude worker only the inference route it
needs:

```yaml
network_policies:
  inference:
    name: claude-inference
    endpoints:
      - host: inference.local
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
        access: full
    binaries:
      - path: /usr/bin/python3
      - path: /usr/local/bin/python
      - path: /usr/local/bin/claude
```

The host or gateway owns the real provider binding:

```yaml
# Conceptual OpenShell / NemoClaw host-side config, not mounted into the agent.
providers:
  anthropic-claude:
    route: inference.local/anthropic
    upstream: https://api.anthropic.com
    secret_ref: ANTHROPIC_API_KEY
    inject_headers:
      x-api-key: "${secret:ANTHROPIC_API_KEY}"
      anthropic-version: "2023-06-01"
```

The proxy should enforce these rules:

- Only policy-approved sandbox processes can connect to `inference.local`.
- The sandbox cannot choose arbitrary upstream hosts.
- Sandbox-supplied `x-api-key`, `Authorization`, and provider auth headers are
  dropped.
- The real provider key is injected after policy evaluation, outside the
  sandbox namespace.
- Request/response metadata is audited without persisting provider secrets.

This means the Claude SDK runner setup should fail closed:

```python
def build_claude_env(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)

    # Placeholder only.  The OpenShell inference proxy injects the real key.
    env["ANTHROPIC_API_KEY"] = "openshell-placeholder"
    env["ANTHROPIC_BASE_URL"] = "https://inference.local/anthropic"

    # Avoid accidentally selecting a credential path that expects cloud creds
    # inside the sandbox unless the OpenShell policy explicitly supports it.
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    return env
```

If the pinned Claude Agent SDK cannot target a custom Anthropic-compatible base
URL, do not mount the real key into the sandbox as a workaround.  Choose one of
these safer paths instead:

1. Add an OpenShell-compatible adapter endpoint that presents the API shape the
   SDK expects at `inference.local`.
2. Run the Claude SDK runner only in a trusted orchestrator environment and use
   NMB/OpenShell for isolated tool execution.
3. Fall back to NemoClaw's native `AgentLoop` or another SDK that can target the
   OpenShell inference route.

For `nv-tools`, use the same principle.  Service API tokens should be injected
by OpenShell/provider configuration or mounted as narrowly scoped runtime
secrets, not written into prompts or broad shell-visible files.  If a service
requires interactive login (for example device-code auth), the sandbox should
surface an auth-required error to the orchestrator rather than attempting to
complete the login flow autonomously.

---

## 5  What Not To Adopt Directly

### 5.1 Do not replace NMB with SDK sessions

Claude SDK sessions preserve local conversation context.  They do not route
messages between isolated sandboxes, audit inter-agent traffic, enforce
sandbox identity, or provide the brokered request/reply semantics needed by
NemoClaw.

NMB remains responsible for:

- peer discovery and routing;
- `task.assign`, `task.progress`, `task.complete`, `task.error`;
- `review.request`, `review.feedback`;
- `audit.flush`;
- future `task.redirect` and `policy.*` messages.

### 5.2 Do not rely on SDK subagents for security isolation

Claude SDK subagents are useful local specialists but they share the parent
process and sandbox.  NemoClaw's coding/review/finalization separation depends
on different OpenShell policies and independent failure domains.

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

### 5.4 Do not treat SDK permissions as the main policy boundary

SDK permissions are a useful guardrail but not a containment model.  OpenShell
must remain the source of truth for filesystem, network, credentials, and
process capabilities.

### 5.5 Do not make Managed Agents the default production path

Claude Managed Agents run in Anthropic-managed infrastructure.  The Agent SDK
runs in "your process, your infrastructure", which is the mode that fits
NemoClaw's OpenShell and NMB design.  Managed Agents may be useful for external
or public workflows later, but they do not satisfy the current internal
sandboxing and credential requirements.

### 5.6 Do not mount real provider API keys into Claude sandboxes

Do not set real values for `ANTHROPIC_API_KEY`, Bedrock credentials, Vertex
credentials, or other provider secrets inside a Claude SDK sandbox.  The
sandbox-visible environment is inspectable by the agent's tools and by ordinary
process debugging.  Provider credentials belong in OpenShell's host-side
inference routing configuration, where the proxy can inject them after policy
checks.

The only acceptable sandbox-side key is a non-secret placeholder used to satisfy
client validation before the request is routed to `inference.local`.

The detailed lifecycle should look like this:

```text
1. Orchestrator creates a Claude worker sandbox
   - OpenShell assigns sandbox identity and policy.
   - Policy allows egress to inference.local only, not api.anthropic.com.
   - Policy allows the Python/Claude worker binary to use that route.

2. Orchestrator starts the Claude SDK process
   - Environment contains ANTHROPIC_API_KEY=openshell-placeholder.
   - Environment/config points the SDK at an Anthropic-compatible
     inference.local endpoint.
   - No real provider token is present in env, files, prompts, or task payloads.

3. Claude SDK validates local config
   - If it requires an API key-shaped value, the placeholder satisfies startup.
   - If it cannot target the OpenShell endpoint, the runner must fail closed.

4. Claude SDK makes an HTTPS request
   - Destination is inference.local, resolved by OpenShell/proxy plumbing.
   - The request may include the placeholder x-api-key or Authorization header.

5. OpenShell proxy evaluates policy
   - Confirms sandbox identity.
   - Confirms the calling process/binary is allowed.
   - Confirms the requested logical provider route is allowed for this sandbox.
   - Rejects any attempt to reach the public provider directly.

6. OpenShell proxy rewrites credentials
   - Drops sandbox-supplied auth headers.
   - Reads the real provider secret from host-side secret storage.
   - Injects the real Anthropic/NIM/provider header outside the sandbox.
   - Forwards to the configured upstream provider.

7. Response returns through the proxy
   - Provider response is passed back to the SDK process.
   - Audit logs record metadata and policy decisions, not provider secrets.
```

The runner should therefore prepare a deliberately boring sandbox environment:

```python
def build_claude_sandbox_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)

    # Placeholder only.  It exists for SDK startup validation, not auth.
    env["ANTHROPIC_API_KEY"] = "openshell-placeholder"

    # Exact variable name must be verified against the pinned SDK/CLI version.
    # The invariant is: all model traffic goes through OpenShell.
    env["ANTHROPIC_BASE_URL"] = "https://inference.local/anthropic"

    # Avoid accidentally selecting credential paths that expect secrets inside
    # the sandbox.
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("AWS_SESSION_TOKEN", None)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)
    env.pop("CLAUDE_CODE_USE_VERTEX", None)
    return env
```

The corresponding OpenShell/provider-side configuration is conceptually:

```yaml
sandbox_policy:
  network_policies:
    inference:
      endpoints:
        - host: inference.local
          port: 443
          protocol: rest
          tls: terminate
          enforcement: enforce
          access: full
      binaries:
        - path: /usr/bin/python3
        - path: /usr/local/bin/python
        - path: /usr/local/bin/claude

host_provider_routes:
  inference.local/anthropic:
    upstream: https://api.anthropic.com
    secret_ref: anthropic_api_key
    strip_request_headers:
      - x-api-key
      - authorization
    inject_request_headers:
      x-api-key: "${secret:anthropic_api_key}"
      anthropic-version: "2023-06-01"
```

This is intentionally analogous to NMB's `messages.local` route: the sandbox
gets a logical capability, while the gateway owns the real endpoint,
authentication, identity injection, and audit.

There are two important implementation details:

- The placeholder must never be accepted by the real upstream.  If a request
  escapes the proxy and reaches `api.anthropic.com` directly, it should fail
  authentication rather than succeed.
- The proxy must strip sandbox-provided auth headers even if they look valid.
  Otherwise an agent could smuggle a different key or cause confusing audit
  attribution.

Failure modes should be explicit:

| Failure | Expected behavior |
|---------|-------------------|
| Missing host-side provider secret | Proxy returns a configuration error; runner sends `task.error`. |
| SDK cannot set custom base URL | Runner fails closed; do not mount the real key. |
| Sandbox tries direct provider egress | OpenShell policy denies the connection. |
| Agent prints env vars | It can only expose the placeholder, not the real key. |
| Proxy sees sandbox auth header | Proxy strips it before forwarding. |
| Provider returns auth error | Error is surfaced to orchestrator without logging secrets. |

If the Claude SDK version cannot be made to use `inference.local`, the safe
fallbacks are:

1. Put an adapter at `inference.local` that speaks the exact API shape the SDK
   expects and forwards through OpenShell's provider injection path.
2. Run the Claude SDK only in a trusted non-sandbox orchestrator process, while
   keeping tool execution and peer workers isolated in OpenShell.
3. Use NemoClaw's native `AgentLoop` for sandboxed workers until the SDK can be
   routed correctly.

The unacceptable fallback is mounting a real provider key into the sandbox.

---

## 6  Conversation Notes Captured In This Design

This section records the design discussion that led to the proposed shape.

### 6.1 Initial question: how would Claude Code connect to NMB?

Question:

```text
how would claude code via agent sdk connect to the nmb bus in @docs/nmb_design.md?
```

Answer captured:

Claude should not connect to peers directly.  A sandbox process creates a
`MessageBus` with a stable sandbox identity, connects to the broker at
`ws://messages.local:9876` (or the current forwarded/tunneled endpoint), listens
for `task.assign`, runs the agent, and replies with `task.complete` or
`task.error`.

The concrete shape:

```text
Orchestrator sandbox
  NemoClaw orchestrator + MessageBus("orchestrator")

NMB broker
  routes task.assign / task.complete / task.error / audit.flush

Coding sandbox
  Claude Agent SDK process + MessageBus("coding-<id>")
```

For the current repo, this maps to `src/nemoclaw_escapades/agent/__main__.py`:
connect to NMB, wait for one assignment, run the task, flush audit, reply, and
exit.

### 6.2 Follow-up: how would peer coordination work?

Question:

```text
how concretely would peer coordination work? write a full code example with agent sdk and the current nmb client implementation
```

Answer captured:

Use separate SDK processes as peers.  The coding process exposes an in-process
MCP tool like `request_review`; that tool uses the current NMB client
`bus.request(reviewer_id, "review.request", ...)`.  The review process listens
for `review.request`, runs its own Claude SDK review prompt, then replies with
`bus.reply(original_msg, "review.feedback", ...)`.

The model sees a narrow semantic tool.  The process wrapper owns the bus,
targets, message types, timeouts, and payload schema.

### 6.3 Correction: this means Claude Agent SDK, not OpenAI Agents SDK

Correction:

```text
i actually meant the claude agent sdk https://code.claude.com/docs/en/agent-sdk/overview#agent-sdk-vs-managed-agents
```

Updated answer captured:

With the Claude Agent SDK, NMB is best exposed through SDK MCP tools created
with `@tool` and `create_sdk_mcp_server`.  Claude's built-in tools handle local
coding work; the custom MCP tools bridge specific coordination actions to NMB.
This is consistent with the SDK's "your process, your infrastructure" model and
keeps OpenShell/NMB as the outer harness.

---

## 7  Full Code Example: Claude SDK Peers Over Current NMB Client

This example uses the current async NMB client implementation:

- `MessageBus.connect_with_retry()`
- `MessageBus.listen()`
- `MessageBus.request()`
- `MessageBus.reply()`
- `MessageBus.send()`

It starts three processes:

```bash
ROLE=reviewer SANDBOX_ID=reviewer-1 python nmb_claude_peer_demo.py
ROLE=coding SANDBOX_ID=coding-1 REVIEWER_ID=reviewer-1 python nmb_claude_peer_demo.py
ROLE=orchestrator CODING_ID=coding-1 python nmb_claude_peer_demo.py
```

In OpenShell, use `NMB_URL=ws://messages.local:9876` once native routing exists.
In the current prototype, set `NMB_URL` to the forwarded or tunneled broker
endpoint, for example `ws://host.docker.internal:9876`.

```python
# nmb_claude_peer_demo.py
#
# Run three processes:
#   ROLE=reviewer SANDBOX_ID=reviewer-1 python nmb_claude_peer_demo.py
#   ROLE=coding SANDBOX_ID=coding-1 REVIEWER_ID=reviewer-1 python nmb_claude_peer_demo.py
#   ROLE=orchestrator CODING_ID=coding-1 python nmb_claude_peer_demo.py
#
# In OpenShell use NMB_URL=ws://messages.local:9876.
# In the current forwarded prototype, use the forwarded URL instead.

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.nmb.models import NMBMessage


NMB_URL = os.getenv("NMB_URL", "ws://messages.local:9876")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", os.getcwd()))


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


async def run_claude(prompt: str, options: ClaudeAgentOptions) -> str:
    final_result = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            final_result = message.result or ""
    return final_result


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

    async for msg in bus.listen():
        if msg.type != "review.request":
            continue

        payload = msg.payload or {}
        diff = payload.get("diff", "")
        original_task = payload.get("original_task", "")

        review_prompt = f"""
You are a strict code reviewer. Review the diff for correctness, regressions,
security issues, and missing tests.

Original task:
{original_task}

Diff:
{diff}

Return exactly this structure:
VERDICT: approve | request_changes
SUMMARY: one paragraph
COMMENTS:
- file/path.py: issue or recommendation
"""

        try:
            review_text = await run_claude(
                review_prompt,
                ClaudeAgentOptions(
                    cwd=WORKSPACE_ROOT,
                    allowed_tools=["Read", "Glob", "Grep"],
                    max_turns=8,
                ),
            )
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

        @tool(
            "send_progress",
            "Send a short progress update to the orchestrator over NMB.",
            {"status": str},
        )
        async def send_progress(args: dict[str, Any]) -> dict[str, Any]:
            await bus.send(
                payload["orchestrator_id"],
                "task.progress",
                {
                    "workflow_id": workflow_id,
                    "status": args["status"],
                },
            )
            return {"content": [{"type": "text", "text": "Progress sent."}]}

        @tool(
            "request_review",
            "Ask the peer review agent to review the current git diff.",
            {"summary": str},
        )
        async def request_review(args: dict[str, Any]) -> dict[str, Any]:
            diff = await run_git_diff(WORKSPACE_ROOT)
            response = await bus.request(
                reviewer_id,
                "review.request",
                {
                    "workflow_id": workflow_id,
                    "original_task": original_task,
                    "summary": args["summary"],
                    "diff": diff,
                    "workspace_root": str(WORKSPACE_ROOT),
                },
                timeout=300,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(response.payload or {}, indent=2),
                    }
                ]
            }

        nmb_tools = create_sdk_mcp_server(
            name="nmb",
            version="1.0.0",
            tools=[send_progress, request_review],
        )

        coding_prompt = f"""
Implement this task in the current repository:

{original_task}

You have two NMB peer-coordination tools:
- mcp__nmb__send_progress: send brief status updates.
- mcp__nmb__request_review: ask the review sandbox to review your current git diff.

Before you finish, call mcp__nmb__request_review. If the reviewer requests
changes, address them and request review again. Finish with a concise summary.
"""

        try:
            result = await run_claude(
                coding_prompt,
                ClaudeAgentOptions(
                    cwd=WORKSPACE_ROOT,
                    permission_mode="acceptEdits",
                    mcp_servers={"nmb": nmb_tools},
                    allowed_tools=[
                        "Read",
                        "Write",
                        "Edit",
                        "Bash",
                        "Glob",
                        "Grep",
                        "mcp__nmb__send_progress",
                        "mcp__nmb__request_review",
                    ],
                    max_turns=40,
                ),
            )
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
coding       -> Claude SDK query(...)
coding       -> NMB send(task.progress) -> orchestrator
coding       -> MCP request_review tool
coding       -> NMB request(review.request) -> reviewer
reviewer     -> Claude SDK query(...)
reviewer     -> NMB reply(review.feedback) -> coding
coding       -> Claude SDK addresses feedback if needed
coding       -> NMB reply(task.complete) -> orchestrator
```

### 7.2 Why this is the right boundary

The code example deliberately keeps NMB outside the model's raw control:

- The wrapper owns the bus connection and sandbox identity.
- The assignment owns `workflow_id`, `orchestrator_id`, and `reviewer_id`.
- The model can only call semantic MCP tools.
- Each semantic tool maps to exactly one allowed NMB operation.
- The reviewer cannot write files because its SDK options expose only read
  tools.
- OpenShell still enforces the real filesystem and network policy.

---

## 8  Proposed NemoClaw Harness API Additions

The Cursor adoption doc proposes an internal `AgentSession` / `AgentRun`
harness.  Claude SDK support should slot into that API as a runtime backend:

```python
agent = await AgentSession.create(
    runtime=OpenShellRuntime(
        runner="claude-agent-sdk",
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
@dataclass(frozen=True)
class ClaudeSdkRunnerOptions:
    allowed_tools: list[str]
    permission_mode: str | None = None
    model: str | None = None
    max_turns: int | None = None
    use_session_client: bool = False


class ClaudeSdkRunner:
    async def run(
        self,
        assignment: TaskAssignPayload,
        bus: MessageBus,
        options: ClaudeSdkRunnerOptions,
    ) -> TaskCompletePayload:
        ...
```

The first version can support only one-shot `query()`.  Later versions can use
`ClaudeSDKClient` for durable sessions and `task.redirect`.

---

## 9  Implementation Plan

### Phase C0 - Prototype wrapper

Create a standalone example or test fixture similar to the code in section 7.
Use a local NMB broker and three local processes.

Exit criteria:

- Orchestrator sends `task.assign` via `bus.request`.
- Coding SDK process receives it and calls Claude SDK.
- Coding process calls reviewer through an MCP `request_review` tool.
- Reviewer SDK process replies with `review.feedback`.
- Coding process replies with `task.complete`.

### Phase C1 - Runner abstraction

Introduce a `ClaudeSdkRunner` that can be called from the existing NMB
sub-agent path.

Exit criteria:

- Runner converts `TaskAssignPayload` to `ClaudeAgentOptions`.
- Runner returns typed `TaskCompletePayload` / `TaskErrorPayload`.
- Current native `AgentLoop` path remains available.

### Phase C1a - OpenShell inference credential routing

Validate that the pinned Claude Agent SDK can target an OpenShell-routed
Anthropic-compatible endpoint without a real sandbox-local API key.

Exit criteria:

- Claude SDK starts with only placeholder sandbox credentials.
- Claude SDK traffic targets `inference.local` or an equivalent OpenShell route.
- OpenShell strips sandbox-supplied provider auth headers and injects the real
  credential outside the sandbox.
- The sandbox policy allows inference only for the Claude worker process and
  does not allow direct `api.anthropic.com` egress.
- A missing/expired provider secret fails as a clear orchestrator-visible
  configuration error, not as a leaked secret or silent fallback.
- If the SDK cannot target the route, implementation chooses an adapter or
  native `AgentLoop` fallback rather than mounting real keys.

### Phase C2 - NMB MCP tool factory

Create a small factory that builds role-specific NMB MCP tools:

```python
tools = create_nmb_mcp_tools(
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

### Phase C2a - nv-tools MCP tool factory

Create a Claude SDK MCP server that exposes `nv-tools` through read-only,
role-specific tools.  Start with the generic `nv_tools_read` wrapper for broad
coverage, then promote common workflows to typed tools such as `glean_search`,
`jira_get_issue`, and `gerrit_get_change`.

Exit criteria:

- `nv-tools health` runs during sandbox bootstrap and reports auth/config state.
- Generic read wrapper rejects `--write`, enforces a service allowlist, and adds
  limits to search/list commands.
- Typed service tools construct CLI argv internally and parse the JSON envelope.
- Failed `nv-tools` envelopes become clear tool errors with `error.code` and
  `error.message`.
- Write-capable wrappers are absent or require explicit orchestrator approval
  and always pass `--write`.

### Phase C3 - Hook and audit integration

Use Claude SDK hooks to feed NemoClaw's audit sink and event stream.

Exit criteria:

- Tool starts and finishes are visible as harness events.
- Tool-call audit rows include workflow ID, parent sandbox ID, agent ID, and
  role.
- Hook failures do not prevent `task.error` replies.

### Phase C4 - Durable sessions

Use `ClaudeSDKClient` for workers that handle multiple assignments or
interrupts.

Exit criteria:

- `task.redirect` maps to `client.interrupt()` plus a follow-up query.
- Session ID is persisted on the run record.
- Reconnect/resume behavior is documented and tested.

### Phase C5 - OpenShell integration

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
| P0 | Claude SDK as runtime adapter | Lets NemoClaw evaluate Claude's mature coding loop without replacing orchestration. |
| P0 | OpenShell inference credential routing | Required before running Claude SDK in sandboxes without leaking provider keys. |
| P0 | NMB MCP tool surface | Enables peer coordination while preserving message policy and auditability. |
| P0 | One-shot `query()` worker | Matches current single-assignment sub-agent lifecycle. |
| P1 | Read-only nv-tools MCP surface | Gives Claude agents NVIDIA-internal research and review context without exposing writes. |
| P1 | Hook-based progress/audit | Improves observability and training trace quality. |
| P1 | Runner abstraction | Allows native, Claude SDK, and future SDK runners behind one harness API. |
| P2 | Typed nv-tools service tools | Production-quality shape after the generic wrapper proves which workflows matter. |
| P2 | `ClaudeSDKClient` durable sessions | Needed for redirect/resume, but not for first peer-review prototype. |
| P2 | SDK subagents inside a sandbox | Useful local specialization, but not required for OpenShell-isolated peers. |
| P3 | Managed Agents interop | Not aligned with current internal sandbox and credential requirements. |

---

## 11  Open Questions

1. Should Claude SDK support be a first-class runtime in `AgentSession`, or an
   experimental runner behind a feature flag?
2. How should `TaskAssignPayload.tool_surface` map to Claude SDK
   `allowed_tools`, `disallowed_tools`, `permission_mode`, and OpenShell policy?
3. Should the NMB MCP tools live in a generic factory or in role-specific
   runner modules?
4. How much of Claude SDK message objects should be persisted, given tool
   outputs may contain sensitive repo or credential-derived data?
5. Should `review.feedback` be free-form text, a typed Pydantic payload, or both?
6. How should Claude SDK session IDs map to NemoClaw `agent_id`, `workflow_id`,
   and future `run_id`?
7. Can `ClaudeSDKClient.interrupt()` reliably implement `task.redirect`, or do
   some tool calls require OpenShell process-level cancellation?
8. Should reviewer peers receive only diffs over NMB, or also a read-only
   workspace clone/snapshot?
9. Should the first `nv-tools` integration expose a generic read-only wrapper,
   or start directly with typed service-specific tools?
10. How should `nv-tools` auth failures be surfaced to the user when a sandbox
    cannot complete interactive login itself?
11. Should `nv-tools` audit JSONL be ingested into NemoClaw's audit DB, or
    should NemoClaw audit only the Claude MCP tool invocation envelope?
12. Which exact Claude Agent SDK / Claude Code environment variable should the
    runner use for the OpenShell-routed Anthropic-compatible base URL?
13. Should `inference.local` expose a native Anthropic Messages API, a
    translation layer, or both Anthropic- and OpenAI-compatible paths?
14. How should provider-secret rotation be reported to live Claude SDK workers
    that only hold placeholder sandbox credentials?

---

## 12  Recommendation

Treat Claude Agent SDK as a strong candidate for a **sandbox-local coding and
review runtime**, not as the NemoClaw orchestrator.

Adopt these pieces first:

- one-shot `query()` execution behind a `ClaudeSdkRunner`;
- OpenShell-routed inference with placeholder sandbox credentials;
- in-process MCP tools that wrap narrow NMB actions;
- read-only `nv-tools` MCP tools for internal research and review context;
- SDK permissions as defense in depth;
- SDK hooks for progress and audit;
- `ResultMessage` extraction into typed `TaskCompletePayload`.

Keep these outside the SDK:

- sandbox creation and deletion;
- network and filesystem policy;
- workflow state and finalization;
- peer identity and routing;
- audit DB authority;
- Slack/dashboard rendering.

The immediate next step is a local three-process prototype using the code in
section 7, then a `ClaudeSdkRunner` that can be selected by the existing
delegation path without changing the orchestrator's NMB protocol.
