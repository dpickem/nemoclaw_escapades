"""Unit tests for :mod:`scripts.gen_config`.

Covers the §16.1 entries *empty .env*, *populated .env*, *unknown
key*, *secret guard*, and *no hostname leak in public source*.

``gen_config`` is a standalone script, not a package module, so the
tests manipulate ``sys.path`` and cwd to load it directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT_PATH: Path = Path(__file__).resolve().parent.parent / "scripts" / "gen_config.py"


def _load_gen_config_module() -> object:
    """Import ``scripts/gen_config.py`` as a module for direct calls.

    ``scripts/`` isn't a package, so ``import scripts.gen_config``
    doesn't work out of the box.  Use ``importlib`` to load the file
    by path.
    """
    spec = importlib.util.spec_from_file_location("gen_config", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_config"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen_config_mod() -> object:
    return _load_gen_config_module()


@pytest.fixture
def sandbox_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run the resolver inside a temp directory with a synthetic repo.

    The resolver reads ``config/defaults.yaml`` and writes
    ``config/orchestrator.resolved.yaml`` — both paths are relative to
    the cwd (not the script's own dir).  Test harnesses chdir into
    a fresh tmp_path and provide a stand-in defaults file.
    """
    repo_root: Path = _SCRIPT_PATH.parent.parent
    (tmp_path / "config").mkdir()
    # Use the real defaults file for accuracy — any fake would
    # duplicate schema and drift.  Tests that need a different base
    # write over this file.
    defaults_src = repo_root / "config" / "defaults.yaml"
    (tmp_path / "config" / "defaults.yaml").write_text(defaults_src.read_text())
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Happy paths ─────────────────────────────────────────────────────


class TestResolverHappyPath:
    """Normal .env → resolved.yaml flow."""

    def test_empty_env_produces_failclosed_yaml(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        # No .env file — category-B fields stay empty / fail-closed.
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved = yaml.safe_load(
            (sandbox_cwd / "config" / "orchestrator.resolved.yaml").read_text()
        )
        assert resolved["coding"]["git_clone_allowed_hosts"] == ""
        assert resolved["toolsets"]["gitlab"]["url"] == ""
        assert resolved["toolsets"]["gerrit"]["url"] == ""

    def test_populated_env_fills_category_b_values(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        (sandbox_cwd / ".env").write_text(
            "GIT_CLONE_ALLOWED_HOSTS=gitlab.example.com,gerrit.example.com\n"
            "GITLAB_URL=https://gitlab.example.com\n"
            "GERRIT_URL=https://gerrit.example.com/r/a\n"
        )
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved = yaml.safe_load(
            (sandbox_cwd / "config" / "orchestrator.resolved.yaml").read_text()
        )
        assert resolved["coding"]["git_clone_allowed_hosts"] == (
            "gitlab.example.com,gerrit.example.com"
        )
        assert resolved["toolsets"]["gitlab"]["url"] == "https://gitlab.example.com"
        assert resolved["toolsets"]["gerrit"]["url"] == "https://gerrit.example.com/r/a"

    def test_unknown_env_key_is_ignored(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        (sandbox_cwd / ".env").write_text(
            "SOME_OTHER_VAR=definitely-not-in-allowlist\nGIT_CLONE_ALLOWED_HOSTS=github.com\n"
        )
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved_text = (sandbox_cwd / "config" / "orchestrator.resolved.yaml").read_text()
        # Allowlisted key flows through.
        assert "github.com" in resolved_text
        # Unknown key never appears in the resolved output.
        assert "SOME_OTHER_VAR" not in resolved_text
        assert "definitely-not-in-allowlist" not in resolved_text


# ── Self-diagnosing summary ─────────────────────────────────────────


class TestSelfDiagnosingSummary:
    """The summary output surfaces unset / empty category-B keys.

    Concrete failure mode: an operator sets ``GITLAB_TOKEN`` but forgets
    ``GITLAB_URL``.  Without this, the only signal is an empty
    ``toolsets.gitlab.url`` in the resolved YAML — easy to miss.
    """

    def test_missing_env_vars_listed_in_summary(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Only one of the three allowlisted keys is present.
        (sandbox_cwd / ".env").write_text("GIT_CLONE_ALLOWED_HOSTS=github.com\n")
        gen_config_mod.main()  # type: ignore[attr-defined]
        stdout = capsys.readouterr().out
        # The applied override is reported.
        assert "GIT_CLONE_ALLOWED_HOSTS → coding.git_clone_allowed_hosts" in stdout
        # The missing ones are called out by dotted path so an operator
        # can match them against the resolved YAML directly.
        assert "GITLAB_URL → toolsets.gitlab.url" in stdout
        assert "GERRIT_URL → toolsets.gerrit.url" in stdout
        # The remediation hint is present.
        assert "Set these in .env" in stdout

    def test_empty_value_counts_as_missing(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A key declared but left empty must be reported, not silently
        # dropped — matches the ``.env.example`` pattern where every
        # category-B key is written with an empty value by default.
        (sandbox_cwd / ".env").write_text("GITLAB_URL=\nGERRIT_URL=\nGIT_CLONE_ALLOWED_HOSTS=\n")
        gen_config_mod.main()  # type: ignore[attr-defined]
        stdout = capsys.readouterr().out
        assert "category-B overrides skipped" in stdout
        assert "GITLAB_URL → toolsets.gitlab.url" in stdout


# ── Secret guard ────────────────────────────────────────────────────


class TestSecretGuard:
    """Secret-suffixed keys cannot sneak into the resolved YAML."""

    def test_env_with_secret_suffix_is_ignored_unless_allowlisted(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        # ``*_TOKEN`` in .env — not on the allowlist, so ignored.
        (sandbox_cwd / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret\nJIRA_AUTH=Basic secret\n")
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved_text = (sandbox_cwd / "config" / "orchestrator.resolved.yaml").read_text()
        # Secrets never appear in the resolved file.
        assert "xoxb-secret" not in resolved_text
        assert "Basic secret" not in resolved_text

    def test_allowlist_with_secret_suffix_fails_hard(
        self,
        sandbox_cwd: Path,
        monkeypatch: pytest.MonkeyPatch,
        gen_config_mod: object,
    ) -> None:
        # Simulate a bad edit that adds a secret-like key to the
        # allowlist.  The resolver must refuse to run.
        monkeypatch.setattr(
            gen_config_mod,
            "_CATEGORY_B_KEYS",
            {"SLACK_BOT_TOKEN": "toolsets.slack_search.user_token"},
        )
        with pytest.raises(SystemExit) as exc_info:
            gen_config_mod.main()  # type: ignore[attr-defined]
        assert exc_info.value.code == 2

    def test_username_suffix_is_blocked(
        self,
        sandbox_cwd: Path,
        monkeypatch: pytest.MonkeyPatch,
        gen_config_mod: object,
    ) -> None:
        # Regression: HTTP Basic-auth usernames (``GERRIT_USERNAME``,
        # ``CONFLUENCE_USERNAME``) are paired identity material and
        # must not flow into shipping config even though they aren't
        # strictly tokens.  A bad allowlist edit pointing at one must
        # fail at resolver-entry time, before any .env reading.
        monkeypatch.setattr(
            gen_config_mod,
            "_CATEGORY_B_KEYS",
            {"GERRIT_USERNAME": "toolsets.gerrit.username"},
        )
        with pytest.raises(SystemExit) as exc_info:
            gen_config_mod.main()  # type: ignore[attr-defined]
        assert exc_info.value.code == 2

    @pytest.mark.parametrize(
        ("env_var", "yaml_path"),
        [
            ("AWS_CREDENTIALS", "toolsets.aws.credentials"),
            ("SESSION_COOKIE", "toolsets.session.cookie"),
        ],
    )
    def test_defense_in_depth_suffixes_blocked(
        self,
        sandbox_cwd: Path,
        monkeypatch: pytest.MonkeyPatch,
        gen_config_mod: object,
        env_var: str,
        yaml_path: str,
    ) -> None:
        # Defence in depth: ``_CREDENTIALS`` and ``_COOKIE`` aren't in
        # use today but guard against future integrations whose naming
        # doesn't otherwise match the list.
        monkeypatch.setattr(
            gen_config_mod,
            "_CATEGORY_B_KEYS",
            {env_var: yaml_path},
        )
        with pytest.raises(SystemExit) as exc_info:
            gen_config_mod.main()  # type: ignore[attr-defined]
        assert exc_info.value.code == 2

    def test_secret_suffix_match_is_case_insensitive(
        self,
        sandbox_cwd: Path,
        monkeypatch: pytest.MonkeyPatch,
        gen_config_mod: object,
    ) -> None:
        # Operators sometimes type env vars in lowercase out of habit
        # (``gerrit_username=…``).  The guard must still catch it.
        monkeypatch.setattr(
            gen_config_mod,
            "_CATEGORY_B_KEYS",
            {"gerrit_username": "toolsets.gerrit.username"},
        )
        with pytest.raises(SystemExit) as exc_info:
            gen_config_mod.main()  # type: ignore[attr-defined]
        assert exc_info.value.code == 2


# ── No hostname leak ────────────────────────────────────────────────


class TestNoHostnameLeak:
    """Confirm ``config/defaults.yaml`` contains no internal hostnames.

    This is the §16.1 *No hostname leak in public source* check.  The
    test fails if somebody reintroduces a leak.
    """

    def test_defaults_yaml_has_no_internal_hostnames(self) -> None:
        repo_root: Path = _SCRIPT_PATH.parent.parent
        defaults = (repo_root / "config" / "defaults.yaml").read_text()
        # Only public SaaS hostnames are permitted.  ``jirasw.nvidia.com``
        # and ``nvidia.atlassian.net`` are public and documented as
        # acceptable in design_m2b.md §5.3.4.
        forbidden_substrings = (
            "gitlab-master.nvidia.com",
            "git-av.nvidia.com",
            "10.120.",
            "internal.nvidia.com",
        )
        for substr in forbidden_substrings:
            assert substr not in defaults, (
                f"{substr!r} leaked into config/defaults.yaml — "
                "move it to .env and let gen_config.py merge it in."
            )


# ── Full-resolver integration ───────────────────────────────────────

# Synthetic .env used by the integration class below.  Deliberately
# uses public, non-owned ``.test.example.com`` / ``.example.com``
# hostnames and RFC 1918 CIDRs so the test needs no ground-truth
# values (no internal NVIDIA URLs, no operator secrets).
#
# Every category-B key on ``gen_config._CATEGORY_B_KEYS`` is set
# here.  If a new key is added to the allowlist, this string must
# grow — the ``test_every_category_b_key_is_covered`` regression
# guard below turns that silent drift into a visible test failure.
_SYNTHETIC_ENV: str = (
    "GITLAB_URL=https://gitlab.test.example.com\n"
    "GERRIT_URL=https://gerrit.test.example.com/r/a\n"
    "GIT_CLONE_ALLOWED_HOSTS=gitlab.test.example.com,gerrit.test.example.com,github.com\n"
)


class TestFullyResolvedConfig:
    """Integration: populated (synthetic) .env → fully-valid resolved config.

    ``TestResolverHappyPath`` above covers individual field flows;
    this class asserts the *aggregate* output is "production-ready":
    every section the app's loader consumes is present, no
    placeholder remains in a resolver-written field, and the output
    round-trips through :meth:`AppConfig.load` without error.

    Uses only synthetic hostnames / CIDRs — no internal NVIDIA values
    or real operator secrets are required for the tests to pass.
    """

    # Top-level YAML sections the loader expects.  Any new dataclass
    # section on ``AppConfig`` that lands on ``_DIRECT_SECTIONS`` must
    # show up here too or the coverage check below fails.
    _EXPECTED_TOP_LEVEL: tuple[str, ...] = (
        "inference",
        "orchestrator",
        "agent_loop",
        "nmb",
        "log",
        "audit",
        "coding",
        "skills",
        "toolsets",
    )

    # Per-service entries under ``toolsets:``.  Mirrors
    # ``nemoclaw_escapades.config._TOOLSET_SECTIONS``.
    _EXPECTED_TOOLSETS: tuple[str, ...] = (
        "jira",
        "gitlab",
        "gerrit",
        "confluence",
        "slack_search",
        "web_search",
    )

    @staticmethod
    def _resolved(cwd: Path) -> dict[str, object]:
        """Return the resolved YAML parsed as a dict."""
        return yaml.safe_load((cwd / "config" / "orchestrator.resolved.yaml").read_text())

    def test_every_category_b_key_is_covered_by_synthetic_env(
        self,
        gen_config_mod: object,
    ) -> None:
        """Regression: a new allowlist entry must extend ``_SYNTHETIC_ENV``.

        Without this guard, the integration tests below would silently
        drift off the allowlist when a new category-B knob is added —
        they'd still pass while failing to exercise the new field.
        """
        # Every allowlist key must appear (verbatim, at line start) in
        # the synthetic env string so the integration tests actually
        # populate it.
        allowlist: dict[str, str] = gen_config_mod._CATEGORY_B_KEYS  # type: ignore[attr-defined]
        missing = [k for k in allowlist if f"{k}=" not in _SYNTHETIC_ENV]
        assert not missing, (
            f"synthetic .env is missing category-B keys: {missing}. "
            "Extend _SYNTHETIC_ENV in tests/test_gen_config.py."
        )

    def test_every_category_b_field_is_populated(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        """Every field the resolver writes has a non-empty value."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_config_mod.main()  # type: ignore[attr-defined]

        resolved = self._resolved(sandbox_cwd)
        # Walk the exact dotted paths the resolver is responsible for
        # and confirm each one resolved to a non-empty string.
        allowlist: dict[str, str] = gen_config_mod._CATEGORY_B_KEYS  # type: ignore[attr-defined]
        for env_key, dotted in allowlist.items():
            node: object = resolved
            for part in dotted.split("."):
                assert isinstance(node, dict), f"dotted path {dotted!r} broken at {part!r}"
                assert part in node, f"dotted path {dotted!r} missing {part!r}"
                node = node[part]
            assert isinstance(node, str) and node, (
                f"{env_key} → {dotted} is empty / non-string: {node!r}"
            )

    def test_every_top_level_section_present(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        """The loader's ``_DIRECT_SECTIONS`` are all present in the output."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved = self._resolved(sandbox_cwd)

        missing_sections = [s for s in self._EXPECTED_TOP_LEVEL if s not in resolved]
        assert not missing_sections, f"resolved YAML missing top-level sections: {missing_sections}"

    def test_every_toolset_entry_present(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        """Every ``toolsets.<name>`` entry the loader expects is present."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved = self._resolved(sandbox_cwd)

        toolsets = resolved.get("toolsets") or {}
        assert isinstance(toolsets, dict)
        missing_toolsets = [t for t in self._EXPECTED_TOOLSETS if t not in toolsets]
        assert not missing_toolsets, f"toolsets missing: {missing_toolsets}"
        # Every toolset carries an ``enabled`` flag.
        for name in self._EXPECTED_TOOLSETS:
            entry = toolsets[name]
            assert isinstance(entry, dict)
            assert "enabled" in entry, f"toolsets.{name} missing 'enabled'"
            assert isinstance(entry["enabled"], bool)

    def test_every_service_url_is_populated(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        """Every service that declares a ``url`` field has it set and parseable.

        Category-A services (``jira``, ``confluence``) ship public
        SaaS URLs in ``defaults.yaml``.  Category-B services
        (``gitlab``, ``gerrit``) get their URL from the synthetic
        ``.env`` via the resolver.  In both cases the final value
        must parse to ``scheme://host[/path]``.
        """
        from urllib.parse import urlparse

        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved = self._resolved(sandbox_cwd)

        toolsets = resolved["toolsets"]  # type: ignore[index]
        for name in ("jira", "gitlab", "gerrit", "confluence"):
            entry = toolsets[name]
            assert "url" in entry, f"toolsets.{name} missing 'url'"
            url = entry["url"]
            assert isinstance(url, str) and url, f"toolsets.{name}.url is empty"
            parsed = urlparse(url)
            assert parsed.scheme in ("http", "https"), (
                f"toolsets.{name}.url has unexpected scheme: {url!r}"
            )
            assert parsed.hostname, f"toolsets.{name}.url has no hostname: {url!r}"

    def test_resolved_config_round_trips_through_appconfig(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The resolved YAML loads cleanly via ``AppConfig.load``.

        End-to-end proof that the resolver's output is consumable by
        the app: run the resolver, point ``AppConfig.load`` at the
        result (secrets supplied via env as production does), and
        assert the category-B values flowed through into the typed
        dataclass.
        """
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_config_mod.main()  # type: ignore[attr-defined]

        # Populate required secrets so ``_check_required_config``
        # passes.  We deliberately *don't* use ``require_slack=False``
        # — the point is the orchestrator-facing validation path.
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("INFERENCE_HUB_API_KEY", "test-key")

        from nemoclaw_escapades.config import AppConfig

        config = AppConfig.load(path=sandbox_cwd / "config" / "orchestrator.resolved.yaml")
        # Category-B URLs flowed through to the typed config.
        assert config.gitlab.url == "https://gitlab.test.example.com"
        assert config.gerrit.url == "https://gerrit.test.example.com/r/a"
        assert config.coding.git_clone_allowed_hosts == (
            "gitlab.test.example.com,gerrit.test.example.com,github.com"
        )
        # Category-A URLs (public SaaS) stayed at the shipped default.
        assert config.jira.url == "https://jirasw.nvidia.com"
        assert config.confluence.url == "https://nvidia.atlassian.net/wiki"
        # Top-level non-toolset sections loaded too.
        assert config.orchestrator.model
        assert config.inference.model
        assert config.agent_loop.max_tool_rounds > 0
        assert config.nmb.broker_url.startswith("ws://")
        assert config.audit.enabled is True

    def test_secrets_in_env_never_leak_into_resolved_output(
        self,
        sandbox_cwd: Path,
        gen_config_mod: object,
    ) -> None:
        """Secrets alongside category-B values stay out of the resolved YAML.

        Operators have both secrets and category-B keys in the same
        ``.env``.  The resolver's allowlist + forbidden-suffix guard
        must keep every secret value out of the resolved output, or
        the committed + image-shipped artefact leaks credentials.
        """
        env_body = _SYNTHETIC_ENV + (
            "SLACK_BOT_TOKEN=xoxb-DO-NOT-LEAK-THIS-TOKEN\n"
            "SLACK_APP_TOKEN=xapp-DO-NOT-LEAK-THIS-TOKEN\n"
            "INFERENCE_HUB_API_KEY=sk-DO-NOT-LEAK-THIS-KEY\n"
            "JIRA_AUTH=Basic DO-NOT-LEAK-THIS-HEADER\n"
            "GITLAB_TOKEN=glpat-DO-NOT-LEAK-THIS-TOKEN\n"
            "GERRIT_HTTP_PASSWORD=DO-NOT-LEAK-THIS-PASSWORD\n"
        )
        (sandbox_cwd / ".env").write_text(env_body)
        gen_config_mod.main()  # type: ignore[attr-defined]

        resolved_text = (sandbox_cwd / "config" / "orchestrator.resolved.yaml").read_text()
        # Category-B synthetic values flow through.
        assert "gitlab.test.example.com" in resolved_text
        # Every secret stays out.
        for forbidden in (
            "xoxb-DO-NOT-LEAK-THIS-TOKEN",
            "xapp-DO-NOT-LEAK-THIS-TOKEN",
            "sk-DO-NOT-LEAK-THIS-KEY",
            "Basic DO-NOT-LEAK-THIS-HEADER",
            "glpat-DO-NOT-LEAK-THIS-TOKEN",
            "DO-NOT-LEAK-THIS-PASSWORD",
        ):
            assert forbidden not in resolved_text, (
                f"secret leaked into resolved YAML: {forbidden!r}"
            )
