# Audit Database Design

> **Status:** Implemented (orchestrator); Proposed (sub-agent)
>
> **Last updated:** 2026-04-11
>
> **Related:**
> [NMB Design](nmb_design.md) |
> [Training Flywheel §4](training_flywheel_deep_dive.md#4--data-capture-layers) |
> [Sandbox Spawn Design](sandbox_spawn_design.md) |
> [Inference Call Auditing](inference_call_auditing_design.md) |
> [Agent Trace Design](agent_trace_design.md)

---

## Table of Contents

1. [Purpose](#1--purpose)
2. [Architecture](#2--architecture)
3. [What Gets Recorded](#3--what-gets-recorded)
4. [Lifecycle](#4--lifecycle)
5. [Storage and Persistence](#5--storage-and-persistence)
6. [Makefile Targets](#6--makefile-targets)
7. [Sub-Agent Tool-Call Auditing](#7--sub-agent-tool-call-auditing)
8. [Relationship to Other Audit Surfaces](#8--relationship-to-other-audit-surfaces)

---

## 1  Purpose

The audit database records every tool invocation that passes through the
orchestrator's agent loop.  It serves two roles:

1. **Operational debugging** — after-the-fact investigation of what tools were
   called, with what arguments, how long they took, and whether they succeeded.
2. **Training data** — the tool-call records (service, args, response, latency,
   success) are a direct source for fine-tuning tool-use models via the training
   flywheel pipeline.

The DB is a single SQLite file managed by Alembic migrations.  Both the
orchestrator and the NMB broker write to the same database — the broker
populates the `messages` and `connections` tables, while the orchestrator
populates `tool_calls` (and, once implemented, `inference_calls`).

---

## 2  Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Orchestrator Sandbox                                    │
│                                                          │
│  ┌──────────┐    log_tool_call()    ┌──────────────────┐ │
│  │Orchestr- │ ─────────────────────▶│  AuditDB         │ │
│  │ator loop │                       │  (async SQLite)  │ │
│  │          │  every tool exec:     │                  │ │
│  │  model   │  service, command,    │  /sandbox/       │ │
│  │  ↕ tools │  args, duration,     │    audit.db      │ │
│  └──────────┘  success, payload     └────────┬─────────┘ │
│                                              │           │
└──────────────────────────────────────────────┼───────────┘
                                               │ PVC mount
                                     ┌─────────▼──────────┐
                                     │  Host filesystem    │
                                     │  (k3s PV storage)   │
                                     └─────────┬──────────┘
                                               │ make audit-download
                                     ┌─────────▼──────────┐
                                     │  ~/.nemoclaw/       │
                                     │    audit.db         │
                                     │  (durable host      │
                                     │   copy)             │
                                     └────────────────────┘
```

---

## 3  What Gets Recorded

Every tool invocation writes one row to the `tool_calls` table.
Orchestrator-local calls are written directly by
`Orchestrator._execute_tool_calls()`.  Sub-agent calls arrive via the
NMB-batched flush path (Option C in §7): child sandboxes buffer tool-call
records in memory and flush them over NMB in batches; the orchestrator
deserializes each batch and writes the rows to the same table, tagged with a
`source_sandbox` column.  If NMB is unavailable the child falls back to a
local JSONL file that the orchestrator picks up at task completion.

| Column | Source | Example |
|--------|--------|---------|
| `id` | Generated UUID (16 hex chars) | `a3f7b2c8e1d04f69` |
| `timestamp` | `time.time()` at insert | `1744345200.0` |
| `session_id` | `request_id` from the orchestrator | `req-abc123` |
| `thread_ts` | Slack thread timestamp | `1744345190.000100` |
| `service` | `ToolSpec.toolset` | `jira`, `gitlab`, `gerrit` |
| `command` | `ToolSpec.name` | `jira_search`, `gitlab_get_mr` |
| `args` | Raw JSON arguments from the model | `{"query": "..."}` |
| `operation_type` | `READ` or `WRITE` (from `ToolSpec.is_read_only`) | `READ` |
| `approval_status` | Reserved for approval gate integration | `null` |
| `duration_ms` | Wall-clock tool execution time | `342.1` |
| `success` | `1` or `0` | `1` |
| `error_message` | Exception string on failure | `null` |
| `response_payload` | Full tool output (when `persist_payloads=True`) | `{"issues": [...]}` |
| `payload_size` | Original payload bytes (always stored) | `4096` |

The `response_payload` column is optional — set `AUDIT_PERSIST_PAYLOADS=false`
to store only metadata.  `payload_size` is always recorded regardless.

---

## 4  Lifecycle

### 4.1  Startup (`main.py`)

```python
audit = AuditDB(audit_path, persist_payloads=config.audit.persist_payloads)
await audit.open()                    # Alembic migrations + engine
await audit.start_background_writer() # async batch-commit task
```

### 4.2  Runtime (`orchestrator.py`)

After each tool call in `_execute_tool_calls()`:

```python
await self._audit.log_tool_call(
    session_id=request_id, thread_ts=thread_ts,
    service=spec.toolset, command=tc.name, args=tc.arguments,
    operation_type="READ" if spec.is_read_only else "WRITE",
    duration_ms=round(tool_timer.ms, 1), success=success,
    error_message=error_msg, response_payload=output,
)
```

Audit failures are caught and logged at DEBUG — they never block the agent
loop.

### 4.3  Shutdown (`main.py`)

```python
await audit.stop_background_writer()  # drain queue, final flush
await audit.close()                   # dispose engine
```

---

## 5  Storage and Persistence

| Environment | Path | Persistence |
|-------------|------|-------------|
| Sandbox (OpenShell >= 0.0.22) | `/sandbox/audit.db` | PVC-backed; survives gateway/host restarts. Lost on `sandbox delete` or `gw destroy`. |
| Local dev | `~/.nemoclaw/audit.db` | Host filesystem; always durable. |

The Makefile provides safeguards against data loss:

- `setup-sandbox` and `clean` both download the sandbox DB to
  `~/.nemoclaw/audit.db` before destroying the sandbox.
- `audit-download` does an on-demand copy.
- `audit-sync` runs a continuous background loop (default: every 60 s).

### Configuration

| Env var | Default (sandbox) | Default (local) |
|---------|-------------------|-----------------|
| `AUDIT_ENABLED` | `true` | `true` |
| `AUDIT_DB_PATH` | `/sandbox/audit.db` | `~/.nemoclaw/audit.db` |
| `AUDIT_PERSIST_PAYLOADS` | `true` | `true` |

---

## 6  Makefile Targets

| Target | Description |
|--------|-------------|
| `audit-download` | Pull sandbox DB → `~/.nemoclaw/audit.db` |
| `audit-stats` | Row counts + last 5 tool calls |
| `audit-query SQL="..."` | Ad-hoc SQL against host copy |
| `audit-export` | JSONL export via Python API |
| `audit-sync` | Background loop, downloads every 60 s |

---

## 7  Sub-Agent Tool-Call Auditing

### 7.1  Problem

When the orchestrator delegates work to child sandboxes (see
[Sandbox Spawn Design](sandbox_spawn_design.md)), those sandboxes run their own
agent loops with their own tool calls.  Today, only the orchestrator's tool
calls are audited.  Sub-agent tool calls are invisible to the audit DB.

### 7.2  Design Options

#### Option A: NMB-Relayed Audit Events

Sub-agents publish `tool.executed` events to an NMB channel.  The orchestrator
(or the NMB broker) subscribes and writes them to the audit DB.

```
Child sandbox                NMB broker              Orchestrator
     │                           │                        │
     │  tool call completes      │                        │
     │──publish(tool.executed)──▶│                        │
     │                           │──deliver──────────────▶│
     │                           │                 log_tool_call()
     │                           │                        │
```

**Pros:** Centralized DB; one schema; broker identity guarantees `from_sandbox`
is trustworthy; no direct DB access from child sandboxes.

**Cons:** Adds latency between execution and persistence; requires NMB
connectivity (if the child disconnects before the event is delivered, the
record is lost); adds a new message type to the NMB protocol.

#### Option B: Per-Sandbox Local Audit DB + Post-Hoc Merge

Each child sandbox writes its own `/sandbox/audit.db`.  On task completion the
orchestrator downloads it (same `openshell sandbox download` pattern) and
merges rows into the central DB.

```
Child sandbox                                   Orchestrator
     │                                               │
     │  tool call → local audit.db                   │
     │  ...task completes...                         │
     │                                               │
     │◀─────── openshell sandbox download ───────────│
     │         /sandbox/audit.db                     │
     │                                    merge into central DB
```

**Pros:** No runtime dependency on NMB for auditing; child sandbox can be fully
offline; no new NMB message types; audit data survives NMB disconnects.

**Cons:** Merge step required (dedup by `id`, resolve conflicts); data only
arrives after task completion (not real-time); child sandbox must include the
audit DB schema and Alembic migrations in its image.

#### ~~Option C (original): Broker-Side Audit Interception~~ (rejected)

The NMB broker already audits every routed message.  The original idea was to
model tool calls as NMB `request`/`reply` pairs so the broker captures them
automatically.

**Why this doesn't work:** Child sandboxes run their own multi-turn agent loops.
A single delegated task might involve 10+ internal tool calls (inference →
tool → inference → tool → ...) before producing a final `task.complete` reply.
The NMB broker only sees the outer envelope — one `task.assign` request and one
`task.complete` reply — not the individual tool invocations inside the child's
loop.  This makes it fundamentally unsuitable for tool-call-level auditing.

The NMB broker audit remains valuable for *inter-sandbox* message tracing
(delegation patterns, latency, failure rates), but it operates at a different
granularity than tool-call auditing.

#### Option C: NMB-Batched Audit Flush

The child accumulates tool-call records in a local in-memory buffer and flushes
them to the orchestrator over NMB in batches at natural boundaries: after each
agent-loop round, on task completion, or when the buffer hits a size threshold.
The orchestrator receives the batch and writes it to the central audit DB.

```
Child sandbox (multi-turn loop)              NMB            Orchestrator
     │                                        │                  │
     │  tool call #1 → buffer                 │                  │
     │  tool call #2 → buffer                 │                  │
     │  tool call #3 → buffer                 │                  │
     │  ...round ends or buffer full...       │                  │
     │                                        │                  │
     │──send(audit.flush, [tc1,tc2,tc3])────▶│                  │
     │                                        │──deliver────────▶│
     │                                        │          bulk log_tool_call()
     │  ...next round...                      │                  │
     │  tool call #4 → buffer                 │                  │
     │  ...task completes...                  │                  │
     │                                        │                  │
     │──send(audit.flush, [tc4])────────────▶│                  │
     │──reply(task.complete, result)─────────▶│                  │
     │                                        │──deliver────────▶│
```

**Pros:** Centralized DB — no merge step, no file download, no schema
duplication in the child image.  Near-real-time (batched per round, not
per task).  Uses existing NMB transport; broker identity guarantees
`from_sandbox` provenance.  Amortizes NMB overhead across many tool calls.
Child doesn't need Alembic, SQLAlchemy, or aiosqlite — just a list buffer
and one NMB `send` call per flush.

**Cons:** Requires NMB connectivity for audit delivery (tool calls executed
during a disconnect are lost unless the child also writes a local fallback).
Adds a new `audit.flush` message type convention.  Orchestrator must handle
the ingest path (deserialize the batch, write rows).

**Fallback for disconnects:** The child can optionally write a local JSONL
file as a fallback buffer.  On task completion, the orchestrator downloads
any remaining JSONL via `openshell sandbox download` — same pattern as
Option B but with a lightweight flat file instead of a full SQLite DB.

### 7.3  Recommendation

**Option C (NMB-batched flush) for the primary path, with JSONL fallback.**

Option C is the best fit for the architecture:

- **No schema coupling** — the child sandbox doesn't need Alembic, SQLAlchemy,
  or the audit module.  It just serializes tool-call records as JSON dicts and
  sends them over NMB.  This keeps child sandbox images lighter and avoids
  version-skew issues with the audit schema.
- **Near-real-time** — the orchestrator sees tool-call data per agent-loop
  round, not just at task completion.  This enables live cost tracking,
  progress dashboards, and early-abort decisions.
- **Centralized writes** — one DB, one writer, no merge/dedup logic.
- **Graceful degradation** — if the child loses NMB connectivity mid-task,
  it falls back to appending JSONL locally.  The orchestrator picks up the
  file at task completion via `openshell sandbox download`.

Option B (full local SQLite + post-hoc merge) is the fallback for environments
where NMB is unavailable or child sandboxes are fully offline.

### 7.4  MVP Implementation Sketch (Option C)

**Child side (lightweight — no audit module dependency):**

1. Tool-call records accumulate in a list buffer as plain dicts:
   ```python
   audit_buffer: list[dict] = []

   # After each tool call:
   audit_buffer.append({
       "id": uuid4().hex[:16],
       "timestamp": time.time(),
       "service": spec.toolset,
       "command": tc.name,
       "args": tc.arguments,
       "operation_type": "READ" if spec.is_read_only else "WRITE",
       "duration_ms": round(timer.ms, 1),
       "success": True,
       "response_payload": output,
   })
   ```

2. At round boundaries or task completion, flush over NMB:
   ```python
   await nmb.send(
       to="orchestrator",
       type="audit.flush",
       payload={"tool_calls": audit_buffer},
   )
   audit_buffer.clear()
   ```

3. Fallback: if the NMB send fails, append to `/sandbox/audit_fallback.jsonl`.

**Orchestrator side:**

1. Subscribe to `audit.flush` messages (or handle inline in the task
   message handler).

2. On receipt, iterate the `tool_calls` array and call `log_tool_call()` for
   each, adding the `source_sandbox` from the NMB `from_sandbox` field.

3. On task completion, check for any remaining fallback file:
   ```python
   openshell sandbox download <child> /sandbox/audit_fallback.jsonl /tmp/...
   # Parse JSONL, call log_tool_call() for each line
   ```

**Schema change:** Add an optional `source_sandbox` column to `tool_calls`
(nullable, defaults to `NULL` for orchestrator-local calls).  Populated with
the child's `sandbox_id` for sub-agent records.

---

## 8  Relationship to Other Audit Surfaces

| Surface | What it captures | Table(s) |
|---------|------------------|----------|
| **Audit DB — `tool_calls`** (this doc) | Tool invocations (service, args, latency, success, response payload) | `tool_calls` |
| **Audit DB — `inference_calls`** ([design](inference_call_auditing_design.md)) | LLM round-trips (model, tokens, latency, finish reason, full request/response payloads) | `inference_calls` |
| **Audit DB — NMB messages** | All routed NMB messages (send, request, reply, publish) + connections | `messages`, `connections` |
| **Structured JSON logs** | Free-form operational logs (errors, retries, debug) | stderr / log file |
| **OpenShell proxy logs** | Every HTTP request through the L7 proxy (host, method, path, policy decision) | `openshell logs` |

All three primary trace tables (`tool_calls`, `inference_calls`, `messages`)
live in a single Alembic-managed SQLite database — `/sandbox/audit.db` in
OpenShell, `~/.nemoclaw/audit.db` locally.  Both the orchestrator and the NMB
broker write to the same file.  This means trace queries can join across all
three tables directly in SQL without cross-database merging.

Together they give a complete agent trace with full content — every LLM
round-trip, every tool execution, and every inter-sandbox message, all
correlated by `session_id` and timestamp.  See
[Agent Trace Design](agent_trace_design.md) for the full trace event taxonomy,
including secondary event types (approval events, delegation tracking,
continuation metadata) that add fidelity beyond these three primary types.

The training flywheel pipeline reads from this single DB, joining by
`session_id` / NMB `id` correlation (see
[Training Flywheel §4](training_flywheel_deep_dive.md#4--data-capture-layers)).
