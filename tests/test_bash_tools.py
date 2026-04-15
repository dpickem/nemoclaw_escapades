"""Tests for the bash tool registration and handler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.tools.bash import register_bash_tool
from nemoclaw_escapades.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    register_bash_tool(reg, str(workspace))
    return reg


class TestBashToolRegistration:
    def test_registers_bash_tool(self, registry: ToolRegistry) -> None:
        assert "bash" in registry
        assert len(registry) == 1

    def test_bash_is_not_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("bash")
        assert spec is not None
        assert spec.is_read_only is False

    def test_bash_has_bash_toolset(self, registry: ToolRegistry) -> None:
        spec = registry.get("bash")
        assert spec is not None
        assert spec.toolset == "bash"

    def test_bash_requires_command(self, registry: ToolRegistry) -> None:
        spec = registry.get("bash")
        assert spec is not None
        assert "command" in spec.input_schema.get("required", [])

    def test_tool_definition_valid_openai_format(self, registry: ToolRegistry) -> None:
        defs = registry.tool_definitions()
        assert len(defs) == 1
        d = defs[0]
        assert d.type == "function"
        assert d.function.name == "bash"
        assert d.function.description
        assert d.function.parameters is not None


class TestBashHandler:
    async def test_echo_command(self, registry: ToolRegistry) -> None:
        result = await registry.execute("bash", json.dumps({"command": "echo hello"}))
        assert "Exit code: 0" in result
        assert "hello" in result

    async def test_exit_code_nonzero(self, registry: ToolRegistry) -> None:
        result = await registry.execute("bash", json.dumps({"command": "false"}))
        assert "Exit code: 1" in result

    async def test_timeout(self, registry: ToolRegistry) -> None:
        result = await registry.execute(
            "bash", json.dumps({"command": "sleep 10", "timeout": 1})
        )
        assert "timed out" in result

    async def test_cwd_is_workspace(self, registry: ToolRegistry, workspace: Path) -> None:
        result = await registry.execute("bash", json.dumps({"command": "pwd"}))
        assert str(workspace.resolve()) in result
