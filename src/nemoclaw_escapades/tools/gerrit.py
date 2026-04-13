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
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec

logger = get_logger("tools.gerrit")

_gerrit_config: GerritConfig | None = None

_REQUEST_TIMEOUT_SECONDS: float = 30.0
_ERROR_BODY_MAX_CHARS: int = 500
_HTTP_ERROR_THRESHOLD: int = 400
_DEFAULT_SEARCH_LIMIT: int = 10

_GERRIT_XSSI_PREFIX: str = ")]}'"


# ---------------------------------------------------------------------------
# Async Gerrit client
# ---------------------------------------------------------------------------


class GerritClient:
    """Async Gerrit REST API client.

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
        return bool(self.base_url and self._username and self._http_password)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(self._username, self._http_password),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _strip_xssi(self, text: str) -> Any:
        """Strip Gerrit's XSSI prefix and parse JSON."""
        cleaned = text
        if cleaned.startswith(_GERRIT_XSSI_PREFIX):
            cleaned = cleaned[len(_GERRIT_XSSI_PREFIX):]
        return json.loads(cleaned)

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
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
        """Fetch a single change by numeric ID or triplet."""
        return await self._request(
            "GET",
            f"/changes/{change_id}",
            params={"o": ["CURRENT_REVISION", "LABELS", "DETAILED_ACCOUNTS"]},
        )

    async def list_changes(
        self, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any] | list[Any]:
        return await self._request(
            "GET", "/changes/", params={"q": query, "n": limit}
        )

    async def get_change_detail(self, change_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/changes/{change_id}/detail",
            params={"o": ["CURRENT_REVISION", "ALL_FILES", "LABELS", "DETAILED_ACCOUNTS"]},
        )

    async def get_comments(self, change_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/changes/{change_id}/comments")

    async def list_files(self, change_id: str, revision: str = "current") -> dict[str, Any]:
        return await self._request(
            "GET", f"/changes/{change_id}/revisions/{revision}/files"
        )

    async def get_diff(
        self, change_id: str, file_path: str, revision: str = "current"
    ) -> dict[str, Any]:
        encoded_path = quote(file_path, safe="")
        return await self._request(
            "GET",
            f"/changes/{change_id}/revisions/{revision}/files/{encoded_path}/diff",
        )

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", "/accounts/self")

    # -- WRITE operations --------------------------------------------------

    async def set_review(
        self,
        change_id: str,
        message: str = "",
        labels: dict[str, int] | None = None,
        revision: str = "current",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if message:
            payload["message"] = message
        if labels:
            payload["labels"] = labels
        return await self._request(
            "POST", f"/changes/{change_id}/revisions/{revision}/review", json=payload
        )

    async def submit(self, change_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/changes/{change_id}/submit")

    async def abandon(self, change_id: str, message: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if message:
            payload["message"] = message
        return await self._request("POST", f"/changes/{change_id}/abandon", json=payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _get_client() -> GerritClient:
    if _gerrit_config is None:
        raise RuntimeError("Gerrit tools not initialised — call register_gerrit_tools first")
    return GerritClient(
        base_url=_gerrit_config.url,
        username=_gerrit_config.username,
        http_password=_gerrit_config.http_password,
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def gerrit_get_change(change_id: str) -> str:
    """Get details of a Gerrit change."""
    return _format(await _get_client().get_change(change_id))


async def gerrit_list_changes(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search Gerrit changes using a query string."""
    return _format(await _get_client().list_changes(query, limit=limit))


async def gerrit_get_change_detail(change_id: str) -> str:
    """Get detailed information about a Gerrit change."""
    return _format(await _get_client().get_change_detail(change_id))


async def gerrit_get_comments(change_id: str) -> str:
    """Get all comments on a Gerrit change."""
    return _format(await _get_client().get_comments(change_id))


async def gerrit_list_files(change_id: str, revision: str = "current") -> str:
    """List files modified in a Gerrit change."""
    return _format(await _get_client().list_files(change_id, revision=revision))


async def gerrit_get_diff(
    change_id: str, file_path: str, revision: str = "current"
) -> str:
    """Get the diff for a specific file in a Gerrit change."""
    return _format(await _get_client().get_diff(change_id, file_path, revision=revision))


async def gerrit_me() -> str:
    """Get the authenticated Gerrit user's account info."""
    return _format(await _get_client().get_account())


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
        await _get_client().set_review(
            change_id, message=message, labels=parsed_labels, revision=revision
        )
    )


async def gerrit_submit(change_id: str) -> str:
    """Submit a Gerrit change."""
    return _format(await _get_client().submit(change_id))


async def gerrit_abandon(change_id: str, message: str = "") -> str:
    """Abandon a Gerrit change."""
    return _format(await _get_client().abandon(change_id, message=message))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _gerrit_available() -> bool:
    return _gerrit_config is not None and _get_client().configured


def register_gerrit_tools(registry: ToolRegistry, config: GerritConfig) -> None:
    """Register all Gerrit tools with the orchestrator's tool registry."""
    global _gerrit_config  # noqa: PLW0603
    _gerrit_config = config

    _ts = "gerrit"
    _ck = _gerrit_available

    registry.register(ToolSpec(
        name="gerrit_get_change",
        display_name="Getting Gerrit change",
        description="Get details of a Gerrit change by numeric ID or change-id.",
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID (numeric or triplet)"},
            },
            "required": ["change_id"],
        },
        handler=gerrit_get_change,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_list_changes",
        display_name="Searching Gerrit changes",
        description=(
            "Search Gerrit changes using a query string. Example: "
            '"owner:self status:open"'
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gerrit search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        handler=gerrit_list_changes,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_get_change_detail",
        display_name="Getting change detail",
        description="Get detailed info about a Gerrit change including all revisions and labels.",
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        handler=gerrit_get_change_detail,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_get_comments",
        display_name="Getting Gerrit comments",
        description="Get all review comments on a Gerrit change.",
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        handler=gerrit_get_comments,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_list_files",
        display_name="Listing changed files",
        description="List files modified in a Gerrit change.",
        input_schema={
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
        handler=gerrit_list_files,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_get_diff",
        display_name="Getting file diff",
        description="Get the diff for a specific file in a Gerrit change.",
        input_schema={
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
        handler=gerrit_get_diff,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_me",
        display_name="Checking Gerrit profile",
        description="Get the authenticated Gerrit user's account info.",
        input_schema={"type": "object", "properties": {}},
        handler=gerrit_me,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_set_review",
        display_name="Posting Gerrit review",
        description=(
            "Post a review on a Gerrit change with a message and/or labels. "
            'Labels should be a JSON string like \'{"Code-Review": 1}\'. Requires approval.'
        ),
        input_schema={
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
        handler=gerrit_set_review,
        is_read_only=False,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_submit",
        display_name="Submitting Gerrit change",
        description="Submit (merge) a Gerrit change. Requires approval.",
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
            },
            "required": ["change_id"],
        },
        handler=gerrit_submit,
        is_read_only=False,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gerrit_abandon",
        display_name="Abandoning Gerrit change",
        description="Abandon a Gerrit change. Requires approval.",
        input_schema={
            "type": "object",
            "properties": {
                "change_id": {"type": "string", "description": "Change ID"},
                "message": {"type": "string", "description": "Optional reason for abandoning"},
            },
            "required": ["change_id"],
        },
        handler=gerrit_abandon,
        is_read_only=False,
        toolset=_ts, check_fn=_ck,
    ))
