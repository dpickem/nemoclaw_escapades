"""Tests for ``agent/git_helpers.py``.

Two helpers, both wrappers around ``tools.git._run_git``:

- ``resolve_baseline`` reads HEAD + ``remote.origin.url`` after
  workspace seeding so the orchestrator can pin a
  ``WorkspaceBaseline`` for the workflow.
- ``diff_against_baseline`` emits ``git diff <base_sha>`` for
  ``TaskCompletePayload.diff``.

These tests use real ``git`` invocations against a tmp-path repo —
the helpers are thin enough that mocking ``_run_git`` would test
the mock more than the helper.  The repo is created via
``git init`` + a single empty commit so we have a known SHA.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from nemoclaw_escapades.agent.git_helpers import (
    GitDiffError,
    WorkspaceNotAGitRepoError,
    _is_shallow,
    diff_against_baseline,
    resolve_baseline,
)


def _git(cwd: Path, *args: str) -> str:
    """Run a synchronous git command for test setup."""
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with one empty commit and a remote URL set."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "init")
    _git(tmp_path, "remote", "add", "origin", "https://example.com/acme/demo.git")
    return tmp_path


class TestResolveBaseline:
    async def test_returns_head_sha_and_remote_url(self, repo: Path) -> None:
        baseline = await resolve_baseline(str(repo), branch="main")
        head = _git(repo, "rev-parse", "HEAD")
        assert baseline.base_sha == head
        assert baseline.repo_url == "https://example.com/acme/demo.git"
        assert baseline.branch == "main"

    async def test_full_clone_is_not_shallow(self, repo: Path) -> None:
        # ``git init`` + one commit is a full clone.  Shallow clones
        # only happen via ``git clone --depth=1``.
        baseline = await resolve_baseline(str(repo), branch="main")
        assert baseline.is_shallow is False

    async def test_no_origin_remote_returns_unknown_url(self, tmp_path: Path) -> None:
        # Bare repo without a remote — exercises the fall-back path
        # in ``resolve_baseline`` where ``git config --get`` fails.
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test")
        _git(tmp_path, "config", "commit.gpgsign", "false")
        _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "init")
        baseline = await resolve_baseline(str(tmp_path), branch="main")
        assert baseline.repo_url == "unknown"

    async def test_non_git_workspace_raises(self, tmp_path: Path) -> None:
        with pytest.raises(WorkspaceNotAGitRepoError):
            await resolve_baseline(str(tmp_path), branch="main")


class TestDiffAgainstBaseline:
    async def test_empty_diff_when_workspace_unchanged(self, repo: Path) -> None:
        head = _git(repo, "rev-parse", "HEAD")
        diff = await diff_against_baseline(str(repo), head)
        assert diff == "" or diff == "\n"  # git's empty-diff output

    async def test_diff_picks_up_committed_changes(self, repo: Path) -> None:
        head = _git(repo, "rev-parse", "HEAD")
        new_file = repo / "hello.txt"
        new_file.write_text("hi\n")
        _git(repo, "add", "hello.txt")
        _git(repo, "commit", "-q", "-m", "add hello")
        diff = await diff_against_baseline(str(repo), head)
        assert "hello.txt" in diff
        assert "+hi" in diff

    async def test_diff_in_real_repo(self, repo: Path) -> None:
        # Confirms the same baseline-anchored diff a sub-agent would
        # ship on ``TaskCompletePayload.diff``.  Not just one file —
        # multi-file edits to make sure the format matches what
        # finalisation expects.
        head = _git(repo, "rev-parse", "HEAD")
        (repo / "a.py").write_text("a = 1\n")
        (repo / "b.py").write_text("b = 2\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "add a, b")
        diff = await diff_against_baseline(str(repo), head)
        assert "a.py" in diff
        assert "b.py" in diff

    async def test_diff_picks_up_uncommitted_modifications(self, repo: Path) -> None:
        # Regression for the original ``<base_sha>..HEAD`` formulation:
        # the sub-agent's tool registry has no ``git_commit``, so a
        # realistic run modifies tracked files without committing.
        # ``..HEAD`` would have returned empty here; the working-tree
        # diff must surface the change.
        (repo / "tracked.txt").write_text("baseline\n")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-q", "-m", "add tracked.txt")
        head = _git(repo, "rev-parse", "HEAD")

        # Modify the file but don't commit.
        (repo / "tracked.txt").write_text("baseline\nedit\n")
        diff = await diff_against_baseline(str(repo), head)
        assert "tracked.txt" in diff
        assert "+edit" in diff

    async def test_diff_picks_up_untracked_files(self, repo: Path) -> None:
        # The other half of the regression: a sub-agent that creates
        # a brand-new file via the ``write_file`` tool ends up with
        # an *untracked* file.  Plain ``git diff <sha>`` ignores
        # untracked content; the helper's ``--intent-to-add --all``
        # pre-step is what makes this work.
        head = _git(repo, "rev-parse", "HEAD")
        (repo / "new_module.py").write_text("def hello():\n    return 'hi'\n")
        diff = await diff_against_baseline(str(repo), head)
        assert "new_module.py" in diff
        assert "+def hello" in diff

    async def test_diff_picks_up_deletions(self, repo: Path) -> None:
        # Deletions of tracked files should also surface — equally
        # invisible to ``..HEAD`` when the sub-agent never commits.
        (repo / "to_delete.txt").write_text("doomed\n")
        _git(repo, "add", "to_delete.txt")
        _git(repo, "commit", "-q", "-m", "add to_delete")
        head = _git(repo, "rev-parse", "HEAD")

        (repo / "to_delete.txt").unlink()
        diff = await diff_against_baseline(str(repo), head)
        assert "to_delete.txt" in diff
        assert "-doomed" in diff

    async def test_git_diff_failure_raises_structured_error(self, repo: Path) -> None:
        with pytest.raises(GitDiffError) as excinfo:
            await diff_against_baseline(str(repo), "not-a-sha")
        assert excinfo.value.base_sha == "not-a-sha"
        assert "Exit code:" in excinfo.value.output


class TestIsShallow:
    """Regression coverage for the ``_is_shallow`` conservative default.

    The contract (per the docstring and the matching defaults on
    ``WorkspaceBaseline.is_shallow`` / the ``delegate_task`` JSON
    schema): only the literal ``"false"`` returned by
    ``git rev-parse --is-shallow-repository`` flips the result to
    "not shallow".  Everything else — ``"true"``, an old git that
    doesn't know the flag, or a non-git workspace that hits an
    ``Exit code: 128`` — must default to shallow so finalisation
    safely deepens before rebasing.
    """

    async def test_full_clone_returns_false(self, repo: Path) -> None:
        # ``git init`` + one commit is not shallow.
        assert await _is_shallow(str(repo)) is False

    async def test_shallow_clone_returns_true(self, repo: Path, tmp_path: Path) -> None:
        # Make ``repo`` reachable as a local file:// remote, then
        # ``--depth=1`` clone of it lands in a sibling dir.
        shallow_dest = tmp_path / "shallow"
        _git(
            tmp_path,
            "clone",
            "--depth=1",
            f"file://{repo}",
            str(shallow_dest),
        )
        assert await _is_shallow(str(shallow_dest)) is True

    async def test_non_git_workspace_defaults_to_shallow(self, tmp_path: Path) -> None:
        # Regression for the ``out == "true"`` bug: a directory that
        # isn't a git repo causes ``git rev-parse`` to exit non-zero,
        # producing an ``"Exit code: 128\n..."`` string.  The helper
        # must treat that as shallow per the docstring's conservative
        # default — the previous ``out == "true"`` returned False
        # here, which would silently skip the ``--unshallow`` step in
        # finalisation.
        bare_dir = tmp_path / "not_a_repo"
        bare_dir.mkdir()
        assert await _is_shallow(str(bare_dir)) is True


@pytest.fixture
def event_loop() -> asyncio.AbstractEventLoop:
    """Provide a per-test event loop (pytest-asyncio default).

    Some tests run subprocess git invocations that are slower than
    the in-process default loop; this is just defensive boilerplate
    so the suite works the same on every platform.
    """
    loop = asyncio.new_event_loop()
    yield loop  # type: ignore[misc]
    loop.close()
