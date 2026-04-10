#!/usr/bin/env python3
"""Verify GitLab REST API connectivity from inside an OpenShell sandbox.

Tests that:
  1. The GITLAB_TOKEN env var is set
  2. The L7 proxy resolves credentials and forwards to gitlab-master.nvidia.com
  3. The GitLab API responds with valid data

Usage:
  make test-gitlab-sandbox
"""

from __future__ import annotations

import asyncio
import os
import sys

from nemoclaw_escapades.config import DEFAULT_GITLAB_URL
from nemoclaw_escapades.tools.gitlab import GitLabClient

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SEPARATOR = "═" * 59


def _print_env() -> None:
    token = os.environ.get("GITLAB_TOKEN", "")
    url = os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL)
    print(f"GITLAB_TOKEN env var: {'set (' + str(len(token)) + ' chars)' if token else 'NOT SET'}")
    print(f"GITLAB_URL env var:   {url}")
    print()


async def _run_checks() -> int:
    client = GitLabClient(
        base_url=os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL),
        token=os.environ.get("GITLAB_TOKEN", ""),
    )
    failures = 0

    print("gitlab_me() — authenticate and get user profile")
    try:
        me = await client.get_current_user()
        if "error" in me:
            print(f"  FAIL: {me['error']}")
            if me.get("body"):
                print(f"       {me['body'][:200]}")
            failures += 1
        else:
            name = me.get("name", me.get("username", "unknown"))
            print(f"  OK: Authenticated as {name} ({me.get('email', 'n/a')})")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    print("gitlab_search_projects() — search for projects")
    try:
        result = await client.search_projects("nemoclaw", limit=3)
        if isinstance(result, dict) and "error" in result:
            print(f"  FAIL: {result['error']}")
            if result.get("body"):
                print(f"       {result['body'][:200]}")
            failures += 1
        elif isinstance(result, list):
            print(f"  OK: Found {len(result)} project(s)")
            for proj in result[:3]:
                print(f"       {proj.get('path_with_namespace', '?')}")
        else:
            print(f"  OK: Response received")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    await client.close()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" GitLab Sandbox Connectivity Test")
    print(_SEPARATOR)
    print()
    _print_env()

    failures = asyncio.run(_run_checks())

    print()
    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All GitLab sandbox tests passed")
    else:
        print(f"{_FAIL} {failures} test(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
