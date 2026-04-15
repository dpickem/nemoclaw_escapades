"""Transcript repair — defensive model output handling.

Handles malformed model output so the conversation loop never crashes or
leaves the user hanging. M1 scope covers:

- Empty/whitespace-only responses → fallback message
- Truncated responses (finish_reason="length") → retry with continuation prompt
- Malformed content → log and surface user-friendly fallback

M2 will extend with: orphaned tool-call repair, duplicate ID dedup,
synthetic placeholder injection, and malformed tool-input fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nemoclaw_escapades.models.types import InferenceResponse
from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("transcript_repair")

# Sent to the user when the model returns an empty or whitespace-only message.
EMPTY_RESPONSE_FALLBACK = "I wasn't able to generate a response. Could you rephrase?"

# Injected as a follow-up user message when the model's output is truncated
# (finish_reason="length").  Mirrors Claude Code's continuation strategy:
# no apology, no recap — just resume mid-thought.
CONTINUATION_PROMPT = (
    "Resume directly, no apology, no recap. "
    "Pick up mid-thought. Break remaining work into smaller pieces."
)

# How many times the orchestrator will re-call the model with
# CONTINUATION_PROMPT before giving up and returning partial content.
MAX_CONTINUATION_RETRIES = 2


class RepairReason(StrEnum):
    EMPTY_RESPONSE = "empty_response"
    TRUNCATED = "truncated"
    CONTENT_FILTER = "content_filter"
    # M2+: orphaned tool call, duplicate ID, malformed tool input, etc.


@dataclass
class RepairResult:
    """Outcome of transcript repair on a model response."""

    content: str
    was_repaired: bool
    repair_reason: RepairReason | None = None
    needs_continuation: bool = False


def repair_response(response: InferenceResponse, request_id: str = "") -> RepairResult:
    """Inspect a model response and repair it if necessary.

    Returns a RepairResult indicating whether repair was needed and
    the (possibly fixed) content.
    """
    content = response.content

    if not content or not content.strip():
        logger.warning(
            "Empty model response detected",
            extra={
                "request_id": request_id,
                "finish_reason": response.finish_reason,
            },
        )
        return RepairResult(
            content=EMPTY_RESPONSE_FALLBACK,
            was_repaired=True,
            repair_reason=RepairReason.EMPTY_RESPONSE,
        )

    if response.finish_reason == "length":
        logger.warning(
            "Truncated model response detected (finish_reason=length)",
            extra={
                "request_id": request_id,
                "content_length": len(content),
            },
        )
        return RepairResult(
            content=content,
            was_repaired=False,
            needs_continuation=True,
            repair_reason=RepairReason.TRUNCATED,
        )

    if response.finish_reason == "content_filter":
        logger.warning(
            "Content filter triggered",
            extra={"request_id": request_id},
        )
        return RepairResult(
            content="My response was filtered. Could you rephrase your request?",
            was_repaired=True,
            repair_reason=RepairReason.CONTENT_FILTER,
        )

    return RepairResult(content=content, was_repaired=False)
