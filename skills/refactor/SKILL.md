---
name: Refactoring
description: Improve the structure of existing code without changing its external behaviour
---

# Refactoring Skill

You are refactoring existing code.  The central rule:

> **Refactoring never changes observable behaviour.**  If the tests
> passed before, they must pass after.

If a refactor reveals a bug, raise it separately — don't silently
"fix" it as part of the refactor.

## Before you touch anything

1. **Understand the current tests.**  Run them; confirm they pass.
   Skim the test file to learn what behaviour is actually pinned
   down.  If coverage is thin in the area you're touching, stop and
   add characterisation tests first.
2. **Identify the smell.**  Be specific — don't refactor
   aesthetically.  Common legitimate triggers:
   - Duplicated logic across 3+ call sites.
   - A function or class that does two unrelated things.
   - Shotgun surgery: a single conceptual change requires edits in
     many files.
   - Stale abstractions (layer of indirection that no longer earns
     its keep).
3. **State the goal.**  Write it in the scratchpad: "Extract the X
   logic from Y into a standalone Z so that W can reuse it."

## While refactoring

- **One kind of change per commit.**  Rename OR extract OR inline OR
  reorganise — not all at once.  Mixing makes the diff unreadable.
- **Run tests after every meaningful step.**  A green baseline
  between each transformation lets you bisect a broken commit.
- **Keep the public API stable** unless the task explicitly
  authorises breaking it.  Callers depend on it.
- **Preserve comments and docstrings** that explain *why*.  Delete
  comments that merely restate *what* the code does — those were
  dead weight before the refactor too.

## Common refactoring patterns

- **Extract function** — pull a coherent block of logic out with a
  descriptive name.
- **Rename** — if the old name is misleading.  Do this in a dedicated
  commit so reviewers can see the rename without other noise.
- **Inline** — remove a one-call-site helper that adds no clarity.
- **Move** — relocate a function/class to the module it actually
  belongs in.
- **Replace conditional with polymorphism** — when you see a
  type-switch statement that keeps growing.

## Stop and ask when

- Tests are missing in the area you need to refactor.  Add them first
  — don't refactor unpinned code.
- The refactor is ballooning beyond the original scope.  Smaller
  refactors land; bigger ones rot in a feature branch.
- You discover a bug mid-refactor.  Finish the current green step,
  commit, then fix the bug in a separate commit.

## Tone in the summary

When you're done, explain **what shape the code had before** and
**what shape it has now**, and why the new shape is easier to change
or reason about.  A good refactor justifies itself in a paragraph.
