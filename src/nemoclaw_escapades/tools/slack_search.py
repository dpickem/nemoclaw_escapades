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
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.slack_search")

# ── Constants ─────────────────────────────────────────────────────────

# Seconds before an HTTP request to the Slack API is aborted
_REQUEST_TIMEOUT_S: float = 30.0
# Max characters of response body included in error messages
_ERROR_BODY_MAX_CHARS: int = 500
# Default result count for search operations
_DEFAULT_SEARCH_LIMIT: int = 10
# Default message count for history/thread operations
_DEFAULT_HISTORY_LIMIT: int = 20
# Slack Web API base URL
_SLACK_API_BASE: str = "https://slack.com/api"
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "slack_search"


# ── Async Slack client ────────────────────────────────────────────────


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
        """Return ``True`` when a user token has been provided."""
        return bool(self._user_token)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a lazily-initialised ``httpx.AsyncClient`` for the Slack API."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_SLACK_API_BASE,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._user_token}",
                },
                timeout=_REQUEST_TIMEOUT_S,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client and release its resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Send a request to the Slack Web API and return the JSON response.

        Args:
            method: HTTP method (e.g. ``"GET"``, ``"POST"``).
            endpoint: API path relative to ``_SLACK_API_BASE``.
            **kwargs: Extra arguments forwarded to ``httpx.AsyncClient.request``.

        Returns:
            Parsed JSON response dict on success, or an error dict.
        """
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
        """Search messages across all accessible channels.

        Args:
            query: Slack search query (supports Slack search syntax).
            count: Maximum number of results to return.

        Returns:
            Slack ``search.messages`` response payload.
        """
        return await self._request(
            "GET", "/search.messages", params={"query": query, "count": count}
        )

    async def list_channels(self, limit: int = _DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        """List public and private channels the user has access to.

        Args:
            limit: Maximum number of channels to return.

        Returns:
            Slack ``conversations.list`` response payload.
        """
        return await self._request(
            "GET",
            "/conversations.list",
            params={"limit": limit, "types": "public_channel,private_channel"},
        )

    async def get_channel_history(
        self, channel_id: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> dict[str, Any]:
        """Fetch recent messages from a single channel.

        Args:
            channel_id: Slack channel ID.
            limit: Maximum number of messages to return.

        Returns:
            Slack ``conversations.history`` response payload.
        """
        return await self._request(
            "GET",
            "/conversations.history",
            params={"channel": channel_id, "limit": limit},
        )

    async def get_thread_replies(
        self, channel_id: str, thread_ts: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> dict[str, Any]:
        """Fetch replies within a specific thread.

        Args:
            channel_id: Slack channel ID containing the thread.
            thread_ts: Timestamp of the parent message.
            limit: Maximum number of replies to return.

        Returns:
            Slack ``conversations.replies`` response payload.
        """
        return await self._request(
            "GET",
            "/conversations.replies",
            params={"channel": channel_id, "ts": thread_ts, "limit": limit},
        )

    async def get_user_info(self, user_id: str) -> dict[str, Any]:
        """Look up profile information for a single user.

        Args:
            user_id: Slack user ID (e.g. ``U12345``).

        Returns:
            Slack ``users.info`` response payload.
        """
        return await self._request("GET", "/users.info", params={"user": user_id})

    async def send_message(self, channel_id: str, text: str, thread_ts: str = "") -> dict[str, Any]:
        """Post a message to a channel or thread.

        Args:
            channel_id: Target Slack channel ID.
            text: Message body text.
            thread_ts: When provided, sends as a reply in the given thread.

        Returns:
            Slack ``chat.postMessage`` response payload.
        """
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._request("POST", "/chat.postMessage", json=payload)


# ── Helpers ───────────────────────────────────────────────────────────


def _format(data: Any) -> str:
    """Serialise *data* to a pretty-printed JSON string."""
    return json.dumps(data, indent=2, default=str)


# ── Tool specs ────────────────────────────────────────────────────────


def _make_slack_search_messages(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_search_messages`` tool spec."""

    @tool(
        "slack_search_messages",
        (
            "Search Slack messages across all channels. "
            'Example: slack_search_messages(query="deployment issue in:#ops")'
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (Slack syntax)"},
                "count": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        display_name="Searching Slack",
        toolset=_TOOLSET,
    )
    async def slack_search_messages(query: str, count: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """Search Slack messages across all channels."""
        return _format(await client.search_messages(query, count=count))

    return slack_search_messages


def _make_slack_list_channels(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_list_channels`` tool spec."""

    @tool(
        "slack_list_channels",
        "List Slack channels accessible to the user.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
        },
        display_name="Listing Slack channels",
        toolset=_TOOLSET,
    )
    async def slack_list_channels(limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """List Slack channels the user has access to."""
        return _format(await client.list_channels(limit=limit))

    return slack_list_channels


def _make_slack_get_channel_history(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_get_channel_history`` tool spec."""

    @tool(
        "slack_get_channel_history",
        "Get recent messages from a Slack channel.",
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "limit": {"type": "integer", "description": "Max messages", "default": 20},
            },
            "required": ["channel_id"],
        },
        display_name="Getting channel history",
        toolset=_TOOLSET,
    )
    async def slack_get_channel_history(
        channel_id: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> str:
        """Get recent messages from a Slack channel."""
        return _format(await client.get_channel_history(channel_id, limit=limit))

    return slack_get_channel_history


def _make_slack_get_thread_replies(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_get_thread_replies`` tool spec."""

    @tool(
        "slack_get_thread_replies",
        "Get replies in a Slack thread.",
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "thread_ts": {"type": "string", "description": "Thread timestamp"},
                "limit": {"type": "integer", "description": "Max replies", "default": 20},
            },
            "required": ["channel_id", "thread_ts"],
        },
        display_name="Getting thread replies",
        toolset=_TOOLSET,
    )
    async def slack_get_thread_replies(
        channel_id: str, thread_ts: str, limit: int = _DEFAULT_HISTORY_LIMIT
    ) -> str:
        """Get replies in a Slack thread."""
        return _format(
            await client.get_thread_replies(channel_id, thread_ts, limit=limit)
        )

    return slack_get_thread_replies


def _make_slack_get_user_info(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_get_user_info`` tool spec."""

    @tool(
        "slack_get_user_info",
        "Get profile information about a Slack user by user ID.",
        {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Slack user ID (e.g. U12345)"},
            },
            "required": ["user_id"],
        },
        display_name="Getting Slack user info",
        toolset=_TOOLSET,
    )
    async def slack_get_user_info(user_id: str) -> str:
        """Get information about a Slack user."""
        return _format(await client.get_user_info(user_id))

    return slack_get_user_info


def _make_slack_send_message(client: SlackSearchClient) -> ToolSpec:
    """Create the ``slack_send_message`` tool spec."""

    @tool(
        "slack_send_message",
        (
            "Send a message to a Slack channel or thread using the user token. "
            "Requires approval."
        ),
        {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "text": {"type": "string", "description": "Message text"},
                "thread_ts": {"type": "string", "description": "Thread timestamp (optional)"},
            },
            "required": ["channel_id", "text"],
        },
        display_name="Sending Slack message",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def slack_send_message(
        channel_id: str, text: str, thread_ts: str = ""
    ) -> str:
        """Send a message to a Slack channel or thread."""
        return _format(
            await client.send_message(channel_id, text, thread_ts=thread_ts)
        )

    return slack_send_message


# ── Registration ──────────────────────────────────────────────────────


def register_slack_search_tools(registry: ToolRegistry, config: SlackSearchConfig) -> None:
    """Register all Slack search/history tools with the tool registry.

    Args:
        registry: The tool registry to populate.
        config: Slack search configuration containing the user token.
    """
    client = SlackSearchClient(user_token=config.user_token)

    def _check() -> bool:
        return client.configured

    for factory in (
        _make_slack_search_messages,
        _make_slack_list_channels,
        _make_slack_get_channel_history,
        _make_slack_get_thread_replies,
        _make_slack_get_user_info,
        _make_slack_send_message,
    ):
        spec = factory(client)
        spec.check_fn = _check
        registry.register(spec)
