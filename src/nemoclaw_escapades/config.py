"""Configuration loading and validation from environment variables.

Secrets (API keys, tokens) are injected by OpenShell at sandbox creation
time.  The application code never reads a .env file — it only reads
os.environ.  For local development without OpenShell, the Makefile
exports .env vars into the shell before launching the process
(see ``make run-local-dev``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Defaults (single source of truth — no magic strings elsewhere) ─

SANDBOX_INFERENCE_BASE_URL: str = "https://inference.local/v1"
DEFAULT_INFERENCE_MODEL: str = "azure/anthropic/claude-opus-4-6"  # hyphenated, not dotted
DEFAULT_INFERENCE_TIMEOUT_S: int = 60
DEFAULT_INFERENCE_MAX_RETRIES: int = 3
DEFAULT_SYSTEM_PROMPT_PATH: str = "prompts/system_prompt.md"
DEFAULT_MAX_THREAD_HISTORY: int = 50
DEFAULT_LOG_LEVEL: str = "INFO"
DEFAULT_TEMPERATURE: float = 0.7
DEFAULT_MAX_TOKENS: int = 2048

# ── NMB broker defaults ───────────────────────────────────────────────

DEFAULT_NMB_HOST: str = "0.0.0.0"
DEFAULT_NMB_PORT: int = 9876
DEFAULT_AUDIT_DB_PATH: str = "~/.nemoclaw/audit.db"
# Maximum WebSocket frame size accepted by the broker server.
DEFAULT_NMB_MAX_MESSAGE_SIZE: int = 10 * 1024 * 1024  # 10 MB
# Maximum JSON payload size enforced by NMBMessage.validate().
DEFAULT_NMB_MAX_PAYLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB
DEFAULT_NMB_MAX_PENDING_PER_SANDBOX: int = 100
DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT: float = 300.0  # seconds
DEFAULT_NMB_MAX_CHANNELS_PER_SANDBOX: int = 50

# ── NMB client defaults ──────────────────────────────────────────────

# WebSocket endpoint exposed by the OpenShell proxy
DEFAULT_NMB_URL: str = "ws://messages.local:9876"
# Seconds the client waits for a broker ACK on send/publish/subscribe
DEFAULT_NMB_ACK_TIMEOUT: float = 10.0
# Seconds before the broker gives up sending to a slow subscriber
DEFAULT_NMB_SUBSCRIBER_SEND_TIMEOUT: float = 5.0

# ── NMB client retry defaults (used by tenacity) ─────────────────────

DEFAULT_NMB_CONNECT_MAX_RETRIES: int = 5
DEFAULT_NMB_CONNECT_WAIT_MIN: float = 1.0  # seconds, exponential backoff floor
DEFAULT_NMB_CONNECT_WAIT_MAX: float = 30.0  # seconds, exponential backoff ceiling

# ── NMB queue / buffer limits ────────────────────────────────────────

# Per-client unmatched delivery buffer.  When full the oldest message is
# dropped — matches the "1 000 then drop oldest" design policy.
DEFAULT_NMB_LISTEN_QUEUE_SIZE: int = 1_000
# Per-subscriber channel delivery buffer (same drop-oldest policy).
DEFAULT_NMB_CHANNEL_QUEUE_SIZE: int = 1_000
# Background audit write buffer.  Larger because audit can lag without
# affecting message routing.
DEFAULT_AUDIT_QUEUE_SIZE: int = 10_000
# Maximum items flushed in a single audit batch commit.
DEFAULT_AUDIT_BATCH_SIZE: int = 100

# ── Jira tool defaults ────────────────────────────────────────────────

DEFAULT_JIRA_URL: str = "https://jirasw.nvidia.com"
DEFAULT_JIRA_AUTH_ENV_VAR: str = "JIRA_AUTH"

# ── Misc ─────────────────────────────────────────────────────────────

_TRUTHY_VALUES: frozenset[str] = frozenset({"true", "1", "yes"})

_FALLBACK_SYSTEM_PROMPT: str = (
    "You are NemoClaw, a helpful AI assistant. "
    "Be concise and direct in your responses. "
    "You do not yet have tools or persistent memory."
)


@dataclass
class SlackConfig:
    """Slack Bot and App tokens for the Slack connector.

    Attributes:
        bot_token: ``xoxb-`` Bot User OAuth token.
        app_token: ``xapp-`` App-Level token for Socket Mode.
    """

    bot_token: str = ""
    app_token: str = ""


@dataclass
class InferenceConfig:
    """Connection parameters for the NVIDIA Inference Hub backend.

    Attributes:
        base_url: OpenAI-compatible base URL (e.g. ``https://inference.local/v1``).
        api_key: API key for the inference endpoint.
        model: Model identifier used in chat completion requests.
        timeout_s: HTTP request timeout in seconds.
        max_retries: Maximum retries on transient failures.
    """

    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_INFERENCE_MODEL
    timeout_s: int = DEFAULT_INFERENCE_TIMEOUT_S
    max_retries: int = DEFAULT_INFERENCE_MAX_RETRIES


@dataclass
class OrchestratorConfig:
    """Parameters for the multi-turn orchestrator agent loop.

    Attributes:
        model: Model identifier forwarded to the inference backend.
        system_prompt_path: File path to the system prompt Markdown file.
        max_thread_history: Maximum messages retained per conversation thread.
        temperature: Sampling temperature for chat completions.
        max_tokens: Maximum tokens in each completion response.
    """

    model: str = DEFAULT_INFERENCE_MODEL
    system_prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH
    max_thread_history: int = DEFAULT_MAX_THREAD_HISTORY
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass
class LogConfig:
    """Logging configuration.

    Attributes:
        level: Python log level name (e.g. ``DEBUG``, ``INFO``, ``WARNING``).
        log_file: Optional file path; ``None`` for stderr-only logging.
    """

    level: str = DEFAULT_LOG_LEVEL
    log_file: str | None = None


@dataclass
class BrokerConfig:
    """Runtime configuration for the NMB broker.

    Attributes:
        host: Bind address.
        port: Bind port.
        audit_db_path: Path to the SQLite audit database.
        persist_payloads: Whether to store full payloads in the audit DB.
        max_message_size: Maximum allowed payload size in bytes.
        max_pending_per_sandbox: Maximum in-flight requests per sandbox.
        default_request_timeout: Default timeout for request-reply in seconds.
        max_channels_per_sandbox: Maximum channel subscriptions per sandbox.
    """

    host: str = DEFAULT_NMB_HOST
    port: int = DEFAULT_NMB_PORT
    audit_db_path: str = DEFAULT_AUDIT_DB_PATH
    persist_payloads: bool = True
    max_message_size: int = DEFAULT_NMB_MAX_MESSAGE_SIZE
    max_pending_per_sandbox: int = DEFAULT_NMB_MAX_PENDING_PER_SANDBOX
    default_request_timeout: float = DEFAULT_NMB_DEFAULT_REQUEST_TIMEOUT
    max_channels_per_sandbox: int = DEFAULT_NMB_MAX_CHANNELS_PER_SANDBOX


@dataclass
class JiraConfig:
    """Configuration for the direct Jira REST integration.

    Attributes:
        enabled: Whether Jira tools are registered with the orchestrator.
        url: Jira instance base URL.
        auth_header: Pre-computed ``Authorization`` header value.  In
            the sandbox this is a proxy placeholder (e.g.
            ``openshell:resolve:env:JIRA_AUTH``) that the L7 proxy
            resolves at request time.  Locally it holds the real
            ``Basic <base64>`` value.
    """

    enabled: bool = True
    url: str = DEFAULT_JIRA_URL
    auth_header: str = ""


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all sub-configs.

    Attributes:
        slack: Slack connector credentials.
        inference: Inference Hub connection parameters.
        orchestrator: Agent loop parameters.
        log: Logging settings.
        jira: Jira REST integration settings.
    """

    slack: SlackConfig = field(default_factory=SlackConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    log: LogConfig = field(default_factory=LogConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)


def load_config() -> AppConfig:
    """Load configuration from environment variables.

    In the OpenShell sandbox, secrets are injected by the gateway.
    For local dev, ``make run-local-dev`` sources ``.env`` into the shell.

    Returns:
        Fully populated ``AppConfig`` ready for use by the application.

    Raises:
        ValueError: If required environment variables (``SLACK_BOT_TOKEN``,
            ``SLACK_APP_TOKEN``, ``INFERENCE_HUB_API_KEY``,
            ``INFERENCE_HUB_BASE_URL``) are missing outside sandbox mode.
    """
    in_sandbox = bool(os.environ.get("OPENSHELL_SANDBOX"))

    # In sandbox, the inference.local proxy handles API key injection —
    # the app never sees the real key.  Locally, read from .env.
    api_key = os.environ.get("INFERENCE_HUB_API_KEY", "")
    if not api_key and not in_sandbox:
        api_key = ""  # will trigger the missing-var error below

    missing: list[str] = []
    if not os.environ.get("SLACK_BOT_TOKEN"):
        missing.append("SLACK_BOT_TOKEN")
    if not os.environ.get("SLACK_APP_TOKEN"):
        missing.append("SLACK_APP_TOKEN")
    if not api_key and not in_sandbox:
        missing.append("INFERENCE_HUB_API_KEY")
    if not os.environ.get("INFERENCE_HUB_BASE_URL") and not in_sandbox:
        missing.append("INFERENCE_HUB_BASE_URL")
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in real values."
        )

    # In sandbox, use inference.local (OpenShell's inference proxy handles
    # auth and routing).  Locally, use the URL from .env.
    default_base_url = (
        SANDBOX_INFERENCE_BASE_URL if in_sandbox else os.environ.get("INFERENCE_HUB_BASE_URL", "")
    )
    model = os.environ.get("INFERENCE_MODEL", DEFAULT_INFERENCE_MODEL)

    return AppConfig(
        slack=SlackConfig(
            bot_token=os.environ["SLACK_BOT_TOKEN"],
            app_token=os.environ["SLACK_APP_TOKEN"],
        ),
        inference=InferenceConfig(
            base_url=os.environ.get("INFERENCE_HUB_BASE_URL", default_base_url),
            api_key=api_key,
            model=model,
            timeout_s=int(os.environ.get("INFERENCE_TIMEOUT_S", str(DEFAULT_INFERENCE_TIMEOUT_S))),
            max_retries=int(
                os.environ.get("INFERENCE_MAX_RETRIES", str(DEFAULT_INFERENCE_MAX_RETRIES))
            ),
        ),
        orchestrator=OrchestratorConfig(
            model=model,
            system_prompt_path=os.environ.get("SYSTEM_PROMPT_PATH", DEFAULT_SYSTEM_PROMPT_PATH),
            max_thread_history=int(
                os.environ.get("MAX_THREAD_HISTORY", str(DEFAULT_MAX_THREAD_HISTORY))
            ),
            temperature=float(os.environ.get("TEMPERATURE", str(DEFAULT_TEMPERATURE))),
            max_tokens=int(os.environ.get("MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        ),
        log=LogConfig(
            level=os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
            log_file=os.environ.get("LOG_FILE"),
        ),
        jira=JiraConfig(
            enabled=os.environ.get("JIRA_ENABLED", "true").lower() in _TRUTHY_VALUES,
            url=os.environ.get("JIRA_URL", DEFAULT_JIRA_URL),
            auth_header=os.environ.get(
                os.environ.get("JIRA_AUTH_ENV_VAR", DEFAULT_JIRA_AUTH_ENV_VAR), ""
            ),
        ),
    )


def load_system_prompt(path: str) -> str:
    """Load the system prompt from a file, falling back to a built-in default.

    Args:
        path: File path to a Markdown system prompt.

    Returns:
        The prompt text (stripped of leading/trailing whitespace), or
        ``_FALLBACK_SYSTEM_PROMPT`` if *path* does not exist.
    """
    p = Path(path)
    if p.is_file():
        return p.read_text().strip()

    return _FALLBACK_SYSTEM_PROMPT
