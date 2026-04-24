"""Jira REST API tools for the orchestrator.

Provides an async Jira client and registers individual Jira operations
as tools in the orchestrator's ToolRegistry.  Each tool maps to one
Jira REST API call.

**Credential model:** The client reads a pre-computed ``Authorization``
header value from the ``JIRA_AUTH`` environment variable.  In the
OpenShell sandbox this is an ``openshell:resolve:env:JIRA_AUTH``
placeholder that the L7 proxy resolves before forwarding the request.
Locally the variable holds the real ``Basic <base64>`` value (set via
the Makefile's ``.env`` export).

Lifted from ``nv_tools.clients.jira.JiraClient`` and converted to async
httpx + tool-registry integration.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from nemoclaw_escapades.config import JiraConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.jira")

# ── Constants ─────────────────────────────────────────────────────────

# Comma-separated field list included in every GET/search response.
_DEFAULT_FIELDS: str = (
    "status,labels,assignee,updated,created,issuetype,description,"
    "priority,summary,reporter,duedate,issuelinks,components"
)

# Per-request timeout for the underlying ``httpx.AsyncClient``.
_REQUEST_TIMEOUT_S: float = 30.0

# Error response bodies are clipped to this length in returned error dicts.
_ERROR_BODY_MAX_CHARS: int = 500

# Default ``maxResults`` sent to the Jira search API.
_DEFAULT_SEARCH_LIMIT: int = 10

# Status codes at or above this value are treated as errors.
_HTTP_ERROR_THRESHOLD: int = 400

# Status codes that indicate success but may return an empty body (e.g. PUT, DELETE).
_SUCCESS_NO_BODY_CODES: tuple[int, ...] = (201, 204)

# Jira custom-field ID for the Epic Link field (server-specific).
_EPIC_LINK_FIELD: str = "customfield_10014"

# Logical toolset name used by the registry for grouping
_TOOLSET: str = "jira"


# ── Async Jira client ────────────────────────────────────────────────


class JiraClient:
    """Async Jira REST API client with OpenShell proxy-compatible auth.

    The ``auth_header`` value is passed in by the caller (ultimately
    from ``JiraConfig`` via ``load_config``).  Inside an OpenShell
    sandbox the value is a proxy placeholder (e.g.
    ``openshell:resolve:env:JIRA_AUTH``) that the L7 proxy swaps for
    real credentials at HTTP request time.

    Attributes:
        base_url: Jira server base URL (no trailing slash).
    """

    def __init__(
        self,
        base_url: str,
        auth_header: str = "",
    ) -> None:
        """Initialise the Jira client.

        Args:
            base_url: Jira server base URL (from ``JiraConfig.url``).
            auth_header: Pre-computed ``Authorization`` header value
                (or a proxy placeholder in sandbox mode).
        """
        self.base_url = base_url.rstrip("/")
        self._auth_header = auth_header
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when both base URL and auth header are set."""
        return bool(self.base_url and self._auth_header)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": self._auth_header,
                },
                timeout=_REQUEST_TIMEOUT_S,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client and release its connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Send an HTTP request to Jira and return the parsed response.

        Args:
            method: HTTP method (``GET``, ``POST``, ``PUT``, etc.).
            endpoint: API path relative to ``base_url``.
            **kwargs: Forwarded to ``httpx.AsyncClient.request``.

        Returns:
            Parsed JSON response dict, or an error dict when the client
            is not configured or the API returns >= 400.
        """
        if not self.configured:
            return {"error": "Jira not configured. Set JIRA_URL and JIRA_AUTH."}

        client = await self._get_client()
        response = await client.request(method, endpoint, **kwargs)

        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return {
                "error": f"Jira API returned {response.status_code}",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            }

        if response.status_code in _SUCCESS_NO_BODY_CODES and not response.text.strip():
            return {"success": True}
        return response.json()  # type: ignore[no-any-return]

    # -- READ operations ---------------------------------------------------

    async def get_issue(self, issue_key: str, fields: str = _DEFAULT_FIELDS) -> dict[str, Any]:
        """Fetch a single Jira issue by key.

        Args:
            issue_key: Issue identifier (e.g. ``PROJ-123``).
            fields: Comma-separated field names to include in the response.

        Returns:
            Parsed JSON dict of the issue, or an error dict on failure.
        """
        return await self._request(
            "GET", f"/rest/api/2/issue/{issue_key}", params={"fields": fields}
        )

    async def search(
        self,
        jql: str,
        limit: int = _DEFAULT_SEARCH_LIMIT,
        fields: str = _DEFAULT_FIELDS,
    ) -> dict[str, Any]:
        """Search Jira issues using JQL.

        Args:
            jql: JQL query string.
            limit: Maximum number of results to return.
            fields: Comma-separated field names, or ``*all``.

        Returns:
            Search result dict containing matched issues.
        """
        payload: dict[str, Any] = {
            "jql": jql,
            "fields": fields.split(",") if fields != "*all" else ["*all"],
            "maxResults": limit,
        }
        return await self._request("POST", "/rest/api/2/search", json=payload)

    async def get_transitions(self, issue_key: str) -> dict[str, Any]:
        """Fetch available status transitions for an issue.

        Args:
            issue_key: Issue identifier (e.g. ``PROJ-123``).

        Returns:
            Dict containing a ``transitions`` list.
        """
        return await self._request("GET", f"/rest/api/2/issue/{issue_key}/transitions")

    async def me(self) -> dict[str, Any]:
        """Return the profile of the currently authenticated user.

        Returns:
            User profile dict with ``displayName``, ``emailAddress``, etc.
        """
        return await self._request("GET", "/rest/api/2/myself")

    # -- WRITE operations --------------------------------------------------

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        labels: list[str] | None = None,
        components: list[str] | None = None,
        epic_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Jira issue.

        Args:
            project_key: Target project (e.g. ``MYPROJ``).
            summary: One-line issue title.
            issue_type: Jira issue type name (default ``Task``).
            description: Full issue description (Jira wiki markup).
            assignee: Username to assign the issue to.
            priority: Priority name (e.g. ``P1``, ``Medium``).
            due_date: Due date in ``YYYY-MM-DD`` format.
            labels: List of label strings to attach.
            components: List of component names.
            epic_key: Issue key of the parent epic.

        Returns:
            Created issue dict (includes ``key`` and ``self`` URL), or
            an error dict on failure.
        """
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = description
        if assignee:
            fields["assignee"] = {"name": assignee}
        if priority:
            fields["priority"] = {"name": priority}
        if due_date:
            fields["duedate"] = due_date
        if labels:
            fields["labels"] = labels
        if components:
            fields["components"] = [{"name": c} for c in components]
        if epic_key:
            fields[_EPIC_LINK_FIELD] = epic_key

        return await self._request("POST", "/rest/api/2/issue", json={"fields": fields})

    async def update_issue(
        self,
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        labels: list[str] | None = None,
        components: list[str] | None = None,
        epic_key: str | None = None,
    ) -> dict[str, Any]:
        """Update fields on an existing issue and return its refreshed state.

        Only non-``None`` parameters are included in the update payload.

        Args:
            issue_key: Issue identifier (e.g. ``PROJ-123``).
            summary: New summary/title.
            description: New description body (Jira wiki markup).
            assignee: New assignee username.
            priority: New priority name (e.g. ``P1``, ``Medium``).
            due_date: New due date in ``YYYY-MM-DD`` format.
            labels: Label strings (replaces existing labels).
            components: Component names (replaces existing components).
            epic_key: Issue key of the parent epic.

        Returns:
            The full issue dict after the update, or an error dict if no
            fields were provided or the API call failed.
        """
        fields: dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = description
        if assignee is not None:
            fields["assignee"] = {"name": assignee}
        if priority is not None:
            fields["priority"] = {"name": priority}
        if due_date is not None:
            fields["duedate"] = due_date
        if labels is not None:
            fields["labels"] = labels
        if components is not None:
            fields["components"] = [{"name": c} for c in components]
        if epic_key is not None:
            fields[_EPIC_LINK_FIELD] = epic_key
        if not fields:
            return {"error": "No fields to update"}

        await self._request("PUT", f"/rest/api/2/issue/{issue_key}", json={"fields": fields})
        return await self.get_issue(issue_key)

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        """Add a comment to a Jira issue.

        Args:
            issue_key: Issue identifier (e.g. ``PROJ-123``).
            body: Comment text (Jira wiki markup).

        Returns:
            The created comment dict.
        """
        return await self._request(
            "POST", f"/rest/api/2/issue/{issue_key}/comment", json={"body": body}
        )

    async def transition_issue(
        self, issue_key: str, transition_id: str, comment: str | None = None
    ) -> dict[str, Any]:
        """Transition a Jira issue to a new status.

        Args:
            issue_key: Issue identifier (e.g. ``PROJ-123``).
            transition_id: Numeric transition ID (from ``get_transitions``).
            comment: Optional comment to attach with the transition.

        Returns:
            The full issue dict after the transition.
        """
        payload: dict[str, Any] = {"transition": {"id": transition_id}}
        if comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}
        await self._request("POST", f"/rest/api/2/issue/{issue_key}/transitions", json=payload)
        return await self.get_issue(issue_key)


# ── Helpers ───────────────────────────────────────────────────────────


def _format(data: dict[str, Any]) -> str:
    """Serialize *data* to pretty-printed JSON.

    Truncation is handled by the registry's ``execute()`` method, so
    this function only serializes.
    """
    return json.dumps(data, indent=2, default=str)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_jira_get_issue(client: JiraClient) -> ToolSpec:
    """Create the ``jira_get_issue`` tool spec."""

    @tool(
        "jira_get_issue",
        "Get details of a Jira issue by key (e.g. PROJ-123).",
        {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key, e.g. PROJ-123"},
            },
            "required": ["issue_key"],
        },
        display_name="Getting Jira issue",
        toolset=_TOOLSET,
        is_core=False,
    )
    async def jira_get_issue(issue_key: str) -> str:
        """Fetch a Jira issue by key."""
        return _format(await client.get_issue(issue_key))

    return jira_get_issue


def _make_jira_search(client: JiraClient) -> ToolSpec:
    """Create the ``jira_search`` tool spec."""

    @tool(
        "jira_search",
        (
            "Search Jira issues using JQL. Example: "
            'jira_search(jql="project = MYPROJ AND status = Open", limit=10)'
        ),
        {
            "type": "object",
            "properties": {
                "jql": {"type": "string", "description": "JQL query string"},
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {_DEFAULT_SEARCH_LIMIT})",
                    "default": _DEFAULT_SEARCH_LIMIT,
                },
            },
            "required": ["jql"],
        },
        display_name="Searching Jira",
        toolset=_TOOLSET,
        is_core=False,
    )
    async def jira_search(jql: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """Search Jira issues using JQL."""
        return _format(await client.search(jql, limit=limit))

    return jira_search


def _make_jira_me(client: JiraClient) -> ToolSpec:
    """Create the ``jira_me`` tool spec."""

    @tool(
        "jira_me",
        "Get the authenticated user's Jira profile.",
        {"type": "object", "properties": {}},
        display_name="Checking Jira profile",
        toolset=_TOOLSET,
        is_core=False,
    )
    async def jira_me() -> str:
        """Get the authenticated user's Jira profile."""
        return _format(await client.me())

    return jira_me


def _make_jira_get_transitions(client: JiraClient) -> ToolSpec:
    """Create the ``jira_get_transitions`` tool spec."""

    @tool(
        "jira_get_transitions",
        "Get available status transitions for a Jira issue.",
        {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key"},
            },
            "required": ["issue_key"],
        },
        display_name="Getting issue transitions",
        toolset=_TOOLSET,
        is_core=False,
    )
    async def jira_get_transitions(issue_key: str) -> str:
        """Get available status transitions for a Jira issue."""
        return _format(await client.get_transitions(issue_key))

    return jira_get_transitions


def _make_jira_create_issue(client: JiraClient) -> ToolSpec:
    """Create the ``jira_create_issue`` tool spec."""

    @tool(
        "jira_create_issue",
        "Create a new Jira issue. Requires approval.",
        {
            "type": "object",
            "properties": {
                "project_key": {
                    "type": "string",
                    "description": "Project key, e.g. MYPROJ",
                },
                "summary": {
                    "type": "string",
                    "description": "Issue summary/title",
                },
                "issue_type": {
                    "type": "string",
                    "description": "Issue type (default: Task)",
                    "default": "Task",
                },
                "description": {
                    "type": "string",
                    "description": "Issue description",
                },
                "assignee": {
                    "type": "string",
                    "description": "Assignee username",
                },
                "priority": {
                    "type": "string",
                    "description": "Priority name (e.g. P1, Medium)",
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date in YYYY-MM-DD format",
                },
                "labels": {
                    "type": "string",
                    "description": "Comma-separated labels",
                },
                "components": {
                    "type": "string",
                    "description": "Comma-separated component names",
                },
                "epic_key": {
                    "type": "string",
                    "description": "Epic issue key to link to (e.g. PROJ-100)",
                },
            },
            "required": ["project_key", "summary"],
        },
        display_name="Creating Jira issue",
        toolset=_TOOLSET,
        is_core=False,
        is_read_only=False,
    )
    async def jira_create_issue(
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        assignee: str = "",
        priority: str = "",
        due_date: str = "",
        labels: str = "",
        components: str = "",
        epic_key: str = "",
    ) -> str:
        """Create a new Jira issue."""
        return _format(
            await client.create_issue(
                project_key,
                summary,
                issue_type,
                description=description or None,
                assignee=assignee or None,
                priority=priority or None,
                due_date=due_date or None,
                labels=labels.split(",") if labels else None,
                components=components.split(",") if components else None,
                epic_key=epic_key or None,
            )
        )

    return jira_create_issue


def _make_jira_update_issue(client: JiraClient) -> ToolSpec:
    """Create the ``jira_update_issue`` tool spec."""

    @tool(
        "jira_update_issue",
        (
            "Update fields on an existing Jira issue. "
            "Pass only the fields you want to change. Requires approval."
        ),
        {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Issue key",
                },
                "summary": {
                    "type": "string",
                    "description": "New summary",
                },
                "description": {
                    "type": "string",
                    "description": "New description",
                },
                "assignee": {
                    "type": "string",
                    "description": "New assignee username",
                },
                "priority": {
                    "type": "string",
                    "description": "New priority (e.g. P1, Medium)",
                },
                "due_date": {
                    "type": "string",
                    "description": "New due date in YYYY-MM-DD format",
                },
                "labels": {
                    "type": "string",
                    "description": "Comma-separated labels (replaces all)",
                },
                "components": {
                    "type": "string",
                    "description": "Comma-separated component names (replaces all)",
                },
                "epic_key": {
                    "type": "string",
                    "description": "Epic issue key to link to",
                },
            },
            "required": ["issue_key"],
        },
        display_name="Updating Jira issue",
        toolset=_TOOLSET,
        is_core=False,
        is_read_only=False,
    )
    async def jira_update_issue(
        issue_key: str,
        summary: str = "",
        description: str = "",
        assignee: str = "",
        priority: str = "",
        due_date: str = "",
        labels: str = "",
        components: str = "",
        epic_key: str = "",
    ) -> str:
        """Update fields on an existing Jira issue."""
        return _format(
            await client.update_issue(
                issue_key,
                summary=summary or None,
                description=description or None,
                assignee=assignee or None,
                priority=priority or None,
                due_date=due_date or None,
                labels=labels.split(",") if labels else None,
                components=[c.strip() for c in components.split(",")] if components else None,
                epic_key=epic_key or None,
            )
        )

    return jira_update_issue


def _make_jira_add_comment(client: JiraClient) -> ToolSpec:
    """Create the ``jira_add_comment`` tool spec."""

    @tool(
        "jira_add_comment",
        "Add a comment to a Jira issue. Requires approval.",
        {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key"},
                "body": {"type": "string", "description": "Comment text (Jira wiki markup)"},
            },
            "required": ["issue_key", "body"],
        },
        display_name="Adding Jira comment",
        toolset=_TOOLSET,
        is_core=False,
        is_read_only=False,
    )
    async def jira_add_comment(issue_key: str, body: str) -> str:
        """Add a comment to a Jira issue."""
        return _format(await client.add_comment(issue_key, body))

    return jira_add_comment


def _make_jira_transition_issue(client: JiraClient) -> ToolSpec:
    """Create the ``jira_transition_issue`` tool spec."""

    @tool(
        "jira_transition_issue",
        (
            "Transition a Jira issue to a new status. Use jira_get_transitions first "
            "to find the transition_id. Requires approval."
        ),
        {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key"},
                "transition_id": {
                    "type": "string",
                    "description": "Transition ID (from jira_get_transitions)",
                },
                "comment": {"type": "string", "description": "Optional transition comment"},
            },
            "required": ["issue_key", "transition_id"],
        },
        display_name="Transitioning Jira issue",
        toolset=_TOOLSET,
        is_core=False,
        is_read_only=False,
    )
    async def jira_transition_issue(issue_key: str, transition_id: str, comment: str = "") -> str:
        """Transition a Jira issue to a new status."""
        return _format(
            await client.transition_issue(issue_key, transition_id, comment=comment or None)
        )

    return jira_transition_issue


# ── Registration ──────────────────────────────────────────────────────


def register_jira_tools(registry: ToolRegistry, config: JiraConfig) -> None:
    """Register all Jira tools with the orchestrator's tool registry.

    Creates a single ``JiraClient`` from *config* and binds every tool
    handler to it via closures.  Tools whose ``check_fn`` returns
    ``False`` are silently skipped by the registry.

    Args:
        registry: The tool registry to populate.
        config: Jira connection settings.
    """
    client = JiraClient(base_url=config.url, auth_header=config.auth_header)

    def _check() -> bool:
        return client.configured

    for factory in (
        _make_jira_get_issue,
        _make_jira_search,
        _make_jira_me,
        _make_jira_get_transitions,
        _make_jira_create_issue,
        _make_jira_update_issue,
        _make_jira_add_comment,
        _make_jira_transition_issue,
    ):
        spec = factory(client)
        spec.check_fn = _check
        registry.register(spec)
