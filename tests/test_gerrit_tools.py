"""Tests for the Gerrit tool registration and handlers."""

from __future__ import annotations

from nemoclaw_escapades.config import GerritConfig
from nemoclaw_escapades.tools.gerrit import (
    GerritClient,
    register_gerrit_tools,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

_TEST_CONFIG = GerritConfig(
    url="https://gerrit.example.com",
    username="testuser",
    http_password="testpass",
)

_EXPECTED_TOOLS = {
    "gerrit_get_change",
    "gerrit_list_changes",
    "gerrit_get_change_detail",
    "gerrit_get_comments",
    "gerrit_list_files",
    "gerrit_get_diff",
    "gerrit_me",
    "gerrit_set_review",
    "gerrit_submit",
    "gerrit_abandon",
}


class TestGerritToolRegistration:
    def test_registers_all_tools(self) -> None:
        registry = ToolRegistry()
        register_gerrit_tools(registry, _TEST_CONFIG)
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_gerrit_tools(registry, _TEST_CONFIG)
        read_tools = _EXPECTED_TOOLS - {"gerrit_set_review", "gerrit_submit", "gerrit_abandon"}
        for name in read_tools:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_write_tools_are_not_read_only(self) -> None:
        registry = ToolRegistry()
        register_gerrit_tools(registry, _TEST_CONFIG)
        for name in ("gerrit_set_review", "gerrit_submit", "gerrit_abandon"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is False, f"{name} should not be read_only"

    def test_all_tools_have_gerrit_toolset(self) -> None:
        registry = ToolRegistry()
        register_gerrit_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "gerrit"

    def test_tool_definitions_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_gerrit_tools(registry, _TEST_CONFIG)
        for d in registry.all_tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_unconfigured_tools_not_registered(self) -> None:
        registry = ToolRegistry()
        empty = GerritConfig(url="https://gerrit.example.com", username="", http_password="")
        register_gerrit_tools(registry, empty)
        assert len(registry) == 0


class TestGerritClient:
    def test_unconfigured_client(self) -> None:
        client = GerritClient(base_url="https://gerrit.example.com")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = GerritClient(
            base_url="https://gerrit.example.com",
            username="user",
            http_password="pass",
        )
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = GerritClient(base_url="https://gerrit.example.com")
        result = await client.get_account()
        assert "error" in result


class TestGerritHandlers:
    async def test_unconfigured_handler_returns_error(self) -> None:
        """Handler with empty credentials returns error JSON without network calls."""
        client = GerritClient(base_url="https://gerrit.example.com")
        result = await client.get_account()
        assert "error" in result
