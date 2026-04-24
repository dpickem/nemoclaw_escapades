"""Runtime-environment detection for NemoClaw.

Multi-signal sandbox health check that runs once at startup.  The
OpenShell sandbox is the only supported runtime for the orchestrator
and coding sub-agent — a missing signal means the deployment is
broken, not that the process should silently fall back to defaults.
See ``docs/design_m2b.md`` §5.4 for the signal rationale.

Two classifications are produced:

- :class:`RuntimeEnvironment.SANDBOX` — running inside a healthy
  OpenShell sandbox (≥ ``_DEFAULT_SANDBOX_SIGNAL_THRESHOLD`` signals present).
- :class:`RuntimeEnvironment.INCONSISTENT` — signal mix below the
  threshold, almost always a deployment bug (policy drift, OpenShell
  version skew, half-applied configuration, or the app launched
  outside a sandbox).  ``main.py`` refuses to start in this state.

Exactly one I/O operation per signal; no network calls, no long
waits.  Detection runs once at startup before any config loading so
its result can inform logging and the startup self-check.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("runtime")


# ── Signal thresholds ────────────────────────────────────────────────

# Signal names the detector evaluates.  Kept as module-level constants
# so unit tests and log-inspection tooling have one source of truth.
_SIGNAL_OPENSHELL_ENV: str = "OPENSHELL_SANDBOX"
_SIGNAL_SANDBOX_DIR: str = "sandbox_dir_writable"
_SIGNAL_APP_SRC_PRESENT: str = "app_src_present"
_SIGNAL_HTTPS_PROXY: str = "https_proxy_env"
_SIGNAL_SSL_CERT_BUNDLE: str = "ssl_cert_bundle_env"
_SIGNAL_INFERENCE_DNS: str = "inference_local_resolves"

_ALL_SIGNALS: tuple[str, ...] = (
    _SIGNAL_OPENSHELL_ENV,
    _SIGNAL_SANDBOX_DIR,
    _SIGNAL_APP_SRC_PRESENT,
    _SIGNAL_HTTPS_PROXY,
    _SIGNAL_SSL_CERT_BUNDLE,
    _SIGNAL_INFERENCE_DNS,
)

# ``SANDBOX`` needs at least this many signals to match.  Sized to
# tolerate one or two flaky / missing signals (e.g. the DNS lookup or
# a proxy CA that hasn't been installed yet).  Below threshold →
# ``INCONSISTENT`` → refuse to start.
_DEFAULT_SANDBOX_SIGNAL_THRESHOLD: int = 4

# Test-only escape hatch: integration tests in
# ``tests/test_integration_coding_agent.py`` spawn the sub-agent as a
# subprocess on a dev laptop where only the three env-based signals
# (``OPENSHELL_SANDBOX``, ``HTTPS_PROXY``, ``SSL_CERT_FILE``) can be
# reliably faked — the path signals need root and the DNS signal needs
# ``inference.local`` to resolve.  Those tests lower the threshold via
# the env var below so the classifier still runs against real signals,
# just with a more permissive cut-off.  Production deployments MUST
# NOT set this env var — if you're seeing ``INCONSISTENT`` in prod,
# the sandbox is genuinely broken.
_SANDBOX_SIGNAL_THRESHOLD_ENV: str = "NEMOCLAW_SANDBOX_SIGNAL_THRESHOLD"


def _sandbox_signal_threshold() -> int:
    """Return the current SANDBOX signal threshold.

    Read per-call (not as an import-time constant) so ``monkeypatch``
    in unit tests and env-var changes between subprocess launches
    take effect immediately.  See :data:`_SANDBOX_SIGNAL_THRESHOLD_ENV`
    for the test-only override contract.
    """
    raw = os.environ.get(_SANDBOX_SIGNAL_THRESHOLD_ENV)
    if raw is None:
        return _DEFAULT_SANDBOX_SIGNAL_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_SANDBOX_SIGNAL_THRESHOLD


# Paths / hostnames the signals inspect.
_SANDBOX_DIR: Path = Path("/sandbox")
_APP_SRC_DIR: Path = Path("/app/src")
_INFERENCE_DNS_NAME: str = "inference.local"


class RuntimeEnvironment(StrEnum):
    """Classification result from :func:`detect_runtime_environment`.

    ``StrEnum`` makes the member values ergonomic in structured logs
    and equality comparisons with plain strings.  Mirrors
    :class:`nemoclaw_escapades.models.types.ErrorCategory` /
    :class:`nemoclaw_escapades.models.types.MessageRole` so the
    codebase stays internally consistent.
    """

    SANDBOX = "SANDBOX"
    INCONSISTENT = "INCONSISTENT"


class SandboxConfigurationError(RuntimeError):
    """Raised when :func:`detect_runtime_environment` returns ``INCONSISTENT``.

    Carries the signal breakdown so callers can present a structured
    log entry to the operator (see ``main.py``).
    """

    def __init__(self, report: RuntimeReport) -> None:
        self.report = report
        super().__init__(
            "Sandbox detection inconsistent — "
            f"{len(report.signals_present)}/{len(_ALL_SIGNALS)} signals present; "
            "refusing to start.  "
            f"Missing: {', '.join(report.signals_missing) or 'none'}.  "
            f"Present: {', '.join(report.signals_present) or 'none'}.  "
            "Run `make status` to inspect providers; recreate sandbox with "
            "`make run-local-sandbox`."
        )


@dataclass(frozen=True)
class RuntimeReport:
    """Structured result of :func:`detect_runtime_environment`.

    Attributes:
        classification: One of the :class:`RuntimeEnvironment` values.
        signals_present: Names of signals that evaluated to ``True``.
        signals_missing: Names of signals that evaluated to ``False``.
        likely_cause: Human-readable hint about what typically produces
            this signal mix, included in the ``INCONSISTENT`` log entry.
    """

    classification: RuntimeEnvironment
    signals_present: tuple[str, ...]
    signals_missing: tuple[str, ...]
    likely_cause: str = ""


# ── Signal evaluators ────────────────────────────────────────────────


def _check_openshell_env() -> bool:
    """``OPENSHELL_SANDBOX`` env var is set (self-identification)."""
    return bool(os.environ.get("OPENSHELL_SANDBOX"))


def _check_sandbox_dir_writable() -> bool:
    """``/sandbox`` directory exists and we can create a file in it.

    The PVC is mounted read-write for the sandbox user and read-only
    for anyone else.  The ``os.access`` check is cheap and doesn't
    actually write — we don't want to leave litter behind.
    """
    return _SANDBOX_DIR.is_dir() and os.access(_SANDBOX_DIR, os.W_OK)


def _check_app_src_present() -> bool:
    """``/app/src`` exists where the Dockerfile copied the app in.

    Presence alone is evidence the container was built by our
    Dockerfile.  Landlock / filesystem policy enforce read-only-ness
    externally; this signal just verifies the image shape is what we
    expect.
    """
    return _APP_SRC_DIR.is_dir()


def _check_https_proxy_env() -> bool:
    """One of the L7 proxy env vars is set.

    OpenShell injects ``HTTPS_PROXY`` (uppercase) via its credential
    resolver.  Some base images set ``https_proxy`` (lowercase).
    Accept either to stay robust against minor env-var name drift.
    """
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))


def _check_ssl_cert_bundle_env() -> bool:
    """Either ``SSL_CERT_FILE`` or ``REQUESTS_CA_BUNDLE`` is set.

    OpenShell installs its CA bundle so the proxy's TLS termination
    is trusted by in-sandbox clients.  The env var pointing at the
    bundle is therefore a strong sandbox signal.
    """
    return bool(os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))


def _check_inference_dns_resolves(timeout_s: float = 0.5) -> bool:
    """``inference.local`` resolves to an IP address.

    Only available inside an OpenShell sandbox (the gateway's DNS
    stub publishes it).  Bounded by a short timeout so a flaky
    resolver doesn't stall startup.  Any DNS error → ``False``.
    """
    # ``socket.gethostbyname`` honours the system resolver but has no
    # per-call timeout parameter.  The global default socket timeout
    # applies to the underlying resolver socket on systems where the
    # resolver uses UDP; at worst this blocks ``timeout_s`` seconds.
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout_s)
        socket.gethostbyname(_INFERENCE_DNS_NAME)
        return True
    except (OSError, socket.gaierror, socket.herror, TimeoutError):
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


def _evaluate_signal(name: str) -> bool:
    """Dispatch on signal name to the right evaluator.

    Intentionally uses module-level function lookups (``globals()``)
    rather than a dict of references so ``monkeypatch.setattr`` at
    test time actually takes effect.  Evaluators are by convention
    named ``_check_<something>()`` and mapped to signal names below.
    """
    # Mapping is inline so patching the functions shows up here.
    if name == _SIGNAL_OPENSHELL_ENV:
        return _check_openshell_env()
    if name == _SIGNAL_SANDBOX_DIR:
        return _check_sandbox_dir_writable()
    if name == _SIGNAL_APP_SRC_PRESENT:
        return _check_app_src_present()
    if name == _SIGNAL_HTTPS_PROXY:
        return _check_https_proxy_env()
    if name == _SIGNAL_SSL_CERT_BUNDLE:
        return _check_ssl_cert_bundle_env()
    if name == _SIGNAL_INFERENCE_DNS:
        return _check_inference_dns_resolves()
    raise KeyError(f"Unknown runtime signal: {name!r}")


# ── Classification ──────────────────────────────────────────────────


def _classify(signals_present: tuple[str, ...]) -> tuple[RuntimeEnvironment, str]:
    """Map a signal count to a :class:`RuntimeEnvironment` classification.

    Returns a ``(classification, likely_cause)`` tuple.  The
    ``likely_cause`` string is embedded in the ``INCONSISTENT`` log
    entry so operators see a diagnostic hint without needing to run
    ``make status`` first.
    """
    if len(signals_present) >= _sandbox_signal_threshold():
        return RuntimeEnvironment.SANDBOX, ""

    # Below threshold — the process must either be outside a sandbox
    # or inside a broken one.  Distinguish the two most common failure
    # shapes so the operator log points at the right fix.
    has_env_signals = (
        _SIGNAL_OPENSHELL_ENV in signals_present
        or _SIGNAL_HTTPS_PROXY in signals_present
        or _SIGNAL_SSL_CERT_BUNDLE in signals_present
    )
    has_path_signals = (
        _SIGNAL_SANDBOX_DIR in signals_present or _SIGNAL_APP_SRC_PRESENT in signals_present
    )
    if has_env_signals and not has_path_signals:
        cause = "sandbox env vars present but paths missing (not inside a sandbox)"
    elif has_path_signals and not has_env_signals:
        cause = (
            "image built correctly but gateway didn't inject env "
            "(recreate with `make run-local-sandbox`)"
        )
    elif len(signals_present) == 0:
        cause = "no sandbox signals present — run via `make run-local-sandbox`"
    else:
        cause = "OpenShell version drift or gateway misconfigured"
    return RuntimeEnvironment.INCONSISTENT, cause


def detect_runtime_environment() -> RuntimeReport:
    """Evaluate all runtime signals and classify the environment.

    Returns:
        :class:`RuntimeReport` with classification, lists of present /
        missing signals, and a ``likely_cause`` hint for the
        ``INCONSISTENT`` case.

    Note:
        Safe to call multiple times.  Signals are re-evaluated on
        each call because env vars and filesystem state can change
        between invocations (e.g. live config reload).
    """
    present: list[str] = []
    missing: list[str] = []
    for name in _ALL_SIGNALS:
        if _evaluate_signal(name):
            present.append(name)
        else:
            missing.append(name)
    classification, likely_cause = _classify(tuple(present))
    return RuntimeReport(
        classification=classification,
        signals_present=tuple(present),
        signals_missing=tuple(missing),
        likely_cause=likely_cause,
    )
