#!/usr/bin/env python3
"""Tiny HTTP server intended to run inside an OpenShell sandbox."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

DEFAULT_OUTBOUND_URL = "https://example.com/"
MAX_OUTBOUND_BYTES = 512


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


class SandboxProbeHandler(BaseHTTPRequestHandler):
    """Serve health checks and a narrow outbound-fetch probe."""

    server_version = "SandboxHTTPProbe/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write(f"{self.log_date_time_string()} {fmt % args}\n")
        sys.stdout.flush()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path in {"", "/"}:
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "browser-sandbox-http-prototype",
                    "endpoints": ["/api/health", "/api/echo?message=...", "/api/outbound"],
                },
            )
            return

        if parsed.path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "browser-sandbox-http-prototype",
                    "hostname": socket.gethostname(),
                    "time": time.time(),
                },
            )
            return

        if parsed.path == "/api/echo":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "message": params.get("message", [""])[0],
                    "source": "sandbox",
                },
            )
            return

        if parsed.path == "/api/outbound":
            outbound_url = params.get("url", [DEFAULT_OUTBOUND_URL])[0]
            self._handle_outbound(outbound_url)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _handle_outbound(self, outbound_url: str) -> None:
        parsed = urlparse(outbound_url)
        if parsed.scheme not in {"http", "https"}:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "only http and https URLs are supported"},
            )
            return

        request = Request(outbound_url, headers={"User-Agent": "sandbox-http-probe/0.1"})
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is user-selected probe input.
                body = response.read(MAX_OUTBOUND_BYTES).decode("utf-8", errors="replace")
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "url": outbound_url,
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "sample": body,
                    },
                )
        except HTTPError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "url": outbound_url, "status": exc.code, "error": str(exc)},
            )
        except URLError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "url": outbound_url, "error": str(exc.reason)},
            )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="host/IP to bind inside the sandbox")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port to bind")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SandboxProbeHandler)
    print(f"Serving sandbox HTTP probe on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
