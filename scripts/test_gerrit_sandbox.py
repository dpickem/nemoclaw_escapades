#!/usr/bin/env python3
"""Verify Gerrit REST API connectivity from inside an OpenShell sandbox.

Tests that:
  1. The GERRIT_USERNAME and GERRIT_HTTP_PASSWORD env vars are set
  2. The L7 proxy resolves credentials and forwards to the Gerrit instance
  3. The Gerrit API responds with valid data

Usage:
  make test-gerrit-sandbox
"""

from __future__ import annotations

import asyncio
import os
import sys

from nemoclaw_escapades.config import DEFAULT_GERRIT_URL
from nemoclaw_escapades.tools.gerrit import GerritClient

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SEPARATOR = "═" * 59


def _print_env() -> None:
    user = os.environ.get("GERRIT_USERNAME", "")
    pw = os.environ.get("GERRIT_HTTP_PASSWORD", "")
    url = os.environ.get("GERRIT_URL", DEFAULT_GERRIT_URL)
    print(f"GERRIT_USERNAME env var:      {'set' if user else 'NOT SET'}")
    print(f"GERRIT_HTTP_PASSWORD env var: {'set (' + str(len(pw)) + ' chars)' if pw else 'NOT SET'}")
    print(f"GERRIT_URL env var:           {url}")
    print()


async def _run_checks() -> int:
    client = GerritClient(
        base_url=os.environ.get("GERRIT_URL", DEFAULT_GERRIT_URL),
        username=os.environ.get("GERRIT_USERNAME", ""),
        http_password=os.environ.get("GERRIT_HTTP_PASSWORD", ""),
    )
    failures = 0

    print("gerrit_me() — authenticate and get account info")
    try:
        me = await client.get_account()
        if "error" in me:
            print(f"  FAIL: {me['error']}")
            failures += 1
        else:
            name = me.get("name", me.get("username", "unknown"))
            print(f"  OK: Authenticated as {name} ({me.get('email', 'n/a')})")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    print("gerrit_list_changes() — search for recent changes")
    try:
        result = await client.list_changes("status:open limit:3")
        if isinstance(result, dict) and "error" in result:
            print(f"  FAIL: {result['error']}")
            failures += 1
        elif isinstance(result, list):
            print(f"  OK: Found {len(result)} change(s)")
            for change in result[:3]:
                print(f"       {change.get('_number', '?')}: {change.get('subject', '?')[:60]}")
        else:
            print(f"  OK: Response received")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    await client.close()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" Gerrit Sandbox Connectivity Test")
    print(_SEPARATOR)
    print()
    _print_env()

    failures = asyncio.run(_run_checks())

    print()
    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All Gerrit sandbox tests passed")
    else:
        print(f"{_FAIL} {failures} test(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
