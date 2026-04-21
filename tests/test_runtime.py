"""Unit tests for :mod:`nemoclaw_escapades.runtime`.

Covers the multi-signal detector and ``SandboxConfigurationError``
error surface — the §16.1 entries *Sandbox detection — LOCAL_DEV*,
*Sandbox detection — SANDBOX*, *Sandbox detection — INCONSISTENT*,
and *Startup self-check*.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nemoclaw_escapades.runtime import (
    RuntimeEnvironment,
    SandboxConfigurationError,
    detect_runtime_environment,
)


# ── Env & path helpers ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var the detector reads so tests start clean."""
    for key in (
        "OPENSHELL_SANDBOX",
        "HTTPS_PROXY",
        "https_proxy",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        monkeypatch.delenv(key, raising=False)


def _mock_all_signals(
    *,
    openshell_env: bool = False,
    sandbox_dir: bool = False,
    app_src: bool = False,
    https_proxy: bool = False,
    ssl_cert: bool = False,
    inference_dns: bool = False,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch every signal evaluator to a deterministic return value.

    Keeps tests fully hermetic — no filesystem or DNS access escapes
    to the host system.
    """
    if openshell_env:
        monkeypatch.setenv("OPENSHELL_SANDBOX", "1")
    if https_proxy:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    if ssl_cert:
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/openshell.pem")
    monkeypatch.setattr(
        "nemoclaw_escapades.runtime._check_sandbox_dir_writable",
        lambda: sandbox_dir,
    )
    monkeypatch.setattr(
        "nemoclaw_escapades.runtime._check_app_src_readonly",
        lambda: app_src,
    )
    monkeypatch.setattr(
        "nemoclaw_escapades.runtime._check_inference_dns_resolves",
        lambda timeout_s=0.5: inference_dns,
    )


# ── Classification ──────────────────────────────────────────────────


class TestClassification:
    """Signal counts mapped to ``RuntimeEnvironment`` values."""

    def test_all_signals_absent_is_local_dev(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_all_signals(monkeypatch=monkeypatch)  # all False
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.LOCAL_DEV
        assert report.signals_present == ()
        # No diagnostic needed — this is the healthy case.
        assert report.likely_cause == ""

    def test_one_signal_present_still_local_dev(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One signal = spurious, e.g. SSL_CERT_FILE set globally on
        # the dev's machine for some other tool.  Stay ``LOCAL_DEV``.
        _mock_all_signals(ssl_cert=True, monkeypatch=monkeypatch)
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.LOCAL_DEV

    def test_all_signals_present_is_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_all_signals(
            openshell_env=True,
            sandbox_dir=True,
            app_src=True,
            https_proxy=True,
            ssl_cert=True,
            inference_dns=True,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.SANDBOX
        assert len(report.signals_present) == 6
        assert report.signals_missing == ()

    def test_threshold_met_with_one_signal_flaky(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 5/6 signals — inference DNS flaky but everything else
        # points solidly at sandbox.  Should still classify SANDBOX.
        _mock_all_signals(
            openshell_env=True,
            sandbox_dir=True,
            app_src=True,
            https_proxy=True,
            ssl_cert=True,
            inference_dns=False,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.SANDBOX

    def test_env_signals_without_path_signals_is_inconsistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Env vars set (sandbox leaked into a local shell) but the
        # paths aren't there.  Classic misconfiguration.
        _mock_all_signals(
            openshell_env=True,
            https_proxy=True,
            ssl_cert=True,
            sandbox_dir=False,
            app_src=False,
            inference_dns=False,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.INCONSISTENT
        assert "leaked" in report.likely_cause

    def test_path_signals_without_env_signals_is_inconsistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Dockerfile copied the app in, /sandbox exists, but the
        # gateway never injected env vars.  Also a misconfiguration.
        _mock_all_signals(
            sandbox_dir=True,
            app_src=True,
            inference_dns=True,
            openshell_env=False,
            https_proxy=False,
            ssl_cert=False,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.INCONSISTENT
        assert "gateway didn't inject" in report.likely_cause

    def test_corporate_proxy_dev_is_local_dev_not_inconsistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression: a developer behind a corporate VPN / MITM proxy
        # usually has HTTPS_PROXY and SSL_CERT_FILE set in their shell
        # profile.  Two signals, no paths, no DNS, no
        # OPENSHELL_SANDBOX.  Must classify LOCAL_DEV — otherwise the
        # startup self-check refuses to run for every such developer.
        _mock_all_signals(
            https_proxy=True,
            ssl_cert=True,
            openshell_env=False,
            sandbox_dir=False,
            app_src=False,
            inference_dns=False,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.LOCAL_DEV
        assert report.likely_cause == ""

    def test_path_signal_alongside_benign_env_is_still_inconsistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Belt and braces: the corporate-proxy escape hatch must NOT
        # kick in when any sandbox-specific signal (path, DNS,
        # OPENSHELL_SANDBOX) is also present.  Here /sandbox is
        # writable in addition to HTTPS_PROXY + SSL_CERT_FILE — that's
        # genuinely suspicious and should still flag INCONSISTENT.
        _mock_all_signals(
            https_proxy=True,
            ssl_cert=True,
            sandbox_dir=True,
            openshell_env=False,
            app_src=False,
            inference_dns=False,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.INCONSISTENT


# ── Exception surface ───────────────────────────────────────────────


class TestSandboxConfigurationError:
    """``SandboxConfigurationError`` carries the diagnostic report."""

    def test_error_message_names_missing_signals(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_all_signals(
            openshell_env=True,
            https_proxy=True,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        assert report.classification is RuntimeEnvironment.INCONSISTENT
        err = SandboxConfigurationError(report)
        # Message lists present and missing signals.
        msg = str(err)
        assert "OPENSHELL_SANDBOX" in msg
        assert "https_proxy_env" in msg
        assert "sandbox_dir_writable" in msg
        assert "refusing to start" in msg

    def test_error_preserves_report_for_structured_log(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_all_signals(
            openshell_env=True,
            https_proxy=True,
            monkeypatch=monkeypatch,
        )
        report = detect_runtime_environment()
        err = SandboxConfigurationError(report)
        # ``main.py`` reads ``err.report`` to emit a structured log.
        assert err.report is report
        assert err.report.classification is RuntimeEnvironment.INCONSISTENT
