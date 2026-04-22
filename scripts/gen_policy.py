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
) -> tuple[list[str], list[str]]:
    """Replace ``host: ""`` placeholders with hostnames from *env*.

    For each ``env_var → policy_name`` entry in
    :data:`_POLICY_HOST_SUBSTITUTIONS`:

    - If *env* has the URL and it parses to a non-empty host, rewrite
      the first endpoint's ``host`` on the matching network_policy.
    - Otherwise leave the placeholder in place and add the policy name
      to the skipped list for the log summary.  The resolved policy
      then contains an empty host, which OpenShell rejects at apply
      time — fail-closed is the correct posture for an OSS consumer
      without a ``.env``.

    Returns:
        ``(applied, skipped)`` — parallel lists of
        ``"<env_var> → <policy_name> = <host>"`` strings and
        ``"<env_var> (→ <policy_name>)"`` strings respectively.
        Used by the caller to print a concise summary.
    """
    network_policies = policy.get("network_policies") or {}
    applied: list[str] = []
    skipped: list[str] = []
    for env_var, policy_name in _POLICY_HOST_SUBSTITUTIONS.items():
        url = env.get(env_var, "")
        host = _extract_host(url)
        policy_entry = network_policies.get(policy_name) or {}
        endpoints = policy_entry.get("endpoints") or []
        if not host:
            skipped.append(f"{env_var} (→ {policy_name})")
            continue
        if not endpoints:
            # Schema drift — e.g. someone removed the network_policy
            # from the base.  Flag loudly so the operator can fix it.
            print(
                f"  Warning: {policy_name} network_policy has no endpoints; "
                f"cannot apply {env_var} substitution.",
                file=sys.stderr,
            )
            skipped.append(f"{env_var} (→ {policy_name})")
            continue
        endpoints[0]["host"] = host
        applied.append(f"{env_var} → {policy_name}.host = {host}")
    return applied, skipped


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
    # exactly what was applied and what's still missing.
    print(f"Policy generated: {RESOLVED_POLICY}")
    if host_applied:
        print(f"  hostnames substituted: {len(host_applied)}")
        for line in host_applied:
            print(f"    • {line}")
    if host_skipped:
        print(
            f"  hostnames NOT substituted (fail-closed): "
            f"{', '.join(host_skipped)}"
        )
    if injected:
        print(f"  allowed_ips injected for: {', '.join(injected)}")
        print(f"  CIDRs: {', '.join(allowed_ips)}")
    elif endpoints_needing_ips:
        print("  no allowed_ips — ALLOWED_IPS not set")


if __name__ == "__main__":
    main()
