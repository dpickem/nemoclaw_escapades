"""Tests for the Confluence tool registration and handlers."""

from __future__ import annotations

import json

from nemoclaw_escapades.config import ConfluenceConfig
from nemoclaw_escapades.tools.confluence import (
    ConfluenceClient,
    register_confluence_tools,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

_TEST_CONFIG = ConfluenceConfig(
    url="https://confluence.example.com",
    username="testuser",
    api_token="test-api-token",
)

_EXPECTED_TOOLS = {
    "confluence_search",
    "confluence_get_page",
    "confluence_get_page_children",
    "confluence_get_comments",
    "confluence_get_labels",
    "confluence_create_page",
    "confluence_update_page",
    "confluence_add_comment",
    "confluence_add_label",
}


class TestConfluenceToolRegistration:
    def test_registers_all_tools(self) -> None:
        registry = ToolRegistry()
        register_confluence_tools(registry, _TEST_CONFIG)
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_confluence_tools(registry, _TEST_CONFIG)
        read_tools = {
            "confluence_search",
            "confluence_get_page",
            "confluence_get_page_children",
            "confluence_get_comments",
            "confluence_get_labels",
        }
        for name in read_tools:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_write_tools_are_not_read_only(self) -> None:
        registry = ToolRegistry()
        register_confluence_tools(registry, _TEST_CONFIG)
        write_tools = {
            "confluence_create_page",
            "confluence_update_page",
            "confluence_add_comment",
            "confluence_add_label",
        }
        for name in write_tools:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is False, f"{name} should not be read_only"

    def test_all_tools_have_confluence_toolset(self) -> None:
        registry = ToolRegistry()
        register_confluence_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "confluence"

    def test_tool_definitions_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_confluence_tools(registry, _TEST_CONFIG)
        for d in registry.tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_unconfigured_tools_not_registered(self) -> None:
        registry = ToolRegistry()
        empty = ConfluenceConfig(url="https://confluence.example.com", username="", api_token="")
        register_confluence_tools(registry, empty)
        assert len(registry) == 0


class TestConfluenceClient:
    def test_unconfigured_client(self) -> None:
        client = ConfluenceClient(base_url="https://confluence.example.com")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = ConfluenceClient(
            base_url="https://confluence.example.com",
            username="user",
            api_token="token",
        )
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = ConfluenceClient(base_url="https://confluence.example.com")
        result = await client.search("test")
        assert "error" in result


class TestConfluenceHandlers:
    async def test_unconfigured_handler_returns_error_json(self) -> None:
        """Handler with empty credentials returns error JSON without network calls."""
        client = ConfluenceClient(base_url="https://confluence.example.com")
        result = await client.search("test")
        assert "error" in result
