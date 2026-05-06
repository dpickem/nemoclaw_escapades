"""Slack-side rendering for finalisation results.

Two surfaces live here:

- **Rendering helpers** — ``build_present_work_response`` returns a
  platform-neutral :class:`RichResponse` carrying the synthesised
  summary, an optional diff preview, and the three action buttons
  (Push & PR, Iterate, Discard).  The ``SlackConnector`` renders
  this through its existing block-builder pipeline (which splits
  oversized ``TextBlock``s automatically).
- **WorkflowRenderer implementation** — :class:`SlackFinalizationRenderer`
  pushes finalisation outputs back to the originating thread by
  posting new Slack messages directly through the Bolt
  ``AsyncWebClient``.  This is the connector's "outbound-from-the-
  orchestrator" channel that the design (§7.1, §8.2) calls for.
  Because the renderer constructs raw Block Kit JSON (it bypasses
  the connector's regular ``RichResponse`` pipeline), it splits
  long bodies itself using
  :func:`connectors.slack.connector._split_text_for_slack` and
  caps blocks at :data:`_SLACK_MAX_TEXTBLOCK_CHUNKS` so the actions
  block always fits under Slack's 50-block message limit.

Action-click → finalisation-tool routing is platform-neutral and
lives on the orchestrator side
(:mod:`nemoclaw_escapades.orchestrator.finalization_actions`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nemoclaw_escapades.connectors.slack.connector import (
    _SLACK_MAX_TEXTBLOCK_CHUNKS,
    _SLACK_SECTION_TEXT_LIMIT,
    _split_text_for_slack,
)
from nemoclaw_escapades.models.types import (
    FINALIZATION_ACTION_DISCARD,
    FINALIZATION_ACTION_ITERATE,
    FINALIZATION_ACTION_PUSH_PR,
    ActionBlock,
    ActionButton,
    RichResponse,
    TextBlock,
)
from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.nmb.protocol import (
        TaskCompletePayload,
        TaskErrorPayload,
        TaskProgressPayload,
    )
    from nemoclaw_escapades.orchestrator.workflow import WorkflowContext

logger = get_logger("connectors.slack.finalization")

# Headroom inside each section's text so the surrounding code-fence
# delimiters (``\`\`\`<lang>\\n`` opener + ``\\n\`\`\``` closer, ≈ 12
# chars) stay within :data:`_SLACK_SECTION_TEXT_LIMIT`.  Sized
# generously to account for the longest ``language`` tag we use
# (``"diff"``).
_FENCE_OVERHEAD: int = 16

# UI-readability cap on the diff body inlined into
# :func:`build_present_work_response`.  The connector's render
# pipeline splits the resulting ``TextBlock`` per
# :data:`_SLACK_SECTION_TEXT_LIMIT`, so this is a soft cap on how
# much diff context we surface in chat — not a hard Slack
# constraint.  Roughly four full section blocks worth, enough for a
# typical PR review preview.
_BUILD_PREVIEW_LIMIT: int = _SLACK_SECTION_TEXT_LIMIT * 4


def build_present_work_response(
    *,
    channel_id: str,
    thread_ts: str | None,
    workflow_id: str,
    summary: str,
    diff: str = "",
) -> RichResponse:
    """Build the Slack-style response carrying the finalisation buttons.

    Used by tests and by any caller that wants a
    :class:`RichResponse` shape rather than a direct
    ``chat_postMessage`` push.  The returned response goes through
    the connector's regular render pipeline, which auto-splits
    oversized ``TextBlock``s — so the diff truncation here is a
    soft UI cap, not a hard Slack constraint.

    Args:
        channel_id: Slack channel the user originally posted to.
        thread_ts: Thread parent timestamp.
        workflow_id: Stamped on every button's ``value`` field so
            the click handler can route back to the right workflow.
        summary: Synthesised user-facing text.
        diff: Optional pre-truncated diff body.

    Returns:
        A :class:`RichResponse` ready for ``SlackConnector.render``.
    """
    text = f"*Sub-agent work ready for review*\n\n{summary}"
    if diff:
        text += f"\n\n```diff\n{diff[:_BUILD_PREVIEW_LIMIT]}\n```"
    return RichResponse(
        channel_id=channel_id,
        thread_ts=thread_ts,
        blocks=[
            TextBlock(text=text),
            ActionBlock(
                actions=[
                    ActionButton(
                        label="Push & PR",
                        action_id=FINALIZATION_ACTION_PUSH_PR,
                        value=workflow_id,
                        style="primary",
                    ),
                    ActionButton(
                        label="Iterate",
                        action_id=FINALIZATION_ACTION_ITERATE,
                        value=workflow_id,
                    ),
                    ActionButton(
                        label="Discard",
                        action_id=FINALIZATION_ACTION_DISCARD,
                        value=workflow_id,
                        style="danger",
                    ),
                ]
            ),
        ],
    )


class SlackFinalizationRenderer:
    """:class:`WorkflowRenderer` implementation for Slack.

    Constructed in ``main.py`` from the connector's already-
    authenticated ``AsyncWebClient``.  All methods are no-ops when
    the workflow context lacks a ``channel_id`` (CLI / headless
    workflows); errors are caught and logged so the dispatcher
    never sees a renderer raise.
    """

    def __init__(self, client: Any) -> None:
        """Store the Bolt ``AsyncWebClient`` to ``chat_postMessage`` through."""
        self._client = client

    async def render_present_work(
        self,
        *,
        context: WorkflowContext,
        summary: str,
        diff: str,
    ) -> None:
        """Post the finalisation result with the three action buttons."""
        if context.channel_id is None:
            return
        header = f":robot_face: *Sub-agent work ready for review*\n\n{summary}"
        blocks = _text_section_blocks(header)
        if diff:
            blocks.extend(_code_fence_section_blocks(diff, language="diff"))
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(
                        "Push & PR",
                        FINALIZATION_ACTION_PUSH_PR,
                        context.workflow_id,
                        "primary",
                    ),
                    _button(
                        "Iterate",
                        FINALIZATION_ACTION_ITERATE,
                        context.workflow_id,
                        None,
                    ),
                    _button(
                        "Discard",
                        FINALIZATION_ACTION_DISCARD,
                        context.workflow_id,
                        "danger",
                    ),
                ],
            }
        )
        await self._post(
            channel=context.channel_id,
            thread_ts=context.thread_ts,
            text=summary,
            blocks=blocks,
        )

    async def render_finalization_action(
        self,
        *,
        context: WorkflowContext,
        action: str,
        result: str,
    ) -> None:
        """Post the outcome of a finalisation tool back to the thread."""
        if context.channel_id is None:
            return
        icon = _ACTION_ICONS.get(action, ":information_source:")
        blocks = _text_section_blocks(f"{icon} *{action}*")
        blocks.extend(_code_fence_section_blocks(result))
        await self._post(
            channel=context.channel_id,
            thread_ts=context.thread_ts,
            text=f"{action}: {result[:200]}",
            blocks=blocks,
        )

    async def render_workflow_progress(
        self,
        *,
        context: WorkflowContext,
        progress: TaskProgressPayload,
    ) -> None:
        """Best-effort thinking indicator update.

        Phase 3b ships a minimal text-only progress line.  The richer
        thinking-indicator UI lives on the connector's per-request
        ``StatusCallback`` flow and is upgraded in Phase 6.
        """
        if context.channel_id is None or progress.note is None:
            return
        await self._post(
            channel=context.channel_id,
            thread_ts=context.thread_ts,
            text=f":hourglass_flowing_sand: {progress.note}",
            blocks=None,
        )

    async def render_workflow_error(
        self,
        *,
        context: WorkflowContext,
        error: TaskErrorPayload,
    ) -> None:
        """Surface a sub-agent ``task.error`` to the originating thread."""
        if context.channel_id is None:
            return
        header = (
            f":warning: *Sub-agent reported an error* "
            f"(`{error.error_kind}`, recoverable={error.recoverable})"
        )
        blocks = _text_section_blocks(header)
        blocks.extend(_code_fence_section_blocks(error.error))
        await self._post(
            channel=context.channel_id,
            thread_ts=context.thread_ts,
            text=error.error,
            blocks=blocks,
        )

    async def render_workflow_completion_failure(
        self,
        *,
        context: WorkflowContext,
        complete: TaskCompletePayload,
        error: str,
    ) -> None:
        """Surface a finalisation-side failure (e.g. baseline drift)."""
        del complete  # carried for renderer parity; not embedded by default
        if context.channel_id is None:
            return
        header = f":x: *Finalisation failed for workflow {context.workflow_id}*"
        footer = (
            "_The sub-agent's diff is on disk; you can inspect "
            "the workspace manually._"
        )
        blocks = _text_section_blocks(header)
        blocks.extend(_code_fence_section_blocks(error))
        blocks.extend(_text_section_blocks(footer))
        await self._post(
            channel=context.channel_id,
            thread_ts=context.thread_ts,
            text=error,
            blocks=blocks,
        )

    async def _post(
        self,
        *,
        channel: str,
        thread_ts: str | None,
        text: str,
        blocks: list[dict[str, Any]] | None,
    ) -> None:
        """Post a message; swallow errors so the dispatcher stays alive."""
        try:
            await self._client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text or "…",
                blocks=blocks,
            )
        except Exception:  # noqa: BLE001 — connector surface is broad
            logger.warning(
                "Slack chat_postMessage failed",
                extra={"channel": channel, "thread_ts": thread_ts},
                exc_info=True,
            )


def _text_section_blocks(text: str) -> list[dict[str, Any]]:
    """Split *text* into Slack-safe mrkdwn section blocks.

    Uses :func:`connectors.slack.connector._split_text_for_slack` so
    the per-section cap matches what the connector's regular
    response pipeline applies.  Output is capped at
    :data:`_SLACK_MAX_TEXTBLOCK_CHUNKS` blocks so a pathological
    body can't push the actions block past Slack's 50-block message
    limit.  Returns an empty list for empty input — the caller
    decides whether to elide or substitute a placeholder.
    """
    if not text:
        return []
    chunks = _split_text_for_slack(text)[:_SLACK_MAX_TEXTBLOCK_CHUNKS]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in chunks
    ]


def _code_fence_section_blocks(
    body: str,
    *,
    language: str = "",
) -> list[dict[str, Any]]:
    """Wrap *body* in code fences split across Slack-safe section blocks.

    Each section is independently fenced so Slack renders each as
    its own monospace block — splitting a single fence across
    sections would leave the first with an unclosed open and the
    second with an orphan close, which Slack would render as raw
    text.  The split limit reserves :data:`_FENCE_OVERHEAD` chars
    inside each chunk so the wrapped section text stays under
    :data:`_SLACK_SECTION_TEXT_LIMIT`.

    Args:
        body: Raw text to fence.
        language: Optional code-fence language (e.g. ``"diff"``).
            Empty ``language`` produces an unstyled fence.

    Returns:
        A possibly-empty list of section blocks.  Empty when *body*
        is empty.  Capped at :data:`_SLACK_MAX_TEXTBLOCK_CHUNKS`.
    """
    if not body:
        return []
    fence_open = f"```{language}" if language else "```"
    fence_close = "```"
    inner_limit = _SLACK_SECTION_TEXT_LIMIT - _FENCE_OVERHEAD
    chunks = _split_text_for_slack(body, inner_limit)[:_SLACK_MAX_TEXTBLOCK_CHUNKS]
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{fence_open}\n{chunk}\n{fence_close}",
            },
        }
        for chunk in chunks
    ]


def _button(label: str, action_id: str, value: str, style: str | None) -> dict[str, Any]:
    """Build a Slack ``actions`` button element."""
    btn: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": label},
        "action_id": action_id,
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


# Cosmetic icons for finalisation-tool result posts.  Falls back to
# an info icon for unknown action names.
_ACTION_ICONS: dict[str, str] = {
    "push_branch": ":arrow_up:",
    "push_and_create_pr": ":arrows_counterclockwise:",
    "discard_work": ":wastebasket:",
    "re_delegate": ":repeat:",
    "destroy_sandbox": ":boom:",
}


__all__ = [
    "SlackFinalizationRenderer",
    "build_present_work_response",
]
