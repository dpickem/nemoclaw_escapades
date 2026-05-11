"""GitHub helpers for orchestrator-owned finalization operations."""

from __future__ import annotations

import asyncio

# GitHub CLI executable.
_GH_COMMAND: str = "gh"

# Timeout for GitHub CLI operations.
_GH_TIMEOUT_S: float = 120.0


class GitHubCommandError(Exception):
    """Raised when a GitHub CLI operation fails."""

    def __init__(self, command: str, output: str) -> None:
        super().__init__(f"gh {command} failed: {output}")
        self.command = command
        self.output = output


async def create_pull_request(
    workspace_root: str,
    *,
    title: str,
    body: str,
) -> str:
    """Create a GitHub PR from the current branch using ``gh pr create``.

    Args:
        workspace_root: Repository path where ``gh`` should run.
        title: Pull request title.
        body: Pull request body.

    Returns:
        GitHub CLI output, typically the PR URL.

    Raises:
        GitHubCommandError: If ``gh pr create`` exits non-zero.
        TimeoutError: If the CLI call exceeds the operation timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        _GH_COMMAND,
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace_root,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_S)
    output = (stdout or stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise GitHubCommandError("pr create", output)
    return output
