"""Slack Web API tools for the orchestrator (user-token based).

Provides search, channel history, and thread replies using a *user*
OAuth token (``xoxp-...``).  This is separate from the bot connector
which uses a *bot* token for messaging.

Lifted from ``nv_tools.clients.slack.SlackClient`` and converted to
async httpx + tool-registry integration.

**Auth:** ``Authorization: Bearer <SLACK_USER_TOKEN>``
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from nemoclaw_escapades.config import SlackSearchConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec

logger = get_logger("tools.slack_search")

_slack_config: SlackSearchConfig | None = None

_REQUEST_TIMEOUT_SECONDS: float = 30.0
_ERROR_BODY_MAX_CHARS: int = 500
_DEFAULT_SEARCH_LIMIT: int = 10
_DEFAULT_HISTORY_LIMIT: int = 20

_SLACK_API_BASE: str = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Async Slack client
# ---------------------------------------------------------------------------


class SlackSearchClient:
    """Async Slack Web API client for search and history operations.

    Attributes:
        configured: Whether the user token is present.
    """

    def __init__(self, user_token: str = "") -> None:
        self._user_token = user_token
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._user_token)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_SLACK_API_BASE,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._user_token}",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if not self.configured:
            return {"error": "Slack search not configured. Set SLACK_USER_TOKEN."}
        client = await self._get_client()
        response = await client.request(method, endpoint, **kwargs)
        data = response.json()
        if not data.get("ok"):
            return {
                "error": f"Slack API error: {data.get('error', 'unknown')}",
                "details": {k: v for k, v in data.items() if k != "ok"},
            }
        return data  # type: ignore[no-any-return]

    # -- Operations --------------------------------------------------------

    async def search_messages(
        self, query: str, count: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        return await self._request(
            "GET", "/search.messages", params={"query": query, "count": count}
        )

    async def list_channels(self, limit: int = _DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/conversations.list",
            params={"limit": limit, "types": "public_channel,private_channel"},
        )

    async def get_channel_history(
        self, channel_id: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/conversations.history",
            params={"channel": channel_id, "limit": limit},
        )

    async def get_thread_replies(
        self, channel_id: str, thread_ts: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/conversations.replies",
            params={"channel": channel_id, "ts": thread_ts, "limit": limit},
        )

    async def get_user_info(self, user_id: str) -> dict[str, Any]:
        return await self._request("GET", "/users.info", params={"user": user_id})

    async def send_message(self, channel_id: str, text: str, thread_ts: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._request("POST", "/chat.postMessage", json=payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _get_client() -> SlackSearchClient:
    if _slack_config is None:
        raise RuntimeError(
            "Slack search tools not initialised — call register_slack_search_tools first"
        )
    return SlackSearchClient(user_token=_slack_config.user_token)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def slack_search_messages(query: str, count: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search Slack messages across all channels."""
    return _format(await _get_client().search_messages(query, count=count))


async def slack_list_channels(limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """List Slack channels the user has access to."""
    return _format(await _get_client().list_channels(limit=limit))


async def slack_get_channel_history(channel_id: str, limit: int = _DEFAULT_HISTORY_LIMIT) -> str:
    """Get recent messages from a Slack channel."""
    return _format(await _get_client().get_channel_history(channel_id, limit=limit))


async def slack_get_thread_replies(
    channel_id: str, thread_ts: str, limit: int = _DEFAULT_HISTORY_LIMIT
) -> str:
    """Get replies in a Slack thread."""
    return _format(await _get_client().get_thread_replies(channel_id, thread_ts, limit=limit))


async def slack_get_user_info(user_id: str) -> str:
    """Get information about a Slack user."""
    return _format(await _get_client().get_user_info(user_id))


async def slack_send_message(channel_id: str, text: str, thread_ts: str = "") -> str:
    """Send a message to a Slack channel or thread."""
    return _format(await _get_client().send_message(channel_id, text, thread_ts=thread_ts))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _slack_search_available() -> bool:
    return _slack_config is not None and _get_client().configured


def register_slack_search_tools(registry: ToolRegistry, config: SlackSearchConfig) -> None:
    """Register all Slack search/history tools with the tool registry."""
    global _slack_config  # noqa: PLW0603
    _slack_config = config

    _ts = "slack_search"
    _ck = _slack_search_available

    registry.register(
        ToolSpec(
            name="slack_search_messages",
            display_name="Searching Slack",
            description=(
                "Search Slack messages across all channels. "
                'Example: slack_search_messages(query="deployment issue in:#ops")'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (Slack syntax)"},
                    "count": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["query"],
            },
            handler=slack_search_messages,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="slack_list_channels",
            display_name="Listing Slack channels",
            description="List Slack channels accessible to the user.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
            },
            handler=slack_list_channels,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="slack_get_channel_history",
            display_name="Getting channel history",
            description="Get recent messages from a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Slack channel ID"},
                    "limit": {"type": "integer", "description": "Max messages", "default": 20},
                },
                "required": ["channel_id"],
            },
            handler=slack_get_channel_history,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="slack_get_thread_replies",
            display_name="Getting thread replies",
            description="Get replies in a Slack thread.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Slack channel ID"},
                    "thread_ts": {"type": "string", "description": "Thread timestamp"},
                    "limit": {"type": "integer", "description": "Max replies", "default": 20},
                },
                "required": ["channel_id", "thread_ts"],
            },
            handler=slack_get_thread_replies,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="slack_get_user_info",
            display_name="Getting Slack user info",
            description="Get profile information about a Slack user by user ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Slack user ID (e.g. U12345)"},
                },
                "required": ["user_id"],
            },
            handler=slack_get_user_info,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="slack_send_message",
            display_name="Sending Slack message",
            description=(
                "Send a message to a Slack channel or thread using the user token. "
                "Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Slack channel ID"},
                    "text": {"type": "string", "description": "Message text"},
                    "thread_ts": {"type": "string", "description": "Thread timestamp (optional)"},
                },
                "required": ["channel_id", "text"],
            },
            handler=slack_send_message,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )
