"""Git helpers for the sub-agent's lifecycle code.

Distinct from ``tools/git.py`` (which exposes ``git_*`` *model-callable*
tools): these helpers are called by the sub-agent's NMB receive loop
and the orchestrator's delegation manager, never by the model.

Two responsibilities:

- **Baseline resolution** — given a freshly seeded workspace, return
  the ``WorkspaceBaseline`` the orchestrator pinned the workflow to
  (``git rev-parse origin/<branch>`` + ``git config --get remote.origin.url``).
- **Baseline-anchored diff** — ``git diff <base_sha>..HEAD`` for
  ``TaskCompletePayload.diff``.

Both reuse ``tools.git._run_git`` so timeout / TLS-bundle / output-cap
behaviour stays consistent with the model-callable tools.

See ``docs/design_m2b.md`` §6.6 (Workspace Baseline Semantics).
"""

from __future__ import annotations

from nemoclaw_escapades.nmb.protocol import WorkspaceBaseline
from nemoclaw_escapades.tools.git import _run_git


class WorkspaceNotAGitRepoError(Exception):
    """Raised when baseline resolution is requested for a non-git workspace.

    Distinct from a generic git failure: a non-git workspace is a
    deliberate "no diff expected" signal in §6.6 (the orchestrator
    sends ``workspace_baseline=None`` for those cases), so callers
    catch this specifically and fall back to the no-baseline path.
    """


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
    head_sha = (await _run_git(workspace_root, "rev-parse", "HEAD")).strip()
    if head_sha.startswith(("Exit code:", "Error:")):
        # Both "not a git repo" and "git not installed" land here;
        # callers branch on the message in the rare cases that matters.
        if "not a git repository" in head_sha or "Not a git repository" in head_sha:
            raise WorkspaceNotAGitRepoError(workspace_root)
        raise RuntimeError(f"git rev-parse HEAD failed: {head_sha}")
    repo_url = (await _run_git(workspace_root, "config", "--get", "remote.origin.url")).strip()
    if repo_url.startswith(("Exit code:", "Error:")):
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
    """Compute the unified diff between *base_sha* and HEAD.

    Used by the sub-agent's NMB receive loop to populate
    ``TaskCompletePayload.diff``.  The orchestrator can re-derive
    the same diff at finalisation time as a cross-check (§6.6.3).

    Args:
        workspace_root: Absolute path to the workspace.
        base_sha: 40-char SHA the workspace started at.

    Returns:
        The diff text (possibly empty if the working tree matches
        ``base_sha``).  Errors from git are returned verbatim with
        the same ``"Exit code:"`` / ``"Error:"`` prefix as the
        ``_run_git`` helper uses, so callers that want to
        distinguish "no changes" from "git failed" check the prefix.
    """
    return await _run_git(workspace_root, "diff", f"{base_sha}..HEAD")


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
    finally caught up with us.  The Pydantic model
    (:class:`WorkspaceBaseline.is_shallow`) and the ``delegate_task``
    JSON schema both default to ``True`` for the same reason — this
    helper now matches them.
    """
    out = (await _run_git(workspace_root, "rev-parse", "--is-shallow-repository")).strip()
    return out != "false"
