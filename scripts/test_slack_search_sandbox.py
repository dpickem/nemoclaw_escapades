#!/usr/bin/env python3
"""Verify Slack user-token API connectivity from inside an OpenShell sandbox.

Tests that:
  1. The SLACK_USER_TOKEN env var is set
  2. The L7 proxy resolves credentials and forwards to slack.com
  3. The Slack API responds with valid data

Usage:
  make test-slack-search-sandbox
"""

from __future__ import annotations

import asyncio
import os
import sys

from nemoclaw_escapades.tools.slack_search import SlackSearchClient

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SEPARATOR = "═" * 59


def _print_env() -> None:
    token = os.environ.get("SLACK_USER_TOKEN", "")
    print(f"SLACK_USER_TOKEN env var: {'set (' + str(len(token)) + ' chars)' if token else 'NOT SET'}")
    print()


async def _run_checks() -> int:
    client = SlackSearchClient(
        user_token=os.environ.get("SLACK_USER_TOKEN", ""),
    )
    failures = 0

    print("slack_list_channels() — list accessible channels")
    try:
        result = await client.list_channels(limit=3)
        if "error" in result:
            print(f"  FAIL: {result['error']}")
            failures += 1
        else:
            channels = result.get("channels", [])
            print(f"  OK: {len(channels)} channel(s) returned")
            for ch in channels[:3]:
                print(f"       #{ch.get('name', '?')} ({ch.get('id', '?')})")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    print("slack_search_messages() — search messages")
    try:
        result = await client.search_messages("hello", count=3)
        if "error" in result:
            print(f"  FAIL: {result['error']}")
            failures += 1
        else:
            msgs = result.get("messages", {})
            total = msgs.get("total", 0) if isinstance(msgs, dict) else 0
            print(f"  OK: {total} total match(es)")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    await client.close()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" Slack User-Token Sandbox Connectivity Test")
    print(_SEPARATOR)
    print()
    _print_env()

    failures = asyncio.run(_run_checks())

    print()
    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All Slack search sandbox tests passed")
    else:
        print(f"{_FAIL} {failures} test(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
