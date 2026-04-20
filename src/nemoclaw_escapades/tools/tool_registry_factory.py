"""Pre-built tool-registry factories for different agent personas.

Factories assemble a ``ToolRegistry`` by calling ``register_*``
functions from the individual tool modules.  Tool *definitions* live
in their own files (``files.py``, ``bash.py``, …); this module only
composes them into ready-to-use registries.

Two entry points:

- ``create_coding_tool_registry`` — just the coding-agent suite
  (file/search/bash/git).  Useful for tests and sub-agents that don't
  need the full service stack.
- ``build_full_tool_registry`` — top-level factory that reads
  ``AppConfig`` and assembles the full process-wide registry: service
  tools (Jira/GitLab/Gerrit/Confluence/Slack/web) layered on top of
  the coding agent suite, plus an optional ``skill`` tool.  This is
  what ``main.py`` calls so the application entry point doesn't need
  to know which individual ``register_*`` functions exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nemoclaw_escapades.tools.bash import register_bash_tool
from nemoclaw_escapades.tools.confluence import register_confluence_tools
from nemoclaw_escapades.tools.files import register_file_tools
from nemoclaw_escapades.tools.gerrit import register_gerrit_tools
from nemoclaw_escapades.tools.git import register_git_tools
from nemoclaw_escapades.tools.gitlab import register_gitlab_tools
from nemoclaw_escapades.tools.jira import register_jira_tools
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.search import register_search_tools
from nemoclaw_escapades.tools.skill import register_skill_tool
from nemoclaw_escapades.tools.slack_search import register_slack_search_tools
from nemoclaw_escapades.tools.web_search import register_web_search_tools

if TYPE_CHECKING:
    from nemoclaw_escapades.agent.skill_loader import SkillLoader
    from nemoclaw_escapades.config import AppConfig


def create_coding_tool_registry(
    workspace_root: str,
    *,
    git_clone_allowed_hosts: str = "",
) -> ToolRegistry:
    """Create a registry with just the coding-agent tools.

    Args:
        workspace_root: Absolute path to the workspace directory.
            All file/search/git/bash tools are rooted here.
        git_clone_allowed_hosts: Comma/space-separated hostnames that
            ``git_clone`` accepts.  Empty disables ``git_clone``.

    Returns:
        A fully populated ``ToolRegistry`` ready for injection into
        an ``AgentLoop``.
    """
    registry = ToolRegistry()
    _register_coding_tools(
        registry,
        workspace_root=workspace_root,
        git_clone_allowed_hosts=git_clone_allowed_hosts,
    )
    return registry


def build_full_tool_registry(
    config: AppConfig,
    skill_loader: SkillLoader | None = None,
) -> ToolRegistry:
    """Top-level factory — assemble the full process-wide tool registry.

    ``main.py`` calls this once at startup.  Encapsulates which
    ``register_*`` functions exist and when they apply so the entry
    point stays config-driven and doesn't import every tool module.

    Args:
        config: Fully populated ``AppConfig``.  The ``enabled`` flag on
            each sub-config gates whether its tools are registered.
        skill_loader: Optional skill loader.  When provided (and
            non-empty), the ``skill`` tool is registered.

    Returns:
        A fully populated ``ToolRegistry``.  May be empty if nothing
        was enabled — callers should check ``len(registry) > 0``.
    """
    registry = ToolRegistry()

    # ── Service tools (external APIs) ──────────────────────────────
    # Each tool module does its own credential check_fn; a misconfigured
    # service is logged once and skipped, never fatal.
    if config.jira.enabled:
        register_jira_tools(registry, config.jira)
    if config.gitlab.enabled:
        register_gitlab_tools(registry, config.gitlab)
    if config.gerrit.enabled:
        register_gerrit_tools(registry, config.gerrit)
    if config.confluence.enabled:
        register_confluence_tools(registry, config.confluence)
    if config.slack_search.enabled:
        register_slack_search_tools(registry, config.slack_search)
    if config.web_search.enabled:
        register_web_search_tools(registry, config.web_search)

    # ── Coding agent tools (file/search/bash/git) ──────────────────
    # Default-OFF — the coding tools mutate the filesystem and run
    # shell commands, so they must be explicitly opted in via
    # ``CODING_AGENT_ENABLED=true``.  Reuses the shared helper so the
    # composition stays in one place.
    if config.coding.enabled:
        workspace_root = str(Path(config.coding.workspace_root).expanduser())
        Path(workspace_root).mkdir(parents=True, exist_ok=True)
        _register_coding_tools(
            registry,
            workspace_root=workspace_root,
            git_clone_allowed_hosts=config.coding.git_clone_allowed_hosts,
        )

    # ── Skill tool ─────────────────────────────────────────────────
    # ``register_skill_tool`` is a no-op when the loader discovered no
    # skills, so it's safe to call whenever a loader is supplied.
    if skill_loader is not None:
        register_skill_tool(registry, skill_loader)

    return registry


def _register_coding_tools(
    registry: ToolRegistry,
    *,
    workspace_root: str,
    git_clone_allowed_hosts: str,
) -> None:
    """Register the coding-agent suite onto *registry*.

    Shared by ``create_coding_tool_registry`` (standalone usage) and
    ``build_full_tool_registry`` (whole-process wiring) so the list of
    coding tools stays defined in exactly one place.

    Args:
        registry: The registry to populate.
        workspace_root: Absolute filesystem path that file/search/
            bash/git tools operate on.
        git_clone_allowed_hosts: Forwarded to ``register_git_tools``;
            empty disables ``git_clone`` (fail-closed).
    """
    register_file_tools(registry, workspace_root)
    register_search_tools(registry, workspace_root)
    register_bash_tool(registry, workspace_root)
    register_git_tools(registry, workspace_root, git_clone_allowed_hosts)
