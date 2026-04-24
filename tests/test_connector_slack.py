"""Tests for the SlackConnector — normalization, rendering, bot filtering,
markdown conversion, and block-builder helpers.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from nemoclaw_escapades.connectors.slack import (
    _SLACK_FALLBACK_TEXT_LIMIT,
    _SLACK_SECTION_TEXT_LIMIT,
    SlackConnector,
    _split_text_for_slack,
    _to_slack_markdown,
    thinking_blocks,
)
from nemoclaw_escapades.models.types import (
    APPROVAL_ACTION_APPROVE,
    APPROVAL_ACTION_DENY,
    ActionBlock,
    ActionButton,
    ActionPayload,
    ConfirmBlock,
    FormBlock,
    FormField,
    NormalizedRequest,
    RichResponse,
    TextBlock,
)


class TestNormalization:
    """Tests for Slack event → NormalizedRequest conversion."""

    def test_normalize_message_event(self, sample_slack_event: dict) -> None:
        request = SlackConnector._normalize(sample_slack_event)
        assert request.text == "Hello, NemoClaw!"
        assert request.user_id == "U12345"
        assert request.channel_id == "C12345"
        assert request.thread_ts == "1234567890.000000"
        assert request.source == "slack"

    def test_normalize_uses_ts_when_no_thread(self) -> None:
        event = {
            "text": "standalone message",
            "user": "U1",
            "channel": "C1",
            "ts": "999.000",
        }
        request = SlackConnector._normalize(event)
        assert request.thread_ts == "999.000"

    def test_normalize_action_event(self) -> None:
        body = {
            "actions": [
                {
                    "action_id": "approve_btn",
                    "value": "yes",
                    "type": "button",
                }
            ],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "123.456", "thread_ts": "123.000"},
        }
        request = SlackConnector._normalize_action(body)
        assert request.text == "yes"
        assert request.action is not None
        assert request.action.action_id == "approve_btn"
        assert request.action.value == "yes"


class TestBotFiltering:
    """Tests for ignoring bot messages."""

    def test_ignore_bot_message_subtype(self) -> None:
        event = {"subtype": "bot_message", "text": "bot says hi"}
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is True

    def test_ignore_message_changed_subtype(self) -> None:
        # Our own chat_update on the thinking placeholder triggers
        # ``message_changed``.  Must stay ignored to prevent bot-loop.
        event = {"subtype": "message_changed", "text": "edited"}
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is True

    def test_allow_thread_broadcast_subtype(self) -> None:
        # ``thread_broadcast`` is a legitimate user message posted as a
        # thread reply *and* broadcast to the channel.  Must pass
        # through so its ``thread_ts`` reaches the orchestrator.
        event = {
            "subtype": "thread_broadcast",
            "user": "U123",
            "text": "broadcast reply",
            "ts": "123.999",
            "thread_ts": "123.000",
        }
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is False

    def test_ignore_event_with_bot_id(self) -> None:
        event = {"bot_id": "B123", "text": "also a bot"}
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is True

    def test_ignore_own_user_id(self) -> None:
        event = {"user": "B999", "text": "my own message"}
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is True

    def test_allow_normal_user_message(self) -> None:
        event = {"user": "U123", "text": "hello"}
        connector = SlackConnector.__new__(SlackConnector)
        connector._bot_user_id = "B999"
        assert connector._should_ignore(event) is False


class TestRendering:
    """Tests for RichResponse → Block Kit JSON conversion."""

    def test_render_text_block_markdown(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text="*bold* text", style="markdown")],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["type"] == "mrkdwn"
        assert blocks[0]["text"]["text"] == "*bold* text"

    def test_render_text_block_plain(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text="plain text", style="plain")],
        )
        blocks = SlackConnector.render(response)
        assert blocks[0]["text"]["type"] == "plain_text"

    def test_render_action_block(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                ActionBlock(
                    actions=[
                        ActionButton(
                            label="Approve",
                            action_id="approve",
                            value="yes",
                            style="primary",
                        ),
                        ActionButton(
                            label="Reject",
                            action_id="reject",
                            value="no",
                            style="danger",
                        ),
                    ]
                )
            ],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        elements = blocks[0]["elements"]
        assert len(elements) == 2
        assert elements[0]["text"]["text"] == "Approve"
        assert elements[0]["style"] == "primary"
        assert elements[1]["action_id"] == "reject"

    def test_render_confirm_block(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                ConfirmBlock(
                    title="Are you sure?",
                    text="This action is destructive.",
                    confirm_label="Yes, do it",
                    deny_label="Cancel",
                    action_id="destructive_action",
                )
            ],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        accessory = blocks[0]["accessory"]
        assert accessory["type"] == "button"
        assert accessory["confirm"]["title"]["text"] == "Are you sure?"

    def test_render_mixed_blocks(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                TextBlock(text="Choose an option:"),
                ActionBlock(
                    actions=[
                        ActionButton(label="Option A", action_id="a", value="a"),
                        ActionButton(label="Option B", action_id="b", value="b"),
                    ]
                ),
            ],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "actions"

    def test_render_empty_response(self) -> None:
        response = RichResponse(channel_id="C1", blocks=[])
        blocks = SlackConnector.render(response)
        assert blocks == []

    def test_render_form_select_field(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                FormBlock(
                    title="Pick a colour",
                    fields=[
                        FormField(
                            label="Colour",
                            field_id="colour_select",
                            field_type="select",
                            options=["Red", "Green", "Blue"],
                        ),
                    ],
                    submit_action_id="submit_colour",
                ),
            ],
        )
        blocks = SlackConnector.render(response)
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["text"] == "Pick a colour"

        select_section = blocks[1]
        assert select_section["type"] == "section"
        accessory = select_section["accessory"]
        assert accessory["type"] == "static_select"
        assert accessory["action_id"] == "colour_select"
        assert len(accessory["options"]) == 3
        assert accessory["options"][0]["value"] == "Red"

        submit = blocks[2]
        assert submit["type"] == "actions"
        assert submit["elements"][0]["action_id"] == "submit_colour"

    def test_render_form_text_field(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                FormBlock(
                    title="Feedback",
                    fields=[
                        FormField(
                            label="Comments",
                            field_id="comments",
                            field_type="text",
                            required=True,
                        ),
                    ],
                    submit_action_id="submit_feedback",
                ),
            ],
        )
        blocks = SlackConnector.render(response)
        assert blocks[0]["type"] == "header"

        text_section = blocks[1]
        assert text_section["type"] == "section"
        assert "Comments" in text_section["text"]["text"]
        assert "Reply in this thread" in text_section["text"]["text"]

    def test_render_form_expands_into_multiple_blocks(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[
                TextBlock(text="Please fill out:"),
                FormBlock(
                    title="Survey",
                    fields=[
                        FormField(label="Name", field_id="name", field_type="text"),
                        FormField(
                            label="Role",
                            field_id="role",
                            field_type="select",
                            options=["Eng", "PM"],
                        ),
                    ],
                    submit_action_id="submit_survey",
                ),
            ],
        )
        blocks = SlackConnector.render(response)
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "header"
        assert blocks[1]["text"]["text"] == "Survey"
        assert blocks[-1]["type"] == "actions"
        assert len(blocks) == 5

    def test_render_markdown_converts_bold(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text="This is **important** info.")],
        )
        blocks = SlackConnector.render(response)
        assert blocks[0]["text"]["text"] == "This is *important* info."

    def test_render_markdown_converts_links(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text="See [docs](https://example.com).")],
        )
        blocks = SlackConnector.render(response)
        assert "<https://example.com|docs>" in blocks[0]["text"]["text"]

    def test_render_long_text_block_splits_into_multiple_sections(self) -> None:
        # Slack rejects any section whose ``text.text`` exceeds 3000
        # chars (``invalid_blocks`` error).  A long TextBlock must be
        # split into multiple section blocks so every chunk fits.
        paragraphs = ["x" * 100 for _ in range(100)]
        long_text = "\n\n".join(paragraphs)
        assert len(long_text) > _SLACK_SECTION_TEXT_LIMIT
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text=long_text, style="markdown")],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) > 1
        for block in blocks:
            assert block["type"] == "section"
            assert block["text"]["type"] == "mrkdwn"
            # Strictly under Slack's 3000-char cap, not just under our
            # internal 2900 target — the whole point of the splitter.
            assert len(block["text"]["text"]) <= 3000

    def test_render_long_plain_text_block_splits_as_plain_sections(self) -> None:
        long_text = ("y" * 100 + "\n\n") * 100
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text=long_text, style="plain")],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) > 1
        for block in blocks:
            assert block["text"]["type"] == "plain_text"
            assert len(block["text"]["text"]) <= 3000

    def test_render_pathological_text_block_truncates_with_marker(self) -> None:
        # 200 KB of unbroken content — can't split on paragraph /
        # line / whitespace.  The splitter must still keep every
        # chunk under the section cap, and the connector must stop
        # before consuming the whole 50-block message budget so
        # trailing interactive blocks survive.
        long_text = "x" * 200_000
        response = RichResponse(
            channel_id="C1",
            blocks=[
                TextBlock(text=long_text),
                ActionBlock(
                    actions=[
                        ActionButton(label="OK", action_id="ok", value="ok"),
                    ]
                ),
            ],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) <= 50
        for block in blocks:
            if block.get("type") == "section":
                assert len(block["text"]["text"]) <= 3000
        # Truncation marker appears somewhere in the rendered
        # sections so the user knows output was cut.
        section_texts = [
            block["text"]["text"] for block in blocks if block.get("type") == "section"
        ]
        assert any("truncated" in t.lower() for t in section_texts)
        # Tail ActionBlock is preserved — truncating an approval
        # prompt's buttons would strand the user with an un-actionable
        # response.
        assert blocks[-1]["type"] == "actions"
        assert blocks[-1]["elements"][0]["action_id"] == "ok"

    def test_render_markdown_formatting_survives_split(self) -> None:
        # Paragraph boundaries are plentiful; each chunk should keep
        # its mrkdwn tokens intact (``*bold*`` never gets cut).
        block = "**bold** and [link](https://example.com)"
        long_text = "\n\n".join(block for _ in range(300))
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text=long_text, style="markdown")],
        )
        blocks = SlackConnector.render(response)
        assert len(blocks) > 1
        for block_dict in blocks:
            text = block_dict["text"]["text"]
            # No dangling ``**`` — the splitter broke on paragraph
            # boundaries, not mid-token.  The mrkdwn converter has
            # already replaced ``**bold**`` with ``*bold*``.
            assert "**" not in text


class TestFallbackTextTruncation:
    """``_extract_fallback_text`` caps length so chat.update/postMessage
    never trip Slack's top-level ``text`` limit (seen as
    ``msg_too_long``).
    """

    def test_short_text_passes_through(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text="short")],
        )
        assert SlackConnector._extract_fallback_text(response) == "short"

    def test_long_text_truncates_with_ellipsis(self) -> None:
        long_text = "x" * 10_000
        response = RichResponse(
            channel_id="C1",
            blocks=[TextBlock(text=long_text)],
        )
        result = SlackConnector._extract_fallback_text(response)
        assert len(result) <= _SLACK_FALLBACK_TEXT_LIMIT
        # Ellipsis marker signals truncation downstream — keeps the
        # fallback self-describing for clients that can't render
        # Block Kit.
        assert result.endswith("…")

    def test_no_text_block_returns_placeholder(self) -> None:
        response = RichResponse(
            channel_id="C1",
            blocks=[ActionBlock(actions=[ActionButton(label="Go", action_id="go", value="")])],
        )
        assert SlackConnector._extract_fallback_text(response) == "…"


class TestSplitTextForSlack:
    """``_split_text_for_slack`` chunks text under the section limit
    while preserving paragraph/line boundaries where possible.
    """

    def test_short_text_returns_single_chunk(self) -> None:
        assert _split_text_for_slack("short") == ["short"]
        assert _split_text_for_slack("", limit=10) == [""]

    def test_exact_limit_returns_single_chunk(self) -> None:
        text = "a" * 100
        assert _split_text_for_slack(text, limit=100) == [text]

    def test_prefers_paragraph_boundary(self) -> None:
        text = ("a" * 1000) + "\n\n" + ("b" * 2000) + "\n\n" + ("c" * 100)
        chunks = _split_text_for_slack(text, limit=2500)
        # Split at the first ``\n\n`` (position 1000), leaving the
        # rest below the 2500 limit.
        assert len(chunks) == 2
        assert chunks[0] == "a" * 1000
        assert chunks[1].startswith("b")

    def test_falls_back_to_line_boundary(self) -> None:
        # No paragraph breaks; single ``\n`` is the best option.
        text = ("a" * 1500) + "\n" + ("b" * 1500)
        chunks = _split_text_for_slack(text, limit=2000)
        assert len(chunks) == 2
        assert all(len(c) <= 2000 for c in chunks)

    def test_falls_back_to_whitespace(self) -> None:
        # No line breaks; the splitter must fall back to spaces so
        # words aren't cut in half.
        words = ["word" for _ in range(1000)]
        text = " ".join(words)
        chunks = _split_text_for_slack(text, limit=200)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 200
            # Hard cuts mid-word would leave a chunk starting or
            # ending with a partial "wor" / "ord".  The whitespace
            # fallback avoids that.
            assert not chunk.endswith("wor")

    def test_hard_cut_when_no_boundaries(self) -> None:
        # Pathological input (no whitespace of any kind) still
        # returns chunks of at most ``limit`` chars — the Slack cap
        # is non-negotiable, so corruption beats a silent 500.
        text = "x" * 5000
        chunks = _split_text_for_slack(text, limit=1000)
        assert len(chunks) == 5
        assert all(len(c) == 1000 for c in chunks)


class TestSlackMarkdownConversion:
    """Tests for _to_slack_markdown."""

    def test_headings(self) -> None:
        assert _to_slack_markdown("# Title") == "*Title*"
        assert _to_slack_markdown("### Sub") == "*Sub*"

    def test_bold(self) -> None:
        assert _to_slack_markdown("**bold**") == "*bold*"

    def test_links(self) -> None:
        assert _to_slack_markdown("[click](http://x.com)") == "<http://x.com|click>"

    def test_combined(self) -> None:
        result = _to_slack_markdown("# Hello\n**bold** and [link](http://a.com)")
        assert "*Hello*" in result
        assert "*bold*" in result
        assert "<http://a.com|link>" in result

    def test_passthrough_when_no_patterns(self) -> None:
        assert _to_slack_markdown("plain text") == "plain text"


class TestThinkingBlocks:
    """Tests for the thinking indicator blocks."""

    def test_thinking_blocks_structure(self) -> None:
        blocks = thinking_blocks()
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert "Thinking" in blocks[0]["text"]["text"]
        assert ":hourglass_flowing_sand:" in blocks[0]["text"]["text"]


class TestApprovalPromptDetection:
    """``_is_approval_prompt`` identifies approval responses by action_id."""

    def test_detects_approve_button(self) -> None:
        resp = RichResponse(
            channel_id="C1",
            thread_ts="t1",
            blocks=[
                TextBlock(text="approval please"),
                ActionBlock(
                    actions=[
                        ActionButton(
                            label="Approve",
                            action_id=APPROVAL_ACTION_APPROVE,
                            value="t1",
                        ),
                        ActionButton(
                            label="Deny",
                            action_id=APPROVAL_ACTION_DENY,
                            value="t1",
                        ),
                    ]
                ),
            ],
        )
        assert SlackConnector._is_approval_prompt(resp) is True

    def test_rejects_normal_response(self) -> None:
        resp = RichResponse(channel_id="C1", blocks=[TextBlock(text="hi")])
        assert SlackConnector._is_approval_prompt(resp) is False

    def test_rejects_response_with_non_approval_buttons(self) -> None:
        resp = RichResponse(
            channel_id="C1",
            blocks=[
                ActionBlock(
                    actions=[
                        ActionButton(label="Retry", action_id="retry_btn", value="x"),
                    ]
                ),
            ],
        )
        assert SlackConnector._is_approval_prompt(resp) is False


class TestApprovalClickOutcome:
    """``_approval_click_outcome`` maps click action_ids to consumed labels."""

    def _make(self, action_id: str | None) -> NormalizedRequest:
        action = ActionPayload(action_id=action_id, value="") if action_id else None
        return NormalizedRequest(
            text="",
            user_id="U",
            channel_id="C1",
            timestamp=0.0,
            source="slack",
            action=action,
        )

    def test_approve_click(self) -> None:
        req = self._make(APPROVAL_ACTION_APPROVE)
        assert SlackConnector._approval_click_outcome(req) == "Approved — executing"

    def test_deny_click(self) -> None:
        req = self._make(APPROVAL_ACTION_DENY)
        assert SlackConnector._approval_click_outcome(req) == "Denied"

    def test_non_approval_click(self) -> None:
        req = self._make("form_submit")
        assert SlackConnector._approval_click_outcome(req) == ""

    def test_text_message_not_a_click(self) -> None:
        # No action → outcome is empty; the connector treats this as a
        # regular message, not an approval lifecycle event.
        req = self._make(None)
        assert SlackConnector._approval_click_outcome(req) == ""


class TestIsApprovalClick:
    """``_is_approval_click`` gates the upfront dedup + button-strip."""

    def _make(self, action_id: str | None) -> NormalizedRequest:
        action = ActionPayload(action_id=action_id, value="") if action_id else None
        return NormalizedRequest(
            text="",
            user_id="U",
            channel_id="C1",
            timestamp=0.0,
            source="slack",
            action=action,
        )

    def test_approve_button(self) -> None:
        assert SlackConnector._is_approval_click(self._make(APPROVAL_ACTION_APPROVE))

    def test_deny_button(self) -> None:
        assert SlackConnector._is_approval_click(self._make(APPROVAL_ACTION_DENY))

    def test_unrelated_action_id(self) -> None:
        # A form submit, a retry button, etc. must not trigger the
        # approval-click preprocessing — otherwise we'd rewrite
        # unrelated messages into consumed-approval state.
        assert not SlackConnector._is_approval_click(self._make("submit_colour"))

    def test_plain_message(self) -> None:
        assert not SlackConnector._is_approval_click(self._make(None))


# ── ``_handle_with_thinking`` approval-click dedup + early strip ─────


class _FakeSlackClient:
    """Async stub recording every Slack-API call in order.

    Deliberately tiny — only the four methods
    ``_handle_with_thinking`` + its helpers actually invoke
    (``chat_postMessage`` / ``chat_update`` / ``chat_delete``) are
    implemented.  Each call appends ``(method, kwargs)`` to
    :attr:`calls` so tests can assert both ordering and content.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_ts = 1_000

    async def chat_postMessage(  # noqa: N802 — matches Slack SDK method name
        self, **kwargs: object
    ) -> dict:
        self.calls.append(("chat_postMessage", kwargs))
        self._next_ts += 1
        return {"ts": f"ts_{self._next_ts}"}

    async def chat_update(self, **kwargs: object) -> dict:
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    async def chat_delete(self, **kwargs: object) -> dict:
        self.calls.append(("chat_delete", kwargs))
        return {"ok": True}


def _bare_connector(handler) -> SlackConnector:
    """Build a ``SlackConnector`` without the Bolt / socket-mode plumbing.

    Uses ``__new__`` to avoid the real ``__init__`` (which would
    create an ``AsyncApp`` and open network listeners).  Only the
    handful of attributes that ``_handle_with_thinking`` reads are
    populated.
    """
    conn = SlackConnector.__new__(SlackConnector)
    conn._bot_user_id = "B999"
    conn._handler = handler  # type: ignore[assignment]
    conn._thread_approval_msg = {}
    conn._approval_in_flight = set()
    conn._error_timestamps = defaultdict(list)
    return conn


def _approval_click_request(
    action_id: str = APPROVAL_ACTION_APPROVE,
    *,
    thread_ts: str = "thread_abc",
    clicked_msg_ts: str = "msg_prompt_1",
) -> NormalizedRequest:
    """Build a normalised approval-click request.

    ``raw_event.message.ts`` is what ``_update_clicked_approval``
    reads to target the ``chat_update`` — tests must set it to
    observe the upfront consumed-state rewrite.
    """
    return NormalizedRequest(
        text="",
        user_id="U1",
        channel_id="C1",
        thread_ts=thread_ts,
        timestamp=0.0,
        source="slack",
        action=ActionPayload(action_id=action_id, value=""),
        raw_event={"message": {"ts": clicked_msg_ts}, "channel": {"id": "C1"}},
    )


class TestHandleWithThinkingApprovalDedup:
    """Regression tests for the double-click race in the approval flow.

    Motivating user report: clicking Approve again while a ``git_clone``
    was still running produced a spurious "No pending write operation
    found for this thread." post (from the old orchestrator behaviour)
    and, even after that wire was suppressed, the user-visible buttons
    remained clickable and generated needless second orchestrator
    round-trips.  The fix in ``_handle_with_thinking`` is twofold:
    strip buttons upfront and dedup in-flight clicks.
    """

    @pytest.mark.asyncio
    async def test_duplicate_click_in_flight_is_dropped(self) -> None:
        # Two concurrent Approve clicks for the same thread.  The first
        # enters normally; the second must return immediately without
        # posting a thinking placeholder or calling the handler.
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()
        handler_calls = 0

        async def handler(
            req: NormalizedRequest,
            cb: object,  # status callback, unused here
        ) -> RichResponse:
            nonlocal handler_calls
            handler_calls += 1
            handler_started.set()
            await release_handler.wait()
            return RichResponse(channel_id=req.channel_id, blocks=[TextBlock(text="done")])

        conn = _bare_connector(handler)
        client = _FakeSlackClient()

        req1 = _approval_click_request()
        req2 = _approval_click_request()

        # Kick off the first click; wait until the handler is running
        # and the thread is marked in-flight.
        task1 = asyncio.create_task(conn._handle_with_thinking(client, req1))
        await handler_started.wait()
        assert "thread_abc" in conn._approval_in_flight

        # Second click arrives mid-flight.  Must short-circuit.
        await conn._handle_with_thinking(client, req2)

        assert handler_calls == 1
        # No additional Slack calls beyond what the first click made —
        # specifically, no second thinking placeholder posted.
        post_calls_before_release = [c for c in client.calls if c[0] == "chat_postMessage"]
        # The first click posts exactly one "Thinking…" placeholder
        # before the handler completes.
        assert len(post_calls_before_release) == 1

        # Let the first click finish so the test cleans up.
        release_handler.set()
        await task1

        # In-flight slot is released once the first click returns.
        assert "thread_abc" not in conn._approval_in_flight

    @pytest.mark.asyncio
    async def test_buttons_stripped_before_handler_runs(self) -> None:
        # The ``chat_update`` that rewrites the clicked message to
        # "Approved — executing" must fire *before* the handler — so a
        # user hammering the button sees it disappear instantly
        # instead of staying clickable for the whole clone duration.
        observed_update_before_handler = False

        async def handler(
            req: NormalizedRequest,
            cb: object,
        ) -> RichResponse:
            nonlocal observed_update_before_handler
            has_consumed = any(
                method == "chat_update"
                and kwargs.get("ts") == "msg_prompt_1"
                and "Approved" in kwargs.get("text", "")
                for method, kwargs in client.calls
            )
            observed_update_before_handler = has_consumed
            return RichResponse(channel_id=req.channel_id, blocks=[TextBlock(text="done")])

        conn = _bare_connector(handler)
        client = _FakeSlackClient()
        await conn._handle_with_thinking(client, _approval_click_request())

        assert observed_update_before_handler, (
            "clicked approval message should be rewritten to its consumed "
            "state before the orchestrator handler runs"
        )

    @pytest.mark.asyncio
    async def test_handler_exception_clears_in_flight_flag(self) -> None:
        # Regression guard: a crash in the handler must not wedge the
        # thread forever.  The ``finally`` in ``_handle_with_thinking``
        # is meant to release the in-flight slot on every exit path.
        async def handler(*_: object) -> RichResponse:
            raise RuntimeError("backend exploded")

        conn = _bare_connector(handler)
        client = _FakeSlackClient()

        with pytest.raises(RuntimeError):
            await conn._handle_with_thinking(client, _approval_click_request())

        assert "thread_abc" not in conn._approval_in_flight

    @pytest.mark.asyncio
    async def test_non_approval_request_bypasses_dedup(self) -> None:
        # A regular message (no ``action`` payload) is not an approval
        # click; it must never touch the in-flight set, and the
        # clicked-message rewrite must not fire.
        handler_calls = 0

        async def handler(*_: object) -> RichResponse:
            nonlocal handler_calls
            handler_calls += 1
            return RichResponse(channel_id="C1", blocks=[TextBlock(text="ok")])

        conn = _bare_connector(handler)
        client = _FakeSlackClient()

        plain_req = NormalizedRequest(
            text="hi",
            user_id="U1",
            channel_id="C1",
            thread_ts="thread_xyz",
            timestamp=0.0,
            source="slack",
        )
        await conn._handle_with_thinking(client, plain_req)

        assert handler_calls == 1
        assert conn._approval_in_flight == set()
        # No chat_update that targeted a specific "clicked" message ts
        # — only the thinking-placeholder → final-response swap.
        consume_updates = [
            c
            for c in client.calls
            if c[0] == "chat_update" and "Approved" in str(c[1].get("text", ""))
        ]
        assert consume_updates == []
