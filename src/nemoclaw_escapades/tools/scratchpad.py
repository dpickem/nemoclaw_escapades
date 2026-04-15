"""Scratchpad tools — tool-adapter layer for the agent's working memory.

This is a thin bridge that exposes the ``Scratchpad`` instance (defined
in ``agent.scratchpad``) to the LLM via the ``ToolRegistry``.  All
domain logic (file I/O, truncation, section management, context
formatting) lives in the core ``Scratchpad`` class; the functions here
simply delegate to its methods.

The ``AgentLoop`` registers these tools automatically when a
``Scratchpad`` instance is provided, so tool modules outside the agent
package don't need to know about the scratchpad.
"""

from __future__ import annotations

from nemoclaw_escapades.agent.scratchpad import Scratchpad
from nemoclaw_escapades.tools.registry import ToolRegistry, tool

# Logical toolset name used by the registry for grouping
_TOOLSET: str = "scratchpad"


def register_scratchpad_tools(registry: ToolRegistry, scratchpad: Scratchpad) -> None:
    """Register scratchpad_read, scratchpad_write, and scratchpad_append tools.

    Args:
        registry: The tool registry to populate.
        scratchpad: The ``Scratchpad`` instance these tools operate on.
    """

    @tool(
        "scratchpad_read",
        "Read the agent's working memory (scratchpad) for the current task.",
        {"type": "object", "properties": {}},
        display_name="Reading scratchpad",
        toolset=_TOOLSET,
    )
    async def scratchpad_read() -> str:
        """Return the current scratchpad contents, or a placeholder if empty."""
        content = scratchpad.read()
        return content if content.strip() else "(scratchpad is empty)"

    @tool(
        "scratchpad_write",
        "Overwrite the scratchpad with new content. Use for recording plans, observations, and decisions.",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Full scratchpad content (Markdown).",
                },
            },
            "required": ["content"],
        },
        display_name="Writing scratchpad",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def scratchpad_write(content: str) -> str:
        """Overwrite the scratchpad with *content*."""
        return scratchpad.write(content)

    @tool(
        "scratchpad_append",
        "Append content under a named section in the scratchpad. Creates the section if it doesn't exist.",
        {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Section name (e.g. 'Observations', 'Plan').",
                },
                "content": {"type": "string", "description": "Text to append."},
            },
            "required": ["section", "content"],
        },
        display_name="Appending to scratchpad",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def scratchpad_append(section: str, content: str) -> str:
        """Append *content* under the heading *section*."""
        return scratchpad.append(section, content)

    registry.register(scratchpad_read)
    registry.register(scratchpad_write)
    registry.register(scratchpad_append)
