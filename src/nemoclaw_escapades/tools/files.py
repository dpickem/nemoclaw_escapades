"""Workspace-rooted file tools for the coding agent.

All paths are resolved relative to a workspace root directory.  Absolute
paths and ``..`` traversals are rejected to prevent the agent from
reading or modifying files outside its workspace (audit DB, NMB config,
system files, etc.).

The sandbox policy provides the real security boundary (Landlock,
read-only mounts); these path checks are defense-in-depth so a
prompt-injection can't trick the model into escaping via the tool layer.
"""

from __future__ import annotations

import os
from pathlib import Path

from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.files")

# ── Constants ─────────────────────────────────────────────────────────

# Max lines returned by read_file when no explicit limit is given
_DEFAULT_READ_LINE_LIMIT: int = 200
# Character cap on read_file output to prevent context-window blowup
_DEFAULT_READ_CHAR_LIMIT: int = 32_768
# Max directory entries returned by list_directory
_DEFAULT_LIST_LIMIT: int = 200
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "files"


# ── Path safety ───────────────────────────────────────────────────────


def _safe_resolve(workspace_root: str, relative_path: str) -> Path:
    """Resolve *relative_path* under *workspace_root*, rejecting escapes.

    Args:
        workspace_root: Absolute path to the workspace directory.
        relative_path: User-supplied path (must be relative, no ``..``).

    Returns:
        Absolute ``Path`` guaranteed to be under *workspace_root*.

    Raises:
        ValueError: If the path is absolute, contains ``..``, or
            resolves outside the workspace.
    """
    if os.path.isabs(relative_path):
        raise ValueError(f"Absolute paths are not allowed: {relative_path}")
    if ".." in Path(relative_path).parts:
        raise ValueError(f"Path traversal ('..') is not allowed: {relative_path}")
    resolved = (Path(workspace_root) / relative_path).resolve()
    root_resolved = Path(workspace_root).resolve()
    if not str(resolved).startswith(str(root_resolved) + os.sep) and resolved != root_resolved:
        raise ValueError(f"Path escapes workspace: {relative_path}")
    return resolved


# ── Helpers ───────────────────────────────────────────────────────────


def _number_and_truncate(
    lines: list[str],
    start: int,
    total: int,
    char_limit: int,
) -> str:
    """Add line numbers and a header, truncating at *char_limit* characters.

    Args:
        lines: Selected lines (with original newlines stripped later).
        start: 0-based index of the first selected line in the file.
        total: Total number of lines in the file.
        char_limit: Character budget for the output body.

    Returns:
        Formatted string with a header and numbered lines.
    """
    result_lines: list[str] = []
    char_count: int = 0
    for i, line in enumerate(lines, start=start + 1):
        numbered = f"{i:>6}|{line.rstrip()}"
        char_count += len(numbered) + 1
        if char_count > char_limit:
            result_lines.append(f"... (truncated at {char_limit} chars)")
            break
        result_lines.append(numbered)

    end = start + len(lines)
    if start > 0 or end < total:
        header = f"(showing lines {start + 1}\u2013{min(end, total)} of {total})"
    else:
        header = f"({total} lines total)"
    return f"{header}\n" + "\n".join(result_lines)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_read_file(workspace_root: str) -> ToolSpec:
    """Create the ``read_file`` tool spec bound to *workspace_root*."""

    @tool(
        "read_file",
        "Read a text file from the workspace. Returns numbered lines. Use offset/limit for large files.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from workspace root."},
                "offset": {"type": "integer", "description": "Start line (1-indexed, optional)."},
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return.",
                    "default": _DEFAULT_READ_LINE_LIMIT,
                },
            },
            "required": ["path"],
        },
        display_name="Reading file",
        toolset=_TOOLSET,
    )
    async def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file and return numbered lines with a summary header.

        Args:
            path: Relative path from workspace root.
            offset: Start line (1-indexed, optional).
            limit: Max lines to return.

        Returns:
            Numbered lines prefixed by a header showing the range and
            total line count.
        """
        try:
            resolved = _safe_resolve(workspace_root, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.is_file():
            return f"Error: file not found: {path}"
        try:
            text = resolved.read_text(errors="replace")
        except OSError as exc:
            return f"Error reading {path}: {exc}"

        lines = text.splitlines(keepends=True)
        total = len(lines)

        start = (offset - 1) if offset and offset > 0 else 0
        end = start + (limit or _DEFAULT_READ_LINE_LIMIT)
        selected = lines[start:end]

        if not selected:
            return "File is empty." if total == 0 else f"No lines in range {start + 1}\u2013{end}."

        return _number_and_truncate(selected, start, total, _DEFAULT_READ_CHAR_LIMIT)

    return read_file


def _make_write_file(workspace_root: str) -> ToolSpec:
    """Create the ``write_file`` tool spec bound to *workspace_root*."""

    @tool(
        "write_file",
        "Create or overwrite a file in the workspace.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from workspace root."},
                "content": {"type": "string", "description": "Full file contents to write."},
            },
            "required": ["path", "content"],
        },
        display_name="Writing file",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def write_file(path: str, content: str) -> str:
        """Create or overwrite a file at *path* with *content*.

        Args:
            path: Relative path from workspace root.
            content: Full file contents to write.

        Returns:
            Confirmation message with the number of bytes written.
        """
        try:
            resolved = _safe_resolve(workspace_root, path)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
        except OSError as exc:
            return f"Error writing {path}: {exc}"
        return f"Wrote {len(content)} bytes to {path}"

    return write_file


def _make_edit_file(workspace_root: str) -> ToolSpec:
    """Create the ``edit_file`` tool spec bound to *workspace_root*."""

    @tool(
        "edit_file",
        "Apply a targeted edit to a file via old/new string replacement. The old_string must occur exactly once. Preferred over write_file for surgical changes.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from workspace root."},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must be unique in the file).",
                },
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
        display_name="Editing file",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace a unique occurrence of *old_string* with *new_string* in a file.

        Args:
            path: Relative path from workspace root.
            old_string: Exact text to find (must appear exactly once).
            new_string: Replacement text.

        Returns:
            Confirmation message, or an error if the string is missing
            or ambiguous.
        """
        try:
            resolved = _safe_resolve(workspace_root, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.is_file():
            return f"Error: file not found: {path}"
        try:
            text = resolved.read_text()
        except OSError as exc:
            return f"Error reading {path}: {exc}"

        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        if count > 1:
            return (
                f"Error: old_string appears {count} times in {path}. "
                "Provide more context to make it unique."
            )
        new_text = text.replace(old_string, new_string, 1)
        try:
            resolved.write_text(new_text)
        except OSError as exc:
            return f"Error writing {path}: {exc}"
        return f"Applied edit to {path}"

    return edit_file


def _make_list_directory(workspace_root: str) -> ToolSpec:
    """Create the ``list_directory`` tool spec bound to *workspace_root*."""

    @tool(
        "list_directory",
        "List files and directories at a given path in the workspace.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path (default: workspace root).",
                    "default": ".",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return.",
                    "default": _DEFAULT_LIST_LIMIT,
                },
            },
        },
        display_name="Listing directory",
        toolset=_TOOLSET,
    )
    async def list_directory(path: str = ".", limit: int = _DEFAULT_LIST_LIMIT) -> str:
        """List files and subdirectories at *path*, with a trailing ``/`` for dirs.

        Args:
            path: Relative path from workspace root.
            limit: Max entries to return.

        Returns:
            Newline-separated directory listing with relative paths.
        """
        try:
            resolved = _safe_resolve(workspace_root, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not resolved.is_dir():
            return f"Error: not a directory: {path}"
        try:
            entries = sorted(resolved.iterdir())
        except OSError as exc:
            return f"Error listing {path}: {exc}"

        lines: list[str] = []
        for entry in entries[:limit]:
            rel = entry.relative_to(Path(workspace_root).resolve())
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{rel}{suffix}")
        total = len(entries)
        if total > limit:
            lines.append(f"... ({total - limit} more entries)")
        return "\n".join(lines) if lines else "(empty directory)"

    return list_directory


# ── Registration ──────────────────────────────────────────────────────


def register_file_tools(registry: ToolRegistry, workspace_root: str) -> None:
    """Register all file tools bound to *workspace_root*.

    Args:
        registry: The tool registry to populate.
        workspace_root: Absolute path to the workspace directory.
            All file operations are restricted to this subtree.
    """
    registry.register(_make_read_file(workspace_root))
    registry.register(_make_write_file(workspace_root))
    registry.register(_make_edit_file(workspace_root))
    registry.register(_make_list_directory(workspace_root))
