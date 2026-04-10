#!/usr/bin/env python3
"""Verify Jira REST API connectivity from inside an OpenShell sandbox.

Calls the Jira Python client directly to test that:
  1. The JIRA_AUTH env var is set (proxy placeholder or real value)
  2. The L7 proxy resolves credentials and forwards to jirasw.nvidia.com
  3. The Jira API responds with valid data

Prerequisites:
  - Sandbox created with: make setup-sandbox
  - Jira provider registered with: make setup-jira-provider

Usage:
  make test-jira-sandbox
"""

from __future__ import annotations

import asyncio
import os
import sys

from nemoclaw_escapades.config import DEFAULT_JIRA_URL
from nemoclaw_escapades.tools.jira import JiraClient

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SEPARATOR = "═" * 59


def _print_env() -> None:
    """Display relevant environment variables."""
    auth = os.environ.get("JIRA_AUTH", "")
    url = os.environ.get("JIRA_URL", "")
    print(f"JIRA_AUTH env var: {'set (' + str(len(auth)) + ' chars)' if auth else 'NOT SET'}")
    print(f"JIRA_URL env var:  {url or 'not set (using default)'}")
    print()


async def _run_checks() -> int:
    """Execute connectivity checks and return the failure count."""
    client = JiraClient(
        base_url=os.environ.get("JIRA_URL", DEFAULT_JIRA_URL),
        auth_header=os.environ.get("JIRA_AUTH", ""),
    )
    failures = 0
    me: dict = {}

    # Test 1: authenticate
    print("jira_me() — authenticate and get user profile")
    try:
        me = await client.me()
        if "error" in me:
            print(f"  FAIL: {me['error']}")
            failures += 1
        else:
            name = me.get("displayName", me.get("name", "unknown"))
            print(f"  OK: Authenticated as {name} ({me.get('emailAddress', 'n/a')})")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    # Test 2: search
    print("jira_search() — search for assigned issues")
    try:
        username = me.get("name", "") if "error" not in me else ""
        jql = f"assignee = {username} ORDER BY updated DESC" if username else "ORDER BY updated DESC"
        result = await client.search(jql=jql, limit=3)
        if "error" in result:
            print(f"  FAIL: {result['error']}")
            failures += 1
        else:
            total = result.get("total", 0)
            issues = result.get("issues", [])
            print(f"  OK: {total} total issues (showing {len(issues)})")
            for issue in issues[:3]:
                key = issue.get("key", "?")
                summary = issue.get("fields", {}).get("summary", "?")[:60]
                print(f"       {key}: {summary}")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    await client.close()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" Jira Sandbox Connectivity Test")
    print(_SEPARATOR)
    print()
    _print_env()

    failures = asyncio.run(_run_checks())

    print()
    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All Jira sandbox tests passed")
    else:
        print(f"{_FAIL} {failures} test(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
