#!/usr/bin/env python3
"""Generate the resolved sandbox policy from the base template + .env overlays.

Reads policies/orchestrator.yaml (committed, public-safe — no category-B
values) and performs two resolve-time substitutions from .env:

- **Hostname substitution** — internal hosts ship as empty placeholders
  (``endpoint.host: ""``) in the base policy; this script fills them
  from the URLs operators put in ``.env``.  See
  ``_POLICY_HOST_SUBSTITUTIONS`` below for the mapping.  Same public-
  base + private-overlay pattern as ``scripts/gen_config.py`` +
  ``config/defaults.yaml`` (design_m2b.md §5.3).
- **``allowed_ips`` injection** — SSRF bypass CIDRs get inlined into
  every endpoint whose host is listed in
  ``ENDPOINTS_NEEDING_ALLOWED_IPS``.  Both CIDRs and the endpoint
  allowlist come from ``.env``.

No internal hostnames or CIDRs are hardcoded anywhere in this script or
the committed base policy — running ``make gen-policy`` with an empty
``.env`` produces a fail-closed resolved file (gitlab / gerrit policies
have empty host strings, which OpenShell rejects at apply time).

Usage:
  make gen-policy                 # via Makefile
  PYTHONPATH=src python scripts/gen_policy.py   # standalone

Writes policies/orchestrator.resolved.yaml (gitignored).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Committed base template (no secrets, no category-B values).
BASE_POLICY = Path("policies/orchestrator.yaml")
# Output path for the fully-resolved policy (gitignored).
RESOLVED_POLICY = Path("policies/orchestrator.resolved.yaml")


# Mapping of .env URL var → name of the network_policy whose first
# endpoint's ``host`` should be rewritten with the URL's hostname.
# Adding a new private host is a one-line change here + a matching
# ``host: ""`` placeholder in the base policy.
#
# URL (not plain hostname) because these same variables configure the
# REST tools in ``src/nemoclaw_escapades/config.py`` as full
# ``scheme://host[:port]/path`` strings — keeping one source of truth
# means the operator can't drift between config and policy.
_POLICY_HOST_SUBSTITUTIONS: dict[str, str] = {
    "GITLAB_URL": "gitlab",
    "GERRIT_URL": "gerrit",
}


def _load_dotenv(path: str = ".env") -> dict[str, str]:
    """Parse a dotenv file and return its key-value pairs.

    Args:
        path: Filesystem path to the dotenv file.  Returns an empty
            dict when the file does not exist.

    Returns:
        Mapping of variable names to their values as read from the file.
    """
    env_path = Path(path)
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _extract_host(url: str) -> str:
    """Return the hostname from a full URL, or ``""`` if unparseable.

    ``GITLAB_URL`` / ``GERRIT_URL`` are full
    ``scheme://host[:port]/path`` strings.  The policy needs just the
    host.  Falls through to an empty string on anything that looks
    bare (no scheme) so a misformatted ``.env`` fails fail-closed
    instead of producing a garbled policy.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return ""
    return parsed.hostname or ""


def _apply_host_substitutions(
    policy: dict[str, Any], env: dict[str, str]
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Replace ``host: ""`` placeholders with hostnames from *env*.

    For each ``env_var → policy_name`` entry in
    :data:`_POLICY_HOST_SUBSTITUTIONS`:

    - If *env* has the URL and it parses to a non-empty host, rewrite
      the first endpoint's ``host`` on the matching network_policy.
    - Otherwise leave the placeholder in place and record *why* —
      missing env var, bare hostname without scheme, missing
      network_policy in the base template — so the caller can emit
      an actionable message.  The resolved policy keeps an empty
      host, which OpenShell rejects at apply time — fail-closed is
      the correct posture for an OSS consumer without a ``.env``.

    Returns:
        ``(applied, skipped)`` where ``applied`` is a list of
        ``"<env_var> → <policy_name> = <host>"`` summary strings and
        ``skipped`` is a list of ``(env_var, policy_name, reason)``
        tuples.  ``reason`` is one of ``"missing"`` (env var not in
        ``.env``), ``"malformed"`` (URL present but unparseable —
        e.g. no scheme), or ``"schema_drift"`` (named policy absent
        from base template).
    """
    network_policies = policy.get("network_policies") or {}
    applied: list[str] = []
    skipped: list[tuple[str, str, str]] = []
    for env_var, policy_name in _POLICY_HOST_SUBSTITUTIONS.items():
        url = env.get(env_var, "")
        host = _extract_host(url)
        policy_entry = network_policies.get(policy_name) or {}
        endpoints = policy_entry.get("endpoints") or []
        if not host:
            # Distinguish "var absent" from "var set but garbage" so
            # the summary can give the operator a concrete next step.
            reason = "missing" if not url.strip() else "malformed"
            skipped.append((env_var, policy_name, reason))
            continue
        if not endpoints:
            # Schema drift — someone removed the network_policy from
            # the base.  Flag loudly so the operator notices and
            # either restores it or drops the substitution entry.
            print(
                f"  Warning: {policy_name} network_policy has no endpoints; "
                f"cannot apply {env_var} substitution.",
                file=sys.stderr,
            )
            skipped.append((env_var, policy_name, "schema_drift"))
            continue
        endpoints[0]["host"] = host
        applied.append(f"{env_var} → {policy_name}.host = {host}")
    return applied, skipped


def _format_host_skip_summary(
    skipped: list[tuple[str, str, str]],
) -> list[str]:
    """Turn the structured skip list into operator-actionable lines.

    Each reason gets its own block so the operator can see at a
    glance which env vars to add and which to rewrite.  Keeping the
    formatting here (rather than inline in ``main``) means future
    tweaks to the UX only touch one function.
    """
    missing = [
        (env_var, policy_name)
        for env_var, policy_name, reason in skipped
        if reason == "missing"
    ]
    malformed = [
        (env_var, policy_name)
        for env_var, policy_name, reason in skipped
        if reason == "malformed"
    ]
    drift = [
        (env_var, policy_name)
        for env_var, policy_name, reason in skipped
        if reason == "schema_drift"
    ]
    lines: list[str] = []
    if missing:
        lines.append(
            "  hostnames NOT substituted (env var missing from .env, "
            "policy stays fail-closed):"
        )
        for env_var, policy_name in missing:
            lines.append(
                f"    • {env_var} (→ network_policies.{policy_name}.endpoints[0].host)"
            )
        lines.append(
            "    Fix: add `{0}=https://<your-host>` to .env, then "
            "re-run `make gen-policy`.".format(missing[0][0])
        )
    if malformed:
        lines.append(
            "  hostnames NOT substituted (URL set but unparseable — "
            "need `scheme://host`):"
        )
        for env_var, policy_name in malformed:
            lines.append(
                f"    • {env_var} (→ network_policies.{policy_name}.endpoints[0].host)"
            )
    if drift:
        lines.append(
            "  hostnames NOT substituted (policy missing from base template):"
        )
        for env_var, policy_name in drift:
            lines.append(f"    • {env_var} (→ {policy_name})")
    return lines


def main() -> None:
    """Generate the resolved sandbox policy file.

    Steps:
        1. Load environment variables from ``.env`` (if present).
        2. Read the base policy template.
        3. Substitute internal hostnames into ``host: ""`` placeholders
           from the URLs in ``.env``.
        4. Inject ``allowed_ips`` CIDR ranges into every endpoint whose
           (now-substituted) host appears in
           ``ENDPOINTS_NEEDING_ALLOWED_IPS``.
        5. Write the resolved policy to :data:`RESOLVED_POLICY`.

    Raises:
        SystemExit: If the base policy template is missing.
    """
    env = _load_dotenv()

    endpoints_raw = env.get("ENDPOINTS_NEEDING_ALLOWED_IPS", "")
    endpoints_needing_ips: set[str] = {
        h.strip() for h in endpoints_raw.split(",") if h.strip()
    }

    allowed_ips_raw = env.get("ALLOWED_IPS", "")
    allowed_ips: list[str] = [
        cidr.strip() for cidr in allowed_ips_raw.split(",") if cidr.strip()
    ]

    if not BASE_POLICY.is_file():
        print(f"Error: {BASE_POLICY} not found", file=sys.stderr)
        sys.exit(1)

    policy: dict[str, object] = yaml.safe_load(BASE_POLICY.read_text())

    # Step 3 — host substitution.  Runs before ``allowed_ips`` injection
    # because the injection step matches on the final host value.
    host_applied, host_skipped = _apply_host_substitutions(policy, env)

    # Step 4 — allowed_ips injection (unchanged).
    injected: list[str] = []
    for _policy_name, policy_entry in policy.get("network_policies", {}).items():
        for endpoint in policy_entry.get("endpoints", []):
            host: str = endpoint.get("host", "")
            if host not in endpoints_needing_ips:
                continue
            if allowed_ips:
                endpoint["allowed_ips"] = allowed_ips
                injected.append(host)
            else:
                print(
                    f"  Warning: {host} needs allowed_ips but "
                    "ALLOWED_IPS is not set in .env",
                )

    with open(RESOLVED_POLICY, "w") as f:
        f.write("# AUTO-GENERATED by scripts/gen_policy.py — do not edit.\n")
        f.write(f"# Source: {BASE_POLICY}\n")
        f.write("# Regenerate: make gen-policy\n\n")
        yaml.dump(policy, f, default_flow_style=False, sort_keys=False)

    # Summary — one block per substitution stage so the operator sees
    # exactly what was applied and what's still missing.  ``skipped``
    # is structured so missing-vs-malformed-vs-schema-drift each get
    # their own actionable hint.
    print(f"Policy generated: {RESOLVED_POLICY}")
    if host_applied:
        print(f"  hostnames substituted: {len(host_applied)}")
        for line in host_applied:
            print(f"    • {line}")
    for line in _format_host_skip_summary(host_skipped):
        print(line)
    if injected:
        print(f"  allowed_ips injected for: {', '.join(injected)}")
        print(f"  CIDRs: {', '.join(allowed_ips)}")
    elif endpoints_needing_ips and host_applied:
        # Only flag when substitution succeeded but ALLOWED_IPS didn't
        # — otherwise the "needs ALLOWED_IPS" hint competes with the
        # more fundamental "add GITLAB_URL / GERRIT_URL" fix above.
        print("  no allowed_ips — ALLOWED_IPS not set in .env")
    # Flag the specific "ENDPOINTS_NEEDING_ALLOWED_IPS is stale" case:
    # operator has real hostnames in that var but the resolved policy
    # has empty ``host`` fields, so no injection target matched.
    if endpoints_needing_ips and not injected and host_skipped:
        print(
            "  note: ENDPOINTS_NEEDING_ALLOWED_IPS references "
            f"{sorted(endpoints_needing_ips)} but none matched the "
            "resolved hosts — fix the missing URL(s) above and these "
            "will auto-inject on the next run."
        )


if __name__ == "__main__":
    main()
