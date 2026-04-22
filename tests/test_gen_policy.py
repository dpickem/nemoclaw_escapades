"""Unit tests for :mod:`scripts.gen_policy`.

Covers the §16.1-style invariants for the policy resolver:

- **Hostname substitution** — ``GITLAB_URL`` / ``GERRIT_URL`` in
  ``.env`` overwrite the ``host: ""`` placeholders in the base
  policy.  Order matters: the substitution runs before the
  ``allowed_ips`` injection so the SSRF-bypass matcher sees the
  final host.
- **Fail-closed** — missing / malformed URLs leave the placeholder
  empty so OpenShell rejects the policy at apply time instead of
  producing a silently-mis-routed rule.
- **No hostname leak** — the committed base policy contains no
  internal NVIDIA hostnames.  Regression guard paired with the
  corresponding check on ``config/defaults.yaml`` in
  ``tests/test_gen_config.py``.

``gen_policy`` is a standalone script, not a package module, so
tests load it via ``importlib`` and chdir into a tmp tree that
mirrors the repo layout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT_PATH: Path = Path(__file__).resolve().parent.parent / "scripts" / "gen_policy.py"


def _load_gen_policy_module() -> object:
    """Import ``scripts/gen_policy.py`` as a module for direct calls.

    Mirrors the loader in ``tests/test_gen_config.py`` — ``scripts/``
    isn't a package so we load by file path.
    """
    spec = importlib.util.spec_from_file_location("gen_policy", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_policy"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen_policy_mod() -> object:
    return _load_gen_policy_module()


@pytest.fixture
def sandbox_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run the resolver inside a temp directory with the real base policy.

    The resolver reads ``policies/orchestrator.yaml`` and writes
    ``policies/orchestrator.resolved.yaml`` — both paths are relative
    to the cwd (not the script's own dir), matching the Makefile's
    "run-from-repo-root" convention.  Tests chdir into a fresh
    ``tmp_path`` and populate it with the real base policy so any
    schema drift in the base is caught by the end-to-end assertions.
    """
    repo_root: Path = _SCRIPT_PATH.parent.parent
    (tmp_path / "policies").mkdir()
    base = repo_root / "policies" / "orchestrator.yaml"
    (tmp_path / "policies" / "orchestrator.yaml").write_text(base.read_text())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _resolved_policy(cwd: Path) -> dict[str, object]:
    """Helper — parse the resolved YAML the resolver just wrote."""
    return yaml.safe_load(
        (cwd / "policies" / "orchestrator.resolved.yaml").read_text()
    )


def _network_policy(doc: dict[str, object], name: str) -> dict[str, object]:
    """Helper — return the ``network_policies.<name>`` sub-doc."""
    nps = doc.get("network_policies") or {}
    assert isinstance(nps, dict)
    entry = nps.get(name)
    assert isinstance(entry, dict), f"missing network_policy {name!r}"
    return entry


# ── Hostname substitution — happy path ──────────────────────────────


class TestHostSubstitution:
    """``GITLAB_URL`` / ``GERRIT_URL`` replace ``host: ""`` placeholders."""

    def test_populated_env_fills_host_placeholders(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        (sandbox_cwd / ".env").write_text(
            "GITLAB_URL=https://gitlab.example.com\n"
            "GERRIT_URL=https://gerrit.example.com/r/a\n"
        )
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved = _resolved_policy(sandbox_cwd)
        gitlab = _network_policy(resolved, "gitlab")
        gerrit = _network_policy(resolved, "gerrit")

        assert gitlab["endpoints"][0]["host"] == "gitlab.example.com"  # type: ignore[index]
        # URL with ``/r/a`` path — path is stripped, host only.
        assert gerrit["endpoints"][0]["host"] == "gerrit.example.com"  # type: ignore[index]

    def test_empty_env_leaves_placeholders_failclosed(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """No ``.env`` → placeholders stay empty.

        OpenShell rejects empty-host policies at apply time, which is
        the correct fail-closed posture for an OSS consumer without a
        ``.env``.  Asserting the resolved file doesn't spuriously fill
        in a host is the regression guard against silently-breaking
        the "no internal hostnames in the image" invariant.
        """
        # Deliberately no .env file.
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved = _resolved_policy(sandbox_cwd)
        gitlab = _network_policy(resolved, "gitlab")
        gerrit = _network_policy(resolved, "gerrit")

        assert gitlab["endpoints"][0]["host"] == ""  # type: ignore[index]
        assert gerrit["endpoints"][0]["host"] == ""  # type: ignore[index]

    def test_partial_env_fills_only_set_urls(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        (sandbox_cwd / ".env").write_text(
            "GITLAB_URL=https://gitlab.example.com\n"
            # GERRIT_URL deliberately missing
        )
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved = _resolved_policy(sandbox_cwd)
        assert _network_policy(resolved, "gitlab")["endpoints"][0]["host"] == (  # type: ignore[index]
            "gitlab.example.com"
        )
        assert _network_policy(resolved, "gerrit")["endpoints"][0]["host"] == ""  # type: ignore[index]

    def test_malformed_url_without_scheme_is_ignored(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """A bare hostname (no ``scheme://``) is not treated as a URL.

        Safety: ``urlparse("gitlab.example.com").hostname`` returns
        ``None`` because the whole string is parsed as ``path``.  We
        explicitly require a scheme so accidental mis-typing of the
        env var (``GITLAB_URL=gitlab.example.com``) doesn't produce a
        garbled policy — the placeholder stays empty, operator sees
        the fail-closed output, fixes the env var.
        """
        (sandbox_cwd / ".env").write_text("GITLAB_URL=gitlab.example.com\n")
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved = _resolved_policy(sandbox_cwd)
        assert _network_policy(resolved, "gitlab")["endpoints"][0]["host"] == ""  # type: ignore[index]


# ── extract_host helper — unit-level ────────────────────────────────


class TestExtractHost:
    """Direct unit tests for ``_extract_host``."""

    def test_full_url(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        assert extract("https://gitlab.example.com") == "gitlab.example.com"

    def test_url_with_path(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        assert extract("https://gerrit.example.com/r/a") == "gerrit.example.com"

    def test_url_with_port(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        # Port stripped — the policy templates carry their own port
        # field; substitution only touches host.
        assert extract("https://gitlab.example.com:8443") == "gitlab.example.com"

    def test_bare_hostname_returns_empty(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        assert extract("gitlab.example.com") == ""

    def test_empty_returns_empty(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        assert extract("") == ""

    def test_whitespace_stripped(self, gen_policy_mod: object) -> None:
        extract = gen_policy_mod._extract_host  # type: ignore[attr-defined]
        assert extract("  https://gitlab.example.com  ") == "gitlab.example.com"


# ── Integration: host substitution + SSRF injection ordering ───────


class TestSubstitutionComposesWithAllowedIps:
    """Substitution runs first so ``allowed_ips`` matches the real host."""

    def test_substituted_host_receives_allowed_ips(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """End-to-end: YAML host gets replaced, then SSRF bypass matches.

        This is the ordering invariant that makes the resolver sound —
        if ``allowed_ips`` ran before substitution, it would try to
        match on ``""`` and never fire.  Regression guard: swapping
        the two phases would break this test.
        """
        (sandbox_cwd / ".env").write_text(
            "GITLAB_URL=https://gitlab.example.com\n"
            "GERRIT_URL=https://gerrit.example.com\n"
            "ENDPOINTS_NEEDING_ALLOWED_IPS=gitlab.example.com,gerrit.example.com\n"
            "ALLOWED_IPS=10.0.0.0/24,10.1.0.0/24\n"
        )
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved = _resolved_policy(sandbox_cwd)
        for name in ("gitlab", "gerrit"):
            ep = _network_policy(resolved, name)["endpoints"][0]  # type: ignore[index]
            assert ep["host"].endswith(".example.com"), ep
            assert ep.get("allowed_ips") == ["10.0.0.0/24", "10.1.0.0/24"], ep


# ── No hostname leak in public source ──────────────────────────────


class TestNoHostnameLeak:
    """The committed base policy must not contain internal hostnames.

    Paired with ``tests/test_gen_config.py::TestNoHostnameLeak`` which
    covers ``config/defaults.yaml``.  Together they enforce §5.3's
    "public source ships no category-B values" invariant across both
    the application config layer and the sandbox policy layer.
    """

    def test_policies_orchestrator_yaml_has_no_internal_hostnames(self) -> None:
        repo_root: Path = _SCRIPT_PATH.parent.parent
        base = (repo_root / "policies" / "orchestrator.yaml").read_text()
        # Public SaaS hostnames are OK to ship — ``jirasw.nvidia.com``
        # is NVIDIA's public Jira, ``nvidia.atlassian.net`` is
        # Atlassian Cloud.  Both documented in design_m2b.md §5.3.4
        # as acceptable values in the base policy.
        forbidden_substrings = (
            "gitlab-master.nvidia.com",
            "git-av.nvidia.com",
            "10.120.",
            "internal.nvidia.com",
        )
        for substr in forbidden_substrings:
            assert substr not in base, (
                f"{substr!r} leaked into policies/orchestrator.yaml — "
                "move it to .env and let gen_policy.py substitute it in."
            )
