"""OpenShell two-sandbox NMB reachability smoke test.

Roles:

- ``broker`` starts an NMB broker plus a tiny orchestrator-side peer in the
  broker sandbox.
- ``probe`` connects to the broker and disconnects without sending task traffic.
- ``client`` connects from the separate client sandbox, announces readiness,
  receives ``task.assign``, and replies with ``task.complete``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from nemoclaw_escapades.config import BrokerConfig
from nemoclaw_escapades.nmb.broker import NMBBroker
from nemoclaw_escapades.nmb.client import MessageBus
from nemoclaw_escapades.nmb.models import NMBMessage

DEFAULT_TASK_ID = "nmb-smoke-001"


def _print_marker(marker: str, **fields: object) -> None:
    """Print machine-readable progress markers for the Makefile."""
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{marker} {suffix}".rstrip(), flush=True)


async def _next_message(
    iterator: Any,
    *,
    timeout: float,
    expected_type: str | None = None,
) -> NMBMessage:
    """Return the next matching message from an async iterator."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {expected_type or 'message'}")

        message = await asyncio.wait_for(anext(iterator), timeout=remaining)
        if expected_type is None or message.type == expected_type:
            return message


async def _close_bus(bus: MessageBus | None) -> None:
    if bus is not None:
        await bus.close()


async def run_broker(args: argparse.Namespace) -> None:
    """Run the broker sandbox side of the smoke test."""
    audit_db = str(Path(args.audit_db).expanduser())
    broker = NMBBroker(
        BrokerConfig(
            host=args.host,
            port=args.port,
            audit_db_path=audit_db,
            default_request_timeout=args.timeout,
        )
    )

    orchestrator: MessageBus | None = None
    await broker.start()
    try:
        broker_url = f"ws://127.0.0.1:{args.port}"
        orchestrator = MessageBus("nmb-smoke-orchestrator", broker_url=broker_url)
        await orchestrator.connect_with_retry(max_retries=10, wait_min=0.25, wait_max=2.0)
        _print_marker(
            "NMB_SMOKE_BROKER_READY",
            broker_url=broker_url,
            orchestrator_id=orchestrator.sandbox_id,
        )

        ready = await _next_message(
            orchestrator.subscribe("smoke.ready"),
            timeout=args.timeout,
            expected_type="client.ready",
        )
        _print_marker("NMB_SMOKE_CLIENT_READY", client_id=ready.from_sandbox)

        await orchestrator.send(
            ready.from_sandbox,
            "task.assign",
            {
                "task_id": args.task_id,
                "instruction": "Complete the NMB cross-sandbox smoke task.",
            },
        )
        _print_marker("NMB_SMOKE_TASK_ASSIGNED", task_id=args.task_id)

        complete = await _next_message(
            orchestrator.listen(),
            timeout=args.timeout,
            expected_type="task.complete",
        )
        if complete.payload.get("task_id") != args.task_id:
            raise RuntimeError(f"Unexpected task_id in task.complete: {complete.payload!r}")

        _print_marker(
            "NMB_SMOKE_SUCCESS",
            task_id=args.task_id,
            completed_by=complete.from_sandbox,
            audit_db=audit_db,
        )

        if args.keep_alive:
            await _wait_until_signal()
    finally:
        await _close_bus(orchestrator)
        await broker.stop()


async def run_client(args: argparse.Namespace) -> None:
    """Run the client sandbox side of the smoke test."""
    bus = MessageBus("nmb-smoke-client", broker_url=args.broker_url)
    await bus.connect_with_retry(max_retries=10, wait_min=0.5, wait_max=3.0)
    try:
        _print_marker(
            "NMB_SMOKE_CLIENT_CONNECTED",
            broker_url=args.broker_url,
            client_id=bus.sandbox_id,
        )
        await bus.publish(
            "smoke.ready",
            "client.ready",
            {
                "task_id": args.task_id,
                "client_id": bus.sandbox_id,
            },
        )
        _print_marker("NMB_SMOKE_READY_PUBLISHED", channel="smoke.ready")

        assign = await _next_message(
            bus.listen(),
            timeout=args.timeout,
            expected_type="task.assign",
        )
        if assign.payload.get("task_id") != args.task_id:
            raise RuntimeError(f"Unexpected task_id in task.assign: {assign.payload!r}")

        await bus.send(
            assign.from_sandbox,
            "task.complete",
            {
                "task_id": args.task_id,
                "status": "complete",
                "result": "NMB cross-sandbox task assignment completed.",
            },
        )
        _print_marker("NMB_SMOKE_CLIENT_SUCCESS", task_id=args.task_id)
    finally:
        await bus.close()


async def run_probe(args: argparse.Namespace) -> None:
    """Open and close a broker connection without consuming smoke messages."""
    bus = MessageBus(args.sandbox_id, broker_url=args.broker_url)
    await bus.connect_with_retry(max_retries=10, wait_min=0.5, wait_max=3.0)
    try:
        _print_marker(
            "NMB_SMOKE_PROBE_CONNECTED",
            broker_url=args.broker_url,
            probe_id=bus.sandbox_id,
        )
    finally:
        await bus.close()


def _broker_host_port(broker_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(broker_url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"Broker URL must include host and port: {broker_url}")
    return parsed.hostname, parsed.port


def _probe_dns_and_tcp(host: str, port: int) -> None:
    proxy_env_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    for key in proxy_env_keys:
        _print_marker("NMB_SMOKE_CLIENT_ENV", key=key, value=os.environ.get(key, ""))

    proxies = urllib.request.getproxies()
    _print_marker("NMB_SMOKE_CLIENT_PROXIES", value=proxies)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        _print_marker("NMB_SMOKE_DNS_FAIL", host=host, error=repr(exc))
        return

    addresses = sorted({info[4][0] for info in infos})
    _print_marker("NMB_SMOKE_DNS_OK", host=host, addresses=",".join(addresses))

    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr: tuple[Any, ...] = (
            (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        )
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(sockaddr)
        except OSError as exc:
            _print_marker("NMB_SMOKE_TCP_FAIL", address=address, port=port, error=repr(exc))
        else:
            _print_marker("NMB_SMOKE_TCP_OK", address=address, port=port)
        finally:
            sock.close()


async def _probe_websocket(broker_url: str, *, use_proxy: bool) -> None:
    label = "default_proxy" if use_proxy else "proxy_disabled"
    kwargs: dict[str, Any] = {
        "additional_headers": {"X-Sandbox-ID": f"nmb-smoke-client-probe-{label}"},
        "open_timeout": 10.0,
    }
    if not use_proxy:
        kwargs["proxy"] = None

    try:
        async with websockets.connect(broker_url, **kwargs):
            _print_marker("NMB_SMOKE_WS_OK", mode=label, broker_url=broker_url)
    except TypeError as exc:
        _print_marker("NMB_SMOKE_WS_UNSUPPORTED", mode=label, error=repr(exc))
    except Exception as exc:
        _print_marker("NMB_SMOKE_WS_FAIL", mode=label, broker_url=broker_url, error=repr(exc))


async def run_client_probe(args: argparse.Namespace) -> None:
    """Diagnose broker reachability from the client sandbox only."""
    host, port = _broker_host_port(args.broker_url)
    _print_marker(
        "NMB_SMOKE_CLIENT_PROBE_START",
        broker_url=args.broker_url,
        host=host,
        port=port,
    )
    _probe_dns_and_tcp(host, port)
    await _probe_websocket(args.broker_url, use_proxy=True)
    await _probe_websocket(args.broker_url, use_proxy=False)


async def _wait_until_signal() -> None:
    """Block until SIGTERM/SIGINT so logs remain available after success."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _set_stop() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _set_stop)
    await stop.wait()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="role", required=True)

    broker = subparsers.add_parser("broker")
    _add_common_args(broker)
    broker.add_argument("--host", default="0.0.0.0")
    broker.add_argument("--port", type=int, default=9876)
    broker.add_argument("--audit-db", default="/sandbox/nmb-smoke-audit.db")
    broker.add_argument(
        "--keep-alive",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("NMB_SMOKE_KEEP_ALIVE", "1") != "0",
    )

    client = subparsers.add_parser("client")
    _add_common_args(client)
    client.add_argument(
        "--broker-url",
        default=os.environ.get("NMB_BROKER_URL", "ws://messages.local:9876"),
    )

    probe = subparsers.add_parser("probe")
    _add_common_args(probe)
    probe.add_argument("--broker-url", required=True)
    probe.add_argument("--sandbox-id", default="nmb-smoke-host-probe")

    client_probe = subparsers.add_parser("client-probe")
    _add_common_args(client_probe)
    client_probe.add_argument("--broker-url", required=True)

    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.role == "broker":
        await run_broker(args)
    elif args.role == "client":
        await run_client(args)
    elif args.role == "probe":
        await run_probe(args)
    elif args.role == "client-probe":
        await run_client_probe(args)
    else:
        raise ValueError(f"Unknown role: {args.role}")


if __name__ == "__main__":
    asyncio.run(main())

