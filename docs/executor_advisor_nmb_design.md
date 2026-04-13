# Local Advisor Pattern over NMB - Design

> **Status:** Proposed
>
> **Last updated:** 2026-04-10
>
> **Related:**
> [Orchestrator Design](orchestrator_design.md) |
> [NMB Design](nmb_design.md) |
> [NMB Integration Tests](nmb_integration_tests_design.md)

---

## 1. Overview

This document describes how to replicate an "executor + advisor" model
locally inside NemoClaw Escapades without relying on an external advisor
service.

- **Executor:** the existing orchestrator process remains the single agent
  that owns user interaction, tool execution, approval gating, and final response.
- **Advisor:** a separate sandboxed process that provides strategic guidance
  over NMB request/reply.
- **Transport:** NMB (`messages.local:9876`) is the only communication path.

The advisor is **consultative**, not authoritative: executor decisions, tool
calls, and safety gates remain unchanged.

---

## 2. Goals and Non-Goals

### Goals

1. Keep the current orchestrator as the executor.
2. Add optional advisor consultation via a separate sandbox.
3. Preserve fail-open behavior (executor continues if advisor is unavailable).
4. Preserve existing approval and tool safety boundaries.
5. Keep the design compatible with current NMB primitives.

### Non-Goals

1. Replacing the orchestrator with a planner-only coordinator.
2. Allowing advisor to execute tools directly in executor scope.
3. Introducing new external dependencies or hosted services.
4. Redesigning the current connector or backend interfaces in this phase.

---

## 3. High-Level Architecture

```
User -> Slack Connector -> Orchestrator (Executor)
                              |
                              |  NMB request/reply
                              v
                        Advisor Sandbox
                              |
                              v
                         NMB Broker
```

### Responsibilities

- **Executor (orchestrator):**
  - Build user-facing conversation state.
  - Decide when to consult advisor.
  - Integrate advisor guidance into model context.
  - Execute tools and apply write-approval gate.
  - Return final user response.

- **Advisor sandbox:**
  - Consume consultation requests.
  - Return concise strategy/risk guidance.
  - Never mutate user systems directly.

- **NMB broker:**
  - Authenticated message routing, timeout handling, audit trail.

---

## 4. Control Flow

## 4.1 Default executor turn

1. Executor builds inference context.
2. Executor optionally calls advisor over NMB.
3. If advice is returned, executor injects it as transient guidance context.
4. Executor calls its model backend and runs normal tool loop.
5. Executor commits final turn and responds to user.

## 4.2 Advisor fail-open behavior

If advisor call times out, target is offline, or response is invalid:

- Log warning with correlation metadata.
- Continue executor turn without advisor context.
- Do not surface transport details to end user.

---

## 5. NMB Protocol Contract

Use request/reply with explicit message types.

### Request

- **type:** `advisor.consult`
- **to:** advisor sandbox id (resolved dynamically; see Section 6)
- **payload fields:**
  - `request_id` (executor correlation id)
  - `thread_key`
  - `round` (agent-loop round)
  - `objective` (optional short task summary)
  - `messages` (bounded trailing message window)
  - `constraints` (e.g., no unsafe writes without approval)

### Reply

- **type:** `advisor.advice`
- **payload fields:**
  - `advice` (required string; concise actionable guidance)
  - `confidence` (optional 0-1)
  - `risk_flags` (optional list)
  - `notes` (optional)

### Validation rules

- If `advice` is missing or non-string, treat as invalid and ignore.
- Non-matching reply `type` should be tolerated if payload is valid.
- Use bounded payload size and bounded context window.

---

## 6. Advisor Discovery and Routing

NMB client sandbox IDs are unique per launch (suffix-appended), so static IDs
are brittle. Use a small registration protocol.

### Proposed discovery pattern

1. Advisor subscribes to `advisor.registry`.
2. Advisor publishes heartbeat:
   - `type: advisor.heartbeat`
   - payload includes `sandbox_id`, `role`, `version`, `capabilities`, `ts`.
3. Executor maintains in-memory advisor routing table keyed by `role`.
4. Executor selects active advisor target by:
   - most recent heartbeat,
   - optional capability match.

### Fallback option

For early prototyping, allow explicit `ADVISOR_SANDBOX_ID` override.

---

## 7. Executor Integration Points

Integrate advisor consultation at inference boundaries, not connector boundaries.

### Primary insertion points

1. Before each model call in tool-use loop.
2. Before continuation retries for truncated responses.

### Context injection strategy

- Append advisor guidance as a transient system message:
  - included in inference request,
  - not persisted in long-term thread history.
- Prefix guidance with explicit semantics:
  - consultative only,
  - executor must verify against tools/policies/current state.

This keeps executor behavior deterministic and minimizes history pollution.

---

## 8. Advisor Sandbox Design

Advisor can start simple and evolve.

### v1 behavior

- Single-purpose loop:
  - receive `advisor.consult`,
  - produce short strategy guidance,
  - reply `advisor.advice`.
- No write tools.
- Optional read-only tools later (behind explicit approval policy if needed).

### Prompt contract (advisor)

Advisor output should include:

1. Recommended next step.
2. Key risk or uncertainty.
3. Suggested verification check.

Avoid generating end-user prose; optimize for executor decision support.

---

## 9. Safety and Policy Model

Safety boundaries remain executor-centric.

- Executor is still the only component that:
  - invokes mutable tools,
  - applies write approval gate,
  - returns final user output.
- Advisor is untrusted guidance input.
- Advisor sandbox policy should be stricter than executor policy:
  - NMB access required,
  - only minimal external network access if any.

---

## 10. Observability and Audit

Add advisor-specific telemetry to existing logs/audit views.

### Required events

1. `advisor_request_sent`
2. `advisor_response_received`
3. `advisor_timeout`
4. `advisor_invalid_response`
5. `advisor_injection_applied`

### Useful dimensions

- `request_id`, `thread_key`, `round`
- advisor `sandbox_id`
- latency ms
- guidance length
- executor outcome category (success/repair/error)

---

## 11. Failure Modes

1. **Advisor offline**
   - Executor continues without guidance.
2. **Advisor timeout**
   - Executor continues; emit timeout metric.
3. **Malformed advisor payload**
   - Ignore payload; log parse issue.
4. **NMB broker disruption**
   - Same as advisor unavailable for this feature path.
5. **Conflicting advice**
   - Executor prompt contract explicitly treats advice as optional input.

---

## 12. Test Plan

## 12.1 Unit tests

1. Advice injected when valid response is returned.
2. No injection on timeout/offline/invalid payload.
3. Context window truncation works as configured.
4. Guidance is transient (not persisted in thread history).

## 12.2 Integration tests (NMB harness)

1. Executor requests advisor and receives advice.
2. Advisor crash mid-turn still yields executor completion.
3. Multiple rounds with consultation do not break approval flow.
4. Discovery heartbeat picks latest advisor instance.

## 12.3 Regression tests

1. Existing tool-loop tests pass with advisor disabled.
2. Existing approval-gate tests remain unchanged.

---

## 13. Rollout Plan

### Phase 0 - Design only

- Finalize protocol and prompt contracts.

### Phase 1 - Feature-flagged consultation

- Disabled by default.
- Static advisor target id allowed for local bring-up.

### Phase 2 - Discovery and heartbeats

- Replace static target with registry-based resolution.

### Phase 3 - Triggered consultation policies

- Shift from "every round" to selective consultation:
  - first round only,
  - before write candidates,
  - after repeated tool failures.

---

## 14. Open Questions

1. Should advisor always be consulted, or only on specific triggers?
2. Should advisor be allowed read-only tools in v1?
3. Do we want one advisor role or multiple advisor specialties?
4. Should advisor guidance be exposed to user for transparency/debugging?
5. What SLO should advisor consultation meet (p95 latency budget)?

---

## 15. Decision Summary

Recommended direction:

1. Keep orchestrator as executor and source of truth.
2. Add advisor as optional NMB consult service in a separate sandbox.
3. Enforce fail-open executor behavior and unchanged approval boundaries.
4. Introduce discovery-based routing for robustness against sandbox id churn.
5. Validate with NMB harness integration tests before enabling by default.
