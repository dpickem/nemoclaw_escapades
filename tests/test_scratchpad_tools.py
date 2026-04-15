"""Tests for the scratchpad tool registration and handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.agent.scratchpad import Scratchpad
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.scratchpad import register_scratchpad_tools


@pytest.fixture
def scratchpad(tmp_path: Path) -> Scratchpad:
    return Scratchpad(str(tmp_path / ".scratchpad.md"))


@pytest.fixture
def registry(scratchpad: Scratchpad) -> ToolRegistry:
    reg = ToolRegistry()
    register_scratchpad_tools(reg, scratchpad)
    return reg


_EXPECTED_TOOLS = {"scratchpad_read", "scratchpad_write", "scratchpad_append"}


class TestScratchpadToolRegistration:
    def test_registers_all_tools(self, registry: ToolRegistry) -> None:
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_is_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("scratchpad_read")
        assert spec is not None
        assert spec.is_read_only is True

    def test_write_tools_are_not_read_only(self, registry: ToolRegistry) -> None:
        for name in ("scratchpad_write", "scratchpad_append"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is False, f"{name} should not be read_only"

    def test_all_tools_have_scratchpad_toolset(self, registry: ToolRegistry) -> None:
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "scratchpad"

    def test_tool_definitions_valid_openai_format(self, registry: ToolRegistry) -> None:
        for d in registry.tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_write_requires_content(self, registry: ToolRegistry) -> None:
        spec = registry.get("scratchpad_write")
        assert spec is not None
        assert "content" in spec.input_schema.get("required", [])

    def test_append_requires_section_and_content(self, registry: ToolRegistry) -> None:
        spec = registry.get("scratchpad_append")
        assert spec is not None
        required = spec.input_schema.get("required", [])
        assert "section" in required
        assert "content" in required


class TestScratchpadHandlers:
    async def test_read_empty(self, registry: ToolRegistry) -> None:
        result = await registry.execute("scratchpad_read", "{}")
        assert "empty" in result

    async def test_write_and_read(self, registry: ToolRegistry) -> None:
        await registry.execute(
            "scratchpad_write", json.dumps({"content": "# Plan\nStep 1"})
        )
        result = await registry.execute("scratchpad_read", "{}")
        assert "Plan" in result
        assert "Step 1" in result

    async def test_append_creates_section(self, registry: ToolRegistry) -> None:
        await registry.execute(
            "scratchpad_append",
            json.dumps({"section": "Observations", "content": "Found a bug"}),
        )
        result = await registry.execute("scratchpad_read", "{}")
        assert "Observations" in result
        assert "Found a bug" in result

    async def test_write_overwrites(self, registry: ToolRegistry) -> None:
        await registry.execute(
            "scratchpad_write", json.dumps({"content": "first"})
        )
        await registry.execute(
            "scratchpad_write", json.dumps({"content": "second"})
        )
        result = await registry.execute("scratchpad_read", "{}")
        assert "second" in result
        assert "first" not in result
