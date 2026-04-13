# Inference Call Auditing Design

> **Status:** Proposed
>
> **Last updated:** 2026-04-12
>
> **Related:**
> [Audit DB Design](audit_db_design.md) |
> [Agent Trace Design](agent_trace_design.md) |
> [Design M2 §12](design_m2.md#12--audit-and-observability) |
> [Training Flywheel §4](training_flywheel_deep_dive.md#4--data-capture-layers)

---

## Table of Contents

1. [Problem](#1--problem)
2. [Goals and Non-Goals](#2--goals-and-non-goals)
3. [Schema: `inference_calls` Table](#3--schema-inference_calls-table)
4. [Recording Mechanism](#4--recording-mechanism)
5. [AuditDB API Addition](#5--auditdb-api-addition)
6. [Prompt and Completion Storage](#6--prompt-and-completion-storage)
7. [Useful Queries](#7--useful-queries)
8. [Migration: `005_inference_calls.py`](#8--migration-005_inference_callspy)
9. [Makefile Additions](#9--makefile-additions)
10. [Relationship to Existing Audit Surfaces](#10--relationship-to-existing-audit-surfaces)

---

## 1  Problem

A functionally complete agent trace requires three primary event types:
**NMB messages** (inter-sandbox routing), **tool calls** (side-effecting
actions), and **inference calls** (LLM round-trips).  See
[Agent Trace Design](agent_trace_design.md) for the full taxonomy, including
secondary event types that add further fidelity.  The audit DB already
captures the first two.  Inference calls are currently logged only as
unstructured JSON on stderr, which means:

- There is no queryable record of per-round token usage, latency, or model
  identity — cost attribution and performance analysis require log parsing.
- Sub-agent inference calls are invisible to the orchestrator entirely; their
  logs stay inside the child sandbox's stderr and are lost on sandbox teardown.
- The training flywheel cannot correlate a tool-call record with the inference
  round that produced it without timestamp heuristics across two different
  formats (SQLite rows vs. log lines).

---

## 2  Goals and Non-Goals

### Goals

1. Record every inference call (orchestrator and sub-agent) in the central
   audit DB with structured, queryable columns.
2. Reuse the existing NMB-batched flush mechanism
   ([Audit DB Design §7](audit_db_design.md#7--open-question-sub-agent-tool-call-auditing))
   so sub-agent inference records arrive through the same path as sub-agent
   tool-call records.
3. Enable per-session cost tracking: `SUM(prompt_tokens + completion_tokens)`
   grouped by `session_id` gives total token spend for a request.
4. Keep the child sandbox lightweight — no new dependencies beyond what the
   tool-call flush already requires (a list buffer + one NMB send).

### Non-Goals

- Real-time streaming of token-level events (SSE chunks).  We audit the
  completed round, not partial tokens.
- Prompt-cache hit/miss tracking.  This is backend-specific metadata that may
  be added later as an optional JSON column.

---

## 3  Schema: `inference_calls` Table

A new `inference_calls` table in the same audit SQLite database, managed by a
new Alembic migration (`005_inference_calls.py`).

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | `TEXT PK` | no | Generated UUID (16 hex chars) |
| `timestamp` | `REAL` | no | `time.time()` at insert |
| `session_id` | `TEXT` | yes | `request_id` from the orchestrator / `task.assign` |
| `thread_ts` | `TEXT` | yes | Slack thread timestamp for message correlation |
| `source_sandbox` | `TEXT` | yes | Child `sandbox_id`; `NULL` for orchestrator-local |
| `model` | `TEXT` | no | Model identifier returned by the backend |
| `round` | `INTEGER` | no | Agent-loop round number (0-indexed) |
| `prompt_tokens` | `INTEGER` | no | Prompt token count from usage |
| `completion_tokens` | `INTEGER` | no | Completion token count from usage |
| `total_tokens` | `INTEGER` | no | Total token count from usage |
| `latency_ms` | `REAL` | no | Wall-clock HTTP round-trip time |
| `finish_reason` | `TEXT` | no | `stop`, `length`, `content_filter`, `tool_calls` |
| `has_tool_calls` | `INTEGER` | no | `1` if the response contained tool calls |
| `tool_call_count` | `INTEGER` | no | Number of tool calls in the response |
| `success` | `INTEGER` | no | `1` or `0` |
| `error_category` | `TEXT` | yes | `ErrorCategory` value on failure |
| `error_message` | `TEXT` | yes | Exception string on failure |
| `request_payload` | `TEXT` | yes | Full JSON message array sent to the model (see §6) |
| `response_payload` | `TEXT` | yes | Full assistant message JSON (see §6) |
| `request_payload_size` | `INTEGER` | no | Original request payload bytes (always stored) |
| `response_payload_size` | `INTEGER` | no | Original response payload bytes (always stored) |

### Indexes

- `ix_inference_calls_session` on `(session_id, timestamp)` — fast per-session
  cost and timeline queries.
- `ix_inference_calls_model` on `(model, timestamp)` — per-model cost
  aggregation.

### Relationship to `tool_calls`

A single inference round that returns tool calls produces:

1. One `inference_calls` row (the LLM round-trip).
2. N `tool_calls` rows (one per tool executed in that round).

These are correlated by `(session_id, timestamp)` ordering and by the
`round` column on `inference_calls`.  A formal foreign key is not used because
the tool-call rows are written asynchronously after each tool executes, not
atomically with the inference row.

---

## 4  Recording Mechanism

### 4.1  Orchestrator-Local Calls

After each `backend.complete()` call in `_run_agent_loop()` (and
`_inference_with_repair()` for the non-tool path), the orchestrator writes one
row via a new `AuditDB.log_inference_call()` method.  Same fire-and-forget
pattern as `log_tool_call()`: failures are caught at DEBUG level and never
block the agent loop.

```python
# In _run_agent_loop(), after result = await self._backend.complete(...)
if self._audit:
    try:
        await self._audit.log_inference_call(
            session_id=request_id,
            thread_ts=thread_ts,
            model=result.model,
            round=round_num,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.total_tokens,
            latency_ms=result.latency_ms,
            finish_reason=result.finish_reason,
            has_tool_calls=bool(result.tool_calls),
            tool_call_count=len(result.tool_calls) if result.tool_calls else 0,
            success=True,
            request_payload=json.dumps(inference_request.messages),
            response_payload=json.dumps(result.raw_response),
        )
    except Exception:
        logger.debug("Audit log_inference_call failed", exc_info=True)
```

Failed inference calls (caught as `InferenceError`) are also recorded with
`success=False` and the error category/message populated.

### 4.2  Sub-Agent Calls (NMB-Batched Flush)

Sub-agents use the same buffer-and-flush mechanism described in
[Audit DB Design §7.4](audit_db_design.md#74--mvp-implementation-sketch-option-c),
extended to carry both tool-call and inference-call records in a single
`audit.flush` message:

```python
await nmb.send(
    to="orchestrator",
    type="audit.flush",
    payload={
        "tool_calls": tool_call_buffer,
        "inference_calls": inference_call_buffer,
    },
)
tool_call_buffer.clear()
inference_call_buffer.clear()
```

The orchestrator's `audit.flush` handler iterates both arrays:

```python
for record in payload.get("inference_calls", []):
    await self._audit.log_inference_call(
        **record,
        source_sandbox=from_sandbox,
    )
```

### 4.3  JSONL Fallback

Same pattern as tool calls: if NMB is unavailable, the child appends
inference-call records to `/sandbox/audit_fallback.jsonl` alongside tool-call
records.  Each line carries a `"record_type": "inference_call"` discriminator
so the orchestrator's ingestion logic can route to the correct
`log_inference_call()` or `log_tool_call()` method.

```json
{"record_type": "inference_call", "model": "meta/llama-3.3-70b-instruct", "round": 0, "prompt_tokens": 2048, ...}
{"record_type": "tool_call", "service": "jira", "command": "jira_search", ...}
{"record_type": "inference_call", "model": "meta/llama-3.3-70b-instruct", "round": 1, ...}
```

---

## 5  AuditDB API Addition

```python
async def log_inference_call(
    self,
    *,
    session_id: str | None = None,
    thread_ts: str | None = None,
    source_sandbox: str | None = None,
    model: str,
    round: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: float,
    finish_reason: str,
    has_tool_calls: bool,
    tool_call_count: int = 0,
    success: bool,
    error_category: str | None = None,
    error_message: str | None = None,
    request_payload: str = "",
    response_payload: str = "",
) -> str:
    """Log a single inference call. Returns the generated row ID."""
```

When `persist_payloads` is `True`, both payload arguments are stored as-is.
When `False`, they are replaced with empty strings.  The `*_payload_size`
columns always record the original byte counts.

Follows the same conventions as `log_tool_call()`: generates a 16-char hex
UUID, writes via the async session, and returns the row ID.

---

## 6  Prompt and Completion Storage

### 6.1  Why Store Full Payloads

Metadata-only inference records (tokens, latency, model) are sufficient for
cost accounting and operational debugging.  But the training flywheel
([Training Flywheel §4.1](training_flywheel_deep_dive.md#41--layer-1-intra-sandbox-per-sandbox-middleware))
requires `llm_request` (the full message array) and `llm_response` (the full
assistant output) to produce fine-tuning examples.  Without the actual text,
the flywheel cannot:

- Construct SFT training pairs (system prompt + user message + tool-use
  trajectory → assistant response).
- Generate DPO preference pairs (same prompt → chosen / rejected completion).
- Run quality filtering or judge-model evaluation on the trace content.
- Re-run PII scrubbing on historical data when the scrubbing pipeline improves.

An audit record that says "round 2 used 3,512 prompt tokens and 580
completion tokens" tells you the cost; it does not tell you what the model
saw or what it said.  For a trace to be training-eligible, the text must be
present.

### 6.2  Tiered Storage Model

Not every deployment needs full payloads.  The design uses the same opt-in
pattern as the `tool_calls` table's `response_payload` column:

| Env var | Default | Effect |
|---------|---------|--------|
| `AUDIT_PERSIST_PAYLOADS` | `true` | Store full `request_payload` and `response_payload` |
| `AUDIT_PERSIST_PAYLOADS=false` | — | Store empty strings; `*_payload_size` still recorded |

When payloads are enabled:

- `request_payload` stores the full JSON message array sent to the model
  (`InferenceRequest.messages`), serialised with `json.dumps`.
- `response_payload` stores the full assistant message JSON
  (`InferenceResponse.raw_response`), including `content`, `tool_calls`,
  and `finish_reason`.

When payloads are disabled, both columns are empty strings.  The
`*_payload_size` columns always record the original byte counts so cost and
size analysis remains possible.

### 6.3  Storage Estimates

Prompt and completion sizes vary widely.  Using conservative estimates from
observed usage:

| Metric | Estimate | Notes |
|--------|----------|-------|
| Average prompt size | ~50k tokens ≈ 200 KB text | System prompt + thread history + tool defs |
| Average completion size | ~500 tokens ≈ 2 KB text | Assistant reply or tool-call JSON |
| Rounds per session | ~15 (range: 3–30) | Simple lookup: ~3. Orchestrator Q&A: ~5–8. Sub-agent coding task: ~15–30. |
| Sessions per day | ~50 | Single active user, moderate use |
| **Daily raw storage** | **~150 MB/day** | (200 + 2) KB × 15 rounds × 50 sessions |
| Monthly | ~4.5 GB | |
| Yearly | ~55 GB | |

At ~55 GB/year, the training flywheel pipeline must export and purge old data
periodically (quarterly or monthly).  The PVC-backed `/sandbox/audit.db` in
OpenShell typically provides 10–50 GB, so without purging the DB fills up in
2–10 months.  See
[Training Flywheel §7](training_flywheel_deep_dive.md#7--quality-filtering--annotation)
and [NMB Design §13](nmb_design.md#13--audit-bandwidth-and-capacity-estimates)
for the full capacity analysis.

**Mitigation for large prompts:** Prompts grow linearly with thread history.
The `PromptBuilder.max_thread_history` cap (default 50 messages) bounds the
worst case.  If storage becomes a concern, the payload columns can be moved
to a separate `inference_payloads` table or a JSONL sidecar file — the
metadata row in `inference_calls` always stays lightweight.

### 6.4  PII and Sensitive Data

Prompts and completions contain user data (Slack messages, Jira ticket
content, code snippets, names, emails).  This is handled at two levels:

**At rest (audit DB):** The audit DB is stored inside the user's own sandbox
(`/sandbox/audit.db`) or on their local machine (`~/.nemoclaw/audit.db`).
The data never leaves the user's environment without explicit action.  This
matches [Training Flywheel §13 Principle 1](training_flywheel_deep_dive.md#13--privacy-safety--data-governance):
"data never leaves without consent."

**Before training use:** The training flywheel pipeline applies mandatory PII
scrubbing before any trace enters the training dataset
([Training Flywheel §7 Stage 4](training_flywheel_deep_dive.md#7--quality-filtering--annotation)):

1. **Regex-based detection** — emails, API keys, URLs, credentials.
2. **NER model** — names, organizations, locations.
3. **Path normalization** — strip usernames from file paths.
4. **Template token replacement** — `<USER>`, `<EMAIL>`, `<API_KEY>`,
   `<INTERNAL_URL>`.
5. **Verification** — regex + NER re-scan; traces that resist scrubbing are
   flagged for manual review.

The audit DB stores the *raw* text.  PII scrubbing happens downstream in the
training pipeline, not at capture time.  This preserves the original data for
debugging while ensuring training data is clean.

**User controls:**

- `AUDIT_PERSIST_PAYLOADS=false` disables payload storage entirely.
- A future `/private` Slack command (Training Flywheel §13 Principle 3) will
  mark individual conversations as non-training-eligible, which the export
  pipeline respects.

### 6.5  Sub-Agent Payload Flushing

When `AUDIT_PERSIST_PAYLOADS` is enabled, sub-agent `audit.flush` messages
include the payload fields.  This increases the NMB message size
significantly — a single flush with 3 inference rounds could be 600+ KB.

**Mitigations:**

- **Flush more frequently** — flush after every round instead of buffering
  multiple rounds, keeping each NMB message ≤ 200 KB.
- **Compress payloads** — gzip the JSON before NMB send; the orchestrator
  decompresses on receipt.  Typical compression ratio for JSON prompt text
  is 5–10×.
- **Payload-free flush + sidecar download** — flush metadata over NMB (same
  as the no-payload mode) and write payloads to a local
  `/sandbox/inference_payloads.jsonl` file.  The orchestrator downloads it
  at task completion alongside the existing audit fallback file.

The recommended approach is **flush per round + optional gzip compression**,
falling back to the sidecar download if NMB message size limits are hit.

---

## 7  Useful Queries

### Total token cost per session

```sql
SELECT session_id,
       SUM(prompt_tokens)     AS total_prompt,
       SUM(completion_tokens)  AS total_completion,
       SUM(total_tokens)       AS total_tokens,
       COUNT(*)                AS inference_rounds
FROM   inference_calls
WHERE  session_id = :sid
GROUP BY session_id;
```

### Latency percentiles per model (last 24 h)

```sql
SELECT model,
       COUNT(*)                             AS calls,
       ROUND(AVG(latency_ms), 1)            AS avg_ms,
       ROUND(MAX(latency_ms), 1)            AS p100_ms
FROM   inference_calls
WHERE  timestamp > :since
GROUP BY model;
```

### Full agent trace for a session (interleaved)

```sql
SELECT 'inference' AS type, timestamp, model, round, latency_ms, NULL AS service, NULL AS command
FROM   inference_calls WHERE session_id = :sid
UNION ALL
SELECT 'tool_call' AS type, timestamp, NULL, NULL, duration_ms, service, command
FROM   tool_calls WHERE session_id = :sid
ORDER BY timestamp;
```

---

## 8  Migration: `005_inference_calls.py`

```python
def upgrade():
    op.create_table(
        "inference_calls",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("timestamp", sa.Float, nullable=False),
        sa.Column("session_id", sa.String, nullable=True),
        sa.Column("thread_ts", sa.String, nullable=True),
        sa.Column("source_sandbox", sa.String, nullable=True),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("latency_ms", sa.Float, nullable=False),
        sa.Column("finish_reason", sa.String, nullable=False),
        sa.Column("has_tool_calls", sa.Integer, nullable=False),
        sa.Column("tool_call_count", sa.Integer, nullable=False),
        sa.Column("success", sa.Integer, nullable=False),
        sa.Column("error_category", sa.String, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("request_payload", sa.String, nullable=True),
        sa.Column("response_payload", sa.String, nullable=True),
        sa.Column("request_payload_size", sa.Integer, nullable=False),
        sa.Column("response_payload_size", sa.Integer, nullable=False),
    )
    op.create_index("ix_inference_calls_session", "inference_calls", ["session_id", "timestamp"])
    op.create_index("ix_inference_calls_model", "inference_calls", ["model", "timestamp"])


def downgrade():
    op.drop_index("ix_inference_calls_model")
    op.drop_index("ix_inference_calls_session")
    op.drop_table("inference_calls")
```

---

## 9  Makefile Additions

| Target | Description |
|--------|-------------|
| `audit-inference-stats` | Row counts + last 5 inference calls with token totals |

The existing `audit-export` target is extended to also export the
`inference_calls` table to a second JSONL file
(`~/.nemoclaw/inference_calls.jsonl`).

---

## 10  Relationship to Existing Audit Surfaces

Updated from [Audit DB Design §8](audit_db_design.md#8--relationship-to-other-audit-surfaces):

| Surface | What it captures | Table(s) |
|---------|------------------|----------|
| **Audit DB — `tool_calls`** | Tool invocations (service, args, latency, success, response payload) | `tool_calls` |
| **Audit DB — `inference_calls`** (this doc) | LLM round-trips (model, tokens, latency, finish reason, full request/response payloads) | `inference_calls` |
| **Audit DB — NMB messages** | All routed NMB messages + connections | `messages`, `connections` |
| **Structured JSON logs** | Free-form operational logs (errors, retries, debug) | stderr / log file |
| **OpenShell proxy logs** | L7 proxy HTTP requests (host, method, path, policy) | `openshell logs` |

All three primary trace tables live in a single Alembic-managed SQLite
database (`/sandbox/audit.db` in OpenShell, `~/.nemoclaw/audit.db` locally).
Together they give a complete agent trace with full content, correlated by
`session_id` and timestamp.  See [Agent Trace Design](agent_trace_design.md)
for the full event taxonomy and secondary event types.
