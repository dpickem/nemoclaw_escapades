#!/usr/bin/env python3
"""Host-side client for the browser-to-sandbox HTTP prototype."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller supplies probe URL.
        body = response.read().decode("utf-8")
        return json.loads(body)


def _wait_for_health(base_url: str, attempts: int, delay: float, timeout: float) -> dict[str, Any]:
    health_url = f"{base_url.rstrip('/')}/api/health"
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = _get_json(health_url, timeout)
            if payload.get("ok") is True:
                return payload
            last_error = f"unexpected health payload: {payload!r}"
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)

        if attempt < attempts:
            time.sleep(delay)

    raise RuntimeError(f"health check failed after {attempts} attempts: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--message", default="hello from the host")
    parser.add_argument("--outbound-url", default="https://example.com/")
    parser.add_argument("--skip-outbound", action="store_true")
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")

    try:
        health = _wait_for_health(base_url, args.attempts, args.delay, args.timeout)
        print("health:", json.dumps(health, sort_keys=True))

        echo_query = urlencode({"message": args.message})
        echo = _get_json(f"{base_url}/api/echo?{echo_query}", args.timeout)
        print("echo:", json.dumps(echo, sort_keys=True))
        if echo.get("message") != args.message:
            raise RuntimeError(f"echo mismatch: {echo!r}")

        if not args.skip_outbound:
            outbound_query = urlencode({"url": args.outbound_url})
            outbound = _get_json(f"{base_url}/api/outbound?{outbound_query}", args.timeout)
            print("outbound:", json.dumps(outbound, sort_keys=True))
            if outbound.get("ok") is not True:
                raise RuntimeError(f"outbound probe failed: {outbound!r}")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("OK: host reached the sandbox HTTP server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
