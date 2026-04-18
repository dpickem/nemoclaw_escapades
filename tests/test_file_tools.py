"""Tests for workspace-rooted file tools, search tools, bash, and git."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.tools.files import _safe_resolve, register_file_tools
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.search import register_search_tools
from nemoclaw_escapades.tools.tool_registry_factory import create_coding_tool_registry

# ── Path safety tests ─────────────────────────────────────────────────


class TestSafeResolve:
    def test_relative_path(self, tmp_path: Path) -> None:
        result = _safe_resolve(str(tmp_path), "foo/bar.txt")
        assert str(result).startswith(str(tmp_path))

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Absolute paths"):
            _safe_resolve(str(tmp_path), "/etc/passwd")

    def test_dotdot_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            _safe_resolve(str(tmp_path), "../escape.txt")

    def test_dotdot_in_middle_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            _safe_resolve(str(tmp_path), "foo/../../escape.txt")

    def test_current_dir(self, tmp_path: Path) -> None:
        result = _safe_resolve(str(tmp_path), ".")
        assert result == tmp_path.resolve()


# ── File tool tests ───────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("line 1\nline 2\nline 3\n")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.py").write_text("print('hello')\n")
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    register_file_tools(reg, str(workspace))
    register_search_tools(reg, str(workspace))
    return reg


class TestReadFile:
    async def test_read_existing_file(self, registry: ToolRegistry) -> None:
        result = await registry.execute("read_file", json.dumps({"path": "hello.txt"}))
        assert "line 1" in result
        assert "3 lines total" in result

    async def test_read_with_offset(self, registry: ToolRegistry) -> None:
        result = await registry.execute(
            "read_file", json.dumps({"path": "hello.txt", "offset": 2, "limit": 1})
        )
        assert "line 2" in result
        assert "line 1" not in result

    async def test_read_missing_file(self, registry: ToolRegistry) -> None:
        result = await registry.execute("read_file", json.dumps({"path": "nonexistent.txt"}))
        assert "Error" in result

    async def test_read_path_escape_blocked(self, registry: ToolRegistry) -> None:
        result = await registry.execute("read_file", json.dumps({"path": "../escape.txt"}))
        assert "Error" in result
        assert "traversal" in result.lower()

    async def test_read_absolute_path_blocked(self, registry: ToolRegistry) -> None:
        result = await registry.execute("read_file", json.dumps({"path": "/etc/passwd"}))
        assert "Error" in result

    async def test_read_nested_file(self, registry: ToolRegistry) -> None:
        result = await registry.execute("read_file", json.dumps({"path": "subdir/nested.py"}))
        assert "print" in result


class TestWriteFile:
    async def test_write_new_file(self, registry: ToolRegistry, workspace: Path) -> None:
        result = await registry.execute(
            "write_file", json.dumps({"path": "new.txt", "content": "hello world"})
        )
        assert "Wrote" in result
        assert (workspace / "new.txt").read_text() == "hello world"

    async def test_write_creates_parent_dirs(self, registry: ToolRegistry, workspace: Path) -> None:
        await registry.execute(
            "write_file",
            json.dumps({"path": "deep/nested/file.txt", "content": "deep"}),
        )
        assert (workspace / "deep" / "nested" / "file.txt").read_text() == "deep"

    async def test_write_path_escape_blocked(self, registry: ToolRegistry) -> None:
        result = await registry.execute(
            "write_file", json.dumps({"path": "../escape.txt", "content": "bad"})
        )
        assert "Error" in result


class TestEditFile:
    async def test_edit_replaces_unique_string(
        self, registry: ToolRegistry, workspace: Path
    ) -> None:
        result = await registry.execute(
            "edit_file",
            json.dumps({"path": "hello.txt", "old_string": "line 2", "new_string": "REPLACED"}),
        )
        assert "Applied edit" in result
        assert "REPLACED" in (workspace / "hello.txt").read_text()

    async def test_edit_missing_string(self, registry: ToolRegistry) -> None:
        result = await registry.execute(
            "edit_file",
            json.dumps(
                {
                    "path": "hello.txt",
                    "old_string": "not in file",
                    "new_string": "x",
                }
            ),
        )
        assert "not found" in result

    async def test_edit_ambiguous_string(self, registry: ToolRegistry, workspace: Path) -> None:
        (workspace / "dup.txt").write_text("aaa\naaa\n")
        result = await registry.execute(
            "edit_file",
            json.dumps({"path": "dup.txt", "old_string": "aaa", "new_string": "bbb"}),
        )
        assert "appears 2 times" in result


class TestListDirectory:
    async def test_list_root(self, registry: ToolRegistry) -> None:
        result = await registry.execute("list_directory", json.dumps({"path": "."}))
        assert "hello.txt" in result
        assert "subdir/" in result

    async def test_list_subdir(self, registry: ToolRegistry) -> None:
        result = await registry.execute("list_directory", json.dumps({"path": "subdir"}))
        assert "nested.py" in result


# ── Search tool tests ─────────────────────────────────────────────────


class TestGrep:
    async def test_grep_finds_match(self, registry: ToolRegistry) -> None:
        result = await registry.execute("grep", json.dumps({"pattern": "line 2"}))
        assert "hello.txt" in result
        assert "line 2" in result

    async def test_grep_no_match(self, registry: ToolRegistry) -> None:
        result = await registry.execute("grep", json.dumps({"pattern": "zzz_no_match"}))
        assert "No matches" in result

    async def test_grep_with_include(self, registry: ToolRegistry) -> None:
        result = await registry.execute("grep", json.dumps({"pattern": "print", "include": "*.py"}))
        assert "nested.py" in result


class TestGlob:
    async def test_glob_py_files(self, registry: ToolRegistry) -> None:
        result = await registry.execute("glob_search", json.dumps({"pattern": "**/*.py"}))
        assert "nested.py" in result

    async def test_glob_no_match(self, registry: ToolRegistry) -> None:
        result = await registry.execute("glob_search", json.dumps({"pattern": "**/*.rs"}))
        assert "No files" in result


# ── Factory test ──────────────────────────────────────────────────────


class TestCodingToolRegistry:
    def test_factory_creates_all_tools(self, workspace: Path) -> None:
        reg = create_coding_tool_registry(str(workspace))
        names = set(reg.names)
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "list_directory" in names
        assert "grep" in names
        assert "glob_search" in names
        assert "bash" in names
        assert "git_diff" in names
        assert "git_commit" in names
        assert "git_log" in names
        # No scratchpad tools when scratchpad is None
        assert "scratchpad_read" not in names

    def test_factory_with_scratchpad(self, workspace: Path) -> None:
        from nemoclaw_escapades.agent.scratchpad import Scratchpad

        sp = Scratchpad(str(workspace / ".scratchpad.md"))
        reg = create_coding_tool_registry(str(workspace), scratchpad=sp)
        assert "scratchpad_read" in reg.names
        assert "scratchpad_write" in reg.names
        assert "scratchpad_append" in reg.names


# ── Output truncation ─────────────────────────────────────────────────


class TestOutputTruncation:
    """Large file reads and grep results are truncated to bounded size."""

    async def test_read_file_truncates_large_output(self, tmp_path: Path) -> None:
        """Reading a large file returns output capped at the registry limit."""
        # Generate a file comfortably larger than both _DEFAULT_READ_CHAR_LIMIT
        # (32K) and the registry's default max_result_chars (8K).
        big = tmp_path / "big.txt"
        big.write_text("\n".join(f"line content {i}" for i in range(5000)))

        reg = ToolRegistry()
        register_file_tools(reg, str(tmp_path))
        # Raise the line limit so read_file actually produces a huge body
        # that's then trimmed by the registry's max_result_chars cap.
        result = await reg.execute("read_file", json.dumps({"path": "big.txt", "limit": 5000}))
        assert "truncated" in result.lower()
        # Registry default max_result_chars = 8000; allow some slack for the
        # truncation notice appended by the registry (~50 chars).
        assert len(result) <= 8100

    async def test_grep_truncates_many_matches(self, tmp_path: Path) -> None:
        """Grep output over the cap gets a truncation marker."""
        for i in range(200):
            (tmp_path / f"file_{i:03d}.txt").write_text(
                "\n".join(f"hit {i} line {j}" for j in range(50))
            )

        reg = ToolRegistry()
        register_search_tools(reg, str(tmp_path))
        result = await reg.execute("grep", json.dumps({"pattern": "hit"}))
        assert "truncated" in result.lower()
        assert len(result) <= 8100

    async def test_small_read_not_truncated(self, tmp_path: Path) -> None:
        """Small files pass through without a truncation marker."""
        small = tmp_path / "small.txt"
        small.write_text("just three\nshort\nlines\n")

        reg = ToolRegistry()
        register_file_tools(reg, str(tmp_path))
        result = await reg.execute("read_file", json.dumps({"path": "small.txt"}))
        assert "truncated" not in result.lower()
