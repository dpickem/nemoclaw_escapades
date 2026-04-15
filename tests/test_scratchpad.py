"""Tests for the agent scratchpad."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nemoclaw_escapades.agent.scratchpad import Scratchpad
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.scratchpad import register_scratchpad_tools


@pytest.fixture
def scratchpad(tmp_path: Path) -> Scratchpad:
    return Scratchpad(str(tmp_path / "scratchpad.md"))


class TestScratchpadClass:
    def test_creates_empty_file(self, tmp_path: Path) -> None:
        path = str(tmp_path / "sp.md")
        Scratchpad(path)
        assert Path(path).exists()
        assert Path(path).read_text() == ""

    def test_read_empty(self, scratchpad: Scratchpad) -> None:
        assert scratchpad.read() == ""

    def test_write_and_read(self, scratchpad: Scratchpad) -> None:
        scratchpad.write("hello")
        assert scratchpad.read() == "hello"

    def test_write_returns_confirmation(self, scratchpad: Scratchpad) -> None:
        result = scratchpad.write("test content")
        assert "updated" in result.lower()
        assert "12 bytes" in result

    def test_write_truncates_at_max_size(self, tmp_path: Path) -> None:
        sp = Scratchpad(str(tmp_path / "sp.md"), max_size=10)
        result = sp.write("a" * 100)
        assert "truncated" in result.lower()
        assert len(sp.read()) <= 10

    def test_append_creates_section(self, scratchpad: Scratchpad) -> None:
        scratchpad.append("Plan", "Step 1: read the code")
        content = scratchpad.read()
        assert "## Plan" in content
        assert "Step 1" in content

    def test_append_to_existing_section(self, scratchpad: Scratchpad) -> None:
        scratchpad.append("Notes", "Note 1")
        scratchpad.append("Notes", "Note 2")
        content = scratchpad.read()
        assert "Note 1" in content
        assert "Note 2" in content
        assert content.count("## Notes") == 1

    def test_snapshot_returns_contents(self, scratchpad: Scratchpad) -> None:
        scratchpad.write("snapshot test")
        assert scratchpad.snapshot() == "snapshot test"

    def test_context_block_empty(self, scratchpad: Scratchpad) -> None:
        assert scratchpad.context_block() == ""

    def test_context_block_with_content(self, scratchpad: Scratchpad) -> None:
        scratchpad.write("my notes")
        block = scratchpad.context_block()
        assert block.startswith("<scratchpad>")
        assert block.endswith("</scratchpad>")
        assert "my notes" in block


class TestScratchpadTools:
    @pytest.fixture
    def registry(self, scratchpad: Scratchpad) -> ToolRegistry:
        reg = ToolRegistry()
        register_scratchpad_tools(reg, scratchpad)
        return reg

    async def test_read_empty(self, registry: ToolRegistry) -> None:
        result = await registry.execute("scratchpad_read", "")
        assert "empty" in result.lower()

    async def test_write_and_read(self, registry: ToolRegistry) -> None:
        await registry.execute("scratchpad_write", json.dumps({"content": "hello"}))
        result = await registry.execute("scratchpad_read", "")
        assert "hello" in result

    async def test_append_and_read(self, registry: ToolRegistry) -> None:
        await registry.execute(
            "scratchpad_append",
            json.dumps({"section": "Findings", "content": "Found a bug"}),
        )
        result = await registry.execute("scratchpad_read", "")
        assert "## Findings" in result
        assert "Found a bug" in result

    def test_tool_registration(self, registry: ToolRegistry) -> None:
        assert "scratchpad_read" in registry
        assert "scratchpad_write" in registry
        assert "scratchpad_append" in registry

    def test_read_is_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("scratchpad_read")
        assert spec is not None
        assert spec.is_read_only is True

    def test_write_is_not_read_only(self, registry: ToolRegistry) -> None:
        spec = registry.get("scratchpad_write")
        assert spec is not None
        assert spec.is_read_only is False
