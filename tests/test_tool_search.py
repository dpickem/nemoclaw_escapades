"""Tests for the ``tool_search`` meta-tool and the registry's surface state.

Covers M2b §14 Phase 2:

- ``ToolSpec.is_core`` defaults to ``True`` (back-compat with every
  existing tool module).
- ``ToolRegistry.tool_definitions`` hides non-core tools until
  surfaced.
- ``ToolRegistry.search`` ranks by where the query matches (name /
  toolset / description) using ``difflib`` fuzzy matching.
- ``tool_search`` marks matches as surfaced and returns a JSON payload
  the model can use to invoke them.
- Orchestrator's service tool modules declare ``is_core=False`` at
  each ``@tool`` site; coding-agent tool modules leave the default
  ``is_core=True`` intact so the full coding surface stays in the
  prompt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool
from nemoclaw_escapades.tools.tool_registry_factory import create_coding_tool_registry
from nemoclaw_escapades.tools.tool_search import register_tool_search_tool


def _echo_tool(name: str, description: str, *, toolset: str = "", is_core: bool = True) -> ToolSpec:
    """Build a ``ToolSpec`` whose handler just echoes its arguments.

    Tests don't care what the handler returns — they care about the
    registry bookkeeping around it.  Keeping the handler trivial
    keeps the test noise level down.
    """

    @tool(
        name,
        description,
        {"type": "object", "properties": {}},
        toolset=toolset,
        is_core=is_core,
    )
    async def _handler(**kwargs: Any) -> str:
        return f"{name}({kwargs})"

    return _handler


class TestIsCoreDefault:
    """New ``is_core`` field preserves existing behaviour by default."""

    def test_tool_spec_defaults_to_core(self) -> None:
        spec = _echo_tool("foo", "does foo")
        assert spec.is_core is True

    def test_decorator_respects_non_core_flag(self) -> None:
        spec = _echo_tool("foo", "does foo", is_core=False)
        assert spec.is_core is False


class TestRegistrySurface:
    """``tool_definitions`` gates non-core tools behind surface state."""

    def _populated(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(_echo_tool("read_file", "read a workspace file"))
        registry.register(
            _echo_tool("search_jira", "search Jira issues", toolset="jira", is_core=False)
        )
        registry.register(
            _echo_tool(
                "fetch_web", "fetch a URL from the open web", toolset="web_search", is_core=False
            )
        )
        return registry

    def test_core_names_and_non_core_names_partition_the_registry(self) -> None:
        registry = self._populated()
        assert registry.core_names == ["read_file"]
        assert set(registry.non_core_names) == {"search_jira", "fetch_web"}

    def test_default_tool_definitions_excludes_non_core(self) -> None:
        registry = self._populated()
        names = {d.function.name for d in registry.tool_definitions()}
        assert names == {"read_file"}

    def test_all_tool_definitions_includes_non_core(self) -> None:
        registry = self._populated()
        names = {d.function.name for d in registry.all_tool_definitions()}
        assert names == {"read_file", "search_jira", "fetch_web"}

    def test_mark_surfaced_includes_named_non_core_tool(self) -> None:
        registry = self._populated()
        registry.mark_surfaced(["search_jira"])
        names = {d.function.name for d in registry.tool_definitions()}
        assert names == {"read_file", "search_jira"}

    def test_mark_surfaced_ignores_unknown_and_core_names(self) -> None:
        registry = self._populated()
        registry.mark_surfaced(["read_file", "does_not_exist"])
        # Core stays core (not tracked); unknown silently dropped.
        assert registry.surfaced_non_core == frozenset()

    def test_reset_tool_surface_wipes_surfaced_tools(self) -> None:
        registry = self._populated()
        registry.mark_surfaced(["search_jira", "fetch_web"])
        registry.reset_tool_surface()
        assert registry.surfaced_non_core == frozenset()
        names = {d.function.name for d in registry.tool_definitions()}
        assert names == {"read_file"}


class TestRegistrySearch:
    """Keyword scoring ranks name matches ahead of toolset / description."""

    def _jira_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            _echo_tool("read_file", "read a workspace file, including jira-exported text files")
        )
        registry.register(
            _echo_tool("search_jira", "search Jira issues", toolset="jira", is_core=False)
        )
        registry.register(
            _echo_tool(
                "get_jira_issue", "fetch a single Jira issue by key", toolset="jira", is_core=False
            )
        )
        registry.register(
            _echo_tool(
                "fetch_web",
                "fetch a URL from the open web",
                toolset="web_search",
                is_core=False,
            )
        )
        return registry

    def test_empty_query_returns_no_matches(self) -> None:
        registry = self._jira_registry()
        assert registry.search("") == []
        assert registry.search("   ") == []

    def test_name_match_outranks_toolset_and_description(self) -> None:
        registry = self._jira_registry()
        hits = registry.search("jira")
        # ``search_jira`` and ``get_jira_issue`` both have ``jira`` in
        # name (+3) AND toolset (+2); the ``read_file`` entry has it in
        # description only (+1) but is core so it's excluded entirely.
        assert [spec.name for spec in hits] == ["get_jira_issue", "search_jira"]

    def test_core_tools_never_appear_in_search_results(self) -> None:
        registry = self._jira_registry()
        hits = registry.search("file")  # would match read_file's description
        assert [spec.name for spec in hits] == []

    def test_limit_caps_returned_results(self) -> None:
        registry = self._jira_registry()
        hits = registry.search("jira", limit=1)
        assert len(hits) == 1

    def test_typo_matches_via_difflib(self) -> None:
        """One-character typo still surfaces the intended tools."""
        registry = self._jira_registry()
        # ``jora`` vs ``jira`` ≈ 0.75 ratio — above the noise floor.
        hits = registry.search("jora")
        assert {spec.name for spec in hits} == {"search_jira", "get_jira_issue"}

    def test_unrelated_query_has_no_hits(self) -> None:
        """Noise floor rejects weak coincidences.

        Guards against ``difflib`` ratios dropping low-signal matches
        (e.g. ``"xyz"`` randomly scoring low positive ratios against
        every description) into the result set.
        """
        registry = self._jira_registry()
        assert registry.search("xyz") == []


class TestToolSearchTool:
    """The meta-tool's handler surfaces matches and returns JSON."""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            _echo_tool("search_jira", "search Jira issues", toolset="jira", is_core=False)
        )
        registry.register(
            _echo_tool("get_jira_issue", "fetch a single Jira issue", toolset="jira", is_core=False)
        )
        registry.register(
            _echo_tool(
                "fetch_web",
                "fetch a URL from the open web",
                toolset="web_search",
                is_core=False,
            )
        )
        register_tool_search_tool(registry)
        return registry

    @pytest.mark.asyncio
    async def test_tool_search_is_core_and_registered(
        self,
        registry: ToolRegistry,
    ) -> None:
        spec = registry.get("tool_search")
        assert spec is not None
        assert spec.is_core is True
        # And it shows up in the default tools list.
        names = {d.function.name for d in registry.tool_definitions()}
        assert "tool_search" in names

    @pytest.mark.asyncio
    async def test_tool_search_returns_matches_and_surfaces_them(
        self,
        registry: ToolRegistry,
    ) -> None:
        raw = await registry.execute("tool_search", json.dumps({"query": "jira"}))
        payload = json.loads(raw)

        assert payload["query"] == "jira"
        hit_names = [m["name"] for m in payload["matches"]]
        assert set(hit_names) == {"search_jira", "get_jira_issue"}

        # Each match carries enough schema info for the model to invoke it.
        for match in payload["matches"]:
            assert match["toolset"] == "jira"
            assert match["description"]
            assert match["input_schema"]["type"] == "object"

        # And the registry now exposes them in ``tool_definitions``.
        assert registry.surfaced_non_core == frozenset({"search_jira", "get_jira_issue"})
        names = {d.function.name for d in registry.tool_definitions()}
        assert {"search_jira", "get_jira_issue"}.issubset(names)

    @pytest.mark.asyncio
    async def test_tool_search_clamps_limit(self, registry: ToolRegistry) -> None:
        raw = await registry.execute("tool_search", json.dumps({"query": "jira", "limit": 999}))
        payload = json.loads(raw)
        assert payload["limit"] == 15  # clamped to _MAX_LIMIT

    @pytest.mark.asyncio
    async def test_tool_search_floor_limit(self, registry: ToolRegistry) -> None:
        raw = await registry.execute("tool_search", json.dumps({"query": "jira", "limit": 0}))
        payload = json.loads(raw)
        assert payload["limit"] == 1  # clamped up

    @pytest.mark.asyncio
    async def test_tool_search_no_matches_surfaces_nothing(
        self,
        registry: ToolRegistry,
    ) -> None:
        raw = await registry.execute("tool_search", json.dumps({"query": "absolutelynothing"}))
        payload = json.loads(raw)
        assert payload["matches"] == []
        assert registry.surfaced_non_core == frozenset()


class TestCodingToolRegistryRegistersToolSearch:
    """Sub-agent factory wires ``tool_search`` even though its surface is core-only."""

    def test_coding_registry_has_tool_search(self, tmp_path) -> None:
        registry = create_coding_tool_registry(str(tmp_path))
        assert "tool_search" in registry
        assert registry.get("tool_search").is_core is True

    def test_coding_registry_has_no_non_core_tools_by_default(self, tmp_path) -> None:
        # Coding sub-agent's full surface is deliberately core: file /
        # search / bash / git are used every turn.  ``tool_search``
        # rides along for future-compat but finds nothing today.
        registry = create_coding_tool_registry(str(tmp_path))
        assert registry.non_core_names == []


class TestFullToolRegistryIntegration:
    """End-to-end: ``build_full_tool_registry`` + ``tool_search`` cycle."""

    def _config_with_jira(self, workspace_root: str) -> Any:
        """Minimal ``AppConfig`` with Jira configured and other services off.

        Trims the integration surface to one service so the test
        doesn't depend on every service tool's registration quirks.
        """
        from nemoclaw_escapades.config import AppConfig

        config = AppConfig()
        config.coding.workspace_root = workspace_root
        # Configured Jira (auth header present) → tools register.
        config.jira.url = "https://jirasw.example.com"
        config.jira.auth_header = "Basic dGVzdDp0ZXN0"
        # Disable every other service so the surface stays small and
        # the ``tool_search``-surfaces-Jira assertion below is precise.
        config.gitlab.enabled = False
        config.gerrit.enabled = False
        config.confluence.enabled = False
        config.slack_search.enabled = False
        config.web_search.enabled = False
        return config

    @pytest.mark.asyncio
    async def test_service_tools_are_non_core_after_factory(
        self,
        tmp_path,
    ) -> None:
        from nemoclaw_escapades.tools.tool_registry_factory import (
            build_full_tool_registry,
        )

        config = self._config_with_jira(str(tmp_path))
        registry = build_full_tool_registry(config)

        # Jira tools registered and flagged non-core at their @tool sites.
        assert registry.names_in_toolset("jira"), "Jira tools should be registered"
        assert all(not registry.get(name).is_core for name in registry.names_in_toolset("jira"))

        # Default tool list is coding + tool_search — no Jira yet.
        default_names = {d.function.name for d in registry.tool_definitions()}
        assert "tool_search" in default_names
        for jira_name in registry.names_in_toolset("jira"):
            assert jira_name not in default_names

    @pytest.mark.asyncio
    async def test_tool_search_surfaces_service_tools_end_to_end(
        self,
        tmp_path,
    ) -> None:
        """Full cycle: agent calls ``tool_search('jira')`` → sees Jira tools."""
        from nemoclaw_escapades.tools.tool_registry_factory import (
            build_full_tool_registry,
        )

        config = self._config_with_jira(str(tmp_path))
        registry = build_full_tool_registry(config)

        before = {d.function.name for d in registry.tool_definitions()}
        raw = await registry.execute("tool_search", json.dumps({"query": "jira"}))
        after = {d.function.name for d in registry.tool_definitions()}
        payload = json.loads(raw)

        surfaced_names = {m["name"] for m in payload["matches"]}
        assert surfaced_names, "tool_search should surface at least one Jira tool"

        # Every surfaced tool now appears in the inference-round tools list.
        assert surfaced_names.issubset(after)
        # And none of them were in the pre-search default list.
        assert not surfaced_names.intersection(before)

    @pytest.mark.asyncio
    async def test_default_prompt_surface_shrinks(self, tmp_path) -> None:
        """Regression: core-only surface is a strict subset of full surface.

        Concrete token count comparisons are brittle across model
        tokenisers, so we assert the structural invariant that makes
        the 40%+ reduction possible: the default tool list is strictly
        smaller than the full tool list when any service is enabled.
        """
        from nemoclaw_escapades.tools.tool_registry_factory import (
            build_full_tool_registry,
        )

        config = self._config_with_jira(str(tmp_path))
        registry = build_full_tool_registry(config)

        core_count = len(registry.tool_definitions())
        full_count = core_count + len(registry.non_core_names)
        # At least the Jira tools + tool_search vs. just tool_search + coding.
        assert full_count > core_count
        # And the ratio confirms the reduction is meaningful — Jira
        # alone adds several tools, so core-only comes in well under
        # the full surface.
        assert core_count < full_count * 0.75
