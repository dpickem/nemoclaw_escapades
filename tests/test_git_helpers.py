"""Tests for ``agent/git_helpers.py``.

Two helpers, both wrappers around ``tools.git._run_git``:

- ``resolve_baseline`` reads HEAD + ``remote.origin.url`` after
  workspace seeding so the orchestrator can pin a
  ``WorkspaceBaseline`` for the workflow.
- ``diff_against_baseline`` emits ``git diff <base_sha>..HEAD`` for
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
    WorkspaceNotAGitRepoError,
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
