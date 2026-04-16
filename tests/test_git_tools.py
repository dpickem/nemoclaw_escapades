"""Tests for the git tool registration and handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.tools.git import register_git_tools
from nemoclaw_escapades.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    register_git_tools(reg, str(workspace))
    return reg


_EXPECTED_TOOLS = {"git_diff", "git_commit", "git_log"}


class TestGitToolRegistration:
    def test_registers_all_tools(self, registry: ToolRegistry) -> None:
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_tools_are_read_only(self, registry: ToolRegistry) -> None:
        for name in ("git_diff", "git_log"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_commit_is_not_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("git_commit")
        assert spec is not None
        assert spec.is_read_only is False

    def test_all_tools_have_git_toolset(self, registry: ToolRegistry) -> None:
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "git"

    def test_tool_definitions_valid_openai_format(self, registry: ToolRegistry) -> None:
        for d in registry.tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_commit_requires_message(self, registry: ToolRegistry) -> None:
        spec = registry.get("git_commit")
        assert spec is not None
        assert "message" in spec.input_schema.get("required", [])


class TestGitHandlers:
    async def test_diff_in_non_git_dir(self, registry: ToolRegistry) -> None:
        result = await registry.execute("git_diff", json.dumps({"staged": False}))
        assert "Exit code" in result or "Error" in result

    async def test_log_in_non_git_dir(self, registry: ToolRegistry) -> None:
        result = await registry.execute("git_log", "{}")
        assert "Exit code" in result or "Error" in result

    async def test_diff_in_real_repo(self, workspace: Path) -> None:
        """git_diff succeeds in an initialised repo with no changes."""
        import subprocess

        subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        reg = ToolRegistry()
        register_git_tools(reg, str(workspace))
        result = await reg.execute("git_diff", json.dumps({"staged": False}))
        assert "No uncommitted changes" in result
