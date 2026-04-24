"""Tests for the ``tool_search`` meta-tool and the registry's surface state.

Covers the mechanism half of M2b §14 Phase 2:

- ``ToolSpec.is_core`` defaults to ``True`` (back-compat with every
  existing tool module).
- ``ToolRegistry.tool_definitions`` hides non-core tools until
  surfaced.
- ``ToolRegistry.search`` ranks by where the query matches (name /
  toolset / description) using ``difflib`` fuzzy matching.
- ``tool_search`` marks matches as surfaced and returns a JSON payload
  the model can use to invoke them.

Integration-level tests (factory wiring, service tools flipped
non-core) live in a follow-up commit alongside the factory / service
edits they validate.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool
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

    def test_reset_surface_wipes_surfaced_tools(self) -> None:
        registry = self._populated()
        registry.mark_surfaced(["search_jira", "fetch_web"])
        registry.reset_surface()
        assert registry.surfaced_non_core == frozenset()
        names = {d.function.name for d in registry.tool_definitions()}
        assert names == {"read_file"}


class TestRegistrySearch:
    """Keyword scoring ranks name matches ahead of toolset / description."""

    def _jira_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            _echo_tool(
                "read_file", "read a workspace file, including jira-exported text files"
            )
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
            _echo_tool(
                "get_jira_issue", "fetch a single Jira issue", toolset="jira", is_core=False
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
        raw = await registry.execute(
            "tool_search", json.dumps({"query": "jira", "limit": 999})
        )
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
