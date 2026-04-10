"""Tests for the Jira tool registration and handlers."""

from __future__ import annotations

import json

from nemoclaw_escapades.config import JiraConfig
from nemoclaw_escapades.tools.jira import (
    JiraClient,
    register_jira_tools,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

# Config with a dummy auth header so check_fn passes during registration.
_TEST_CONFIG = JiraConfig(
    url="https://jira.example.com",
    auth_header="Basic dGVzdDp0ZXN0",
)


class TestJiraToolRegistration:
    def test_registers_all_tools(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        expected = {
            "jira_get_issue",
            "jira_search",
            "jira_me",
            "jira_get_transitions",
            "jira_create_issue",
            "jira_update_issue",
            "jira_add_comment",
            "jira_transition_issue",
        }
        assert set(registry.names) == expected

    def test_read_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        for name in ("jira_get_issue", "jira_search", "jira_me", "jira_get_transitions"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_write_tools_are_not_read_only(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        write_tools = (
            "jira_create_issue",
            "jira_update_issue",
            "jira_add_comment",
            "jira_transition_issue",
        )
        for name in write_tools:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is False, f"{name} should not be read_only"

    def test_tool_definitions_are_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        defs = registry.tool_definitions()
        assert len(defs) == 8
        for d in defs:
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_search_tool_has_required_jql(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        spec = registry.get("jira_search")
        assert spec is not None
        assert "jql" in spec.input_schema.get("required", [])

    def test_all_tools_have_jira_toolset(self) -> None:
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "jira", f"{name} should have toolset='jira'"

    def test_unconfigured_tools_not_registered(self) -> None:
        """When auth_header is empty, check_fn returns False and tools are skipped."""
        registry = ToolRegistry()
        empty_config = JiraConfig(url="https://jira.example.com", auth_header="")
        register_jira_tools(registry, empty_config)
        assert len(registry) == 0


class TestJiraClient:
    def test_unconfigured_client(self) -> None:
        client = JiraClient(base_url="https://jira.example.com", auth_header="")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = JiraClient(
            base_url="https://jira.example.com",
            auth_header="Basic dGVzdDp0ZXN0",
        )
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = JiraClient(base_url="https://jira.example.com", auth_header="")
        result = await client.me()
        assert "error" in result


class TestJiraHandlers:
    async def test_handlers_return_json_string(self) -> None:
        """Handler returns valid JSON when the client has no auth."""
        import nemoclaw_escapades.tools.jira as jira_mod

        jira_mod._jira_config = JiraConfig(url="https://jira.example.com", auth_header="")
        result = await jira_mod.jira_me()
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "error" in data

    async def test_execute_via_registry(self) -> None:
        """Registry execute returns valid JSON when client has no auth."""
        registry = ToolRegistry()
        register_jira_tools(registry, _TEST_CONFIG)
        import nemoclaw_escapades.tools.jira as jira_mod

        jira_mod._jira_config = JiraConfig(url="https://jira.example.com", auth_header="")
        result = await registry.execute("jira_me", "{}")
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "error" in data
