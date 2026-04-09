"""CLI tool for the NemoClaw Message Bus.

Provides command-line access to the NMB for testing, shell-based
agent integrations, and debugging.

Usage::

    nmb-client send orchestrator task.complete '{"diff": "..."}'
    nmb-client request review-sandbox-1 review.request '{"diff": "..."}' --timeout 300
    nmb-client listen
    nmb-client subscribe progress.coding-1
    nmb-client publish progress.coding-1 task.progress '{"status": "running"}'
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``nmb-client`` CLI.

    Parses arguments, connects via the sync ``MessageBus``, executes the
    requested subcommand (send, request, listen, subscribe, publish),
    and prints results as JSON to stdout.

    Args:
        argv: Argument list to parse.  Defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        prog="nmb-client",
        description="NemoClaw Message Bus CLI",
    )
    parser.add_argument(
        "--sandbox-id",
        default="",
        help="Sandbox identity (default: $NMB_SANDBOX_ID)",
    )
    parser.add_argument(
        "--url",
        default="ws://messages.local:9876",
        help="Broker WebSocket URL",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = sub.add_parser("send", help="Send a fire-and-forget message")
    p_send.add_argument("to", help="Target sandbox ID")
    p_send.add_argument("type", help="Message type")
    p_send.add_argument("payload", help="JSON payload")

    # request
    p_req = sub.add_parser("request", help="Send a request and wait for reply")
    p_req.add_argument("to", help="Target sandbox ID")
    p_req.add_argument("type", help="Message type")
    p_req.add_argument("payload", help="JSON payload")
    p_req.add_argument("--timeout", type=float, default=300.0, help="Reply timeout (seconds)")

    # listen
    sub.add_parser("listen", help="Listen for incoming messages (blocking)")

    # subscribe
    p_sub = sub.add_parser("subscribe", help="Subscribe to a channel")
    p_sub.add_argument("channel", help="Channel name")

    # publish
    p_pub = sub.add_parser("publish", help="Publish to a channel")
    p_pub.add_argument("channel", help="Channel name")
    p_pub.add_argument("type", help="Message type")
    p_pub.add_argument("payload", help="JSON payload")

    args = parser.parse_args(argv)

    from nemoclaw_escapades.nmb.models import serialize_frame
    from nemoclaw_escapades.nmb.sync import MessageBus

    bus = MessageBus(sandbox_id=args.sandbox_id, url=args.url)

    try:
        bus.connect()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == "send":
            payload = json.loads(args.payload)
            bus.send(args.to, args.type, payload)
            print("Sent.")

        elif args.command == "request":
            payload = json.loads(args.payload)
            reply = bus.request(args.to, args.type, payload, timeout=args.timeout)
            print(serialize_frame(reply))

        elif args.command == "listen":
            for msg in bus.listen():
                print(serialize_frame(msg))
                sys.stdout.flush()

        elif args.command == "subscribe":
            for msg in bus.subscribe(args.channel):
                print(serialize_frame(msg))
                sys.stdout.flush()

        elif args.command == "publish":
            payload = json.loads(args.payload)
            bus.publish(args.channel, args.type, payload)
            print("Published.")

    except KeyboardInterrupt:
        pass
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON payload: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
