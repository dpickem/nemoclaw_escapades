---
name: Scratchpad
description: Use a notes file in your workspace as working memory during a task
---

# Scratchpad Skill

You don't have unlimited short-term memory.  A multi-step task — a bug
hunt, a refactor, a code review — benefits from **externalising** what
you've learned so you can come back to it later in the same task,
even after a context compaction.

The convention in this codebase: **use the file tools.**  There is no
separate scratchpad tool.  Pick a filename, write Markdown to it, read
it back when you need to.

## The pattern

1. **Find your identifier.**  Your system prompt's runtime-metadata
   layer contains a line like `Agent ID: <id>`.  Copy that `<id>`
   verbatim — it uniquely identifies you across all concurrent tasks
   in this workspace.  If for some reason you don't see an Agent ID
   line (legacy config, local dev), fall back to any other stable
   identifier you have access to: the thread timestamp the user's
   message came from, a short slug you invent and stick with for the
   whole task, or a `bash date +%s` epoch.  Whatever you pick, **use
   the same identifier everywhere in this task** — filename *and*
   file header.
2. **Check what's already there, then pick a task-specific path that
   embeds your identifier.**  The workspace is shared across
   sessions — other agents (or past runs of you) may have left notes
   files behind.  Before you write anything:
   1. Call `list_directory` on the workspace root to see existing
      files.  Look for anything matching `notes*.md` or `*-notes.md`.
   2. **Pick a name of the form
      `notes-<task-slug>-<agent-id>.md`**.  The task slug is a short
      lowercase-kebab-case description of the work; the agent id is
      the identifier from step 1 (truncate long UUIDs to 8 chars for
      readability).  Examples:
      - `notes-auth-refactor-a3f1b9c2.md`
      - `notes-flaky-ci-1729531200.md`
      - `notes-review-cl-12345-a3f1b9c2.md`
      - `notes-bug-slow-startup-a3f1b9c2.md`
   3. **Never use a bare `notes.md`.**  It's guaranteed to collide
      the moment a second agent runs in the same workspace, and
      concurrent appends without locking silently lose writes.
   4. **Never overwrite a pre-existing notes file without reading it
      first.**  If your chosen name already exists: `read_file` it,
      check the `Owner:` line in the header (step 3), and decide:
      - **Owner matches your id** → it's yours from earlier in the
        same task; continue in it.
      - **Owner is different, file is clearly stale** (old
        timestamp, unrelated topic) → don't overwrite it either;
        pick a fresh name with your own id appended.
      - **Owner is different, file looks active** → leave it alone
        entirely; pick a different name.
3. **Start the file with an ownership header.**  The first thing you
   write should be a small metadata block so anyone (including
   future you) reading this file can tell at a glance whose it is
   and what task it's tracking:

   ```
   # notes-auth-refactor-a3f1b9c2.md
   Owner: a3f1b9c2
   Task: Consolidate session-token validation helpers
   Started: 2026-04-15T14:32:00Z
   ```

   The `Owner:` line is the contract.  It lets step 2.4 work — any
   agent reading a notes file can compare `Owner:` against its own
   identifier and know immediately whether the file is theirs.
4. **Write structured Markdown** with `##`-level sections so you (and
   the compactor) can scan it later:
   - `## Goal` — what you're trying to accomplish.
   - `## Plan` — numbered steps you intend to execute.
   - `## Evidence` — raw observations from `read_file`, `grep`, `bash`.
   - `## Hypotheses` — candidate explanations during debugging.
   - `## Decisions` — choices you've made and *why*, so you don't
     re-litigate them later.
   - `## Open questions` — things you couldn't resolve alone.
5. **Append as you go** (`edit_file` with a small old_string / larger
   new_string, or `write_file` with the full updated contents).  Don't
   wait until the end of the task — intermediate notes are the whole
   point.
6. **Re-read** the file whenever you resume work, re-plan, or get
   confused about where you are.

## When to use it

- **Always**, for tasks that span 3+ tool-use rounds.
- **Especially** when the task involves cross-referencing multiple
  files, log excerpts, or git history — your context window won't hold
  all of it simultaneously, but a Markdown file will.
- **Definitely** when the user asks you to pause ("come back to this
  tomorrow") — the note file is the only state that survives a fresh
  session.

## When NOT to bother

- Single-step requests ("rename this variable", "what does this
  function do?").  Overhead > benefit.
- When the answer is obvious from a single `read_file` or `grep`.

## Naming & ownership — quick reference

| Do | Don't |
|----|-------|
| `list_directory` first, inspect what's already there | Write blindly and hope for the best |
| `notes-<task-slug>-<agent-id>.md` | `notes.md` or `notes-<task>.md` without an id |
| Copy your `Agent ID` from the runtime-metadata layer of the system prompt | Invent a new id on every round — use one identifier per task |
| First line after the title: `Owner: <agent-id>` | Skip the header and rely on the filename alone |
| `read_file` a pre-existing match and check its `Owner:` line | Overwrite an existing notes file without reading it |
| lowercase-kebab-case slug (`notes-auth-refactor-a3f1b9c2.md`) | PascalCase / spaces / emoji in the filename |
| One notes file per distinct task | One shared notes file across unrelated tasks |

If two concurrent tasks ever land in the same workspace without unique
ids, one will silently clobber the other's working memory.  There are
no locks on these files and no warning on overwrite — the identifier
is your only safety net.

## Example

Assume your system prompt contains `Agent ID: a3f1b9c2-7d4e-4a11-…`.
You truncate to the first 8 chars, pick a task slug, and write:

```
# notes-auth-refactor-a3f1b9c2.md
Owner: a3f1b9c2
Task: Consolidate session-token validation helpers
Started: 2026-04-15T14:32:00Z

## Goal
Consolidate the three copies of session-token validation into a single
helper in `auth/session.py`.

## Plan
1. Read call sites (`grep "validate_token" src/`)
2. Identify the common contract
3. Write the helper + tests
4. Update call sites one by one, run tests after each

## Evidence
- 3 copies: `auth/login.py:45`, `api/middleware.py:78`, `admin/session.py:22`
- The admin copy has a bug: it forgets to check expiry. Track this separately.

## Decisions
- Keep the signature boolean-returning (not exception-raising) to match
  the two correct call sites.
```

That file — written with `write_file`, updated with `edit_file`, read
with `read_file` — *is* your scratchpad.  No special tooling required.
The filename stem (`a3f1b9c2`) and the `Owner:` line inside are the same
identifier; either alone tells anyone who opens this file that it
belongs to you.
