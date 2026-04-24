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
import ipaddress
import re
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
    return yaml.safe_load((cwd / "policies" / "orchestrator.resolved.yaml").read_text())


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
            "GITLAB_URL=https://gitlab.example.com\nGERRIT_URL=https://gerrit.example.com/r/a\n"
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


# ── Full-resolver integration ───────────────────────────────────────

# Synthetic .env used by the integration class below.  Deliberately
# uses public ``.test.example.com`` hostnames and RFC 1918 CIDRs so
# the test needs no ground-truth values (no internal NVIDIA URLs,
# no operator-specific subnets).
_SYNTHETIC_ENV: str = (
    "GITLAB_URL=https://gitlab.test.example.com\n"
    "GERRIT_URL=https://gerrit.test.example.com/r/a\n"
    "ENDPOINTS_NEEDING_ALLOWED_IPS=gitlab.test.example.com,gerrit.test.example.com\n"
    "ALLOWED_IPS=10.0.0.0/24,10.1.0.0/24,192.168.0.0/16\n"
)

# Valid ``endpoint.protocol`` values accepted by OpenShell's policy
# schema.  Keep in sync with the values the base ``orchestrator.yaml``
# uses — a new value in the base must also appear here or the
# :class:`TestFullyResolvedPolicy` schema check flags unknown values.
_VALID_PROTOCOLS: frozenset[str] = frozenset({"rest", "tcp"})

# Valid ``endpoint.enforcement`` values.
_VALID_ENFORCEMENTS: frozenset[str] = frozenset({"enforce", "warn", "disabled"})

# Valid ``endpoint.access`` values.  Used when the endpoint grants a
# CONNECT tunnel rather than HTTP rule-based enforcement.
_VALID_ACCESS: frozenset[str] = frozenset({"full", "read_only"})

# RFC-1123-ish hostname regex.  Accepts plain FQDNs
# (``gitlab.example.com``), glob prefixes (``*.slack.com``), and
# single-label hostnames (``inference.local``).  Not a full DNS
# validator — just enough to catch obvious garbage like empty
# strings, embedded whitespace, or dotted-decimal placeholders.
_HOSTNAME_RE: re.Pattern[str] = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$",
    re.IGNORECASE,
)


class TestFullyResolvedPolicy:
    """Integration: populated (synthetic) .env → fully-valid resolved policy.

    ``TestHostSubstitution`` and ``TestSubstitutionComposesWithAllowedIps``
    above cover individual flows; this class asserts the *aggregate*
    output is "production-ready":

    - every endpoint has a non-empty, well-shaped host;
    - every endpoint that requires SSRF-bypass CIDRs has them;
    - every CIDR, port, protocol, and enforcement value is valid;
    - the resolved YAML carries every required top-level section.

    Uses only synthetic hostnames and CIDRs — no internal NVIDIA
    values are required for the tests to pass.
    """

    # Top-level keys the resolved policy must always carry.
    _REQUIRED_TOP_LEVEL: tuple[str, ...] = (
        "version",
        "filesystem_policy",
        "landlock",
        "process",
        "network_policies",
    )

    def test_every_endpoint_host_is_non_empty_and_wellshaped(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Every ``network_policies.<name>.endpoints[i].host`` is valid."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        network_policies = resolved.get("network_policies") or {}
        assert isinstance(network_policies, dict)
        for name, policy_entry in network_policies.items():
            assert isinstance(policy_entry, dict), name
            endpoints = policy_entry.get("endpoints") or []
            assert endpoints, f"{name} has no endpoints"
            for i, ep in enumerate(endpoints):
                assert isinstance(ep, dict), f"{name}.endpoints[{i}]"
                host = ep.get("host")
                assert isinstance(host, str), f"{name}.endpoints[{i}].host not a string: {host!r}"
                assert host, f"{name}.endpoints[{i}].host is empty"
                assert _HOSTNAME_RE.match(host), f"{name}.endpoints[{i}].host malformed: {host!r}"

    def test_every_endpoint_port_is_valid(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """All ports are integers within the valid TCP/UDP range."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        for name, policy_entry in resolved["network_policies"].items():  # type: ignore[index]
            for i, ep in enumerate(policy_entry["endpoints"]):  # type: ignore[index]
                port = ep.get("port")
                assert isinstance(port, int), f"{name}.endpoints[{i}].port not int: {port!r}"
                assert 1 <= port <= 65535, f"{name}.endpoints[{i}].port out of range: {port}"

    def test_every_endpoint_protocol_and_enforcement_is_valid(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Every endpoint uses a known protocol / enforcement / access value.

        Regression guard: a typo in the base policy (e.g. ``protocoll:
        rest``) would make OpenShell reject the policy at apply time.
        Catching it in test is cheaper than finding it in deploy.
        """
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        for name, policy_entry in resolved["network_policies"].items():  # type: ignore[index]
            for i, ep in enumerate(policy_entry["endpoints"]):  # type: ignore[index]
                # At least one of ``protocol`` / ``access`` must be set
                # — OpenShell rejects an endpoint with neither.
                assert "protocol" in ep or "access" in ep, (
                    f"{name}.endpoints[{i}] has neither 'protocol' nor 'access'"
                )
                if "protocol" in ep:
                    assert ep["protocol"] in _VALID_PROTOCOLS, (
                        f"{name}.endpoints[{i}].protocol invalid: {ep['protocol']!r}"
                    )
                if "enforcement" in ep:
                    assert ep["enforcement"] in _VALID_ENFORCEMENTS, (
                        f"{name}.endpoints[{i}].enforcement invalid: {ep['enforcement']!r}"
                    )
                if "access" in ep:
                    assert ep["access"] in _VALID_ACCESS, (
                        f"{name}.endpoints[{i}].access invalid: {ep['access']!r}"
                    )

    def test_rfc1918_endpoints_have_valid_allowed_ips(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Every host in ``ENDPOINTS_NEEDING_ALLOWED_IPS`` has CIDRs set.

        And every CIDR is well-formed according to the ``ipaddress``
        stdlib module.  ``strict=True`` rejects host-bits set (e.g.
        ``10.0.0.1/24``) — fail-closed against sloppy edits.
        """
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        # Reconstruct the expected "needs allowed_ips" set from the
        # synthetic env to keep the assertion data-driven.
        needs_ips = {
            line.split("=", 1)[1].strip()
            for line in _SYNTHETIC_ENV.splitlines()
            if line.startswith("ENDPOINTS_NEEDING_ALLOWED_IPS=")
        }.pop().split(",")
        for want_host in needs_ips:
            # Find the endpoint by host across all network_policies.
            matches: list[dict[str, object]] = []
            for policy_entry in resolved["network_policies"].values():  # type: ignore[index]
                for ep in policy_entry["endpoints"]:  # type: ignore[index]
                    if ep.get("host") == want_host:
                        matches.append(ep)
            assert matches, (
                f"{want_host!r} listed in ENDPOINTS_NEEDING_ALLOWED_IPS "
                "but no endpoint matched in resolved policy"
            )
            for ep in matches:
                allowed_ips = ep.get("allowed_ips")
                assert isinstance(allowed_ips, list) and allowed_ips, (
                    f"host={want_host!r} missing / empty allowed_ips: {ep}"
                )
                for cidr in allowed_ips:
                    assert isinstance(cidr, str), (
                        f"host={want_host!r} allowed_ips entry not str: {cidr!r}"
                    )
                    try:
                        ipaddress.ip_network(cidr, strict=True)
                    except ValueError as exc:
                        pytest.fail(f"host={want_host!r} has invalid CIDR {cidr!r}: {exc}")

    def test_required_top_level_sections_present(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """The resolved policy carries every OpenShell-required top-level key."""
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        missing = [k for k in self._REQUIRED_TOP_LEVEL if k not in resolved]
        assert not missing, f"resolved policy missing top-level keys: {missing}"
        # ``version: 1`` is the only shape OpenShell accepts today.
        assert resolved["version"] == 1

    def test_filesystem_policy_paths_are_absolute(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """``filesystem_policy.{read_only,read_write}`` contain absolute paths.

        OpenShell treats relative paths as a misconfiguration —
        catching them in test keeps the "it applied but silently
        matched nothing" failure mode out of production.
        """
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        fs = resolved["filesystem_policy"]  # type: ignore[index]
        assert isinstance(fs, dict)
        for key in ("read_only", "read_write"):
            paths = fs.get(key) or []
            assert paths, f"filesystem_policy.{key} is empty"
            for p in paths:
                assert isinstance(p, str), f"{key} entry not str: {p!r}"
                assert p.startswith("/"), f"filesystem_policy.{key} has non-absolute path: {p!r}"

    def test_every_endpoint_has_at_least_one_binary(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Each network_policy declares the binaries permitted to dial it.

        OpenShell requires a non-empty ``binaries`` list per policy —
        without it, no process is authorised to use the endpoint and
        every outbound call fails at proxy level.  Guard against a
        future edit that drops the list by mistake.
        """
        (sandbox_cwd / ".env").write_text(_SYNTHETIC_ENV)
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        for name, policy_entry in resolved["network_policies"].items():  # type: ignore[index]
            binaries = policy_entry.get("binaries") or []
            assert binaries, f"{name} has no binaries"
            for b in binaries:
                assert isinstance(b, dict) and "path" in b, f"{name} binary entry malformed: {b!r}"
                assert isinstance(b["path"], str) and b["path"].startswith("/"), (
                    f"{name} binary.path not absolute: {b['path']!r}"
                )

    def test_empty_env_leaves_a_failclosed_resolved_policy(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Complement to the fully-populated run: with no ``.env`` at all,
        every category-B host stays empty and ``allowed_ips`` is never
        injected.  This mirrors an OSS consumer's first run and must
        NOT raise — it produces a file OpenShell will reject at apply
        time (the correct fail-closed posture).
        """
        # Deliberately no .env written.
        gen_policy_mod.main()  # type: ignore[attr-defined]
        resolved = _resolved_policy(sandbox_cwd)

        for name in ("gitlab", "gerrit"):
            ep = _network_policy(resolved, name)["endpoints"][0]  # type: ignore[index]
            assert ep["host"] == ""
            assert "allowed_ips" not in ep

    def test_no_secret_leaks_into_resolved_policy(
        self,
        sandbox_cwd: Path,
        gen_policy_mod: object,
    ) -> None:
        """Tokens alongside substitution vars stay out of the resolved policy.

        The policy resolver has a narrower surface than the config
        resolver (only reads ``GITLAB_URL`` / ``GERRIT_URL`` /
        ``ENDPOINTS_NEEDING_ALLOWED_IPS`` / ``ALLOWED_IPS``), but the
        same fail-closed discipline applies: secrets in the same
        ``.env`` must never appear in the resolved output.
        """
        env_body = _SYNTHETIC_ENV + (
            "SLACK_BOT_TOKEN=xoxb-DO-NOT-LEAK\n"
            "INFERENCE_HUB_API_KEY=sk-DO-NOT-LEAK\n"
            "GITLAB_TOKEN=glpat-DO-NOT-LEAK\n"
            "GERRIT_HTTP_PASSWORD=DO-NOT-LEAK-PW\n"
        )
        (sandbox_cwd / ".env").write_text(env_body)
        gen_policy_mod.main()  # type: ignore[attr-defined]

        resolved_text = (sandbox_cwd / "policies" / "orchestrator.resolved.yaml").read_text()
        # Category-B synthetic values flow through.
        assert "gitlab.test.example.com" in resolved_text
        # Every secret stays out.
        for forbidden in (
            "xoxb-DO-NOT-LEAK",
            "sk-DO-NOT-LEAK",
            "glpat-DO-NOT-LEAK",
            "DO-NOT-LEAK-PW",
        ):
            assert forbidden not in resolved_text, (
                f"secret leaked into resolved policy: {forbidden!r}"
            )
