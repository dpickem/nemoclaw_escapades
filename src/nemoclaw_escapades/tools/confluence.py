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
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec

logger = get_logger("tools.confluence")

_confluence_config: ConfluenceConfig | None = None

_REQUEST_TIMEOUT_SECONDS: float = 30.0
_ERROR_BODY_MAX_CHARS: int = 500
_HTTP_ERROR_THRESHOLD: int = 400
_DEFAULT_SEARCH_LIMIT: int = 10


# ---------------------------------------------------------------------------
# Async Confluence client
# ---------------------------------------------------------------------------


class ConfluenceClient:
    """Async Confluence REST API client.

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
        return bool(self.base_url and self._username and self._api_token)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(self._username, self._api_token),
                headers={"Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
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
        """Search Confluence using CQL."""
        return await self._request(
            "GET",
            "/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "space,version"},
        )

    async def get_page(
        self, page_id: str, expand: str = "body.storage,version,space"
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}",
            params={"expand": expand},
        )

    async def get_page_children(
        self, page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/page",
            params={"limit": limit, "expand": "version"},
        )

    async def get_comments(
        self, page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/comment",
            params={"limit": limit, "expand": "body.storage"},
        )

    async def get_labels(self, page_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/rest/api/content/{page_id}/label")

    # -- WRITE operations --------------------------------------------------

    async def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
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
        payload = {
            "version": {"number": version_number},
            "title": title,
            "type": "page",
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return await self._request("PUT", f"/rest/api/content/{page_id}", json=payload)

    async def add_comment(self, page_id: str, body: str) -> dict[str, Any]:
        payload = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return await self._request("POST", "/rest/api/content", json=payload)

    async def add_label(self, page_id: str, label: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/rest/api/content/{page_id}/label",
            json=[{"prefix": "global", "name": label}],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _get_client() -> ConfluenceClient:
    if _confluence_config is None:
        raise RuntimeError(
            "Confluence tools not initialised — call register_confluence_tools first"
        )
    return ConfluenceClient(
        base_url=_confluence_config.url,
        username=_confluence_config.username,
        api_token=_confluence_config.api_token,
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def confluence_search(cql: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search Confluence pages using CQL."""
    return _format(await _get_client().search(cql, limit=limit))


async def confluence_get_page(page_id: str) -> str:
    """Get a Confluence page by ID including its body content."""
    return _format(await _get_client().get_page(page_id))


async def confluence_get_page_children(page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """List child pages of a Confluence page."""
    return _format(await _get_client().get_page_children(page_id, limit=limit))


async def confluence_get_comments(page_id: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Get comments on a Confluence page."""
    return _format(await _get_client().get_comments(page_id, limit=limit))


async def confluence_get_labels(page_id: str) -> str:
    """Get labels on a Confluence page."""
    return _format(await _get_client().get_labels(page_id))


async def confluence_create_page(
    space_key: str,
    title: str,
    body: str,
    parent_id: str = "",
) -> str:
    """Create a new Confluence page."""
    return _format(
        await _get_client().create_page(space_key, title, body, parent_id=parent_id or None)
    )


async def confluence_update_page(page_id: str, title: str, body: str, version_number: int) -> str:
    """Update an existing Confluence page."""
    return _format(await _get_client().update_page(page_id, title, body, version_number))


async def confluence_add_comment(page_id: str, body: str) -> str:
    """Add a comment to a Confluence page."""
    return _format(await _get_client().add_comment(page_id, body))


async def confluence_add_label(page_id: str, label: str) -> str:
    """Add a label to a Confluence page."""
    return _format(await _get_client().add_label(page_id, label))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _confluence_available() -> bool:
    return _confluence_config is not None and _get_client().configured


def register_confluence_tools(registry: ToolRegistry, config: ConfluenceConfig) -> None:
    """Register all Confluence tools with the orchestrator's tool registry."""
    global _confluence_config  # noqa: PLW0603
    _confluence_config = config

    _ts = "confluence"
    _ck = _confluence_available

    registry.register(
        ToolSpec(
            name="confluence_search",
            display_name="Searching Confluence",
            description=(
                "Search Confluence pages using CQL. Example: "
                '"confluence_search(cql=\'type=page AND space=MYSPACE AND text~\\"deployment\\"\')"'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cql": {"type": "string", "description": "CQL query string"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["cql"],
            },
            handler=confluence_search,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_get_page",
            display_name="Getting Confluence page",
            description="Get a Confluence page by ID, including its body content.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Confluence page ID"},
                },
                "required": ["page_id"],
            },
            handler=confluence_get_page,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_get_page_children",
            display_name="Listing child pages",
            description="List child pages of a Confluence page.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Parent page ID"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["page_id"],
            },
            handler=confluence_get_page_children,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_get_comments",
            display_name="Getting page comments",
            description="Get comments on a Confluence page.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["page_id"],
            },
            handler=confluence_get_comments,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_get_labels",
            display_name="Getting page labels",
            description="Get labels on a Confluence page.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID"},
                },
                "required": ["page_id"],
            },
            handler=confluence_get_labels,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_create_page",
            display_name="Creating Confluence page",
            description="Create a new Confluence page in a space. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "space_key": {"type": "string", "description": "Space key (e.g. MYSPACE)"},
                    "title": {"type": "string", "description": "Page title"},
                    "body": {
                        "type": "string",
                        "description": "Page body (Confluence storage format)",
                    },
                    "parent_id": {"type": "string", "description": "Optional parent page ID"},
                },
                "required": ["space_key", "title", "body"],
            },
            handler=confluence_create_page,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_update_page",
            display_name="Updating Confluence page",
            description="Update an existing Confluence page. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID to update"},
                    "title": {"type": "string", "description": "New page title"},
                    "body": {"type": "string", "description": "New page body (storage format)"},
                    "version_number": {
                        "type": "integer",
                        "description": "New version number (current version + 1)",
                    },
                },
                "required": ["page_id", "title", "body", "version_number"],
            },
            handler=confluence_update_page,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_add_comment",
            display_name="Adding Confluence comment",
            description="Add a comment to a Confluence page. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID"},
                    "body": {"type": "string", "description": "Comment body (storage format)"},
                },
                "required": ["page_id", "body"],
            },
            handler=confluence_add_comment,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="confluence_add_label",
            display_name="Adding Confluence label",
            description="Add a label to a Confluence page. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID"},
                    "label": {"type": "string", "description": "Label name"},
                },
                "required": ["page_id", "label"],
            },
            handler=confluence_add_label,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )
