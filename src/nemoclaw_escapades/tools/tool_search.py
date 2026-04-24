"""``tool_search`` meta-tool — keyword search over non-core tools.

Phase 2 (M2b) deliverable that keeps the default prompt's tool block
small.  Tools registered with ``is_core=False`` are hidden from the
inference API until ``tool_search`` surfaces them on demand, after
which the loop's next round sees them in the ``tools`` list and can
invoke them normally.

See :mod:`nemoclaw_escapades.tools.registry` for the surface-state
mechanics and ``docs/design_m2b.md`` §10 for the design motivation.
"""

from __future__ import annotations

import json
from typing import Any

from nemoclaw_escapades.tools.registry import ToolRegistry, tool

# Logical toolset name for registry diagnostics.
_TOOLSET: str = "meta"

# Default number of matches surfaced per invocation.  Lines up with the
# registry's own default but kept explicit here so the tool's JSON
# schema documents it.
_DEFAULT_LIMIT: int = 5

# Upper bound to stop a single ``tool_search`` call from surfacing
# every non-core tool at once (which would defeat the whole point).
_MAX_LIMIT: int = 15


def _summarise(
    spec_name: str, description: str, toolset: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Shape one search hit into the response payload.

    Kept outside the registered tool so the handler's only statement
    is "ask the registry, summarise, surface".  The model sees the
    tool name, a short description, the logical toolset it came from,
    and its input schema — enough to emit a valid tool call on the
    next round.
    """
    return {
        "name": spec_name,
        "toolset": toolset,
        "description": description,
        "input_schema": schema,
    }


def register_tool_search_tool(registry: ToolRegistry) -> None:
    """Register the ``tool_search`` meta-tool onto *registry*.

    The meta-tool is always a core tool (``is_core=True``) — it's how
    non-core tools get discovered in the first place.  Registration is
    a no-op when no non-core tools are present (the handler still
    registers but returns "no tools match" for every query); callers
    can check ``registry.non_core_names`` if they want to skip
    registration entirely.

    Args:
        registry: Registry to register onto.  The handler captures
            this reference in a closure so subsequent ``search`` calls
            hit the same registry the loop reads.
    """

    @tool(
        "tool_search",
        "Search the registry for tools that aren't currently in your "
        "tools list.  Returns a ranked list of matching tool names, "
        "descriptions, and input schemas; the matches become callable "
        "on your next turn.  Use this when the user's request mentions "
        "a system or service (Jira, GitLab, Confluence, the web, …) "
        "you don't already have a tool for.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords describing the capability you need "
                        "(e.g. 'jira issue', 'gitlab merge request', "
                        "'web search')."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "default": _DEFAULT_LIMIT,
                    "description": ("Maximum number of tools to surface this round."),
                },
            },
            "required": ["query"],
        },
        display_name="Searching tools",
        toolset=_TOOLSET,
        is_core=True,
        is_read_only=True,
    )
    async def tool_search(query: str, limit: int = _DEFAULT_LIMIT) -> str:
        """Return matching non-core tools and mark them surfaced.

        Args:
            query: Keyword query.  Passed as-is to
                :meth:`ToolRegistry.search`, which tokenises on
                whitespace and does case-insensitive substring matching.
            limit: Hard-capped at ``_MAX_LIMIT``; defaults to
                ``_DEFAULT_LIMIT``.

        Returns:
            A JSON object with ``query``, the resolved ``limit``, and
            a list of ``matches`` (each with name / toolset / description
            / input_schema).  Also registers the returned names as
            surfaced so the model can invoke them next round.
        """
        # Distinguish ``limit=None`` (use default) from ``limit=0`` /
        # negative (clamp up to 1).  ``limit or _DEFAULT_LIMIT`` would
        # lose the second case — it treats 0 as falsy and substitutes
        # the default, masking a malformed tool call.
        requested = _DEFAULT_LIMIT if limit is None else int(limit)
        bounded_limit = max(1, min(requested, _MAX_LIMIT))
        hits = registry.search(query, limit=bounded_limit)
        registry.mark_surfaced(spec.name for spec in hits)
        payload: dict[str, Any] = {
            "query": query,
            "limit": bounded_limit,
            "matches": [
                _summarise(
                    spec.name,
                    spec.description,
                    spec.toolset,
                    spec.input_schema,
                )
                for spec in hits
            ],
        }
        return json.dumps(payload, indent=2)

    registry.register(tool_search)
