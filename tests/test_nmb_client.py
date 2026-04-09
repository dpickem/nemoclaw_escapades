"""Tests for the async NMB client against an embedded test broker."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from nemoclaw_escapades.config import BrokerConfig
from nemoclaw_escapades.nmb.broker import NMBBroker
from nemoclaw_escapades.nmb.client import MessageBus, NMBConnectionError


@pytest.fixture
async def broker_and_url() -> tuple[NMBBroker, str]:
    """Start a broker and return (broker, ws_url)."""
    db_path = str(Path(tempfile.mkdtemp()) / "client_test.db")
    config = BrokerConfig(
        host="127.0.0.1",
        port=0,
        audit_db_path=db_path,
        default_request_timeout=5.0,
    )
    b = NMBBroker(config)
    await b.start()
    assert b._server is not None
    for sock in b._server.sockets:
        addr = sock.getsockname()
        url = f"ws://{addr[0]}:{addr[1]}"
        break
    else:
        raise RuntimeError("No sockets")
    yield b, url  # type: ignore[misc]
    await b.stop()


class TestClientConnect:
    async def test_connect_and_close(self, broker_and_url: tuple[NMBBroker, str]) -> None:
        _, url = broker_and_url
        bus = MessageBus(sandbox_id="test-client", broker_url=url)
        await bus.connect()
        await bus.close()

    async def test_connect_to_bad_url_raises(self) -> None:
        bus = MessageBus(sandbox_id="test", broker_url="ws://127.0.0.1:1")
        with pytest.raises(NMBConnectionError):
            await bus.connect()

    async def test_connect_with_retry_succeeds_on_first_try(
        self, broker_and_url: tuple[NMBBroker, str]
    ) -> None:
        _, url = broker_and_url
        bus = MessageBus(sandbox_id="retry-ok", broker_url=url)
        await bus.connect_with_retry(max_retries=3, wait_min=0.05, wait_max=0.5)
        await bus.close()

    async def test_connect_with_retry_exhausted(self) -> None:
        bus = MessageBus(sandbox_id="retry-fail", broker_url="ws://127.0.0.1:1")
        with pytest.raises(NMBConnectionError):
            await bus.connect_with_retry(max_retries=2, wait_min=0.05, wait_max=0.1)


class TestClientSend:
    async def test_send_and_listen(self, broker_and_url: tuple[NMBBroker, str]) -> None:
        _, url = broker_and_url
        sender = MessageBus(sandbox_id="sender", broker_url=url)
        receiver = MessageBus(sandbox_id="receiver", broker_url=url)
        await sender.connect()
        await receiver.connect()

        await sender.send(receiver.sandbox_id, "task.assign", {"prompt": "test"})

        msg = None
        async for m in receiver.listen():
            msg = m
            break

        assert msg is not None
        assert msg.type == "task.assign"
        assert msg.payload == {"prompt": "test"}
        assert msg.from_sandbox.startswith("sender-")

        await sender.close()
        await receiver.close()

    async def test_send_to_offline_target_raises(
        self, broker_and_url: tuple[NMBBroker, str]
    ) -> None:
        """send() must propagate TARGET_OFFLINE as NMBConnectionError."""
        _, url = broker_and_url
        bus = MessageBus(sandbox_id="lonely-sender", broker_url=url)
        await bus.connect()
        with pytest.raises(NMBConnectionError, match="TARGET_OFFLINE"):
            await bus.send("nobody", "ping", {})
        await bus.close()

    async def test_publish_to_empty_channel_succeeds(
        self, broker_and_url: tuple[NMBBroker, str]
    ) -> None:
        """publish() to a channel with no subscribers should still ACK."""
        _, url = broker_and_url
        bus = MessageBus(sandbox_id="pub-empty", broker_url=url)
        await bus.connect()
        await bus.publish("no-subscribers", "event", {"x": 1})
        await bus.close()


class TestClientRequestReply:
    async def test_request_reply(self, broker_and_url: tuple[NMBBroker, str]) -> None:
        _, url = broker_and_url
        requester = MessageBus(sandbox_id="req-client", broker_url=url)
        responder = MessageBus(sandbox_id="resp-client", broker_url=url)
        await requester.connect()
        await responder.connect()

        async def responder_loop() -> None:
            async for msg in responder.listen():
                if msg.type == "review.request":
                    await responder.reply(msg, "review.feedback", {"verdict": "lgtm"})
                    return

        resp_task = asyncio.create_task(responder_loop())

        reply = await requester.request(
            responder.sandbox_id, "review.request", {"diff": "..."}, timeout=5.0
        )

        assert reply.type == "review.feedback"
        assert reply.payload == {"verdict": "lgtm"}

        await resp_task
        await requester.close()
        await responder.close()


class TestClientPubSub:
    async def test_subscribe_and_publish(self, broker_and_url: tuple[NMBBroker, str]) -> None:
        _, url = broker_and_url
        pub = MessageBus(sandbox_id="pub-client", broker_url=url)
        sub = MessageBus(sandbox_id="sub-client", broker_url=url)
        await pub.connect()
        await sub.connect()

        received: list[dict[str, object]] = []

        async def subscribe_loop() -> None:
            async for msg in sub.subscribe("progress.test"):
                received.append(msg.payload or {})
                if len(received) >= 2:
                    return

        sub_task = asyncio.create_task(subscribe_loop())
        await asyncio.sleep(0.1)

        await pub.publish("progress.test", "task.progress", {"pct": 25})
        await pub.publish("progress.test", "task.progress", {"pct": 75})

        await asyncio.wait_for(sub_task, timeout=3)

        assert len(received) == 2
        assert received[0] == {"pct": 25}
        assert received[1] == {"pct": 75}

        await pub.close()
        await sub.close()
