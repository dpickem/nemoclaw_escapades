# Build Your Own OpenClaw — Deep Dive

> **Source:** [czl9707/build-your-own-openclaw](https://github.com/czl9707/build-your-own-openclaw)
> (1.1k stars, MIT license)
>
> **Reference implementation:** [czl9707/pickle-bot](https://github.com/czl9707/pickle-bot)
>
> **Last reviewed:** 2026-04-14

---

## Table of Contents

1. [Overview](#1--overview)
2. [Tutorial Structure & Progressive Architecture](#2--tutorial-structure--progressive-architecture)
   - [2.4 Per-Step Mapping: Tutorial Steps → NemoClaw Milestones & Phases](#24-per-step-mapping-tutorial-steps--nemoclaw-milestones--phases)
3. [Core Agent Loop & Tool Execution](#3--core-agent-loop--tool-execution)
4. [Event-Driven Architecture](#4--event-driven-architecture)
5. [Compaction & Context Management](#5--compaction--context-management)
6. [Skill System](#6--skill-system)
7. [Multi-Agent Routing](#7--multi-agent-routing)
8. [Sub-Agent Dispatch](#8--sub-agent-dispatch)
9. [Concurrency Control](#9--concurrency-control)
10. [Prompt Layering](#10--prompt-layering)
11. [Config Hot-Reload](#11--config-hot-reload)
12. [Channel Abstraction & WebSocket](#12--channel-abstraction--websocket)
13. [Cron & Scheduled Tasks](#13--cron--scheduled-tasks)
14. [Persistence & Session Management](#14--persistence--session-management)
15. [Memory System](#15--memory-system)
16. [Explicit Gaps (`GAP.md`)](#16--explicit-gaps-gapmd)
17. [Architecture Comparison: BYOO Tutorial vs NemoClaw](#17--architecture-comparison-byoo-tutorial-vs-nemoclaw)
18. [What to Lift for NemoClaw Escapades](#18--what-to-lift-for-nemoclaw-escapades)

---

## 1  Overview

"Build Your Own OpenClaw" (BYOO) is an 18-step progressive tutorial that builds
a lightweight version of [OpenClaw](https://github.com/openclaw/openclaw) from
scratch. Each step adds one capability — from a bare chat loop to a multi-agent
system with memory, cron, and concurrency control. The companion project
[pickle-bot](https://github.com/czl9707/pickle-bot) is the full reference
implementation that the tutorial distills.

The tutorial is valuable to NemoClaw Escapades for two reasons:

1. **Minimal working implementations** of patterns NemoClaw designs in the
   abstract. The tutorial's `EventBus`, `ContextGuard`, `RoutingTable`,
   `subagent_dispatch`, and `PromptBuilder` are 50–150 line Python
   implementations of concepts that NemoClaw specifies in multi-page design
   docs. They serve as validation that the designs are implementable and
   correctly scoped.

2. **Progressive architecture validation.** The tutorial's 18-step progression
   (chat loop → tools → skills → persistence → compaction → event-driven →
   channels → WebSocket → routing → cron → prompts → dispatch → concurrency →
   memory) mirrors NemoClaw's milestone structure (M1 → M6). The fact that the
   tutorial successfully layers each capability without rewriting prior steps
   validates the NemoClaw milestone ordering.

Key facts:
- Written in Python (94.7%), async throughout
- Uses LiteLLM for model-agnostic inference
- Single-process architecture (no containerization)
- YAML-based configuration with hot-reload
- JSONL-based session persistence
- Event-bus-driven server mode with typed events

---

## 2  Tutorial Structure & Progressive Architecture

### 2.1 Four Phases

| Phase | Steps | Theme | Parallel in NemoClaw |
|-------|-------|-------|---------------------|
| **Phase 1: Capable Single Agent** | 00–06 | Chat loop, tools, skills, persistence, compaction, web tools | M1 (Foundation) |
| **Phase 2: Event-Driven** | 07–10 | Event bus, config hot-reload, channels, WebSocket | M1 (Slack connector, event handling) |
| **Phase 3: Autonomous & Multi-Agent** | 11–16 | Routing, cron, prompt layering, proactive messaging, sub-agent dispatch, concurrency | M2 (sub-agents), M6 (cron, self-learning) |
| **Phase 4: Production & Scale** | 16–17 | Concurrency control, long-term memory | M2 (concurrency), M5 (memory) |

### 2.2 Key Architectural Layers

The final codebase (step 17) has a clean layered architecture:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Transport Layer                                                      │
│  CLI | Telegram | Discord | WebSocket                                 │
│  (Channel ABC + ChannelWorker + WebSocketWorker)                      │
├──────────────────────────────────────────────────────────────────────┤
│  Event Layer                                                          │
│  EventBus (single asyncio.Queue, typed events, subscriber fan-out)    │
│  InboundEvent | OutboundEvent | DispatchEvent | DispatchResultEvent    │
├──────────────────────────────────────────────────────────────────────┤
│  Orchestration Layer                                                  │
│  AgentWorker (dispatch + concurrency semaphores + retry)              │
│  RoutingTable (regex bindings, tiered specificity, session cache)     │
│  CommandRegistry (slash commands, pre-LLM deterministic)              │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Layer                                                          │
│  Agent (factory: loads AgentDef, creates sessions)                    │
│  AgentSession (owns SessionState, tools, context guard, chat loop)    │
│  SessionState (messages + persistence via HistoryStore)                │
├──────────────────────────────────────────────────────────────────────┤
│  Tool Layer                                                           │
│  ToolRegistry (builtins + skill + web + post_message + subagent)      │
│  BaseTool / FunctionTool / @tool decorator                            │
├──────────────────────────────────────────────────────────────────────┤
│  Infrastructure                                                       │
│  Config (user + runtime YAML, hot-reload via watchdog)                │
│  HistoryStore (JSONL index + per-session files)                       │
│  PromptBuilder (5-layer system prompt construction)                   │
│  ContextGuard (token-based compaction with session rolling)           │
│  SharedContext (wires all services together)                           │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.3 The `SharedContext` Pattern

All services are wired through a single `SharedContext` dataclass that is
passed to every component. This avoids global state while keeping dependency
injection simple.

```python
class SharedContext:
    config: Config
    history_store: HistoryStore
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    cron_loader: CronLoader
    command_registry: CommandRegistry
    routing_table: RoutingTable
    prompt_builder: PromptBuilder
    channels: list[Channel[Any]]
    eventbus: EventBus
    websocket_worker: "WebSocketWorker | None"
```

**Lesson for NemoClaw:** This is analogous to NemoClaw's planned dependency
injection through `AgentSetupBundle` (design_m2 §4.8). The tutorial validates
that a single context object is sufficient for wiring — NemoClaw doesn't need a
DI framework.

### 2.4 Per-Step Mapping: Tutorial Steps → NemoClaw Milestones & Phases

The §2.1 table maps BYOO tutorial *phases* to NemoClaw milestones at a high
level. The table below expands that to **every individual step**, and — where
applicable — to the M2 implementation phases (P1–P7) defined in
[design_m2 §14](../design_m2.md#14--implementation-plan). M2 is currently the
only milestone with named implementation phases; M1 deliverables are referenced
without sub-phase granularity.

| Step | Capability Added | BYOO Phase | NemoClaw Milestone | Phase | NemoClaw Parallel |
|------|-----------------|------------|-------------------|-------|-------------------|
| 00 | Bare chat loop | Phase 1 | **M1** | — | Orchestrator loop: receive Slack message → call LLM → reply |
| 01 | Tool execution | Phase 1 | **M1** → **M2a** | M2a P1 | Basic tool system in M1; concurrent `asyncio.gather` extracted into `AgentLoop` in M2a P1 |
| 02 | Skills | Phase 1 | **M2a** | M2a P3 | Basic `SKILL.md` loading via `skill` tool (promoted from M6). Auto-creation deferred to M6. |
| 03 | Persistence | Phase 1 | **M1** | — | In-memory thread history in M1; SQLite `AuditDB` replaces JSONL |
| 04 | Compaction | Phase 1 | **M2a** | M2a P3 | Two-tier compaction: micro (tool truncation) + full (LLM summary + session roll). Promoted from M3. |
| 05 | Web tools | Phase 1 | **M2a** | M2a P2 | File tools (`read_file`, `write_file`, `edit_file`), search tools (`grep`, `glob`), `bash`, git tools |
| 06 | Command registry | Phase 1 | **M1** | — | Approval interface and pre-LLM deterministic routing in M1 |
| 07 | Event bus | Phase 2 | **M2b** | M2b P2–P3 | NMB event loop (P2); at-least-once outbound delivery + crash recovery (P3) |
| 08 | Config hot-reload | Phase 2 | **M1** | — | Config system in M1; runtime hot-reload via two-file pattern is a reference for local dev mode |
| 09 | Channel abstraction | Phase 2 | **M1** | — | `ConnectorBase` ABC + `SlackConnector` (first channel impl) |
| 10 | WebSocket | Phase 2 | **M2b** | — | NMB WebSocket-based inter-sandbox messaging |
| 11 | Multi-agent routing | Phase 3 | **M2b** | M2b P2 | Orchestrator delegation routing; Slack thread → sub-agent binding |
| 12 | Cron & scheduled tasks | Phase 3 | **M2b** | M2b P4 | Basic operational cron promoted from M6. `CRON.md` definitions + self-learning cron remain M6. |
| 13 | Prompt layering | Phase 3 | **M2a** | M2a P3 | Layered `PromptBuilder` with cache boundary; channel hint for sub-agents |
| 14 | Proactive messaging | Phase 3 | **M6** | — | Proactive tick system (`KAIROS` / `PROACTIVE` flags from Claude Code) |
| 15 | Sub-agent dispatch | Phase 3 | **M2b** | M2b P1–P3 | Coding agent entrypoint (P1), delegation + NMB event loop (P2), in-process `LocalDispatcher` (P3) |
| 16 | Concurrency control | Phase 3/4 | **M2b** | M2b P2 | Per-agent `asyncio.Semaphore`, `max_concurrent_tasks`, `max_spawn_depth` caps |
| 17 | Long-term memory | Phase 4 | **M5** | — | Three-layer memory: Honcho (user) + SecondBrain (knowledge) + working memory |

#### Milestone Overlap Matrix

The tutorial's linear 18-step progression scatters across NemoClaw's parallel
milestones. The M2a/M2b split (April 2026) distributes M2's load more evenly.
Skills (step 02), compaction (step 04), and basic cron (step 12) were promoted
forward to match the tutorial's ordering.

```
                    M1        M2a        M2b         M3    M4    M5          M6
                 Foundation  AgentLoop  Multi-Agent Review Notes Memory   Self-Learn
               ─────────── ───────── ────────────  ────── ───── ──────── ──────────
Phase 1 (00-06) ████████░░  ████████  ░░░░░░░░░░
Phase 2 (07-10) ████████░░  ░░░░░░░░  ░░████████
Phase 3 (11-16)             ░░██░░░░  ██████████                         ░░░░██░░░░
Phase 4 (16-17)                       ██░░░░░░░░                ████████
```

Key observations:

1. **M1 draws from Phases 1 and 2** — The foundational chat loop, connector
   abstraction, config system, and persistence all land in NemoClaw's first
   milestone.

2. **M2a draws primarily from Phase 1** — The single-agent capabilities (tools,
   compaction, skills, prompt builder) are concentrated in Phase 1 steps. The
   M2a/M2b split aligns NemoClaw with the tutorial's natural Phase 1 → Phase 3
   boundary.

3. **M2b draws from Phases 2 and 3** — The multi-agent capabilities (event bus,
   routing, dispatch, concurrency, cron) land in M2b, matching the tutorial's
   Phase 3 progression.

4. **M6 is lighter after promotions** — Skills (step 02) and cron (step 12)
   were promoted to M2a/M2b. Only proactive messaging (step 14) remains as a
   Phase 3 step deferred to M6. M6 now focuses on self-learning (auto-skill
   creation, configurable cron, outcome evaluation) rather than basic
   infrastructure.

5. **M3/M4 have no direct tutorial parallels** — The review agent and
   note-taking system are NemoClaw-specific capabilities.

6. **M5 maps to a single step** — The tutorial's file-based memory (step 17)
   is the simplest possible implementation. NemoClaw's three-layer memory
   architecture (M5) is far richer.

#### M2a/M2b Phase Coverage from Tutorial Patterns

The tables below show which BYOO tutorial steps directly informed each
implementation phase after the M2a/M2b split.

**M2a Phases** (single capable agent):

| Phase | Theme | Tutorial Steps | Source |
|-------|-------|---------------|--------|
| M2a P1 | `AgentLoop` + concurrent tool execution | 01 (tools) | §18 High-1 (concurrent execution) |
| M2a P2 | File tools and scratchpad | 05 (web tools) | NemoClaw-specific tool set, validated by tutorial |
| M2a P3 | Compaction + skills + prompt builder | 02 (skills), 04 (compaction), 13 (prompt layering) | §18 High-5 (session-rolling compaction), §18 High-6 (channel hint) |

**M2b Phases** (multi-agent orchestration):

| Phase | Theme | Tutorial Steps | Source |
|-------|-------|---------------|--------|
| M2b P1 | Coding agent process + sub-agent entrypoint | 15 (dispatch) | §18 High-3 (in-process dispatch) |
| M2b P2 | Delegation + NMB + concurrency | 07 (event bus), 11 (routing), 16 (concurrency) | §18 High-2 (per-agent semaphore) |
| M2b P3 | At-least-once delivery + in-process dispatch | 07 (event bus), 15 (dispatch) | §18 High-4 (at-least-once delivery) |
| M2b P4 | `ToolSearch` + basic cron | 12 (cron) | Claude Code / Hermes pattern + BYOO §13 |
| M2b P5 | Polish, hardening, gaps doc | — | §18 Medium-5 (`GAP.md` → `DEFERRED.md`) |

---

## 3  Core Agent Loop & Tool Execution

### 3.1 The Chat Loop

The agent loop is a standard multi-turn tool-use loop:

1. Build messages (system prompt + history)
2. Call LLM via LiteLLM `acompletion`
3. If response has `tool_calls` → execute all concurrently → append results → go to 2
4. If response has text → return text

```python
# Simplified from 17-memory/src/mybot/core/agent.py
async def chat(self, user_message: str) -> str:
    self.state.add_message({"role": "user", "content": user_message})

    while True:
        # Context guard: check token count, compact if needed
        self.state = await self.context_guard.check_and_compact(self.state)

        messages = self.state.build_messages()  # includes system prompt
        response, tool_calls = await self.llm.chat(messages, self.tools.get_tools())

        if tool_calls:
            # Record assistant message with tool_calls
            self.state.add_message(assistant_msg_with_tool_calls)
            await self._handle_tool_calls(tool_calls)
        else:
            self.state.add_message({"role": "assistant", "content": response})
            return response
```

### 3.2 Concurrent Tool Execution

All tool calls within a single turn are executed concurrently via
`asyncio.gather` — no `is_concurrency_safe` flag, no sequential fallback.

```python
async def _handle_tool_calls(self, tool_calls):
    tool_call_results = await asyncio.gather(
        *[self._execute_tool_call(tc) for tc in tool_calls]
    )
    for tool_call, result in zip(tool_calls, tool_call_results):
        self.state.add_message({
            "role": "tool",
            "content": result,
            "tool_call_id": tool_call.id,
        })
```

Error handling: exceptions are caught per-tool and returned as error strings
in the tool message — the loop never crashes from a tool failure.

**Lesson for NemoClaw:** The tutorial proves that concurrent-by-default tool
execution is safe and simple. NemoClaw's planned `is_concurrency_safe` flag adds
defense-in-depth for write tools but the default should be concurrent.

### 3.3 Tool Registration

Tools use a decorator pattern with JSON Schema parameters:

```python
@tool(
    name="skill",
    description=f"Load and invoke a specialized skill. {skills_xml}",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "enum": skill_enum,
                "description": "The name of the skill to load",
            }
        },
        "required": ["skill_name"],
    },
)
async def skill_tool(skill_name: str, session: "AgentSession") -> str: ...
```

Tools receive the `session` object as a keyword argument, giving them access
to the full agent context. This is similar to NemoClaw's planned pattern where
tools receive injected dependencies.

**Adopted in NemoClaw:** The `@tool` decorator has been implemented in
`tools/registry.py` and goes further than the BYOO version — it auto-generates
the JSON Schema from Python type annotations and Google-style docstring `Args:`
sections, eliminating the hand-written `input_schema` dicts entirely. All coding
tools (files, search, bash, git, scratchpad) have been migrated. See
[design_m2.md §14 Phase 2](../design_m2.md#phase-2--file-tools-scratchpad-and-tool-decorator).

---

## 4  Event-Driven Architecture

The tutorial's event-driven refactor (step 07) is one of the most valuable
patterns for NemoClaw.

### 4.1 Event Types

Four typed events carry all communication:

| Event | Direction | Purpose |
|-------|-----------|---------|
| `InboundEvent` | Platform/CLI/cron → agent | User message, cron trigger, retry |
| `OutboundEvent` | Agent → platform | Agent response for delivery |
| `DispatchEvent` | Parent agent → child agent | Sub-agent task assignment |
| `DispatchResultEvent` | Child agent → parent agent | Sub-agent result |

All events carry: `session_id`, `source` (typed `EventSource`), `content`,
`timestamp`, `retry_count`.

### 4.2 EventBus

Single `asyncio.Queue`, serial dispatch, subscriber fan-out via
`asyncio.gather`:

```python
class EventBus(Worker):
    def __init__(self, context):
        self._subscribers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self.pending_dir = context.config.event_path / "pending"

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def _dispatch(self, event: Event) -> None:
        await self._persist_outbound(event)
        await self._notify_subscribers(event)
```

### 4.3 At-Least-Once Outbound Delivery

`OutboundEvent` messages are persisted to disk before delivery and deleted only
after acknowledgement. On crash recovery, pending events are replayed:

```python
async def _persist_outbound(self, event: Event) -> None:
    if not isinstance(event, OutboundEvent):
        return
    # Atomic write: tmp + fsync + rename
    with open(tmp_path, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp_path), str(final_path))

async def _recover(self) -> int:
    """Recover pending events from previous crash."""
    for file_path in self.pending_dir.glob("*.json"):
        event = deserialize_event(json.load(open(file_path)))
        await self._notify_subscribers(event)

def ack(self, event: Event) -> None:
    """Delete persisted event after successful delivery."""
    final_path = self.pending_dir / f"{event.timestamp}_{event.session_id}.json"
    if final_path.exists():
        final_path.unlink()
```

**Lesson for NemoClaw:** NMB should adopt this pattern for critical messages
(`task.complete`, `audit.flush`). The atomic write (tmp → fsync → rename) is
the correct pattern for crash safety.

### 4.4 EventSource Registry

Each event source has a `_namespace` and a `from_string` parser for
serialization/deserialization:

```python
class TelegramEventSource(EventSource):
    _namespace = "telegram"
    chat_id: str

    def __str__(self) -> str:
        return f"telegram:{self.chat_id}"

    @classmethod
    def from_string(cls, value: str) -> "TelegramEventSource":
        return cls(chat_id=value.split(":", 1)[1])
```

This maps to NemoClaw's `ConnectorBase` / `NormalizedRequest` pattern — the
tutorial validates that a namespace:value string is sufficient for routing and
persistence.

### 4.5 Design Trade-offs

| Decision | Tutorial choice | NemoClaw approach | Notes |
|----------|----------------|-------------------|-------|
| Event queue | Single `asyncio.Queue` | NMB (distributed WebSocket) | Tutorial is single-process; NMB supports cross-host |
| Event serialization | JSON to disk | Wire protocol over WebSocket | Tutorial persists for crash recovery; NMB should add similar persistence |
| Subscriber model | In-process `asyncio.gather` | NMB pub/sub channels | Tutorial's fan-out is immediate; NMB adds network hop |
| Backpressure | None (unbounded queue) | Configurable (NMB message limits) | Tutorial avoids complexity; production needs backpressure |

---

## 5  Compaction & Context Management

### 5.1 `ContextGuard`

The tutorial's context management is a two-stage process implemented in
`ContextGuard`:

**Stage 1 — Truncate large tool results:**
```python
def _truncate_large_tool_results(self, messages):
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > self.max_tool_result_chars:  # 10,000 chars
                truncated = content[:self.max_tool_result_chars]
                msg = {**msg, "content": f"{truncated}\n\n[Truncated - original: {len(content)} chars]"}
    return result
```

**Stage 2 — LLM summary + session roll:**
```python
async def compact_and_roll(self, state):
    new_session = state.agent.new_session(state.source)
    # Re-point routing cache to new session
    self.shared_context.routing_table.config_source_session_cache(
        str(state.source), new_session.session_id
    )
    compacted_history = await self._build_compacted_messages(state)
    for message in compacted_history:
        new_session.state.add_message(message)
    return new_session.state
```

### 5.2 Summary Generation

The compaction summarizes the oldest ~50% of messages and keeps the newest ~20%:

```python
def _compress_message_count(self, state):
    keep_count = max(4, int(len(state.messages) * 0.2))
    compress_count = max(2, int(len(state.messages) * 0.5))
    return min(compress_count, len(state.messages) - keep_count)
```

The summary is injected as a synthetic user message + assistant acknowledgement:

```python
messages = [
    {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
    {"role": "assistant", "content": "Understood, I have the context."},
    *state.messages[compress_count:],  # keep recent messages
]
```

### 5.3 Comparison with NemoClaw's Planned Three-Tier Compaction

| Tier | Claude Code | BYOO Tutorial | NemoClaw (planned) |
|------|-------------|---------------|-------------------|
| **Micro** | ~256 tokens, heuristic, no API call | Tool result truncation (10K char cap) | Heuristic pruning (same idea) |
| **Full** | ~4K tokens, LLM summary | LLM summary + session roll | LLM summary (same idea) |
| **Session memory** | Zero-cost key-fact cache | Not implemented | Key-fact cache (planned M3+) |

**Lesson for NemoClaw:** The tutorial's "session roll" pattern (create new
session, copy summary + tail, update routing cache) is a clean way to implement
full compaction. NemoClaw should adopt this for the full-compaction tier rather
than mutating the existing session in place.

---

## 6  Skill System

### 6.1 Skill Definition Format

Skills use `SKILL.md` files with optional YAML frontmatter:

```markdown
---
name: My Skill
description: Does something useful
---

# My Skill

Instructions for the agent...
```

A shared `discover_definitions()` function scans one folder per skill.

### 6.2 Skill Tool (Tool-Based Loading)

Skills are exposed via a single `skill` tool with an enum of available skill IDs:

```python
@tool(
    name="skill",
    description=f"Load and invoke a specialized skill. {skills_xml}",
    parameters={"properties": {"skill_name": {"type": "string", "enum": skill_ids}}},
)
async def skill_tool(skill_name: str, session: "AgentSession") -> str:
    content = skill_loader.load(skill_name)
    session.state.add_message({"role": "user", "content": f"[Skill: {name}]\n{content}"})
    # The next LLM call will see the skill content and follow it
```

### 6.3 Alternative: OpenClaw-Style Prompt Injection

The tutorial's README notes the alternative approach used by OpenClaw: inject
skill metadata into the system prompt and let the model call a `read` tool to
load the full skill content. The tutorial picks the tool-based approach for
self-contained discovery.

**Lesson for NemoClaw:** Both approaches are valid. NemoClaw should start with
the tool-based approach (consistent with the design doc) and add progressive
disclosure (like Hermes's Level 0/1/2) when the skill count grows.

---

## 7  Multi-Agent Routing

### 7.1 Routing Table

The `RoutingTable` uses regex bindings with tier-based specificity:

```python
class Binding:
    agent: str
    value: str
    tier: int  # 0 = literal, 1 = regex without .*, 2 = wildcard

    def __post_init__(self):
        self.pattern = re.compile(f"^{self.value}$")
        self.tier = self._compute_tier()
```

Bindings are sorted by `(tier, declaration_order)` — most specific match wins.
Unmatched sources fall through to `default_agent`.

### 7.2 Session Affinity

When a new source arrives, the routing table resolves an agent, creates a
session, and caches the mapping in `config.runtime.yaml`:

```python
def get_or_create_session_id(self, source: EventSource) -> str:
    source_str = str(source)
    cached = self.context.config.sources.get(source_str)
    if cached:
        return cached.session_id

    agent_id = self.resolve(source_str)
    agent = Agent(agent_def, self.context)
    session = agent.new_session(source)

    # Persist to runtime config
    self.context.config.set_runtime(
        f"sources.{source_str}", SourceSessionConfig(session_id=session.session_id)
    )
    return session.session_id
```

**Lesson for NemoClaw:** The routing table pattern is directly applicable to
the orchestrator's message routing. NemoClaw's Slack connector should use the
same tier-based regex matching for routing messages to sub-agents by
channel/thread pattern.

---

## 8  Sub-Agent Dispatch

### 8.1 The `subagent_dispatch` Tool

The dispatch mechanism is elegant: publish a `DispatchEvent`, subscribe to
`DispatchResultEvent` filtered by session ID, await an `asyncio.Future`:

```python
async def subagent_dispatch(agent_id, task, session, context=""):
    agent = Agent(agent_def, shared_context)
    agent_session = agent.new_session(AgentEventSource(current_agent_id))
    session_id = agent_session.session_id

    result_future = asyncio.get_running_loop().create_future()

    async def handle_result(event: DispatchResultEvent):
        if event.session_id == session_id:
            if not result_future.done():
                result_future.set_result(event.content)

    shared_context.eventbus.subscribe(DispatchResultEvent, handle_result)
    try:
        await shared_context.eventbus.publish(DispatchEvent(
            session_id=session_id,
            source=AgentEventSource(current_agent_id),
            content=user_message,
        ))
        response = await result_future
    finally:
        shared_context.eventbus.unsubscribe(handle_result)

    return json.dumps({"result": response, "session_id": session_id})
```

### 8.2 AgentWorker Dispatch

The `AgentWorker` handles both `InboundEvent` and `DispatchEvent` uniformly.
The only difference is the response type:

```python
async def _emit_response(self, event, content, agent_id, error=None):
    if isinstance(event, DispatchEvent):
        result_event = DispatchResultEvent(...)
    else:
        result_event = OutboundEvent(...)
    await self.context.eventbus.publish(result_event)
```

### 8.3 Design Trade-offs

| Aspect | BYOO Tutorial | NemoClaw NMB |
|--------|---------------|-------------|
| **Isolation** | None (same process, shared `SharedContext`) | Full (separate OpenShell sandboxes) |
| **Latency** | <1ms (in-process `Future`) | ~20-50ms (WebSocket) |
| **Failure domain** | Shared (crash kills parent + child) | Independent (sandbox crash doesn't kill orchestrator) |
| **Scalability** | Single process | Multi-host via NMB |
| **Simplicity** | ~50 lines | ~500+ lines (broker + client + proxy) |

**Lesson for NemoClaw:** The tutorial's dispatch pattern should be the
**development-mode fallback** for NemoClaw. When running without OpenShell (e.g.
local dev, testing), use in-process dispatch with the same logical flow. The
NMB path activates for production with sandbox isolation.

---

## 9  Concurrency Control

### 9.1 Per-Agent Semaphore

The `AgentWorker` maintains a `dict[agent_id, Semaphore]`:

```python
class AgentWorker:
    def __init__(self, context):
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def exec_session(self, event, agent_def):
        sem = self._get_or_create_semaphore(agent_def)
        async with sem:
            # ... execute session ...
        self._maybe_cleanup_semaphores(agent_def)

    def _get_or_create_semaphore(self, agent_def):
        if agent_def.id not in self._semaphores:
            self._semaphores[agent_def.id] = asyncio.Semaphore(
                agent_def.max_concurrency
            )
        return self._semaphores[agent_def.id]

    def _maybe_cleanup_semaphores(self, agent_def):
        if agent_def.id in self._semaphores:
            if not self._semaphores[agent_def.id]._waiters:
                del self._semaphores[agent_def.id]
```

### 9.2 Retry on Failure

Failed sessions retry up to 3 times by republishing a modified event:

```python
if event.retry_count < MAX_RETRIES:
    retry_event = replace(event, retry_count=event.retry_count + 1, content=".")
    await self.context.eventbus.publish(retry_event)
```

### 9.3 Gaps

- No per-user concurrency limits
- No priority queuing
- No timeout enforcement
- No cross-agent budget tracking

**Lesson for NemoClaw:** The per-agent semaphore is the right primitive for M2.
NemoClaw should add `max_concurrent_tasks` to the agent config and enforce via
semaphore before sandbox creation. Priority and budgets are M3+ concerns.

---

## 10  Prompt Layering

### 10.1 Five-Layer System Prompt

The `PromptBuilder` constructs the system prompt from five layers:

```python
class PromptBuilder:
    def build(self, state: "SessionState") -> str:
        layers = []
        layers.append(state.agent.agent_def.agent_md)          # Layer 1: Identity (AGENT.md)
        if state.agent.agent_def.soul_md:
            layers.append(f"## Personality\n\n{soul_md}")       # Layer 2: Soul (optional)
        bootstrap = self._load_bootstrap_context()
        if bootstrap:
            layers.append(bootstrap)                            # Layer 3: Bootstrap + AGENTS.md + crons
        layers.append(self._build_runtime_context(agent_id))    # Layer 4: Runtime (agent ID, timestamp)
        layers.append(self._build_channel_hint(state.source))   # Layer 5: Channel hint
        return "\n\n".join(layers)
```

### 10.2 Channel Hint Layer

The channel hint tells the agent how its response will be delivered:

```python
def _build_channel_hint(self, source):
    if source.is_cron:
        return "You are running as a background cron job. Your response will not be sent to user directly."
    if source.is_agent:
        return "You are running as a dispatched subagent. Your response will be sent to main agent."
    elif source.is_platform:
        return f"You are responding via {source.platform_name}."
```

**Lesson for NemoClaw:** This directly validates the planned system prompt
construction in [Orchestrator Design §4](../orchestrator_design.md#4--system-prompt-construction).
The channel hint is essential for sub-agents — they need to know they're
responding to a parent agent, not a user.

---

## 11  Config Hot-Reload

### 11.1 Two-File Config System

The tutorial separates durable user config from ephemeral runtime state:

- `config.user.yaml` — User-edited, watched by `watchdog` for hot-reload
- `config.runtime.yaml` — Programmatically written (session caches, routing
  bindings, default delivery source)

Deep merge: runtime overrides user, nested dicts merged recursively:

```python
@classmethod
def _load_merged_configs(cls, workspace_dir):
    config_data = {}
    if user_config.exists():
        config_data = cls._deep_merge(config_data, yaml.safe_load(f))
    if runtime_config.exists():
        config_data = cls._deep_merge(config_data, yaml.safe_load(f))
    return config_data
```

### 11.2 Hot-Reload via Watchdog

On `config.user.yaml` change, `Config.reload()` replaces all fields on the
**same** Config instance (in-place setattr), so all components holding a
reference to the config see the updated values immediately.

### 11.3 Design Trade-offs

| Aspect | BYOO Tutorial | NemoClaw |
|--------|---------------|----------|
| **Durable config** | `config.user.yaml` (watched) | Config system (orchestrator-managed) |
| **Ephemeral state** | `config.runtime.yaml` (programmatic) | In-memory + NMB state |
| **Hot-reload trigger** | Filesystem watch (watchdog) | Signal / API call |
| **Concurrency** | Single-process, no locking | Multi-process needs coordination |

**Lesson for NemoClaw:** The two-file split is sound. For local development
mode, NemoClaw should support the same pattern. For production (multi-sandbox),
use the orchestrator's config system with NMB-based propagation.

---

## 12  Channel Abstraction & WebSocket

### 12.1 Channel ABC

```python
class Channel(ABC, Generic[T]):
    async def run(self, on_message: Callable) -> None: ...
    async def reply(self, source: EventSource, content: str) -> None: ...
    def is_allowed(self, source: EventSource) -> bool: ...
    async def stop(self) -> None: ...
```

`from_config` builds a list of platform channels from config. The
`ChannelWorker` maps platform → channel, enforces whitelists, and publishes
`InboundEvent`.

### 12.2 WebSocket Integration

A FastAPI app exposes `/ws` only (no REST API — intentional gap). The
`WebSocketWorker` validates JSON messages, creates `WebSocketEventSource`,
resolves routing, and publishes `InboundEvent`. Responses are broadcast to
all connected WebSocket clients.

### 12.3 First-Message Session Binding

The first non-CLI platform message sets the `default_delivery_source` in
runtime config, ensuring all outbound events (including cron responses) are
delivered to the correct platform.

**Lesson for NemoClaw:** NemoClaw's `ConnectorBase` ABC maps to the tutorial's
`Channel` ABC. The "first message binds the session" pattern is relevant for
Slack thread management — the first message in a thread should bind it to the
correct sub-agent.

---

## 13  Cron & Scheduled Tasks

### 13.1 CRON.md Definition

Cron jobs use `CRON.md` files with YAML frontmatter:

```markdown
---
name: Daily Summary
description: Summarize daily activity
schedule: "0 9 * * *"
agent: default
---

Generate a summary of yesterday's activity...
```

### 13.2 CronWorker

- Sleeps 60 seconds between checks
- Uses `croniter.match` to find due jobs
- Publishes `DispatchEvent` with the cron body as content
- Enforces minimum 5-minute schedule granularity
- Supports `one_off` jobs (delete folder after execution)

### 13.3 Cron Operations as a Skill

Rather than adding cron management tools, the tutorial exposes cron operations
as a SKILL that the agent reads and follows. This avoids tool proliferation.

**Lesson for NemoClaw:** NemoClaw's planned cron system (M6) should follow this
pattern: `CRON.md` definitions with `DispatchEvent` triggers. The "cron-ops as
skill" approach is clever for reducing tool count.

---

## 14  Persistence & Session Management

### 14.1 Storage Layout

```
.history/
├── index.jsonl          # session metadata (id, title, agent_id, timestamps)
└── sessions/
    └── {session_id}.jsonl  # per-session message history
```

### 14.2 Behavior

- Every `SessionState.add_message()` appends to both the session file and
  updates the index
- Title is set from the first user message
- Sessions are sorted by `updated_at` for recency
- Resume loads messages from file and rebuilds `SessionState`

**Lesson for NemoClaw:** NemoClaw uses SQLite for session persistence (matching
Hermes). The JSONL approach is simpler but less queryable. The tutorial validates
that per-session files + index is sufficient for single-process use.

---

## 15  Memory System

### 15.1 Memory as a Specialized Agent

The tutorial's memory system (step 17) positions long-term memory as a
**separate agent** (e.g. "cookie") reachable via `subagent_dispatch`, with
files stored under `memories/`. This is not a vector store or database — it's
orchestration + filesystem by convention.

### 15.2 Alternatives Considered (from README)

The tutorial's README discusses multiple memory approaches:

| Approach | Pros | Cons |
|----------|------|------|
| File-based (tutorial choice) | Simple, transparent, agent-readable | No semantic search, no indexing |
| Vector DB (e.g. ChromaDB) | Semantic search, relevance ranking | Extra dependency, operational overhead |
| Database (e.g. SQLite) | Queryable, structured | Schema design, migration overhead |
| External service (e.g. Honcho) | Managed, user modeling | Vendor dependency |

**Lesson for NemoClaw:** NemoClaw's planned three-layer memory (working memory +
user memory via Honcho + knowledge memory via SecondBrain) is more sophisticated.
The tutorial validates that file-based memory is a viable starting point for M2
scratchpad persistence before the full memory system is built in M5.

---

## 16  Explicit Gaps (`GAP.md`)

The tutorial maintains a `GAP.md` documenting features intentionally excluded
from the tutorial vs pickle-bot:

| Gap | Why excluded |
|-----|-------------|
| Template substitution (`{{variable}}` in SKILL/AGENT.md) | Tutorial focuses on core concepts; users hard-code paths |
| REST API endpoints | WebSocket-only keeps tutorial focused; REST is production polish |

**Lesson for NemoClaw:** Maintain a per-milestone `DEFERRED.md` or equivalent
tracking features explicitly punted to future milestones. This prevents scope
creep and makes "not in scope" decisions visible to contributors.

---

## 17  Architecture Comparison: BYOO Tutorial vs NemoClaw

| Dimension | BYOO Tutorial | NemoClaw M1/M2 | Delta |
|-----------|---------------|----------------|-------|
| **Process model** | Single process, single event loop | Multi-process (orchestrator + sandboxed sub-agents via OpenShell) | NemoClaw has stronger isolation but higher complexity |
| **Inter-agent comms** | In-process `EventBus` with `Future`-based rendezvous | NMB (WebSocket, cross-host) | NemoClaw supports multi-host; tutorial is local-only |
| **Tool execution** | Concurrent by default (`asyncio.gather`) | Sequential (M2); concurrent deferred | Tutorial is ahead; NemoClaw should match |
| **Context compaction** | `ContextGuard`: token threshold → truncate → LLM summary → session roll | Planned three-tier (micro/full/session memory) | Tutorial has working implementation; NemoClaw has richer design |
| **Routing** | Regex bindings with tier-based specificity | Slack thread → sub-agent mapping (planned) | Similar concept; tutorial has working implementation |
| **Concurrency** | Per-agent `asyncio.Semaphore` | Per-workflow asyncio tasks; no caps yet | Tutorial has working implementation; NemoClaw should lift |
| **Persistence** | JSONL (index + per-session files) | SQLite (AuditDB) | NemoClaw is more queryable and concurrent-safe |
| **Config** | YAML with hot-reload (user + runtime files) | Python config system | Tutorial's hot-reload is more dynamic |
| **Prompt construction** | 5-layer PromptBuilder | System prompt with cache boundary (planned) | Similar concept; NemoClaw adds cache optimization |
| **Audit** | Logging only | Full audit DB (tool calls + inference calls + NMB messages) | NemoClaw is far more comprehensive |
| **Crash recovery** | Outbound event persistence + replay | NMB reliability (planned) | Tutorial has working pattern; NemoClaw should adopt |
| **Skills** | `SKILL.md` with tool-based loading | `SKILL.md` with tool-based loading (planned) | Nearly identical |
| **Cron** | `CRON.md` with `DispatchEvent` triggers | Planned (M6) | Tutorial has working implementation |
| **Memory** | File-based via specialized agent | Three-layer: Honcho + SecondBrain + working memory (planned M5) | NemoClaw is far richer |
| **Web UI** | WebSocket-only, no REST | Mission Control Dashboard (planned) | NemoClaw is more ambitious |
| **Sandbox isolation** | None (single process) | Full (OpenShell: Landlock + seccomp + network policy) | NemoClaw's key differentiator |
| **Model agnostic** | Yes (via LiteLLM) | Yes (via pluggable BackendBase) | Both achieve this |

---

## 18  What to Lift for NemoClaw Escapades

### High Priority — Lift for M2

| Pattern | Source | Adaptation for NemoClaw | Status |
|---------|--------|----------------------|--------|
| **`@tool` decorator with auto-schema** | `@tool` decorator in `tools/base.py` | Extended with auto-schema from type hints + docstrings; all NemoClaw `ToolSpec` metadata supported as kwargs | **Done** — `tools/registry.py`, all coding tools migrated |
| **Concurrent tool execution** | `_handle_tool_calls` with `asyncio.gather` | Default to concurrent; add `is_concurrency_safe=False` only for write tools that mutate shared state | Designed (§4.2) |
| **Per-agent semaphore concurrency** | `AgentWorker._semaphores` | Add `max_concurrent_tasks` to agent config; enforce in delegation module before `openshell sandbox create` | Designed (Phase 4) |
| **In-process dispatch for dev mode** | `subagent_dispatch` with `Future`-based rendezvous | When OpenShell is unavailable, use same logical flow as NMB but in-process | Designed (Phase 5) |
| **At-least-once outbound delivery** | `EventBus._persist_outbound` + `ack()` + `_recover()` | Adopt for NMB `task.complete` and `audit.flush` messages | Designed (Phase 5) |
| **Session-rolling compaction** | `ContextGuard.compact_and_roll` | Use as the full-compaction tier: create new session, copy summary + tail, update routing | Designed (deferred to M3) |
| **Channel hint in system prompt** | `PromptBuilder._build_channel_hint` | Sub-agents need to know they're responding to a parent, not a user | Designed (Phase 3) |

### Medium Priority — Lift for M3+

| Pattern | Source | Adaptation for NemoClaw |
|---------|--------|----------------------|
| **Tool result truncation** | `ContextGuard._truncate_large_tool_results` | Use as the micro-compaction tier (no API call) |
| **Regex routing with tier specificity** | `RoutingTable` with `Binding.tier` | Apply to Slack channel/thread → sub-agent routing |
| **Retry on failure** | `AgentWorker` with `retry_count` + event republish | Add to NMB dispatch: retry `task.assign` up to N times |
| **CRON.md definitions** | `CronLoader` + `CronWorker` | Adopt for M6 cron system: definition files + `DispatchEvent` triggers |
| **`GAP.md` / `DEFERRED.md`** | Tutorial practice | Track per-milestone deferred features to prevent scope creep |

### Lower Priority — Reference for Future Milestones

| Pattern | Source | Relevance |
|---------|--------|-----------|
| **Two-file config (user + runtime)** | `Config._load_merged_configs` | Useful for local development mode |
| **Skill tool with enum** | `create_skill_tool` | Starting point for skill loading; evolve to progressive disclosure. `@tool` decorator already adopted. |
| **EventSource namespace registry** | `EventSource._namespace` + `from_string` | Reference for connector type system |
| **Memory as specialized agent** | Step 17 README | Alternative to dedicated memory service; useful for quick prototyping |

### Explicitly Not Lifting

| Pattern | Reason |
|---------|--------|
| **Single-process architecture** | NemoClaw requires multi-sandbox isolation for security |
| **JSONL persistence** | SQLite is more appropriate for concurrent access and querying |
| **No audit trail** | NemoClaw's audit DB is a core requirement for the training flywheel |
| **WebSocket-only server** | NemoClaw needs REST APIs for the Mission Control Dashboard |
| **No template substitution** | NemoClaw skills will use template variables for path injection |

---

### Sources

- [Build Your Own OpenClaw](https://github.com/czl9707/build-your-own-openclaw)
  (source, 1.1k stars, MIT)
- [pickle-bot](https://github.com/czl9707/pickle-bot) (reference implementation)
- [NemoClaw Design Document](../design.md)
- [NemoClaw M2 Design](../design_m2.md)
- [Orchestrator Design](../orchestrator_design.md)
- [NMB Design](../nmb_design.md)
- [OpenClaw Deep Dive](openclaw_deep_dive.md)
- [Hermes Deep Dive](hermes_deep_dive.md)
- [Claude Code Deep Dive](claude_code_deep_dive.md)
