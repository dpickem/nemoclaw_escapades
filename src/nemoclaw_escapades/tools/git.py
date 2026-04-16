"""Git tools for the coding agent — diff, commit, and log.

All commands run in the workspace root via ``git`` subprocess calls.
The sandbox policy controls which git operations are allowed at the
network level (e.g. push requires network access, which coding agents
typically don't have).

**Why subprocess instead of a Python git library?**

- ``GitPython`` shells out to the ``git`` binary under the hood — same
  thing with an extra abstraction layer.
- ``pygit2`` (libgit2 bindings) requires compiling a C library, doesn't
  support all porcelain commands (e.g. ``git log --oneline`` has no
  clean equivalent), and behaves subtly differently from the CLI in
  edge cases (config resolution, credential helpers, hooks).
- Subprocess guarantees identical behaviour to typing ``git diff`` in a
  terminal — same binary, same config, same hooks.
- The ``bash`` tool already provides arbitrary git access for anything
  the dedicated tools don't cover.
"""

from __future__ import annotations

import asyncio

from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.git")

# ── Constants ─────────────────────────────────────────────────────────

# Max commits shown by git_log
_DEFAULT_LOG_LIMIT: int = 20
# Character cap on combined git stdout/stderr before truncation
_OUTPUT_MAX_BYTES: int = 65_536
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "git"
# Default timeout (seconds) for subprocess git invocations
_GIT_TIMEOUT_S: int = 30


# ── Helpers ───────────────────────────────────────────────────────────


async def _run_git(workspace_root: str, *args: str, timeout: int = _GIT_TIMEOUT_S) -> str:
    """Run a git command and return its output.

    Args:
        workspace_root: Working directory for the git command.
        *args: Git subcommand and arguments.
        timeout: Maximum seconds before the process is killed; defaults to
            ``_GIT_TIMEOUT_S``.

    Returns:
        Combined stdout + stderr with exit code prefix on failure.
    """
    cmd = ["git", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_root,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        return f"Error: git command timed out after {timeout}s"
    except FileNotFoundError:
        return "Error: git is not installed"

    out = stdout.decode(errors="replace") if stdout else ""
    err = stderr.decode(errors="replace") if stderr else ""

    if proc.returncode != 0:
        return f"Exit code: {proc.returncode}\n{err.strip()}"

    output = out if out else err
    if len(output) > _OUTPUT_MAX_BYTES:
        output = output[:_OUTPUT_MAX_BYTES] + f"\n... (truncated at {_OUTPUT_MAX_BYTES} bytes)"
    return output


# ── Tool specs ────────────────────────────────────────────────────────


def _make_git_diff(workspace_root: str) -> ToolSpec:
    """Create the ``git_diff`` tool spec bound to *workspace_root*."""

    @tool(
        "git_diff",
        "Show uncommitted changes in the workspace. Use staged=true for staged-only.",
        {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show only staged changes.",
                    "default": False,
                },
            },
        },
        display_name="Checking git diff",
        toolset=_TOOLSET,
    )
    async def git_diff(staged: bool = False) -> str:
        """Show uncommitted changes in the working tree or staged area.

        Args:
            staged: When True, show only staged changes (``--cached``).

        Returns:
            Diff text, or a short message when there are no changes.
        """
        args = ["diff"]
        if staged:
            args.append("--cached")
        result = await _run_git(workspace_root, *args)
        return result if result.strip() else "No uncommitted changes."

    return git_diff


def _make_git_commit(workspace_root: str) -> ToolSpec:
    """Create the ``git_commit`` tool spec bound to *workspace_root*."""

    @tool(
        "git_commit",
        "Stage all changes and commit with a message.",
        {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "add_all": {
                    "type": "boolean",
                    "description": "Stage all changes first.",
                    "default": True,
                },
            },
            "required": ["message"],
        },
        display_name="Committing changes",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def git_commit(message: str, add_all: bool = True) -> str:
        """Stage changes (optionally) and create a commit with *message*.

        Args:
            message: Commit message passed to ``git commit -m``.
            add_all: When True, run ``git add -A`` before committing.

        Returns:
            Git output on success, or an error string from staging or commit.
        """
        if add_all:
            add_result = await _run_git(workspace_root, "add", "-A")
            if add_result.startswith("Error:") or add_result.startswith("Exit code:"):
                return f"Failed to stage: {add_result}"
        return await _run_git(workspace_root, "commit", "-m", message)

    return git_commit


def _make_git_log(workspace_root: str) -> ToolSpec:
    """Create the ``git_log`` tool spec bound to *workspace_root*."""

    @tool(
        "git_log",
        "Show recent commit history (one line per commit).",
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max commits to show.",
                    "default": _DEFAULT_LOG_LIMIT,
                },
            },
        },
        display_name="Checking git log",
        toolset=_TOOLSET,
    )
    async def git_log(limit: int = _DEFAULT_LOG_LIMIT) -> str:
        """Show recent commit history as one-line abbreviated hashes.

        Args:
            limit: Maximum number of commits to include.

        Returns:
            Output of ``git log --oneline`` (possibly truncated by helpers).
        """
        return await _run_git(
            workspace_root, "log", f"--max-count={limit}", "--oneline", "--no-decorate"
        )

    return git_log


# ── Registration ──────────────────────────────────────────────────────


def register_git_tools(registry: ToolRegistry, workspace_root: str) -> None:
    """Register git_diff, git_commit, and git_log tools.

    Args:
        registry: The tool registry to populate.
        workspace_root: Working directory for git commands.
    """
    registry.register(_make_git_diff(workspace_root))
    registry.register(_make_git_commit(workspace_root))
    registry.register(_make_git_log(workspace_root))
