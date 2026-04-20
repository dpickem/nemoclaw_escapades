---
name: Code Review
description: Perform a structured, constructive code review of the provided changes
---

# Code Review Skill

You are performing a code review.  Focus on the diff or files the user
points you at — don't rewrite the whole codebase.  Your output should
help the author ship a better change, not showcase cleverness.

## Review checklist

Walk through the following dimensions in order.  Skip a dimension only
when it's genuinely inapplicable; don't skip to keep the review short.

1. **Correctness.** Does the code do what the commit message / PR
   description claims?  Look for off-by-one errors, unhandled edge
   cases (empty input, None, concurrent modification), incorrect
   error handling, and silent exception swallowing.
2. **Safety.** Flag anything that could corrupt data, leak secrets,
   execute untrusted input, or create race conditions.  Look
   specifically at: SQL / shell / template injection, credential
   handling, unchecked user input reaching filesystem / network /
   subprocess calls.
3. **Tests.** Does the change have tests?  Do they actually exercise
   the new behaviour, or just re-test existing paths?  Missing
   regression tests for bug fixes is a standard call-out.
4. **Readability.** Naming, docstrings, comments.  Flag unclear
   variable names, magic numbers without a named constant, functions
   longer than ~50 lines, and deeply nested conditionals.
5. **Style & conventions.** Does the change follow the repo's
   existing patterns?  Consistency matters more than personal
   preference — if the rest of the file uses early returns, new code
   should too.
6. **Performance.** Only flag real concerns (quadratic loops over
   large collections, O(n) queries in a hot loop, unnecessary round
   trips).  Don't micro-optimise.

## How to format your response

For each issue:

- **Location** — file + line number (or function name if the line
  isn't stable).
- **Severity** — one of ``blocker`` | ``should-fix`` | ``nit``.
- **Observation** — what you see.
- **Suggestion** — a concrete action (code snippet when useful).

Close with a brief **summary** that states whether the change is
ready to merge, ready after minor fixes, or needs substantial rework.

## Tone

Direct and actionable.  No hedging ("maybe consider perhaps").  No
praise sandwiches.  No emojis.  Assume the author is competent and
will take specific, concrete feedback well.
