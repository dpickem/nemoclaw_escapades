# Mission Control Dashboard — Design

> **Predecessor:** [Milestone 2b — Multi-Agent Orchestration: Delegation, NMB & Concurrency](design_m2b.md)
>
> **Cross-reference:** [Project design — §9 Web UI — Mission Control Dashboard](design.md#9--web-ui--mission-control-dashboard)
>
> **Last updated:** 2026-04-27

---

## Table of Contents

1. [Overview](#1--overview)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
   - [2.1 Goals](#21-goals)
   - [2.2 Non-Goals](#22-non-goals)
3. [Architecture](#3--architecture)
   - [3.1 Sandbox Topology](#31-sandbox-topology)
   - [3.2 Why a separate dashboard sandbox?](#32-why-a-separate-dashboard-sandbox)
   - [3.3 Why not have the browser talk to NMB directly?](#33-why-not-have-the-browser-talk-to-nmb-directly)
4. [Data Paths to NMB](#4--data-paths-to-nmb)
   - [4.1 Path A — Live events via pub/sub (`system.events`)](#41-path-a--live-events-via-pubsub-systemevents)
   - [4.2 Path B — Broker snapshot via `request`/`reply`](#42-path-b--broker-snapshot-via-requestreply)
   - [4.3 Path C — Historical data via the audit DB](#43-path-c--historical-data-via-the-audit-db)
5. [Sandbox & Policy Changes](#5--sandbox--policy-changes)
   - [5.1 Orchestrator sandbox: broker runs alongside the orchestrator](#51-orchestrator-sandbox-broker-runs-alongside-the-orchestrator)
   - [5.2 Dashboard sandbox: a new policy and image](#52-dashboard-sandbox-a-new-policy-and-image)
   - [5.3 Audit DB access across sandbox boundaries](#53-audit-db-access-across-sandbox-boundaries)
6. [Makefile & Tooling Changes](#6--makefile--tooling-changes)
   - [6.1 Remove `run-broker`](#61-remove-run-broker)
   - [6.2 Add `setup-dashboard-sandbox` and `run-dashboard-sandbox`](#62-add-setup-dashboard-sandbox-and-run-dashboard-sandbox)
7. [Dashboard Backend (inside the dashboard sandbox)](#7--dashboard-backend-inside-the-dashboard-sandbox)
   - [7.1 Process layout](#71-process-layout)
   - [7.2 Connection lifecycle](#72-connection-lifecycle)
   - [7.3 REST + WebSocket surface to the browser](#73-rest--websocket-surface-to-the-browser)
8. [Event Schema (`system.events`)](#8--event-schema-systemevents)
9. [Browser Reach: how the user actually opens the dashboard](#9--browser-reach-how-the-user-actually-opens-the-dashboard)
10. [Security Model](#10--security-model)
11. [Implementation Plan](#11--implementation-plan)
    - [Phase D0 — Spawn the broker as a sibling process in the sandbox image](#phase-d0--spawn-the-broker-as-a-sibling-process-in-the-sandbox-image)
    - [Phase D1 — Event schema + orchestrator emits lifecycle events](#phase-d1--event-schema--orchestrator-emits-lifecycle-events)
    - [Phase D2 — Broker `system.broker` request/reply for snapshot](#phase-d2--broker-systembroker-requestreply-for-snapshot)
    - [Phase D3 — Dashboard sandbox + minimal backend](#phase-d3--dashboard-sandbox--minimal-backend)
    - [Phase D4 — React SPA: live agent panel](#phase-d4--react-spa-live-agent-panel)
    - [Phase D5 — Audit-query NMB op + history view](#phase-d5--audit-query-nmb-op--history-view)
    - [Phase D6 — Remove `run-broker` and update docs](#phase-d6--remove-run-broker-and-update-docs)
12. [Open Questions](#12--open-questions)

---

## 1  Overview

NemoClaw is designed to run entirely inside OpenShell sandboxes —
orchestrator, sub-agents, and the **NMB broker** all live together in the
orchestrator sandbox per M2b §3.1: the broker runs as its **own
process**, sibling to the orchestrator process, **inside the orchestrator
sandbox** (M2b §3.1's diagram shows them as two distinct boxes nested
inside the OpenShell sandbox rectangle).  The supported runtime has no
host-side process; the `make run-broker` target is a developer-machine
convenience for exercising the broker outside the sandbox during M2b
development and is to be removed as part of this work, with the broker
process started inside the orchestrator sandbox by the sandbox image's
process manager instead.

This doc designs a **Mission Control web dashboard** that visually monitors
which agents are active, similar to Hermes / OpenClaw Studio — under the
constraint that **everything runs in a sandbox**, including the dashboard.

The dashboard is a **separate sandbox** that connects to the existing NMB
broker (running inside the orchestrator sandbox) the same way any sub-agent
sandbox would in M3: through the OpenShell L7 proxy on
`messages.local:9876`, governed by the existing `policies/nmb-enabled.yaml`
fragment.  No new transport, no new daemon on the host, no new trust path —
just one more NMB client identity (`sandbox_id="dashboard"`) plus a small
HTML frontend served by FastAPI inside that sandbox.

---

## 2  Goals and Non-Goals

### 2.1 Goals

1. Provide a **live agent dashboard** showing connected sandboxes, current
   tasks, recent events, and basic health — accessible in a web browser.
2. Run the dashboard backend + SPA **entirely inside an OpenShell sandbox**
   that is distinct from the orchestrator sandbox.
3. Reuse the **existing NMB broker** (running inside the orchestrator
   sandbox) without inventing a parallel admin transport.
4. Reuse the **existing audit SQLite DB** for historical views; do not
   build a second event store.
5. Keep the orchestrator sandbox's NMB surface area **agent-protocol
   only** — administrative observability is layered as new ops on the
   existing protocol, not a separate HTTP control plane.
6. Drop `make run-broker` and document that the broker is part of the
   orchestrator sandbox lifecycle.

### 2.2 Non-Goals

1. **No host-side processes.**  Neither the broker nor the dashboard
   backend runs outside an OpenShell sandbox in the supported runtime.
2. **No new wire transport.**  Dashboards use NMB just like sub-agents.
3. **No bypass of policy.**  Browser → dashboard uses the same OpenShell
   proxy / auth path as any other sandboxed HTTP service; there is no
   "host process binds 3000" shortcut.
4. **No replacement for Slack.**  Slack remains the conversational and
   notification surface (design.md §3); the dashboard is for deep
   observability.
5. **No multi-orchestrator federation.**  One orchestrator sandbox →
   one dashboard sandbox.  Multi-host federation (NMB §14) is out of
   scope here.
6. **No new auth subsystem in this milestone.**  The dashboard inherits
   whatever browser-reach mechanism the project chooses (§9 — Tailscale,
   localhost-only, OpenShell-routed); proper SSO is future work.

---

## 3  Architecture

### 3.1 Sandbox Topology

```
                 ┌────────── OpenShell L7 Proxy ──────────┐
                 │                                        │
┌──────── Orchestrator sandbox ────────┐    ┌──────── Dashboard sandbox ────────┐
│                                      │    │                                   │
│  Orchestrator process                │    │  FastAPI backend (Python)         │
│   • AgentLoop                        │    │   • NMBClient (sandbox_id =       │
│   • DelegationManager                │    │     "dashboard")                  │
│   • SlackConnector                   │    │   • subscribes system.events      │
│                                      │    │   • request/reply to broker      │
│  NMB broker process                  │    │   • audit-query proxy via NMB     │
│   • ws://0.0.0.0:9876                │    │     request/reply                 │
│   • SQLite audit DB (/sandbox/...)   │    │   • serves SPA + WS to browser    │
│                                      │    │  React SPA (built into image)     │
│  Coding sub-agent process            │    │                                   │
│   • NMBClient                        │    └─────────────┬─────────────────────┘
│                                      │                  │
└────────────────┬─────────────────────┘                  │ HTTP/WS to user's browser
                 │                                        │ (via OpenShell-routed
                 │ ws://messages.local:9876               │  endpoint or Tailscale)
                 │ (X-Sandbox-ID: dashboard)              │
                 └─────────── L7 Proxy ───────────────────┘
```

Three properties this preserves from M2b:

- The broker runs as a **separate process inside the orchestrator
  sandbox** — sibling to the orchestrator process, not embedded in
  it.  M2b §3.1's diagram makes this explicit by drawing two
  distinct boxes (`Orchestrator Process`, `NMB Broker`) nested
  inside one OpenShell sandbox rectangle.  Communication between
  the orchestrator's NMB client and the broker is the same
  WebSocket protocol every other client uses; it just happens
  over loopback.
- All NMB clients — including the dashboard — reach the broker via
  `ws://messages.local:9876`, the OpenShell proxy hostname declared
  in `policies/nmb-enabled.yaml`.
- The dashboard sandbox has **no special privilege** beyond a normal
  sub-agent's network policy.  It is just one more NMB participant
  with a reserved `sandbox_id`.

### 3.2 Why a separate dashboard sandbox?

| Reason | Detail |
|--------|--------|
| **Blast radius** | A dashboard backend renders HTML/JS, parses URLs, parses NMB query results into JSON, and (eventually) serves user-supplied content. Embedding it in the orchestrator sandbox would expand the orchestrator's attack surface for no functional benefit. |
| **Independent lifecycle** | The dashboard can be redeployed / restarted independently of the orchestrator. M2b's `make setup-sandbox` already destroys the orchestrator sandbox on every rebuild — co-locating the dashboard would force an orchestrator restart for a CSS change. |
| **Independent policy** | The dashboard needs slightly different egress (npm registry at build time; the user's browser at runtime). Keeping it in its own policy file (`policies/dashboard.yaml`) avoids relaxing the orchestrator's policy. |
| **Mirrors M3's direction** | M3 plans for sub-agents in their own sandboxes anyway. The dashboard is the first non-orchestrator sandbox; it validates the multi-sandbox NMB path that the coding agent will use in M3 (see M2b §1, scope note about M3 "spawn mechanism changes from `subprocess` to `openshell sandbox create`"). |

### 3.3 Why not have the browser talk to NMB directly?

A browser can open WebSockets, so in theory it could connect straight to
`ws://messages.local:9876`.  In practice, three properties make this
unsuitable:

| Concern | Why it doesn't work for a browser |
|---------|-----------------------------------|
| **`X-Sandbox-ID` injection** | The broker requires `X-Sandbox-ID` on the upgrade request (`broker.py`'s `_process_request`).  The browser `WebSocket` API cannot set arbitrary request headers — only sub-protocols and cookies.  A backend can. |
| **Trust path** | `policies/nmb-enabled.yaml` notes that the broker trusts `X-Sandbox-ID` because the OpenShell proxy authenticates the calling sandbox first.  A browser is not a sandbox; it has no sandbox identity to attach. |
| **Audit DB** | History views need read access to the SQLite audit DB.  Browsers can't open SQLite files; a backend can. |

So the dashboard sandbox runs a **backend** that holds the NMB
identity, and the browser only ever talks to that backend.

---

## 4  Data Paths to NMB

The dashboard backend uses **three NMB-native data paths**.  All are
read-only with respect to agent execution; nothing the dashboard
sends can drive an agent.

### 4.1 Path A — Live events via pub/sub (`system.events`)

NMB already supports `subscribe` / `publish` (broker.py
`_handle_subscribe`, `_handle_publish`).  The orchestrator and every
sub-agent emit lifecycle events to a reserved channel:

```python
# Inside the orchestrator / sub-agent code
await mb.publish("system.events", SystemEvent(
    type="agent.started",
    sandbox_id=self.sandbox_id,
    agent_type="coding",
    task_id=task_id,
    ts=time.time(),
).model_dump())
```

The dashboard backend subscribes once and fans out to every connected
browser tab:

```python
mb = MessageBus(broker_url=cfg.nmb.broker_url, sandbox_id="dashboard")
await mb.connect()
async for msg in mb.subscribe("system.events"):
    await ws_hub.broadcast_to_browsers(msg.payload)
```

This is the primary data path for "is this agent active right now?"
visualisation — push-based, low-latency, and uses the existing
protocol unchanged.

### 4.2 Path B — Broker snapshot via `request`/`reply`

Pure event subscription has a gap: a dashboard that connects after an
agent has already started will not have the corresponding
`agent.started` event in its in-memory state.  We need an authoritative
"who is connected right now?" snapshot.

The broker process already holds this data internally:
`broker.health()` returns connected sandboxes, pending requests, and
channel memberships.  Today
that method is exposed only through the broker module's `--health` CLI
flag.  We expose it on the bus by treating the broker itself as an
addressable peer:

- **Reserved sandbox identity:** the broker registers itself as
  `sandbox_id="broker"` (or `__broker__`) in its own connection map.
- **Op:** existing `request` op with `to_sandbox="broker"` and
  `type="system.broker.snapshot"`.
- **Reply:** broker handler returns `health()` as a typed reply.

```python
reply = await mb.request(
    to_sandbox="broker",
    type="system.broker.snapshot",
    payload={},
    timeout_s=2.0,
)
# reply.payload == {"connected_sandboxes": [...],
#                   "channels": {...},
#                   "num_pending_requests": ...}
```

Why this rather than a separate HTTP admin endpoint on the broker:

- It keeps the broker's external surface to a single port (9876),
  which is the only one declared in `policies/nmb-enabled.yaml`.
- It reuses the existing trust path (`X-Sandbox-ID` over the proxy)
  for ops queries; we don't need to authenticate a second channel.
- It composes with M2b's reliable-send / idempotency story (Phase 4)
  if we ever want push-on-change snapshots.

The dashboard backend polls this every 2–5 s and reconciles its
in-memory state with the snapshot, so it self-heals after a
disconnect / restart / missed event.

### 4.3 Path C — Historical data via NMB-mediated audit queries

For "what happened?" views (timelines, per-task drill-downs), the
dashboard needs read access to the **audit SQLite DB** that the
broker writes to via `AuditDB` (see
`src/nemoclaw_escapades/audit/db.py`; broker invokes it through
`broker.py:_audit`).  The DB lives at `/sandbox/audit.db` inside
the orchestrator sandbox (`Makefile:AUDIT_DB_SANDBOX`).

The natural-feeling design is "RO-mount the DB into the dashboard
sandbox so it can query the file directly."  **OpenShell does not
support that** in the deployment shape this project targets.  The
deep dive at [`docs/deep_dives/openshell_deep_dive.md`](deep_dives/openshell_deep_dive.md)
documents the cross-sandbox primitives:

- `openshell sandbox create` has no `--volume` / `--mount` flag
  for shared storage between sandboxes (deep dive §6's CLI surface,
  lines 449–478).
- `/sandbox` is a per-sandbox directory declared in the policy's
  `filesystem_policy.read_write` list (deep dive §7, lines
  512–520).  On OpenShell ≥ 0.0.22 it is backed by a Kubernetes
  PVC managed by k3s under the gateway; default k3s ships only the
  `local-path` storage class, which is `ReadWriteOnce` — the PVC
  cannot be attached to two sandboxes at the same time.
- The only documented cross-sandbox data-transfer primitives are
  `openshell sandbox upload` and `openshell sandbox download`
  (deep dive lines 467–468) — gateway-mediated file copies, used
  in this repo's `Makefile` (lines 214, 355, 396, 429) precisely
  because the audit DB cannot be mounted from outside the
  orchestrator sandbox.

So Path C uses the **NMB bus itself** as the access mechanism — the
broker (which already owns the audit DB) becomes the dashboard's
audit-query backend, exactly as it became the dashboard's
broker-state backend in §4.2.

**C1 (recommended).  Audit-query `request`/`reply` through the
broker.**  The dashboard sends typed audit queries to the broker,
which executes them against `audit.db` in its own process and
returns paged results.

```python
reply = await mb.request(
    to_sandbox="broker",
    type="system.audit.query",
    payload={
        "kind": "messages_since",
        "since_ts": 1714250000.0,
        "from_sandbox": "coding-agent-7f3a",
        "limit": 100,
    },
    timeout_s=5.0,
)
# reply.payload["rows"] -> list of audit rows as JSON
```

A small set of named query templates (`messages_since`,
`events_for_sandbox`, `task_history`) keeps the surface tight; we
do **not** ship arbitrary SQL over the wire.  Pagination is by
`since_ts` cursor, capped at, say, 500 rows per request.  The
dashboard backend caches recent results in memory for the SPA.

This path works on stock OpenShell with no infrastructure changes,
keeps the audit DB single-reader-single-writer (the broker
process), and reuses the §4.2 trust path.

**C2 (alternative).  Audit replication via NMB.**  The broker
publishes new audit rows to a `system.audit` channel; the
dashboard subscribes and maintains its own SQLite mirror inside
its `/sandbox`.  Useful if we eventually want offline / large
historical queries that would be expensive to round-trip through
the broker, or if the dashboard ever needs to outlive the
orchestrator sandbox.  More moving parts; defer until C1 is felt
to be insufficient.

**C3 (last-resort).  RWX storage class at the OpenShell layer.**
Reconfigure the gateway's k3s with an RWX storage class (NFS,
Longhorn, etc.) so the same PVC can be attached read-only to the
dashboard sandbox.  This is an infrastructure-level change that
affects every deployment of this project; not recommended unless
multi-pod RWX is being added for an unrelated reason.

**Recommendation:** ship **C1**.  It works on stock OpenShell, has
the smallest blast radius, and integrates with the existing NMB
trust path.  C2 is a documented future option; C3 is documented
for completeness only.

---

## 5  Sandbox & Policy Changes

### 5.1 Orchestrator sandbox: broker runs alongside the orchestrator

The orchestrator sandbox topology stays as M2b §3.1 specifies: the
broker runs as its **own process** inside the orchestrator sandbox,
binding `0.0.0.0:9876` on the sandbox's loopback / sandbox-internal
interface.  The orchestrator process and the broker process are
siblings under the sandbox image's process manager (concrete spawn
mechanism is M2b §3.1's call — `supervisord`, a small Python
launcher, or two `ENTRYPOINT` arguments to the same image; tracked
in §12 Q9).

The orchestrator's own NMB client connects over loopback —
`ws://localhost:9876` from inside the sandbox.  Sub-agent and
dashboard sandboxes connect via `ws://messages.local:9876` through
the OpenShell L7 proxy, exactly as today.

The one additional responsibility this design layers on the broker:
register itself as a synthetic peer (`sandbox_id="broker"`) and route
`type="system.broker.*"` requests to internal handlers (§4.2).  A few
dozen lines in `broker.py`, additive only.

### 5.2 Dashboard sandbox: a new policy and image

A new sandbox spec, parallel to the orchestrator's:

| Artefact | Path | Purpose |
|----------|------|---------|
| Policy | `policies/dashboard.yaml` | Includes `policies/nmb-enabled.yaml` (egress to `messages.local:9876`).  Adds nothing else for the MVP — no Slack, no Jira, no inference. |
| Dockerfile | `docker/Dockerfile.dashboard` | Multi-stage: stage 1 builds the React SPA (`npm run build`), stage 2 is a thin Python image with FastAPI + the built static assets. |
| Entrypoint | `python -m nemoclaw_escapades.dashboard` | New module under `src/nemoclaw_escapades/dashboard/`. |
| Sandbox name | `nemoclaw-dashboard` | Distinct from `nemoclaw-orchestrator`. |
| Sandbox identity | `sandbox_id="dashboard"` | Reserved string; broker rejects sub-agents that try to claim it (one-line check in `broker.py`). |

The dashboard policy intentionally **does not** request inference,
Slack, or any project-tooling provider.  Its purpose is to render
state, not to execute agent logic.

### 5.3 Audit DB access across sandbox boundaries

The dashboard needs read access to `/sandbox/audit.db` inside the
orchestrator sandbox (path from `Makefile:AUDIT_DB_SANDBOX`, PVC-backed
in OpenShell ≥ 0.0.22).  Two infrastructure paths exist on OpenShell;
§4.3 already picked one as the MVP:

| Option | How it works on real OpenShell | Verdict |
|--------|--------------------------------|---------|
| **5.3.a NMB-mediated audit query (§4.3 C1)** | Dashboard sends `request(to_sandbox="broker", type="system.audit.query", ...)`; broker process executes the query against its own audit DB and returns JSON rows. | **MVP.**  Works on stock OpenShell, no infra changes, smallest blast radius (only the broker process opens `audit.db`), reuses the §4.2 trust path. |
| **5.3.b NMB audit replication (§4.3 C2)** | Broker publishes each new audit row to a `system.audit` channel; dashboard maintains its own mirror SQLite under its `/sandbox`. | Future option.  Adds offline-capable history at the cost of replication state. |
| **(rejected) Shared OpenShell volume** | Attach the same `/sandbox` PVC to the dashboard sandbox read-only. | Not viable on stock OpenShell — see §4.3 for the chain of evidence (no `--volume` flag on `sandbox create`; default k3s `local-path` storage class is RWO; project's existing `Makefile` already uses `sandbox download` rather than shared mounts). Documented as §4.3 C3 with the prerequisite of an RWX storage class at the gateway level. |

The MVP plan picks **5.3.a**.  No volume mounts, no replication, no
infrastructure changes — just one more `request`/`reply` op on the
broker.

---

## 6  Makefile & Tooling Changes

### 6.1 Remove `run-broker`

The current `Makefile` declares the broker module as a top-level
constant and a host-side run target:

```70:70:Makefile
BROKER_MODULE := nemoclaw_escapades.nmb.broker
```

```245:247:Makefile
.PHONY: run-broker
run-broker: ## Run the NMB broker locally (host-side dev helper)
	PYTHONPATH=src $(CONDA_RUN) python -m $(BROKER_MODULE) --audit-db $(AUDIT_DB_LOCAL)
```

This target runs the broker on the host, which contradicts the M2b
§5.3.6 sandbox-only invariant ("dropped the bare-process 'local-dev'
runtime in favour of sandbox-only execution") and is removed as part
of this work.

The broker module's `__main__` (`broker.py:_main`) is **kept** —
that is the very entrypoint the orchestrator sandbox image invokes
to spawn the broker process alongside the orchestrator (Phase D0).
What changes is **where** it runs: inside the sandbox, started by
the image's process manager, not on the host via `make`.

After Phase D0, `BROKER_MODULE` either disappears entirely (if no
remaining Makefile target references it) or is repurposed for
sandbox-internal admin commands such as
`openshell sandbox exec nemoclaw-orchestrator -- python -m
nemoclaw_escapades.nmb.broker --health`.

### 6.2 Add `setup-dashboard-sandbox` and `run-dashboard-sandbox`

New Makefile targets, parallel in shape to `setup-sandbox` and
`run-local-sandbox`:

```makefile
.PHONY: setup-dashboard-sandbox
setup-dashboard-sandbox: gen-policy gen-config ## Build dashboard image and create dashboard sandbox
	@echo "Creating dashboard sandbox..."
	@command -v openshell >/dev/null 2>&1 && { \
		openshell sandbox delete nemoclaw-dashboard 2>/dev/null || true; \
		ln -sf docker/Dockerfile.dashboard Dockerfile; \
		openshell sandbox create \
			--name nemoclaw-dashboard \
			--from . \
			--policy policies/dashboard.yaml \
			-- python -m nemoclaw_escapades.dashboard; \
		rm -f Dockerfile; \
	} || echo "openshell CLI not found"

.PHONY: run-dashboard-sandbox
run-dashboard-sandbox: setup-gateway setup-dashboard-sandbox ## (Re)create and run the dashboard sandbox
```

`make run-local-sandbox` (orchestrator) and `make run-dashboard-sandbox`
are independent — either can be restarted without touching the other,
which is the operational benefit called out in §3.2.

---

## 7  Dashboard Backend (inside the dashboard sandbox)

### 7.1 Process layout

Single Python process, two halves:

- **Upstream half — NMB-only.**
  - `MessageBus(sandbox_id="dashboard")` permanently connected to
    `ws://messages.local:9876`.
  - One subscriber task on `system.events` populating an in-memory
    `AgentRegistry` (sandbox_id → last-known state).
  - One periodic poller task hitting `request(to_sandbox="broker",
    type="system.broker.snapshot")` every N seconds; reconciles
    the registry.
  - On-demand `request(to_sandbox="broker",
    type="system.audit.query", ...)` calls (§4.3 C1) for history
    queries when the SPA asks for them.  Results are returned
    synchronously in the request/reply round trip; the dashboard
    does not maintain its own SQLite mirror.

- **Downstream half — FastAPI app.**
  - `GET /` serves the built React SPA.
  - `GET /api/agents` returns the current `AgentRegistry` snapshot.
  - `GET /api/messages?...` proxies a `system.audit.query` call to
    the broker and translates the reply into the SPA-facing JSON
    shape.
  - `WS /api/stream` long-lived browser WebSocket; pushes a
    fan-out of every `system.events` payload as it arrives.

The dashboard sandbox holds **no** persistent state of its own —
no SQLite file, no log directory worth preserving — so a
`make run-dashboard-sandbox` recreate is fully idempotent.  The
broker remains the single source of truth.

### 7.2 Connection lifecycle

```
dashboard sandbox boot
        │
        ▼
  MessageBus.connect("dashboard")
        │   X-Sandbox-ID: dashboard
        ▼
   broker registers connection
        │
        ▼
  subscribe("system.events")  ─────────────────┐
        │                                       │
  request(to_sandbox="broker",                  │ live events
          type="system.broker.snapshot")        │ stream into
        │                                       │ AgentRegistry
        ▼                                       │
  initial reconciliation                        │
        │                                       │
        ▼                                       │
  start FastAPI uvicorn server  ◀───────────────┘
        │
        ▼
  serve browsers
```

If the broker connection drops, the existing NMB client's reconnect
logic re-establishes the WebSocket; on reconnect the dashboard
re-runs the snapshot reconciliation step rather than trusting its
local state.

### 7.3 REST + WebSocket surface to the browser

Minimal MVP API.  All endpoints live inside the dashboard sandbox;
the browser hits them through whatever reach mechanism §9 picks.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve SPA `index.html` (built into image). |
| `GET` | `/static/*` | SPA assets. |
| `GET` | `/api/agents` | Current `AgentRegistry` snapshot. |
| `GET` | `/api/agents/{sandbox_id}` | Per-agent detail (last events, current task, recent tool calls from audit DB). |
| `GET` | `/api/messages?since=...&from_sandbox=...&limit=...` | Audit-DB-backed timeline. |
| `GET` | `/api/health` | Dashboard backend liveness — does it have a live broker connection? |
| `WS`  | `/api/stream` | Live event stream; one frame per `system.events` payload. |

No `POST` / `PATCH` endpoints in the MVP.  The dashboard is read-only
with respect to agent state — it is an observer, not a controller.
Approval-gate and re-delegate UI flows are deferred until after the
read-only dashboard works (§12 Q4).

---

## 8  Event Schema (`system.events`)

Define a Pydantic model alongside `nmb/protocol.py`:

```python
# src/nemoclaw_escapades/nmb/system_events.py

class SystemEvent(BaseModel):
    """A lifecycle / observability event emitted on the
    `system.events` channel."""

    type: Literal[
        "agent.started",
        "agent.stopped",
        "task.assigned",
        "task.progress",
        "task.completed",
        "task.error",
        "approval.requested",
        "approval.resolved",
    ]
    sandbox_id: str
    ts: float                        # epoch seconds
    agent_type: str | None = None    # "orchestrator", "coding", ...
    task_id: str | None = None
    payload: dict[str, Any] = {}     # type-specific extra fields
```

Emission sites — all additive; no protocol changes to existing typed
payloads (M2b §6.3):

| Event type | Emitted by | When |
|------------|-----------|------|
| `agent.started` | every agent (orchestrator + sub-agents) | After successful `MessageBus.connect()`. |
| `agent.stopped` | every agent | Best-effort on graceful shutdown; broker also derives this from disconnect on the connection map. |
| `task.assigned` | orchestrator | After `task.assign` is sent and ack'd. |
| `task.progress` | sub-agents | At each major phase boundary in the agent loop. |
| `task.completed` | sub-agents | After `task.complete` is sent. |
| `task.error` | sub-agents | After `task.error` is sent. |
| `approval.requested` / `approval.resolved` | orchestrator | When a dangerous tool waits for / receives human approval. |

The events are intentionally redundant with the typed `task.*`
protocol payloads.  That redundancy is the point: typed payloads are
end-to-end agent contracts (M2b §6.3); `system.events` is a
broadcast mirror tailored for observability, with consistent shape
across event types so a dashboard can render them without
case-by-case knowledge of every protocol payload.

---

## 9  Browser Reach: how the user actually opens the dashboard

The dashboard backend listens on a port inside its sandbox (say
`:8080`).  How does a browser on the user's laptop reach it without
violating the "no host process" rule?  Three options in increasing
order of operational complexity:

| Option | How it works | When to pick |
|--------|--------------|--------------|
| **9.a OpenShell-routed endpoint** | OpenShell already forwards inbound HTTP for sandboxes that declare an exposed endpoint in their policy.  The user opens `http://nemoclaw-dashboard.local:8080` (or whatever the gateway publishes); routing is the gateway's job, no host-side process. | MVP and dev. |
| **9.b Tailscale sidecar in the sandbox** | The dashboard sandbox runs a `tailscaled` userspace process that joins the user's tailnet; the browser opens `http://nemoclaw-dashboard.your-tailnet.ts.net`.  Useful for mobile / off-LAN access and matches `design.md` §9.1's mobile-responsive goal. | When mobile/remote access is needed. |
| **9.c Reverse-proxy through the orchestrator gateway** | OpenShell gateway exposes a single HTTPS endpoint and routes `/dashboard/*` to the dashboard sandbox's port.  Single endpoint, single auth seam. | When we add SSO / multi-user access. |

The first two require no host process; the third is the gateway
itself doing routing (which is OpenShell's job).  None of them
re-introduce a `python` running on the host.

---

## 10  Security Model

| Concern | Mitigation |
|---------|------------|
| **Privilege creep** | Dashboard policy includes only `nmb-enabled.yaml`.  No inference, no Slack, no Jira/Gitlab providers.  No `git_clone` egress. |
| **`X-Sandbox-ID` spoofing** | Same model as M2b — the OpenShell proxy authenticates the dashboard sandbox before forwarding the WS upgrade.  Broker enforces a one-time check that the `dashboard` identity is reserved (rejects any other sandbox claiming `sandbox_id="dashboard"`). |
| **Audit-DB tamper** | The dashboard never opens `audit.db`; only the broker process does.  All audit access is through the typed `system.audit.query` request/reply, which has no write opcode. |
| **Browser → backend auth** | MVP relies on whichever §9 reach option is chosen (OpenShell-routed = LAN-trusted; Tailscale = ACL-trusted).  Real SSO is future work; called out in §12 Q3. |
| **Dashboard backend compromise** | An attacker who pops the dashboard sandbox can read agent state but cannot send agent-level control (no `task.assign` permission, broker rejects writes that don't originate from the orchestrator sandbox or the broker itself).  Worst case is information disclosure of audit history — which is exactly what the dashboard is designed to disclose, so blast-radius is bounded. |
| **Broker → broker abuse** | The new `to_sandbox="broker"` request handlers must validate the calling sandbox.  Snapshot ops are allowed for any registered peer; mutation ops (none exist today; we should keep it that way) require an explicit allowlist. |
| **Cross-sandbox file access** | Not used.  See §4.3 — OpenShell's stock surface (no `--volume` flag, RWO `local-path` storage class) doesn't support shared mounts; the design avoids them entirely. |

---

## 11  Implementation Plan

Phases are sized for "land one PR, watch CI, move on."  Each phase
is independently shippable; the dashboard becomes useful at D4 and
adds depth from there.

### Phase D0 — Spawn the broker as a sibling process in the sandbox image

Strictly an M2b cleanup, but it is a prerequisite for everything in
this design — the dashboard sandbox cannot connect to a broker that
isn't running.

The broker runs as **its own process** inside the orchestrator
sandbox, sibling to the orchestrator process (per M2b §3.1).  The
sandbox image needs a process manager that can start both:

- Update `docker/Dockerfile.orchestrator` so the container runs a
  small process manager (e.g. `supervisord`, `s6-overlay`, or a
  ~30-line Python launcher in
  `src/nemoclaw_escapades/sandbox_entrypoint.py`) that spawns:
  - `python -m nemoclaw_escapades.nmb.broker --audit-db
    /sandbox/audit.db` (broker; binds 0.0.0.0:9876 inside the
    sandbox).
  - `python -m nemoclaw_escapades.main` (orchestrator).
- The orchestrator's NMB client connects over loopback —
  `ws://localhost:9876` from inside the sandbox.  Sub-agent and
  dashboard sandboxes connect via `ws://messages.local:9876`
  through the OpenShell L7 proxy, exactly as today.
- The process manager's policy: if either child exits, terminate
  the other so OpenShell restarts the whole sandbox cleanly.  No
  partial-state operation.
- Update the `Makefile`'s `setup-sandbox` target's final
  `openshell sandbox create` invocation: today its trailing
  `-- python -m $(MAIN_MODULE)` runs only the orchestrator; replace
  with the new entrypoint that fronts the process manager.

Exit criterion: `make run-local-sandbox` brings up the orchestrator
sandbox with **two** processes — broker and orchestrator —
listening on 9876 from inside the sandbox.  M2b's existing
integration tests pass without any test-harness override.

### Phase D1 — Event schema + orchestrator emits lifecycle events

- Add `nmb/system_events.py` with the `SystemEvent` Pydantic model.
- Wire emission sites in the orchestrator and the existing M2b
  coding sub-agent (connect, task assigned, task complete/error).
- Add unit tests asserting events round-trip through a real broker
  + a test subscriber.

Exit criterion: a dummy subscriber printing `system.events` shows
correct lifecycle for every M2b integration test scenario.

### Phase D2 — Broker `system.broker` request/reply for snapshot

- Register `sandbox_id="broker"` as a synthetic peer in the broker's
  connection map.
- Add a handler for `request` ops where `to_sandbox="broker"`;
  dispatch by `type` (initially: only `system.broker.snapshot`).
- Reject duplicate `sandbox_id="broker"` claims from real clients.
- Tests: another sandbox sends `request(to_sandbox="broker", ...)`
  and gets `health()` back as `reply.payload`.

Exit criterion: an integration test connects two clients, one of
them queries `broker.snapshot`, and the response correctly lists
both sandbox IDs.

### Phase D3 — Dashboard sandbox + minimal backend

- New module `src/nemoclaw_escapades/dashboard/`:
  - `__main__.py` — uvicorn entrypoint.
  - `state.py` — `AgentRegistry`.
  - `nmb_client.py` — subscriber + snapshot poller.
  - `api.py` — FastAPI routes (`/api/agents`, `/api/health`, `WS
    /api/stream`).
- New `policies/dashboard.yaml` (includes `nmb-enabled.yaml`,
  nothing else).
- New `docker/Dockerfile.dashboard`.
- New `Makefile` targets `setup-dashboard-sandbox`,
  `run-dashboard-sandbox`.
- A minimal hardcoded HTML page proves the round trip end-to-end
  (no React yet).

Exit criterion: `make run-dashboard-sandbox` brings up the sandbox;
`curl` against the published endpoint returns a JSON list of
connected agents.

### Phase D4 — React SPA: live agent panel

- Vite + React + TailwindCSS scaffold under `frontend/`.
- One screen: an "Agent Roster" panel listing connected sandboxes
  with status pills (running / idle / error) and last-event
  timestamps.  Driven by `WS /api/stream` for liveness, with
  `GET /api/agents` as initial fetch.
- Image build adds the SPA build step (multi-stage Dockerfile).

Exit criterion: opening the dashboard URL in a browser shows a
live-updating list of orchestrator + sub-agent identities as they
join and leave.

### Phase D5 — Audit-query NMB op + history view

- Add `system.audit.query` request/reply support to the broker's
  `to_sandbox="broker"` dispatcher (extends Phase D2's handler
  table).
- Define a small set of named query templates as a Pydantic
  enum/discriminated-union: `messages_since`,
  `events_for_sandbox`, `task_history`.  No raw SQL on the wire.
- Each template maps to a parameterised SQL string in the broker
  process; results are paged (default 100 rows, max 500) and
  returned as JSON.
- Dashboard backend exposes thin `GET /api/messages` and
  `GET /api/agents/{id}/history` endpoints that translate to the
  appropriate `system.audit.query` request and unpack the reply.
- New SPA screen: per-agent timeline with task transitions and
  recent tool calls.

Exit criterion: clicking a sub-agent in the roster opens a
chronological history reconstructed from the audit DB, with no
shared volume mount and no SQLite handle in the dashboard sandbox.

### Phase D6 — Remove `run-broker` and update docs

- Delete the `run-broker` Makefile target.
- Delete the `BROKER_MODULE` constant if Phase D0 did not already
  remove its last reference.
- Delete the audit-DB-only host-side helpers (`AUDIT_DB_LOCAL`
  references that exist solely to support `run-broker` — keep the
  ones used by `setup-sandbox`'s pre-recreation save).
- Update `README.md` and `docs/design.md` §9 to point at this design
  doc as the canonical source.
- Update `docs/nmb_design.md` to reflect that the broker is
  orchestrator-sandbox-resident in the supported runtime and that
  CLI server mode is test-only.

Exit criterion: `git grep run-broker` returns 0 matches; CI green;
no surviving documentation tells users to start the broker on the
host.

---

## 12  Open Questions

| # | Question | Notes |
|---|----------|-------|
| Q1 | **Audit-query throughput.** | The MVP routes every dashboard history query through the broker process via `system.audit.query` (§4.3 C1).  If the dashboard ever drives heavy historical reads (e.g. multi-day timelines, full-text search), broker CPU contention with hot-path message routing becomes a concern.  Mitigations in order: (a) keep the named-template surface tight; (b) cache popular queries in the broker; (c) ship §4.3 C2 replication and let the dashboard query its own mirror.  Revisit only if profiling shows the broker is bottlenecked on `audit.query` work. |
| Q2 | **Where does the broker's `sandbox_id="broker"` identity live?** | We can hardcode it in `broker.py`, or carve it out as a config-file constant.  Hardcoded is simpler and harder to misconfigure. |
| Q3 | **Browser-to-backend authentication.** | MVP relies on §9.a/b (LAN/Tailscale identity).  For multi-user or internet-exposed deployments we need a real auth seam — likely OpenShell gateway-mediated SSO.  Tracked but out of scope for the MVP. |
| Q4 | **When do we add write paths (re-delegate, approve, cancel)?** | Read-only first, then approval gates (mirrors design.md §9.2.4).  Each write op needs an audit story and a "did the dashboard authenticate" decision before we add it. |
| Q5 | **One dashboard sandbox or per-orchestrator?** | Single-orchestrator deployments use one dashboard sandbox.  Multi-orchestrator is a §12-out-of-scope topology and is deferred. |
| Q6 | **Should `system.events` ever carry chain-of-thought / model output?** | Default no — it would balloon the channel and leak prompts.  If we want LLM thinking traces in the dashboard (design.md §9.2.2 "Thinking logs toggle"), pull them on-demand from the audit DB rather than streaming over `system.events`. |
| Q7 | **Snapshot poll interval.** | 2–5 s is fine for MVP.  Eventually we want push-on-change from the broker so the dashboard stops polling entirely.  Revisit after Phase D5. |
| Q8 | **Dashboard sandbox warm restart.** | OpenShell sandbox recreate on every `make run-dashboard-sandbox` is fine for dev.  For production we may want `openshell sandbox restart` semantics (preserve image, restart process); revisit when the dashboard accumulates persistent in-sandbox state (it should not). |
| Q9 | **Process manager for the orchestrator sandbox.** | The sandbox image needs to spawn two sibling processes (broker, orchestrator).  Three concrete options: (a) `supervisord` — battle-tested, slight image-size cost; (b) `s6-overlay` — minimal, popular in container images; (c) a ~30-line Python launcher in `sandbox_entrypoint.py` — no extra dependency, but we own the supervision logic.  Pick at Phase D0; option (c) is probably right for an MVP given the image already has Python. |
