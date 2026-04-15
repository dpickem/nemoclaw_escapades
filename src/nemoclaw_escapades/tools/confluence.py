"""Confluence REST API tools for the orchestrator.

Provides an async Confluence client and registers individual operations
as tools.  Lifted from ``nv_tools.clients.confluence.ConfluenceClient``
and converted to async httpx + tool-registry integration.

**Auth:** Uses HTTP Basic auth (``CONFLUENCE_USERNAME`` / ``CONFLUENCE_API_TOKEN``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from nemoclaw_escapades.config import ConfluenceConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.confluence")

# ── Constants ─────────────────────────────────────────────────────────

# Seconds before an HTTP request to the Confluence API is aborted
_REQUEST_TIMEOUT_S: float = 30.0
# Max characters of response body included in error messages
_ERROR_BODY_MAX_CHARS: int = 500
# HTTP status codes at or above this threshold are treated as errors
_HTTP_ERROR_THRESHOLD: int = 400
# Default page size for search and list operations
_DEFAULT_SEARCH_LIMIT: int = 10
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "confluence"


# ── Async Confluence client ───────────────────────────────────────────


class ConfluenceClient:
    """Async Confluence REST API client.

    A single instance should be created at registration time and shared
    across all tool handlers via closures.  The underlying
    ``httpx.AsyncClient`` is created lazily on first request.

    Attributes:
        base_url: Confluence instance base URL (no trailing slash).
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        api_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._api_token = api_token
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when all required credentials are present."""
        return bool(self.base_url and self._username and self._api_token)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(self._username, self._api_token),
                headers={"Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_S,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Send an HTTP request and return the parsed JSON response.

        Args:
            method: HTTP method (``GET``, ``POST``, ``PUT``, etc.).
            endpoint: API path relative to the base URL.
            **kwargs: Forwarded to ``httpx.AsyncClient.request``.

        Returns:
            Parsed JSON response, or an error dict on failure.
        """
        if not self.configured:
            return {
                "error": "Confluence not configured. Set CONFLUENCE_URL, "
                "CONFLUENCE_USERNAME, and CONFLUENCE_API_TOKEN."
            }
        client = await self._get_client()
        response = await client.request(method, endpoint, **kwargs)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return {
                "error": f"Confluence API returned {response.status_code}",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            }
        if not response.text.strip():
            return {"success": True}
        return response.json()  # type: ignore[no-any-return]

    # -- READ operations ---------------------------------------------------

    async def search(self, cql: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        """Search Confluence pages using CQL.

        Args:
            cql: Confluence Query Language expression.
            limit: Max results to return.
        """
        return await self._request(
            "GET",
            "/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "space,version"},
        )

    async def get_page(
        self, page_id: str, expand: str = "body.storage,version,space"
    ) -> dict[str, Any]:
        """Retrieve a page by ID.

        Args:
            page_id: Confluence page ID.
            expand: Comma-separated expansions.
        """
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}",
            params={"expand": expand},
        )

    async def get_page_children(
        self, page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """List child pages of *page_id*.

        Args:
            page_id: Parent page ID.
            limit: Max results to return.
        """
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/page",
            params={"limit": limit, "expand": "version"},
        )

    async def get_comments(
        self, page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """List comments on *page_id*.

        Args:
            page_id: Page ID.
            limit: Max results to return.
        """
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/comment",
            params={"limit": limit, "expand": "body.storage"},
        )

    async def get_labels(self, page_id: str) -> dict[str, Any]:
        """List labels attached to *page_id*.

        Args:
            page_id: Page ID.
        """
        return await self._request("GET", f"/rest/api/content/{page_id}/label")

    # -- WRITE operations --------------------------------------------------

    async def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new page in *space_key*.

        Args:
            space_key: Confluence space key.
            title: Page title.
            body: Page body in Confluence storage format.
            parent_id: Optional parent page ID.
        """
        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": parent_id}]
        return await self._request("POST", "/rest/api/content", json=payload)

    async def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
    ) -> dict[str, Any]:
        """Update an existing page.

        Args:
            page_id: Page ID to update.
            title: New page title.
            body: New page body in Confluence storage format.
            version_number: New version number (current + 1).
        """
        payload = {
            "version": {"number": version_number},
            "title": title,
            "type": "page",
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return await self._request("PUT", f"/rest/api/content/{page_id}", json=payload)

    async def add_comment(self, page_id: str, body: str) -> dict[str, Any]:
        """Add a comment to *page_id*.

        Args:
            page_id: Page ID.
            body: Comment body in Confluence storage format.
        """
        payload = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return await self._request("POST", "/rest/api/content", json=payload)

    async def add_label(self, page_id: str, label: str) -> dict[str, Any]:
        """Add a label to *page_id*.

        Args:
            page_id: Page ID.
            label: Label name to add.
        """
        return await self._request(
            "POST",
            f"/rest/api/content/{page_id}/label",
            json=[{"prefix": "global", "name": label}],
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _format(data: Any) -> str:
    """Serialize *data* as indented JSON for model consumption."""
    return json.dumps(data, indent=2, default=str)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_confluence_search(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_search`` tool spec."""

    @tool(
        "confluence_search",
        (
            "Search Confluence pages using CQL. Example: "
            "confluence_search(cql='type=page AND space=MYSPACE AND text~\"deployment\"')"
        ),
        {
            "type": "object",
            "properties": {
                "cql": {"type": "string", "description": "CQL query string."},
                "limit": {
                    "type": "integer",
                    "description": "Max results.",
                    "default": _DEFAULT_SEARCH_LIMIT,
                },
            },
            "required": ["cql"],
        },
        display_name="Searching Confluence",
        toolset=_TOOLSET,
    )
    async def confluence_search(cql: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """Search Confluence pages using CQL."""
        return _format(await client.search(cql, limit=limit))

    return confluence_search


def _make_confluence_get_page(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_get_page`` tool spec."""

    @tool(
        "confluence_get_page",
        "Get a Confluence page by ID, including its body content.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Confluence page ID."},
            },
            "required": ["page_id"],
        },
        display_name="Getting Confluence page",
        toolset=_TOOLSET,
    )
    async def confluence_get_page(page_id: str) -> str:
        """Retrieve a Confluence page by ID."""
        return _format(await client.get_page(page_id))

    return confluence_get_page


def _make_confluence_get_page_children(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_get_page_children`` tool spec."""

    @tool(
        "confluence_get_page_children",
        "List child pages of a Confluence page.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Parent page ID."},
                "limit": {
                    "type": "integer",
                    "description": "Max results.",
                    "default": _DEFAULT_SEARCH_LIMIT,
                },
            },
            "required": ["page_id"],
        },
        display_name="Listing child pages",
        toolset=_TOOLSET,
    )
    async def confluence_get_page_children(
        page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> str:
        """List child pages of a Confluence page."""
        return _format(await client.get_page_children(page_id, limit=limit))

    return confluence_get_page_children


def _make_confluence_get_comments(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_get_comments`` tool spec."""

    @tool(
        "confluence_get_comments",
        "Get comments on a Confluence page.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page ID."},
                "limit": {
                    "type": "integer",
                    "description": "Max results.",
                    "default": _DEFAULT_SEARCH_LIMIT,
                },
            },
            "required": ["page_id"],
        },
        display_name="Getting page comments",
        toolset=_TOOLSET,
    )
    async def confluence_get_comments(
        page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> str:
        """Get comments on a Confluence page."""
        return _format(await client.get_comments(page_id, limit=limit))

    return confluence_get_comments


def _make_confluence_get_labels(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_get_labels`` tool spec."""

    @tool(
        "confluence_get_labels",
        "Get labels on a Confluence page.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page ID."},
            },
            "required": ["page_id"],
        },
        display_name="Getting page labels",
        toolset=_TOOLSET,
    )
    async def confluence_get_labels(page_id: str) -> str:
        """Get labels on a Confluence page."""
        return _format(await client.get_labels(page_id))

    return confluence_get_labels


def _make_confluence_create_page(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_create_page`` tool spec."""

    @tool(
        "confluence_create_page",
        "Create a new Confluence page in a space. Requires approval.",
        {
            "type": "object",
            "properties": {
                "space_key": {"type": "string", "description": "Space key (e.g. MYSPACE)."},
                "title": {"type": "string", "description": "Page title."},
                "body": {
                    "type": "string",
                    "description": "Page body (Confluence storage format).",
                },
                "parent_id": {"type": "string", "description": "Optional parent page ID."},
            },
            "required": ["space_key", "title", "body"],
        },
        display_name="Creating Confluence page",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def confluence_create_page(
        space_key: str, title: str, body: str, parent_id: str = ""
    ) -> str:
        """Create a new Confluence page."""
        return _format(
            await client.create_page(space_key, title, body, parent_id=parent_id or None)
        )

    return confluence_create_page


def _make_confluence_update_page(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_update_page`` tool spec."""

    @tool(
        "confluence_update_page",
        "Update an existing Confluence page. Requires approval.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page ID to update."},
                "title": {"type": "string", "description": "New page title."},
                "body": {"type": "string", "description": "New page body (storage format)."},
                "version_number": {
                    "type": "integer",
                    "description": "New version number (current version + 1).",
                },
            },
            "required": ["page_id", "title", "body", "version_number"],
        },
        display_name="Updating Confluence page",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def confluence_update_page(
        page_id: str, title: str, body: str, version_number: int
    ) -> str:
        """Update an existing Confluence page."""
        return _format(await client.update_page(page_id, title, body, version_number))

    return confluence_update_page


def _make_confluence_add_comment(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_add_comment`` tool spec."""

    @tool(
        "confluence_add_comment",
        "Add a comment to a Confluence page. Requires approval.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page ID."},
                "body": {"type": "string", "description": "Comment body (storage format)."},
            },
            "required": ["page_id", "body"],
        },
        display_name="Adding Confluence comment",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def confluence_add_comment(page_id: str, body: str) -> str:
        """Add a comment to a Confluence page."""
        return _format(await client.add_comment(page_id, body))

    return confluence_add_comment


def _make_confluence_add_label(client: ConfluenceClient) -> ToolSpec:
    """Create the ``confluence_add_label`` tool spec."""

    @tool(
        "confluence_add_label",
        "Add a label to a Confluence page. Requires approval.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Page ID."},
                "label": {"type": "string", "description": "Label name."},
            },
            "required": ["page_id", "label"],
        },
        display_name="Adding Confluence label",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def confluence_add_label(page_id: str, label: str) -> str:
        """Add a label to a Confluence page."""
        return _format(await client.add_label(page_id, label))

    return confluence_add_label


# ── Registration ──────────────────────────────────────────────────────


def register_confluence_tools(registry: ToolRegistry, config: ConfluenceConfig) -> None:
    """Register all Confluence tools with the orchestrator's tool registry.

    Creates a single ``ConfluenceClient`` from *config* and binds every
    tool handler to it via closures.  Tools whose ``check_fn`` returns
    ``False`` are silently skipped by the registry.

    Args:
        registry: The tool registry to populate.
        config: Confluence connection settings.
    """
    client = ConfluenceClient(
        base_url=config.url,
        username=config.username,
        api_token=config.api_token,
    )

    def _check() -> bool:
        return client.configured

    for factory in (
        _make_confluence_search,
        _make_confluence_get_page,
        _make_confluence_get_page_children,
        _make_confluence_get_comments,
        _make_confluence_get_labels,
        _make_confluence_create_page,
        _make_confluence_update_page,
        _make_confluence_add_comment,
        _make_confluence_add_label,
    ):
        spec = factory(client)
        spec.check_fn = _check
        registry.register(spec)
