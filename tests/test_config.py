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

    Several env vars are required for ``AppConfig.load()`` to pass
    validation (``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN`` /
    ``INFERENCE_HUB_API_KEY`` / ``INFERENCE_HUB_BASE_URL``), so tests
    that call ``load()`` set them explicitly.  The fixture removes
    them so a test running after another doesn't inherit stale values.
    """
    for key in (
        "OPENSHELL_SANDBOX",
        "NEMOCLAW_CONFIG_PATH",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "INFERENCE_HUB_API_KEY",
        "INFERENCE_HUB_BASE_URL",
        "INFERENCE_MODEL",
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
        "JIRA_AUTH",
        "GITLAB_URL",
        "GITLAB_TOKEN",
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
        "ORCHESTRATOR_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_required_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the secrets ``_check_required_secrets`` insists on."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("INFERENCE_HUB_API_KEY", "test-key")
    monkeypatch.setenv("INFERENCE_HUB_BASE_URL", "http://test")


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
        yaml_path.write_text(
            "coding:\n  workspace_root: /sandbox/workspace\n"
        )
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
            "future_feature:\n  knob: 42\n"
            "coding:\n  workspace_root: /sandbox/workspace\n"
        )
        config = AppConfig.load(path=yaml_path)
        # Known section still applied.
        assert config.coding.workspace_root == "/sandbox/workspace"
        # Unknown section logged at WARNING.
        assert any(
            "Unknown top-level key in YAML overlay" in r.message
            for r in caplog.records
        )

    def test_unknown_field_in_known_section_logs_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "bad_field.yaml"
        yaml_path.write_text(
            "coding:\n  workspace_root: /sandbox/workspace\n  future_knob: 42\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.coding.workspace_root == "/sandbox/workspace"
        assert any(
            "Unknown field in YAML overlay section" in r.message
            for r in caplog.records
        )

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
        yaml_path.write_text(
            "agent_loop:\n"
            "  max_tool_rounds: 20\n"
            "  compaction_min_keep: 8\n"
        )
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
    """``INFERENCE_MODEL`` propagation to ``orchestrator.model``."""

    def test_inference_model_propagates_when_orchestrator_is_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backwards compat: with nothing else set, env propagates both."""
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("INFERENCE_MODEL", "azure/claude-test")
        config = AppConfig.load()
        assert config.inference.model == "azure/claude-test"
        assert config.orchestrator.model == "azure/claude-test"

    def test_inference_model_propagates_to_agent_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``INFERENCE_MODEL`` reaches ``config.agent_loop.model``.

        Sub-agents consume ``config.agent_loop`` directly (see
        ``agent/__main__.py::_run_task``).  Before
        ``_sync_agent_loop_prompting_fields`` existed, an operator-set
        ``INFERENCE_MODEL`` moved ``orchestrator.model`` but left
        ``agent_loop.model`` stuck at ``DEFAULT_INFERENCE_MODEL`` — the
        orchestrator ran on the new model while the sub-agent silently
        ran on the old one.  Now the two stay in lockstep.
        """
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("INFERENCE_MODEL", "azure/claude-test")
        config = AppConfig.load()
        assert config.orchestrator.model == "azure/claude-test"
        assert config.agent_loop.model == "azure/claude-test"

    def test_temperature_and_max_tokens_propagate_to_agent_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same propagation covers the other two shared prompting fields."""
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("TEMPERATURE", "0.33")
        monkeypatch.setenv("MAX_TOKENS", "12345")
        config = AppConfig.load()
        assert config.orchestrator.temperature == 0.33
        assert config.orchestrator.max_tokens == 12345
        # Sub-agents inherit via the post-env sync.
        assert config.agent_loop.temperature == 0.33
        assert config.agent_loop.max_tokens == 12345

    def test_agent_loop_yaml_pin_survives_orchestrator_propagation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """YAML-set ``agent_loop.*`` wins over the orchestrator sync.

        Enables the "run sub-agents on a different model" story: the
        operator pins ``agent_loop.model`` explicitly and it's not
        overwritten by the INFERENCE_MODEL-induced propagation to
        the orchestrator.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "orchestrator:\n  model: orch-model\n"
            "agent_loop:\n"
            "  model: sub-agent-model\n"
            "  temperature: 0.1\n"
            "  max_tokens: 500\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.orchestrator.model == "orch-model"
        # agent_loop fields differ from defaults → not synced.
        assert config.agent_loop.model == "sub-agent-model"
        assert config.agent_loop.temperature == 0.1
        assert config.agent_loop.max_tokens == 500

    def test_inference_model_respects_yaml_orchestrator_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: YAML-set ``orchestrator.model`` isn't clobbered.

        Previously the env var hook unconditionally overwrote both
        fields, so an operator who deliberately pinned the orchestrator
        to one model while switching the inference backend's default
        (``INFERENCE_MODEL``) to another would see their pin silently
        reverted.  Now ``orchestrator.model`` is updated only when
        it's still at the dataclass default.
        """
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "orchestrator:\n  model: pinned/orchestrator-model\n"
        )
        monkeypatch.setenv("INFERENCE_MODEL", "env/inference-model")
        config = AppConfig.load(path=yaml_path)
        # Inference still tracks ``INFERENCE_MODEL``.
        assert config.inference.model == "env/inference-model"
        # Orchestrator keeps the YAML pin.
        assert config.orchestrator.model == "pinned/orchestrator-model"
        # Sub-agent tracks the shared inference baseline, not the
        # orchestrator's YAML pin — per the "orchestrator-only" contract
        # that ``ORCHESTRATOR_MODEL`` documents for the env-var twin.
        assert config.agent_loop.model == "env/inference-model"

    def test_orchestrator_model_env_does_not_leak_to_agent_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``ORCHESTRATOR_MODEL`` is orchestrator-only.

        ``.env.example`` documents ``ORCHESTRATOR_MODEL`` as pinning
        "the orchestrator to a specific model while the rest of the
        inference backend defaults to INFERENCE_MODEL."  Without this
        guard, ``_sync_agent_loop_prompting_fields`` would pull from
        ``config.orchestrator.model`` (which holds the pin) and the
        sub-agent would silently run on the orchestrator's private
        model instead of the shared inference baseline.  Fix:
        ``model`` syncs from ``config.inference.model``.
        """
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("INFERENCE_MODEL", "fast-model")
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "smart-model")
        config = AppConfig.load()
        assert config.inference.model == "fast-model"
        assert config.orchestrator.model == "smart-model"
        # The regression: sub-agent must stay on the shared baseline.
        assert config.agent_loop.model == "fast-model"

    def test_orchestrator_model_env_wins_over_propagation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ORCHESTRATOR_MODEL`` gives operators an explicit escape hatch."""
        _set_required_secrets(monkeypatch)
        monkeypatch.setenv("INFERENCE_MODEL", "inference-env-model")
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "orchestrator-env-model")
        config = AppConfig.load()
        assert config.inference.model == "inference-env-model"
        assert config.orchestrator.model == "orchestrator-env-model"
        # Sub-agent follows the inference baseline, not the
        # orchestrator's ``ORCHESTRATOR_MODEL`` override.
        assert config.agent_loop.model == "inference-env-model"


class TestEnvOverrides:
    """Env vars trump YAML per the documented precedence."""

    def test_env_overrides_yaml_per_field(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "coding:\n"
            "  workspace_root: /sandbox/workspace\n"
            "  git_clone_allowed_hosts: yaml-host.example.com\n"
        )
        # Env wins for the one field it sets; the other keeps YAML.
        monkeypatch.setenv("GIT_CLONE_ALLOWED_HOSTS", "env-host.example.com")
        config = AppConfig.load(path=yaml_path)
        assert config.coding.workspace_root == "/sandbox/workspace"
        assert config.coding.git_clone_allowed_hosts == "env-host.example.com"

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

    def test_agent_loop_env_overrides_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same per-field precedence we verify elsewhere, applied to
        # loop-runtime knobs.  YAML sets a value, env var wins.
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text("agent_loop:\n  max_tool_rounds: 20\n")
        monkeypatch.setenv("AGENT_LOOP_MAX_TOOL_ROUNDS", "42")
        config = AppConfig.load(path=yaml_path)
        assert config.agent_loop.max_tool_rounds == 42

    def test_nmb_section_populates_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "nmb:\n"
            "  broker_url: ws://broker.example:9999\n"
            "  sandbox_id: sub-42\n"
        )
        config = AppConfig.load(path=yaml_path)
        assert config.nmb.broker_url == "ws://broker.example:9999"
        assert config.nmb.sandbox_id == "sub-42"

    def test_nmb_env_overrides_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``NMB_URL`` / ``AGENT_SANDBOX_ID`` are the same env var names
        # the sub-agent used to read directly; they now route through
        # the config loader as per-field overrides.
        _set_required_secrets(monkeypatch)
        yaml_path = tmp_path / "cfg.yaml"
        yaml_path.write_text(
            "nmb:\n  broker_url: ws://yaml.example:1\n  sandbox_id: yaml-id\n"
        )
        monkeypatch.setenv("NMB_URL", "ws://env.example:2")
        monkeypatch.setenv("AGENT_SANDBOX_ID", "env-id")
        config = AppConfig.load(path=yaml_path)
        assert config.nmb.broker_url == "ws://env.example:2"
        assert config.nmb.sandbox_id == "env-id"


# ── Secret validation ──────────────────────────────────────────────


class TestDotenvLoader:
    """``load_dotenv_if_present`` wires ``.env`` into ``os.environ``.

    Regression: running ``python -m nemoclaw_escapades{,.agent}``
    directly (outside ``make run-local-dev``) used to fail with
    "Missing required environment variables: INFERENCE_HUB_API_KEY /
    SLACK_BOT_TOKEN" because the entrypoints read ``os.environ``
    without first loading the operator's ``.env``.  The helper's
    job is to close that gap on the entrypoint side without changing
    the test-friendly ``AppConfig.load`` (tests control their env
    explicitly via ``monkeypatch``).
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
    """``_check_required_secrets`` refuses to return a config without tokens."""

    def test_missing_slack_tokens_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("INFERENCE_HUB_API_KEY", "k")
        monkeypatch.setenv("INFERENCE_HUB_BASE_URL", "http://x")
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            AppConfig.load()

    def test_sandbox_does_not_require_inference_hub_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        # Deliberately skip INFERENCE_HUB_*.  In the sandbox, the
        # proxy supplies them; the loader must not raise.
        monkeypatch.setenv("OPENSHELL_SANDBOX", "1")
        config = AppConfig.load()
        # Sandbox backfill fires because env is empty and in_sandbox.
        assert config.inference.base_url == "https://inference.local/v1"

    def test_env_argument_overrides_openshell_sandbox_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``env=SANDBOX`` unlocks sandbox branches even without the env var.

        Regression: before this path was threaded, the loader read
        ``OPENSHELL_SANDBOX`` directly and could disagree with the
        multi-signal detector.  Now a caller-supplied
        :class:`RuntimeEnvironment` is the source of truth.
        """
        from nemoclaw_escapades.runtime import RuntimeEnvironment

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.delenv("OPENSHELL_SANDBOX", raising=False)
        monkeypatch.delenv("INFERENCE_HUB_API_KEY", raising=False)
        monkeypatch.delenv("INFERENCE_HUB_BASE_URL", raising=False)
        # Without env=..., the loader would treat this as LOCAL_DEV
        # and refuse to start (missing INFERENCE_HUB_*).  With env
        # passed in, the sandbox branch relaxes that requirement and
        # the inference URL gets backfilled.
        config = AppConfig.load(env=RuntimeEnvironment.SANDBOX)
        assert config.inference.base_url == "https://inference.local/v1"

    def test_sub_agent_path_does_not_require_slack_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``require_slack=False`` skips the Slack check.

        The coding sub-agent never touches Slack — CLI mode prints to
        stdout and NMB mode talks to the broker — so requiring
        ``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN`` for its startup path
        makes ``python -m nemoclaw_escapades.agent --task ...`` fail
        on any machine whose ``.env`` isn't fully configured for the
        orchestrator.  ``AppConfig.load(require_slack=False)`` opts
        out of that check while keeping the inference-secret
        validation untouched.
        """
        # Inference secrets present; Slack ones deliberately absent.
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.setenv("INFERENCE_HUB_API_KEY", "k")
        monkeypatch.setenv("INFERENCE_HUB_BASE_URL", "http://x")
        # Must not raise.
        config = AppConfig.load(require_slack=False)
        assert config.slack.bot_token == ""
        assert config.slack.app_token == ""
        assert config.inference.api_key == "k"

    def test_sub_agent_path_still_validates_inference_in_local_dev(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``require_slack=False`` is about *Slack only* — inference
        is still required in local dev.  Regression guard so a future
        edit doesn't accidentally broaden the opt-out.
        """
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("INFERENCE_HUB_API_KEY", raising=False)
        monkeypatch.delenv("INFERENCE_HUB_BASE_URL", raising=False)
        monkeypatch.delenv("OPENSHELL_SANDBOX", raising=False)
        with pytest.raises(ValueError, match="INFERENCE_HUB"):
            AppConfig.load(require_slack=False)

    def test_env_argument_local_dev_requires_inference_hub(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``env=LOCAL_DEV`` keeps the strict secrets requirement.

        Mirror of the test above from the other side: even with the
        ``OPENSHELL_SANDBOX`` env var set (stale from a prior shell),
        an explicit ``env=LOCAL_DEV`` forces the strict local-dev
        validation.
        """
        from nemoclaw_escapades.runtime import RuntimeEnvironment

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        # OPENSHELL_SANDBOX stale in the shell — with the old single-
        # signal check this would wrongly enable sandbox mode.
        monkeypatch.setenv("OPENSHELL_SANDBOX", "1")
        monkeypatch.delenv("INFERENCE_HUB_API_KEY", raising=False)
        monkeypatch.delenv("INFERENCE_HUB_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="INFERENCE_HUB"):
            AppConfig.load(env=RuntimeEnvironment.LOCAL_DEV)
