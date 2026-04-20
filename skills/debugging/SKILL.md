---
name: Debugging
description: Systematically investigate a bug or unexpected behaviour to identify the root cause
---

# Debugging Skill

You are debugging a reported issue.  Your goal is to find the **root
cause** — not to write a patch.  A confidently wrong fix is worse than
no fix at all.

## Required process

Follow these steps in order.  Don't jump to step 4 without finishing
steps 1–3.

### 1. Clarify the symptom

Before touching any code, make sure you understand:

- What was the **expected** behaviour?
- What **actually** happened?
- Is the failure **deterministic** or intermittent?
- What are the **minimum reproduction steps**?  (If the user hasn't
  given you these, ask — don't guess.)

Write this down in your notes file (see the `scratchpad` skill) under
a `## Symptom` section.

### 2. Gather evidence

Use the available tools to collect runtime evidence before forming
hypotheses:

- `read_file` / `grep` — inspect the code path the symptom points to.
- `bash` (if available) — run the failing test, reproduce the bug,
  collect logs.
- `git_log` / `git_diff` — identify recent changes that could have
  introduced the regression (`git log --since=...`, `git blame`).

Record every non-trivial observation in your notes under
`## Evidence`.

### 3. Form and test hypotheses

List the top 2–3 candidate causes in your notes under
`## Hypotheses`.  For each, note how you'll **falsify** it (not just
confirm it — confirmation bias is the debugger's nemesis).

Eliminate hypotheses one at a time using the tools.  When a
hypothesis is confirmed, note the specific evidence that proved it
(log line, failing assertion, git commit).

### 4. State the root cause

Only after you have evidence that eliminates competing hypotheses,
write a clear **`## Root cause`** section stating:

- What code is responsible.
- Why it produces the observed symptom.
- Under what conditions the bug triggers.

### 5. Propose a fix (optional)

If asked, propose a fix in a separate `## Fix` section.  Keep it
minimal — a surgical change that addresses the root cause, not a
refactor.  Include the specific files + line ranges that need to
change.

## Anti-patterns to avoid

- **Pattern matching without evidence.** "It looks like X, so it must
  be X" is a guess, not a diagnosis.
- **Fixing symptoms not causes.** Catching and swallowing the
  exception is not a fix.
- **Scope creep.** Don't refactor adjacent code while debugging.
- **Stopping at the first plausible explanation.** Keep going until
  you've falsified the alternatives.
