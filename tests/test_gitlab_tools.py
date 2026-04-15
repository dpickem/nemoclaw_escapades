"""Tests for the GitLab tool registration and handlers."""

from __future__ import annotations

import json

from nemoclaw_escapades.config import GitLabConfig
from nemoclaw_escapades.tools.gitlab import (
    GitLabClient,
    register_gitlab_tools,
)
from nemoclaw_escapades.tools.registry import ToolRegistry

_TEST_CONFIG = GitLabConfig(
    url="https://gitlab.example.com",
    token="glpat-test-token",
)

_EXPECTED_READ_TOOLS = {
    "gitlab_search_projects",
    "gitlab_get_project",
    "gitlab_list_merge_requests",
    "gitlab_get_merge_request",
    "gitlab_get_merge_request_changes",
    "gitlab_list_pipelines",
    "gitlab_get_pipeline",
    "gitlab_list_pipeline_jobs",
    "gitlab_get_job_log",
    "gitlab_get_file",
    "gitlab_list_mr_notes",
    "gitlab_list_mr_discussions",
    "gitlab_get_mr_approvals",
    "gitlab_list_branches",
    "gitlab_list_commits",
    "gitlab_get_commit",
    "gitlab_compare",
    "gitlab_me",
}

_EXPECTED_WRITE_TOOLS = {
    "gitlab_create_mr_note",
    "gitlab_update_mr_note",
    "gitlab_reply_to_discussion",
    "gitlab_resolve_discussion",
    "gitlab_approve_mr",
    "gitlab_unapprove_mr",
    "gitlab_merge_mr",
    "gitlab_rebase_mr",
    "gitlab_update_mr",
    "gitlab_create_mr",
}

_EXPECTED_TOOLS = _EXPECTED_READ_TOOLS | _EXPECTED_WRITE_TOOLS


class TestGitLabToolRegistration:
    def test_registers_all_tools(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, _TEST_CONFIG)
        assert set(registry.names) == _EXPECTED_TOOLS

    def test_read_tools_are_read_only(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, _TEST_CONFIG)
        for name in _EXPECTED_READ_TOOLS:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is True, f"{name} should be read_only"

    def test_write_tools_are_not_read_only(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, _TEST_CONFIG)
        for name in _EXPECTED_WRITE_TOOLS:
            spec = registry.get(name)
            assert spec is not None
            assert spec.is_read_only is False, f"{name} should NOT be read_only"

    def test_all_tools_have_gitlab_toolset(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, _TEST_CONFIG)
        for name in registry.names:
            spec = registry.get(name)
            assert spec is not None
            assert spec.toolset == "gitlab"

    def test_tool_definitions_valid_openai_format(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, _TEST_CONFIG)
        for d in registry.tool_definitions():
            assert d.type == "function"
            assert d.function.name
            assert d.function.description
            assert d.function.parameters is not None

    def test_unconfigured_tools_not_registered(self) -> None:
        registry = ToolRegistry()
        register_gitlab_tools(registry, GitLabConfig(url="https://gitlab.example.com", token=""))
        assert len(registry) == 0


class TestGitLabClient:
    def test_unconfigured_client(self) -> None:
        client = GitLabClient(base_url="https://gitlab.example.com", token="")
        assert client.configured is False

    def test_configured_client(self) -> None:
        client = GitLabClient(base_url="https://gitlab.example.com", token="glpat-test")
        assert client.configured is True

    async def test_unconfigured_returns_error(self) -> None:
        client = GitLabClient(base_url="https://gitlab.example.com", token="")
        result = await client.get_current_user()
        assert "error" in result


class TestGitLabHandlers:
    async def test_handlers_return_json_string(self) -> None:
        import nemoclaw_escapades.tools.gitlab as mod

        mod._gitlab_config = GitLabConfig(url="https://gitlab.example.com", token="")
        result = await mod.gitlab_me()
        data = json.loads(result)
        assert isinstance(data, dict)
        assert "error" in data
