"""Gerrit REST API tools for the orchestrator.

Provides an async Gerrit client and registers individual operations as
tools.  Lifted from ``nv_tools.clients.gerrit.GerritClient`` and
converted to async httpx + tool-registry integration.

**Auth:** Uses HTTP Basic auth (``GERRIT_USERNAME`` / ``GERRIT_HTTP_PASSWORD``).
**Response:** Gerrit prefixes JSON responses with ``)]}'`` — the client
strips this before parsing.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from nemoclaw_escapades.config import GerritConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.gerrit")

# ── Constants ─────────────────────────────────────────────────────────

# Seconds before an HTTP request to the Gerrit API is aborted
_REQUEST_TIMEOUT_S: float = 30.0
# Max characters of response body included in error messages
_ERROR_BODY_MAX_CHARS: int = 500
# HTTP status codes at or above this threshold are treated as errors
_HTTP_ERROR_THRESHOLD: int = 400
# Default page size for search and list operations
_DEFAULT_SEARCH_LIMIT: int = 10
# Gerrit XSSI protection prefix stripped from every response
_GERRIT_XSSI_PREFIX: str = ")]}'"
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "gerrit"


# ── Async Gerrit client ──────────────────────────────────────────────


class GerritClient:
    """Async Gerrit REST API client.

    A single instance should be created at registration time and shared
    across all tool handlers via closures.  The underlying
    ``httpx.AsyncClient`` is created lazily on first request.

    Attributes:
        base_url: Gerrit server base URL (no trailing slash).
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        http_password: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._http_password = http_password
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when all required credentials are present."""
        return bool(self.base_url and self._username and self._http_password)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(self._username, self._http_password),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=_REQUEST_TIMEOUT_S,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _strip_xssi(self, text: str) -> Any:
        """Strip Gerrit's XSSI prefix and parse JSON."""
        cleaned = text
        if cleaned.startswith(_GERRIT_XSSI_PREFIX):
            cleaned = cleaned[len(_GERRIT_XSSI_PREFIX) :]
        return json.loads(cleaned)

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Send an HTTP request and return the parsed JSON response.

        Args:
            method: HTTP method (``GET``, ``POST``, etc.).
            endpoint: API path relative to the base URL.
            **kwargs: Forwarded to ``httpx.AsyncClient.request``.

        Returns:
            Parsed JSON response, or an error dict on failure.
        """
        if not self.configured:
            return {
                "error": "Gerrit not configured. Set GERRIT_URL, GERRIT_USERNAME, "
                "and GERRIT_HTTP_PASSWORD."
            }
        client = await self._get_client()
        response = await client.request(method, endpoint, **kwargs)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return {
                "error": f"Gerrit API returned {response.status_code}",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            }
        return self._strip_xssi(response.text)  # type: ignore[no-any-return]

    # -- READ operations ---------------------------------------------------

    async def get_change(self, change_id: str) -> dict[str, Any]:
        """Fetch a single change by numeric ID or triplet.

        Args:
            change_id: Change ID (numeric or triplet).
        """
        return await self._request(
            "GET",
            f"/changes/{change_id}",
            params={"o": ["CURRENT_REVISION", "LABELS", "DETAILED_ACCOUNTS"]},
        )

    async def list_changes(
        self, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any] | list[Any]:
        """Search changes using a Gerrit query string.

        Args:
            query: Gerrit search query.
            limit: Max results to return.
        """
        return await self._request("GET", "/changes/", params={"q": query, "n": limit})

    async def get_change_detail(self, change_id: str) -> dict[str, Any]:
        """Fetch detailed info about a change including all revisions and labels.

        Args:
            change_id: Change ID (numeric or triplet).
        """
        return await self._request(
            "GET",
            f"/changes/{change_id}/detail",
            params={"o": ["CURRENT_REVISION", "ALL_FILES", "LABELS", "DETAILED_ACCOUNTS"]},
        )

    async def get_comments(self, change_id: str) -> dict[str, Any]:
        """Retrieve all review comments on a change.

        Args:
            change_id: Change ID (numeric or triplet).
        """
        return await self._request("GET", f"/changes/{change_id}/comments")

    async def list_files(self, change_id: str, revision: str = "current") -> dict[str, Any]:
        """List files modified in a change at a given revision.

        Args:
            change_id: Change ID (numeric or triplet).
            revision: Patch-set revision (default ``current``).
        """
        return await self._request("GET", f"/changes/{change_id}/revisions/{revision}/files")

    async def get_diff(
        self, change_id: str, file_path: str, revision: str = "current"
    ) -> dict[str, Any]:
        """Get the diff for a specific file in a change.

        Args:
            change_id: Change ID (numeric or triplet).
            file_path: File path within the change.
            revision: Patch-set revision (default ``current``).
        """
        encoded_path = quote(file_path, safe="")
        return await self._request(
            "GET",
            f"/changes/{change_id}/revisions/{revision}/files/{encoded_path}/diff",
        )

    async def get_account(self) -> dict[str, Any]:
        """Return the authenticated user's Gerrit account info."""
        return await self._request("GET", "/accounts/self")

    # -- WRITE operations --------------------------------------------------

    async def set_review(
        self,
        change_id: str,
        message: str = "",
        labels: dict[str, int] | None = None,
        revision: str = "current",
    ) -> dict[str, Any]:
        """Post a review (message and/or labels) on a change.

        Args:
            change_id: Change ID (numeric or triplet).
            message: Optional review message.
            labels: Optional label votes, e.g. ``{"Code-Review": 1}``.
            revision: Patch-set revision (default ``current``).
        """
        payload: dict[str, Any] = {}
        if message:
            payload["message"] = message
        if labels:
            payload["labels"] = labels
        return await self._request(
            "POST", f"/changes/{change_id}/revisions/{revision}/review", json=payload
        )

    async def submit(self, change_id: str) -> dict[str, Any]:
        """Submit (merge) a change.

        Args:
            change_id: Change ID (numeric or triplet).
        """
        return await self._request("POST", f"/changes/{change_id}/submit")

    async def abandon(self, change_id: str, message: str = "") -> dict[str, Any]:
        """Abandon a change with an optional reason.

        Args:
            change_id: Change ID (numeric or triplet).
            message: Optional reason for abandoning.
        """
        payload: dict[str, Any] = {}
        if message:
            payload["message"] = message
        return await self._request("POST", f"/changes/{change_id}/abandon", json=payload)


# ── Helpers ───────────────────────────────────────────────────────────


def _format(data: Any) -> str:
    """Serialize *data* as indented JSON for model consumption."""
    return json.dumps(data, indent=2, default=str)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_gerrit_get_change(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_get_change`` tool spec."""

    @tool(
        "gerrit_get_change",
        "Get details of a Gerrit change by numeric ID or change-id.",
        {
            "type": "object",
            "properties": {
                "change_id": {
                    "type": "string",
                    "description": "Change ID (numeric or triplet)",
                },
            },
            "required": ["change_id"],
        },
        display_name="Getting Gerrit change",
        toolset=_TOOLSET,
    )
    async def gerrit_get_change(change_id: str) -> str:
        """Get details of a Gerrit change."""
        return _format(await client.get_change(change_id))

    return gerrit_get_change


def _make_gerrit_list_changes(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_list_changes`` tool spec."""

    @tool(
        "gerrit_list_changes",
        'Search Gerrit changes using a query string. Example: "owner:self status:open"',
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gerrit search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        display_name="Searching Gerrit changes",
        toolset=_TOOLSET,
    )
    async def gerrit_list_changes(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """Search Gerrit changes using a query string."""
        return _format(await client.list_changes(query, limit=limit))

    return gerrit_list_changes


def _make_gerrit_get_change_detail(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_get_change_detail`` tool spec."""

    @tool(
        "gerrit_get_change_detail",
        "Get detailed info about a Gerrit change including all revisions and labels.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        display_name="Getting change detail",
        toolset=_TOOLSET,
    )
    async def gerrit_get_change_detail(change_id: str) -> str:
        """Get detailed information about a Gerrit change."""
        return _format(await client.get_change_detail(change_id))

    return gerrit_get_change_detail


def _make_gerrit_get_comments(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_get_comments`` tool spec."""

    @tool(
        "gerrit_get_comments",
        "Get all review comments on a Gerrit change.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        display_name="Getting Gerrit comments",
        toolset=_TOOLSET,
    )
    async def gerrit_get_comments(change_id: str) -> str:
        """Get all comments on a Gerrit change."""
        return _format(await client.get_comments(change_id))

    return gerrit_get_comments


def _make_gerrit_list_files(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_list_files`` tool spec."""

    @tool(
        "gerrit_list_files",
        "List files modified in a Gerrit change.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
                "revision": {
                    "type": "string",
                    "description": "Revision (default: current)",
                    "default": "current",
                },
            },
            "required": ["change_id"],
        },
        display_name="Listing changed files",
        toolset=_TOOLSET,
    )
    async def gerrit_list_files(change_id: str, revision: str = "current") -> str:
        """List files modified in a Gerrit change."""
        return _format(await client.list_files(change_id, revision=revision))

    return gerrit_list_files


def _make_gerrit_get_diff(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_get_diff`` tool spec."""

    @tool(
        "gerrit_get_diff",
        "Get the diff for a specific file in a Gerrit change.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
                "file_path": {"type": "string", "description": "File path in the change"},
                "revision": {
                    "type": "string",
                    "description": "Revision (default: current)",
                    "default": "current",
                },
            },
            "required": ["change_id", "file_path"],
        },
        display_name="Getting file diff",
        toolset=_TOOLSET,
    )
    async def gerrit_get_diff(
        change_id: str, file_path: str, revision: str = "current"
    ) -> str:
        """Get the diff for a specific file in a Gerrit change."""
        return _format(await client.get_diff(change_id, file_path, revision=revision))

    return gerrit_get_diff


def _make_gerrit_me(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_me`` tool spec."""

    @tool(
        "gerrit_me",
        "Get the authenticated Gerrit user's account info.",
        {"type": "object", "properties": {}},
        display_name="Checking Gerrit profile",
        toolset=_TOOLSET,
    )
    async def gerrit_me() -> str:
        """Get the authenticated Gerrit user's account info."""
        return _format(await client.get_account())

    return gerrit_me


def _make_gerrit_set_review(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_set_review`` tool spec."""

    @tool(
        "gerrit_set_review",
        (
            "Post a review on a Gerrit change with a message and/or labels. "
            'Labels should be a JSON string like \'{"Code-Review": 1}\'. Requires approval.'
        ),
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
                "message": {"type": "string", "description": "Review message"},
                "labels": {
                    "type": "string",
                    "description": 'JSON object of labels, e.g. {"Code-Review": 1}',
                },
                "revision": {
                    "type": "string",
                    "description": "Revision (default: current)",
                    "default": "current",
                },
            },
            "required": ["change_id"],
        },
        display_name="Posting Gerrit review",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gerrit_set_review(
        change_id: str,
        message: str = "",
        labels: str = "",
        revision: str = "current",
    ) -> str:
        """Post a review (message and/or labels) on a Gerrit change."""
        parsed_labels: dict[str, int] | None = None
        if labels:
            parsed_labels = json.loads(labels)
        return _format(
            await client.set_review(
                change_id, message=message, labels=parsed_labels, revision=revision
            )
        )

    return gerrit_set_review


def _make_gerrit_submit(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_submit`` tool spec."""

    @tool(
        "gerrit_submit",
        "Submit (merge) a Gerrit change. Requires approval.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        display_name="Submitting Gerrit change",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gerrit_submit(change_id: str) -> str:
        """Submit a Gerrit change."""
        return _format(await client.submit(change_id))

    return gerrit_submit


def _make_gerrit_abandon(client: GerritClient) -> ToolSpec:
    """Create the ``gerrit_abandon`` tool spec."""

    @tool(
        "gerrit_abandon",
        "Abandon a Gerrit change. Requires approval.",
        {
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
                "message": {"type": "string", "description": "Optional reason for abandoning"},
            },
            "required": ["change_id"],
        },
        display_name="Abandoning Gerrit change",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gerrit_abandon(change_id: str, message: str = "") -> str:
        """Abandon a Gerrit change."""
        return _format(await client.abandon(change_id, message=message))

    return gerrit_abandon


# ── Registration ──────────────────────────────────────────────────────


def register_gerrit_tools(registry: ToolRegistry, config: GerritConfig) -> None:
    """Register all Gerrit tools with the orchestrator's tool registry.

    Creates a single ``GerritClient`` from *config* and binds every
    tool handler to it via closures.  Tools whose ``check_fn`` returns
    ``False`` are silently skipped by the registry.

    Args:
        registry: The tool registry to populate.
        config: Gerrit connection settings.
    """
    client = GerritClient(
        base_url=config.url,
        username=config.username,
        http_password=config.http_password,
    )

    def _check() -> bool:
        return client.configured

    for factory in (
        _make_gerrit_get_change,
        _make_gerrit_list_changes,
        _make_gerrit_get_change_detail,
        _make_gerrit_get_comments,
        _make_gerrit_list_files,
        _make_gerrit_get_diff,
        _make_gerrit_me,
        _make_gerrit_set_review,
        _make_gerrit_submit,
        _make_gerrit_abandon,
    ):
        spec = factory(client)
        spec.check_fn = _check
        registry.register(spec)
