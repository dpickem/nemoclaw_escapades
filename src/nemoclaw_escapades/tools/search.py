"""Search tools for the coding agent — grep and glob.

Both tools are workspace-rooted and use the same path safety checks
as the file tools.  ``grep`` delegates to ``rg`` (ripgrep) when
available, falling back to Python's ``re`` module.  ``glob`` uses
``pathlib.Path.glob``.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.files import _safe_resolve
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.search")

# Max matching lines returned by grep
_DEFAULT_GREP_LIMIT: int = 50
# Max file paths returned by glob
_DEFAULT_GLOB_LIMIT: int = 200
# Hard cap on grep output to prevent context-window blowup
_GREP_OUTPUT_MAX_BYTES: int = 32_768
# Seconds before a grep subprocess is killed
_GREP_TIMEOUT_S: int = 30
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "search"


# ── Grep helpers ─────────────────────────────────────────────────────


async def _grep_with_rg(
    rg_path: str,
    pattern: str,
    search_dir: Path,
    workspace_root: str,
    include: str,
    limit: int,
) -> str:
    """Run ripgrep and return workspace-relative matching lines.

    Args:
        rg_path: Absolute path to the ``rg`` binary.
        pattern: Regex pattern to search for.
        search_dir: Resolved directory or file to search.
        workspace_root: Workspace root for relativising output paths.
        include: Glob filter for file names (e.g. ``'*.py'``).
        limit: Max matching lines.

    Returns:
        Matching lines with workspace-relative paths, or an error /
        "no matches" message.
    """
    cmd = [rg_path, "--no-heading", "--line-number", "--max-count", str(limit)]
    if include:
        cmd.extend(["--glob", include])
    cmd.extend(["--", pattern, str(search_dir)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_root,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GREP_TIMEOUT_S)
    except TimeoutError:
        return f"Error: grep timed out after {_GREP_TIMEOUT_S}s"

    if proc.returncode == 1:
        return "No matches found."
    if proc.returncode and proc.returncode > 1:
        err = stderr.decode(errors="replace").strip() if stderr else ""
        return f"Error: rg exited with code {proc.returncode}: {err}"

    output = stdout.decode(errors="replace") if stdout else ""
    root_prefix = str(Path(workspace_root).resolve()) + "/"
    return output.replace(root_prefix, "")


def _grep_with_re(
    pattern: str,
    search_dir: Path,
    workspace_root: str,
    include: str,
    limit: int,
) -> str:
    """Pure-Python regex fallback when ripgrep is not installed.

    Args:
        pattern: Regex pattern to search for.
        search_dir: Resolved directory or file to search.
        workspace_root: Workspace root for relativising output paths.
        include: Glob filter for file names (e.g. ``'*.py'``).
        limit: Max matching lines.

    Returns:
        Matching lines with workspace-relative paths, or an error /
        "no matches" message.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"

    root = Path(workspace_root).resolve()
    search_path = search_dir if search_dir.is_dir() else search_dir.parent
    glob_pat = include or "**/*"
    matches: list[str] = []

    for file_path in sorted(search_path.glob(glob_pat)):
        if not file_path.is_file():
            continue
        try:
            for line_num, line in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
                if compiled.search(line):
                    rel = file_path.relative_to(root)
                    matches.append(f"{rel}:{line_num}:{line.rstrip()}")
                    if len(matches) >= limit:
                        break
        except OSError:
            continue
        if len(matches) >= limit:
            break

    if not matches:
        return "No matches found."
    return "\n".join(matches)


# ── Tool specs ───────────────────────────────────────────────────────


def _make_grep(workspace_root: str) -> ToolSpec:
    """Create the ``grep`` tool spec bound to *workspace_root*."""

    @tool(
        "grep",
        (
            "Search file contents by regex pattern. Returns matching lines "
            "with file paths and line numbers. Supports glob filtering."
        ),
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {
                    "type": "string",
                    "description": "Relative directory or file to search (default: root).",
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": "Glob filter for file names (e.g. '*.py').",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matching lines.",
                    "default": _DEFAULT_GREP_LIMIT,
                },
            },
            "required": ["pattern"],
        },
        display_name="Searching file contents",
        toolset=_TOOLSET,
    )
    async def grep(
        pattern: str,
        path: str = ".",
        include: str = "",
        limit: int = _DEFAULT_GREP_LIMIT,
    ) -> str:
        """Search files via ripgrep (falling back to ``re``) and return matching lines.

        Args:
            pattern: Regex pattern to search for.
            path: Relative directory or file to search.
            include: Glob filter for file names (e.g. ``'*.py'``).
            limit: Max matching lines to return.

        Returns:
            Matching lines formatted as ``path:line_num:content``,
            truncated to ``_GREP_OUTPUT_MAX_BYTES``.
        """
        try:
            resolved = _safe_resolve(workspace_root, path)
        except ValueError as exc:
            return f"Error: {exc}"

        rg = shutil.which("rg")
        if rg:
            output = await _grep_with_rg(rg, pattern, resolved, workspace_root, include, limit)
        else:
            output = _grep_with_re(pattern, resolved, workspace_root, include, limit)

        if len(output) > _GREP_OUTPUT_MAX_BYTES:
            output = output[:_GREP_OUTPUT_MAX_BYTES] + "\n... (truncated)"
        return output

    return grep


def _make_glob(workspace_root: str) -> ToolSpec:
    """Create the ``glob_search`` tool spec bound to *workspace_root*."""

    @tool(
        "glob_search",
        "Find files matching a glob pattern (e.g. '**/*.py').",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results.",
                    "default": _DEFAULT_GLOB_LIMIT,
                },
            },
            "required": ["pattern"],
        },
        display_name="Finding files",
        toolset=_TOOLSET,
    )
    async def glob_search(pattern: str, limit: int = _DEFAULT_GLOB_LIMIT) -> str:
        """Find files matching a glob pattern under the workspace.

        Args:
            pattern: Glob pattern (e.g. ``'**/*.py'``).
            limit: Max file paths to return.

        Returns:
            Newline-separated relative paths of matching files.
        """
        root = Path(workspace_root).resolve()
        try:
            all_matches = sorted(root.glob(pattern))
        except (ValueError, OSError) as exc:
            return f"Error: {exc}"

        results: list[str] = []
        total_files: int = 0
        for match in all_matches:
            if not match.is_file():
                continue
            total_files += 1
            if len(results) < limit:
                results.append(str(match.relative_to(root)))

        if not results:
            return "No files matched."
        output = "\n".join(results)
        if total_files > limit:
            output += f"\n... ({total_files - limit} more files)"
        return output

    return glob_search


# ── Registration ─────────────────────────────────────────────────────


def register_search_tools(registry: ToolRegistry, workspace_root: str) -> None:
    """Register grep and glob tools bound to *workspace_root*.

    Args:
        registry: The tool registry to populate.
        workspace_root: Absolute path to the workspace directory.
    """
    registry.register(_make_grep(workspace_root))
    registry.register(_make_glob(workspace_root))
