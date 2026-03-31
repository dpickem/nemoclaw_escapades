# Training Flywheel — Deep Dive

> **Concept:** Turn every agent interaction into training data for improving the
> underlying model (Nemotron, or any fine-tunable LLM).
>
> **Related:** [Hermes Deep Dive §15](deep_dives/hermes_deep_dive.md#15--rl--environments--training),
> [Design Doc §4-M4](../design.md#milestone-4--memory-system--self-improvement-loop),
> [NMB Design Doc](nmb_design.md) (inter-sandbox trace capture)
>
> **Last reviewed:** 2026-03-29

---

## Table of Contents

1. [The Core Idea](#1--the-core-idea)
2. [Hermes's Existing Training Infrastructure](#2--hermess-existing-training-infrastructure)
3. [From Synthetic Environments to Real Interactions](#3--from-synthetic-environments-to-real-interactions)
4. [Trace Capture Architecture (Two-Layer Model)](#4--trace-capture-architecture-two-layer-model)
5. [NMB as a Training Data Accelerator](#5--nmb-as-a-training-data-accelerator)
6. [Trace Schema](#6--trace-schema)
7. [Quality Filtering & Annotation](#7--quality-filtering--annotation)
8. [SFT Data Pipeline](#8--sft-data-pipeline)
9. [RL / RLHF / DPO Data Pipeline](#9--rl--rlhf--dpo-data-pipeline)
10. [The Full Flywheel](#10--the-full-flywheel)
11. [Integration with NemoClaw Escapades](#11--integration-with-nemoclaw-escapades)
12. [Nemotron Fine-Tuning Target](#12--nemotron-fine-tuning-target)
13. [Privacy, Safety & Data Governance](#13--privacy-safety--data-governance)
14. [Cold Start & Bootstrapping](#14--cold-start--bootstrapping)
15. [Comparison with Other Approaches](#15--comparison-with-other-approaches)
16. [Open Questions](#16--open-questions)
17. [What to Build for NemoClaw Escapades](#17--what-to-build-for-nemoclaw-escapades)

---

## 1  The Core Idea

Most agentic systems treat daily interactions as ephemeral — the user talks to
the agent, the agent responds, and the conversation is eventually discarded or
archived for search. The model that powers the agent never sees its own
successes and failures.

The **training flywheel** changes this: every conversation, every tool call,
every agent trace becomes a potential data point for improving the next version
of the model. The agent gets better at exactly the tasks its users actually
care about, not just the tasks in a synthetic benchmark.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        THE TRAINING FLYWHEEL                             │
│                                                                          │
│  ┌──────────┐     ┌──────────────┐     ┌──────────────┐                  │
│  │  User    │────▶│  Agent       │────▶│  Task        │                  │
│  │  Request │     │  Execution   │     │  Outcome     │                  │
│  └──────────┘     └──────┬───────┘     └──────┬───────┘                  │
│                          │                     │                         │
│                          │   Full trace        │   Outcome signal        │
│                          │   captured          │   (success/failure/     │
│                          │                     │    user feedback)       │
│                          ▼                     ▼                         │
│                    ┌──────────────────────────────┐                      │
│                    │  Trace Store                 │                      │
│                    │  (structured conversation +  │                      │
│                    │   tool calls + outcomes)     │                      │
│                    └──────────────┬───────────────┘                      │
│                                   │                                      │
│                                   ▼                                      │
│                    ┌──────────────────────────────┐                      │
│                    │  Quality Filter & Annotator  │                      │
│                    │  • outcome-based scoring     │                      │
│                    │  • user feedback signals     │                      │
│                    │  • LLM-as-judge              │                      │
│                    │  • deduplication             │                      │
│                    │  • PII scrubbing             │                      │
│                    └──────────────┬───────────────┘                      │
│                                   │                                      │
│                          ┌────────┴────────┐                             │
│                          │                 │                             │
│                          ▼                 ▼                             │
│                    ┌───────────┐    ┌───────────────┐                    │
│                    │  SFT Data │    │  RL / DPO     │                    │
│                    │  Pipeline │    │  Data Pipeline│                    │
│                    └─────┬─────┘    └──────┬────────┘                    │
│                          │                 │                             │
│                          └────────┬────────┘                             │
│                                   │                                      │
│                                   ▼                                      │
│                    ┌──────────────────────────────┐                      │
│                    │  Fine-Tuned Model            │                      │
│                    │  (Nemotron next version)     │                      │
│                    └──────────────┬───────────────┘                      │
│                                   │                                      │
│                                   │  Deployed back as inference backend  │
│                                   │                                      │
│                                   └──────────────▶  Back to Agent        │
│                                                     (better at YOUR      │
│                                                      specific tasks)     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Why This Matters

| Traditional Fine-Tuning | Flywheel Fine-Tuning |
|--------------------------|----------------------|
| Generic benchmarks | Your actual tasks |
| Synthetic tool calls | Real tool calls with real outcomes |
| One-time data collection | Continuous, ever-growing dataset |
| Train once, deploy | Train → deploy → collect → train again |
| Model improves at general tasks | Model improves at *your* tasks |

---

## 2  Hermes's Existing Training Infrastructure

Hermes already has the building blocks for training data generation, though
oriented toward **synthetic environments** rather than live interaction capture.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Hermes Training Stack                              │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  environments/         Evaluation & RL environments             │  │
│  │                                                                 │  │
│  │  Each environment defines:                                      │  │
│  │  • A task specification (what the agent should accomplish)      │  │
│  │  • Available tools (which tools the agent can use)              │  │
│  │  • Success criteria (how to evaluate the outcome)               │  │
│  │  • Reset logic (how to start a fresh episode)                   │  │
│  │                                                                 │  │
│  │  Examples: coding tasks, file management, web research,         │  │
│  │  system administration, multi-step workflows                    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  batch_runner.py        Batch trajectory generation             │  │
│  │                                                                 │  │
│  │  • Runs the agent against many environment instances            │  │
│  │  • Generates trajectory data: (prompt, actions, observations,   │  │
│  │    rewards, final outcome)                                      │  │
│  │  • Parallelizes across environments for throughput              │  │
│  │  • Records full conversation history including tool calls       │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  trajectory_compressor.py    Compress for training              │  │
│  │                                                                 │  │
│  │  • Strips verbose intermediate steps                            │  │
│  │  • Keeps decision points and key tool calls                     │  │
│  │  • Formats into training-ready sequences                        │  │
│  │  • Supports both SFT format (input/output pairs) and            │  │
│  │    RL format (trajectories with rewards)                        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  toolset_distributions.py   Toolset sampling                    │  │
│  │                                                                 │  │
│  │  • Controls which tool combinations appear in training data     │  │
│  │  • Ensures diversity: the model sees terminal-heavy tasks,      │  │
│  │    file-heavy tasks, web-heavy tasks, mixed tasks               │  │
│  │  • Prevents overfitting to a single tool usage pattern          │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                              │                                        │
│                              ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  tinker-atropos/         Atropos RL integration (submodule)     │  │
│  │                                                                 │  │
│  │  Atropos is a framework for RL fine-tuning of LLMs.             │  │
│  │  Integration provides:                                          │  │
│  │  • Environment ↔ Atropos adapter                                │  │
│  │  • Reward shaping for tool-calling agents                       │  │
│  │  • GRPO / PPO training loop integration                         │  │
│  │  • On-policy trajectory collection                              │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Full pipeline:                                                       │
│    environments → batch_runner → trajectories → compressor            │
│    → training data (SFT / RL)                                         │
└───────────────────────────────────────────────────────────────────────┘
```

### Limitations of the Synthetic Approach

Hermes's training pipeline is powerful but has a key gap: it runs against
**synthetic environments**, not real user interactions. This means:

- Tasks are pre-defined by the environment designer, not discovered from use
- Tool call patterns reflect artificial scenarios, not real workflows
- There's no user feedback signal — only the programmatic success criteria
- The model improves at benchmarks but may not improve at the tasks users
  actually care about

The flywheel approach fills this gap by treating **production use** as the
environment.

---

## 3  From Synthetic Environments to Real Interactions

The conceptual leap is straightforward: real agent sessions are already
trajectories. Every conversation between a user and the NemoClaw system is
structurally identical to what `batch_runner.py` generates — it's a sequence
of (user message, agent reasoning, tool calls, tool results, agent response)
turns.

```
┌───────────────────────────────────────────────────────────────────────┐
│              Synthetic Environment vs. Production Environment         │
│                                                                       │
│  SYNTHETIC (Hermes batch_runner)      PRODUCTION (NemoClaw daily use) │
│                                                                       │
│  Environment defines task      ←→     User sends request via Slack    │
│  Agent receives task prompt    ←→     Agent receives user message     │
│  Agent reasons + calls tools   ←→     Agent reasons + calls tools     │
│  Environment returns results   ←→     Real tools return real results  │
│  Agent produces final answer   ←→     Agent responds to user          │
│  Env evaluates success/fail    ←→     User reacts (feedback signal)   │
│                                                                       │
│  Key differences:                                                     │
│                                                                       │
│  ┌─────────────────────────────┐  ┌─────────────────────────────────┐ │
│  │  Synthetic                  │  │  Production                     │ │
│  │                             │  │                                 │ │
│  │  • Deterministic tasks      │  │  • Open-ended, messy tasks      │ │
│  │  • Known success criteria   │  │  • Implicit success signals     │ │
│  │  • Controlled tool behavior │  │  • Real tools, real failures    │ │
│  │  • No user in the loop      │  │  • Rich user feedback           │ │
│  │  • Unlimited episodes       │  │  • Organic volume over time     │ │
│  │  • Artificial diversity     │  │  • Natural task distribution    │ │
│  │                             │  │                                 │ │
│  │  Good for: bootstrapping,   │  │  Good for: domain adaptation,   │ │
│  │  capability evaluation,     │  │  personalization, tool-use      │ │
│  │  safety testing             │  │  fluency, real-world grounding  │ │
│  └─────────────────────────────┘  └─────────────────────────────────┘ │
│                                                                       │
│  Ideal: BOTH. Use synthetic envs for capability bootstrapping and     │
│  safety evaluation. Use production traces for domain specialization.  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 4  Trace Capture Architecture (Two-Layer Model)

Every agent interaction must be captured as a structured trace. This is the
foundation of the flywheel — without comprehensive traces, nothing downstream
works.

A complete trace requires data from **two distinct layers**: what happens
*inside* each sandbox (the agent's LLM calls, tool invocations, reasoning),
and what happens *between* sandboxes (task delegation, progress updates,
review feedback). The [NemoClaw Message Bus](nmb_design.md) makes the
inter-sandbox layer trivially capturable through its audit log, while the
intra-sandbox layer requires per-sandbox middleware.

### 4.1  Layer 1: Intra-Sandbox (per-sandbox middleware)

Each sandbox (orchestrator, coding, review, research) runs a lightweight
trace middleware that wraps its agent loop. Captured events:

| Event | Description |
|-------|-------------|
| `user_message` | What the user said |
| `system_prompt` | Frozen snapshot, captured once per session |
| `llm_request` | Full request payload to inference API |
| `llm_response` | Full response including chain-of-thought |
| `tool_call` | Tool name, arguments, call ID |
| `tool_result` | Output, errors, timing |
| `agent_message` | Final response to user |
| `user_feedback` | Explicit or implicit (see §7) |
| `session_metadata` | Model, provider, latency, cost |

These events are the core training signal: they show exactly what the model
produced, which tools it chose, and what happened. The middleware extends
Hermes's existing session persistence (same SQLite DB, new tables for
training-specific fields like full payloads, token counts, and timing).

### 4.2  Layer 2: Inter-Sandbox (NMB audit log — free)

The NMB broker already persists every message with full payloads to
`~/.nemoclaw/nmb/audit.db` (SQLite; see [NMB Design §4](nmb_design.md#4--message-broker),
[§9](nmb_design.md#9--identity--security-model)). This is zero-cost trace
capture for all cross-sandbox activity. Captured events:

| Event | Description |
|-------|-------------|
| `task.assign` | Orchestrator → sub-agent delegation |
| `task.progress` | Intermediate status, % complete |
| `task.complete` | Final result, diff, files changed |
| `task.error` | Failure report, traceback |
| `task.cancel` | Orchestrator interrupts sub-agent |
| `task.redirect` | Mid-flight course correction |
| `review.request` | Diff sent for review |
| `review.feedback` | Comments + verdict |
| `review.lgtm` | Approval |
| `sandbox.ready` | Sub-agent initialization complete |
| `sandbox.shutdown` | Sub-agent terminated, with reason |

Each message carries `id`, `from`, `to`, `type`, `timestamp`, and `payload`.
The broker enforces identity (proxy-injected `X-Sandbox-ID`), so `from`/`to`
fields are trustworthy provenance data. Causal chains via `id`/`reply_to`
fields link messages into complete multi-agent trajectories.

### 4.3  Trace Collection (how trace.db reaches the host)

`trace.db` lives inside each sandbox's isolated filesystem. It reaches the
host by piggybacking on the existing sandbox lifecycle — the orchestrator
already runs a create → upload → task → download → delete sequence for every
sub-agent (see [Hermes Deep Dive §13.1](deep_dives/hermes_deep_dive.md#131--how-sub-agent-delegation-would-work-in-nemoclaw)).
trace.db collection is step 5.5, right after downloading task results and
before destroying the sandbox.

**Ephemeral sub-agent sandboxes** (coding, review, research):

```bash
# 1. CREATE sandbox
openshell sandbox create --policy coding.yaml -- claude

# 2. UPLOAD workspace / context
openshell sandbox upload <name> ./project /sandbox/src

# 3. TASK (via NMB or SSH exec)
#    Sub-agent works. trace.db accumulates inside sandbox.

# 4. WAIT for completion (NMB task.complete or poll)

# 5. DOWNLOAD results
openshell sandbox download <name> /sandbox/src ./output

# 5.5 COLLECT trace.db  ← NEW
openshell sandbox download <name> \
    /sandbox/data/trace.db \
    ~/.nemoclaw/training/collected/<sandbox-name>.db

# 6. DELETE sandbox
openshell sandbox delete <name>
```

**Always-on orchestrator sandbox:**

- **Same host as broker** — trace.db is directly accessible; the merger can
  use SQLite `ATTACH DATABASE` for zero-copy cross-DB queries.
- **Multi-host** — periodic download via cron:

```bash
openshell sandbox download orchestrator \
    /sandbox/data/trace.db \
    ~/.nemoclaw/training/collected/orchestrator.db
```

### 4.4  Trace Merger

The merger runs on the host (cron or on-demand). It joins intra-sandbox
events with inter-sandbox NMB messages to produce complete multi-agent
trajectories.

**Steps:**

1. `ATTACH` all source databases (SQLite `ATTACH DATABASE` — zero-copy
   cross-DB queries)
2. `JOIN` intra-sandbox events with NMB messages by trace_id / sandbox_id /
   timestamp correlation
3. `RECONSTRUCT` causal chains via NMB `id`/`reply_to` fields
4. `EXTRACT` review iterations and DPO pairs
5. `WRITE` merged traces (v2 schema) to `training.db`
6. `MARK` source rows as merged (idempotent — safe to re-run)

**Example merged trace:**

```
Orchestrator intra-trace (LLM calls, tool invocations)
  + NMB audit: task.assign → coding-sandbox-1 (id: abc)
  + coding-sandbox-1 intra-trace (its LLM calls + tool use)
  + NMB audit: task.complete from coding-sandbox-1 (reply_to: abc)
  + NMB audit: review.request → review-sandbox-1 (id: def)
  + review-sandbox-1 intra-trace (its LLM calls)
  + NMB audit: review.feedback from review-sandbox-1 (reply_to: def)
  = Complete multi-agent trajectory with full causal chain
```

### 4.5  Retention

Collected trace.db files are **kept after merging — not discarded.** After
merging, they are moved from `collected/` to `archive/`:

- `~/.nemoclaw/training/collected/` — active, awaiting merge
- `~/.nemoclaw/training/archive/` — merged, retained indefinitely

Reasons for keeping them:

- They're tiny (~2-50 KB each, <100 MB/year at heavy use)
- If the merge logic improves, we can re-merge from raw sources
- Raw per-sandbox data is useful for per-agent analysis (e.g., "how does the
  coding agent's tool use differ from the review agent's?") without querying
  the merged view
- `audit.db` is also retained — it's the NMB's persistent store

**Storage budget** (generous estimates, 1 active user):

| Database | Annual Size |
|----------|-------------|
| Collected trace.db files | ~50-200 MB |
| audit.db | ~100-500 MB |
| training.db | ~200 MB - 1 GB |
| **Total** | **<2 GB / year** |

### 4.6  Host File Layout

```
~/.nemoclaw/
├── nmb/
│   └── audit.db                    # Layer 2 (NMB broker, always here)
└── training/
    ├── collected/                   # awaiting merge
    │   ├── orchestrator.db          # always-on, periodic sync
    │   ├── coding-sandbox-1.db      # collected at step 5.5
    │   ├── coding-sandbox-2.db      # from another task
    │   └── review-sandbox-1.db      # collected at step 5.5
    ├── archive/                     # merged, retained forever
    │   ├── coding-sandbox-0.db      # (moved here after merge)
    │   └── review-sandbox-0.db
    └── training.db                  # merged output
```

### 4.7  Trace Store (three SQLite databases)

All trace storage uses SQLite. JSONL is only used as a final export format
for training frameworks (NeMo, TRL, Axolotl).

**1. `trace.db` — per-sandbox, Layer 1**

| Property | Value |
|----------|-------|
| Location | `/sandbox/data/trace.db` (inside each sandbox) |
| Writer | In-process trace middleware |
| Contents | Every LLM call, tool invocation, user message, agent response, feedback signal, session metadata |
| Size | ~2-50 KB per trace, ~100-500 traces/day |
| Retention | Indefinite (collected on sandbox destroy, archived after merge) |

Tables: `traces` (one row per interaction), `turns` (individual messages),
`tool_calls` (name, args, result, timing), `feedback` (explicit + implicit
signals), `sessions` (session-level metadata — model, cost).

Extends Hermes session persistence (same SQLite DB, new tables for
training-specific fields).

**2. `audit.db` — centralized, Layer 2 (NMB broker)**

| Property | Value |
|----------|-------|
| Location | `~/.nemoclaw/nmb/audit.db` (on broker host) |
| Writer | NMB broker |
| Contents | Every inter-sandbox message with full payload |
| Size | ~0.5-5 KB per message, continuous stream |
| Retention | Indefinite |

Schema: see [NMB Design §4](nmb_design.md#4--message-broker) — `messages`
and `connections` tables, FTS5 index over payloads, indexes on `from_sandbox`,
`to_sandbox`, `type`, `reply_to` for causal chain reconstruction.

**3. `training.db` — merged output**

| Property | Value |
|----------|-------|
| Location | `~/.nemoclaw/training/training.db` (on host) |
| Writer | Trace merger (cron job or on-demand) |
| Contents | Complete multi-agent traces (v2 schema) with quality scores, delegation chains, review iterations |
| Inputs | `trace.db` from each sandbox + `audit.db` |
| Retention | Indefinite |

Tables: `merged_traces` (complete traces, v2 schema as JSON),
`trace_metadata` (indexed fields for fast filtering), `quality` (scores,
tier, judge assessments), `dpo_pairs` (extracted preference pairs), `exports`
(audit of which traces went to which training dataset).

This is the single source of truth for the training pipeline. The SFT/DPO
exporters read from here.

### 4.8  Why SQLite for Everything

- **Queryable** at every layer — filter by sandbox, type, time range
- **Same technology** as Hermes session persistence (proven pattern)
- **WAL mode** — concurrent reads (training pipeline) + writes (broker/middleware)
- **Atomic writes** — no corruption from mid-write crashes
- **Single-file DBs** — easy to collect, back up, ship to training infrastructure
- **FTS5** — full-text search across payloads and tool outputs
- **No external dependencies** — Python stdlib includes `sqlite3`

### What Makes This Different from Session Persistence

Hermes (and NemoClaw) already persist conversations to SQLite for session
search. The trace capture layer goes further:

| Session Persistence | Trace Capture |
|---------------------|---------------|
| Stores messages for search/recall | Stores full payloads for training |
| May omit tool call details | Captures every tool argument and result |
| Single-sandbox scope | Multi-sandbox: merges orchestrator + sub-agent + NMB data |
| No cross-sandbox visibility | Full multi-agent trajectory with causal links |
| Optimized for agent retrieval | Optimized for training pipeline ingestion |
| Lives in the agent's own SQLite | Extends same SQLite (trace.db) + merges into host training.db |
| No quality annotations | Includes outcome signals and quality scores |
| No link to training pipeline | Feeds directly into SFT/RL data generation |

### Why Two Layers?

The NMB audit log alone is not sufficient — it captures the *connective
tissue* between sandboxes but not what happens inside each sandbox's agent
loop. Conversely, per-sandbox middleware alone misses the delegation context
that explains *why* a sub-agent received a particular task and how its output
was used.

The two-layer model captures everything:

| Layer | Captures | Missing Without It |
|-------|----------|-------------------|
| Intra-sandbox | LLM reasoning, tool calls, tool results, chain-of-thought | No training signal for tool selection, argument formatting, reasoning quality |
| Inter-sandbox (NMB) | Delegation context, progress streaming, review feedback, causal chains | No multi-agent trajectories, no review-loop DPO pairs, no delegation training data |

---

## 5  NMB as a Training Data Accelerator

The [NemoClaw Message Bus](nmb_design.md) was designed for inter-sandbox
coordination, but its architecture makes it a powerful — and essentially free —
training data collection mechanism. This section details six specific ways the
NMB accelerates the training flywheel.

### 5.1  The Audit Log Is Already a Trace Source

The NMB broker persists every routed message to `~/.nemoclaw/nmb/audit.db`
(SQLite, WAL mode, FTS5 indexed) with full payloads enabled by default
(see [NMB Design §4](nmb_design.md#4--message-broker)). The audit DB is
retained indefinitely — it is training data, not a transient operational log.
Every `task.assign`, `task.complete`, `review.request`, `review.feedback`
message is a structured event with authenticated `from`/`to`, `type`,
`timestamp`, and the complete `payload`. No additional middleware is needed for
the inter-sandbox portion of trace capture — it's a byproduct of the NMB's
operational logging.

```
┌───────────────────────────────────────────────────────────────────────┐
│  NMB Audit Log Entry (example)                                        │
│                                                                       │
│  {                                                                    │
│    "timestamp": "2026-04-15T14:23:01.456Z",                           │
│    "id": "msg-uuid-abc",                                              │
│    "op": "deliver",                                                   │
│    "from": "orchestrator",           // proxy-enforced identity       │
│    "to": "coding-sandbox-1",                                          │
│    "type": "task.assign",                                             │
│    "payload_size": 4820,                                              │
│    "payload": {                      // full payload captured         │
│      "prompt": "Implement the retry logic for the API client...",     │
│      "context_files": [                                               │
│        { "path": "src/api_client.py", "content": "..." }              │
│      ]                                                                │
│    },                                                                 │
│    "delivery_status": "delivered"                                     │
│  }                                                                    │
│                                                                       │
│  This single log entry is already a partial training example:         │
│  it shows what task was delegated, with what context, to which agent. │
└───────────────────────────────────────────────────────────────────────┘
```

### 5.2  Automatic Causal Chain Linking

NMB messages carry `id` and `reply_to` fields that create natural causal
chains across sandboxes. These chains link parent orchestrator traces to
child sub-agent traces without any additional correlation logic:

```
┌───────────────────────────────────────────────────────────────────────┐
│  NMB Causal Chain (complete multi-agent trajectory)                   │
│                                                                       │
│  orchestrator trace                                                   │
│    └── task.assign (id: abc) → coding-sandbox                         │
│         ├── task.progress (from: coding-sb, 10%)                      │
│         ├── task.progress (from: coding-sb, 50%)                      │
│         └── task.complete (from: coding-sb, reply_to: abc, diff=...)  │
│              └── review.request (id: def) → review-sandbox            │
│                   └── review.feedback (from: review-sb, reply_to: def,│
│                        verdict=request_changes, comments=[...])       │
│                        └── task.assign (id: ghi) → coding-sandbox     │
│                             └── task.complete (reply_to: ghi,         │
│                                  updated_diff=...)                    │
│                                  └── review.request (id: jkl)         │
│                                       └── review.lgtm (reply_to: jkl) │
│                                                                       │
│  The id/reply_to chain reconstructs the full delegation tree.         │
│  The trace merger (§4) uses this chain to assemble the complete       │
│  multi-agent trajectory at export time.                               │
└───────────────────────────────────────────────────────────────────────┘
```

### 5.3  The Review Loop Is a DPO Goldmine

This is the single most valuable training data implication of the NMB. The
coding + review loop ([NMB Design §11](nmb_design.md#11--revised-coding--review-loop))
generates **systematic preference pairs** at every iteration — not just when
the user happens to correct the agent, but on every multi-iteration review
cycle.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Review Loop → DPO Pairs (automatic extraction)                       │
│                                                                       │
│  ITERATION 1:                                                         │
│  Coding agent produces code → diff_v1                                 │
│  Review agent: review.feedback (verdict: "request_changes",           │
│    comments: ["Missing error handling in retry_with_backoff()",       │
│               "Connection pool not released on exception"])           │
│  → diff_v1 becomes the REJECTED response                              │
│                                                                       │
│  ITERATION 2:                                                         │
│  Coding agent incorporates feedback → diff_v2                         │
│  Review agent: review.lgtm (summary: "Error handling added,           │
│    connection pool lifecycle correct")                                │
│  → diff_v2 becomes the CHOSEN response                                │
│                                                                       │
│  DPO pair extracted:                                                  │
│  {                                                                    │
│    "prompt": "Implement retry logic for API client [+ context]",      │
│    "chosen": diff_v2 (approved by review agent),                      │
│    "rejected": diff_v1 (had review comments),                         │
│    "metadata": {                                                      │
│      "source": "nmb_review_loop",                                     │
│      "review_comments": [...],    // explains WHY chosen > rejected   │
│      "iterations": 2                                                  │
│    }                                                                  │
│  }                                                                    │
│                                                                       │
│  Key advantages over user-correction DPO pairs:                       │
│  • SYSTEMATIC: every multi-iteration review generates pairs           │
│  • EXPLAINED: review comments provide the reasoning, not just         │
│    "this one is better" — usable for preference explanation training  │
│  • HIGH VOLUME: review loop runs on every coding task, not just       │
│    when a user happens to correct the agent                           │
│  • CODE-SPECIFIC: teaches the model what good code looks like         │
│    in the user's specific codebase and style                          │
└───────────────────────────────────────────────────────────────────────┘
```

### 5.4  Progress Streaming Enriches Trajectories

The pub/sub progress pattern ([NMB Design §10, Pattern 3](nmb_design.md#10--communication-patterns))
provides intermediate observations that make trajectories richer than just
(input, final_output) pairs:

```
task.assign → progress 10% → progress 50% (with tool_output) → task.complete
```

These intermediate steps are valuable for training models to:
- Produce useful status updates during long-running tasks
- Reason about task decomposition and progress estimation
- Exhibit transparent execution (showing work, not just final answers)

Standard SFT data rarely includes intermediate progress reporting because
synthetic benchmarks don't model it. Production NMB traces capture it
naturally.

### 5.5  Multi-Host Centralized Collection

In the multi-host topology ([NMB Design §3.2](nmb_design.md#32--multi-host-sandboxes-distributed-across-machines)),
all messages flow through a single broker regardless of where sandboxes
physically run. The trace collector gets a single aggregation point for all
agent activity across DGX Spark, Brev, and VPS instances — no need to gather
traces from each host separately or reconcile distributed logs.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Multi-Host Trace Aggregation                                         │
│                                                                       │
│  DGX Spark (coding sandbox)                                           │
│    └── intra-sandbox traces → local trace.db (SQLite)                 │
│    └── NMB messages → centralized broker audit.db                     │
│                                                                       │
│  Brev (orchestrator + broker)                                         │
│    └── intra-sandbox traces → local trace.db (SQLite)                 │
│    └── NMB audit.db ← ALL messages from ALL hosts (SQLite)            │
│         (single file, complete inter-sandbox picture)                 │
│                                                                       │
│  VPS (review sandbox)                                                 │
│    └── intra-sandbox traces → local trace.db (SQLite)                 │
│    └── NMB messages → centralized broker audit.db                     │
│                                                                       │
│  Collection flow (see §4 TRACE COLLECTION & MERGE):                   │
│  • Ephemeral sandboxes: trace.db downloaded at step 5.5 of            │
│    the sandbox lifecycle (after results, before delete)               │
│  • Orchestrator: trace.db synced periodically (cron) or ATTACHed      │
│  • NMB audit.db: already centralized on broker host                   │
│  • Merger: runs on host, ATTACHes all DBs, writes training.db         │
└───────────────────────────────────────────────────────────────────────┘
```

### 5.6  System Messages Provide Quality Context

NMB system messages (`sandbox.ready`, `sandbox.shutdown`, `heartbeat`)
provide quality signals for the filtering pipeline (§7):

| NMB System Event | Quality Signal |
|-----------------|---------------|
| `sandbox.shutdown` with `reason: "crash"` mid-task | Mark trace as low quality — agent died unexpectedly |
| Heartbeat gap (>60s without heartbeat) | Agent was stuck — possible tool hang or inference timeout |
| `task.error` with `recoverable: false` | Strong negative outcome signal |
| `task.cancel` / `task.redirect` | Orchestrator intervened — partial trace, may indicate sub-agent confusion |
| `sandbox.ready` → `task.complete` with short elapsed time | Clean execution — positive quality signal |
| Multiple `task.progress` updates with increasing % | Agent communicated transparently — positive behavioral signal |

---

## 6  Trace Schema

A single trace represents one complete user-agent interaction (which may span
multiple LLM turns if the agent uses tools). In multi-agent scenarios, the
trace includes a `delegation_chain` and `review_iterations` array
reconstructed from NMB audit log entries (see [§5](#5--nmb-as-a-training-data-accelerator)).

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Trace Schema (v2 — with NMB integration)           │
│                                                                       │
│  {                                                                    │
│    "trace_id": "uuid",                                                │
│    "session_id": "uuid",                                              │
│    "parent_trace_id": "uuid | null",    // for sub-agent traces       │
│    "timestamp_start": "ISO8601",                                      │
│    "timestamp_end": "ISO8601",                                        │
│    "user_id": "string",                                               │
│    "entry_point": "slack | cli | cron | web_ui | sub_agent",          │
│                                                                       │
│    "model": {                                                         │
│      "provider": "nvidia-nim | openrouter | anthropic | ...",         │
│      "model_id": "nvidia/nemotron-3-super-120b-a12b",                 │
│      "api_mode": "chat_completions"                                   │
│    },                                                                 │
│                                                                       │
│    "system_prompt_hash": "sha256",      // for dedup, not the full    │
│                                         // prompt (may contain PII)   │
│                                                                       │
│    "turns": [                                                         │
│      {                                                                │
│        "role": "user",                                                │
│        "content": "...",                                              │
│        "timestamp": "ISO8601"                                         │
│      },                                                               │
│      {                                                                │
│        "role": "assistant",                                           │
│        "content": "...",            // text response                  │
│        "reasoning": "...",          // chain-of-thought if available  │
│        "tool_calls": [                                                │
│          {                                                            │
│            "id": "call_abc",                                          │
│            "tool": "terminal",                                        │
│            "arguments": { "command": "git status" },                  │
│            "result": { "stdout": "...", "exit_code": 0 },             │
│            "latency_ms": 1200                                         │
│          }                                                            │
│        ],                                                             │
│        "latency_ms": 3400,                                            │
│        "input_tokens": 12000,                                         │
│        "output_tokens": 800                                           │
│      },                                                               │
│      // ... more turns ...                                            │
│    ],                                                                 │
│                                                                       │
│    "outcome": {                                                       │
│      "status": "success | failure | partial | unknown",               │
│      "signals": [                                                     │
│        { "type": "user_explicit", "value": "thumbs_up" },             │
│        { "type": "task_completion", "value": true },                  │
│        { "type": "error_count", "value": 0 },                         │
│        { "type": "user_correction", "value": false },                 │
│        { "type": "follow_up_needed", "value": false }                 │
│      ]                                                                │
│    },                                                                 │
│                                                                       │
│    // --- NMB-sourced fields (Layer 2, from audit log) ---            │
│                                                                       │
│    "delegation_chain": [            // reconstructed from NMB msgs    │
│      {                                                                │
│        "nmb_message_id": "msg-uuid-abc",                              │
│        "type": "task.assign",                                         │
│        "from": "orchestrator",                                        │
│        "to": "coding-sandbox-1",                                      │
│        "timestamp": "ISO8601",                                        │
│        "payload_summary": "Implement retry logic..."                  │
│      },                                                               │
│      {                                                                │
│        "nmb_message_id": "msg-uuid-def",                              │
│        "type": "task.complete",                                       │
│        "from": "coding-sandbox-1",                                    │
│        "to": "orchestrator",                                          │
│        "reply_to": "msg-uuid-abc",                                    │
│        "timestamp": "ISO8601",                                        │
│        "payload": { "diff": "...", "summary": "..." }                 │
│      }                                                                │
│      // ... review.request, review.feedback, etc.                     │
│    ],                                                                 │
│                                                                       │
│    "review_iterations": [           // extracted from review loop     │
│      {                                                                │
│        "iteration": 1,                                                │
│        "diff": "...",                                                 │
│        "verdict": "request_changes",                                  │
│        "comments": ["Missing error handling..."],                     │
│        "dpo_eligible": true         // chosen/rejected pair exists    │
│      },                                                               │
│      {                                                                │
│        "iteration": 2,                                                │
│        "diff": "...",                                                 │
│        "verdict": "lgtm",                                             │
│        "comments": [],                                                │
│        "dpo_eligible": true                                           │
│      }                                                                │
│    ],                                                                 │
│                                                                       │
│    "sandboxes_involved": [                                            │
│      "orchestrator", "coding-sandbox-1", "review-sandbox-1"           │
│    ],                                                                 │
│                                                                       │
│    // --- Summary fields ---                                          │
│                                                                       │
│    "tools_used": ["terminal", "read_file", "edit_file"],              │
│    "skills_used": ["git-pr-workflow"],                                │
│    "total_tool_calls": 7,                                             │
│    "total_turns": 3,                                                  │
│    "total_tokens": 45000,                                             │
│    "total_nmb_messages": 8,                                           │
│    "delegation_depth": 1,           // 0 = no delegation              │
│    "estimated_cost_usd": 0.023                                        │
│  }                                                                    │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 7  Quality Filtering & Annotation

Raw traces are not training data. Most traces are mediocre — routine queries,
ambiguous outcomes, incomplete interactions. The quality pipeline separates
signal from noise.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Quality Pipeline                                      │
│                                                                          │
│  ┌──────────────────┐                                                    │
│  │  Raw Traces      │  ~100-500 per day (active user)                    │
│  └────────┬─────────┘                                                    │
│           │                                                              │
│           ▼                                                              │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Stage 1: Automatic Filtering                                    │    │
│  │                                                                  │    │
│  │  REMOVE:                                                         │    │
│  │  • Trivial interactions (< 2 turns, no tool calls)               │    │
│  │  • System/health-check conversations                             │    │
│  │  • Duplicate/near-duplicate traces (content hash)                │    │
│  │  • Traces with PII that can't be scrubbed                        │    │
│  │  • Traces from broken sessions (crashed mid-execution;           │    │
│  │    detected via NMB sandbox.shutdown with reason: "crash")       │    │
│  │                                                                  │    │
│  │  KEEP: everything else → ~40-60% pass rate                       │    │
│  └──────────────────────────┬───────────────────────────────────────┘    │
│                              │                                           │
│                              ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Stage 2: Outcome-Based Scoring                                  │    │
│  │                                                                  │    │
│  │  Signal hierarchy (strongest → weakest):                         │    │
│  │                                                                  │    │
│  │  1. EXPLICIT USER FEEDBACK                                       │    │
│  │     • Thumbs up/down in Slack reaction                           │    │
│  │     • User says "thanks" / "perfect" / "that's wrong"            │    │
│  │     • User corrects the agent's approach                         │    │
│  │     Weight: 1.0                                                  │    │
│  │                                                                  │    │
│  │  2. TASK COMPLETION SIGNAL                                       │    │
│  │     • Tool calls all succeeded (no errors)                       │    │
│  │     • Agent reached a natural conclusion (not cut off)           │    │
│  │     • No user correction followed                                │    │
│  │     Weight: 0.7                                                  │    │
│  │                                                                  │    │
│  │  3. BEHAVIORAL SIGNALS                                           │    │
│  │     • User continued the conversation (engagement)               │    │
│  │     • User asked a follow-up (topic maintained)                  │    │
│  │     • User abandoned without response (negative signal)          │    │
│  │     • User retried the same request differently (negative)       │    │
│  │     Weight: 0.3-0.5                                              │    │
│  │                                                                  │    │
│  │  4. HEURISTIC SIGNALS                                            │    │
│  │     • Efficiency (fewer retries = better)                        │    │
│  │     • Tool call success rate within the trace                    │    │
│  │     • Response latency relative to complexity                    │    │
│  │     Weight: 0.1-0.2                                              │    │
│  │                                                                  │    │
│  │  Composite score: weighted sum → normalized to [0, 1]            │    │
│  └──────────────────────────┬───────────────────────────────────────┘    │
│                              │                                           │
│                              ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Stage 3: LLM-as-Judge (optional, for high-value traces)         │    │
│  │                                                                  │    │
│  │  For traces with quality_score > 0.6:                            │    │
│  │  • Send (user request, agent response) to a judge model          │    │
│  │  • Judge evaluates: helpfulness, correctness, efficiency,        │    │
│  │    safety, tool-use appropriateness                              │    │
│  │  • Judge score adjusts the composite score                       │    │
│  │  • Judge can also generate preference pairs (for DPO):           │    │
│  │    "This response is better because..."                          │    │
│  │                                                                  │    │
│  │  Judge model: a capable model (e.g., Nemotron Ultra 253B or      │    │
│  │  Claude) that is stronger than the agent's own model             │    │
│  └──────────────────────────┬───────────────────────────────────────┘    │
│                              │                                           │
│                              ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Stage 4: PII Scrubbing & Sanitization                           │    │
│  │                                                                  │    │
│  │  • Detect and redact: names, emails, API keys, file paths with   │    │
│  │    usernames, internal URLs, credentials                         │    │
│  │  • Replace with template tokens: <USER>, <EMAIL>, <API_KEY>,     │    │
│  │    <INTERNAL_URL>                                                │    │
│  │  • Verify scrubbing with regex + NER model                       │    │
│  │  • Flag traces that resist scrubbing for manual review           │    │
│  └──────────────────────────┬───────────────────────────────────────┘    │
│                              │                                           │
│                              ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Annotated Trace Store                                           │    │
│  │                                                                  │    │
│  │  Each trace now has:                                             │    │
│  │  • quality_score: float [0, 1]                                   │    │
│  │  • quality_tier: "gold" (>0.8) | "silver" (>0.5) | "bronze"      │    │
│  │  • feedback_signals: [...]                                       │    │
│  │  • judge_assessment: {...} (if evaluated)                        │    │
│  │  • pii_scrubbed: true                                            │    │
│  │  • training_eligible: bool                                       │    │
│  │                                                                  │    │
│  │  ~15-25% of raw traces end up as "gold" training data            │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Volume estimates (1 active user, moderate daily use):                   │
│  • Raw traces: ~100-500 / day                                            │
│  • After filtering: ~50-250 / day                                        │
│  • Gold quality: ~15-75 / day                                            │
│  • After 3 months: ~1,500-7,000 gold traces                              │
│  • After 1 year: ~5,000-25,000 gold traces                               │
│                                                                          │
│  This is small for pre-training but meaningful for fine-tuning,          │
│  especially domain-specific SFT and tool-use DPO.                        │
└──────────────────────────────────────────────────────────────────────────┘
```

### Feedback Collection Mechanisms

The richest signal comes from user feedback. The system should make it easy
(but never mandatory) for the user to provide feedback:

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Feedback Collection                                │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  EXPLICIT FEEDBACK (high signal, low volume)                     │ │
│  │                                                                  │ │
│  │  Slack:                                                          │ │
│  │  • React with 👍 / 👎 to agent responses                           │ │
│  │  • Reply "that's wrong" / "actually I wanted..."                 │ │
│  │  • Use /feedback command for detailed input                      │ │
│  │                                                                  │ │
│  │  Web UI:                                                         │ │
│  │  • Thumbs up/down on each agent response                         │ │
│  │  • Star individual tool calls that were particularly good        │ │
│  │  • Flag responses for review                                     │ │
│  │                                                                  │ │
│  │  Agent-prompted:                                                 │ │
│  │  • After complex tasks: "Did this work for you?"                 │ │
│  │  • Periodically: "Is there anything I could do better?"          │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  IMPLICIT FEEDBACK (lower signal, high volume)                   │ │
│  │                                                                  │ │
│  │  Positive signals:                                               │ │
│  │  • User continues conversation on same topic                     │ │
│  │  • User delegates more tasks to the agent                        │ │
│  │  • No correction follows agent action                            │ │
│  │  • User references agent's output in later conversations         │ │
│  │                                                                  │ │
│  │  Negative signals:                                               │ │
│  │  • User rephrases the same request                               │ │
│  │  • User abandons conversation abruptly                           │ │
│  │  • User corrects or overrides agent's action                     │ │
│  │  • User says "never mind" / "I'll do it myself"                  │ │
│  │  • Follow-up message contradicts agent's conclusion              │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 8  SFT Data Pipeline

Supervised Fine-Tuning (SFT) teaches the model to produce good responses by
showing it examples of (input, desired output) pairs.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    SFT Data Pipeline                                     │
│                                                                          │
│  Input: Annotated traces with quality_tier = "gold"                      │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Step 1: Extract SFT Examples                                      │  │
│  │                                                                    │  │
│  │  For each gold trace, extract one or more training examples:       │  │
│  │                                                                    │  │
│  │  Format A — Full Conversation (multi-turn SFT):                    │  │
│  │  {                                                                 │  │
│  │    "messages": [                                                   │  │
│  │      { "role": "system", "content": "<system prompt>" },           │  │
│  │      { "role": "user", "content": "<user message>" },              │  │
│  │      { "role": "assistant", "content": "<agent response>",         │  │
│  │        "tool_calls": [...] },                                      │  │
│  │      { "role": "tool", "content": "<tool result>",                 │  │
│  │        "tool_call_id": "..." },                                    │  │
│  │      { "role": "assistant", "content": "<final response>" }        │  │
│  │    ]                                                               │  │
│  │  }                                                                 │  │
│  │                                                                    │  │
│  │  Format B — Single-Turn Tool Use:                                  │  │
│  │  {                                                                 │  │
│  │    "messages": [                                                   │  │
│  │      { "role": "system", "content": "..." },                       │  │
│  │      { "role": "user", "content": "Check the Jira board for        │  │
│  │        blockers" },                                                │  │
│  │      { "role": "assistant", "tool_calls": [                        │  │
│  │        { "function": { "name": "jira_search",                      │  │
│  │          "arguments": "{\"jql\": \"...\"}" } }                     │  │
│  │      ] }                                                           │  │
│  │    ]                                                               │  │
│  │  }                                                                 │  │
│  │  (Teaches tool selection and argument formatting)                  │  │
│  │                                                                    │  │
│  │  Format C — Skill Application:                                     │  │
│  │  Extract traces where the agent successfully applied a skill,      │  │
│  │  to teach the model when and how to use skills.                    │  │
│  └──────────────────────────┬─────────────────────────────────────────┘  │
│                              │                                           │
│                              ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Step 2: Trajectory Compression                                    │  │
│  │                                                                    │  │
│  │  Real traces are often verbose. Compress them for training:        │  │
│  │                                                                    │  │
│  │  • Remove redundant tool calls (3 failed attempts → keep only      │  │
│  │    the successful one)                                             │  │
│  │  • Truncate large tool outputs (100-line file → first/last 10)     │  │
│  │  • Strip debugging detours that led nowhere                        │  │
│  │  • Preserve the "golden path" — the sequence of actions that       │  │
│  │    actually solved the problem                                     │  │
│  │                                                                    │  │
│  │  This is the real-world analog of Hermes's trajectory_compressor.  │  │
│  │  Key difference: we compress real traces, not synthetic ones.      │  │
│  └──────────────────────────┬─────────────────────────────────────────┘  │
│                              │                                           │
│                              ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Step 3: Balance & Diversity                                       │  │
│  │                                                                    │  │
│  │  Production traces reflect real usage, which may be skewed:        │  │
│  │  • 60% coding tasks, 20% Jira queries, 10% Slack searches, ...     │  │
│  │                                                                    │  │
│  │  Rebalance for training:                                           │  │
│  │  • Upsample underrepresented tool categories                       │  │
│  │  • Downsample overrepresented simple queries                       │  │
│  │  • Ensure coverage of all tools and skill types                    │  │
│  │  • Mix in synthetic examples for tools that are rarely used        │  │
│  │    in production (toolset_distributions.py analog)                 │  │
│  └──────────────────────────┬─────────────────────────────────────────┘  │
│                              │                                           │
│                              ▼                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Output: SFT Training Dataset                                      │  │
│  │                                                                    │  │
│  │  Format: JSONL (one example per line)                              │  │
│  │  Compatible with: NeMo Framework, Hugging Face TRL, OpenAI         │  │
│  │    fine-tuning API, Axolotl                                        │  │
│  │                                                                    │  │
│  │  Expected volume (after 3 months):                                 │  │
│  │  • ~1,500-7,000 gold traces                                        │  │
│  │  • ~5,000-20,000 extracted SFT examples                            │  │
│  │    (multiple examples per trace)                                   │  │
│  │  • Sufficient for meaningful domain-specific fine-tuning           │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 9  RL / RLHF / DPO Data Pipeline

Reinforcement Learning from Human Feedback (RLHF) and Direct Preference
Optimization (DPO) require **preference pairs** — two responses to the same
prompt where one is better than the other. Production traces are a rich source
of these.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    RL / DPO Data Pipeline                                │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Source 1: Natural Preference Pairs (from corrections)             │  │
│  │                                                                    │  │
│  │  When a user corrects the agent, we get a free preference pair:    │  │
│  │                                                                    │  │
│  │  User: "Search Jira for all P0 bugs assigned to my team"           │  │
│  │  Agent (rejected): uses wrong JQL syntax, returns no results       │  │
│  │  User: "That's wrong, the project key is AVPC not AV"              │  │
│  │  Agent (chosen): fixes JQL, returns correct results                │  │
│  │                                                                    │  │
│  │  → Preference pair:                                                │  │
│  │    prompt: "Search Jira for all P0 bugs assigned to my team"       │  │
│  │    chosen: correct JQL response                                    │  │
│  │    rejected: incorrect JQL response                                │  │
│  │                                                                    │  │
│  │  These are the HIGHEST quality pairs because they reflect          │  │
│  │  real user preferences in real context.                            │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Source 2: Retry-Based Pairs                                       │  │
│  │                                                                    │  │
│  │  When the agent tries something, fails, and tries again:           │  │
│  │                                                                    │  │
│  │  Turn 1 (rejected): `git push origin main` → permission denied     │  │
│  │  Turn 2 (chosen): `git push origin feature/xyz` → success          │  │
│  │                                                                    │  │
│  │  The successful attempt is the "chosen" response;                  │  │
│  │  the failed attempt is the "rejected" response.                    │  │
│  │  (Context: the trace shows what the agent learned between tries)   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Source 3: LLM-as-Judge Pairs (synthetic augmentation)             │  │
│  │                                                                    │  │
│  │  For gold traces, generate an alternative response using a         │  │
│  │  weaker model or different sampling parameters:                    │  │
│  │                                                                    │  │
│  │  1. Take a gold trace's (prompt, response) as the "chosen"         │  │
│  │  2. Re-run the same prompt through a weaker model → "rejected"     │  │
│  │     OR re-run with temperature=1.5 → "rejected"                    │  │
│  │  3. Use a judge model to verify the chosen is actually better      │  │
│  │                                                                    │  │
│  │  This multiplies preference data volume at the cost of inference.  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Source 4: A/B Test Pairs (advanced)                               │  │
│  │                                                                    │  │
│  │  Periodically run the same prompt through two model versions       │  │
│  │  and let the user (or a judge) pick the better response:           │  │
│  │                                                                    │  │
│  │  Prompt → Model A response | Model B response → User picks A       │  │
│  │                                                                    │  │
│  │  Requires careful UX design to avoid annoying the user.            │  │
│  │  Best implemented as an opt-in "help improve the model" mode.      │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Source 5: NMB Review Loop Pairs (systematic, high volume)         │  │
│  │                                                                    │  │
│  │  The coding + review agent collaboration via NMB (see §5.3)        │  │
│  │  generates systematic preference pairs at every review iteration:  │  │
│  │                                                                    │  │
│  │  Iteration 1: coding agent → diff_v1 → reviewer: request_changes   │  │
│  │  Iteration 2: coding agent → diff_v2 → reviewer: lgtm              │  │
│  │  → diff_v1 = rejected, diff_v2 = chosen                            │  │
│  │                                                                    │  │
│  │  Advantages over other DPO sources:                                │  │
│  │  • Systematic: EVERY multi-iteration review produces pairs         │  │
│  │  • Explained: review.feedback comments provide reasoning           │  │
│  │  • Code-specific: teaches what good code looks like in YOUR style  │  │
│  │  • High volume: runs on every coding task, not just corrections    │  │
│  │  • Structured: NMB audit log has the data in machine-readable      │  │
│  │    format with causal links (id/reply_to)                          │  │
│  │                                                                    │  │
│  │  The review_iterations array in the trace schema (§6) is           │  │
│  │  designed specifically to support this extraction.                 │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Output: DPO Training Dataset                                      │  │
│  │                                                                    │  │
│  │  Format:                                                           │  │
│  │  {                                                                 │  │
│  │    "prompt": "...",                                                │  │
│  │    "chosen": [ { "role": "assistant", "content": "..." } ],        │  │
│  │    "rejected": [ { "role": "assistant", "content": "..." } ]       │  │
│  │  }                                                                 │  │
│  │                                                                    │  │
│  │  Compatible with: NeMo Aligner, Hugging Face TRL DPOTrainer,       │  │
│  │  Axolotl DPO                                                       │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  On-Policy RL (PPO / GRPO — alternative to DPO)                    │  │
│  │                                                                    │  │
│  │  PPO requires a trained reward model. GRPO does NOT — it samples   │  │
│  │  multiple completions per prompt and uses group-relative ranking   │  │
│  │  to compute advantages (no separate reward model needed).          │  │
│  │                                                                    │  │
│  │  Both approaches benefit from production reward signals:           │  │
│  │  • +1.0: explicit positive feedback (thumbs up, "thanks")          │  │
│  │  • +0.5: task completed without errors                             │  │
│  │  • +0.3: user continued conversation (engagement)                  │  │
│  │  •  0.0: neutral (unknown outcome)                                 │  │
│  │  • -0.3: user abandoned conversation                               │  │
│  │  • -0.5: user corrected the agent                                  │  │
│  │  • -1.0: explicit negative feedback (thumbs down, "that's wrong")  │  │
│  │                                                                    │  │
│  │  PPO path: train a reward model on these signals from              │  │
│  │  (prompt, response) pairs, then use it for on-policy RL.           │  │
│  │                                                                    │  │
│  │  GRPO path (simpler): sample N completions per prompt, score each  │  │
│  │  using the signals above as a verifiable reward function, then     │  │
│  │  optimize the policy using group-relative advantages. No reward    │  │
│  │  model training step needed — the production signals ARE the       │  │
│  │  reward. Integrates via Atropos.                                   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 10  The Full Flywheel

Putting it all together — the complete cycle from daily use to model
improvement and back.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    THE FULL FLYWHEEL (end-to-end)                        │
│                                                                          │
│                         ┌──────────────┐                                 │
│                         │  1. DEPLOY   │                                 │
│               ┌────────▶│  Model v(N)  │                                 │
│               │         │  as inference│                                 │
│               │         │  backend     │                                 │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  2. USE      │                                 │
│               │         │  Daily agent │  Users interact with the agent  │
│               │         │  interactions│  via Slack, CLI, Web UI, Cron   │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  3. CAPTURE  │                                 │
│               │         │  Full traces │  Every interaction is logged    │
│               │         │  + feedback  │  with tool calls + outcomes     │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  4. FILTER   │                                 │
│               │         │  Quality     │  Automatic + LLM-as-judge       │
│               │         │  pipeline    │  → gold / silver / bronze       │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  5. GENERATE │                                 │
│               │         │  SFT + DPO   │  Extract training examples      │
│               │         │  datasets    │  from filtered traces           │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  6. TRAIN    │                                 │
│               │         │  Fine-tune   │  SFT → DPO → evaluation         │
│               │         │  Model v(N+1)│  via NeMo Framework / NeMo      │
│               │         │              │  Customizer                     │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│               │                ▼                                         │
│               │         ┌──────────────┐                                 │
│               │         │  7. EVALUATE │                                 │
│               │         │  Compare v(N)│  Run both models on held-out    │
│               │         │  vs v(N+1)   │  traces + synthetic benchmarks  │
│               │         └──────┬───────┘                                 │
│                         │              │                                 │
│                         │              │  v(N+1) better?                 │
│                         │              │                                 │
│               │         YES ───┘                                         │
│               │                                                          │
│               └─────────────────  Deploy v(N+1) as new backend           │
│                                                                          │
│  Cycle time: weeks to months (not real-time)                             │
│  Each cycle: model gets better at YOUR specific tasks                    │
│                                                                          │
│  Key insight: the system is both the product AND the data collection     │
│  platform. Using the agent IS training data generation.                  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Flywheel Acceleration Strategies

| Strategy | Effect | Implementation |
|----------|--------|----------------|
| **Encourage explicit feedback** | Higher quality signals per trace | Slack reactions, periodic prompts |
| **Diversify agent use** | Broader training coverage | Use the agent for varied tasks, not just coding |
| **Synthetic augmentation** | Multiply real traces with variations | LLM-as-judge generates alternative responses |
| **Skill-aware training** | Model learns when to apply which skill | Tag traces with skill usage metadata |
| **Multi-user deployment** | Linear data volume scaling | Each user generates independent traces |
| **Cron-generated traces** | Consistent baseline volume even when user is idle | Background tasks produce tool-calling traces |

---

## 11  Integration with NemoClaw Escapades

How the training flywheel maps to the existing NemoClaw Escapades architecture.

```
┌──────────────────────────────────────────────────────────────────────────┐
│          NemoClaw Escapades — Training Flywheel Integration              │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  EXISTING (from Design Doc + Deep Dives)                           │  │
│  │                                                                    │  │
│  │  • Orchestrator Agent in OpenShell sandbox                         │  │
│  │  • Sub-agents (coding, review, research) in ephemeral sandboxes    │  │
│  │  • NMB for inter-sandbox communication                             │  │
│  │  • Hermes-style self-learning loop (skills + memory)               │  │
│  │  • Session persistence (SQLite FTS5)                               │  │
│  │  • Slack as primary interface                                      │  │
│  │  • Cron-driven background tasks                                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  NEW (training flywheel additions)                                 │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  Trace Collector (middleware in orchestrator agent loop)     │  │  │
│  │  │                                                              │  │  │
│  │  │  Hooks into:                                                 │  │  │
│  │  │  • Orchestrator's run_conversation() → captures all turns    │  │  │
│  │  │  • NMB message bus → captures sub-agent delegations          │  │  │
│  │  │  • Tool dispatcher → captures every tool call + result       │  │  │
│  │  │  • Slack connector → captures user feedback (reactions)      │  │  │
│  │  │  • Session persistence → extends with training metadata      │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  Trace Store (three SQLite databases — see §4)               │  │  │
│  │  │                                                              │  │  │
│  │  │  trace.db   — per-sandbox, inside each sandbox at            │  │  │
│  │  │               /sandbox/data/trace.db                         │  │  │
│  │  │  audit.db   — centralized NMB audit log on broker host       │  │  │
│  │  │               at ~/.nemoclaw/nmb/audit.db                    │  │  │
│  │  │  training.db — merged traces + quality scores + DPO pairs    │  │  │
│  │  │               at ~/.nemoclaw/training/training.db            │  │  │
│  │  │                                                              │  │  │
│  │  │  Retention: indefinite (training data is never discarded)    │  │  │
│  │  │  Backup: periodic export to host via openshell download      │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  Quality Pipeline (cron job, runs daily)                     │  │  │
│  │  │                                                              │  │  │
│  │  │  1. Scan new traces from the past 24h                        │  │  │
│  │  │  2. Apply automatic filters (§7 Stage 1)                     │  │  │
│  │  │  3. Compute quality scores (§7 Stage 2)                      │  │  │
│  │  │  4. Optionally run LLM-as-judge on top candidates (Stage 3)  │  │  │
│  │  │  5. Scrub PII (Stage 4)                                      │  │  │
│  │  │  6. Update trace store with annotations                      │  │  │
│  │  │                                                              │  │  │
│  │  │  Runs as a Hermes-style cron job inside the orchestrator.    │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  Training Data Exporter (on-demand or periodic)              │  │  │
│  │  │                                                              │  │  │
│  │  │  • Queries trace store for training-eligible traces          │  │  │
│  │  │  • Generates SFT dataset (JSONL, chat format)                │  │  │
│  │  │  • Generates DPO dataset (JSONL, preference pairs)           │  │  │
│  │  │  • Applies trajectory compression                            │  │  │
│  │  │  • Balances tool/skill distribution                          │  │  │
│  │  │  • Exports to host filesystem for training pipeline pickup   │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────────────────────────────────────┐  │  │
│  │  │  Training Dashboard (Web UI addition)                        │  │  │
│  │  │                                                              │  │  │
│  │  │  New panel in the Mission Control dashboard:                 │  │  │
│  │  │  • Trace volume over time (chart)                            │  │  │
│  │  │  • Quality distribution (gold/silver/bronze pie chart)       │  │  │
│  │  │  • Tool usage distribution in traces                         │  │  │
│  │  │  • Feedback collection rate                                  │  │  │
│  │  │  • Training dataset readiness indicator                      │  │  │
│  │  │  • Export button → download SFT/DPO datasets                 │  │  │
│  │  └──────────────────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 12  Nemotron Fine-Tuning Target

The training data generated by the flywheel feeds into fine-tuning Nemotron
(or any open-weight model that supports fine-tuning).

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Fine-Tuning Options                                   │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Option A: NeMo Customizer (NVIDIA-managed)                        │  │
│  │                                                                    │  │
│  │  NVIDIA's cloud-hosted fine-tuning service.                        │  │
│  │                                                                    │  │
│  │  Flow:                                                             │  │
│  │  1. Export SFT/DPO dataset from trace store                        │  │
│  │  2. Upload to NeMo Customizer via API                              │  │
│  │  3. Select base model (e.g., Nemotron 3 Super 120B)                │  │
│  │  4. Configure training (LoRA rank, epochs, learning rate)          │  │
│  │  5. Launch fine-tuning job                                         │  │
│  │  6. Evaluate on held-out traces                                    │  │
│  │  7. Deploy fine-tuned model via NIM endpoint                       │  │
│  │  8. Point NemoClaw inference routing at the new endpoint           │  │
│  │                                                                    │  │
│  │  Pros: managed, no GPU needed for training, integrated with NIM    │  │
│  │  Cons: data leaves your machine, cost per training run             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Option B: NeMo Framework (self-hosted)                            │  │
│  │                                                                    │  │
│  │  Run fine-tuning on your own GPU infrastructure.                   │  │
│  │                                                                    │  │
│  │  Flow:                                                             │  │
│  │  1. Export SFT/DPO dataset                                         │  │
│  │  2. Set up NeMo Framework on DGX Spark / Brev GPU instance         │  │
│  │  3. Run SFT: nemo_llm_finetune --model nemotron-3-super            │  │
│  │       --data ./sft_dataset.jsonl --method lora                     │  │
│  │  4. Run DPO: nemo_llm_align --model ./sft_checkpoint               │  │
│  │       --data ./dpo_dataset.jsonl --method dpo                      │  │
│  │  5. Evaluate on held-out traces + synthetic benchmarks             │  │
│  │  6. Deploy via local NIM container                                 │  │
│  │  7. Point NemoClaw at the local endpoint                           │  │
│  │                                                                    │  │
│  │  Pros: data stays local, full control, iterate faster              │  │
│  │  Cons: need GPU(s) for training, more setup                        │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Option C: Hugging Face / Open-Source Stack                        │  │
│  │                                                                    │  │
│  │  Fine-tune using TRL, Axolotl, or LLaMA Factory.                   │  │
│  │                                                                    │  │
│  │  Flow:                                                             │  │
│  │  1. Export SFT/DPO dataset (standard JSONL format)                 │  │
│  │  2. Use any fine-tuning framework:                                 │  │
│  │     • TRL SFTTrainer + DPOTrainer (Hugging Face)                   │  │
│  │     • Axolotl (config-driven, supports LoRA/QLoRA)                 │  │
│  │     • LLaMA Factory (web UI for fine-tuning)                       │  │
│  │  3. Base model: any HF-compatible model (Nemotron, Llama, etc.)    │  │
│  │  4. Deploy via vLLM, TGI, or NIM                                   │  │
│  │                                                                    │  │
│  │  Pros: most flexible, huge community, works with any model         │  │
│  │  Cons: more manual setup, need to handle serving yourself          │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  Recommended path for NemoClaw Escapades:                                │
│                                                                          │
│  Phase 1: Collect traces (no training yet — just build the pipeline)     │
│  Phase 2: SFT with NeMo Customizer or TRL (simplest to start)            │
│  Phase 3: Add DPO alignment using preference pairs from corrections      │
│  Phase 4: Self-hosted NeMo Framework on DGX Spark for full control       │
└──────────────────────────────────────────────────────────────────────────┘
```

### Model Selection for Fine-Tuning

| Base Model | Parameters | Fine-Tuning Feasibility | Notes |
|------------|-----------|------------------------|-------|
| Nemotron 3 Super 120B (MoE, 12B active) | 120B total | LoRA on 1-2 GPUs | Good balance of capability and efficiency; MoE means LoRA is cheap |
| Nemotron 3 Nano 30B (MoE, 3B active) | 30B total | Full fine-tune on 1 GPU | Small enough for fast iteration; good for prototyping the pipeline |
| Nemotron Super 49B v1.5 | 49B | LoRA on 1-2 GPUs | Dense model, strong baseline |
| Nemotron Ultra 253B | 253B | LoRA on 4+ GPUs | Overkill for single-user flywheel; reserve as judge model |

---

## 13  Privacy, Safety & Data Governance

Training on real interactions requires careful handling of sensitive data.

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Privacy & Safety Framework                         │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Principle 1: DATA NEVER LEAVES WITHOUT CONSENT                 │  │
│  │                                                                 │  │
│  │  • Traces are stored locally by default (orchestrator sandbox)  │  │
│  │  • Export to training infrastructure requires explicit action   │  │
│  │  • Cloud-hosted fine-tuning (NeMo Customizer) requires user     │  │
│  │    acknowledgment that data will be uploaded                    │  │
│  │  • No telemetry, no automatic data sharing                      │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Principle 2: PII SCRUBBING IS MANDATORY                        │  │
│  │                                                                 │  │
│  │  Before any trace enters the training pipeline:                 │  │
│  │  • Regex-based detection of emails, API keys, URLs              │  │
│  │  • NER model for names, organizations, locations                │  │
│  │  • Path normalization (remove usernames from file paths)        │  │
│  │  • Template token replacement (<USER>, <EMAIL>, etc.)           │  │
│  │  • Human review for traces that fail automatic scrubbing        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Principle 3: OPT-IN FEEDBACK                                   │  │
│  │                                                                 │  │
│  │  • Users must opt in to the training flywheel                   │  │
│  │  • Feedback collection is never mandatory                       │  │
│  │  • Users can exclude specific conversations from training       │  │
│  │  • A /private command marks the rest of the conversation        │  │
│  │    as non-training-eligible                                     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Principle 4: SAFETY FILTERING                                  │  │
│  │                                                                 │  │
│  │  Training data must not include:                                │  │
│  │  • Traces where the agent produced harmful content              │  │
│  │  • Traces involving credential exposure                         │  │
│  │  • Traces from jailbreak attempts (even failed ones)            │  │
│  │  • Traces containing proprietary code that shouldn't be         │  │
│  │    embedded in model weights                                    │  │
│  │                                                                 │  │
│  │  Safety classifier runs as part of Stage 1 filtering.           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Principle 5: AUDIT TRAIL                                       │  │
│  │                                                                 │  │
│  │  • Every trace records its provenance (who, when, what model)   │  │
│  │  • Every quality annotation is logged with the method used      │  │
│  │  • Every training dataset export records which traces included  │  │
│  │  • Every fine-tuning run records the dataset version used       │  │
│  │  • Full lineage from user interaction to model weight change    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Work vs. Personal Separation (from Design Doc §5 Q16):               │
│                                                                       │
│  • Professional traces (Jira, Gerrit, Slack work channels):           │
│    NEVER exported for external training. May train internal model.    │
│  • Personal/hobby traces (SecondBrain, personal Slack):               │
│    Training-eligible with user consent.                               │
│  • The flywheel respects the same work/personal boundary as the       │
│    rest of the system.                                                │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 14  Cold Start & Bootstrapping

The flywheel needs data to start, but the system needs to work well to
generate good data. Breaking the cold start problem:

```
┌───────────────────────────────────────────────────────────────────────┐
│                    Cold Start Strategy                                │
│                                                                       │
│  Phase 0: PRE-FLYWHEEL (Day 1 - Month 1)                              │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • Use the best available base model (Nemotron 3 Super 120B)    │  │
│  │  • Rely on Hermes-style self-learning loop for runtime          │  │
│  │    improvement (skills, memory — no weight changes)             │  │
│  │  • Start capturing traces from Day 1 (even if quality is low)   │  │
│  │  • Supplement with synthetic traces from batch_runner           │  │
│  │  • Focus on building the trace capture infrastructure           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Phase 1: SEED DATA (Month 1 - Month 3)                               │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • ~1,500-7,000 gold traces accumulated                         │  │
│  │  • Supplement with Hermes environment-generated trajectories    │  │
│  │  • Mix: ~70% real traces + ~30% synthetic (for tool diversity)  │  │
│  │  • First SFT fine-tuning run                                    │  │
│  │  • A/B test: base model vs. fine-tuned on held-out traces       │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Phase 2: ALIGNMENT (Month 3 - Month 6)                               │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • Enough correction-based preference pairs for DPO             │  │
│  │  • Run DPO on top of SFT checkpoint                             │  │
│  │  • Model now understands your specific tool usage patterns      │  │
│  │  • Feedback loop: users notice improvement → more engagement    │  │
│  │    → more traces → more data → better model                     │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Phase 3: STEADY STATE (Month 6+)                                     │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • Periodic fine-tuning cycles (monthly or quarterly)           │  │
│  │  • Each cycle incorporates the latest traces                    │  │
│  │  • Model continually adapts to evolving user needs              │  │
│  │  • Synthetic data decreases as real data volume grows           │  │
│  │  • Optional: on-policy RL with Atropos for further gains        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Bootstrapping shortcuts:                                             │
│  • Import existing Cursor agent transcripts as seed traces            │
│  • Import existing Slack bot conversation logs                        │
│  • Generate synthetic traces using Hermes environments + batch_runner │
│  • Use public tool-calling datasets (e.g., ToolBench, API-Bank)       │
│    as additional SFT data                                             │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 15  Comparison with Other Approaches

| Approach | Data Source | Reward Signal | Scale | Domain Fit |
|----------|-----------|---------------|-------|------------|
| **OpenAI RLHF** | Hired labelers rate outputs | Human preference labels | Massive (millions) | General |
| **Constitutional AI (Anthropic)** | AI self-critique | Model-generated preference | Massive | General + safety |
| **Cursor Real-Time RL** | Production inference tokens | Implicit user behavior (edit persists, dissatisfied follow-up) | Massive (billions of tokens/cycle) | Coding (Composer) |
| **Hermes batch_runner** | Synthetic environments | Programmatic success criteria | Configurable | Tool-calling (synthetic) |
| **Atropos RL** | Task environments | Environment reward function | Configurable | Task-specific |
| **This flywheel** | Real user interactions | User feedback + task outcomes | Small but growing | Highly domain-specific |
| **WebArena / OSWorld** | Web/OS environments | Task completion | Benchmark-scale | Web/OS tasks |

### 15.1  Deep Comparison: Cursor Real-Time RL vs. This Flywheel

Cursor's [real-time RL for Composer](https://cursor.com/blog/real-time-rl-for-composer)
(March 2026) is the closest prior art to what this flywheel proposes. Both
systems use production interactions as training signal. Both observe real user
behavior rather than relying on synthetic benchmarks. Both aim to close the
train-test mismatch by training on the same distribution the model encounters
in deployment.

The differences are instructive — they stem from the fundamental gap between
a company with millions of users and a single-user personal agent system.

**What Cursor does:**

- Serves model checkpoints to production, collects billions of tokens from
  real Composer sessions, distills them into reward signals
- Reward signals are implicit user behavior: "did the edit persist in the
  codebase?" and "did the user send a dissatisfied follow-up?"
- Runs on-policy RL — the model being trained is the same model that
  generated the data
- Ships a new checkpoint **every five hours** — multiple times per day
- A/B tests behind the "Auto" model selector to measure real-world impact
- Reported gains: +2.28% edit persistence, -3.13% dissatisfied follow-ups,
  -10.3% latency

**What we propose:**

- Capture full traces (LLM calls, tool invocations, outcomes) from daily
  agent interactions via per-sandbox `trace.db` + NMB `audit.db`
- Reward signals are a mix of explicit feedback (Slack reactions, user
  corrections) and implicit behavior (task completion, conversation
  abandonment), plus structured review feedback from the review agent loop
- Training is offline and periodic (weeks to months, not hours)
- Fine-tuning via SFT + DPO/GRPO, not on-policy RL (at least initially)
- Single-user system — data volume is orders of magnitude smaller

**Key contrasts:**

| Dimension | Cursor Real-Time RL | NemoClaw Flywheel |
|-----------|--------------------|--------------------|
| **Scale** | Billions of tokens per cycle, millions of users | Hundreds of traces per day, single user |
| **Cycle time** | ~5 hours (new checkpoint multiple times/day) | Weeks to months (periodic fine-tuning) |
| **On-policy vs. off-policy** | Fully on-policy (critical for their approach) | Off-policy (data collected over time, trained in batch) |
| **Reward design** | Implicit behavioral signals (edit persists, follow-up tone) | Explicit + implicit + structured (Slack reactions, review verdicts, task completion) |
| **Reward hacking risk** | High — Cursor reports models learning to emit broken tool calls to avoid negative reward, and deferring edits via clarifying questions | Lower — single user, slower cycle, manual inspection feasible |
| **Train-test mismatch** | Eliminated (same users, same environment) | Mostly eliminated (same user, same tools), but off-policy gap introduces some mismatch |
| **Model ownership** | Cursor trains their own model | We fine-tune an open-weight model (Nemotron) |
| **Specialization** | Moving toward per-organization specialization | Per-user specialization from day 1 (single-user system) |
| **Multi-agent** | Single agent (Composer) | Multi-agent traces (orchestrator + coding + review) with NMB causal chains |
| **Infrastructure** | Massive: client instrumentation, backend data pipelines, fast deployment path, eval suites | Lightweight: SQLite trace DBs, NMB audit log, cron-based merge + export |

**What we can learn from Cursor:**

1. **Reward hacking is real and subtle.** Cursor found that Composer learned
   to emit broken tool calls to avoid negative reward, and learned to defer
   edits by asking clarifying questions. Our quality pipeline (§7) should
   monitor for similar pathologies — e.g., an agent that avoids tool calls to
   keep its "error rate" low, or one that over-asks for clarification.

2. **On-policy matters at scale.** Cursor emphasizes keeping data on-policy
   (the model being trained generates the data). Our flywheel is inherently
   off-policy since we train in batch on accumulated data. This is fine at our
   scale (the distribution shift is small when you're the only user), but
   becomes important if the system grows to multiple users or faster training
   cycles.

3. **Implicit signals can be surprisingly rich.** Cursor's primary signal —
   "did the edit persist?" — is simple but powerful. We should look for
   similar binary signals: did the user undo the agent's action? Did the
   coding agent's PR get merged? Did the review agent's feedback lead to
   accepted changes?

4. **Longer horizons are the frontier.** Cursor notes they're adapting to
   longer background tasks where feedback is less frequent but higher fidelity.
   Our multi-agent flywheel already operates at this longer horizon — a coding
   + review loop may take 10-30 minutes, and the feedback (review verdict) is
   high fidelity. We're ahead of Cursor's current cycle on this axis.

5. **Specialization is the endgame.** Cursor is exploring per-organization
   tailoring. Our flywheel does this from day 1 since it's a single-user
   system — the model adapts to one person's tools, workflows, and
   preferences. This is the extreme case of their specialization vision.

**The fundamental trade-off:**

Cursor has scale (billions of tokens, millions of users, 5-hour cycles) but
trains a general-purpose coding model. We have specificity (one user's exact
workflows, multi-agent traces with causal chains, structured review feedback)
but lack scale. The flywheel's bet is that a small amount of highly relevant
data, combined with an already-capable base model, is enough to produce
meaningful domain-specific improvement.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Base model training (NVIDIA/Nous/Meta):                              │
│    Massive general-purpose data → broad capabilities                  │
│           │                                                           │
│           ▼                                                           │
│  Cursor-style real-time RL (if you have the scale):                   │
│    Billions of production tokens → fast iterative improvement         │
│           │                                                           │
│           ▼                                                           │
│  Flywheel fine-tuning (your data):                                    │
│    Small domain-specific data → specialized capabilities              │
│           │                                                           │
│           ▼                                                           │
│  Result: A model that is generally capable, production-hardened,      │
│  AND specialized to your workflows, tools, and preferences.           │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 16  Open Questions

| # | Question | Notes |
|---|----------|-------|
| T1 | What is the minimum viable dataset size for meaningful SFT on Nemotron? | Likely 1,000-5,000 examples for LoRA; depends on task diversity. |
| T2 | How to handle traces from sub-agents (coding, review) that use different models (e.g., Claude for coding)? | May need model-specific training pipelines, or only train on Nemotron-generated traces. |
| T3 | Should the trajectory compressor run at capture time or export time? | Export time is simpler; capture time saves storage. |
| T4 | How to evaluate whether a fine-tuned model is actually better at tool-calling? | Hold-out traces as eval set + Hermes environments for systematic benchmarks. |
| T5 | Can we use the flywheel to improve skill generation quality? | Yes — train the model to produce better SKILL.md files based on traces of successful skill creations. |
| T6 | What's the right feedback collection rate to avoid annoying the user? | Start with implicit only; add explicit prompts at most 1 per 20 interactions. |
| T7 | Should traces from different users be mixed in training, or kept separate? | Depends on whether the model is personalized per-user or shared. Start with single-user. |
| T8 | How to prevent catastrophic forgetting of general capabilities during fine-tuning? | Use LoRA (modular, preserves base weights), mix in general data, evaluate on broad benchmarks. |
| T9 | How does this relate to the Hermes self-learning loop? | Complementary. Hermes's loop improves the agent at runtime (skills, memory). The flywheel improves the model at training time (weights). Together: runtime adaptation + weight adaptation. |
| T10 | Can Cursor agent transcripts from this project be used as seed data? | Yes, if the format can be converted to the trace schema. These are rich multi-turn tool-calling conversations. |

---

## 17  What to Build for NemoClaw Escapades

### Milestone Mapping

The training flywheel is not a standalone milestone — it layers on top of
existing milestones.

| Core Milestone | Flywheel Addition |
|----------------|-------------------|
| M1 — Foundation | Add trace capture middleware to the orchestrator. Start logging all interactions. |
| M2 — Knowledge Mgmt | Capture SecondBrain tool calls in traces. |
| M3 — Coding Agent | Capture sub-agent delegations and their outcomes. Link parent ↔ child traces. |
| M4 — Self-Learning | Add quality pipeline as a cron job. Add feedback collection via Slack reactions. Add training dashboard to Web UI. |
| M5 — Review Agent | Capture review feedback as preference signals (reviewer corrections = DPO pairs). |
| M6 — Professional KB | Capture Slack/Teams scraping traces for training on information retrieval tasks. |
| **New: M7 — Training Flywheel** | First SFT fine-tuning run. A/B evaluation. Deploy fine-tuned model. Iterate. |

### Implementation Priority

```
┌───────────────────────────────────────────────────────────────────────┐
│  Priority 1 (build immediately, minimal effort):                      │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • Trace capture middleware (instrument the agent loop)         │  │
│  │  • Per-sandbox trace.db (SQLite, extends session persistence)   │  │
│  │  • training.db on host (trace merger output)                    │  │
│  │  • Basic automatic filtering (remove trivial interactions)      │  │
│  │                                                                 │  │
│  │  This costs almost nothing and starts accumulating data from    │  │
│  │  day 1. Even if the training pipeline isn't built yet, the      │  │
│  │  data is being collected.                                       │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Priority 2 (build at M4, moderate effort):                           │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • Quality scoring pipeline (outcome-based + behavioral)        │  │
│  │  • Feedback collection via Slack reactions                      │  │
│  │  • PII scrubbing pipeline                                       │  │
│  │  • SFT data exporter (JSONL chat format)                        │  │
│  │  • Trajectory compressor                                        │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Priority 3 (build at M7, significant effort):                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • DPO preference pair extraction                               │  │
│  │  • LLM-as-judge pipeline                                        │  │
│  │  • Training integration (NeMo Customizer or TRL)                │  │
│  │  • Evaluation harness (held-out traces + synthetic benchmarks)  │  │
│  │  • A/B testing infrastructure                                   │  │
│  │  • Training dashboard in Web UI                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Priority 4 (future, advanced):                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  • On-policy RL with Atropos                                    │  │
│  │  • Multi-user flywheel (federated data collection)              │  │
│  │  • Automated training pipeline (cron-triggered fine-tuning)     │  │
│  │  • Model routing (different fine-tuned models for different     │  │
│  │    task types)                                                  │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### The Key Insight

> **Start capturing traces on Day 1.** The training pipeline can be built
> later, but the data must be collected from the beginning. Every interaction
> you don't capture is training data you'll never get back.

The Hermes self-learning loop improves the **agent** at runtime (better skills,
richer memory, smarter routing). The training flywheel improves the **model**
at training time (better weights, more natural tool use, domain-specific
fluency). Together, they form a compound improvement loop where both the agent
wrapper *and* the model core get better over time.

---

### Sources

- [Cursor: Improving Composer through real-time RL](https://cursor.com/blog/real-time-rl-for-composer) (March 2026 — production RL from inference tokens, reward hacking, on-policy training)
- [NemoClaw Message Bus — Design Document](nmb_design.md) (trace capture via audit log, review loop DPO pairs)
- [NMB Audit Log](nmb_design.md#4--message-broker) (full-payload logging for training data)
- [NMB Review Loop](nmb_design.md#11--revised-coding--review-loop) (systematic DPO pair generation)
- [Hermes Agent — RL / Environments / Training](deep_dives/hermes_deep_dive.md#15--rl--environments--training)
- [Hermes Agent — The Self-Learning Loop](deep_dives/hermes_deep_dive.md#14--the-self-learning-loop)
- [NeMo Framework](https://docs.nvidia.com/nemo-framework/user-guide/latest/)
- [NeMo Customizer](https://docs.nvidia.com/nemo/microservices/customizer/latest/)
- [NeMo Aligner — DPO/PPO/RLHF](https://docs.nvidia.com/nemo-framework/user-guide/latest/modelalignment/)
- [Atropos RL Framework](https://github.com/NousResearch/Atropos)
- [TRL — Transformer Reinforcement Learning (Hugging Face)](https://huggingface.co/docs/trl/)
- [DPO: Direct Preference Optimization (Rafailov et al., 2023)](https://arxiv.org/abs/2305.18290)
- [GRPO: Group Relative Policy Optimization (Shao et al., 2024)](https://arxiv.org/abs/2402.03300)
