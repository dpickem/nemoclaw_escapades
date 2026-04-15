"""Tests for the web search tool registration and handlers."""

from __future__ import annotations

import json

from nemoclaw_escapades.config import WebSearchConfig
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.web_search import (
    BraveSearchClient,
    _format_search_results,
    register_web_search_tools,
)


_TEST_CONFIG = WebSearchConfig(api_key="test-brave-api-key")


class TestWebSearchToolRegistration:
    def test_registers_both_tools_with_key(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        assert set(registry.names) == {"web_search", "web_fetch"}

    def test_web_fetch_registered_without_key(self) -> None:
        """web_fetch works without an API key; web_search is skipped."""
        registry = ToolRegistry()
        register_web_search_tools(registry, WebSearchConfig(api_key=""))
        assert "web_fetch" in registry
        assert "web_search" not in registry

    def test_both_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        for name in ("web_search", "web_fetch"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_all_tools_have_web_search_toolset(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "web_search"

    def test_tool_definitions_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        for d in registry.tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_web_search_requires_query(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        spec = registry.get("web_search")
        assert spec is not None
        assert "query" in spec.input_schema.get("required", [])

    def test_web_fetch_requires_url(self) -> None:
        registry = ToolRegistry()
        register_web_search_tools(registry, _TEST_CONFIG)
        spec = registry.get("web_fetch")
        assert spec is not None
        assert "url" in spec.input_schema.get("required", [])


class TestBraveSearchClient:
    def test_unconfigured_client(self) -> None:
        client = BraveSearchClient(api_key="")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = BraveSearchClient(api_key="test-key")
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = BraveSearchClient(api_key="")
        result = await client.search("test")
        assert "error" in result


class TestFormatSearchResults:
    def test_formats_results(self) -> None:
        data = {
            "web": {
                "results": [
                    {"title": "Example", "url": "https://example.com", "description": "A test."},
                    {"title": "Other", "url": "https://other.com", "description": "Another."},
                ]
            }
        }
        result = _format_search_results(data)
        assert "1. Example" in result
        assert "https://example.com" in result
        assert "2. Other" in result

    def test_no_results(self) -> None:
        data = {"web": {"results": []}}
        result = _format_search_results(data)
        assert "No results" in result

    def test_error_passthrough(self) -> None:
        data = {"error": "API returned 401"}
        result = _format_search_results(data)
        parsed = json.loads(result)
        assert "error" in parsed


class TestWebFetchHandler:
    async def test_fetch_invalid_url(self) -> None:
        """Fetching an invalid URL returns an error string."""
        registry = ToolRegistry()
        register_web_search_tools(registry, WebSearchConfig(api_key=""))
        result = await registry.execute(
            "web_fetch", json.dumps({"url": "https://this-domain-does-not-exist-12345.example"})
        )
        assert "Error" in result
