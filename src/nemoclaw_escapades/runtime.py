"""Runtime-environment detection for NemoClaw.

Replaces the single-signal ``OPENSHELL_SANDBOX`` env-var check with a
multi-signal evaluation so a broken sandbox deployment fails fast
instead of silently reverting to local-dev defaults.  See
``docs/design_m2b.md`` §5.4 for the motivation and signal rationale.

Three classifications are produced:

- :class:`RuntimeEnvironment.LOCAL_DEV` — running outside a sandbox
  (none or almost none of the sandbox signals present).
- :class:`RuntimeEnvironment.SANDBOX` — running inside a healthy
  OpenShell sandbox (≥ ``_SANDBOX_SIGNAL_THRESHOLD`` signals present).
- :class:`RuntimeEnvironment.INCONSISTENT` — signal mix, almost
  always a deployment bug (policy drift, OpenShell version skew,
  half-applied configuration).  ``main.py`` refuses to start in this
  state.

Exactly one I/O operation per signal; no network calls, no long
waits.  Detection runs once at startup before any config loading so
its result can inform logging, YAML path selection, and the startup
self-check.
"""

from __future__ import annotations

import enum
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from nemoclaw_escapades.observability.logging import get_logger

logger = get_logger("runtime")


# ── Signal thresholds ────────────────────────────────────────────────

# Signal names the detector evaluates.  Kept as module-level constants
# so unit tests and log-inspection tooling have one source of truth.
_SIGNAL_OPENSHELL_ENV: str = "OPENSHELL_SANDBOX"
_SIGNAL_SANDBOX_DIR: str = "sandbox_dir_writable"
_SIGNAL_APP_SRC_READONLY: str = "app_src_readonly"
_SIGNAL_HTTPS_PROXY: str = "https_proxy_env"
_SIGNAL_SSL_CERT_BUNDLE: str = "ssl_cert_bundle_env"
_SIGNAL_INFERENCE_DNS: str = "inference_local_resolves"

_ALL_SIGNALS: tuple[str, ...] = (
    _SIGNAL_OPENSHELL_ENV,
    _SIGNAL_SANDBOX_DIR,
    _SIGNAL_APP_SRC_READONLY,
    _SIGNAL_HTTPS_PROXY,
    _SIGNAL_SSL_CERT_BUNDLE,
    _SIGNAL_INFERENCE_DNS,
)

# ``SANDBOX`` needs at least this many signals to match.  Sized to
# tolerate one or two flaky / missing signals (e.g. the DNS lookup or
# a proxy CA that hasn't been installed yet) without weakening the
# check.  Less than this → ``INCONSISTENT`` unless *everything* is
# missing, in which case ``LOCAL_DEV``.
_SANDBOX_SIGNAL_THRESHOLD: int = 4

# Below this count → confident ``LOCAL_DEV``.  Between this and the
# sandbox threshold → ``INCONSISTENT``, *unless* the present signals
# are only from :data:`_BENIGN_LOCAL_ENV_SIGNALS` (see below).
_LOCAL_DEV_SIGNAL_CEILING: int = 1

# Signals that can show up in a perfectly normal local-dev shell for
# reasons unrelated to OpenShell — so we shouldn't trip
# ``INCONSISTENT`` when *only* these are present.
#
# Concretely: developers behind a corporate VPN / MITM proxy usually
# have ``HTTPS_PROXY`` in their shell profile and
# ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` pointing at the
# corporate CA their IT team installed.  Two signals, no paths, no
# DNS — not an OpenShell sandbox, just the user's normal dev
# environment.  If we classified that as ``INCONSISTENT`` the
# ``SandboxConfigurationError`` in ``main.py`` would refuse to start
# for every such developer.
#
# Any path signal (``/sandbox`` writable, ``/app/src`` present) or
# the ``inference.local`` DNS signal *is* OpenShell-specific, so if
# one of those appears alongside the env signals we keep classifying
# as ``INCONSISTENT``.
_BENIGN_LOCAL_ENV_SIGNALS: frozenset[str] = frozenset(
    {
        _SIGNAL_HTTPS_PROXY,
        _SIGNAL_SSL_CERT_BUNDLE,
    }
)

# Paths / hostnames the signals inspect.
_SANDBOX_DIR: Path = Path("/sandbox")
_APP_SRC_DIR: Path = Path("/app/src")
_INFERENCE_DNS_NAME: str = "inference.local"


class RuntimeEnvironment(str, enum.Enum):
    """Classification result from :func:`detect_runtime_environment`.

    Inherits from ``str`` so it's ergonomic in structured logs and
    equality comparisons with plain strings.
    """

    LOCAL_DEV = "LOCAL_DEV"
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


def _check_app_src_readonly() -> bool:
    """``/app/src`` exists (Dockerfile copied the app in) and is read-only.

    Presence alone is evidence the container was built by our
    Dockerfile.  We don't require read-only-ness to be *enforced* at
    Landlock level — just that the directory exists where the image
    put it.
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
    if name == _SIGNAL_APP_SRC_READONLY:
        return _check_app_src_readonly()
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
    n_present = len(signals_present)
    if n_present >= _SANDBOX_SIGNAL_THRESHOLD:
        return RuntimeEnvironment.SANDBOX, ""
    if n_present <= _LOCAL_DEV_SIGNAL_CEILING:
        return RuntimeEnvironment.LOCAL_DEV, ""

    # Corporate-proxy escape hatch: if every present signal is a
    # ``_BENIGN_LOCAL_ENV_SIGNALS`` member (no path signal, no DNS
    # signal, no ``OPENSHELL_SANDBOX``), the environment is almost
    # certainly a developer's normal shell behind a VPN / MITM CA.
    # Classify as ``LOCAL_DEV`` so the startup self-check doesn't
    # refuse to run for every developer behind corporate networking.
    if set(signals_present).issubset(_BENIGN_LOCAL_ENV_SIGNALS):
        return RuntimeEnvironment.LOCAL_DEV, ""

    # Between the two thresholds.  Common misdiagnoses, in rough order
    # of frequency:
    #
    # - OpenShell version drift (env vars injected differently across
    #   releases — e.g. ``HTTPS_PROXY`` without the cert bundle).
    # - Gateway restart mid-policy-apply (paths exist but env missing).
    # - A local-dev run accidentally inheriting sandbox env vars from
    #   a previous shell (e.g. after ``source .env`` in a prod profile).
    causes: list[str] = []
    has_env_signals = (
        _SIGNAL_OPENSHELL_ENV in signals_present
        or _SIGNAL_HTTPS_PROXY in signals_present
        or _SIGNAL_SSL_CERT_BUNDLE in signals_present
    )
    has_path_signals = (
        _SIGNAL_SANDBOX_DIR in signals_present
        or _SIGNAL_APP_SRC_READONLY in signals_present
    )
    if has_env_signals and not has_path_signals:
        causes.append(
            "sandbox env vars leaked into a local process "
            "(unset OPENSHELL_SANDBOX / HTTPS_PROXY and retry)"
        )
    elif has_path_signals and not has_env_signals:
        causes.append(
            "image built correctly but gateway didn't inject env "
            "(recreate with `make run-local-sandbox`)"
        )
    else:
        causes.append("OpenShell version drift or gateway misconfigured")
    return RuntimeEnvironment.INCONSISTENT, "; ".join(causes)


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
