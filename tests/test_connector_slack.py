"""Tests for the SlackConnector — normalization, rendering, bot filtering,
markdown conversion, and block-builder helpers.
"""

from __future__ import annotations

from nemoclaw_escapades.connectors.slack import (
    SlackConnector,
    _to_slack_markdown,
    thinking_blocks,
)
from nemoclaw_escapades.models.types import (
    ActionBlock,
    ActionButton,
    ConfirmBlock,
    FormBlock,
    FormField,
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
