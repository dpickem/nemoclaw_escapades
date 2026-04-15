"""Pre-built tool-registry factories for different agent personas.

Each factory assembles a ``ToolRegistry`` by calling ``register_*``
functions from the individual tool modules.  Tool *definitions* live in
their own files (``files.py``, ``bash.py``, …); this module only
composes them into ready-to-use registries.
"""

from __future__ import annotations

from nemoclaw_escapades.agent.scratchpad import Scratchpad
from nemoclaw_escapades.tools.bash import register_bash_tool
from nemoclaw_escapades.tools.files import register_file_tools
from nemoclaw_escapades.tools.git import register_git_tools
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.scratchpad import register_scratchpad_tools
from nemoclaw_escapades.tools.search import register_search_tools


def create_coding_tool_registry(
    workspace_root: str,
    scratchpad: Scratchpad | None = None,
) -> ToolRegistry:
    """Create a tool registry with all coding agent tools.

    Args:
        workspace_root: Absolute path to the workspace directory.
            All file/search/git/bash tools are rooted here.
        scratchpad: Optional scratchpad instance.  When provided,
            ``scratchpad_read``, ``scratchpad_write``, and
            ``scratchpad_append`` tools are registered.

    Returns:
        A fully populated ``ToolRegistry`` ready for injection into
        an ``AgentLoop``.
    """
    registry = ToolRegistry()

    register_file_tools(registry, workspace_root)
    register_search_tools(registry, workspace_root)
    register_bash_tool(registry, workspace_root)
    register_git_tools(registry, workspace_root)

    if scratchpad:
        register_scratchpad_tools(registry, scratchpad)

    return registry
