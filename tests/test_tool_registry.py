"""Tests for the tool registry."""

from __future__ import annotations

import json

import pytest

from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec


async def _echo_handler(message: str = "default") -> str:
    return f"echo: {message}"


async def _sum_handler(a: int = 0, b: int = 0) -> str:
    return str(a + b)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="Echo a message",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            handler=_echo_handler,
        )
    )
    return reg


class TestToolRegistry:
    def test_register_and_get(self, registry: ToolRegistry) -> None:
        spec = registry.get("echo")
        assert spec is not None
        assert spec.name == "echo"
        assert spec.is_read_only is True

    def test_register_duplicate_raises(self, registry: ToolRegistry) -> None:
        with pytest.raises(ValueError, match="already registered"):
            registry.register(
                ToolSpec(
                    name="echo",
                    description="dupe",
                    input_schema={},
                    handler=_echo_handler,
                )
            )

    def test_get_missing_returns_none(self, registry: ToolRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_contains(self, registry: ToolRegistry) -> None:
        assert "echo" in registry
        assert "missing" not in registry

    def test_len(self, registry: ToolRegistry) -> None:
        assert len(registry) == 1

    def test_names(self, registry: ToolRegistry) -> None:
        assert registry.names == ["echo"]

    def test_tool_definitions_format(self, registry: ToolRegistry) -> None:
        defs = registry.tool_definitions()
        assert len(defs) == 1
        assert defs[0].type == "function"
        assert defs[0].function.name == "echo"
        assert defs[0].function.parameters is not None

    async def test_execute_with_args(self, registry: ToolRegistry) -> None:
        result = await registry.execute("echo", json.dumps({"message": "hello"}))
        assert result == "echo: hello"

    async def test_execute_with_empty_args(self, registry: ToolRegistry) -> None:
        result = await registry.execute("echo", "")
        assert result == "echo: default"

    async def test_execute_missing_tool_raises(self, registry: ToolRegistry) -> None:
        with pytest.raises(KeyError):
            await registry.execute("nonexistent", "{}")

    async def test_multiple_tools(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="echo",
                description="Echo",
                input_schema={},
                handler=_echo_handler,
            )
        )
        reg.register(
            ToolSpec(
                name="sum",
                description="Sum",
                input_schema={},
                handler=_sum_handler,
            )
        )
        assert len(reg) == 2
        assert await reg.execute("sum", json.dumps({"a": 3, "b": 4})) == "7"
