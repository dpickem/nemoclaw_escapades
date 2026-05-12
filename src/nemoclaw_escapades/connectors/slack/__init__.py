"""Slack connector package.

The connector itself lives in :mod:`connector`; the finalisation
renderer lives in :mod:`finalization`.  Both are re-exported here so
existing call sites (``main.py``, the unit tests) keep their import
paths unchanged.
"""

from nemoclaw_escapades.connectors.slack.connector import (
    _SLACK_FALLBACK_TEXT_LIMIT,
    _SLACK_MAX_TEXTBLOCK_CHUNKS,
    _SLACK_SECTION_TEXT_LIMIT,
    DIVIDER,
    SlackConnector,
    _split_text_for_slack,
    _to_slack_markdown,
    thinking_blocks,
)
from nemoclaw_escapades.connectors.slack.finalization import (
    SlackFinalizationRenderer,
    build_present_work_response,
)

__all__ = [
    "DIVIDER",
    "SlackConnector",
    "SlackFinalizationRenderer",
    "_SLACK_FALLBACK_TEXT_LIMIT",
    "_SLACK_MAX_TEXTBLOCK_CHUNKS",
    "_SLACK_SECTION_TEXT_LIMIT",
    "_split_text_for_slack",
    "_to_slack_markdown",
    "build_present_work_response",
    "thinking_blocks",
]
