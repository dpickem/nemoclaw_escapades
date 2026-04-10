"""Platform-neutral data types for the NemoClaw agent loop.

All request/response types live here so every component imports from one place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    #: Set when the orchestrator is returning a classified failure to the user
    #: (connectors may use this for rate limiting instead of parsing message text).
    error_category: ErrorCategory | None = None


# ---------------------------------------------------------------------------
# Tool types
# ---------------------------------------------------------------------------


@dataclass
class FunctionDefinition:
    """Schema for a single function exposed to the model.

    Mirrors the ``function`` object inside an OpenAI tool definition.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolDefinition:
    """OpenAI-format tool definition sent in inference requests.

    Wraps a ``FunctionDefinition`` with the ``type`` discriminator
    expected by the chat-completions API.
    """

    function: FunctionDefinition
    type: str = "function"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the dict shape the inference API expects."""
        return {
            "type": self.type,
            "function": {
                "name": self.function.name,
                "description": self.function.description,
                "parameters": self.function.parameters,
            },
        }


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: str  # JSON-encoded argument string from the model


@dataclass
class ToolResult:
    """Result of executing a tool call, fed back to the model."""

    tool_call_id: str
    content: str
    is_error: bool = False


# ---------------------------------------------------------------------------
# Inference types
# ---------------------------------------------------------------------------

# Messages may contain tool_calls (assistant role) or tool_call_id (tool role),
# so the value type must be Any rather than str.
Message = dict[str, Any]


@dataclass
class InferenceRequest:
    messages: list[Message]
    model: str
    temperature: float = 0.7
    max_tokens: int = 2048
    request_id: str = ""
    tools: list[ToolDefinition] | None = None


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
    finish_reason: str = "stop"  # "stop", "length", "content_filter", "tool_calls"
    tool_calls: list[ToolCall] | None = None
    raw_response: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Approval types
# ---------------------------------------------------------------------------


@dataclass
class ApprovalResult:
    """Outcome of an approval gate check."""

    approved: bool
    reason: str | None = None


@dataclass
class PendingApproval:
    """State saved when write tool calls are blocked pending user approval.

    Captures the full agent-loop context so execution can resume after
    the user approves (or be discarded on denial).
    """

    tool_calls: list[ToolCall]
    working_messages: list[Message]
    assistant_message: Message
    request_id: str
    description: str
    original_user_text: str = ""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InferenceError(Exception):
    """Raised when an inference call fails after all retries."""

    def __init__(self, message: str, category: ErrorCategory, raw: object = None):
        super().__init__(message)
        self.category = category
        self.raw = raw
