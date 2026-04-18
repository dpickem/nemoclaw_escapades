"""Bash tool for the coding agent.

Executes shell commands in the workspace root with a configurable
timeout.  The sandbox policy (Landlock, network restrictions) provides
the real security boundary; the timeout and output truncation are
convenience limits to prevent context window blowup and runaway
processes.
"""

from __future__ import annotations

import asyncio

from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.bash")

# Seconds before a shell command is killed
_DEFAULT_TIMEOUT_S: int = 120
# Hard cap on captured stdout to prevent context-window blowup
_OUTPUT_MAX_BYTES: int = 65_536
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "bash"


def _make_bash(workspace_root: str) -> ToolSpec:
    """Create the ``bash`` tool spec bound to *workspace_root*."""

    @tool(
        "bash",
        (
            "Execute a shell command in the workspace. Use for running tests, "
            "installing dependencies, build tools, etc. Commands run in the "
            "workspace root with a configurable timeout (default: 120s)."
        ),
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds before kill.",
                    "default": _DEFAULT_TIMEOUT_S,
                },
            },
            "required": ["command"],
        },
        display_name="Running command",
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def bash(command: str, timeout: int = _DEFAULT_TIMEOUT_S) -> str:
        """Run *command* via ``asyncio.create_subprocess_shell`` and return its output.

        Args:
            command: Shell command to execute.
            timeout: Max seconds before the process is killed.

        Returns:
            Exit code and combined stdout/stderr, truncated to
            ``_OUTPUT_MAX_BYTES``.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workspace_root,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return f"Error: command timed out after {timeout}s\nCommand: {command}"
        except OSError as exc:
            return f"Error: failed to execute command: {exc}"

        output = stdout.decode(errors="replace") if stdout else ""
        exit_code = proc.returncode or 0

        if len(output) > _OUTPUT_MAX_BYTES:
            output = output[:_OUTPUT_MAX_BYTES] + f"\n... (truncated at {_OUTPUT_MAX_BYTES} bytes)"

        return f"Exit code: {exit_code}\n{output}"

    return bash


def register_bash_tool(registry: ToolRegistry, workspace_root: str) -> None:
    """Register the bash tool bound to *workspace_root*.

    Args:
        registry: The tool registry to populate.
        workspace_root: Working directory for command execution.
    """
    registry.register(_make_bash(workspace_root))
