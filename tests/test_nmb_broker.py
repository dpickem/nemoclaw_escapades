"""Integration tests for the NMB broker — connect, send, request-reply, pub/sub, errors."""

from __future__ import annotations

import asyncio

import pytest
import websockets

from nemoclaw_escapades.config import BrokerConfig
from nemoclaw_escapades.nmb.broker import NMBBroker
from nemoclaw_escapades.nmb.models import NMBMessage, Op


@pytest.fixture
async def broker(tmp_path: object) -> NMBBroker:
    """Start a broker on a random port with a temp audit DB."""
    import tempfile
    from pathlib import Path

    db_path = str(Path(tempfile.mkdtemp()) / "test.db")
    config = BrokerConfig(
        host="127.0.0.1",
        port=0,  # let OS pick
        audit_db_path=db_path,
        default_request_timeout=5.0,
    )
    b = NMBBroker(config)
    await b.start()
    yield b  # type: ignore[misc]
    await b.stop()


def _broker_url(broker: NMBBroker) -> str:
    """Extract the ws:// URL from a running broker."""
    assert broker._server is not None
    for sock in broker._server.sockets:
        addr = sock.getsockname()
        return f"ws://{addr[0]}:{addr[1]}"
    raise RuntimeError("No sockets")


async def _connect(
    broker: NMBBroker, sandbox_id: str
) -> websockets.asyncio.client.ClientConnection:
    url = _broker_url(broker)
    return await websockets.connect(url, additional_headers={"X-Sandbox-ID": sandbox_id})


class TestBrokerConnect:
    async def test_connect_registers_sandbox(self, broker: NMBBroker) -> None:
        ws = await _connect(broker, "test-sandbox")
        await asyncio.sleep(0.05)
        assert "test-sandbox" in broker._connections
        await ws.close()

    async def test_connect_without_header_rejected(self, broker: NMBBroker) -> None:
        url = _broker_url(broker)
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await websockets.connect(url)

    async def test_disconnect_unregisters(self, broker: NMBBroker) -> None:
        ws = await _connect(broker, "ephemeral")
        await asyncio.sleep(0.05)
        assert "ephemeral" in broker._connections
        await ws.close()
        await asyncio.sleep(0.1)
        assert "ephemeral" not in broker._connections


class TestBrokerSend:
    async def test_send_delivers_to_target(self, broker: NMBBroker) -> None:
        sender = await _connect(broker, "sender")
        receiver = await _connect(broker, "receiver")
        await asyncio.sleep(0.05)

        msg = NMBMessage(op=Op.SEND, to_sandbox="receiver", type="task.assign", payload={"x": 1})
        await sender.send(msg.to_json())

        # Sender gets ACK
        ack_raw = await asyncio.wait_for(sender.recv(), timeout=2)
        ack = NMBMessage.from_json(str(ack_raw))
        assert ack.op == Op.ACK

        # Receiver gets deliver
        deliver_raw = await asyncio.wait_for(receiver.recv(), timeout=2)
        deliver = NMBMessage.from_json(str(deliver_raw))
        assert deliver.op == Op.DELIVER
        assert deliver.type == "task.assign"
        assert deliver.from_sandbox == "sender"
        assert deliver.payload == {"x": 1}

        await sender.close()
        await receiver.close()

    async def test_send_to_offline_returns_error(self, broker: NMBBroker) -> None:
        sender = await _connect(broker, "lonely")
        await asyncio.sleep(0.05)

        msg = NMBMessage(op=Op.SEND, to_sandbox="nobody", type="t", payload={})
        await sender.send(msg.to_json())

        err_raw = await asyncio.wait_for(sender.recv(), timeout=2)
        err = NMBMessage.from_json(str(err_raw))
        assert err.op == Op.ERROR
        assert err.code == "TARGET_OFFLINE"

        await sender.close()


class TestBrokerRequestReply:
    async def test_request_reply_flow(self, broker: NMBBroker) -> None:
        requester = await _connect(broker, "requester")
        responder = await _connect(broker, "responder")
        await asyncio.sleep(0.05)

        req = NMBMessage(
            op=Op.REQUEST,
            to_sandbox="responder",
            type="review.request",
            timeout=10.0,
            payload={"diff": "..."},
        )
        await requester.send(req.to_json())

        # Requester gets ACK
        ack_raw = await asyncio.wait_for(requester.recv(), timeout=2)
        ack = NMBMessage.from_json(str(ack_raw))
        assert ack.op == Op.ACK

        # Responder gets deliver
        deliver_raw = await asyncio.wait_for(responder.recv(), timeout=2)
        deliver = NMBMessage.from_json(str(deliver_raw))
        assert deliver.op == Op.DELIVER
        assert deliver.type == "review.request"

        # Responder sends reply
        reply = NMBMessage(
            op=Op.REPLY,
            reply_to=deliver.id,
            type="review.feedback",
            payload={"verdict": "approve"},
        )
        await responder.send(reply.to_json())

        # Requester gets the reply delivered
        reply_raw = await asyncio.wait_for(requester.recv(), timeout=2)
        reply_deliver = NMBMessage.from_json(str(reply_raw))
        assert reply_deliver.op == Op.DELIVER
        assert reply_deliver.type == "review.feedback"
        assert reply_deliver.payload == {"verdict": "approve"}

        await requester.close()
        await responder.close()

    async def test_request_timeout(self, broker: NMBBroker) -> None:
        requester = await _connect(broker, "timeout-req")
        target = await _connect(broker, "timeout-target")
        await asyncio.sleep(0.05)

        # Override broker timeout to be very short for the test
        broker.config.default_request_timeout = 0.5

        req = NMBMessage(
            op=Op.REQUEST,
            to_sandbox="timeout-target",
            type="slow.request",
            payload={},
        )
        await requester.send(req.to_json())

        # ACK
        ack_raw = await asyncio.wait_for(requester.recv(), timeout=2)
        NMBMessage.from_json(str(ack_raw))

        # Target gets deliver but does NOT reply
        await asyncio.wait_for(target.recv(), timeout=2)

        # Requester should get a timeout
        timeout_raw = await asyncio.wait_for(requester.recv(), timeout=3)
        timeout_msg = NMBMessage.from_json(str(timeout_raw))
        assert timeout_msg.op == Op.TIMEOUT

        await requester.close()
        await target.close()


class TestBrokerPubSub:
    async def test_publish_delivers_to_subscribers(self, broker: NMBBroker) -> None:
        pub = await _connect(broker, "publisher")
        sub = await _connect(broker, "subscriber")
        await asyncio.sleep(0.05)

        # Subscribe
        sub_msg = NMBMessage(op=Op.SUBSCRIBE, channel="progress.c1")
        await sub.send(sub_msg.to_json())
        ack_raw = await asyncio.wait_for(sub.recv(), timeout=2)
        assert NMBMessage.from_json(str(ack_raw)).op == Op.ACK

        # Publish
        pub_msg = NMBMessage(
            op=Op.PUBLISH,
            channel="progress.c1",
            type="task.progress",
            payload={"pct": 50},
        )
        await pub.send(pub_msg.to_json())
        await asyncio.wait_for(pub.recv(), timeout=2)  # ACK for publisher

        # Subscriber receives
        deliver_raw = await asyncio.wait_for(sub.recv(), timeout=2)
        deliver = NMBMessage.from_json(str(deliver_raw))
        assert deliver.op == Op.DELIVER
        assert deliver.payload == {"pct": 50}

        await pub.close()
        await sub.close()

    async def test_publisher_does_not_receive_own_message(self, broker: NMBBroker) -> None:
        ws = await _connect(broker, "self-pub")
        await asyncio.sleep(0.05)

        sub_msg = NMBMessage(op=Op.SUBSCRIBE, channel="echo")
        await ws.send(sub_msg.to_json())
        await asyncio.wait_for(ws.recv(), timeout=2)  # ACK

        pub_msg = NMBMessage(op=Op.PUBLISH, channel="echo", type="t", payload={})
        await ws.send(pub_msg.to_json())
        await asyncio.wait_for(ws.recv(), timeout=2)  # ACK

        # Should NOT receive a deliver of own message
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.5)

        await ws.close()


class TestBrokerTimeoutAudit:
    async def test_timeout_updates_delivery_status(self, broker: NMBBroker) -> None:
        """Timeout must UPDATE the existing audit row, not INSERT a duplicate."""
        requester = await _connect(broker, "audit-req")
        target = await _connect(broker, "audit-target")
        await asyncio.sleep(0.05)

        broker.config.default_request_timeout = 0.3

        req = NMBMessage(
            op=Op.REQUEST,
            to_sandbox="audit-target",
            type="slow.op",
            payload={},
        )
        await requester.send(req.to_json())

        await asyncio.wait_for(requester.recv(), timeout=2)  # ACK
        await asyncio.wait_for(target.recv(), timeout=2)  # deliver

        # Wait for timeout to fire and background writer to flush
        await asyncio.sleep(0.8)

        assert broker._audit is not None
        rows = await broker._audit.query(
            "SELECT delivery_status FROM messages WHERE id = :id",
            {"id": req.id},
        )
        assert len(rows) == 1, f"Expected exactly 1 row, got {len(rows)}"
        assert rows[0]["delivery_status"] == "timeout"

        await requester.close()
        await target.close()


class TestBrokerFanout:
    async def test_publish_to_multiple_subscribers_concurrently(
        self, broker: NMBBroker
    ) -> None:
        """Publish should deliver to all subscribers without serialized blocking."""
        pub = await _connect(broker, "fanout-pub")
        sub1 = await _connect(broker, "fanout-sub1")
        sub2 = await _connect(broker, "fanout-sub2")
        await asyncio.sleep(0.05)

        for sub in (sub1, sub2):
            sub_msg = NMBMessage(op=Op.SUBSCRIBE, channel="fanout.test")
            await sub.send(sub_msg.to_json())
            ack = await asyncio.wait_for(sub.recv(), timeout=2)
            assert NMBMessage.from_json(str(ack)).op == Op.ACK

        pub_msg = NMBMessage(
            op=Op.PUBLISH,
            channel="fanout.test",
            type="event",
            payload={"seq": 1},
        )
        await pub.send(pub_msg.to_json())
        await asyncio.wait_for(pub.recv(), timeout=2)  # ACK

        d1 = NMBMessage.from_json(str(await asyncio.wait_for(sub1.recv(), timeout=2)))
        d2 = NMBMessage.from_json(str(await asyncio.wait_for(sub2.recv(), timeout=2)))
        assert d1.op == Op.DELIVER
        assert d2.op == Op.DELIVER
        assert d1.payload == {"seq": 1}
        assert d2.payload == {"seq": 1}

        await pub.close()
        await sub1.close()
        await sub2.close()


class TestBrokerDuplicateConnection:
    async def test_duplicate_sandbox_id_is_rejected(self, broker: NMBBroker) -> None:
        """A second connection with the same sandbox_id must be rejected."""
        ws1 = await _connect(broker, "dup-sandbox")
        await asyncio.sleep(0.05)
        assert "dup-sandbox" in broker._connections
        original_ws_id = id(broker._connections["dup-sandbox"])

        ws2 = await _connect(broker, "dup-sandbox")
        await asyncio.sleep(0.1)

        # The original connection is still the registered one
        assert id(broker._connections["dup-sandbox"]) == original_ws_id

        # ws2 was closed by the broker
        with pytest.raises((websockets.ConnectionClosed, asyncio.TimeoutError)):
            await asyncio.wait_for(ws2.recv(), timeout=0.5)

        await ws1.close()
        await asyncio.sleep(0.1)


class TestBrokerHealth:
    async def test_health_reports_connections(self, broker: NMBBroker) -> None:
        ws = await _connect(broker, "health-check")
        await asyncio.sleep(0.05)
        health = broker.health()
        assert "health-check" in health["connected_sandboxes"]
        assert health["num_connections"] == 1
        await ws.close()
