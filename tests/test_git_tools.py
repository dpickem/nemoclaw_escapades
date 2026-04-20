"""Tests for the git tool registration and handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.tools.git import (
    _default_clone_dest,
    _extract_git_url_host,
    _parse_allowed_hosts,
    register_git_tools,
)
from nemoclaw_escapades.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    register_git_tools(reg, str(workspace))
    return reg


_EXPECTED_TOOLS = {
    "git_diff",
    "git_commit",
    "git_log",
    "git_checkout",
    "git_clone",
}


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


class TestGitCheckout:
    """``git_checkout`` — branch-switch tool."""

    async def test_checkout_nonexistent_ref_errors(self, registry: ToolRegistry) -> None:
        result = await registry.execute("git_checkout", json.dumps({"ref": "does-not-exist"}))
        assert "Error" in result or "Exit code" in result

    def test_checkout_requires_ref(self, registry: ToolRegistry) -> None:
        spec = registry.get("git_checkout")
        assert spec is not None
        assert "ref" in spec.input_schema.get("required", [])

    def test_checkout_is_not_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("git_checkout")
        assert spec is not None
        assert spec.is_read_only is False
        assert spec.is_concurrency_safe is False


class TestGitClone:
    """``git_clone`` — fail-closed allowlist gate."""

    async def test_clone_disabled_without_allowlist(self, registry: ToolRegistry) -> None:
        """With the default empty allowlist, git_clone refuses all URLs."""
        result = await registry.execute(
            "git_clone", json.dumps({"repo_url": "https://github.com/foo/bar.git"})
        )
        assert "disabled" in result.lower()
        assert "allowlist" in result.lower()

    async def test_clone_rejects_host_not_in_allowlist(self, workspace: Path) -> None:
        reg = ToolRegistry()
        register_git_tools(reg, str(workspace), git_clone_allowed_hosts="github.com")
        result = await reg.execute(
            "git_clone", json.dumps({"repo_url": "https://evil.example.com/pwn.git"})
        )
        assert "not in" in result.lower()
        assert "evil.example.com" in result

    async def test_clone_rejects_path_traversal_dest(self, workspace: Path) -> None:
        reg = ToolRegistry()
        register_git_tools(reg, str(workspace), git_clone_allowed_hosts="github.com")
        result = await reg.execute(
            "git_clone",
            json.dumps({"repo_url": "https://github.com/foo/bar.git", "dest": "../escape"}),
        )
        assert "escape" in result.lower()
        assert "workspace" in result.lower()

    async def test_clone_refuses_existing_dest(self, workspace: Path) -> None:
        (workspace / "bar").mkdir()
        reg = ToolRegistry()
        register_git_tools(reg, str(workspace), git_clone_allowed_hosts="github.com")
        result = await reg.execute(
            "git_clone",
            json.dumps({"repo_url": "https://github.com/foo/bar.git"}),
        )
        assert "already exists" in result.lower()


class TestGitCloneHelpers:
    """Pure-function helpers used by git_clone."""

    def test_parse_allowed_hosts_empty(self) -> None:
        assert _parse_allowed_hosts("") == frozenset()

    def test_parse_allowed_hosts_comma_separated(self) -> None:
        assert _parse_allowed_hosts("github.com, gitlab.com") == frozenset(
            {"github.com", "gitlab.com"}
        )

    def test_parse_allowed_hosts_whitespace_separated(self) -> None:
        assert _parse_allowed_hosts("github.com gitlab.com") == frozenset(
            {"github.com", "gitlab.com"}
        )

    def test_extract_host_https(self) -> None:
        assert _extract_git_url_host("https://github.com/foo/bar.git") == "github.com"

    def test_extract_host_ssh_scp_style(self) -> None:
        assert _extract_git_url_host("git@github.com:foo/bar.git") == "github.com"

    def test_extract_host_ssh_url_style(self) -> None:
        assert _extract_git_url_host("ssh://git@gitlab.com/foo/bar.git") == "gitlab.com"

    def test_extract_host_malformed(self) -> None:
        assert _extract_git_url_host("not-a-url") is None

    def test_default_dest_strips_git_suffix(self) -> None:
        assert _default_clone_dest("https://github.com/foo/myproj.git") == "myproj"

    def test_default_dest_no_git_suffix(self) -> None:
        assert _default_clone_dest("https://github.com/foo/myproj") == "myproj"

    def test_default_dest_scp_style(self) -> None:
        assert _default_clone_dest("git@github.com:foo/myproj.git") == "myproj"
