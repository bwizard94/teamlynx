"""FieldRelay: tick-batched downlink, immediate events, session log, router-safe imports, bridge."""

import asyncio
import subprocess
import sys
import time

from websockets.asyncio.client import connect

from lynx.field.relay import BRIDGE_SID, FieldRelayConfig
from lynx.field.session import load_records
from lynx.net.client import BackgroundClient, LynxClient
from lynx.net.schema import Ping, PingType, Team, Telemetry, decode, encode, now_us

from .conftest import FieldRelayThread, wait_for


def test_relay_imports_stay_router_safe():
    code = ("import sys, lynx.field.relay, lynx.field.discovery, lynx.field.session; "
            "bad = [m for m in ('numpy', 'cv2', 'scipy') if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_tick_batches_telemetry_but_not_events():
    relay = FieldRelayThread(field=FieldRelayConfig(beacon=False, tick_s=0.2)).start()

    async def main():
        a = await connect(relay.url, compression=None, proxy=None)
        b = await connect(relay.url, compression=None, proxy=None)
        await asyncio.sleep(0.1)
        tel_lat, ping_lat = [], []

        async def rx():
            async for frame in b:
                msg = decode(frame)
                lat = (now_us() - msg.ts_us) / 1000.0
                (tel_lat if isinstance(msg, Telemetry) else ping_lat).append(lat)

        task = asyncio.create_task(rx())
        for i in range(20):
            t = Telemetry(node_id=5, seq=i, ts_us=now_us(), team=Team.BLUE, callsign="E", x=float(i))
            await a.send(encode(t))
            if i == 10:
                p = Ping(node_id=5, seq=1000, ts_us=now_us(), ping_id=1, owner=5, ping_type=PingType.CONTACT,
                         ttl_ms=5000)
                await a.send(encode(p))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.4)
        task.cancel()
        await a.close()
        await b.close()
        return tel_lat, ping_lat

    try:
        tel, ping = asyncio.run(main())
        stats = relay.server.fstats
    finally:
        relay.stop()
    assert len(ping) == 1 and ping[0] < 50.0  # events wake the sender immediately
    assert 0 < len(tel) < 20  # latest-value-wins per tick: 400 ms of 50 Hz -> ~3-5 flushes + 1 with the ping
    assert max(tel) > 100.0  # telemetry waited for the 200 ms tick
    assert stats.batches_out >= 1


def test_per_frame_mode_is_plain_relay():
    relay = FieldRelayThread(field=FieldRelayConfig(beacon=False, tick_s=0.0)).start()
    a = BackgroundClient(LynxClient(relay.url, 1, "A")).start()
    b = BackgroundClient(LynxClient(relay.url, 2, "B")).start()
    try:
        assert wait_for(lambda: a.connected and b.connected)
        for i in range(5):
            a.send_telemetry(i, 0, 1.7, 0, 0, 0)
            time.sleep(0.01)
        assert wait_for(lambda: 1 in b.snapshot()[0] and b.snapshot()[0][1].telemetry.x == 4.0)
        assert relay.server.fstats.batches_out == 0
    finally:
        a.stop()
        b.stop()
        relay.stop()


def test_session_log_records_fanout(tmp_path):
    relay = FieldRelayThread(field=FieldRelayConfig(beacon=False, log_path=str(tmp_path), log_telemetry_hz=5.0),
                             sweep_interval_s=0.05).start()
    a = BackgroundClient(LynxClient(relay.url, 1, "ALPHA")).start()
    try:
        assert wait_for(lambda: a.connected)
        for i in range(40):  # 1 s at 40 Hz -> ~5-6 logged at 5 Hz
            a.send_telemetry(float(i), 0.0, 1.7, 90.0, 0.0, 0.0)
            time.sleep(0.025)
        a.send_ping(10.0, 0.0, 0.0, PingType.DANGER, 30.0)
        time.sleep(0.2)
    finally:
        a.stop()
        relay.stop()
    log = load_records([tmp_path])
    kinds = [(r["kind"], r.get("msg", {}).get("type")) for r in log.records]
    assert kinds[0] == ("meta", None) and log.sources == ["relay"]
    tel = [k for k in kinds if k[1] == "telemetry"]
    assert 4 <= len(tel) <= 8
    assert ("msg", "ping") in kinds and ("msg", "node_leave") in kinds


def bridged_pair(**secondary_field):
    primary = FieldRelayThread(field=FieldRelayConfig(beacon=False, tick_s=0.02), sweep_interval_s=0.05).start()
    fcfg = FieldRelayConfig(beacon=False, tick_s=0.02, role="secondary", upstream=primary.url, **secondary_field)
    secondary = FieldRelayThread(field=fcfg, sweep_interval_s=0.05).start()
    assert wait_for(lambda: secondary.server.bridge.connected.is_set(), 5.0)
    return primary, secondary


def pump(clients, seconds=0.5, rate=20.0):
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end:
        for nid, c in clients.items():
            c.send_telemetry(float(nid) + i * 0.01, 0.0, 1.7, 0.0, 0.0, 0.0)
        i += 1
        time.sleep(1.0 / rate)


def test_bridge_joins_headsets_split_across_relays(tmp_path):
    primary, secondary = bridged_pair(log_path=str(tmp_path))
    a = BackgroundClient(LynxClient(primary.url, 1, "ALPHA")).start()
    b = BackgroundClient(LynxClient(secondary.url, 2, "BRAVO")).start()
    try:
        assert wait_for(lambda: a.connected and b.connected)
        pump({1: a, 2: b})
        assert wait_for(lambda: 2 in a.snapshot()[0] and 1 in b.snapshot()[0], 3.0)
        pa = a.send_ping(1, 2, 0, PingType.MOVE, 30.0)
        pb = b.send_ping(3, 4, 0, PingType.CONTACT, 30.0)
        assert wait_for(lambda: (2, pb.ping_id) in a.snapshot()[1] and (1, pa.ping_id) in b.snapshot()[1], 3.0)
        b.cancel_ping(pb.ping_id)
        assert wait_for(lambda: (2, pb.ping_id) not in a.snapshot()[1], 3.0)
        assert secondary.server.upstream_nodes() == {1}
        assert set(secondary.server.local_nodes()) == {2}
        # clean leave on the secondary reaches the primary's clients at once (not after the 5 s stale sweep)
        b.stop()
        assert wait_for(lambda: 2 not in primary.server.state.node_ids, 1.5)
        assert wait_for(lambda: 2 not in a.snapshot()[0], 1.5)
    finally:
        a.stop()
        primary.stop()
        secondary.stop()
    log = load_records([tmp_path])
    assert any(r.get("msg", {}).get("node_id") == 1 for r in log.records)  # secondary logged upstream traffic


def test_bridge_failover_headset_reclaims_node_and_primary_loss():
    primary, secondary = bridged_pair()
    a = BackgroundClient(LynxClient(primary.url, 1, "ALPHA")).start()
    b = BackgroundClient(LynxClient(secondary.url, 2, "BRAVO")).start()
    c = BackgroundClient(LynxClient(primary.url, 3, "CHARLIE")).start()
    try:
        assert wait_for(lambda: a.connected and b.connected and c.connected)
        pump({1: a, 2: b, 3: c})
        assert wait_for(lambda: secondary.server._owner.get(1) == BRIDGE_SID)
        # ALPHA fails over: its old primary session goes away, a new one opens on the secondary
        a.stop()
        a = BackgroundClient(LynxClient(secondary.url, 1, "ALPHA")).start()
        assert wait_for(lambda: a.connected)
        pump({1: a, 2: b, 3: c})
        assert secondary.server._owner.get(1) not in (None, BRIDGE_SID)  # taken back from the bridge copy
        assert wait_for(lambda: 1 in c.snapshot()[0], 3.0)  # CHARLIE on the primary sees ALPHA via the bridge
        # the primary dies: the secondary keeps serving its local headsets
        c.stop()
        primary.stop()
        pump({1: a, 2: b}, 0.6)
        assert not secondary.server.bridge.connected.is_set()
        assert 2 in a.snapshot()[0] and 1 in b.snapshot()[0]
    finally:
        for x in (a, b, c):
            x.stop()
        secondary.stop()
        primary.stop()
