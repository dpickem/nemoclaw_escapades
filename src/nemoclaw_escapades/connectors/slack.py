"""Slack connector — Bolt for Python in socket mode.

First concrete ``ConnectorBase`` implementation.  Bridges the gap between
the Slack Events API and NemoClaw's platform-neutral request/response
model.

**Connection model** — Uses
`Slack Bolt for Python <https://slack.dev/bolt-python/>`_ with
*socket mode*.  Socket mode opens an outbound WebSocket to Slack,
which means:

- No public HTTP endpoint or ngrok tunnel required.
- Works from inside an OpenShell sandbox (outbound 443 to
  ``*.slack.com`` is all that's needed).
- Bolt handles reconnection automatically on transient disconnects.

**Event handling** — Listens for three event families:

- ``message`` — channel and DM messages.
- ``app_mention`` — ``@dbot`` mentions.
- ``block_actions`` — button clicks and other interactive callbacks.

Each event is *normalised* into a ``NormalizedRequest`` (stripping away
all Slack-specific structure) and forwarded to the orchestrator via the
``MessageHandler`` callback.

**Thinking indicator** — On every inbound message the connector posts a
transient ":hourglass_flowing_sand: Thinking…" placeholder in the thread
*before* calling the orchestrator.  When the orchestrator returns, the
placeholder is replaced in-place with the real response via
``chat_update``.  This gives the user instant visual feedback even when
inference takes several seconds.

**Bot-message filtering** — The connector silently drops its own
messages and any event with ``subtype=bot_message`` or a ``bot_id``
field, preventing infinite echo loops.

**Rendering** — The orchestrator returns a ``RichResponse`` built from
platform-neutral block types (``TextBlock``, ``ActionBlock``,
``ConfirmBlock``, ``FormBlock``).  The connector's ``render()`` method
translates each block into Slack Block Kit JSON:

- ``TextBlock``    → ``section`` with ``mrkdwn`` (markdown auto-converted)
- ``ActionBlock``  → ``actions`` with ``button`` elements
- ``ConfirmBlock`` → ``section`` with a ``confirm`` dialog accessory
- ``FormBlock``    → ``header`` + field sections with ``static_select``
  dropdowns + submit button
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any, cast

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from nemoclaw_escapades.connectors.base import ConnectorBase, MessageHandler
from nemoclaw_escapades.models.types import (
    APPROVAL_ACTION_APPROVE,
    APPROVAL_ACTION_DENY,
    ActionBlock,
    ActionPayload,
    ConfirmBlock,
    FormBlock,
    NormalizedRequest,
    ResponseBlock,
    RichResponse,
    TextBlock,
)
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("slack_connector")

# ── Constants ──────────────────────────────────────────────────────

# Compiled regexes for Markdown → Slack mrkdwn conversion.
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Reusable Block Kit divider block.
DIVIDER: dict[str, str] = {"type": "divider"}

# ── Slack payload limits ───────────────────────────────────────────
#
# Slack enforces these per-message limits; exceeding any of them
# causes the whole reply to be rejected with ``invalid_blocks`` or
# ``msg_too_long`` and the user sees nothing.  We keep a safety margin
# under each documented ceiling so later mrkdwn rewrites or emoji
# expansions can't push us over.
#
# - ``section.text.text``: Slack hard-limit 3000 chars.  Target 2900.
# - Blocks array: Slack hard-limit 50 blocks.  We budget at most
#   ``_SLACK_MAX_TEXTBLOCK_CHUNKS`` section blocks from splitting a
#   single oversize ``TextBlock`` so interactive tail blocks
#   (ActionBlock, ConfirmBlock) are preserved even for huge responses.
# - Top-level ``text`` fallback: Slack hard-limit ~40,000 chars.  We
#   truncate to 3000 since the fallback is only rendered by clients
#   that don't support Block Kit and can't show our real content
#   anyway.
_SLACK_SECTION_TEXT_LIMIT = 2900
_SLACK_MAX_TEXTBLOCK_CHUNKS = 40
_SLACK_FALLBACK_TEXT_LIMIT = 3000

# ── Error-response rate-limit window ───────────────────────────────
#
# Guard against the orchestrator's own error branch spamming a channel
# when the backend is persistently failing: at most
# ``_ERROR_MAX_PER_WINDOW`` error replies per channel per
# ``_ERROR_WINDOW_S`` seconds.
_ERROR_WINDOW_S: float = 60.0
_ERROR_MAX_PER_WINDOW: int = 3

# Slack ``message`` event subtypes we silently drop.  Deliberately an
# *explicit deny-list* (not "any subtype is not None"): Slack ships
# many subtypes that still represent real user content — e.g.
# ``file_share``, ``thread_broadcast``, ``me_message`` — and dropping
# those can strand a thread-reply event and cause the orchestrator to
# build an empty history.  If Slack adds a new bot-loop-triggering
# subtype, add it here explicitly.
_IGNORED_MESSAGE_SUBTYPES: frozenset[str] = frozenset(
    {
        # Bot-loop triggers — our own ``chat_update`` on the thinking
        # placeholder produces ``message_changed``; our own
        # ``chat_postMessage`` can echo back as ``bot_message``.
        "bot_message",
        "message_changed",
        "message_deleted",
        # Channel lifecycle — not user content.
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
    }
)

# ── Markdown → Slack mrkdwn conversion ─────────────────────────────


def _to_slack_markdown(text: str) -> str:
    """Convert common Markdown patterns to Slack mrkdwn syntax.

    - ``# Heading``     → ``*Heading*`` (bold)
    - ``**bold**``      → ``*bold*``
    - ``[label](url)``  → ``<url|label>``

    Args:
        text: Markdown-formatted string (typically from an LLM).

    Returns:
        The same text with patterns replaced for Slack rendering.
    """
    text = _HEADING_RE.sub(r"*\1*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _LINK_RE.sub(r"<\2|\1>", text)
    return text


def _split_text_for_slack(text: str, limit: int = _SLACK_SECTION_TEXT_LIMIT) -> list[str]:
    """Split *text* into chunks of at most *limit* characters each.

    Greedily chooses the highest-preference boundary that fits in the
    current window: paragraph break (``\\n\\n``) → line break
    (``\\n``) → whitespace → hard cut at *limit*.  Paragraph and line
    boundaries are preferred because they almost never split inline
    mrkdwn tokens (``*bold*``, ``<url|label>``) — a hard cut mid-token
    would corrupt the rendering.

    Hard cuts are only used for pathological input (prose with no
    whitespace at all).

    Args:
        text:  Slack-ready content (already markdown-converted if
               needed).  Must be non-empty.
        limit: Max chars per chunk.  Defaults to the per-section limit
               Slack enforces on ``section.text.text``.

    Returns:
        A non-empty list of chunks, each at most *limit* characters.
        Concatenating them (with a single newline between) reproduces
        the visible content modulo whitespace at chunk boundaries.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n\n")
        if split_at <= 0:
            split_at = window.rfind("\n")
        if split_at <= 0:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


# ── Primitive Block Kit helpers (à la nv-claw) ─────────────────────


def _section(text: str) -> dict[str, Any]:
    """Build a ``section`` block with mrkdwn text.

    Args:
        text: Slack mrkdwn-formatted content.

    Returns:
        A Block Kit section dict.
    """
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _plain_section(text: str) -> dict[str, Any]:
    """Build a ``section`` block with plain_text.

    Args:
        text: Plain (unformatted) content.

    Returns:
        A Block Kit section dict.
    """
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


def _context(*texts: str) -> dict[str, Any]:
    """Build a ``context`` block with one or more mrkdwn elements.

    Args:
        *texts: Slack mrkdwn strings, each rendered as a separate
                element in the context block.

    Returns:
        A Block Kit context dict.
    """
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": t} for t in texts],
    }


def _header(text: str) -> dict[str, Any]:
    """Build a ``header`` block.

    Args:
        text: Plain-text header content (max 150 chars per Slack).

    Returns:
        A Block Kit header dict.
    """
    return {"type": "header", "text": {"type": "plain_text", "text": text}}


def _button_actions(
    *buttons: tuple[str, str, str | None, str],
) -> dict[str, Any]:
    """Build an ``actions`` block from button descriptors.

    Args:
        *buttons: Variable number of 4-tuples, each containing
                  ``(label, action_id, style_or_None, value)``.

    Returns:
        A Block Kit actions dict.
    """
    elements: list[dict[str, Any]] = []
    for label, action_id, style, value in buttons:
        btn: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": action_id,
            "value": value,
        }
        if style:
            btn["style"] = style
        elements.append(btn)
    return {"type": "actions", "elements": elements}


def thinking_blocks() -> list[dict[str, Any]]:
    """Build the transient "Thinking …" placeholder.

    Returns:
        A single-element list with an hourglass section block.
    """
    return [_section(":hourglass_flowing_sand: *Thinking…*")]


# ── SlackConnector ─────────────────────────────────────────────────


class SlackConnector(ConnectorBase):
    """Slack-specific connector using Bolt in socket mode."""

    def __init__(
        self,
        handler: MessageHandler,
        bot_token: str,
        app_token: str,
    ) -> None:
        """Create the Bolt app, configure socket mode, and register listeners.

        Args:
            handler:   Orchestrator callback invoked for every normalised
                       request.  Signature:
                       ``async (NormalizedRequest) -> RichResponse``.
            bot_token: Slack bot OAuth token (``xoxb-...``).  Used to
                       authenticate API calls and identify the bot user.
            app_token: Slack app-level token (``xapp-...``).  Required
                       for the socket-mode WebSocket connection.
        """
        super().__init__(handler)
        self._bot_token = bot_token
        self._app_token = app_token
        self._bot_user_id: str | None = None

        self._app = AsyncApp(token=bot_token)
        self._socket_handler: AsyncSocketModeHandler | None = None

        self._error_timestamps: dict[str, list[float]] = defaultdict(list)

        # Tracks the most recent live approval-prompt message per thread
        # so the connector can mark the old prompt as superseded when a
        # new one supersedes it, and remove buttons after Approve / Deny.
        # Keyed by the conversation ``thread_key`` (thread_ts for threaded
        # messages, top-level ts otherwise); value is the ``(channel,
        # message_ts)`` of the live approval message.
        self._thread_approval_msg: dict[str, tuple[str, str]] = {}

        # Threads with an in-flight approval click — populated by
        # ``_handle_with_thinking`` to drop a rapid double-click before
        # it races into ``_post_thinking`` / the handler.  Cleared in
        # the same method's ``finally`` block.
        self._approval_in_flight: set[str] = set()

        self._register_listeners()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Authenticate with Slack and open the socket-mode WebSocket.

        Calls ``auth.test`` to resolve the bot's own user ID (needed
        for bot-message filtering), then starts the
        ``AsyncSocketModeHandler`` which maintains a persistent
        WebSocket to Slack's servers.
        """
        auth = await self._app.client.auth_test()
        self._bot_user_id = auth.get("user_id")
        logger.info("Slack bot authenticated", extra={"user_id": self._bot_user_id})

        self._socket_handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._socket_handler.start_async()  # type: ignore[no-untyped-call]
        logger.info("Slack connector started in socket mode")

    async def stop(self) -> None:
        """Close the socket-mode WebSocket and release resources."""
        if self._socket_handler:
            await self._socket_handler.close_async()  # type: ignore[no-untyped-call]
        logger.info("Slack connector stopped")

    # ------------------------------------------------------------------
    # Event registration
    # ------------------------------------------------------------------

    def _register_listeners(self) -> None:
        """Wire up Bolt event handlers for messages, mentions, and actions.

        All three event types funnel into ``_on_event``, which owns the
        shared pipeline: filter → normalise → thinking → orchestrate →
        reply.  The thin closures below only differ in how they extract
        a ``NormalizedRequest`` from the Slack payload.
        """

        @self._app.event("message")
        async def on_message(event: dict[str, Any], client: Any) -> None:
            await self._on_event(event, client)

        @self._app.event("app_mention")
        async def on_mention(event: dict[str, Any], client: Any) -> None:
            await self._on_event(event, client)

        # Bolt's @app.action("") only matches actions whose action_id is
        # literally the empty string.  A regex catch-all ensures buttons and
        # selects emitted by ActionBlock / FormBlock (which carry real
        # action_ids like "approve", "submit_colour", etc.) are routed here.
        @self._app.action(re.compile(r".*"))
        async def on_action(ack: Any, body: dict[str, Any], client: Any) -> None:
            await ack()
            request = self._normalize_action(body)
            logger.info(
                "Action received",
                extra={
                    "request_id": request.request_id,
                    "action": request.action.action_id if request.action else None,
                },
            )
            await self._handle_with_thinking(client, request)

        @self._app.event("app_home_opened")
        async def on_app_home(event: dict[str, Any], client: Any) -> None:
            user_id = event["user"]
            logger.info("App Home opened", extra={"user_id": user_id})
            try:
                await client.views_publish(
                    user_id=user_id,
                    view={
                        "type": "home",
                        "blocks": self._build_home_blocks(),
                    },
                )
            except Exception:
                logger.warning("Failed to publish App Home view", exc_info=True)

    async def _on_event(self, event: dict[str, Any], client: Any) -> None:
        """Shared handler for ``message`` and ``app_mention`` events.

        Applies bot-message filtering, normalises the event, logs it,
        and delegates to ``_handle_with_thinking``.

        Args:
            event:  Raw Slack event dict.
            client: Slack ``AsyncWebClient`` injected by Bolt.
        """
        # Log the raw Slack event shape *before* filtering so we can
        # diagnose missing ``thread_ts``, unexpected subtypes, or
        # duplicate deliveries.  Cheap — just a handful of fields.
        would_ignore = self._should_ignore(event)
        logger.info(
            "Slack event received",
            extra={
                "event_type": event.get("type"),
                "subtype": event.get("subtype"),
                "ts": event.get("ts"),
                "thread_ts": event.get("thread_ts"),
                "channel": event.get("channel"),
                "channel_type": event.get("channel_type"),
                "user": event.get("user"),
                "has_bot_id": bool(event.get("bot_id")),
                "would_ignore": would_ignore,
            },
        )
        if would_ignore:
            return
        request = self._normalize(event)
        logger.info(
            "Request received",
            extra={
                "request_id": request.request_id,
                "user_id": request.user_id,
                "channel_id": request.channel_id,
                "thread_ts": request.thread_ts,
            },
        )
        await self._handle_with_thinking(client, request)

    # ------------------------------------------------------------------
    # Thinking indicator + response delivery
    # ------------------------------------------------------------------

    async def _handle_with_thinking(self, client: Any, request: NormalizedRequest) -> None:
        """Post a thinking placeholder, call the orchestrator, then
        replace the placeholder with the real response.

        If the thinking message cannot be posted (e.g. permissions),
        falls back to posting a new message after the orchestrator
        returns.

        The thinking placeholder is updated in real time as the
        orchestrator executes tool calls (e.g. "Searching Jira...").

        Error responses are rate-limited: at most ``_ERROR_MAX_PER_WINDOW``
        error messages per channel per ``_ERROR_WINDOW_S`` seconds.  This
        prevents spam loops when the backend is persistently failing.

        Approval-click handling deviates from the regular request path
        in two ways, both to eliminate the double-click race where a
        user clicks Approve a second time while the orchestrator is
        still executing the first click's write:

        1. **Connector-side dedup.**  If an approval click for the same
           thread is already in flight, this call is dropped before any
           further work (no thinking placeholder, no orchestrator call).
        2. **Early button strip.**  A first-time approval click
           rewrites the clicked message to ":white_check_mark: Approved
           — executing" / ":x: Denied" *before* the orchestrator runs,
           so the buttons disappear the moment Slack delivers the event.
           Slack snapshots the message state at click time, so without
           this the buttons remain visible — and clickable — for as
           long as the tool executes.

        Args:
            client:  Slack ``AsyncWebClient`` injected by Bolt.
            request: The normalised inbound request.
        """
        channel = request.channel_id
        thread_ts = request.thread_ts
        # Key used to correlate live approval prompts with follow-up
        # clicks.  Mirrors the orchestrator's ``thread_key`` — the
        # Slack thread_ts when present, otherwise a synthetic key
        # anchored on the originating request id so standalone
        # (non-threaded) messages don't collide across channels.
        thread_key = thread_ts or f"{channel}:{request.request_id}"

        # ── Approval-click pre-processing ────────────────────────────
        # Performed before anything else so a rapid double-click can't
        # race into ``_post_thinking`` / ``_handler``.  ``ack()`` was
        # already sent by Bolt's listener.
        is_approval = self._is_approval_click(request)
        if is_approval:
            if thread_key in self._approval_in_flight:
                logger.info(
                    "Dropping duplicate approval click",
                    extra={
                        "request_id": request.request_id,
                        "thread_key": thread_key,
                        "action_id": (request.action.action_id if request.action else None),
                    },
                )
                return
            self._approval_in_flight.add(thread_key)
            # Rewrite the clicked message immediately so further clicks
            # can't target the same buttons.  Idempotent: a superseded
            # message is already buttonless; a second rewrite is a no-op.
            await self._consume_approval_click_ui(client, request, thread_key)

        try:
            # 1. Post a transient "Thinking…" placeholder so the user
            #    gets instant visual feedback while the orchestrator
            #    works.
            thinking_ts = await self._post_thinking(client, channel, thread_ts)

            # 2. Build a status callback that the orchestrator calls
            #    during tool execution (e.g. "Searching Jira…").  Each
            #    call swaps the placeholder text in-place via chat_update.
            async def _on_status(status: str) -> None:
                if thinking_ts:
                    try:
                        await client.chat_update(
                            channel=channel,
                            ts=thinking_ts,
                            text=status,
                            blocks=[_section(f":hourglass_flowing_sand: *{status}*")],
                        )
                    except Exception:
                        logger.debug("Failed to update thinking status", exc_info=True)

            # 3. Run the full agent loop (inference + tool calls + approval).
            response = await self._handler(request, _on_status)

            # 4. If the orchestrator returned an error and we've already
            #    sent too many error messages in this channel recently,
            #    silently delete the placeholder to avoid spam loops.
            if response.error_category is not None and self._is_error_rate_limited(channel):
                logger.warning(
                    "Suppressing error response (rate limit)",
                    extra={"channel": channel, "thread_ts": thread_ts},
                )
                if thinking_ts:
                    try:
                        await client.chat_delete(channel=channel, ts=thinking_ts)
                    except Exception:
                        pass
                return

            # 4b. Apply post-handler approval-lifecycle side effects.
            #     - Suppressed response (stale click that slipped past
            #       the in-flight dedup, e.g. a click from a previous
            #       bot run) → delete thinking placeholder, skip posting.
            #     - New approval prompt → supersede the prior live one
            #       so only the latest buttons look actionable.
            if await self._apply_approval_lifecycle(
                client, request, response, thread_key, thinking_ts
            ):
                return

            # 5. Render the platform-neutral RichResponse into Block Kit JSON.
            blocks = self.render(response)
            fallback_text = self._extract_fallback_text(response)

            # 6. Swap the placeholder with the real content (or post a
            #    new message if the placeholder was never created).
            if thinking_ts:
                posted_ts = await self._update_message(
                    client, channel, thinking_ts, fallback_text, blocks, request.request_id
                )
            else:
                posted_ts = await self._post_message(
                    client, channel, thread_ts, fallback_text, blocks, request.request_id
                )

            # 7. Record the live approval message's ts for later lifecycle
            #    updates (supersede / consume).  Only set when we actually
            #    posted an approval prompt and know the message's ts.
            if self._is_approval_prompt(response) and posted_ts:
                self._thread_approval_msg[thread_key] = (channel, posted_ts)
        finally:
            # Always release the in-flight slot, even on exception paths,
            # so a crash in one click doesn't wedge approvals for the
            # thread.  ``discard`` is a no-op for non-approval requests.
            if is_approval:
                self._approval_in_flight.discard(thread_key)

    def _is_error_rate_limited(self, channel: str) -> bool:
        """Check if error responses for this channel have exceeded the limit."""
        now = time.monotonic()
        timestamps = self._error_timestamps[channel]
        timestamps[:] = [t for t in timestamps if now - t < _ERROR_WINDOW_S]
        timestamps.append(now)
        return len(timestamps) > _ERROR_MAX_PER_WINDOW

    async def _post_thinking(self, client: Any, channel: str, thread_ts: str | None) -> str | None:
        """Post the transient thinking indicator and return its ``ts``.

        Args:
            client:    Slack ``AsyncWebClient``.
            channel:   Channel ID to post in.
            thread_ts: Parent thread timestamp.

        Returns:
            The ``ts`` of the posted message, or ``None`` on failure.
        """
        try:
            resp = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Thinking…",
                blocks=thinking_blocks(),
            )
            return cast(str | None, resp.get("ts"))
        except Exception:
            logger.warning("Failed to post thinking indicator", exc_info=True)
            return None

    async def _update_message(
        self,
        client: Any,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]],
        request_id: str = "",
    ) -> str | None:
        """Replace an existing message (the thinking placeholder) in-place.

        Falls back to posting a new message if the update fails.

        Args:
            client:     Slack ``AsyncWebClient``.
            channel:    Channel ID containing the message.
            ts:         Timestamp of the message to update.
            text:       Plain-text fallback.
            blocks:     Block Kit JSON dicts for the updated message.
            request_id: Correlation ID for structured logging.

        Returns:
            The ts of the (still-in-place) message on success, or the ts
            of the fallback post, or ``None`` if posting also failed.
            Callers use this to correlate posted approval prompts with
            later button clicks (see ``_thread_approval_msg``).
        """
        try:
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=text or "…",
                blocks=blocks if blocks else None,
            )
            logger.info(
                "Response sent (updated thinking message)",
                extra={"request_id": request_id, "channel_id": channel, "ts": ts},
            )
            return ts
        except Exception:
            logger.warning(
                "Failed to update thinking message, posting new message",
                exc_info=True,
            )
            return await self._post_message(client, channel, None, text, blocks, request_id)

    async def _post_message(
        self,
        client: Any,
        channel: str,
        thread_ts: str | None,
        text: str,
        blocks: list[dict[str, Any]],
        request_id: str = "",
    ) -> str | None:
        """Post a new message (fallback when thinking update fails).

        Args:
            client:     Slack ``AsyncWebClient``.
            channel:    Channel ID.
            thread_ts:  Parent thread timestamp.
            text:       Plain-text fallback.
            blocks:     Block Kit JSON dicts.
            request_id: Correlation ID for structured logging.

        Returns:
            The posted message's ``ts`` on success, or ``None`` on failure.
        """
        try:
            resp = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text or "…",
                blocks=blocks if blocks else None,
            )
            logger.info(
                "Response sent (new message)",
                extra={"request_id": request_id, "channel_id": channel, "thread_ts": thread_ts},
            )
            return cast(str | None, resp.get("ts"))
        except Exception:
            logger.error(
                "Failed to send Slack reply",
                extra={
                    "request_id": request_id,
                    "channel_id": channel,
                    "error_category": "connector_error",
                },
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Approval-button lifecycle
    # ------------------------------------------------------------------

    # Per-action-id UI state.  Routes the icon and consumed-state
    # label off the ``action_id`` directly rather than parsing a
    # human-readable label — so renaming "Approved — executing" to
    # "Approved, running" elsewhere doesn't break the icon mapping.
    _APPROVAL_CLICK_UI: dict[str, tuple[str, str]] = {
        APPROVAL_ACTION_APPROVE: (":white_check_mark:", "Approved — executing"),
        APPROVAL_ACTION_DENY: (":x:", "Denied"),
    }

    @staticmethod
    def _is_approval_prompt(response: RichResponse) -> bool:
        """Return ``True`` if *response* is a pending-write approval prompt.

        Detected by the presence of an Approve button on one of the
        response's action blocks.  Connectors use this to apply
        approval-specific UI lifecycle rules (supersede the prior
        prompt, mark consumed after click) without the orchestrator
        having to tag the response explicitly.
        """
        return any(
            isinstance(block, ActionBlock)
            and any(btn.action_id == APPROVAL_ACTION_APPROVE for btn in block.actions)
            for block in response.blocks
        )

    @classmethod
    def _approval_click_ui(cls, request: NormalizedRequest) -> tuple[str, str] | None:
        """Return ``(icon, consumed_label)`` for an approval-button click.

        Keyed on ``action_id`` — the label strings are derived from
        the action, not the other way around, so the UI stays
        consistent even if the user-facing phrasing is edited later.
        Returns ``None`` when *request* is not an approval click.
        """
        action = request.action
        if action is None:
            return None
        return cls._APPROVAL_CLICK_UI.get(action.action_id)

    @classmethod
    def _approval_click_outcome(cls, request: NormalizedRequest) -> str:
        """Return the consumed-state label for an approval-button click.

        Kept as a separate accessor because several call sites only
        care about the label ("did a click happen at all?") and
        shouldn't have to destructure the UI tuple.
        """
        ui = cls._approval_click_ui(request)
        return ui[1] if ui is not None else ""

    @staticmethod
    def _is_approval_click(request: NormalizedRequest) -> bool:
        """Return ``True`` when *request* carries an Approve or Deny button click.

        Cheap pre-check used to gate the approval-click-specific
        preprocessing (dedup + early button strip) in
        ``_handle_with_thinking`` without running the full UI lookup.
        """
        action = request.action
        return action is not None and action.action_id in (
            APPROVAL_ACTION_APPROVE,
            APPROVAL_ACTION_DENY,
        )

    async def _consume_approval_click_ui(
        self,
        client: Any,
        request: NormalizedRequest,
        thread_key: str,
    ) -> None:
        """Rewrite the clicked approval message to its consumed state.

        Invoked upfront — before ``_post_thinking`` / ``_handler`` —
        so the Approve / Deny buttons vanish the moment Slack
        delivers the click event.  Without this, Slack snapshots the
        message state at click time and the user can click Approve
        again while the first click's ``git_clone`` is still running;
        the stale click then races into the orchestrator and posts
        noise.

        Idempotent: a subsequent click on the same (already-consumed)
        message just rewrites the same state; the ``_thread_approval_msg``
        entry is popped once and stays gone.

        Args:
            client: Slack ``AsyncWebClient``.
            request: Inbound approval-click request.
            thread_key: Conversation thread key; matches the
                orchestrator's ``thread_key`` for keyed-per-thread state.
        """
        ui = self._approval_click_ui(request)
        if ui is None:
            return
        icon, outcome = ui
        await self._update_clicked_approval(client, request, icon, outcome)
        self._thread_approval_msg.pop(thread_key, None)

    async def _apply_approval_lifecycle(
        self,
        client: Any,
        request: NormalizedRequest,
        response: RichResponse,
        thread_key: str,
        thinking_ts: str | None,
    ) -> bool:
        """Apply post-handler approval-lifecycle side effects.

        Runs *after* the orchestrator returns.  Two effects:

        1. **Suppressed response (stale click that slipped past the
           in-flight dedup).**  The orchestrator returned
           ``suppress_post=True`` — delete the thinking placeholder
           and return ``True`` so the caller skips the normal post
           path.  Can still happen for a click that arrives from a
           previous bot run (in-flight tracking is in-memory).
        2. **New approval prompt.**  When the response is itself a
           new Approve/Deny prompt and the thread already tracks a
           prior live one, rewrite the prior as "Superseded" so only
           the newest buttons look actionable.

        The **consumed-state rewrite** (Approved — executing / Denied)
        for the clicked message happens upfront in
        ``_consume_approval_click_ui``, not here — see that method's
        docstring for the rationale.

        Args:
            client: Slack ``AsyncWebClient``.
            request: The inbound request (may carry an action payload).
            response: Orchestrator's reply.
            thread_key: Connector-side thread key; matches the
                orchestrator's ``thread_key`` for keyed-per-thread state.
            thinking_ts: Thinking placeholder ``ts``, or ``None`` if
                we never managed to post it.

        Returns:
            ``True`` iff the caller should skip the normal post /
            update path (stale-click short-circuit).  ``False``
            otherwise.
        """
        channel = request.channel_id
        # 1. Suppressed response: clean placeholder, tell caller to skip.
        if response.suppress_post:
            if thinking_ts:
                try:
                    await client.chat_delete(channel=channel, ts=thinking_ts)
                except Exception:
                    logger.debug("Failed to delete thinking placeholder", exc_info=True)
            return True

        # 2. New approval prompt: supersede the prior one, if any.
        if self._is_approval_prompt(response):
            await self._supersede_prior_approval(client, thread_key)

        return False

    async def _update_clicked_approval(
        self,
        client: Any,
        request: NormalizedRequest,
        icon: str,
        outcome: str,
    ) -> None:
        """Rewrite the clicked approval message to its consumed state.

        Strips the Approve / Deny buttons and replaces them with a
        one-line status so the user can tell at a glance which prompts
        are still actionable.  Failures are logged at DEBUG — the main
        response still posts regardless.
        """
        raw = request.raw_event
        message = raw.get("message") if isinstance(raw, dict) else None
        if not isinstance(message, dict):
            return
        message_ts = message.get("ts")
        if not message_ts:
            return
        try:
            await client.chat_update(
                channel=request.channel_id,
                ts=message_ts,
                text=f"{icon} {outcome}",
                blocks=[_section(f"{icon} _{outcome}_")],
            )
        except Exception:
            logger.debug(
                "Failed to update consumed approval message",
                extra={"channel_id": request.channel_id, "ts": message_ts},
                exc_info=True,
            )

    async def _supersede_prior_approval(self, client: Any, thread_key: str) -> None:
        """Mark the previous live approval prompt for *thread_key* as superseded.

        A new approval prompt is about to be posted; rewrite the prior
        one to strip its buttons so the user doesn't click both.  No-op
        when no prior prompt is tracked for the thread.
        """
        prior = self._thread_approval_msg.pop(thread_key, None)
        if prior is None:
            return
        channel, ts = prior
        try:
            await client.chat_update(
                channel=channel,
                ts=ts,
                text="Superseded — see newer approval prompt below",
                blocks=[_section(":arrow_down: _Superseded — see newer approval prompt below._")],
            )
        except Exception:
            logger.debug(
                "Failed to supersede prior approval message",
                extra={"channel_id": channel, "ts": ts},
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _should_ignore(self, event: dict[str, Any]) -> bool:
        """Return ``True`` if this event should be silently dropped.

        Drops events whose ``subtype`` is in ``_IGNORED_MESSAGE_SUBTYPES``
        — primarily ``message_changed``/``message_deleted``/``bot_message``,
        which the bot generates for itself whenever it updates the
        thinking placeholder — plus channel lifecycle events that
        aren't user content.

        Intentionally does **not** drop on "any subtype is non-null":
        Slack uses subtypes for many legitimate user messages
        (``file_share``, ``thread_broadcast``, ``me_message``, …), and
        dropping those would strand the matching thread-reply event
        and make the orchestrator see an empty conversation.

        As a defense-in-depth measure, also drops events with a
        ``bot_id`` field or matching the bot's own user ID.

        Args:
            event: Raw Slack event dict.

        Returns:
            ``True`` to skip the event, ``False`` to process it.
        """
        subtype = event.get("subtype")
        if subtype is not None and subtype in _IGNORED_MESSAGE_SUBTYPES:
            return True
        if event.get("bot_id"):
            return True
        if self._bot_user_id and event.get("user") == self._bot_user_id:
            return True
        return False

    @staticmethod
    def _normalize(event: dict[str, Any]) -> NormalizedRequest:
        """Convert a raw Slack ``message`` or ``app_mention`` event into
        a platform-neutral ``NormalizedRequest``.

        Args:
            event: Raw Slack event dict containing ``text``, ``user``,
                   ``channel``, ``ts``, and optionally ``thread_ts``.

        Returns:
            A ``NormalizedRequest`` with ``source="slack"`` and the
            original event preserved in ``raw_event``.
        """
        return NormalizedRequest(
            text=event.get("text", ""),
            user_id=event.get("user", ""),
            channel_id=event.get("channel", ""),
            thread_ts=event.get("thread_ts") or event.get("ts"),
            timestamp=time.time(),
            source="slack",
            raw_event=event,
        )

    @staticmethod
    def _normalize_action(body: dict[str, Any]) -> NormalizedRequest:
        """Convert a Slack ``block_actions`` interaction payload into a
        ``NormalizedRequest`` with an ``ActionPayload``.

        Extracts the first action from the ``actions`` array, the user
        and channel from the top-level body, and the thread context from
        the originating message.

        Args:
            body: The full Slack interaction payload (contains
                  ``actions``, ``user``, ``channel``, ``message``,
                  etc.).

        Returns:
            A ``NormalizedRequest`` whose ``action`` field is populated
            with the button/interaction metadata.
        """
        actions = body.get("actions", [{}])
        action_data = actions[0] if actions else {}
        channel = body.get("channel", {})
        user = body.get("user", {})
        message = body.get("message", {})

        return NormalizedRequest(
            text=action_data.get("value", ""),
            user_id=user.get("id", ""),
            channel_id=channel.get("id", "") if isinstance(channel, dict) else str(channel),
            thread_ts=message.get("thread_ts") or message.get("ts"),
            timestamp=time.time(),
            source="slack",
            action=ActionPayload(
                action_id=action_data.get("action_id", ""),
                value=action_data.get("value", ""),
                metadata=action_data,
            ),
            raw_event=body,
        )

    # ------------------------------------------------------------------
    # Rendering: RichResponse → Block Kit JSON
    # ------------------------------------------------------------------

    @staticmethod
    def render(response: RichResponse) -> list[dict[str, Any]]:
        """Translate platform-neutral ``ResponseBlock`` objects into
        Slack Block Kit JSON dicts.

        Iterates over ``response.blocks`` and delegates each one to
        ``_render_block``.  Most blocks produce a single Block Kit dict;
        ``FormBlock`` expands to multiple (header + fields + submit).
        Unrecognised block subclasses fall back to a plain-text
        ``str()`` representation so no content is lost.

        Args:
            response: The orchestrator's platform-neutral response.

        Returns:
            A flat list of Block Kit JSON dicts ready for the Slack
            API's ``blocks`` parameter.
        """
        result: list[dict[str, Any]] = []
        for block in response.blocks:
            rendered = SlackConnector._render_block(block)
            if isinstance(rendered, list):
                result.extend(rendered)
            else:
                result.append(rendered)
        return result

    @staticmethod
    def _render_block(
        block: ResponseBlock,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Render a single ``ResponseBlock`` into Block Kit JSON.

        Mapping:

        - ``TextBlock``    → ``section`` with ``mrkdwn`` (markdown
          auto-converted) or ``plain_text``
        - ``ActionBlock``  → ``actions`` with ``button`` elements
        - ``ConfirmBlock`` → ``section`` with a ``confirm`` dialog
          accessory
        - ``FormBlock``    → list of blocks: ``header`` + field
          sections + submit button

        Args:
            block: A platform-neutral ``ResponseBlock`` subclass.

        Returns:
            A single Block Kit JSON dict, or a list of dicts for
            compound blocks (``FormBlock``).  Unrecognised block types
            fall back to a plain-text ``section`` using ``str(block)``.
        """
        if isinstance(block, TextBlock):
            if block.style == "markdown":
                text = _to_slack_markdown(block.text)
                section_builder = _section
            else:
                text = block.text
                section_builder = _plain_section
            chunks = _split_text_for_slack(text)
            # Cap the per-TextBlock chunk count so a pathologically
            # long response (e.g. an LLM dumping 200 KB of prose)
            # can't consume the whole 50-block message budget and
            # starve out interactive tail blocks like ActionBlock.
            if len(chunks) > _SLACK_MAX_TEXTBLOCK_CHUNKS:
                logger.warning(
                    "TextBlock exceeded max chunks; truncating",
                    extra={
                        "total_chars": len(text),
                        "total_chunks": len(chunks),
                        "kept_chunks": _SLACK_MAX_TEXTBLOCK_CHUNKS,
                    },
                )
                chunks = chunks[: _SLACK_MAX_TEXTBLOCK_CHUNKS - 1] + [
                    ":warning: _Response truncated — too long to display in full._"
                ]
            if len(chunks) == 1:
                return section_builder(chunks[0])
            return [section_builder(chunk) for chunk in chunks]

        if isinstance(block, ActionBlock):
            return _button_actions(
                *[(btn.label, btn.action_id, btn.style, btn.value) for btn in block.actions]
            )

        if isinstance(block, ConfirmBlock):
            return {
                "type": "section",
                "text": {"type": "mrkdwn", "text": block.text},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": block.confirm_label},
                    "action_id": block.action_id,
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": block.title},
                        "text": {"type": "mrkdwn", "text": block.text},
                        "confirm": {
                            "type": "plain_text",
                            "text": block.confirm_label,
                        },
                        "deny": {"type": "plain_text", "text": block.deny_label},
                    },
                },
            }

        if isinstance(block, FormBlock):
            return SlackConnector._render_form(block)

        logger.warning(
            "Unrecognised ResponseBlock subclass %s; falling back to str()",
            type(block).__name__,
        )
        return _section(str(block))

    @staticmethod
    def _render_form(block: FormBlock) -> list[dict[str, Any]]:
        """Render a ``FormBlock`` as a sequence of Block Kit blocks.

        Slack messages cannot contain ``input`` blocks (those are
        modal-only), so the rendering adapts per field type:

        - ``select`` fields → ``section`` with a ``static_select``
          accessory dropdown populated from ``field.options``.
        - ``text`` / ``multiline`` / other fields → labelled
          ``section`` prompting the user to reply in-thread.

        A submit button is appended when ``submit_action_id`` is set.

        Args:
            block: The ``FormBlock`` to render.

        Returns:
            A list of Block Kit JSON dicts (header + field sections +
            submit button).
        """
        result: list[dict[str, Any]] = []

        if block.title:
            result.append(_header(block.title))

        for field in block.fields:
            required_marker = " \\*" if field.required else ""

            if field.field_type == "select" and field.options:
                options = [
                    {
                        "text": {"type": "plain_text", "text": opt},
                        "value": opt,
                    }
                    for opt in field.options
                ]
                result.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{field.label}*{required_marker}",
                        },
                        "accessory": {
                            "type": "static_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": f"Choose {field.label.lower()}…",
                            },
                            "action_id": field.field_id,
                            "options": options,
                        },
                    }
                )
            else:
                hint = (
                    "Reply in this thread with your response."
                    if field.field_type in ("text", "multiline")
                    else f"_{field.field_type}_"
                )
                result.append(_section(f"*{field.label}*{required_marker}\n{hint}"))

        if block.submit_action_id:
            result.append(_button_actions(("Submit", block.submit_action_id, "primary", "")))

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_home_blocks() -> list[dict[str, Any]]:
        """Build Block Kit blocks for the App Home tab."""
        return [
            _header(":robot_face: dbot"),
            _section(
                "Your AI-powered assistant for developer services — "
                "Jira, Confluence, Slack, GitLab, Gerrit, and more."
            ),
            DIVIDER,
            _header(":sparkles: What can I do?"),
            _section(
                ":mag: *Search & lookup*\n"
                '_"What are my open Jira tickets?"_\n'
                '_"Show me AVPC-61317"_\n'
                '_"Find Confluence pages about deployment"_'
            ),
            _section(
                ":hammer_and_wrench: *Take action*\n"
                '_"Create a Jira ticket for ..."_\n'
                '_"Transition AVPC-12345 to In Review"_\n'
                '_"Post a summary to #my-channel"_'
            ),
            _section(
                ":brain: *Analyze & summarize*\n"
                '_"Summarize the last sprint\'s tickets"_\n'
                '_"What changed in CL 12345?"_'
            ),
            DIVIDER,
            _header(":rocket: Getting started"),
            _section(
                "Just send me a DM or mention *@dbot* in any channel. "
                "I'll figure out which tools to use and ask for approval "
                "before taking any write actions."
            ),
            DIVIDER,
            _context("Built with NemoClaw Escapades  :zap:  Powered by nv-tools"),
        ]

    @staticmethod
    def _extract_fallback_text(response: RichResponse) -> str:
        """Extract a plain-text fallback from the first ``TextBlock``.

        Used for Slack notifications and non-Block-Kit clients.  The
        result is truncated to ``_SLACK_FALLBACK_TEXT_LIMIT`` chars so
        a pathologically long text block can't trip Slack's top-level
        ``text`` length limit (observed in practice as a
        ``msg_too_long`` error on ``chat.update``).  Non-Block-Kit
        clients can't show the full response anyway.

        Args:
            response: The orchestrator's platform-neutral response.

        Returns:
            Up to ``_SLACK_FALLBACK_TEXT_LIMIT`` chars of the first
            ``TextBlock``'s text, or ``"…"`` if no text block is found.
        """
        for block in response.blocks:
            if isinstance(block, TextBlock):
                text = block.text
                if len(text) > _SLACK_FALLBACK_TEXT_LIMIT:
                    text = text[: _SLACK_FALLBACK_TEXT_LIMIT - 1] + "…"
                return text
        return "…"
