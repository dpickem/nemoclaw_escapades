"""Unit tests for :mod:`nemoclaw_escapades.config`.

Covers the three-source loader: dataclass defaults, YAML overlay,
environment-variable overrides.  Mirrors the design doc's §15 / §16.1
entries for *Config YAML overlay*, *Config env-var precedence*,
*Config unknown keys*, and *Config secret isolation (loader)*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nemoclaw_escapades.config import (
    DEFAULT_CODING_WORKSPACE_ROOT,
    DEFAULT_GIT_CLONE_ALLOWED_HOSTS,
    DEFAULT_SKILLS_DIR,
    AppConfig,
    load_dotenv_if_present,
)

# ── Helpers ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var the loader consults so each test starts clean.

    After the M2b P1 config-SSOT refactor, only *secret* env vars
    reach the loader.  Non-secret vars (``LOG_LEVEL``,
    ``AGENT_LOOP_*``, ``NMB_URL``, ``CODING_*``, etc.) are no-ops at
    runtime but a few tests deliberately set them to assert they stay
    ignored; scrub them here so a stale shell export doesn't accidentally
    satisfy a precondition.
    """
    for key in (
        # Sandbox detection.
        "OPENSHELL_SANDBOX",
        # YAML path selector.
        "NEMOCLAW_CONFIG_PATH",
        # Secret env vars the loader still honours.
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "INFERENCE_HUB_API_KEY",
        "JIRA_AUTH",
        "GITLAB_TOKEN",
        "GERRIT_USERNAME",
        "GERRIT_HTTP_PASSWORD",
        "CONFLUENCE_USERNAME",
        "CONFLUENCE_API_TOKEN",
        "SLACK_USER_TOKEN",
        "BRAVE_SEARCH_API_KEY",
        "JINA_API_KEY",
        # Formerly-honoured non-secret env vars — kept in the scrub
        # list so ``test_non_secret_env_vars_are_ignored`` doesn't
        # inherit stale values from previous runs.
        "INFERENCE_HUB_BASE_URL",
        "INFERENCE_MODEL",
        "ORCHESTRATOR_MODEL",
        "TEMPERATURE",
        "MAX_TOKENS",
        "LOG_LEVEL",
        "LOG_FILE",
        "AUDIT_ENABLED",
        "AUDIT_DB_PATH",
        "CODING_AGENT_ENABLED",
        "CODING_WORKSPACE_ROOT",
        "GIT_CLONE_ALLOWED_HOSTS",
        "SKILLS_ENABLED",
        "SKILLS_DIR",
        "JIRA_URL",
        "GITLAB_URL",
        "GERRIT_URL",
        "AGENT_LOOP_MAX_TOOL_ROUNDS",
        "AGENT_LOOP_MAX_CONTINUATION_RETRIES",
        "AGENT_LOOP_MICRO_COMPACTION_CHARS",
        "AGENT_LOOP_COMPACTION_THRESHOLD_CHARS",
        "AGENT_LOOP_COMPACTION_COMPRESS_RATIO",
        "AGENT_LOOP_COMPACTION_MIN_KEEP",
        "AGENT_LOOP_COMPACTION_MODEL",
        "NMB_URL",
        "AGENT_SANDBOX_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_required_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the secrets ``_check_required_config`` insists on.

    ``INFERENCE_HUB_API_KEY`` is deliberately absent — the sandbox
    never sees it (the L7 proxy handles auth via a separately-named
    ``OPENAI_API_KEY`` credential) and the loader doesn't require it.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")


# ── Dataclass defaults ──────────────────────────────────────────────


class TestDataclassDefaults:
    """Bare-defaults view of ``AppConfig()`` with no YAML, no env."""

    def test_defaults_are_local_dev_friendly(self) -> None:
        config = AppConfig()
        # Local-dev paths — not sandbox ones.  The YAML overlay is
        # what swaps these to /sandbox/* at runtime.
        assert config.coding.workspace_root == DEFAULT_CODING_WORKSPACE_ROOT
        assert config.skills.skills_dir == DEFAULT_SKILLS_DIR
        # Category-B URLs are empty in the public source.
        assert config.gitlab.url == ""
        assert config.gerrit.url == ""
        # Fail-closed allowlist by default.
        assert config.coding.git_clone_allowed_hosts == DEFAULT_GIT_CLONE_ALLOWED_HOSTS


# ── YAML overlay ────────────────────────────────────────────────────


class TestYamlOverlay:
    """``AppConfig.load`` with a YAML overlay applied."""

    def test_missing_yaml_is_not_an_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        # Path that doesn't exist — loader should silently fall back
        # to dataclass defaults.
        missing = tmp_path / "does_not_exist.yaml"
        config = AppConfig.load(path=missing)
        assert config.coding.workspace_root == DEFAULT_CODING_WORKSPACE_ROOT

    def test_partial_overlay_keeps_unspecified_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "partial.yaml"
        yaml_path.write_text("coding:\n  workspace_root: /sandbox/workspace\n")
        config = AppConfig.load(path=yaml_path)
        # Overridden by YAML.
        assert config.coding.workspace_root == "/sandbox/workspace"
        # Not mentioned — keeps dataclass default.
        assert config.skills.skills_dir == DEFAULT_SKILLS_DIR
        # Not mentioned — keeps dataclass default.
        assert config.coding.git_clone_allowed_hosts == DEFAULT_GIT_CLONE_ALLOWED_HOSTS

    def test_toolsets_group_maps_to_top_level_configs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "toolsets.yaml"
        yaml_path.write_text(
            "toolsets:\n"
            "  gitlab:\n"
            "    url: https://gitlab.example.com\n"
            "  gerrit:\n"
            "    enabled: false\n"
        )
        config = AppConfig.load(path=yaml_path)
        # toolsets.gitlab.url → config.gitlab.url.
        assert config.gitlab.url == "https://gitlab.example.com"
        # toolsets.gerrit.enabled → config.gerrit.enabled.
        assert config.gerrit.enabled is False

    def test_unknown_top_level_key_logs_warning_but_loads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "weird.yaml"
        yaml_path.write_text(
            "future_feature:\n  knob: 42\ncoding:\n  workspace_root: /sandbox/workspace\n"
        )
        config = AppConfig.load(path=yaml_path)
        # Known section still applied.
        assert config.coding.workspace_root == "/sandbox/workspace"
        # Unknown section logged at WARNING.
        assert any("Unknown top-level key in YAML overlay" in r.message for r in caplog.records)

    def test_unknown_field_in_known_section_logs_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "bad_field.yaml"
        yaml_path.write_text("coding:\n  workspace_root: /sandbox/workspace\n  future_knob: 42\n")
        config = AppConfig.load(path=yaml_path)
        assert config.coding.workspace_root == "/sandbox/workspace"
        assert any("Unknown field in YAML overlay section" in r.message for r in caplog.records)

    def test_malformed_yaml_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "broken.yaml"
        # Indentation error — not parseable.
        yaml_path.write_text("coding:\n  workspace_root: /sandbox\n bad_indent\n")
        with pytest.raises(ValueError, match="Invalid YAML"):
            AppConfig.load(path=yaml_path)

    def test_non_mapping_top_level_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "list.yaml"
        yaml_path.write_text("- just_a_list\n- not_a_mapping\n")
        with pytest.raises(ValueError, match="must be a mapping"):
            AppConfig.load(path=yaml_path)

    def test_agent_loop_section_populates_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ``agent_loop`` is a first-class section: values from the YAML
        # land on ``config.agent_loop`` and don't trigger the unknown-
        # top-level-key warning.
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "agent_loop.yaml"
        yaml_path.write_text("agent_loop:\n  max_tool_rounds: 20\n  compaction_min_keep: 8\n")
        caplog.clear()
        config = AppConfig.load(path=yaml_path)
        assert config.agent_loop.max_tool_rounds == 20
        assert config.agent_loop.compaction_min_keep == 8
        assert not any(
            "Unknown top-level key" in r.message and "agent_loop" in str(r.__dict__)
            for r in caplog.records
        )


# ── Env-var overrides ──────────────────────────────────────────────


class TestInferenceModelPropagation:
    """YAML ``inference.model`` propagation to ``orchestrator.model`` and ``agent_loop.model``.

    The previous env-var path (``INFERENCE_MODEL`` / ``ORCHESTRATOR_MODEL``)
    is retired; non-secret knobs now live in YAML only.  Propagation
    semantics still matter though: a YAML-set ``inference.model``
    should flow through to the agent-loop sub-agents that consume
    ``config.agent_loop.model`` directly.
    """

    def test_inference_model_propagates_to_agent_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """YAML ``inference.model`` reaches ``config.agent_loop.model``.

        Sub-agents consume ``config.agent_loop`` directly (see
        ``agent/__main__.py::_run_task``).  Without the post-YAML sync
        an operator-set ``inference.model`` would move
        ``config.inference.model`` but leave ``agent_loop.model`` stuck
        at ``DEFAULT_INFERENCE_MODEL`` — the backend would use the new
        model while the sub-agent silently ran on the old one.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("inference:\n  model: azure/claude-test\n")
        config = AppConfig.load(path=yaml_path)
        assert config.inference.model == "azure/claude-test"
        assert config.agent_loop.model == "azure/claude-test"

    def test_orchestrator_temperature_and_max_tokens_propagate_to_agent_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Orchestrator prompting fields sync into ``agent_loop``."""
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("orchestrator:\n  temperature: 0.33\n  max_tokens: 12345\n")
        config = AppConfig.load(path=yaml_path)
        assert config.orchestrator.temperature == 0.33
        assert config.orchestrator.max_tokens == 12345
        assert config.agent_loop.temperature == 0.33
        assert config.agent_loop.max_tokens == 12345

    def test_agent_loop_yaml_pin_survives_propagation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """YAML-set ``agent_loop.*`` wins over the post-YAML sync.

        Enables the "run sub-agents on a different model" story: the
        operator pins ``agent_loop.model`` explicitly and it's not
        overwritten by the inference-model propagation.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "inference:\n  model: inference-model\n"
            "orchestrator:\n  model: orch-model\n"
            "agent_loop:\n"
            "  model: sub-agent-model\n"
            "  temperature: 0.1\n"
            "  max_tokens: 500\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.inference.model == "inference-model"
        assert config.orchestrator.model == "orch-model"
        # agent_loop fields differ from defaults → not synced.
        assert config.agent_loop.model == "sub-agent-model"
        assert config.agent_loop.temperature == 0.1
        assert config.agent_loop.max_tokens == 500

    def test_agent_loop_tracks_inference_not_orchestrator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: sub-agent follows ``inference.model``, not ``orchestrator.model``.

        Operators can pin the orchestrator to a specific model via
        ``orchestrator.model`` while the sub-agent continues to track
        the shared inference baseline.  Fix:
        ``_sync_agent_loop_prompting_fields`` reads from
        ``config.inference.model``, not ``config.orchestrator.model``.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "inference:\n  model: fast-model\norchestrator:\n  model: smart-model\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.inference.model == "fast-model"
        assert config.orchestrator.model == "smart-model"
        assert config.agent_loop.model == "fast-model"


class TestYamlPrecedence:
    """YAML sections populate ``AppConfig`` — the non-secret source of truth."""

    def test_nemoclaw_config_path_env_selects_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "from_env.yaml"
        yaml_path.write_text("log:\n  level: DEBUG\n")
        monkeypatch.setenv("NEMOCLAW_CONFIG_PATH", str(yaml_path))
        config = AppConfig.load()  # no path argument
        assert config.log.level == "DEBUG"

    def test_nmb_section_populates_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("nmb:\n  broker_url: ws://broker.example:9999\n  sandbox_id: sub-42\n")
        config = AppConfig.load(path=yaml_path)
        assert config.nmb.broker_url == "ws://broker.example:9999"
        assert config.nmb.sandbox_id == "sub-42"

    def test_agent_loop_section_populates_runtime_knobs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("agent_loop:\n  max_tool_rounds: 42\n  compaction_min_keep: 8\n")
        config = AppConfig.load(path=yaml_path)
        assert config.agent_loop.max_tool_rounds == 42
        assert config.agent_loop.compaction_min_keep == 8

    def test_coding_section_populates_workspace_and_allowlist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "coding:\n"
            "  workspace_root: /sandbox/workspace\n"
            "  git_clone_allowed_hosts: host-a.example.com,host-b.example.com\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.coding.workspace_root == "/sandbox/workspace"
        assert config.coding.git_clone_allowed_hosts == "host-a.example.com,host-b.example.com"


class TestSecretEnvOverrides:
    """Secret env vars are the only runtime overrides the loader honours.

    Non-secret knobs (URLs, models, paths, feature flags) come from
    YAML only.  This class exercises the narrow "env for secrets"
    contract that the refactor preserves.
    """

    def test_slack_tokens_override_from_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-from-env")
        config = AppConfig.load()
        assert config.slack.bot_token == "xoxb-from-env"
        assert config.slack.app_token == "xapp-from-env"

    def test_service_credentials_flow_through_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each service credential env var populates the matching dataclass field."""
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("JIRA_AUTH", "Basic aGVsbG86d29ybGQ=")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
        monkeypatch.setenv("GERRIT_USERNAME", "gerrit-user")
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "gerrit-pw")
        monkeypatch.setenv("CONFLUENCE_USERNAME", "conf-user")
        monkeypatch.setenv("CONFLUENCE_API_TOKEN", "conf-token")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user")
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
        monkeypatch.setenv("JINA_API_KEY", "jina-key")
        config = AppConfig.load()
        assert config.jira.auth_header == "Basic aGVsbG86d29ybGQ="
        assert config.gitlab.token == "glpat-xyz"
        assert config.gerrit.username == "gerrit-user"
        assert config.gerrit.http_password == "gerrit-pw"
        assert config.confluence.username == "conf-user"
        assert config.confluence.api_token == "conf-token"
        assert config.slack_search.user_token == "xoxp-user"
        assert config.web_search.api_key == "brave-key"
        assert config.web_search.jina_api_key == "jina-key"

    def test_non_secret_env_vars_are_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-secret env vars no longer override YAML — only the YAML wins.

        Regression guard: before this refactor, ``LOG_LEVEL``,
        ``INFERENCE_MODEL``, ``AGENT_LOOP_*``, ``NMB_URL``,
        ``GIT_CLONE_ALLOWED_HOSTS``, etc. all had env-var hooks.
        They're gone now — if an operator sets one it must be silently
        ignored.  (``scripts/gen_config.py`` reads ``GITLAB_URL`` /
        ``GIT_CLONE_ALLOWED_HOSTS`` at build time to write the YAML,
        which is a separate code path.)
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "log:\n  level: DEBUG\n"
            "agent_loop:\n  max_tool_rounds: 20\n"
            "nmb:\n  broker_url: ws://yaml.example:1\n"
            "coding:\n  git_clone_allowed_hosts: yaml-host.example.com\n"
        )
        # Every one of these env vars used to win over YAML.  They
        # must now be no-ops.
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        monkeypatch.setenv("AGENT_LOOP_MAX_TOOL_ROUNDS", "99")
        monkeypatch.setenv("NMB_URL", "ws://env.example:2")
        monkeypatch.setenv("GIT_CLONE_ALLOWED_HOSTS", "env-host.example.com")
        config = AppConfig.load(path=yaml_path)
        assert config.log.level == "DEBUG"
        assert config.agent_loop.max_tool_rounds == 20
        assert config.nmb.broker_url == "ws://yaml.example:1"
        assert config.coding.git_clone_allowed_hosts == "yaml-host.example.com"


class TestRuntimeEnvOverrides:
    """The narrow, named ``NEMOCLAW_*`` non-secret runtime layer.

    Distinct from :class:`TestSecretEnvOverrides` (credentials) and
    from :class:`TestNonSecretEnvIgnored`-style YAML-only fields.
    These two env vars exist because the orchestrator's spawn
    callback needs to assign per-process identity + workspace at
    delegation time, and that's not expressible as shared YAML.
    Adding new entries here should be rare and obvious in review.
    """

    def test_nemoclaw_sandbox_id_overrides_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulates the orchestrator spawning a sub-agent with a
        # specific, broker-known id.  The YAML's value (e.g. left
        # empty for "auto-generate") must yield to the env knob —
        # otherwise the orchestrator dials a sandbox the sub-agent
        # never registers under, hitting the readiness retry.
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("nmb:\n  sandbox_id: yaml-default\n")
        monkeypatch.setenv("NEMOCLAW_SANDBOX_ID", "coding-12345678")
        config = AppConfig.load(path=yaml_path)
        assert config.nmb.sandbox_id == "coding-12345678"

    def test_nemoclaw_workspace_root_overrides_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same shape: the orchestrator picks a per-agent subdir at
        # spawn time so concurrent sub-agents don't clobber each
        # other's scratchpad / notes (M2b §16.2 row "Sub-agent
        # workspace isolation").
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("coding:\n  workspace_root: /sandbox/workspace\n")
        monkeypatch.setenv("NEMOCLAW_WORKSPACE_ROOT", "/sandbox/workspace/agent-deadbeef")
        config = AppConfig.load(path=yaml_path)
        assert config.coding.workspace_root == "/sandbox/workspace/agent-deadbeef"

    def test_unset_means_yaml_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sanity check the contract is "env wins when set" not "env
        # always wins".  An unset env var leaves YAML alone — the
        # default sub-agent self-generates an id from
        # ``_make_agent_id`` if neither layer pinned one.
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "nmb:\n  sandbox_id: yaml-default\n"
            "coding:\n  workspace_root: /sandbox/workspace\n"
        )
        monkeypatch.delenv("NEMOCLAW_SANDBOX_ID", raising=False)
        monkeypatch.delenv("NEMOCLAW_WORKSPACE_ROOT", raising=False)
        config = AppConfig.load(path=yaml_path)
        assert config.nmb.sandbox_id == "yaml-default"
        assert config.coding.workspace_root == "/sandbox/workspace"


# ── Secret validation ──────────────────────────────────────────────


class TestDotenvLoader:
    """``load_dotenv_if_present`` wires ``.env`` into ``os.environ``.

    Regression: host-side tooling (``scripts/gen_config.py``,
    ``scripts/gen_policy.py``, ``make run-broker``) used to fail with
    "Missing required environment variables: ..." because the
    entrypoints read ``os.environ`` without first loading the
    operator's ``.env``.  The helper closes that gap on the
    entrypoint side without changing the test-friendly
    ``AppConfig.load`` — tests control their env explicitly via
    ``monkeypatch``.
    """

    def test_loads_env_file_from_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``.env`` in CWD populates ``os.environ``."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("INFERENCE_HUB_API_KEY", raising=False)
        (tmp_path / ".env").write_text("INFERENCE_HUB_API_KEY=from-dotenv\n")

        loaded = load_dotenv_if_present()

        assert loaded is True
        import os

        assert os.environ.get("INFERENCE_HUB_API_KEY") == "from-dotenv"

    def test_missing_dotenv_is_not_an_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ``.env`` → ``False`` + no side effects.

        This is the CI / OSS-consumer / subprocess-test case.  The
        subprocess integration tests run in a ``tmp_path`` cwd
        specifically so the loader no-ops.
        """
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / ".env").exists()
        assert load_dotenv_if_present() is False

    def test_shell_env_wins_over_dotenv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``override=False`` preserves the documented precedence.

        Shell-exported vars beat the ``.env`` file — same rule as
        ``AppConfig.load``'s env-wins-over-YAML precedence.
        Operators can still ``INFERENCE_HUB_API_KEY=foo make
        run-local-dev`` to override what's in ``.env`` without
        editing the file.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INFERENCE_HUB_API_KEY", "from-shell")
        (tmp_path / ".env").write_text("INFERENCE_HUB_API_KEY=from-dotenv\n")

        load_dotenv_if_present()

        import os

        assert os.environ["INFERENCE_HUB_API_KEY"] == "from-shell"


class TestSecretValidation:
    """``_check_required_config`` refuses to return a config missing required fields."""

    def test_missing_slack_tokens_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            AppConfig.load()

    def test_missing_inference_api_key_is_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``INFERENCE_HUB_API_KEY`` is deliberately **not** required.

        In the sandbox the inference provider is registered under a
        different credential name (``OPENAI_API_KEY``, see the
        Makefile's ``setup-providers``) and the L7 proxy at
        ``inference.local`` injects the real key at HTTP-request
        time.  The app never reads an API key — ``InferenceHubBackend``
        omits the ``Authorization`` header when
        ``config.inference.api_key`` is empty — so requiring one would
        crash every sandbox startup with a false-positive.
        Regression guard against reintroducing that check.
        """
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("INFERENCE_HUB_API_KEY", raising=False)
        # Must not raise.
        config = AppConfig.load()
        assert config.inference.api_key == ""

    def test_inference_base_url_comes_from_yaml_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``config/defaults.yaml`` pins ``inference.base_url`` to the
        sandbox proxy endpoint.  Regression guard so a future edit
        doesn't reintroduce a silent in-code backfill.
        """
        _set_required_secrets(monkeypatch)
        config = AppConfig.load()
        assert config.inference.base_url == "https://inference.local/v1"

    def test_missing_inference_base_url_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Fail-fast when a per-deployment YAML nukes ``inference.base_url``.

        The default YAML always sets it; this test forces the blank
        state via a minimal custom YAML and asserts the loader
        refuses it instead of silently routing to a hardcoded fallback.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "blank.yaml"
        yaml_path.write_text("inference:\n  base_url: ''\n")
        monkeypatch.setenv("NEMOCLAW_CONFIG_PATH", str(yaml_path))
        with pytest.raises(ValueError, match="inference.base_url"):
            AppConfig.load()

    def test_sub_agent_path_does_not_require_slack_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``require_slack=False`` skips the Slack check.

        The coding sub-agent never touches Slack — both of its run
        modes (``--task`` CLI mode, ``--nmb`` broker mode) live inside
        the sandbox and talk to stdout or the broker, never to Slack —
        so requiring ``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN`` for its
        startup path makes ``python -m nemoclaw_escapades.agent
        --task ...`` fail on any machine whose ``.env`` isn't fully
        configured for the orchestrator.
        """
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        # Must not raise.
        config = AppConfig.load(require_slack=False)
        assert config.slack.bot_token == ""
        assert config.slack.app_token == ""

    def test_sub_agent_path_still_validates_base_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``require_slack=False`` is about *Slack only* — the
        ``inference.base_url`` YAML fail-fast still applies so a
        malformed per-deployment YAML surfaces at startup for the
        sub-agent too.  Regression guard so a future edit doesn't
        accidentally broaden the opt-out.
        """
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        yaml_path = tmp_path / "blank.yaml"
        yaml_path.write_text("inference:\n  base_url: ''\n")
        monkeypatch.setenv("NEMOCLAW_CONFIG_PATH", str(yaml_path))
        with pytest.raises(ValueError, match="inference.base_url"):
            AppConfig.load(require_slack=False)
