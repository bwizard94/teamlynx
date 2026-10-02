"""Relay integration tests: real WebSockets on localhost with in-process clients."""

import asyncio
import time

import pytest
from websockets.asyncio.client import connect

from lynx.net.client import BackgroundClient, LynxClient
from lynx.net.schema import (
    CancelReason,
    LeaveReason,
    NodeLeave,
    Ping,
    PingCancel,
    PingType,
    Team,
    Telemetry,
    decode,
)
from lynx.net.server import RelayConfig, RelayServer, TokenBucket


async def wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


@pytest.fixture
async def relay():
    cfg = RelayConfig(host="127.0.0.1", port=0, stale_timeout_s=0.5, sweep_interval_s=0.02)
    server = RelayServer(cfg)
    await server.start()
    yield server
    await server.stop()


class Harness:
    def __init__(self, relay):
        self.relay = relay
        self.url = f"ws://127.0.0.1:{relay.port}"
        self.clients = []
        self.tasks = []

    async def client(self, node, callsign=None, encoding="binary", **kw):
        c = LynxClient(self.url, node, callsign or f"N{node}", encoding=encoding, reconnect=False, **kw)
        self.clients.append(c)
        self.tasks.append(asyncio.create_task(c.run()))
        await c.wait_connected()
        return c

    async def close(self):
        for c in self.clients:
            await c.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
async def h(relay):
    harness = Harness(relay)
    yield harness
    await harness.close()


class RawClient:
    """Bare WebSocket peer that records every decoded downlink message."""

    def __init__(self, ws):
        self.ws = ws
        self.received = []
        self.task = asyncio.create_task(self._rx())

    async def _rx(self):
        try:
            async for frame in self.ws:
                self.received.append(decode(frame))
        except Exception:
            pass

    def of_type(self, cls):
        return [m for m in self.received if isinstance(m, cls)]

    async def close(self):
        await self.ws.close()
        await self.task


async def raw(relay, query=""):
    ws = await connect(f"ws://127.0.0.1:{relay.port}/{query}")
    return RawClient(ws)


# -- fan-out ---------------------------------------------------------------------------------


async def test_telemetry_fanout_and_no_echo(h):
    a = await h.client(1, "ALPHA")
    b = await h.client(2, "BRAVO")
    c = await h.client(3, "CHARLIE", encoding="json")
    await a.send_telemetry(1, 2, 1.7, 90, -5, 0)
    assert await wait_until(lambda: b.state.node(1) is not None and c.state.node(1) is not None)
    assert c.state.node(1).telemetry.callsign == "ALPHA"
    assert c.state.node(1).telemetry.heading == 90
    assert a.state.node(1) is None  # sender is not echoed


async def test_json_uplink_binary_downlink_interop(relay, h):
    b = await h.client(2)
    r = await raw(relay, "?encoding=json")
    await r.ws.send(Telemetry(node_id=8, seq=0, callsign="JSON", x=5.0).to_json())
    assert await wait_until(lambda: b.state.node(8) is not None)
    assert b.state.node(8).telemetry.x == 5.0
    await b.send_telemetry(0, 0, 0, 0, 0, 0)
    assert await wait_until(lambda: len(r.of_type(Telemetry)) == 1)
    await r.close()


async def test_ping_and_cancel_propagate(h):
    a = await h.client(1)
    b = await h.client(2)
    p = await a.send_ping(3, 4, 0, PingType.CONTACT, ttl_s=30)
    assert await wait_until(lambda: b.state.ping(p.key) is not None)
    got = b.state.ping(p.key).ping
    assert (got.x, got.y, got.ping_type, got.owner) == (3, 4, PingType.CONTACT, 1)
    await a.cancel_ping(p.ping_id)
    assert await wait_until(lambda: b.state.ping(p.key) is None)


async def test_ping_ttl_expiry_broadcast(relay, h):
    a = await h.client(1)
    r = await raw(relay)
    p = await a.send_ping(0, 0, 0, ttl_s=0.2)
    assert await wait_until(lambda: any(isinstance(m, Ping) for m in r.received))
    assert await wait_until(lambda: any(isinstance(m, PingCancel) for m in r.received), timeout=2)
    cancel = r.of_type(PingCancel)[0]
    assert cancel.reason is CancelReason.EXPIRED and cancel.key == p.key and cancel.node_id == 0
    assert relay.state.ping(p.key) is None
    assert await wait_until(lambda: a.state.ping(p.key) is None)
    await r.close()


async def test_ttl_clamped_and_zero_means_default(relay, h):
    a = await h.client(1)
    p = await a.send_ping(0, 0, 0, ttl_s=10_000)
    assert await wait_until(lambda: relay.state.ping(p.key) is not None)
    assert relay.state.ping(p.key).ping.ttl_ms == relay.config.max_ttl_ms
    r = await raw(relay)
    await r.ws.send(Ping(node_id=5, owner=5, ping_id=1, ttl_ms=0).to_bytes())
    assert await wait_until(lambda: relay.state.ping((5, 1)) is not None)
    assert relay.state.ping((5, 1)).ping.ttl_ms == relay.config.default_ttl_ms
    await r.close()


async def test_per_node_ping_limit_replaces_oldest(relay, h):
    relay.config.max_pings_per_node = 3
    relay.config.ping_burst = 10
    a = await h.client(1)
    b = await h.client(2)
    pings = []
    for i in range(4):
        pings.append(await a.send_ping(i, 0, 0))
        await asyncio.sleep(0.01)
    assert await wait_until(lambda: len(relay.state.pings_by_owner(1)) == 3 and relay.state.ping(pings[0].key) is None)
    assert await wait_until(lambda: b.state.ping(pings[0].key) is None and b.state.ping(pings[3].key) is not None)
    assert await wait_until(lambda: a.state.ping(pings[0].key) is None)  # owner told too


async def test_late_joiner_snapshot_has_remaining_ttl(relay, h):
    a = await h.client(1, "ALPHA")
    await a.send_telemetry(1, 1, 1.7, 0, 0, 0)
    p = await a.send_ping(5, 5, 0, ttl_s=10)
    assert await wait_until(lambda: relay.state.ping(p.key) is not None and relay.state.node(1) is not None)
    await asyncio.sleep(0.3)
    late = await h.client(2)
    assert await wait_until(lambda: late.state.ping(p.key) is not None and late.state.node(1) is not None)
    ttl = late.state.ping(p.key).ping.ttl_ms
    assert 9_000 < ttl <= 9_750


# -- liveness ---------------------------------------------------------------------------------


async def test_stale_node_evicted(relay, h):
    a = await h.client(1)
    b = await h.client(2)
    await a.send_telemetry(0, 0, 0, 0, 0, 0)
    assert await wait_until(lambda: b.state.node(1) is not None)
    # a stays connected but silent -> relay evicts after stale_timeout (0.5 s)
    r = await raw(relay)
    assert await wait_until(lambda: any(isinstance(m, NodeLeave) for m in r.received), timeout=3)
    leave = r.of_type(NodeLeave)[0]
    assert leave.node == 1 and leave.reason is LeaveReason.STALE
    assert await wait_until(lambda: b.state.node(1) is None)
    assert relay.stats.nodes_evicted == 1
    # node comes back
    await a.send_telemetry(1, 0, 0, 0, 0, 0)
    assert await wait_until(lambda: b.state.node(1) is not None)
    await r.close()


async def test_disconnect_broadcasts_leave(relay, h):
    a = await h.client(1)
    b = await h.client(2)
    await a.send_telemetry(0, 0, 0, 0, 0, 0)
    assert await wait_until(lambda: b.state.node(1) is not None)
    await a.close(leave=False)  # abrupt close, no NODE_LEAVE from the client
    assert await wait_until(lambda: b.state.node(1) is None)
    assert relay.state.node(1) is None


async def test_pings_survive_owner_leaving(relay, h):
    a = await h.client(1)
    b = await h.client(2)
    p = await a.send_ping(1, 1, 0, ttl_s=30)
    assert await wait_until(lambda: b.state.ping(p.key) is not None)
    await a.close()
    await asyncio.sleep(0.1)
    assert b.state.ping(p.key) is not None


# -- validation / abuse ------------------------------------------------------------------------


async def test_node_id_ownership(relay, h):
    a = await h.client(1)
    b = await h.client(2)
    await a.send_telemetry(1, 1, 1, 0, 0, 0)
    assert await wait_until(lambda: b.state.node(1) is not None)
    imposter = await raw(relay)
    await imposter.ws.send(Telemetry(node_id=1, seq=999, callsign="FAKE", x=50).to_bytes())
    await imposter.ws.send(PingCancel(node_id=2, owner=2, ping_id=1).to_bytes())  # not its id either
    await asyncio.sleep(0.2)
    assert b.state.node(1).telemetry.callsign == "N1"
    assert relay.stats.rejected >= 1
    await imposter.close()


async def test_only_owner_may_create_or_cancel(relay, h):
    a = await h.client(1)
    b = await h.client(2)
    p = await a.send_ping(0, 0, 0)
    assert await wait_until(lambda: relay.state.ping(p.key) is not None)
    r = await raw(relay)
    await r.ws.send(PingCancel(node_id=9, owner=1, ping_id=p.ping_id).to_bytes())
    await r.ws.send(Ping(node_id=9, owner=1, ping_id=77).to_bytes())
    await r.ws.send(NodeLeave(node_id=9, node=1).to_bytes())
    await asyncio.sleep(0.2)
    assert relay.state.ping(p.key) is not None
    assert relay.state.ping((1, 77)) is None
    assert b.state.ping(p.key) is not None
    assert relay.stats.rejected == 3
    await r.close()


async def test_bad_frames_ignored_then_disconnected(relay, h):
    relay.config.max_consecutive_errors = 5
    b = await h.client(2)
    r = await raw(relay)
    await r.ws.send(b"garbage")
    await r.ws.send("{not json")
    await r.ws.send(Telemetry(node_id=4, seq=0, callsign="OK").to_bytes())
    assert await wait_until(lambda: b.state.node(4) is not None)
    for _ in range(5):
        await r.ws.send(b"\x00" * 56)
    await asyncio.wait_for(r.task, 2)
    assert r.ws.close_code == 1007
    assert relay.stats.decode_errors == 7


async def test_out_of_order_telemetry_dropped(relay, h):
    b = await h.client(2)
    r = await raw(relay)
    await r.ws.send(Telemetry(node_id=4, seq=10, x=1.0).to_bytes())
    await r.ws.send(Telemetry(node_id=4, seq=9, x=2.0).to_bytes())
    await r.ws.send(Telemetry(node_id=4, seq=10, x=3.0).to_bytes())
    await r.ws.send(Telemetry(node_id=4, seq=11, x=4.0).to_bytes())
    assert await wait_until(lambda: b.state.node(4) is not None and b.state.node(4).telemetry.x == 4.0)
    assert relay.stats.stale_seq == 2
    await r.close()


async def test_inbound_rate_limit(relay, h):
    relay.config.telemetry_rate_hz = 10
    relay.config.telemetry_burst = 5
    r = await raw(relay)
    for i in range(50):
        await r.ws.send(Telemetry(node_id=4, seq=i).to_bytes())
    assert await wait_until(lambda: relay.stats.frames_in == 50)
    assert 40 <= relay.stats.rate_limited <= 45
    await r.close()


async def test_slow_consumer_gets_latest_telemetry_only(relay):
    """Outbound telemetry is latest-value-wins per node: a backlog never accumulates."""
    from lynx.net.server import _Session

    class FakeWS:
        async def send(self, data):
            pass

    sess = _Session(ws=FakeWS(), encoding="binary", sid=999)
    for i in range(100):
        relay._enqueue(sess, Telemetry(node_id=4, seq=i, x=float(i)))
    assert len(sess.pending_telemetry) == 1
    assert sess.pending_telemetry[4].x == 99.0
    relay._enqueue(sess, NodeLeave(node=4))
    assert 4 not in sess.pending_telemetry  # leave supersedes pending pose
    assert len(sess.events) == 1


def test_token_bucket():
    b = TokenBucket(rate=2.0, burst=3.0, now=0.0)
    assert [b.allow(0.0) for _ in range(4)] == [True, True, True, False]
    assert b.allow(0.5)
    assert not b.allow(0.5)
    assert b.allow(10.0)


# -- client resilience --------------------------------------------------------------------------


async def test_client_reconnects_and_republishes_pings():
    cfg = RelayConfig(host="127.0.0.1", port=0, sweep_interval_s=0.02)
    relay = RelayServer(cfg)
    await relay.start()
    port = relay.port
    c = LynxClient(f"ws://127.0.0.1:{port}", 1, "A", backoff_initial_s=0.05, backoff_max_s=0.1)
    task = asyncio.create_task(c.run())
    try:
        await c.wait_connected()
        p = await c.send_ping(1, 2, 0, ttl_s=30)
        assert await wait_until(lambda: relay.state.ping(p.key) is not None)
        await relay.stop()
        assert await wait_until(lambda: not c.connected.is_set())
        relay2 = RelayServer(RelayConfig(host="127.0.0.1", port=port, sweep_interval_s=0.02))
        await relay2.start()
        try:
            assert await wait_until(lambda: relay2.state.ping(p.key) is not None, timeout=5)
            assert relay2.state.ping(p.key).ping.ttl_ms < 30_000
        finally:
            await c.close()
            await task
            await relay2.stop()
    finally:
        if not task.done():
            task.cancel()


def test_background_client_thread(relay_url_thread):
    url, relay_state = relay_url_thread
    bc = BackgroundClient(LynxClient(url, 5, "BG", Team.AMBER)).start()
    try:
        deadline = time.monotonic() + 3
        while not bc.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bc.connected
        bc.send_telemetry(1, 2, 3, 4, 5, 6)
        p = bc.send_ping(7, 8, 0, PingType.RALLY, 5.0)
        assert p is not None and p.owner == 5
        _, pings = bc.snapshot()
        assert p.key in pings
        deadline = time.monotonic() + 3
        while relay_state().node(5) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert relay_state().node(5).telemetry.team is Team.AMBER
    finally:
        bc.stop()


@pytest.fixture
def relay_url_thread():
    """A relay running on its own loop in a thread (for testing the thread-hosted client)."""
    import threading

    loop = asyncio.new_event_loop()
    holder = {}
    started = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        server = RelayServer(RelayConfig(host="127.0.0.1", port=0))
        loop.run_until_complete(server.start())
        holder["server"] = server
        started.set()
        loop.run_forever()
        loop.run_until_complete(server.stop())
        loop.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    started.wait(5)
    server = holder["server"]
    yield f"ws://127.0.0.1:{server.port}", lambda: server.state
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
