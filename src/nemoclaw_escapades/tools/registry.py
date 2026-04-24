"""Tool registry — maps tool names to handlers and generates OpenAI-format definitions.

The registry is the single source of truth for what tools the orchestrator
can invoke.  Each tool is a ``ToolSpec`` with a name, description, JSON Schema
for parameters, and an async handler function.  The registry:

1. Provides ``tool_definitions()`` for the inference API request.
2. Dispatches ``execute()`` calls to the right handler by name.
3. Carries metadata (read-only flag, toolset, availability) for the
   approval gate and startup diagnostics.
4. Enforces a per-tool output size cap (``max_result_chars``) so
   individual tools don't need their own truncation logic.

The ``@tool`` decorator wraps an async function into a ``ToolSpec`` with
an explicit name, description, and JSON Schema — no docstring parsing,
no type introspection.  Inspired by the Build Your Own OpenClaw tutorial's
decorator pattern.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from nemoclaw_escapades.models.types import FunctionDefinition, ToolDefinition
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("tools.registry")

# Applied when a tool's own max_result_chars is not set.
_DEFAULT_MAX_RESULT_CHARS: int = 8000

# Default number of matches returned by ``ToolRegistry.search``.
_DEFAULT_SEARCH_LIMIT: int = 5

# Minimum ``difflib.SequenceMatcher.ratio()`` for a field-level hit to
# count.  Calibrated to tolerate one-character typos ("jora"/"gira" vs
# "jira" ≈ 0.75) while rejecting the long-description coincidences
# ``difflib``'s 0.6 default would otherwise let through ("file" vs
# "single" ≈ 0.6 from shared "i" + "le" is noise, not a tool match).
_MIN_FIELD_SCORE: float = 0.7


def _field_score(query: str, field_value: str) -> float:
    """Best similarity of *query* against any token in *field_value*.

    Ratcliff/Obershelp via :class:`difflib.SequenceMatcher` applied to
    (a) the full field string and (b) each whitespace/underscore-
    separated token inside it — so ``"jira"`` matches the ``jira``
    token inside ``search_jira`` even though the full-string ratio
    would be diluted by the ``search_`` prefix.  Below
    :data:`_MIN_FIELD_SCORE` the match is discarded as noise.

    Args:
        query: Single query term, already lowercased by the caller.
        field_value: Tool field to search (lowercased by the caller).

    Returns:
        Best ratio in ``[0.0, 1.0]``, or ``0.0`` if the best match
        falls below the noise threshold.
    """
    if not field_value:
        return 0.0
    tokens = [field_value, *field_value.replace("_", " ").split()]
    best = max(SequenceMatcher(None, query, tok).ratio() for tok in tokens)
    return best if best >= _MIN_FIELD_SCORE else 0.0


@dataclass
class ToolSpec:
    """Definition of a single tool the orchestrator can invoke.

    Attributes:
        name: Unique tool name (must match what the model emits).
        description: Human-readable description shown to the model.
        input_schema: JSON Schema object for the tool's parameters.
        handler: Async callable that accepts keyword arguments matching
            the schema and returns a string result.
        is_read_only: Whether this tool only reads data (affects approval).
        is_concurrency_safe: Whether this tool can safely run in parallel
            with other concurrent-safe tool calls via ``asyncio.gather``.
            Default ``True`` (safe).  Set ``False`` for tools that mutate
            shared workspace state (``write_file``, ``edit_file``,
            ``bash``, ``git_commit``, etc.).
        is_core: Whether this tool is always in the prompt's tool list
            (``True``, default) or discoverable only via ``tool_search``
            (``False``).  Core tools are the ones the agent needs on
            every turn (file / search / bash / git + ``tool_search``
            itself); non-core tools are domain-specific service tools
            (Jira, GitLab, web search, …) that bloat the prompt when
            they're always visible.
        display_name: Short label for the thinking indicator
            (e.g. "Searching Jira"). Falls back to ``name`` if empty.
        toolset: Logical group this tool belongs to (e.g. ``"jira"``,
            ``"confluence"``).  Used to enable/disable whole services.
        check_fn: Optional callable that returns ``True`` when the tool
            is usable (e.g. API key is set).  Checked at registration
            time; tools that fail the check are skipped with a warning.
        max_result_chars: Hard cap on the string returned by the handler.
            The registry truncates longer results in ``execute()``.
            Defaults to ``_DEFAULT_MAX_RESULT_CHARS``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    is_core: bool = True
    display_name: str = ""
    toolset: str = ""
    check_fn: Callable[[], bool] | None = None
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS

    def to_definition(self) -> ToolDefinition:
        """Produce the wire-format ``ToolDefinition`` for the inference API."""
        return ToolDefinition(
            function=FunctionDefinition(
                name=self.name,
                description=self.description,
                parameters=self.input_schema,
            )
        )


# ── @tool decorator ──────────────────────────────────────────────────


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    display_name: str = "",
    toolset: str = "",
    is_read_only: bool = True,
    is_concurrency_safe: bool = True,
    is_core: bool = True,
    check_fn: Callable[[], bool] | None = None,
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS,
) -> Callable[[Callable[..., Awaitable[str]]], ToolSpec]:
    """Decorator that wraps an async function into a ``ToolSpec``.

    The caller supplies the tool name, description, and JSON Schema
    explicitly — no docstring parsing or type introspection.

    Usage::

        @tool(
            "read_file",
            "Read a text file from the workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path."},
                },
                "required": ["path"],
            },
            display_name="Reading file",
            toolset="files",
        )
        async def read_file(path: str) -> str:
            ...

        # read_file is now a ToolSpec, not a function.
        registry.register(read_file)
    """

    def decorator(fn: Callable[..., Awaitable[str]]) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=description,
            input_schema=parameters,
            handler=fn,
            is_read_only=is_read_only,
            is_concurrency_safe=is_concurrency_safe,
            is_core=is_core,
            display_name=display_name,
            toolset=toolset,
            check_fn=check_fn,
            max_result_chars=max_result_chars,
        )

    return decorator


def _truncate(text: str, limit: int) -> str:
    """Trim *text* to *limit* characters, appending an omission notice."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n... (truncated, {omitted} chars omitted)"


class ToolRegistry:
    """Registry of available tools.

    Registration is one-shot at startup.  The only mutable state at
    runtime is the :attr:`_surfaced_non_core` set — a per-task buffer
    that tracks which non-core tools the :mod:`tool_search` meta-tool
    has exposed during the current :meth:`AgentLoop.run` invocation.
    :class:`~nemoclaw_escapades.agent.loop.AgentLoop` calls
    :meth:`reset_tool_surface` at the start of every ``run()`` so
    each task begins with only the core tools visible.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._skipped_toolsets: set[str] = set()
        # Non-core tool names made visible to the model by the
        # ``tool_search`` meta-tool during the current task.
        self._surfaced_non_core: set[str] = set()

    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification.

        If the spec has a ``check_fn`` and it returns ``False``, the
        tool is **not** registered.  A warning is logged once per
        toolset (not per tool) to keep startup logs concise.

        Args:
            spec: The tool to register.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} already registered")

        if spec.check_fn is not None and not spec.check_fn():
            if spec.toolset and spec.toolset not in self._skipped_toolsets:
                self._skipped_toolsets.add(spec.toolset)
                logger.warning(
                    "Toolset not available, skipping",
                    extra={"toolset": spec.toolset},
                )
            elif not spec.toolset:
                logger.warning(
                    "Tool not available, skipping",
                    extra={"tool": spec.name},
                )
            return

        self._tools[spec.name] = spec
        logger.info(
            "Registered tool",
            extra={"tool": spec.name, "toolset": spec.toolset},
        )

    def get(self, name: str) -> ToolSpec | None:
        """Look up a tool by name, or ``None`` if not registered."""
        return self._tools.get(name)

    def display_name(self, name: str) -> str:
        """Return the thinking-indicator label for *name*, falling back to the raw name."""
        spec = self._tools.get(name)
        if spec and spec.display_name:
            return spec.display_name
        return f"Running {name}"

    @property
    def names(self) -> list[str]:
        """All registered tool names."""
        return list(self._tools)

    @property
    def toolsets(self) -> set[str]:
        """Distinct toolset names across all registered tools."""
        return {s.toolset for s in self._tools.values() if s.toolset}

    def names_in_toolset(self, toolset: str) -> list[str]:
        """Return tool names belonging to *toolset*."""
        return [s.name for s in self._tools.values() if s.toolset == toolset]

    # ── Non-core tool surfacing ─────────────────────────────────────
    #
    # OpenAI-compatible tool-calling constrains the model's
    # ``tool_calls`` output against the request's ``tools`` list
    # (Anthropic's tool-use protocol does the same).  A tool whose
    # name isn't in ``tools`` literally can't be emitted — most
    # backends apply constrained decoding, and the permissive ones
    # reject the request server-side.  Showing a tool's schema to
    # the model via a chat-history message (as the ``tool_search``
    # result does) is therefore *discovery metadata* only; actually
    # letting the model call the tool next round requires the
    # backend to advertise it in ``tools``.
    #
    # ``_surfaced_non_core`` bridges that gap.  ``tool_search`` calls
    # :meth:`mark_surfaced` to mirror its matches into the next
    # round's ``tools`` list; :meth:`tool_definitions` reads the set;
    # :class:`~nemoclaw_escapades.agent.loop.AgentLoop` calls
    # :meth:`reset_tool_surface` at the start of each ``run()`` so
    # cross-task carryover can't accidentally expose tools.

    @property
    def core_names(self) -> list[str]:
        """Registered tools flagged ``is_core=True`` (always in prompt)."""
        return [s.name for s in self._tools.values() if s.is_core]

    @property
    def non_core_names(self) -> list[str]:
        """Registered tools flagged ``is_core=False`` (searchable)."""
        return [s.name for s in self._tools.values() if not s.is_core]

    @property
    def surfaced_non_core(self) -> frozenset[str]:
        """Non-core tools currently surfaced for the active task."""
        return frozenset(self._surfaced_non_core)

    def reset_tool_surface(self) -> None:
        """Forget which non-core tools are surfaced (start of a new task)."""
        self._surfaced_non_core.clear()

    def mark_surfaced(self, names: Iterable[str]) -> None:
        """Mark non-core tools as surfaced so they appear in the next round.

        Unknown names are silently ignored (e.g. ``tool_search`` on a
        query with no matches).  Core tools are also ignored — they're
        always visible, so there's nothing to track.
        """
        for name in names:
            spec = self._tools.get(name)
            if spec is not None and not spec.is_core:
                self._surfaced_non_core.add(name)

    def tool_definitions(self) -> list[ToolDefinition]:
        """Return typed tool definitions for the inference API.

        Always includes core tools.  Non-core tools are included only
        after ``tool_search`` has surfaced them via :meth:`mark_surfaced`.
        """
        return [
            spec.to_definition()
            for spec in self._tools.values()
            if spec.is_core or spec.name in self._surfaced_non_core
        ]

    def all_tool_definitions(self) -> list[ToolDefinition]:
        """Return every registered tool's definition, ignoring surface state.

        For tests and diagnostics — the loop should use
        :meth:`tool_definitions` so non-core tools stay out of the
        default prompt.  Kept as a separate method (rather than a
        kwarg) so the loop call site can't accidentally opt into the
        all-tools view.
        """
        return [spec.to_definition() for spec in self._tools.values()]

    def search(self, query: str, *, limit: int = _DEFAULT_SEARCH_LIMIT) -> list[ToolSpec]:
        """Fuzzy-search non-core tools by name, toolset, and description.

        Uses :class:`difflib.SequenceMatcher` (Ratcliff/Obershelp) per
        field so queries with typos or partial words still hit — a
        bespoke substring scorer wouldn't find ``search_jira`` for
        ``"jora"``.  Per-field scores are weighted 3 (name) / 2
        (toolset) / 1 (description) and summed across query terms.

        Args:
            query: Natural-language query (whitespace-split into terms).
            limit: Maximum number of matches to return.

        Returns:
            Matching non-core :class:`ToolSpec` instances, sorted by
            descending total score with name as tie-breaker.
        """
        terms = [t.lower() for t in query.split() if t]
        if not terms:
            return []

        scored: list[tuple[float, ToolSpec]] = []
        for spec in self._tools.values():
            if spec.is_core:
                continue
            name_l = spec.name.lower()
            toolset_l = spec.toolset.lower()
            desc_l = spec.description.lower()
            total = 0.0
            for term in terms:
                total += (
                    _field_score(term, name_l) * 3
                    + _field_score(term, toolset_l) * 2
                    + _field_score(term, desc_l) * 1
                )
            if total > 0:
                scored.append((total, spec))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [spec for _, spec in scored[:limit]]

    async def execute(self, name: str, arguments_json: str) -> str:
        """Execute a tool by name with JSON-encoded arguments.

        The result is truncated to the tool's ``max_result_chars`` limit
        so individual handlers don't need their own truncation logic.

        Args:
            name: Tool name (must be registered).
            arguments_json: JSON string of keyword arguments.

        Returns:
            String result from the tool handler, possibly truncated.

        Raises:
            KeyError: If the tool name is not registered.
            json.JSONDecodeError: If arguments_json is invalid JSON.
        """
        spec = self._tools[name]
        args: dict[str, Any] = json.loads(arguments_json) if arguments_json else {}
        logger.info("Executing tool", extra={"tool": name, "args_keys": list(args)})
        result = await spec.handler(**args)
        return _truncate(result, spec.max_result_chars)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
