# Agent Trace Design

> **Status:** Proposed
>
> **Last updated:** 2026-04-12
>
> **Related:**
> [Audit DB Design](audit_db_design.md) |
> [Inference Call Auditing](inference_call_auditing_design.md) |
> [NMB Design](nmb_design.md) |
> [Design M2 §12](design_m2.md#12--audit-and-observability) |
> [Training Flywheel §4](training_flywheel_deep_dive.md#4--data-capture-layers)

---

## Table of Contents

1. [Motivation](#1--motivation)
2. [Trace Event Taxonomy](#2--trace-event-taxonomy)
3. [Primary Event Types (Implemented / In Design)](#3--primary-event-types-implemented--in-design)
4. [Secondary Event Types (Not Yet Captured)](#4--secondary-event-types-not-yet-captured)
5. [Correlation Model](#5--correlation-model)
6. [Reconstructing a Full Trace](#6--reconstructing-a-full-trace)
7. [Gaps and Future Work](#7--gaps-and-future-work)

---

## 1  Motivation

Debugging, cost accounting, and training-data extraction all require the
ability to reconstruct **what the agent did** for a given user request — from
initial message through delegation, inference, tool execution, and final
reply.  The audit DB stores full content for every event (complete prompt
message arrays, model responses, tool arguments and outputs, NMB message
payloads), making traces usable not just for debugging but also as direct
input to the training flywheel.  Today this information is spread across
multiple storage backends with no unified trace model.

This document defines what a "complete agent trace" means, catalogues every
event type that contributes to it, identifies which are already captured in
queryable form and which are not, and proposes a roadmap for closing the gaps.

---

## 2  Trace Event Taxonomy

A single user request can produce events at five levels of granularity:

```
User message
 └─ Session (1 per request)
     ├─ Inference call          ← LLM round-trip
     │   └─ Tool calls (0..N)  ← side-effecting actions from that round
     ├─ Approval event          ← human-in-the-loop gate
     ├─ Inference call           (next round, after tool results fed back)
     │   └─ Tool calls (0..N)
     ├─ Delegation              ← orchestrator → sub-agent handoff
     │   └─ Sub-agent session
     │       ├─ Inference call
     │       │   └─ Tool calls
     │       ├─ Inference call
     │       │   └─ Tool calls
     │       └─ ...
     ├─ NMB messages            ← inter-sandbox routing envelopes
     └─ Final text response
```

Each level maps to a concrete event type described below.

---

## 3  Primary Event Types (Implemented / In Design)

These three types cover the agent's external interactions and are either
already persisted in the audit DB or have approved designs.

### 3.1  Tool Calls

| | |
|---|---|
| **What** | Every tool invocation (service, args, latency, success, response) |
| **Storage** | `tool_calls` table in `/sandbox/audit.db` |
| **Status** | Implemented for orchestrator; NMB-batched flush for sub-agents in design ([Audit DB Design §7](audit_db_design.md#7--open-question-sub-agent-tool-call-auditing)) |
| **Delivery** | Orchestrator: direct write. Sub-agent: NMB `audit.flush` batch + JSONL fallback. |

### 3.2  Inference Calls

| | |
|---|---|
| **What** | Every LLM round-trip: metadata (model, tokens, latency, finish reason, tool-call count) **plus full request and response payloads** (the complete message array sent to the model and the full assistant response) |
| **Storage** | `inference_calls` table in `/sandbox/audit.db` |
| **Status** | Design complete ([Inference Call Auditing](inference_call_auditing_design.md)) |
| **Delivery** | Same mechanism as tool calls: direct write + NMB batch flush. |
| **Payloads** | Controlled by `AUDIT_PERSIST_PAYLOADS` (default `true`). When enabled, `request_payload` stores the full JSON message array and `response_payload` stores the raw API response. `*_payload_size` columns are always recorded. See [Inference Call Auditing §6](inference_call_auditing_design.md#6--prompt-and-completion-storage) for storage estimates and PII handling. |

### 3.3  NMB Messages

| | |
|---|---|
| **What** | Every inter-sandbox message (send, request, reply, publish, stream) + connection lifecycle |
| **Storage** | `messages` and `connections` tables in the same audit DB (`~/.nemoclaw/audit.db`) |
| **Status** | Implemented ([NMB Design](nmb_design.md)) |
| **Delivery** | Broker writes directly on every routed message. |

---

## 4  Secondary Event Types (Not Yet Captured)

The primary types cover what the agent *did* (called an LLM, ran a tool,
sent a message).  The following events capture **why the agent paused, how
rounds relate to each other, and what context shaped each decision**.  None
are persisted in queryable form today.

### 4.1  Approval Gate Events

When a write tool is blocked by the `ApprovalGate`, the agent loop pauses and
waits for human input.  The user eventually clicks Approve or Deny.  This
creates a gap in the trace: the `tool_calls` row has an `approval_status`
column, but there is no discrete event for:

- The moment the loop paused (with the proposed tool calls and their
  arguments).
- The user's decision (who approved, when, from which device/session).
- The wall-clock wait time between pause and resume.

For trace reconstruction this means you can see "tool X was approved and took
340 ms to execute" but not "the agent was idle for 47 seconds waiting for the
user to click Approve."

**Proposed capture:** An `approval_events` table or additional columns on the
existing `tool_calls` row:

| Column | Description |
|--------|-------------|
| `requested_at` | Timestamp when the approval prompt was sent |
| `decided_at` | Timestamp when Approve/Deny was received |
| `decision` | `approved` or `denied` |
| `decided_by` | User ID of the person who clicked |
| `wait_ms` | `decided_at - requested_at` in milliseconds |

**Priority:** Medium.  Important for understanding end-to-end latency in
interactive sessions and for auditing who authorized write operations.

### 4.2  Continuation / Retry Events

When `finish_reason="length"` triggers a continuation retry, each retry is a
separate `backend.complete()` call and *will* get its own `inference_calls`
row.  However, the trace currently cannot distinguish:

- A normal agent-loop round (model chose to call tools, got results, continued)
  from a continuation retry (model ran out of output tokens, got re-prompted
  with "please continue").
- The original truncated response from its continuation chunks.

**Proposed capture:** Add an `inference_type` column to `inference_calls`:

| Value | Meaning |
|-------|---------|
| `agent_loop` | Normal multi-turn round in the agent loop |
| `continuation` | `finish_reason=length` retry via `_continue_truncated` |
| `repair` | Transcript repair re-prompt (empty reply, content filter) |
| `no_tools` | Single-shot inference via `_inference_with_repair` |

Plus a nullable `continuation_of` column pointing to the `id` of the
truncated inference call that triggered the retry.

**Priority:** Low.  Useful for debugging "why did this session use so many
tokens?" but not blocking for core trace completeness.

### 4.3  Delegation Events

When the orchestrator spawns a sub-agent, the parent-child relationship is
implicit: you can correlate it by matching a `delegate_task` tool call in
`tool_calls` with a `task.assign` NMB message in `messages`, then matching the
child's `source_sandbox` on subsequent `tool_calls` / `inference_calls` rows.
But this requires multi-table joins with timestamp heuristics.

**Proposed capture:** A `delegations` table or a first-class relationship:

| Column | Description |
|--------|-------------|
| `id` | Delegation UUID |
| `parent_session_id` | Orchestrator's `session_id` |
| `child_session_id` | Sub-agent's `session_id` (from `task.assign`) |
| `child_sandbox_id` | OpenShell sandbox ID |
| `task_prompt` | The prompt sent to the sub-agent (truncated) |
| `status` | `running`, `completed`, `failed`, `timed_out` |
| `created_at` | When `task.assign` was sent |
| `completed_at` | When `task.complete` was received |

**Priority:** Medium-high for multi-agent M2 workflows.  Without this, tracing
a request that fans out to three sub-agents requires manual NMB message
archaeology.

### 4.4  Prompt Context Metadata

**Largely addressed.**  The `inference_calls` table now stores the full
request payload (`request_payload` column) when `AUDIT_PERSIST_PAYLOADS` is
enabled (see [Inference Call Auditing §6](inference_call_auditing_design.md#6--prompt-and-completion-storage)).
This means the exact message array the model saw — system prompt, thread
history, tool results — is recorded for every round.

What remains uncaptured is **structured metadata about prompt composition**:
which system prompt version was used, how many history messages were included
before the cap, which context files were attached.  This is derivable from the
stored `request_payload` by parsing the
message array, but a first-class decomposition would make analysis easier.

**Proposed capture (future):** Additional columns or a JSON metadata field on
`inference_calls`:

| Field | Description |
|-------|-------------|
| `system_prompt_hash` | SHA-256 of the system prompt (for version tracking / dedup) |
| `history_message_count` | Number of conversation history messages included |
| `context_file_count` | Number of context files attached |

**Priority:** Low.  The raw payload covers training needs.  Structured
decomposition is a quality-of-life improvement for prompt debugging.

---

## 5  Correlation Model

All trace events are joined by two keys:

| Key | Scope | Source |
|-----|-------|--------|
| `session_id` | All events within a single user request (including sub-agent events if the orchestrator propagates it via `task.assign`) | `request_id` on `NormalizedRequest` |
| `source_sandbox` | Distinguishes orchestrator-local events (`NULL`) from sub-agent events (child's `sandbox_id`) | NMB `from_sandbox` on `audit.flush` |

Within a session, events are ordered by `timestamp`.  The `round` column on
`inference_calls` provides an additional ordering key within a single agent
loop.

All tables live in a single SQLite database (`~/.nemoclaw/audit.db` locally,
`/sandbox/audit.db` in OpenShell), so joins between `inference_calls`,
`tool_calls`, and `messages` are straightforward SQL — no cross-database
merging required.  The NMB broker's `messages.id` can also correlate with
specific `task.assign` / `task.complete` envelopes.

---

## 6  Reconstructing a Full Trace

### 6.1  SQL: Interleaved Event Timeline

```sql
SELECT 'inference'  AS event_type, timestamp, session_id, source_sandbox,
       model, round, latency_ms, prompt_tokens, completion_tokens,
       NULL AS service, NULL AS command, NULL AS success
FROM   inference_calls
WHERE  session_id = :sid

UNION ALL

SELECT 'tool_call'  AS event_type, timestamp, session_id, NULL,
       NULL, NULL, duration_ms, NULL, NULL,
       service, command, success
FROM   tool_calls
WHERE  session_id = :sid

ORDER BY timestamp;
```

To include NMB messages, add a third `UNION ALL` arm against the `messages`
table in the same database.

### 6.2  Example Trace Output

```
 #  timestamp       event_type  round  model                       service         command          ms     tokens
 1  1744345200.001  inference   0      meta/llama-3.3-70b-instruct                                 1820   2048+312
 2  1744345201.823  tool_call                                       jira            jira_search      342
 3  1744345202.170  tool_call                                       gitlab          gitlab_get_mr    127
 4  1744345202.301  inference   1      meta/llama-3.3-70b-instruct                                 2105   3512+580
 5  1744345204.410  tool_call                                       gerrit          gerrit_get_cl    289
 6  1744345204.702  inference   2      meta/llama-3.3-70b-instruct                                 1540   4200+410
```

Row 1 is the first inference round; it returned two tool calls (rows 2-3).
After feeding tool results back, round 1 (row 4) returned one more tool call
(row 5).  Round 2 (row 6) produced the final text response
(`finish_reason=stop`, `has_tool_calls=0`).

---

## 7  Gaps and Future Work

Summary of what is and isn't captured, with recommended priorities:

| Event type | Queryable today? | Design exists? | Priority | Target |
|-----------|------------------|----------------|----------|--------|
| Tool calls (metadata + response payload) | Yes (orchestrator) | Yes (sub-agent: [Audit DB §7](audit_db_design.md#7--open-question-sub-agent-tool-call-auditing)) | — | M2 |
| Inference calls (metadata + full payloads) | No | Yes ([Inference Call Auditing](inference_call_auditing_design.md)) | High | M2 |
| NMB messages (full payloads) | Yes | Implemented | — | M1 |
| Approval events | Partial (`approval_status` on `tool_calls`) | No | Medium | M3 |
| Delegation events | No (implicit via NMB joins) | No | Medium-high | M2/M3 |
| Continuation/retry metadata | No | No | Low | M3 |
| Prompt context metadata | Partially (raw payload stored; structured decomposition not) | No | Low | M3+ |

The first three give a **complete trace with full content**: every LLM
round-trip (including the exact prompt sent and completion received), every
tool execution (with response payloads), and every inter-sandbox message (with
full payloads).  This is sufficient for operational debugging, cost
attribution, **and** training-data extraction — the training flywheel can
construct SFT pairs and DPO preference data directly from the stored payloads.
The remaining four add fidelity (human-in-the-loop timing, parent-child
relationships, retry semantics, structured prompt decomposition) but are not
required for core trace completeness.
