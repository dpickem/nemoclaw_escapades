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

DEFAULT_INFERENCE_BASE_URL = "https://inference-api.nvidia.com/v1"
SANDBOX_INFERENCE_BASE_URL = "https://inference.local/v1"
DEFAULT_INFERENCE_MODEL = "azure/anthropic/claude-opus-4-6"  # hyphenated, not dotted
DEFAULT_INFERENCE_TIMEOUT_S = 60
DEFAULT_INFERENCE_MAX_RETRIES = 3
DEFAULT_SYSTEM_PROMPT_PATH = "prompts/system_prompt.md"
DEFAULT_MAX_THREAD_HISTORY = 50
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 2048


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""


@dataclass
class InferenceConfig:
    base_url: str = DEFAULT_INFERENCE_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_INFERENCE_MODEL
    timeout_s: int = DEFAULT_INFERENCE_TIMEOUT_S
    max_retries: int = DEFAULT_INFERENCE_MAX_RETRIES


@dataclass
class OrchestratorConfig:
    model: str = DEFAULT_INFERENCE_MODEL
    system_prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH
    max_thread_history: int = DEFAULT_MAX_THREAD_HISTORY
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass
class LogConfig:
    level: str = DEFAULT_LOG_LEVEL
    log_file: str | None = None


@dataclass
class AppConfig:
    slack: SlackConfig = field(default_factory=SlackConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    log: LogConfig = field(default_factory=LogConfig)


def load_config() -> AppConfig:
    """Load configuration from environment variables.

    In the OpenShell sandbox, secrets are injected by the gateway.
    For local dev, ``make run-local-dev`` sources .env into the shell.

    Raises ValueError if required variables are missing.
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
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in real values."
        )

    # In sandbox, use inference.local (OpenShell's inference proxy handles
    # auth and routing).  Locally, hit the inference API directly.
    default_base_url = SANDBOX_INFERENCE_BASE_URL if in_sandbox else DEFAULT_INFERENCE_BASE_URL
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
    )


def load_system_prompt(path: str) -> str:
    """Load the system prompt from a file, falling back to a built-in default."""
    p = Path(path)
    if p.is_file():
        return p.read_text().strip()

    return (
        "You are NemoClaw, a helpful AI assistant. "
        "Be concise and direct in your responses. "
        "You do not yet have tools or persistent memory."
    )
