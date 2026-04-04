"""Platform-neutral data types for the NemoClaw agent loop.

All request/response types live here so every component imports from one place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Error categorization
# ---------------------------------------------------------------------------

class ErrorCategory(str, Enum):
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    MODEL_ERROR = "model_error"
    CONNECTOR_ERROR = "connector_error"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Connector layer types
# ---------------------------------------------------------------------------

@dataclass
class ActionPayload:
    action_id: str
    value: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class NormalizedRequest:
    text: str
    user_id: str
    channel_id: str
    timestamp: float
    source: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    thread_ts: str | None = None
    action: ActionPayload | None = None
    raw_event: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Platform-neutral response blocks
# ---------------------------------------------------------------------------

@dataclass
class ResponseBlock:
    """Base type for response blocks. Subclasses represent specific block kinds."""


@dataclass
class TextBlock(ResponseBlock):
    text: str
    style: str = "markdown"  # "plain" or "markdown"


@dataclass
class ActionButton:
    label: str
    action_id: str
    value: str
    style: str | None = None  # "primary", "danger", or None


@dataclass
class ActionBlock(ResponseBlock):
    actions: list[ActionButton] = field(default_factory=list)


@dataclass
class ConfirmBlock(ResponseBlock):
    title: str = ""
    text: str = ""
    confirm_label: str = "Confirm"
    deny_label: str = "Cancel"
    action_id: str = ""


@dataclass
class FormField:
    label: str
    field_id: str
    field_type: str = "text"  # "text", "select", "multiline", etc.
    options: list[str] | None = None
    required: bool = False


@dataclass
class FormBlock(ResponseBlock):
    title: str = ""
    fields: list[FormField] = field(default_factory=list)
    submit_action_id: str = ""


@dataclass
class RichResponse:
    """Platform-neutral response the orchestrator produces."""
    channel_id: str
    thread_ts: str | None = None
    blocks: list[ResponseBlock] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inference types
# ---------------------------------------------------------------------------

@dataclass
class InferenceRequest:
    messages: list[dict[str, str]]
    model: str
    temperature: float = 0.7
    max_tokens: int = 2048


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class InferenceResponse:
    content: str
    model: str
    usage: TokenUsage
    latency_ms: float
    finish_reason: str = "stop"  # "stop", "length", "content_filter", etc.
    raw_response: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Approval types
# ---------------------------------------------------------------------------


@dataclass
class ApprovalResult:
    """Outcome of an approval gate check."""

    approved: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class InferenceError(Exception):
    """Raised when an inference call fails after all retries."""

    def __init__(self, message: str, category: ErrorCategory, raw: object = None):
        super().__init__(message)
        self.category = category
        self.raw = raw
