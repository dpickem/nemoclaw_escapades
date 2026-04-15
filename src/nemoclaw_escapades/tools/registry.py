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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nemoclaw_escapades.models.types import FunctionDefinition, ToolDefinition
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("tools.registry")

# Applied when a tool's own max_result_chars is not set.
_DEFAULT_MAX_RESULT_CHARS: int = 8000


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

    Thread-safe for reads after initial registration (no runtime mutation).
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._skipped_toolsets: set[str] = set()

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

    def tool_definitions(self) -> list[ToolDefinition]:
        """Return typed tool definitions for the inference API."""
        return [spec.to_definition() for spec in self._tools.values()]

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
