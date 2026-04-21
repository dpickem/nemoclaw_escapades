"""Configuration loading and validation.

Three sources feed the final :class:`AppConfig`, lowest to highest
precedence (see ``docs/design_m2b.md`` §5.3):

1. **Dataclass field defaults** — hardcoded below.  These reflect
   local-dev sensible defaults (e.g. ``~/.nemoclaw/workspace``).
2. **YAML overlay** — ``/app/config.yaml`` inside the sandbox, a merged
   file produced at build time by ``scripts/gen_config.py`` from
   ``config/defaults.yaml`` plus category-B values from ``.env``.
   Missing file is not an error — local dev simply gets dataclass
   defaults.  ``NEMOCLAW_CONFIG_PATH`` overrides the default path.
3. **Environment variables** — override individual fields at runtime.
   This is the escape hatch for local-dev knob-twiddling
   (``LOG_LEVEL=DEBUG make run-local-dev``) and for the L7 proxy's
   secret placeholders (``SLACK_BOT_TOKEN``, ``JIRA_AUTH``, etc.).

Secrets (API keys, tokens, usernames, passwords) are **never** written
to the YAML — they flow through env vars only.  In the sandbox the env
values are OpenShell-provider-injected placeholders that the L7 proxy
resolves at HTTP-request time.  For local dev, ``make run-local-dev``
exports ``.env`` into the shell before launching the process.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from nemoclaw_escapades.observability.logging import get_logger

if TYPE_CHECKING:
    from nemoclaw_escapades.runtime import RuntimeEnvironment

logger = get_logger("config")

# ── Defaults (single source of truth — no magic strings elsewhere) ─

# Base URL the sandbox points at for inference.  Kept here (not in the
# YAML) because the proxy routes every request to ``inference.local``
# regardless of the deployment and the value is not an operator knob.
SANDBOX_INFERENCE_BASE_URL: str = "https://inference.local/v1"
DEFAULT_INFERENCE_MODEL: str = "azure/anthropic/claude-opus-4-6"
DEFAULT_INFERENCE_TIMEOUT_S: int = 60
DEFAULT_INFERENCE_MAX_RETRIES: int = 3
DEFAULT_SYSTEM_PROMPT_PATH: str = "prompts/system_prompt.md"
DEFAULT_MAX_THREAD_HISTORY: int = 50
DEFAULT_LOG_LEVEL: str = "INFO"
DEFAULT_TEMPERATURE: float = 0.7
DEFAULT_MAX_TOKENS: int = 2048

# ── Agent loop defaults ──────────────────────────────────────────────

# Safety limit: max inference calls per AgentLoop.run() before returning
# a partial answer.  Prevents infinite tool-call spirals.
DEFAULT_MAX_TOOL_ROUNDS: int = 10
# How many times to re-prompt when finish_reason="length" truncates output.
DEFAULT_MAX_CONTINUATION_RETRIES: int = 2

# ── Context compaction defaults ───────────────────────────────────────

# Micro-compaction: tool results exceeding this char count are truncated
# in-place before inference (no API call, zero cost).
DEFAULT_MICRO_COMPACTION_CHARS: int = 10_000
# Full compaction triggers when total message chars exceed this threshold.
# Approximates ~80% of a 128K-token context window at ~4 chars/token.
DEFAULT_COMPACTION_THRESHOLD_CHARS: int = 400_000
# Fraction of oldest messages to summarize during full compaction.
DEFAULT_COMPACTION_COMPRESS_RATIO: float = 0.5
# Minimum number of messages to keep verbatim (most recent) after compaction.
DEFAULT_COMPACTION_MIN_KEEP: int = 4
# Model used for the compaction summary call (same as main model by default).
DEFAULT_COMPACTION_MODEL: str = ""


@dataclass
class AgentLoopConfig:
    """Configuration for a single ``AgentLoop`` instance.

    Attributes:
        model: Model identifier forwarded to the inference backend.
        temperature: Sampling temperature for chat completions.
        max_tokens: Maximum tokens per completion response.
        max_tool_rounds: Safety limit — maximum inference calls per
            ``run()`` invocation before returning a partial answer.
        max_continuation_retries: How many times to re-prompt the model
            when ``finish_reason="length"`` truncates the output.
        micro_compaction_chars: Tool results exceeding this char count
            are truncated in-place before inference (zero-cost).
        compaction_threshold_chars: Total message chars that trigger
            full compaction (LLM summary + session roll).
        compaction_compress_ratio: Fraction of oldest messages to
            summarize during full compaction.
        compaction_min_keep: Minimum messages to keep verbatim after
            full compaction (always the most recent).
        compaction_model: Model for the summary call.  Empty string
            means use the same model as ``model``.
    """

    model: str = DEFAULT_INFERENCE_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    max_continuation_retries: int = DEFAULT_MAX_CONTINUATION_RETRIES
    micro_compaction_chars: int = DEFAULT_MICRO_COMPACTION_CHARS
    compaction_threshold_chars: int = DEFAULT_COMPACTION_THRESHOLD_CHARS
    compaction_compress_ratio: float = DEFAULT_COMPACTION_COMPRESS_RATIO
    compaction_min_keep: int = DEFAULT_COMPACTION_MIN_KEEP
    compaction_model: str = DEFAULT_COMPACTION_MODEL


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
# Commits between WAL checkpoints.  Keeps the main .db file fresh so
# single-file copies (openshell sandbox download) contain all data.
DEFAULT_AUDIT_CHECKPOINT_INTERVAL: int = 10

# ── Jira tool defaults ────────────────────────────────────────────────

DEFAULT_JIRA_URL: str = "https://jirasw.nvidia.com"
DEFAULT_JIRA_AUTH_ENV_VAR: str = "JIRA_AUTH"

# ── GitLab / Gerrit tool defaults ─────────────────────────────────────
#
# Left deliberately empty in the public source — these URLs live in
# ``.env`` and are merged into ``config/orchestrator.resolved.yaml`` by
# ``scripts/gen_config.py``.  See ``docs/design_m2b.md`` §5.3.2 for the
# rationale ("category B — private non-secret").

DEFAULT_GITLAB_URL: str = ""
DEFAULT_GERRIT_URL: str = ""

# ── Confluence tool defaults ─────────────────────────────────────────

DEFAULT_CONFLUENCE_URL: str = "https://nvidia.atlassian.net/wiki"

# ── Slack search tool defaults ───────────────────────────────────────
# (user-token based search/history — separate from the bot connector)

# ── Web search tool defaults ────────────────────────────────────────

DEFAULT_WEB_SEARCH_API: str = "brave"
DEFAULT_WEB_SEARCH_LIMIT: int = 5

# ── Coding agent defaults ────────────────────────────────────────────

# Local-development default for the coding-agent workspace.  The
# sandbox overrides this via ``/app/config.yaml`` (``coding.workspace_root:
# /sandbox/workspace``), which is produced at build time by the
# ``gen_config.py`` resolver.
DEFAULT_CODING_WORKSPACE_ROOT: str = "~/.nemoclaw/workspace"

# ``git_clone`` host allowlist.  Empty = fail-closed: the tool refuses
# to clone anything until an operator supplies hosts via
# ``GIT_CLONE_ALLOWED_HOSTS`` in ``.env`` (then picked up by the
# resolver into the sandbox YAML) or directly in the runtime
# environment for local dev.
DEFAULT_GIT_CLONE_ALLOWED_HOSTS: str = ""

# ── Skills defaults ──────────────────────────────────────────────────

# Local-development default for the ``SkillLoader`` scan root.  In the
# sandbox the Dockerfile copies ``skills/`` into ``/app/skills`` and
# the YAML overlay selects that path via ``skills.skills_dir``.
DEFAULT_SKILLS_DIR: str = "skills"

# ── YAML overlay ─────────────────────────────────────────────────────

# Path of the resolved config file inside the sandbox image.  Set by
# ``scripts/gen_config.py`` + Dockerfile ``COPY``.  Missing file is
# not an error — the loader falls back to dataclass defaults, which
# is the correct behaviour for local-dev (no YAML shipped).
_DEFAULT_YAML_PATH: Path = Path("/app/config.yaml")

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
class GitLabConfig:
    """Configuration for the GitLab REST API integration.

    Uses ``Authorization: Bearer <PAT>`` — the token placeholder is
    placed directly in the header so the OpenShell proxy can resolve it
    (same pattern as Slack user-token auth).

    Attributes:
        enabled: Whether GitLab tools are registered with the orchestrator.
        url: GitLab instance base URL.  Empty by default because the
            specific URL is a category-B value stored in ``.env`` and
            merged into ``/app/config.yaml`` by ``gen_config.py``.
        token: Personal Access Token (``glpat-...``).
    """

    enabled: bool = True
    url: str = DEFAULT_GITLAB_URL
    token: str = ""


@dataclass
class GerritConfig:
    """Configuration for the Gerrit REST API integration.

    Uses HTTP Basic auth with separate username/password credentials
    to match the OpenShell proxy's credential resolution model.

    Attributes:
        enabled: Whether Gerrit tools are registered with the orchestrator.
        url: Gerrit instance base URL (including ``/a`` if needed).
            Empty by default — category-B value, merged in at build time.
        username: HTTP Basic auth username.
        http_password: HTTP Basic auth password.
    """

    enabled: bool = True
    url: str = DEFAULT_GERRIT_URL
    username: str = ""
    http_password: str = ""


@dataclass
class ConfluenceConfig:
    """Configuration for the Confluence REST API integration.

    Attributes:
        enabled: Whether Confluence tools are registered.
        url: Confluence instance base URL.
        username: Atlassian account email / username.
        api_token: Atlassian API token (used as password in Basic auth).
    """

    enabled: bool = True
    url: str = DEFAULT_CONFLUENCE_URL
    username: str = ""
    api_token: str = ""


@dataclass
class SlackSearchConfig:
    """Configuration for the Slack user-token search/history tools.

    These use a *user* OAuth token (``xoxp-...``) — separate from the
    *bot* token used by the Slack connector for messaging.

    Attributes:
        enabled: Whether Slack search tools are registered.
        user_token: Slack user OAuth token (``xoxp-...``).
    """

    enabled: bool = True
    user_token: str = ""


@dataclass
class WebSearchConfig:
    """Web search and URL fetch settings for the orchestrator.

    Uses the Brave Search API for ``web_search`` and the Jina Reader
    API for ``web_fetch``.  Set ``BRAVE_SEARCH_API_KEY`` to enable
    search.

    ``web_fetch`` uses Jina Reader's **free tier** by default — no API
    key required, rate-limited to 20 RPM.  This is sufficient for
    typical agent workloads.  To raise the limit to 500 RPM, obtain a
    free Jina API key (comes with 10M tokens) at https://jina.ai/reader/
    and set ``JINA_API_KEY``.

    Attributes:
        enabled: Whether web search tools are registered.
        api_key: Brave Search API key.
        jina_api_key: Jina Reader API key.  Optional — the free tier
            works without one.  Set for higher rate limits.
        default_limit: Default number of search results to return.
    """

    enabled: bool = True
    api_key: str = ""
    jina_api_key: str = ""
    default_limit: int = DEFAULT_WEB_SEARCH_LIMIT


@dataclass
class CodingAgentConfig:
    """Configuration for the coding-agent tool suite.

    When ``enabled`` is true, ``main`` registers the workspace-rooted
    file, search, bash, and git tools into the orchestrator's
    ``ToolRegistry``.  Any working-memory / scratchpad needs are
    handled by the agent itself via ordinary file tools (see the
    ``scratchpad`` skill for the convention).

    Attributes:
        enabled: Whether the coding tools are wired in.  Defaults to
            ``True`` — the coding tools are the orchestrator's core
            capability.  File writes are still gated by the
            write-approval flow, and ``git_clone`` stays fail-closed
            via an empty ``git_clone_allowed_hosts`` allowlist.  Set
            ``CODING_AGENT_ENABLED=false`` to opt out.
        workspace_root: Absolute path to the directory that file/search/
            bash/git tools operate on.  Path traversal outside this
            directory is rejected by the file tools.  Sandbox override
            (``/sandbox/workspace``) comes from the YAML overlay at
            startup; the dataclass default matches local-dev.
        git_clone_allowed_hosts: Comma-separated list of host names that
            ``git_clone`` will accept.  Empty disables ``git_clone``
            entirely (fail-closed for security).  Populated in the
            sandbox via the YAML overlay (``gen_config.py`` merges the
            operator's ``GIT_CLONE_ALLOWED_HOSTS`` from ``.env``).
    """

    enabled: bool = True
    workspace_root: str = DEFAULT_CODING_WORKSPACE_ROOT
    git_clone_allowed_hosts: str = DEFAULT_GIT_CLONE_ALLOWED_HOSTS


@dataclass
class SkillsConfig:
    """Configuration for the ``SkillLoader`` and ``skill`` tool.

    When ``enabled`` is true (and ``skills_dir`` contains at least one
    ``SKILL.md`` file), the ``skill`` tool is registered with a dynamic
    enum of discovered skill IDs.

    Attributes:
        enabled: Whether skill loading is wired in.
        skills_dir: Directory tree scanned for ``SKILL.md`` files.
            Sandbox uses ``/app/skills`` via the YAML overlay; the
            dataclass default targets a ``skills/`` directory relative
            to CWD for local-dev runs from the repo root.
    """

    enabled: bool = True
    skills_dir: str = DEFAULT_SKILLS_DIR


@dataclass
class NmbClientConfig:
    """Configuration for the NMB client (sub-agent / orchestrator side).

    Separate from :class:`BrokerConfig` which owns the broker
    *server*-side settings (bind host/port, queue caps, audit DB
    path).  These fields are what a *client* needs to connect:
    where the broker lives and what sandbox identity to announce.

    Attributes:
        broker_url: WebSocket URL the client connects to.  Inside an
            OpenShell sandbox this is normally ``ws://messages.local:9876``
            — the gateway proxies it.  Overridable for local-dev runs
            that stand up their own broker.
        sandbox_id: Identifier this client announces to the broker in
            ``sandbox.ready`` / ``task.complete`` / etc.  Empty string
            means the agent generates an id at startup (one per
            process invocation); set explicitly to pin the identity
            across restarts (useful for cron-style jobs or sub-agents
            whose work survives a restart).
    """

    broker_url: str = DEFAULT_NMB_URL
    sandbox_id: str = ""


@dataclass
class AuditConfig:
    """Configuration for the SQLite audit database.

    The audit DB records every tool invocation (service, args, result,
    latency, approval status) for operational debugging and training-data
    extraction.

    In the sandbox the DB lives on the ``/sandbox`` PVC (persistent across
    gateway restarts with OpenShell >= 0.0.22).  Locally it defaults to
    ``~/.nemoclaw/audit.db``.

    Attributes:
        enabled: Whether audit logging is active.
        db_path: Filesystem path to the SQLite database file.
        persist_payloads: Store full JSON request/response payloads.
            Set to ``False`` to save disk while keeping metadata.
    """

    enabled: bool = True
    db_path: str = DEFAULT_AUDIT_DB_PATH
    persist_payloads: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all sub-configs.

    Attributes:
        slack: Slack connector credentials.
        inference: Inference Hub connection parameters.
        orchestrator: Orchestrator-facing prompt and history settings.
        agent_loop: Reusable ``AgentLoop`` runtime knobs (tool-round
            cap, continuation retries, compaction thresholds).  The
            orchestrator merges ``model`` / ``temperature`` /
            ``max_tokens`` from ``OrchestratorConfig`` when it builds
            its loop; sub-agents use the ``AgentLoopConfig`` values
            directly so they can be tuned independently.
        nmb: NMB client settings — broker URL and sandbox identity.
            Category-A non-secret config that must not be plumbed
            through raw ``os.environ`` inside the sandbox (the whole
            point of §5.3 is that non-secret config flows through the
            YAML overlay, not ad-hoc env vars).
        log: Logging settings.
        audit: Audit database settings.
        jira: Jira REST integration settings.
        gitlab: GitLab REST integration settings.
        gerrit: Gerrit REST integration settings.
        confluence: Confluence REST integration settings.
        slack_search: Slack user-token search/history settings.
        web_search: Web search and URL fetch settings.
        coding: Coding-agent tool suite settings.
        skills: ``SkillLoader`` + ``skill`` tool settings.
    """

    slack: SlackConfig = field(default_factory=SlackConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    agent_loop: AgentLoopConfig = field(default_factory=AgentLoopConfig)
    nmb: NmbClientConfig = field(default_factory=NmbClientConfig)
    log: LogConfig = field(default_factory=LogConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    gerrit: GerritConfig = field(default_factory=GerritConfig)
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)
    slack_search: SlackSearchConfig = field(default_factory=SlackSearchConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    coding: CodingAgentConfig = field(default_factory=CodingAgentConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: RuntimeEnvironment | None = None,
    ) -> AppConfig:
        """Load configuration from dataclass defaults + YAML overlay + env vars.

        The three sources combine in the precedence documented at the
        top of this module:

        1. Start from dataclass defaults (local-dev friendly values).
        2. Apply the YAML overlay at *path*, ``NEMOCLAW_CONFIG_PATH``,
           or :data:`_DEFAULT_YAML_PATH` (first hit wins).  Missing
           file is not an error.
        3. Apply environment-variable overrides per-field.
        4. Validate that required secrets (Slack tokens, inference key
           /URL) are present.

        Args:
            path: Optional YAML path override.  Takes precedence over
                ``NEMOCLAW_CONFIG_PATH`` and :data:`_DEFAULT_YAML_PATH`.
            env: Runtime classification from
                :func:`nemoclaw_escapades.runtime.detect_runtime_environment`.
                Used to gate the two remaining sandbox-vs-local-dev
                branches — inference backfill on missing base URL and
                relaxing the secrets requirement when the proxy
                supplies them.  When ``None``, the loader falls back to
                the same single-signal ``OPENSHELL_SANDBOX`` env-var
                check the legacy code used (preserves backwards compat
                for tests that pre-date the multi-signal detector).
                New call sites should pass the already-detected
                classification so there's exactly one source of truth
                for "am I in a sandbox" per process.

        Returns:
            A fully populated ``AppConfig``.

        Raises:
            ValueError: If required secrets are missing, or if the
                YAML at *path* is malformed.
        """
        in_sandbox = _env_is_sandbox(env)
        config = cls()
        _apply_yaml_overlay(config, path)
        _apply_env_overrides(config, in_sandbox=in_sandbox)
        _check_required_secrets(config, in_sandbox=in_sandbox)
        return config


# ── YAML overlay ────────────────────────────────────────────────────

# YAML top-level sections that map 1:1 to an ``AppConfig`` sub-config
# field of the *same name*.  Tuple (not a dict) because the YAML key
# always equals the attribute name today — a dict-shape mapping
# would be an identity map.  If a future rename breaks the 1:1
# invariant, turn this back into a ``dict[str, str]`` and add the
# non-identity entries there.  ``toolsets`` is handled separately
# because it nests the per-service configs one level deeper in the
# YAML.
_DIRECT_SECTIONS: tuple[str, ...] = (
    "orchestrator",
    "agent_loop",
    "nmb",
    "log",
    "audit",
    "coding",
    "skills",
)

# YAML ``toolsets.<name>`` → ``AppConfig.<name>`` mapping.  The sub-
# configs live at the top level of ``AppConfig`` to keep call sites
# short (``config.jira`` rather than ``config.toolsets.jira``); the
# YAML groups them under ``toolsets`` for readability.  Tuple again
# — the per-service YAML key always equals the attribute name.
_TOOLSET_SECTIONS: tuple[str, ...] = (
    "jira",
    "gitlab",
    "gerrit",
    "confluence",
    "slack_search",
    "web_search",
)

# Top-level YAML keys that the loader recognises but doesn't map to
# an ``AppConfig`` field (yet).  Keeps forward-compat: unknown keys
# log a warning, known-but-unmapped keys stay silent.  Empty for now
# — every section in ``defaults.yaml`` corresponds to a real field.
_RESERVED_YAML_KEYS: frozenset[str] = frozenset()


def _resolve_yaml_path(path: str | Path | None) -> Path:
    """Resolve which YAML path the loader should try.

    Precedence:
        1. Caller-supplied ``path`` argument.
        2. ``NEMOCLAW_CONFIG_PATH`` environment variable.
        3. :data:`_DEFAULT_YAML_PATH` (``/app/config.yaml``).

    Args:
        path: Explicit path override, or ``None`` to consult the env
            and default.

    Returns:
        A ``Path`` object.  The file may or may not exist — the
        caller is responsible for the ``.is_file()`` check.
    """
    if path is not None:
        return Path(path)
    env_path = os.environ.get("NEMOCLAW_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_YAML_PATH


def _apply_yaml_overlay(config: AppConfig, path: str | Path | None) -> None:
    """Apply the YAML overlay to *config*, mutating in place.

    Missing file is not an error — local-dev runs have no YAML and
    simply keep the dataclass defaults.  A malformed YAML *is* an
    error and surfaces as ``ValueError``.

    Args:
        config: ``AppConfig`` instance to mutate.
        path: Optional explicit YAML path.  Falls back to the env /
            default per :func:`_resolve_yaml_path`.

    Raises:
        ValueError: If the YAML file is present but unparseable.
    """
    yaml_path = _resolve_yaml_path(path)
    if not yaml_path.is_file():
        return

    try:
        overlay = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {yaml_path}: {exc}") from exc

    if not isinstance(overlay, dict):
        raise ValueError(
            f"Top-level YAML in {yaml_path} must be a mapping, got {type(overlay).__name__}"
        )

    # Direct top-level sections.
    for name in _DIRECT_SECTIONS:
        section = overlay.get(name)
        if section is not None:
            _apply_section(getattr(config, name), section, name)

    # Grouped toolset sections.
    toolsets = overlay.get("toolsets") or {}
    if not isinstance(toolsets, dict):
        logger.warning(
            "YAML 'toolsets' must be a mapping; ignoring",
            extra={"path": str(yaml_path), "got": type(toolsets).__name__},
        )
    else:
        for name in _TOOLSET_SECTIONS:
            section = toolsets.get(name)
            if section is not None:
                _apply_section(
                    getattr(config, name),
                    section,
                    f"toolsets.{name}",
                )

    # Unknown top-level keys: log, but don't fail.  Forward-compat.
    known_top_level = (
        set(_DIRECT_SECTIONS) | {"toolsets"} | _RESERVED_YAML_KEYS
    )
    for key in overlay:
        if key not in known_top_level:
            logger.warning(
                "Unknown top-level key in YAML overlay",
                extra={"section": key, "path": str(yaml_path)},
            )

    logger.info("Loaded YAML config overlay", extra={"path": str(yaml_path)})


def _apply_section(sub_config: Any, values: dict[str, Any], path_for_log: str) -> None:
    """Apply YAML ``values`` onto a dataclass instance, mutating in place.

    Unknown fields log a warning but don't raise — the YAML may be
    newer than the Python (forward-compat) or older (with fields
    since renamed).

    Args:
        sub_config: Dataclass instance (e.g. ``config.coding``).
        values: Mapping of field name → value as loaded from YAML.
        path_for_log: YAML section path for warning messages.
    """
    if not isinstance(values, dict):
        logger.warning(
            "YAML section must be a mapping; ignoring",
            extra={"section": path_for_log, "got": type(values).__name__},
        )
        return

    known_fields = {f.name for f in dataclasses.fields(sub_config)}
    for key, value in values.items():
        if key in known_fields:
            setattr(sub_config, key, value)
        else:
            logger.warning(
                "Unknown field in YAML overlay section",
                extra={
                    "section": path_for_log,
                    "field": key,
                    "dataclass": type(sub_config).__name__,
                },
            )


# ── Env overrides ────────────────────────────────────────────────────


def _env_is_sandbox(env: RuntimeEnvironment | None) -> bool:
    """Resolve "am I in the sandbox" for loader branching.

    Prefers the caller-supplied :class:`RuntimeEnvironment` (the
    multi-signal detector's result — what ``main.py`` and
    ``agent/__main__.py`` pass in).  Falls back to the legacy single-
    signal ``OPENSHELL_SANDBOX`` env-var check when the caller hasn't
    supplied one, which keeps pre-existing tests that use
    ``monkeypatch.setenv("OPENSHELL_SANDBOX", "1")`` working unchanged.
    """
    if env is not None:
        from nemoclaw_escapades.runtime import RuntimeEnvironment  # noqa: PLC0415
        return env is RuntimeEnvironment.SANDBOX
    return bool(os.environ.get("OPENSHELL_SANDBOX"))


def _apply_env_overrides(config: AppConfig, *, in_sandbox: bool = False) -> None:
    """Apply environment-variable overrides on top of *config*.

    Mutates *config* in place.  For each env var that is non-empty
    (``os.environ.get(...)`` returns a truthy string), the matching
    field on *config* is overwritten.  Env vars trump YAML, matching
    the documented precedence.

    In practice this runs in both local-dev and sandbox modes — inside
    the sandbox most of these env vars are OpenShell-provider-injected
    placeholder strings that the L7 proxy resolves at HTTP-request
    time.  Either way the loader treats the raw string the same.

    The one piece of sandbox-aware branching: ``INFERENCE_HUB_BASE_URL``
    is backfilled with :data:`SANDBOX_INFERENCE_BASE_URL` inside the
    sandbox when neither the env var nor the YAML supplies a value.
    This isn't a category-B leak — ``inference.local`` is the
    proxy-side endpoint the sandbox always talks to, irrespective
    of deployment.

    Args:
        config: ``AppConfig`` instance to mutate.
        in_sandbox: Whether the process is running inside an OpenShell
            sandbox (as resolved by :func:`_env_is_sandbox`).  Used
            only for the inference-URL backfill.
    """

    # ── Slack (required secrets) ───────────────────────────────────
    if bot := os.environ.get("SLACK_BOT_TOKEN"):
        config.slack.bot_token = bot
    if app := os.environ.get("SLACK_APP_TOKEN"):
        config.slack.app_token = app

    # ── Inference ──────────────────────────────────────────────────
    if url := os.environ.get("INFERENCE_HUB_BASE_URL"):
        config.inference.base_url = url
    elif in_sandbox and not config.inference.base_url:
        # Proxy-mediated endpoint inside the sandbox.
        config.inference.base_url = SANDBOX_INFERENCE_BASE_URL
    if key := os.environ.get("INFERENCE_HUB_API_KEY"):
        config.inference.api_key = key
    if model := os.environ.get("INFERENCE_MODEL"):
        config.inference.model = model
        # Orchestrator historically shared ``INFERENCE_MODEL`` — but
        # only as a convenience for the "everything at defaults"
        # path.  If a YAML overlay set ``orchestrator.model`` to a
        # specific value (or a later ``ORCHESTRATOR_MODEL`` env sets
        # it), propagating ``INFERENCE_MODEL`` here would silently
        # clobber that explicit choice.  Only update when the
        # orchestrator is still at the dataclass default.
        if config.orchestrator.model == DEFAULT_INFERENCE_MODEL:
            config.orchestrator.model = model
    if val := os.environ.get("ORCHESTRATOR_MODEL"):
        # Explicit orchestrator-only override.  Wins over
        # ``INFERENCE_MODEL``-induced propagation so operators can
        # run the orchestrator on a different model from the
        # inference backend's default.
        config.orchestrator.model = val
    if val := os.environ.get("INFERENCE_TIMEOUT_S"):
        config.inference.timeout_s = int(val)
    if val := os.environ.get("INFERENCE_MAX_RETRIES"):
        config.inference.max_retries = int(val)

    # ── Orchestrator ───────────────────────────────────────────────
    if val := os.environ.get("SYSTEM_PROMPT_PATH"):
        config.orchestrator.system_prompt_path = val
    if val := os.environ.get("MAX_THREAD_HISTORY"):
        config.orchestrator.max_thread_history = int(val)
    if val := os.environ.get("TEMPERATURE"):
        config.orchestrator.temperature = float(val)
    if val := os.environ.get("MAX_TOKENS"):
        config.orchestrator.max_tokens = int(val)

    # ── NMB client ─────────────────────────────────────────────────
    # Historically ``agent/__main__.py`` read ``NMB_URL`` /
    # ``AGENT_SANDBOX_ID`` directly from the env.  That pattern is
    # exactly what §5.3 retires for non-secret config — the values
    # now flow through ``/app/config.yaml`` (``nmb:`` section) with
    # per-field env overrides preserved for local dev.
    if val := os.environ.get("NMB_URL"):
        config.nmb.broker_url = val
    if val := os.environ.get("AGENT_SANDBOX_ID"):
        config.nmb.sandbox_id = val

    # ── Agent loop ─────────────────────────────────────────────────
    # Namespaced env vars so they don't collide with orchestrator-level
    # knobs of the same name (``MAX_TOKENS`` belongs to the orchestrator
    # prompting config; loop-runtime knobs use the ``AGENT_LOOP_`` prefix).
    if val := os.environ.get("AGENT_LOOP_MAX_TOOL_ROUNDS"):
        config.agent_loop.max_tool_rounds = int(val)
    if val := os.environ.get("AGENT_LOOP_MAX_CONTINUATION_RETRIES"):
        config.agent_loop.max_continuation_retries = int(val)
    if val := os.environ.get("AGENT_LOOP_MICRO_COMPACTION_CHARS"):
        config.agent_loop.micro_compaction_chars = int(val)
    if val := os.environ.get("AGENT_LOOP_COMPACTION_THRESHOLD_CHARS"):
        config.agent_loop.compaction_threshold_chars = int(val)
    if val := os.environ.get("AGENT_LOOP_COMPACTION_COMPRESS_RATIO"):
        config.agent_loop.compaction_compress_ratio = float(val)
    if val := os.environ.get("AGENT_LOOP_COMPACTION_MIN_KEEP"):
        config.agent_loop.compaction_min_keep = int(val)
    if val := os.environ.get("AGENT_LOOP_COMPACTION_MODEL"):
        config.agent_loop.compaction_model = val

    # ── Log ────────────────────────────────────────────────────────
    if val := os.environ.get("LOG_LEVEL"):
        config.log.level = val
    # ``LOG_FILE`` unset → None, as before.  Explicit empty string
    # still means stderr-only.
    if "LOG_FILE" in os.environ:
        raw = os.environ["LOG_FILE"]
        config.log.log_file = raw or None

    # ── Audit ──────────────────────────────────────────────────────
    if val := os.environ.get("AUDIT_ENABLED"):
        config.audit.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("AUDIT_DB_PATH"):
        config.audit.db_path = val
    if val := os.environ.get("AUDIT_PERSIST_PAYLOADS"):
        config.audit.persist_payloads = val.lower() in _TRUTHY_VALUES

    # ── Jira ───────────────────────────────────────────────────────
    if val := os.environ.get("JIRA_ENABLED"):
        config.jira.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("JIRA_URL"):
        config.jira.url = val
    # JIRA_AUTH_ENV_VAR selects *which* env var holds the auth header;
    # defaults to JIRA_AUTH.  Preserve that indirection for parity with
    # the previous loader.
    auth_env = os.environ.get("JIRA_AUTH_ENV_VAR", DEFAULT_JIRA_AUTH_ENV_VAR)
    if val := os.environ.get(auth_env):
        config.jira.auth_header = val

    # ── GitLab ─────────────────────────────────────────────────────
    if val := os.environ.get("GITLAB_ENABLED"):
        config.gitlab.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("GITLAB_URL"):
        config.gitlab.url = val
    if val := os.environ.get("GITLAB_TOKEN"):
        config.gitlab.token = val

    # ── Gerrit ─────────────────────────────────────────────────────
    if val := os.environ.get("GERRIT_ENABLED"):
        config.gerrit.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("GERRIT_URL"):
        config.gerrit.url = val
    if val := os.environ.get("GERRIT_USERNAME"):
        config.gerrit.username = val
    if val := os.environ.get("GERRIT_HTTP_PASSWORD"):
        config.gerrit.http_password = val

    # ── Confluence ─────────────────────────────────────────────────
    if val := os.environ.get("CONFLUENCE_ENABLED"):
        config.confluence.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("CONFLUENCE_URL"):
        config.confluence.url = val
    if val := os.environ.get("CONFLUENCE_USERNAME"):
        config.confluence.username = val
    if val := os.environ.get("CONFLUENCE_API_TOKEN"):
        config.confluence.api_token = val

    # ── Slack search ───────────────────────────────────────────────
    if val := os.environ.get("SLACK_SEARCH_ENABLED"):
        config.slack_search.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("SLACK_USER_TOKEN"):
        config.slack_search.user_token = val

    # ── Web search ─────────────────────────────────────────────────
    if val := os.environ.get("WEB_SEARCH_ENABLED"):
        config.web_search.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("BRAVE_SEARCH_API_KEY"):
        config.web_search.api_key = val
    if val := os.environ.get("JINA_API_KEY"):
        config.web_search.jina_api_key = val
    if val := os.environ.get("WEB_SEARCH_DEFAULT_LIMIT"):
        config.web_search.default_limit = int(val)

    # ── Coding agent ───────────────────────────────────────────────
    if val := os.environ.get("CODING_AGENT_ENABLED"):
        config.coding.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("CODING_WORKSPACE_ROOT"):
        config.coding.workspace_root = val
    if val := os.environ.get("GIT_CLONE_ALLOWED_HOSTS"):
        config.coding.git_clone_allowed_hosts = val

    # ── Skills ─────────────────────────────────────────────────────
    if val := os.environ.get("SKILLS_ENABLED"):
        config.skills.enabled = val.lower() in _TRUTHY_VALUES
    if val := os.environ.get("SKILLS_DIR"):
        config.skills.skills_dir = val


# ── Validation ──────────────────────────────────────────────────────


def _check_required_secrets(config: AppConfig, *, in_sandbox: bool = False) -> None:
    """Ensure required secrets are present.

    Slack bot + app tokens are required in every environment.
    Inference API key and base URL are required for local dev; inside
    the sandbox, ``inference.local`` resolution + proxy-injected key
    make both optional from the app's perspective (the proxy supplies
    them at HTTP-request time).

    Env-var-based secret handling is primarily a *local-dev* concern:
    in local dev ``make run-local-dev`` exports ``.env`` into the
    shell, and the app reads real tokens from ``os.environ``.  Inside
    the sandbox, every secret env var holds an OpenShell-provider
    placeholder string that the L7 proxy resolves at request time —
    the app only ever sees the placeholder, never the real secret.

    Args:
        config: ``AppConfig`` to validate.
        in_sandbox: Whether the process is running inside an OpenShell
            sandbox (from :func:`_env_is_sandbox`).  Relaxes the
            Inference Hub requirement when True.

    Raises:
        ValueError: If any required secret is missing.  Message lists
            all missing keys so the operator sees the full set in one
            pass.
    """
    missing: list[str] = []
    if not config.slack.bot_token:
        missing.append("SLACK_BOT_TOKEN")
    if not config.slack.app_token:
        missing.append("SLACK_APP_TOKEN")
    if not in_sandbox:
        if not config.inference.api_key:
            missing.append("INFERENCE_HUB_API_KEY")
        if not config.inference.base_url:
            missing.append("INFERENCE_HUB_BASE_URL")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in real values."
        )


# ── Public entry point ──────────────────────────────────────────────


def load_config() -> AppConfig:
    """Load configuration from the standard sources.

    Thin backward-compat wrapper around :meth:`AppConfig.load`.  New
    call sites should prefer ``AppConfig.load()`` directly.

    Returns:
        Fully populated ``AppConfig`` ready for use by the application.

    Raises:
        ValueError: If required environment variables are missing.
    """
    return AppConfig.load()


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
