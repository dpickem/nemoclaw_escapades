# Sandbox-to-Sandbox Launch Design (OpenShell + NMB)

> **Status:** Proposed
>
> **Last updated:** 2026-04-10
>
> **Related:**
> [NMB Design](nmb_design.md) |
> [Orchestrator Design](orchestrator_design.md) |
> [OpenShell Deep Dive](deep_dives/openshell_deep_dive.md)

---

## 1  Goal

Allow the orchestrator running inside an OpenShell sandbox to launch additional
OpenShell sandboxes for scoped tasks.

The hierarchy is encoded at the application layer (NMB metadata), not by
nested containers.

---

## 2  Non-Goals

- No Docker-in-Docker or sandbox-inside-sandbox runtime.
- No change to OpenShell core architecture.
- No broker protocol rewrite; use existing NMB payload fields.

---

## 3  Architecture (Control Plane + Message Plane)

```mermaid
flowchart LR
  orchestratorSandbox["OrchestratorSandbox"]
  openShellGateway["OpenShellGateway"]
  childSandbox["ChildSandbox"]
  nmbBroker["NMBBroker(messages.local)"]

  orchestratorSandbox -->|"openshell sandbox create/delete"| openShellGateway
  openShellGateway -->|"provision + lifecycle"| childSandbox
  orchestratorSandbox <-->|"control + result messages"| nmbBroker
  childSandbox <-->|"task + status messages"| nmbBroker
```

Key point: the orchestrator sandbox and child sandboxes are sibling containers
managed by the gateway. "Parent/child" is a logical relationship tracked in NMB
messages and task state.

---

## 4  Spawn Contract (NMB Metadata)

No wire-protocol changes are required. Add hierarchy metadata in payloads:

- `workflow_id`: stable ID for the delegated task tree.
- `root_sandbox_id`: the top-level orchestrator sandbox ID.
- `parent_sandbox_id`: immediate launcher sandbox ID.
- `role`: `coding`, `review`, `research`, etc.
- `ttl_s`: optional expected lifetime for cleanup automation.

Suggested control events:

- `sandbox.spawn.request`
- `sandbox.spawn.started`
- `sandbox.spawn.failed`
- `sandbox.spawn.terminated`

This keeps topology and lifecycle observable without coupling NMB to OpenShell
internals.

---

## 5  Required Docker Image Changes

Target file: [`docker/Dockerfile.orchestrator`](../docker/Dockerfile.orchestrator)

1. **Install OpenShell CLI in the runtime image**
   - Track the latest stable OpenShell CLI by default.
   - Keep a build-time override (`OPENSHELL_VERSION`) so we can quickly pin/roll
     back if a new release regresses behavior.
   - Preferred pattern: download official release artifact in build stage and
     copy the binary into runtime stage.
   - Validate at build time with `openshell --version`.

2. **Add minimal runtime dependencies for CLI execution**
   - Ensure `ca-certificates` and `curl` are present.
   - Keep image lean; avoid adding Docker engine/socket support.

3. **Prepare writable CLI config location**
   - Keep `HOME=/app` (already aligned with sandbox user home).
   - Ensure `/app/.config` exists and is writable by `sandbox` user
     (already allowed by policy).
   - Optional: set `XDG_CONFIG_HOME=/app/.config` explicitly.

4. **Do not change current OpenShell supervisor assumptions**
   - Continue to avoid setting `USER` and `ENTRYPOINT`.
   - Continue creating `sandbox` user/group for policy `run_as_user`.

Example shape (illustrative, not exact release URL):

```dockerfile
ARG OPENSHELL_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# Resolve latest stable release when OPENSHELL_VERSION=latest.
RUN curl -fsSL "<openshell-release-url>" -o /tmp/openshell \
    && install -m 0755 /tmp/openshell /usr/local/bin/openshell \
    && openshell --version
ENV XDG_CONFIG_HOME=/app/.config
```

---

## 6  Policy + Config Deltas

### 6.1  OpenShell Network Policy

Target file: [`policies/orchestrator.yaml`](../policies/orchestrator.yaml)

- Add a network policy entry for the gateway endpoint used by the in-sandbox
  OpenShell CLI.
- Include `/usr/local/bin/openshell` in allowed binaries for that endpoint.
- Keep the scope narrow to the configured gateway host/port only.

### 6.2  Runtime Configuration

Targets:
- [`src/nemoclaw_escapades/config.py`](../src/nemoclaw_escapades/config.py)
- [`.env.example`](../.env.example)

Add optional settings such as:

- `SANDBOX_SPAWN_ENABLED=true|false`
- `OPENSHELL_GATEWAY_URL=...`
- `OPENSHELL_GATEWAY_NAME=...`
- `CHILD_SANDBOX_POLICY=...`
- `CHILD_SANDBOX_IMAGE_SOURCE=...` (for `--from`)

---

## 7  Orchestrator Changes (Minimal)

1. Add an OpenShell sandbox lifecycle tool module, e.g.
   `src/nemoclaw_escapades/tools/openshell_sandbox.py`:
   - `openshell_sandbox_create` (WRITE)
   - `openshell_sandbox_delete` (WRITE)
   - `openshell_sandbox_get` / `openshell_sandbox_list` (READ)

2. Register the toolset in `main.py` when `SANDBOX_SPAWN_ENABLED=true`.

3. Mark spawn/delete as high-risk in the approval gate so user confirmation is
   required before creating or deleting child sandboxes.

4. Emit NMB lifecycle events (`sandbox.spawn.started`, `sandbox.spawn.failed`,
   `sandbox.spawn.terminated`) for visibility and recovery.

---

## 8  Testing Plan

- **Unit tests**
  - Tool command construction and argument validation.
  - Approval classification for create/delete operations.

- **Integration tests**
  - Orchestrator sandbox launches child sandbox successfully.
  - Child connects to NMB and exchanges one request/reply cycle.
  - Failure path emits `spawn.failed` and does not leak resources.
  - Cleanup path always issues delete (normal and timeout cases).

---

## 9  Risks and Guardrails

- **Credential scope:** use dedicated gateway credentials with limited lifetime.
- **Resource leaks:** enforce TTL + watchdog cleanup for child sandboxes.
- **Name collisions:** use deterministic prefix + unique suffix per workflow.
- **Fast-moving CLI versions:** gate upgrades with a smoke test (spawn/create/get/delete)
  and support immediate rollback by setting `OPENSHELL_VERSION` explicitly.
- **Privilege creep:** no Docker socket mount; OpenShell gateway remains the
  only control-plane surface.

---

## 10  `/sandbox` PVC Mapping Semantics

When multiple OpenShell sandboxes are spawned with persistent `/sandbox`
storage, they should be treated as isolated by default:

- Each sandbox gets its own persistent volume claim / volume object.
- Sandbox A's `/sandbox` does not automatically map to the same host volume as
  sandbox B's `/sandbox`.
- Multiple PVCs may still be backed by the same physical disk pool or storage
  class, but they remain logically separate volumes.
- Shared writable state across sandboxes requires explicit configuration
  (shared mount, shared PVC, or API-based exchange such as NMB).
