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
        self._ERROR_WINDOW_S = 60.0
        self._ERROR_MAX_PER_WINDOW = 3

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
        if self._should_ignore(event):
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

        Args:
            client:  Slack ``AsyncWebClient`` injected by Bolt.
            request: The normalised inbound request.
        """
        channel = request.channel_id
        thread_ts = request.thread_ts

        # 1. Post a transient "Thinking…" placeholder so the user gets
        #    instant visual feedback while the orchestrator works.
        thinking_ts = await self._post_thinking(client, channel, thread_ts)

        # 2. Build a status callback that the orchestrator calls during
        #    tool execution (e.g. "Searching Jira…").  Each call swaps
        #    the placeholder text in-place via chat_update.
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

        # 5. Render the platform-neutral RichResponse into Block Kit JSON.
        blocks = self.render(response)
        fallback_text = self._extract_fallback_text(response)

        # 6. Swap the placeholder with the real content (or post a new
        #    message if the placeholder was never created).
        if thinking_ts:
            await self._update_message(
                client, channel, thinking_ts, fallback_text, blocks, request.request_id
            )
        else:
            await self._post_message(
                client, channel, thread_ts, fallback_text, blocks, request.request_id
            )

    def _is_error_rate_limited(self, channel: str) -> bool:
        """Check if error responses for this channel have exceeded the limit."""
        now = time.monotonic()
        timestamps = self._error_timestamps[channel]
        timestamps[:] = [t for t in timestamps if now - t < self._ERROR_WINDOW_S]
        timestamps.append(now)
        return len(timestamps) > self._ERROR_MAX_PER_WINDOW

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
    ) -> None:
        """Replace an existing message (the thinking placeholder) in-place.

        Falls back to posting a new message if the update fails.

        Args:
            client:     Slack ``AsyncWebClient``.
            channel:    Channel ID containing the message.
            ts:         Timestamp of the message to update.
            text:       Plain-text fallback.
            blocks:     Block Kit JSON dicts for the updated message.
            request_id: Correlation ID for structured logging.
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
        except Exception:
            logger.warning(
                "Failed to update thinking message, posting new message",
                exc_info=True,
            )
            await self._post_message(client, channel, None, text, blocks, request_id)

    async def _post_message(
        self,
        client: Any,
        channel: str,
        thread_ts: str | None,
        text: str,
        blocks: list[dict[str, Any]],
        request_id: str = "",
    ) -> None:
        """Post a new message (fallback when thinking update fails).

        Args:
            client:     Slack ``AsyncWebClient``.
            channel:    Channel ID.
            thread_ts:  Parent thread timestamp.
            text:       Plain-text fallback.
            blocks:     Block Kit JSON dicts.
            request_id: Correlation ID for structured logging.
        """
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text or "…",
                blocks=blocks if blocks else None,
            )
            logger.info(
                "Response sent (new message)",
                extra={"request_id": request_id, "channel_id": channel, "thread_ts": thread_ts},
            )
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

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _should_ignore(self, event: dict[str, Any]) -> bool:
        """Return ``True`` if this event should be silently dropped.

        Only processes events with no ``subtype`` (normal user messages).
        All subtypes — ``bot_message``, ``message_changed``,
        ``message_deleted``, etc. — are dropped.  This prevents infinite
        loops where the bot's own ``chat_update`` calls generate
        ``message_changed`` events that trigger reprocessing.

        As a defense-in-depth measure, also drops events with a
        ``bot_id`` field or matching the bot's own user ID.

        Args:
            event: Raw Slack event dict.

        Returns:
            ``True`` to skip the event, ``False`` to process it.
        """
        if event.get("subtype") is not None:
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
                return _section(_to_slack_markdown(block.text))
            return _plain_section(block.text)

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

        Used for Slack notifications and non-Block-Kit clients.

        Args:
            response: The orchestrator's platform-neutral response.

        Returns:
            The text content of the first ``TextBlock``, or ``"…"`` if
            no text block is found.
        """
        for block in response.blocks:
            if isinstance(block, TextBlock):
                return block.text
        return "…"
