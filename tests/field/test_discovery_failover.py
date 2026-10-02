"""Relay discovery (UDP beacon, candidate ordering) and the failover client."""

import asyncio
import random
import time

import pytest

from lynx.field.discovery import (
    RelayAnnouncement,
    RelayBeacon,
    discover_beacons,
    encode_announcement,
    encode_query,
    is_query,
    parse_announcement,
    resolve_relay_urls,
)
from lynx.field.failover import FailoverClient, probe_relay
from lynx.field.relay import FieldRelayConfig
from lynx.net.client import BackgroundClient, LynxClient
from lynx.net.schema import Team

from .conftest import FieldRelayThread, free_port, free_udp_port, wait_for


def test_announcement_codec_and_validation():
    data = encode_announcement(8765, "secondary", 10, "LYNX", "jetson-01", 3)
    a = parse_announcement(data, ("192.168.8.11", 8766))
    assert a == RelayAnnouncement("192.168.8.11", 8765, "secondary", 10, "LYNX", "jetson-01", 3)
    assert a.url == "ws://192.168.8.11:8765"
    assert RelayAnnouncement("fe80::1", 8765).url == "ws://[fe80::1]:8765"
    assert parse_announcement(b"not json", ("1.2.3.4", 1)) is None
    assert parse_announcement(b'{"svc":"other","v":1}', ("1.2.3.4", 1)) is None
    assert parse_announcement(b'{"svc":"lynx-relay","v":1,"port":99999}', ("1.2.3.4", 1)) is None
    assert parse_announcement(encode_query("LYNX"), ("1.2.3.4", 1)) is None
    assert is_query(encode_query("LYNX")) == "LYNX" and is_query(data) is None
    assert parse_announcement(b"x" * 600, ("1.2.3.4", 1)) is None
    ordered = sorted([RelayAnnouncement("b", role="secondary", priority=10), RelayAnnouncement("a", priority=0),
                      RelayAnnouncement("c", role="secondary", priority=0)], key=lambda r: r.sort_key)
    assert [r.host for r in ordered] == ["a", "c", "b"]


def test_beacon_answers_queries_and_announces():
    bport, lport = free_udp_port(), free_udp_port()

    async def main():
        primary = RelayBeacon(8765, "primary", 0, "LYNX", "router", bind=("127.0.0.1", bport),
                              targets=[("127.0.0.1", lport)], interval_s=0.2, clients_fn=lambda: 4)
        await primary.start()
        try:
            # active query (fast path) + passive announcements on the listen port
            found = await asyncio.to_thread(discover_beacons, 0.6, "LYNX", [("127.0.0.1", bport)], lport)
            other_squad = await asyncio.to_thread(discover_beacons, 0.3, "WOLF", [("127.0.0.1", bport)], None)
        finally:
            await primary.stop()
        return found, other_squad, primary

    found, other, beacon = asyncio.run(main())
    assert [(a.host, a.port, a.role, a.clients) for a in found] == [("127.0.0.1", 8765, "primary", 4)]
    assert other == [] and beacon.queries >= 1 and beacon.sent >= 2


def test_resolve_orders_explicit_then_beacons_then_dns(monkeypatch):
    import lynx.field.discovery as d

    monkeypatch.setattr(d, "discover_beacons", lambda *a, **k: [RelayAnnouncement("10.0.0.2", 8765, "primary"),
                                                                RelayAnnouncement("10.0.0.3", 8765, "secondary", 10)])
    monkeypatch.setattr(d, "_resolves", lambda name, t: name == "relay.lynx")
    monkeypatch.setattr(d, "default_gateway", lambda: "10.0.0.1")
    urls = resolve_relay_urls(["ws://override:9000", "ws://10.0.0.2:8765"], mdns=False)
    assert urls == ["ws://override:9000", "ws://10.0.0.2:8765", "ws://10.0.0.3:8765", "ws://relay.lynx:8765",
                    "ws://10.0.0.1:8765"]


# ------------------------------------------------------------------------------------- failover


def fast_client(urls, node=1, callsign="ALPHA", **kw) -> FailoverClient:
    kw.setdefault("rng", random.Random(1))
    return FailoverClient(urls, node, callsign, Team.BLUE, backoff_initial_s=0.05, backoff_max_s=0.2,
                          keepalive_interval_s=0.2, keepalive_timeout_s=0.4, open_timeout_s=0.5,
                          probe_interval_s=0.2, failback_after=2, **kw)


def test_failover_to_secondary_and_failback_with_pings_carried():
    port_a = free_port()
    a = FieldRelayThread(port_a, sweep_interval_s=0.05).start()
    b = FieldRelayThread(sweep_interval_s=0.05).start()
    fc = fast_client([a.url, b.url])
    net = BackgroundClient(fc).start()
    peer = BackgroundClient(LynxClient(b.url, 9, "WATCH", reconnect=True, backoff_initial_s=0.05)).start()
    try:
        assert wait_for(lambda: net.connected and fc.on_primary)
        ping = net.send_ping(5.0, 5.0, 0.0, ttl_s=60.0)
        assert ping is not None
        a.stop()
        assert wait_for(lambda: net.connected and fc.current_url == b.url, 5.0)
        assert fc.failovers == 1
        # the own ping was re-published to the secondary: a client there sees it
        assert wait_for(lambda: (1, ping.ping_id) in peer.snapshot()[1], 3.0)
        a = FieldRelayThread(port_a, sweep_interval_s=0.05).start()
        assert wait_for(lambda: net.connected and fc.on_primary, 6.0)
        assert fc.failbacks == 1
        kinds = [e.kind for e in fc.events]
        assert "failback" in kinds and kinds.count("connected") >= 3
        assert wait_for(lambda: (1, ping.ping_id) in a.server.state.ping_keys, 3.0)
    finally:
        net.stop()
        peer.stop()
        a.stop()
        b.stop()


def test_dead_link_detected_by_keepalive_and_all_down_backoff_is_jittered():
    dead = [f"ws://127.0.0.1:{free_port()}", f"ws://127.0.0.1:{free_port()}"]
    fc = fast_client(dead, rng=random.Random(7))
    sleeps = []

    async def main():
        orig = fc._sleep

        async def record(s):
            sleeps.append(s)
            await orig(min(s, 0.01))

        fc._sleep = record
        task = asyncio.create_task(fc.run())
        await asyncio.sleep(0.5)
        await fc.close(leave=False)
        await asyncio.wait_for(task, 2.0)

    asyncio.run(main())
    assert len(sleeps) >= 3
    caps = [min(0.2, 0.05 * 2 ** i) for i in range(len(sleeps))]
    assert all(0.0 <= s <= c + 1e-9 for s, c in zip(sleeps, caps))
    assert len(set(round(s, 6) for s in sleeps)) == len(sleeps)  # jittered, not lock-step
    assert sum(e.kind == "connect-failed" for e in fc.events) >= 2 * len(sleeps)
    assert fc.link_down_s() > 0


def test_rediscovery_replaces_candidates():
    relay = FieldRelayThread(sweep_interval_s=0.05).start()
    calls = []

    def rediscover():
        calls.append(1)
        return [relay.url]

    fc = fast_client([f"ws://127.0.0.1:{free_port()}"], rediscover=rediscover)
    net = BackgroundClient(fc).start()
    try:
        assert wait_for(lambda: net.connected, 5.0)
        assert fc.urls == [relay.url] and calls
        assert any(e.kind == "rediscovered" for e in fc.events)
    finally:
        net.stop()
        relay.stop()


def test_probe_relay(field_relay):
    assert asyncio.run(probe_relay(field_relay.url, 1.0))
    assert not asyncio.run(probe_relay(f"ws://127.0.0.1:{free_port()}", 0.3))


def test_failover_client_requires_url():
    with pytest.raises(ValueError):
        FailoverClient([], 1, "A")


def test_dscp_marked_on_both_ends(field_relay):
    from lynx.field.sockopts import get_tos

    fc = fast_client([field_relay.url])
    net = BackgroundClient(fc).start()
    try:
        assert wait_for(lambda: net.connected)
        assert get_tos(fc._ws) == 46 << 2
        sess = next(iter(field_relay.server._sessions.values()))
        assert get_tos(sess.ws) == 46 << 2
    finally:
        net.stop()


def test_beacon_starts_inside_field_relay():
    bport, lport = free_udp_port(), free_udp_port()
    relay = FieldRelayThread(field=FieldRelayConfig(beacon_bind=("127.0.0.1", bport),
                                                    beacon_targets=[("127.0.0.1", lport)], role="secondary",
                                                    priority=10)).start()
    try:
        found = discover_beacons(0.5, "LYNX", [("127.0.0.1", bport)], None)
        assert [(a.role, a.priority, a.port) for a in found] == [("secondary", 10, relay.port)]
    finally:
        relay.stop()
    time.sleep(0.05)
