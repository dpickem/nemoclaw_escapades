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
            "SOME_OTHER_VAR=definitely-not-in-allowlist\n"
            "GIT_CLONE_ALLOWED_HOSTS=github.com\n"
        )
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved_text = (
            sandbox_cwd / "config" / "orchestrator.resolved.yaml"
        ).read_text()
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
        (sandbox_cwd / ".env").write_text(
            "GIT_CLONE_ALLOWED_HOSTS=github.com\n"
        )
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
        (sandbox_cwd / ".env").write_text(
            "GITLAB_URL=\n"
            "GERRIT_URL=\n"
            "GIT_CLONE_ALLOWED_HOSTS=\n"
        )
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
        (sandbox_cwd / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-secret\n"
            "JIRA_AUTH=Basic secret\n"
        )
        gen_config_mod.main()  # type: ignore[attr-defined]
        resolved_text = (
            sandbox_cwd / "config" / "orchestrator.resolved.yaml"
        ).read_text()
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
