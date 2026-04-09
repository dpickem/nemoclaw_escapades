# NMB Integration Tests — Design Document

> **Status:** Implemented
>
> **Last updated:** 2026-04-09
>
> **Related:**
> [NMB Design Doc](nmb_design.md) |
> [NMB Design Doc §8 — Network Policy](nmb_design.md#8--network-policy-integration) |
> [NMB Design Doc §9 — Identity & Security](nmb_design.md#9--identity--security-model) |
> [NMB Design Doc §11 — Coding + Review Loop](nmb_design.md#11--revised-coding--review-loop)

---

## Table of Contents

1. [Goals](#1--goals)
2. [Architecture](#2--architecture)
3. [Policy Model](#3--policy-model)
4. [Test Harness API](#4--test-harness-api)
5. [Test Scenarios](#5--test-scenarios)
6. [Package Layout](#6--package-layout)
7. [Running Tests](#7--running-tests)
8. [Design Decisions](#8--design-decisions)
9. [Future Extensions](#9--future-extensions)

---

## 1  Goals

The NMB integration tests validate the full message-routing stack with
multiple sandboxes operating under realistic policy constraints.  Unlike
the existing unit/component tests (`test_nmb_broker.py`,
`test_nmb_client.py`) which test individual subsystems in isolation,
the integration tests:

1. **Spin up a complete topology** — a PolicyBroker with 2–3 connected
   sandbox clients, each running a `MessageBus`.
2. **Enforce per-sandbox policies** — egress targets, ingress sources,
   channel access, and op-type restrictions — simulating the OpenShell
   network policy engine (NMB design doc §8).
3. **Exercise all op types end-to-end** — `send`/`deliver`, `request`/
   `reply`, `subscribe`/`publish`, `stream`, lifecycle events.
4. **Validate policy enforcement at the routing level** — denied egress,
   denied ingress, blocked channels, blocked ops, connection denial.
5. **Run the full coding → review → fix → approve workflow** — the
   primary multi-agent coordination pattern from NMB design doc §11.

---

## 2  Architecture

### 2.1  Test Topology

```
┌───────────────────────────────────────────────────────────┐
│                  Integration Test Process                   │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  PolicyBroker (127.0.0.1:<ephemeral>)               │  │
│  │  ┌──────────────────────────────────┐               │  │
│  │  │  Standard NMBBroker routing      │               │  │
│  │  │  + per-sandbox policy layer      │               │  │
│  │  │    • connection policy           │               │  │
│  │  │    • egress/ingress checks       │               │  │
│  │  │    • channel restrictions        │               │  │
│  │  │    • op-type restrictions        │               │  │
│  │  └──────────────────────────────────┘               │  │
│  │  Audit DB: temp SQLite (Alembic-migrated)           │  │
│  └──────────────┬──────────────┬───────────────────────┘  │
│                 │              │                           │
│       ┌─────────┘              └─────────┐                │
│       │                                  │                │
│  ┌────┴──────────────┐  ┌───────────────┴────────────┐   │
│  │  SandboxHandle    │  │  SandboxHandle              │   │
│  │  "orchestrator"   │  │  "coding-1"                 │   │
│  │  ┌──────────────┐ │  │  ┌──────────────┐           │   │
│  │  │ MessageBus   │ │  │  │ MessageBus   │           │   │
│  │  │ (async)      │ │  │  │ (async)      │           │   │
│  │  └──────────────┘ │  │  └──────────────┘           │   │
│  │  policy:          │  │  policy:                    │   │
│  │    egress→coding-1│  │    egress→orchestrator      │   │
│  │    channels: *    │  │    channels: progress.*     │   │
│  │  received: [...]  │  │  received: [...]            │   │
│  └───────────────────┘  └────────────────────────────┘   │
│                                                           │
│  (Optional third sandbox: "review-1")                     │
└───────────────────────────────────────────────────────────┘
```

Everything runs in a single asyncio event loop within the pytest
process.  No Docker, no OpenShell, no network — the broker binds to
`127.0.0.1:0` (OS picks port) and clients connect via `ws://`.

### 2.2  Production vs Test Policy Enforcement

| Dimension | Production | Integration Tests |
|-----------|-----------|-------------------|
| **Connection** | OpenShell proxy gates connectivity; only sandboxes with the NMB network policy entry can reach `messages.local` | `PolicyBroker._process_request` rejects `can_connect=False` sandboxes with HTTP 403 |
| **Identity** | Proxy injects `X-Sandbox-ID`; broker overwrites `from_sandbox` | Same — broker overwrites `from_sandbox` with the authenticated `sandbox_id` |
| **Egress** | OpenShell network policy restricts outbound connections | `PolicyBroker._enforce_policy` checks `allowed_egress_targets` |
| **Ingress** | Not natively supported (proxy is one-way) | `PolicyBroker._enforce_policy` checks `allowed_ingress_sources` |
| **Channels** | Network policy path rules (§8) | `PolicyBroker._enforce_policy` checks `allowed_channels` with wildcard support |
| **Ops** | Not natively supported | `PolicyBroker._enforce_policy` checks `allowed_ops` |

The test layer is strictly **more restrictive** than production: it
enforces ingress and op-type constraints that OpenShell does not
natively support, giving us stronger isolation guarantees during
testing.

---

## 3  Policy Model

### 3.1  SandboxPolicy

```python
@dataclass
class SandboxPolicy:
    sandbox_id: str
    can_connect: bool = True
    allowed_egress_targets: set[str] | None = None
    allowed_ingress_sources: set[str] | None = None
    allowed_channels: set[str] | None = None
    allowed_ops: set[Op] | None = None
```

`None` on any rule field means **unrestricted** for that dimension.
An empty `set()` means **nothing allowed**.

### 3.2  Channel Pattern Matching

`allowed_channels` supports three pattern types:

| Pattern | Example | Matches |
|---------|---------|---------|
| Exact | `"progress.coding-1"` | Only `progress.coding-1` |
| Wildcard suffix | `"progress.*"` | `progress.coding-1`, `progress.review-1`, etc. |
| Global wildcard | `"*"` | Any channel name |

### 3.3  Identity Model and Name Resolution

The broker uses a **single-level identity**: each connection is keyed
by a globally unique `sandbox_id` that the `MessageBus` generates at
construction time by appending a random 8-hex suffix to the
caller-supplied display name (e.g. `"orchestrator"` →
`"orchestrator-a1b2c3d4"`).  There is no separate `instance_id`.

This creates a mismatch: tests and policies use human-readable
**display names** (`"orchestrator"`, `"coding-1"`), while the broker
sees **unique sandbox_ids** (`"orchestrator-a1b2c3d4"`,
`"coding-1-f7e8d9c0"`).

The `PolicyBroker.rekey_policy()` method bridges this gap.  Called by
the harness after each `MessageBus` is constructed (and its unique
`sandbox_id` is known), it:

1. Re-indexes the policy dict from the display name to the unique
   sandbox_id.
2. Patches `allowed_egress_targets` and `allowed_ingress_sources` in
   **all other** policies so that cross-sandbox allow-lists reference
   the unique IDs the broker will actually see.

The `SandboxHandle` also carries a `_resolve` callable (provided by
the harness) that translates display names to unique sandbox_ids in
the `send`, `request`, and `stream` helpers.  This lets test code use
friendly names throughout while the wire traffic uses the unique IDs
the broker expects.

```
Test code                     Harness / PolicyBroker           Broker
─────────                     ──────────────────────           ──────
send("coding-1", ...)  ──→   _resolve("coding-1")     ──→    _connections["coding-1-f7e8"]
                              = "coding-1-f7e8"
```

### 3.4  ErrorCode: POLICY_DENIED

A new `POLICY_DENIED` error code is added to `ErrorCode` (in
`models.py`).  When the PolicyBroker denies a message, it sends an
`error` frame with this code.  The client's `_dispatch_error` routes
it to the pending ACK or request future, which raises
`NMBConnectionError`.

### 3.5  Policy Enforcement Flow

```
Client sends frame
  │
  ▼
PolicyBroker._dispatch()
  │
  ├─ Parse JSON → NMBMessage
  ├─ Set from_sandbox, timestamp
  ├─ Validate required fields
  │
  ├─ _enforce_policy()
  │    ├─ Check allowed_ops
  │    ├─ Check allowed_egress_targets  (send/request/stream)
  │    ├─ Check allowed_ingress_sources (send/request/stream)
  │    └─ Check allowed_channels        (subscribe/publish)
  │
  │  If denied → send error(POLICY_DENIED) → return
  │
  └─ Route to standard handler (inherited from NMBBroker)
```

### 3.6  Example: Orchestrator + Coding + Review Topology

```python
policies = [
    SandboxPolicy(
        sandbox_id="orchestrator",
        allowed_egress_targets={"coding-1", "review-1"},
        allowed_ingress_sources={"coding-1", "review-1"},
        allowed_channels={"progress.*", "system"},
    ),
    SandboxPolicy(
        sandbox_id="coding-1",
        allowed_egress_targets={"orchestrator"},
        allowed_ingress_sources={"orchestrator"},
        allowed_channels={"progress.coding-1", "system"},
    ),
    SandboxPolicy(
        sandbox_id="review-1",
        allowed_egress_targets={"orchestrator"},
        allowed_ingress_sources={"orchestrator"},
        allowed_channels={"progress.review-1", "system"},
    ),
]
```

This creates a **star topology** where the orchestrator is the hub.
Workers cannot communicate with each other directly — all coordination
flows through the orchestrator, matching the production deployment
model from NMB design doc §3.

---

## 4  Test Harness API

### 4.1  IntegrationHarness

Lifecycle manager for multi-sandbox tests.

```python
harness = IntegrationHarness()
await harness.start(policies=[...])       # start broker + connect sandboxes
orch = harness["orchestrator"]            # get a SandboxHandle
await orch.send("coding-1", "task.assign", {...})
await harness.stop()                       # tear down everything
```

| Method | Purpose |
|--------|---------|
| `start(policies, broker_config=)` | Start PolicyBroker, connect all `can_connect=True` sandboxes |
| `stop()` | Disconnect all sandboxes, stop broker |
| `sandbox(name)` / `[name]` | Get a SandboxHandle by sandbox_id |
| `add_sandbox(policy)` | Connect a new sandbox at runtime |
| `remove_sandbox(name)` | Disconnect and remove a sandbox |
| `broker` | Access the PolicyBroker (for health, audit queries) |
| `url` | The `ws://` URL of the running broker |

### 4.2  SandboxHandle

Per-sandbox convenience wrapper.

```python
handle = harness["coding-1"]
await handle.send("orchestrator", "task.complete", {...})
msg = await handle.wait_for_message("task.assign", timeout=5.0)
reply = await handle.request("review-1", "review.request", {...})
```

| Method | Purpose |
|--------|---------|
| `send(to, type, payload)` | Fire-and-forget send (ACK-tracked) |
| `request(to, type, payload, timeout)` | Request-reply |
| `reply(original, type, payload)` | Reply to a received request |
| `publish(channel, type, payload)` | Publish to a channel |
| `subscribe(channel)` | Subscribe (returns async iterator) |
| `stream(to, type, chunks)` | Ordered chunk streaming |
| `wait_for_message(type, timeout)` | Wait for a message in `received` |
| `messages_of_type(type)` | Filter `received` by type |
| `received` | All collected deliver messages (append-only list) |
| `close()` | Stop collecting, close connection |

The handle starts a background `_collect_loop` that reads from the
`MessageBus.listen()` queue and appends to `received`.  This runs
automatically — tests don't need to manage it.

### 4.3  Pytest Fixtures

```python
@pytest.fixture
async def harness():
    """Bare harness — call start() with custom policies."""
    h = IntegrationHarness()
    yield h
    await h.stop()

@pytest.fixture
async def two_sandbox_harness(harness):
    """Orchestrator + coding-1."""
    await harness.start([...])
    return harness

@pytest.fixture
async def three_sandbox_harness(harness):
    """Orchestrator + coding-1 + review-1."""
    await harness.start([...])
    return harness
```

---

## 5  Test Scenarios

### 5.1  Point-to-Point Send / Deliver

| Test | What it validates |
|------|-------------------|
| `test_orchestrator_sends_task_to_worker` | send → deliver with correct type, payload, from_sandbox |
| `test_worker_sends_result_to_orchestrator` | Reverse direction works |
| `test_bidirectional_exchange` | Full assign → progress → complete round-trip |
| `test_send_to_offline_allowed_target` | TARGET_OFFLINE error for offline target |
| `test_multiple_messages_arrive_in_order` | 5 sequential sends maintain order |

### 5.2  Request / Reply

| Test | What it validates |
|------|-------------------|
| `test_orchestrator_requests_review` | Full request → deliver → reply → deliver cycle |
| `test_request_reply_between_orchestrator_and_coder` | Same with coding sandbox |
| `test_request_timeout_fires` | Broker timeout frame when target doesn't reply |
| `test_concurrent_requests_to_different_targets` | Parallel requests to coding + review |

### 5.3  Pub/Sub

| Test | What it validates |
|------|-------------------|
| `test_worker_publishes_progress_to_orchestrator` | subscribe + 2 publishes |
| `test_multiple_subscribers_receive_same_message` | Fanout to 2 subscribers |
| `test_publisher_does_not_receive_own_message` | Self-exclusion |
| `test_unsubscribe_stops_delivery` | No messages after unsubscribe |

### 5.4  Streaming

| Test | What it validates |
|------|-------------------|
| `test_worker_streams_chunks_to_orchestrator` | 3 data chunks + done marker |
| `test_stream_preserves_ordering` | 20 chunks with correct seq numbers |
| `test_stream_done_has_empty_payload` | Done chunk has `payload={}` |
| `test_stream_all_chunks_share_stream_id` | One stream_id per stream |

### 5.5  Policy Enforcement

| Test | What it validates |
|------|-------------------|
| `test_egress_to_allowed_target_succeeds` | Allowed egress passes through |
| `test_egress_to_blocked_target_denied` | Denied egress → POLICY_DENIED |
| `test_egress_to_unknown_target_denied` | Unknown target → POLICY_DENIED |
| `test_ingress_from_allowed_source` | Allowed ingress passes through |
| `test_ingress_from_blocked_source` | Denied ingress → POLICY_DENIED |
| `test_subscribe_to_allowed_channel` | Allowed channel → ACK |
| `test_subscribe_to_blocked_channel_denied` | Denied channel → POLICY_DENIED |
| `test_wildcard_channel_match` | `progress.*` matches `progress.coding-1` |
| `test_publish_to_blocked_channel_denied` | Denied publish → POLICY_DENIED |
| `test_allowed_op_succeeds` | Op in allowed_ops passes |
| `test_blocked_op_denied` | Op not in allowed_ops → POLICY_DENIED |
| `test_allowed_sandbox_connects` | `can_connect=True` → success |
| `test_blocked_sandbox_cannot_connect` | `can_connect=False` → 403 → NMBConnectionError |

### 5.6  Lifecycle

| Test | What it validates |
|------|-------------------|
| `test_sandboxes_appear_in_health` | Health endpoint shows connected sandboxes |
| `test_add_sandbox_at_runtime` | Dynamic sandbox addition |
| `test_disconnect_unregisters_sandbox` | Clean removal |
| `test_system_shutdown_notification` | `sandbox.shutdown` on system channel |
| `test_reconnect_after_disconnect` | Reconnected sandbox can send/receive |
| `test_audit_records_connection_history` | Audit DB has connection + disconnect rows |

### 5.7  Full Workflow

| Test | What it validates |
|------|-------------------|
| `test_single_review_iteration` | assign → code → review → LGTM |
| `test_review_with_changes_requested` | assign → code → reject → fix → LGTM |
| `test_workflow_with_progress_streaming` | Task with pub/sub progress updates |
| `test_audit_records_all_messages` | Audit DB captures workflow messages |

---

## 6  Package Layout

```
src/nemoclaw_escapades/nmb/
├── ... (existing: broker.py, client.py, models.py, ...)
└── testing/
    ├── __init__.py              # Exports: IntegrationHarness, SandboxHandle,
    │                            #          PolicyBroker, SandboxPolicy
    ├── policy.py                # SandboxPolicy, PolicyBroker, _channel_matches
    └── harness.py               # IntegrationHarness, SandboxHandle

tests/
├── test_nmb_models.py           # Unit: wire protocol types
├── test_nmb_audit.py            # Unit: audit DB
├── test_nmb_broker.py           # Component: broker (raw websockets)
├── test_nmb_client.py           # Component: client (MessageBus)
└── integration/
    ├── __init__.py
    ├── conftest.py              # Harness fixtures (bare, 2-sandbox, 3-sandbox)
    ├── test_send_deliver.py     # Point-to-point messaging
    ├── test_request_reply.py    # Request-reply with timeouts
    ├── test_pubsub.py           # Pub/sub channels
    ├── test_streaming.py        # Ordered chunk streaming
    ├── test_policy.py           # Policy enforcement (egress, ingress, channel, op, connection)
    ├── test_lifecycle.py        # Connect, disconnect, reconnect, health
    └── test_full_workflow.py    # End-to-end coding + review loops
```

### Changes to Existing Files

| File | Change |
|------|--------|
| `models.py` | Added `ErrorCode.POLICY_DENIED` |
| `pyproject.toml` | Added `markers = ["integration: ..."]` |
| `Makefile` | Added `test-integration`, `test-all` targets; `test` now excludes integration |

---

## 7  Running Tests

```bash
# Unit + component tests only (fast, no multi-sandbox overhead)
make test

# Integration tests only
make test-integration

# Everything
make test-all

# Specific test file
PYTHONPATH=src pytest tests/integration/test_policy.py -v

# Specific test class
PYTHONPATH=src pytest tests/integration/test_full_workflow.py::TestCodingReviewLoop -v
```

---

## 8  Design Decisions

### 8.1  PolicyBroker as a Subclass (not a Proxy)

**Decision:** Implement policy enforcement by subclassing `NMBBroker`
and overriding `_dispatch` / `_process_request`.

**Alternatives considered:**
- **Proxy middleware:** A separate WebSocket proxy between clients and
  the broker.  More realistic but adds latency, complexity, and a
  second process to manage.
- **Client-side enforcement:** Wrap `MessageBus` to check policies
  before sending.  Less realistic — a misbehaving client could bypass
  it.

**Rationale:** Broker-side enforcement is the closest analogy to
production (where the OpenShell proxy sits in front of the broker).
Subclassing keeps the test infrastructure in-process and fast while
still validating the full routing path.

### 8.2  Background Collection Instead of Explicit listen()

**Decision:** `SandboxHandle` runs a background `_collect_loop` that
consumes `bus.listen()` and appends to `received`.

**Rationale:** This frees tests from manually managing listener tasks.
The `wait_for_message()` helper provides a polling-based query over
`received` with a timeout — simple, debuggable, and sufficient for
test assertions.  Tests that need subscription-based delivery use
`subscribe()` directly (the two paths are independent in the client).

### 8.3  Duplicated _dispatch Logic

**Decision:** `PolicyBroker._dispatch` duplicates the parse → validate →
route logic from `NMBBroker._dispatch` with policy checks inserted.

**Rationale:** The policy checks must happen *after* parsing and
validation but *before* handler dispatch.  There is no clean hook point
in the base class.  Duplicating ~20 lines of dispatch logic is
preferable to adding test-only hooks to production code.

### 8.4  rekey_policy for Display-Name ↔ Unique-ID Translation

**Decision:** Translate between human-readable display names and
broker-unique sandbox_ids via `PolicyBroker.rekey_policy()` (rewrites
the policy dict keys and cross-references) plus a client-side
`_resolve` callable in `SandboxHandle`.

**Alternatives considered:**
- **Broker-side prefix matching:** The broker could resolve `"coding-1"`
  to `"coding-1-abc12345"` by scanning `_connections` for a prefix
  match.  Fragile (e.g. `"coding"` would false-match `"coding-1-…"`)
  and adds O(n) overhead to every route.
- **Two-level identity map:** Restore the old `_name_to_instance` /
  `_instance_names` dicts from a prior broker revision.  Adds
  complexity to production code for a test-only need.

**Rationale:** `rekey_policy` is a one-time O(p²) rewrite at harness
startup (where p = number of policies, typically 2–3).  After that,
all lookups are O(1) dict hits with no production code changes.  The
client-side `_resolve` in `SandboxHandle` means test code never sees
the random suffixes.

### 8.5  _process_request Override (Static → Instance Method)

**Decision:** Override `NMBBroker._process_request` (a `@staticmethod`)
as an instance method on `PolicyBroker`.

**Rationale:** The parent's `start()` passes `self._process_request` to
`websockets.serve()`.  For a static method, Python returns the bare
function; for an instance method, it returns a bound method.  Both are
valid callables with `(connection, request)` arity.  This lets the
policy layer access `self._policies` without modifying `start()`.

---

## 9  Future Extensions

| Extension | Description |
|-----------|-------------|
| **Rate-limit testing** | Add `max_messages_per_second` to `SandboxPolicy` and enforce in `_enforce_policy`. |
| **Payload-size policy** | Per-sandbox max payload size (more granular than the broker's global limit). |
| **Dynamic policy updates** | `harness.update_policy(sandbox_id, new_policy)` to change rules mid-test. |
| **Latency injection** | Configurable delays in PolicyBroker routing to simulate cross-host latency. |
| **Multi-broker topologies** | Two PolicyBrokers peered together to test multi-host scenarios. |
| **Chaos testing** | Random connection drops, message corruption, and reordering. |
| **Performance benchmarks** | Throughput and latency measurements under load with policy enforcement. |
| **TLS/auth testing** | When multi-host TLS/token/mTLS is implemented (§13.3), test it in the harness. |
