#!/usr/bin/env python3
"""Verify Confluence REST API connectivity from inside an OpenShell sandbox.

Tests that:
  1. The CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN env vars are set
  2. The L7 proxy resolves credentials and forwards to the Confluence instance
  3. The Confluence API responds with valid data

Usage:
  make test-confluence-sandbox
"""

from __future__ import annotations

import asyncio
import os
import sys

from nemoclaw_escapades.config import DEFAULT_CONFLUENCE_URL
from nemoclaw_escapades.tools.confluence import ConfluenceClient

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_SEPARATOR = "═" * 59


def _print_env() -> None:
    user = os.environ.get("CONFLUENCE_USERNAME", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    url = os.environ.get("CONFLUENCE_URL", DEFAULT_CONFLUENCE_URL)
    print(f"CONFLUENCE_USERNAME env var:  {'set' if user else 'NOT SET'}")
    print(f"CONFLUENCE_API_TOKEN env var: {'set (' + str(len(token)) + ' chars)' if token else 'NOT SET'}")
    print(f"CONFLUENCE_URL env var:       {url or 'NOT SET'}")
    print()


async def _run_checks() -> int:
    client = ConfluenceClient(
        base_url=os.environ.get("CONFLUENCE_URL", DEFAULT_CONFLUENCE_URL),
        username=os.environ.get("CONFLUENCE_USERNAME", ""),
        api_token=os.environ.get("CONFLUENCE_API_TOKEN", ""),
    )
    failures = 0

    print("confluence_search() — search for pages")
    try:
        result = await client.search("type=page", limit=3)
        if "error" in result:
            print(f"  FAIL: {result['error']}")
            failures += 1
        else:
            results_list = result.get("results", [])
            total = result.get("totalSize", result.get("size", len(results_list)))
            print(f"  OK: {total} total result(s) (showing {len(results_list)})")
            for page in results_list[:3]:
                title = page.get("title", "?")[:60]
                space = page.get("space", {}).get("key", "?")
                print(f"       [{space}] {title}")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        failures += 1

    await client.close()
    return failures


def main() -> None:
    print(_SEPARATOR)
    print(" Confluence Sandbox Connectivity Test")
    print(_SEPARATOR)
    print()
    _print_env()

    failures = asyncio.run(_run_checks())

    print()
    print(_SEPARATOR)
    if failures == 0:
        print(f"{_PASS} All Confluence sandbox tests passed")
    else:
        print(f"{_FAIL} {failures} test(s) failed")
    print(_SEPARATOR)
    sys.exit(failures)


if __name__ == "__main__":
    main()
