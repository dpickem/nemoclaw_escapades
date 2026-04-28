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
from nemoclaw_escapades.tools.delegation import register_delegation_tool
from nemoclaw_escapades.tools.files import register_file_tools
from nemoclaw_escapades.tools.gerrit import register_gerrit_tools
from nemoclaw_escapades.tools.git import register_git_tools
from nemoclaw_escapades.tools.gitlab import register_gitlab_tools
from nemoclaw_escapades.tools.jira import register_jira_tools
from nemoclaw_escapades.tools.registry import ToolRegistry
from nemoclaw_escapades.tools.search import register_search_tools
from nemoclaw_escapades.tools.skill import register_skill_tool
from nemoclaw_escapades.tools.slack_search import register_slack_search_tools
from nemoclaw_escapades.tools.tool_search import register_tool_search_tool
from nemoclaw_escapades.tools.web_search import register_web_search_tools

if TYPE_CHECKING:
    from nemoclaw_escapades.agent.skill_loader import SkillLoader
    from nemoclaw_escapades.audit.db import AuditDB
    from nemoclaw_escapades.config import AppConfig
    from nemoclaw_escapades.orchestrator.delegation import DelegationManager


def create_coding_tool_registry(
    workspace_root: str,
    *,
    git_clone_allowed_hosts: str = "",
    skill_loader: SkillLoader | None = None,
) -> ToolRegistry:
    """Create a registry with just the coding sub-agent's tools.

    The sub-agent's git suite is **read-only plus clone**
    (``git_diff``, ``git_log``, ``git_checkout``, ``git_clone``) —
    ``git_commit`` is deliberately excluded.  Per design §7.1 the
    orchestrator owns finalisation: sub-agents report their changes,
    the orchestrator decides how they land (commit, push, open PR)
    via its finalization tools.  Giving the sub-agent a direct
    commit path would create two write sources against the same
    repository state and bypass the orchestrator's review gate.

    When a ``skill_loader`` is supplied (and has discovered at least
    one ``SKILL.md``), the dynamic ``skill(<id>)`` tool is also
    registered.  This is how the sub-agent's system prompt's
    "load ``scratchpad`` skill" instruction actually works — without
    the loader, the model would try to invoke a tool that doesn't
    exist in its registry, wasting rounds on "unknown tool" errors.

    Args:
        workspace_root: Absolute path to the workspace directory.
            All file/search/git/bash tools are rooted here.
        git_clone_allowed_hosts: Comma/space-separated hostnames that
            ``git_clone`` accepts.  Empty disables ``git_clone``.
        skill_loader: Optional pre-scanned ``SkillLoader``.  When
            supplied, ``register_skill_tool`` adds a ``skill`` tool
            whose parameter enum lists every discovered skill id.
            Passing ``None`` omits the tool entirely — used by tests
            and any caller that deliberately wants a pure coding
            surface.  ``register_skill_tool`` is itself a no-op when
            the loader has zero skills, so a never-populated
            ``skills/`` directory doesn't produce a broken tool.

    Returns:
        A fully populated ``ToolRegistry`` ready for injection into
        an ``AgentLoop``.
    """
    registry = ToolRegistry()
    _register_coding_tools(
        registry,
        workspace_root=workspace_root,
        git_clone_allowed_hosts=git_clone_allowed_hosts,
        include_git_commit=False,
    )
    if skill_loader is not None:
        register_skill_tool(registry, skill_loader)

    # ``tool_search`` rides along even though the sub-agent's current
    # surface is entirely core.  Registering it now means the moment
    # someone adds a non-core tool to the coding registry (e.g. a
    # tightly-scoped enterprise tool for a particular sub-agent role)
    # the discovery path exists without a second migration.
    register_tool_search_tool(registry)
    return registry


def build_full_tool_registry(
    config: AppConfig,
    skill_loader: SkillLoader | None = None,
    *,
    delegation_manager: DelegationManager | None = None,
    audit: AuditDB | None = None,
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
        delegation_manager: Optional :class:`DelegationManager`
            already constructed by the entrypoint.  When supplied,
            the orchestrator-only ``delegate_task`` tool is
            registered.  Sub-agents do **not** receive this tool —
            recursive delegation is an M3 review-agent concern, not
            an M2b capability (see §16.3 "No recursive delegation").
        audit: Optional :class:`AuditDB`.  Forwarded to
            ``register_delegation_tool`` so the per-workflow
            ``log_delegation_*`` calls land in the central DB.

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
    # Default-ON — the coding tools are the orchestrator's core
    # capability.  Writes are still gated by the write-approval flow,
    # and ``git_clone`` stays fail-closed until an allowlist is
    # provided via ``GIT_CLONE_ALLOWED_HOSTS``.  Opt out with
    # ``CODING_AGENT_ENABLED=false``.
    if config.coding.enabled:
        workspace_root = str(Path(config.coding.workspace_root).expanduser())
        Path(workspace_root).mkdir(parents=True, exist_ok=True)
        _register_coding_tools(
            registry,
            workspace_root=workspace_root,
            git_clone_allowed_hosts=config.coding.git_clone_allowed_hosts,
        )

    # ── Delegation tool (orchestrator only) ────────────────────────
    # Only registers when the entrypoint supplied a manager — that's
    # the wire that connects ``delegate_task`` to a live NMB bus.
    # Without this guard a ``delegate_task`` invocation would have no
    # way to actually reach a sub-agent, defeating the point.  The
    # sub-agent's ``create_coding_tool_registry`` factory deliberately
    # never reaches this branch; recursive delegation is M3 territory.
    if delegation_manager is not None:
        workspace_root = str(Path(config.coding.workspace_root).expanduser())
        register_delegation_tool(
            registry,
            manager=delegation_manager,
            parent_sandbox_id=config.nmb.sandbox_id or "orchestrator",
            workspace_root=workspace_root,
            audit=audit,
        )

    # ── Skill tool ─────────────────────────────────────────────────
    # ``register_skill_tool`` is a no-op when the loader discovered no
    # skills, so it's safe to call whenever a loader is supplied.
    if skill_loader is not None:
        register_skill_tool(registry, skill_loader)

    # ``tool_search`` itself stays core — it's how the model reaches
    # the non-core service tools in the first place.  Each service
    # module tags its own ``@tool`` definitions with ``is_core=False``
    # so the factory doesn't need to know which toolsets are non-core.
    register_tool_search_tool(registry)

    return registry


def _register_coding_tools(
    registry: ToolRegistry,
    *,
    workspace_root: str,
    git_clone_allowed_hosts: str,
    include_git_commit: bool = True,
) -> None:
    """Register the coding-agent suite onto *registry*.

    Shared by ``create_coding_tool_registry`` (standalone usage, sub-
    agents) and ``build_full_tool_registry`` (whole-process wiring,
    orchestrator) so the list of coding tools stays defined in
    exactly one place.

    Args:
        registry: The registry to populate.
        workspace_root: Absolute filesystem path that file/search/
            bash/git tools operate on.
        git_clone_allowed_hosts: Forwarded to ``register_git_tools``;
            empty disables ``git_clone`` (fail-closed).
        include_git_commit: Forwarded to ``register_git_tools``.
            Orchestrator-side callers default to ``True``; sub-agents
            pass ``False`` so ``git_commit`` stays an orchestrator-
            only capability (see ``create_coding_tool_registry``
            docstring for the design rationale).
    """
    register_file_tools(registry, workspace_root)
    register_search_tools(registry, workspace_root)
    register_bash_tool(registry, workspace_root)
    register_git_tools(
        registry,
        workspace_root,
        git_clone_allowed_hosts,
        include_commit=include_git_commit,
    )
