"""Git helpers for sub-agent and orchestrator lifecycle code.

Distinct from ``tools/git.py`` (which exposes ``git_*`` *model-callable*
tools): these helpers are called by the sub-agent's NMB receive loop,
the orchestrator's delegation manager, and the orchestrator's
finalisation flow — never by the model.

Three responsibilities:

- **Baseline resolution** — given a freshly seeded workspace, return
  the ``WorkspaceBaseline`` the orchestrator pinned the workflow to.
- **Baseline-anchored diff** — ``git diff <base_sha>`` for
  ``TaskCompletePayload.diff`` and the orchestrator's §6.6.3
  cross-check.
- **Finalisation git ops** — commit / branch / push primitives the
  orchestrator's finalisation tools call after the sub-agent reports
  ``task.complete``.

All helpers reuse :func:`tools.git.run_git` so timeout, TLS-bundle,
and output-cap behaviour stay consistent with the model-callable
tools.

See ``docs/design_m2b.md`` §6.6 (Workspace Baseline Semantics) and
§7 (Work Collection and Finalization).
"""

from __future__ import annotations

from nemoclaw_escapades.nmb.protocol import WorkspaceBaseline
from nemoclaw_escapades.tools.git import is_git_error, run_git

# ── Constants ──────────────────────────────────────────────────────

# Network operations (push) get a longer timeout than local-only
# operations (commit, checkout) because remote handshake + transfer
# dominates the wall-clock cost.
_PUSH_TIMEOUT_S: int = 120


class WorkspaceNotAGitRepoError(Exception):
    """Raised when baseline resolution is requested for a non-git workspace.

    Distinct from a generic git failure: a non-git workspace is a
    deliberate "no diff expected" signal in §6.6 (the orchestrator
    sends ``workspace_baseline=None`` for those cases), so callers
    catch this specifically and fall back to the no-baseline path.
    """


class GitDiffError(Exception):
    """Raised when baseline diff computation fails.

    Attributes:
        workspace_root: Workspace path that failed.
        base_sha: Baseline SHA passed to ``git diff``.
        output: Structured git-tool error text returned by :func:`run_git`.
    """

    def __init__(self, workspace_root: str, base_sha: str, output: str) -> None:
        super().__init__(f"git diff {base_sha} failed in {workspace_root}: {output}")
        self.workspace_root = workspace_root
        self.base_sha = base_sha
        self.output = output


class GitCommandError(Exception):
    """Raised when a finalisation git op fails.

    Attributes:
        command: Git subcommand that failed (e.g. ``"commit"``).
        output: Structured git-tool error text returned by :func:`run_git`.
    """

    def __init__(self, command: str, output: str) -> None:
        super().__init__(f"git {command} failed: {output}")
        self.command = command
        self.output = output


# ── Baseline resolution ────────────────────────────────────────────


async def resolve_baseline(workspace_root: str, branch: str) -> WorkspaceBaseline:
    """Read the current workspace's pinned baseline.

    Run after workspace seeding (clone / checkout) to capture what
    the orchestrator should ship on the matching ``TaskAssignPayload``.
    The orchestrator carries this dict through every subsequent
    iteration of the workflow so iteration #2's diff stays
    comparable to iteration #1's (§6.6.2 case C).

    Args:
        workspace_root: Absolute path to the seeded workspace.
        branch: Branch name to pin against; the orchestrator passes
            this through from the user's task spec.  Recorded on the
            baseline for audit / PR construction; the SHA below is
            the actual diff anchor.

    Returns:
        A populated ``WorkspaceBaseline``.

    Raises:
        WorkspaceNotAGitRepoError: If ``workspace_root`` is not a git
            repo (e.g. a one-shot data-extraction task that doesn't
            need a baseline).
        RuntimeError: If git is installed but the rev-parse fails for
            an unexpected reason — surfaces the git error verbatim.
    """
    head_sha = (await run_git(workspace_root, "rev-parse", "HEAD")).strip()
    if is_git_error(head_sha):
        # Both "not a git repo" and "git not installed" land here;
        # callers branch on the message in the rare cases that matters.
        if "not a git repository" in head_sha or "Not a git repository" in head_sha:
            raise WorkspaceNotAGitRepoError(workspace_root)
        raise RuntimeError(f"git rev-parse HEAD failed: {head_sha}")
    repo_url = (await run_git(workspace_root, "config", "--get", "remote.origin.url")).strip()
    if is_git_error(repo_url):
        # No origin configured (operator added a local-only branch
        # for testing).  Empty string is a valid sentinel for
        # "unknown" — the orchestrator's finalisation echo-match
        # still works because this same string round-trips on the
        # complete payload.
        repo_url = ""
    return WorkspaceBaseline(
        repo_url=repo_url or "unknown",
        branch=branch,
        base_sha=head_sha,
        is_shallow=await _is_shallow(workspace_root),
    )


async def diff_against_baseline(workspace_root: str, base_sha: str) -> str:
    """Compute the unified diff between *base_sha* and the working tree.

    Used by the sub-agent's NMB receive loop to populate
    ``TaskCompletePayload.diff`` and by the orchestrator at
    finalisation time to re-derive the same diff as a §6.6.3
    cross-check.

    Working tree, not ``HEAD``.  The sub-agent's tool surface
    deliberately omits ``git_commit`` (orchestrator-only per §7.1),
    so a normal sub-agent run leaves all of its edits in the working
    tree and never advances ``HEAD``.  ``git diff <base_sha>..HEAD``
    would silently return empty for those runs; ``git diff
    <base_sha>`` (no ``..HEAD``) compares ``<base_sha>`` against the
    working tree, picking up modifications and deletions of tracked
    files.

    To also include **untracked** files (the common case for
    sub-agent-created files like new modules), we first mark every
    untracked path with ``git add --intent-to-add --all``.

    Args:
        workspace_root: Absolute path to the workspace.
        base_sha: 40-char SHA the workspace started at.

    Returns:
        The diff text, possibly empty if the working tree matches
        ``base_sha``.

    Raises:
        GitDiffError: If ``git diff`` itself fails.
    """
    # Best-effort: ``--intent-to-add`` failures (read-only repo,
    # weird permissions) shouldn't sink the diff entirely — we still
    # get the tracked-file diff below.  The error string surfaces in
    # the sub-agent's structured log via :func:`run_git`.
    await run_git(workspace_root, "add", "--intent-to-add", "--all")
    diff = await run_git(workspace_root, "diff", base_sha)
    if is_git_error(diff):
        raise GitDiffError(workspace_root, base_sha, diff)
    return diff


# ── Finalisation git ops ───────────────────────────────────────────


async def commit_workspace(workspace_root: str, message: str) -> str:
    """Stage all changes and create a commit with *message*.

    Returns the commit's stdout/stderr text on success.  Treats
    "nothing to commit" as success (returns the message verbatim) —
    finalisation may legitimately run on a workspace whose only
    edits were already staged-and-committed by an iteration of the
    sub-agent.

    Args:
        workspace_root: Absolute path to the workspace.
        message: Commit message (passed to ``git commit -m``).

    Returns:
        Combined git output.  Empty changes return the
        ``"nothing to commit"`` line verbatim.

    Raises:
        GitCommandError: If ``git add`` or ``git commit`` itself
            fails for any reason other than empty changes.
    """
    add_result = await run_git(workspace_root, "add", "-A")
    if is_git_error(add_result):
        raise GitCommandError("add", add_result)
    commit_result = await run_git(workspace_root, "commit", "-m", message)
    if is_git_error(commit_result) and "nothing to commit" not in commit_result:
        raise GitCommandError("commit", commit_result)
    return commit_result


async def checkout_branch(workspace_root: str, branch: str, *, create: bool = True) -> str:
    """Switch to *branch*, creating it if needed.

    Args:
        workspace_root: Absolute path to the workspace.
        branch: Branch name to switch to.
        create: When True (default), pass ``-B`` so an existing branch
            is reset to HEAD and a missing branch is created.  Pass
            False to fail if *branch* doesn't already exist.

    Returns:
        Git output on success.

    Raises:
        GitCommandError: If the checkout fails.
    """
    args: tuple[str, ...] = ("checkout", "-B", branch) if create else ("checkout", branch)
    result = await run_git(workspace_root, *args)
    if is_git_error(result):
        raise GitCommandError("checkout", result)
    return result


async def push_branch(workspace_root: str, branch: str, *, remote: str = "origin") -> str:
    """Push *branch* to *remote* with upstream tracking.

    Uses :data:`_PUSH_TIMEOUT_S` since network handshake + transfer
    dominates wall-clock cost.

    Args:
        workspace_root: Absolute path to the workspace.
        branch: Local branch name to push.
        remote: Remote name (default ``origin``).

    Returns:
        Git output on success.

    Raises:
        GitCommandError: If the push fails (auth, divergence, etc.).
    """
    result = await run_git(workspace_root, "push", "-u", remote, branch, timeout=_PUSH_TIMEOUT_S)
    if is_git_error(result):
        raise GitCommandError("push", result)
    return result


# ── Internal ───────────────────────────────────────────────────────


async def _is_shallow(workspace_root: str) -> bool:
    """Check whether the workspace is a shallow clone.

    ``git rev-parse --is-shallow-repository`` returns ``"true"`` /
    ``"false"`` on stdout; anything else (a git error, an unfamiliar
    repo state, an older git that doesn't know the flag) is treated
    as shallow.

    The asymmetry is deliberate: on an unknown state the safe move
    is to assume *shallow*, because finalisation can then run
    ``git fetch --unshallow`` defensively and proceed.  The opposite
    default (``out == "true"``) would skip the deepen step on git
    failure and crash at rebase time when the missing history
    finally caught up with us.
    """
    out = (await run_git(workspace_root, "rev-parse", "--is-shallow-repository")).strip()
    return out != "false"
