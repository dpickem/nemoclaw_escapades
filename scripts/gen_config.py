#!/usr/bin/env python3
"""Generate the resolved sandbox configuration from the base YAML + .env overrides.

Reads ``config/defaults.yaml`` (committed, no secrets) and produces
``config/orchestrator.resolved.yaml`` (gitignored).  The resolved file is
``COPY``'d into the sandbox image at build time as ``/app/config.yaml``
and loaded at startup by :func:`nemoclaw_escapades.config.AppConfig.load`.

This mirrors :mod:`scripts.gen_policy` — same split between a public base
and a private overlay.  The distinction here is that this file deals with
application config, not network policy.

Three categories of configuration values, as defined in ``design_m2b.md``
§5.3.2:

- **Category A — public non-secret**: already shipped in ``defaults.yaml``.
  No action by this script.
- **Category B — private non-secret**: internal hostnames, host
  allowlists, infra URLs.  This script reads each of the keys in
  ``_CATEGORY_B_KEYS`` from ``.env`` and writes them into the corresponding
  YAML dotted path in the resolved output.  Anything in ``.env`` that
  isn't on this explicit allowlist is ignored on purpose — operators
  can't accidentally promote arbitrary environment variables into the
  shipping config.
- **Category C — secret**: tokens, API keys, usernames, passwords.
  Continue to flow through OpenShell providers and the L7 proxy at
  runtime.  ``_FORBIDDEN_KEY_SUFFIXES`` makes ``gen_config.py`` *refuse*
  to write any key whose name matches one of the secret-like suffixes,
  even if it appears on the allowlist — a guardrail against accidental
  rotations.

Usage::

    make gen-config                                 # via Makefile
    PYTHONPATH=src python scripts/gen_config.py     # standalone

OSS / CI consumers without a ``.env`` get a resolved file that's
byte-identical to ``defaults.yaml`` — every category-B field holds its
fail-closed value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# Committed base — public-safe, no category-B values.
BASE_CONFIG: Path = Path("config/defaults.yaml")

# Resolved output — gitignored; ``COPY``'d into the sandbox image as
# ``/app/config.yaml``.
RESOLVED_CONFIG: Path = Path("config/orchestrator.resolved.yaml")


# Category-B allowlist: .env var name → dotted YAML path in ``defaults.yaml``.
#
# Anything that isn't on this list is ignored, so operators can't
# accidentally leak arbitrary environment variables into the sandbox image.
# Add here when a new category-B knob is introduced; mirror the addition in
# ``.env.example`` so operators know to set it.
_CATEGORY_B_KEYS: dict[str, str] = {
    "GIT_CLONE_ALLOWED_HOSTS": "coding.git_clone_allowed_hosts",
    "GITLAB_URL": "toolsets.gitlab.url",
    "GERRIT_URL": "toolsets.gerrit.url",
}


# Any ``.env`` key ending in one of these suffixes is *never* allowed into
# the resolved YAML — even if it somehow ended up in ``_CATEGORY_B_KEYS``.
# This is a defence-in-depth guard against accidental secret rotation into
# file-based config.  Matching is case-insensitive.
_FORBIDDEN_KEY_SUFFIXES: tuple[str, ...] = (
    "_TOKEN",
    "_AUTH",
    "_PASSWORD",
    "_KEY",
    "_SECRET",
    "_API_KEY",
    "_BOT_TOKEN",
    "_APP_TOKEN",
    "_USER_TOKEN",
)


def _load_dotenv(path: str = ".env") -> dict[str, str]:
    """Parse a dotenv file and return its key-value pairs.

    Args:
        path: Filesystem path to the dotenv file.  Returns an empty
            dict when the file does not exist.

    Returns:
        Mapping of variable names to their values as read from the
        file.  Blank lines, ``#``-comments, and malformed lines are
        skipped silently (matching ``scripts.gen_policy`` behaviour).
    """
    env_path = Path(path)
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _has_forbidden_suffix(key: str) -> bool:
    """Return ``True`` if *key* ends with a secret-like suffix.

    Matching is case-insensitive and walks the tuple of forbidden
    suffixes.  Used to fail-fast if a category-B allowlist entry ever
    points at a secret.
    """
    upper = key.upper()
    return any(upper.endswith(suffix) for suffix in _FORBIDDEN_KEY_SUFFIXES)


def _apply_dotted_path(doc: dict[str, Any], dotted: str, value: str) -> None:
    """Set a value in a nested dict via a dotted path.

    Args:
        doc: The YAML-parsed dict to mutate.
        dotted: Dotted YAML path (e.g. ``"coding.git_clone_allowed_hosts"``).
            Intermediate keys must already exist — the base YAML is
            the source of truth for schema shape; this script only
            fills values, never invents new keys.
        value: String value to write.

    Raises:
        KeyError: If an intermediate key is missing.  That's a hard
            error — it means ``defaults.yaml`` and ``_CATEGORY_B_KEYS``
            have drifted.
    """
    parts = dotted.split(".")
    node: Any = doc
    for part in parts[:-1]:
        if part not in node:
            raise KeyError(
                f"Dotted path {dotted!r} not present in {BASE_CONFIG} "
                f"(missing intermediate {part!r}).  "
                f"Add the key to defaults.yaml or fix _CATEGORY_B_KEYS."
            )
        node = node[part]
    node[parts[-1]] = value


def main() -> None:
    """Generate ``config/orchestrator.resolved.yaml`` from base + ``.env``.

    Steps:
        1. Load ``.env`` (may be empty / missing).
        2. Verify the category-B allowlist doesn't include any
           secret-suffixed keys.
        3. Read ``defaults.yaml`` as the base.
        4. For every key in the allowlist that is present in ``.env``
           *and* whose value is non-empty *and* whose name does not
           match a forbidden suffix, write the value into the resolved
           doc at the corresponding dotted path.
        5. Write the doc to :data:`RESOLVED_CONFIG` with an explanatory
           header.

    Raises:
        SystemExit: If the base YAML is missing, or if the category-B
            allowlist points at a forbidden key (misconfiguration).
    """
    env = _load_dotenv()

    if not BASE_CONFIG.is_file():
        print(f"Error: {BASE_CONFIG} not found", file=sys.stderr)
        sys.exit(1)

    # Guard: the allowlist itself must not point at secret-suffixed keys.
    # This catches a class of mistake where somebody adds
    # ``"SLACK_BOT_TOKEN": "toolsets.slack.bot_token"`` to _CATEGORY_B_KEYS.
    bad_allowlist = [k for k in _CATEGORY_B_KEYS if _has_forbidden_suffix(k)]
    if bad_allowlist:
        print(
            f"Error: category-B allowlist includes secret-suffixed keys: "
            f"{', '.join(bad_allowlist)}.  Secrets must flow through "
            f"env vars, not file-based config.",
            file=sys.stderr,
        )
        sys.exit(2)

    doc: dict[str, Any] = yaml.safe_load(BASE_CONFIG.read_text())

    applied: list[str] = []
    skipped_forbidden: list[str] = []
    for env_key, dotted in _CATEGORY_B_KEYS.items():
        if env_key not in env:
            continue
        value = env[env_key]
        if not value:
            continue
        # Runtime-check again in case a future edit makes the allowlist
        # wider — belt-and-braces against an operator supplying a secret
        # under an allowed-looking name (e.g. via shell export).
        if _has_forbidden_suffix(env_key):
            skipped_forbidden.append(env_key)
            continue
        _apply_dotted_path(doc, dotted, value)
        applied.append(f"{env_key} → {dotted}")

    if skipped_forbidden:
        # Hard fail — this is a misconfiguration, not a warning.
        print(
            f"Error: secret-suffixed env vars found on category-B "
            f"allowlist at runtime: {', '.join(skipped_forbidden)}.",
            file=sys.stderr,
        )
        sys.exit(3)

    RESOLVED_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(RESOLVED_CONFIG, "w") as f:
        f.write("# AUTO-GENERATED by scripts/gen_config.py — do not edit.\n")
        f.write(f"# Source: {BASE_CONFIG}\n")
        f.write("# Regenerate: make gen-config\n")
        f.write("# Category-B values are merged from .env at build time.\n\n")
        yaml.dump(doc, f, default_flow_style=False, sort_keys=False)

    if applied:
        print(f"Config generated: {RESOLVED_CONFIG}")
        print(f"  category-B overrides applied: {len(applied)}")
        for line in applied:
            print(f"    • {line}")
    else:
        print(
            f"Config generated: {RESOLVED_CONFIG} "
            "(no category-B overrides — all fields at fail-closed defaults)"
        )


if __name__ == "__main__":
    main()
