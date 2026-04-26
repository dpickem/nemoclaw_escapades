"""Tests for the Slack search tool registration and handlers."""

from __future__ import annotations

from nemoclaw_escapades.config import SlackSearchConfig
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.slack_search import (
    SlackSearchClient,
    register_slack_search_tools,
)

_TEST_CONFIG = SlackSearchConfig(user_token="xoxp-test-token")

_EXPECTED_TOOLS = {
    "slack_search_messages",
    "slack_list_channels",
    "slack_get_channel_history",
    "slack_get_thread_replies",
    "slack_get_user_info",
    "slack_send_message",
}


class TestSlackSearchToolRegistration:
    def test_registers_all_tools(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, _TEST_CONFIG)
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, _TEST_CONFIG)
        read_tools = _EXPECTED_TOOLS - {"slack_send_message"}
        for name in read_tools:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_write_tools_are_not_read_only(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, _TEST_CONFIG)
        spec = registry.get("slack_send_message")
        assert spec is not None
        assert spec.is_read_only is False

    def test_all_tools_have_slack_search_toolset(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "slack_search"

    def test_tool_definitions_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, _TEST_CONFIG)
        for d in registry.all_tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_unconfigured_tools_not_registered(self) -> None:
        registry = ToolRegistry()
        register_slack_search_tools(registry, SlackSearchConfig(user_token=""))
        assert len(registry) == 0


class TestSlackSearchClient:
    def test_unconfigured_client(self) -> None:
        client = SlackSearchClient(user_token="")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = SlackSearchClient(user_token="xoxp-test")
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = SlackSearchClient(user_token="")
        result = await client.search_messages("test")
        assert "error" in result


class TestSlackSearchHandlers:
    async def test_unconfigured_handler_returns_error(self) -> None:
        """Handler with empty token returns error JSON without network calls."""
        client = SlackSearchClient(user_token="")
        result = await client.search_messages("test")
        assert "error" in result
