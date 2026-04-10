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
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec

logger = get_logger("tools.jira")

# Set by ``register_jira_tools``; ``None`` until first call.
_jira_config: JiraConfig | None = None

# Comma-separated field list included in every GET/search response.
_DEFAULT_FIELDS: str = (
    "status,labels,assignee,updated,created,issuetype,description,"
    "priority,summary,reporter,duedate,issuelinks,components"
)

# Per-request timeout for the underlying ``httpx.AsyncClient``.
_REQUEST_TIMEOUT_SECONDS: float = 30.0

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


# ---------------------------------------------------------------------------
# Async Jira client
# ---------------------------------------------------------------------------


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
                timeout=_REQUEST_TIMEOUT_SECONDS,
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


def _format(data: dict[str, Any]) -> str:
    """Serialize *data* to pretty-printed JSON.

    Truncation is handled by the registry's ``execute()`` method, so
    this function only serializes.
    """
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool handlers (async, return str)
# ---------------------------------------------------------------------------


def _get_client() -> JiraClient:
    """Create a JiraClient per call using the module-level ``_jira_config``.

    A fresh client is needed because httpx.AsyncClient is bound to the
    event loop that created it. In the orchestrator (single loop) this
    is redundant but harmless; in test scripts that call asyncio.run()
    multiple times, reusing a client from a closed loop would crash.

    Raises:
        RuntimeError: If ``register_jira_tools`` has not been called yet.
    """
    if _jira_config is None:
        raise RuntimeError("Jira tools not initialised — call register_jira_tools first")
    return JiraClient(base_url=_jira_config.url, auth_header=_jira_config.auth_header)


async def jira_get_issue(issue_key: str) -> str:
    """Fetch a Jira issue by key and return it as formatted JSON.

    Args:
        issue_key: Issue identifier (e.g. ``PROJ-123``).

    Returns:
        Pretty-printed JSON string of the issue fields.
    """
    return _format(await _get_client().get_issue(issue_key))


async def jira_search(jql: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search Jira issues using JQL and return formatted results.

    Args:
        jql: JQL query string.
        limit: Maximum number of results to return.

    Returns:
        Pretty-printed JSON string of matching issues.
    """
    return _format(await _get_client().search(jql, limit=limit))


async def jira_me() -> str:
    """Return the authenticated user's Jira profile as formatted JSON.

    Returns:
        Pretty-printed JSON string of the user profile.
    """
    return _format(await _get_client().me())


async def jira_get_transitions(issue_key: str) -> str:
    """List available status transitions for a Jira issue.

    Args:
        issue_key: Issue identifier (e.g. ``PROJ-123``).

    Returns:
        Pretty-printed JSON string of available transitions.
    """
    return _format(await _get_client().get_transitions(issue_key))


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
    """Create a new Jira issue and return the result as formatted JSON.

    String parameters that accept comma-separated values (``labels``,
    ``components``) are split before being forwarded to the API.

    Args:
        project_key: Target project (e.g. ``MYPROJ``).
        summary: One-line issue title.
        issue_type: Jira issue type name (default ``Task``).
        description: Full issue description body.
        assignee: Username to assign the issue to.
        priority: Priority name (e.g. ``P1``, ``Medium``).
        due_date: Due date in ``YYYY-MM-DD`` format.
        labels: Comma-separated label strings.
        components: Comma-separated component names.
        epic_key: Issue key of the parent epic.

    Returns:
        Pretty-printed JSON string of the created issue.
    """
    return _format(
        await _get_client().create_issue(
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
    """Update fields on an existing Jira issue.

    Only non-empty parameters are included in the update payload.
    String parameters that accept comma-separated values (``labels``,
    ``components``) are split before being forwarded to the API.

    Args:
        issue_key: Issue identifier (e.g. ``PROJ-123``).
        summary: New summary/title.
        description: New description body.
        assignee: New assignee username.
        priority: New priority name (e.g. ``P1``, ``Medium``).
        due_date: New due date in ``YYYY-MM-DD`` format.
        labels: Comma-separated labels (replaces existing labels).
        components: Comma-separated component names (replaces existing).
        epic_key: Epic issue key to link to.

    Returns:
        Pretty-printed JSON string of the updated issue, or an error
        JSON string if no fields were provided.
    """
    return _format(
        await _get_client().update_issue(
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


async def jira_add_comment(issue_key: str, body: str) -> str:
    """Add a comment to a Jira issue.

    Args:
        issue_key: Issue identifier (e.g. ``PROJ-123``).
        body: Comment text (Jira wiki markup).

    Returns:
        Pretty-printed JSON string of the created comment.
    """
    return _format(await _get_client().add_comment(issue_key, body))


async def jira_transition_issue(issue_key: str, transition_id: str, comment: str = "") -> str:
    """Transition a Jira issue to a new status.

    Args:
        issue_key: Issue identifier (e.g. ``PROJ-123``).
        transition_id: Numeric transition ID (from ``jira_get_transitions``).
        comment: Optional comment to attach with the transition.

    Returns:
        Pretty-printed JSON string of the issue after the transition.
    """
    return _format(
        await _get_client().transition_issue(issue_key, transition_id, comment=comment or None)
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _jira_available() -> bool:
    """Return ``True`` when Jira credentials are present."""
    return _jira_config is not None and _get_client().configured


def register_jira_tools(registry: ToolRegistry, config: JiraConfig) -> None:
    """Register all Jira tools with the orchestrator's tool registry.

    Stores *config* at module level so tool handlers can create properly
    configured ``JiraClient`` instances via ``_get_client()``.

    Each tool is registered with ``toolset="jira"`` and a ``check_fn``
    that verifies the Jira auth header is present.  If the check fails,
    the registry skips the tool with a warning.

    Args:
        registry: The orchestrator's tool registry to add tools to.
        config: Jira configuration (URL, auth env var, etc.).
    """
    global _jira_config  # noqa: PLW0603
    _jira_config = config

    registry.register(
        ToolSpec(
            name="jira_get_issue",
            display_name="Getting Jira issue",
            description="Get details of a Jira issue by key (e.g. PROJ-123).",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key, e.g. PROJ-123"},
                },
                "required": ["issue_key"],
            },
            handler=jira_get_issue,
            is_read_only=True,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_search",
            display_name="Searching Jira",
            description=(
                "Search Jira issues using JQL. Example: "
                'jira_search(jql="project = MYPROJ AND status = Open", limit=10)'
            ),
            input_schema={
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
            handler=jira_search,
            is_read_only=True,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_me",
            display_name="Checking Jira profile",
            description="Get the authenticated user's Jira profile.",
            input_schema={"type": "object", "properties": {}},
            handler=jira_me,
            is_read_only=True,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_get_transitions",
            display_name="Getting issue transitions",
            description="Get available status transitions for a Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key"},
                },
                "required": ["issue_key"],
            },
            handler=jira_get_transitions,
            is_read_only=True,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_create_issue",
            display_name="Creating Jira issue",
            description="Create a new Jira issue. Requires approval.",
            input_schema={
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
            handler=jira_create_issue,
            is_read_only=False,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_update_issue",
            display_name="Updating Jira issue",
            description=(
                "Update fields on an existing Jira issue. "
                "Pass only the fields you want to change. Requires approval."
            ),
            input_schema={
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
            handler=jira_update_issue,
            is_read_only=False,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_add_comment",
            display_name="Adding Jira comment",
            description="Add a comment to a Jira issue. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key"},
                    "body": {"type": "string", "description": "Comment text (Jira wiki markup)"},
                },
                "required": ["issue_key", "body"],
            },
            handler=jira_add_comment,
            is_read_only=False,
            toolset="jira",
            check_fn=_jira_available,
        )
    )

    registry.register(
        ToolSpec(
            name="jira_transition_issue",
            display_name="Transitioning Jira issue",
            description=(
                "Transition a Jira issue to a new status. Use jira_get_transitions first "
                "to find the transition_id. Requires approval."
            ),
            input_schema={
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
            handler=jira_transition_issue,
            is_read_only=False,
            toolset="jira",
            check_fn=_jira_available,
        )
    )
