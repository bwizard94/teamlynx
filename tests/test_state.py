from lynx.net.schema import NodeLeave, Ping, PingCancel, Telemetry
from lynx.net.state import WorldState


def tel(node, seq, x=0.0):
    return Telemetry(node_id=node, seq=seq, callsign="T", x=x)


def test_telemetry_sequence_filtering():
    s = WorldState(seq_reset_s=2.0)
    assert s.apply(tel(1, 10, 1.0), now=0.0)
    assert not s.apply(tel(1, 10, 2.0), now=0.1)  # duplicate
    assert not s.apply(tel(1, 9, 3.0), now=0.2)  # reordered
    assert s.node(1).telemetry.x == 1.0
    assert s.apply(tel(1, 11, 4.0), now=0.3)
    assert s.node(1).telemetry.x == 4.0


def test_sequence_reset_after_silence_and_force():
    s = WorldState(seq_reset_s=2.0)
    s.apply(tel(1, 1000), now=0.0)
    assert s.apply(tel(1, 0), now=5.0)  # sender restarted after being quiet
    s.apply(tel(1, 50), now=5.1)
    assert s.apply(tel(1, 3), now=5.2, force=True)


def test_ping_lifecycle():
    s = WorldState()
    p = Ping(node_id=2, owner=2, ping_id=1, ttl_ms=1500)
    assert s.apply(p, now=10.0)
    assert s.ping((2, 1)).remaining_ms(10.5) == 1000
    assert s.expire_pings(11.0) == []
    expired = s.expire_pings(11.5)
    assert [e.ping.key for e in expired] == [(2, 1)]
    assert s.ping((2, 1)) is None
    s.apply(p, now=0.0)
    assert s.apply(PingCancel(owner=2, ping_id=1), now=0.1)
    assert not s.apply(PingCancel(owner=2, ping_id=1), now=0.2)


def test_stale_eviction_and_leave():
    s = WorldState()
    s.apply(tel(1, 0), now=0.0)
    s.apply(tel(2, 0), now=4.0)
    assert s.evict_stale(now=5.5, timeout_s=5.0) == [1]
    assert s.node_ids == [2]
    assert s.apply(NodeLeave(node=2), now=6.0)
    assert len(s) == 0


def test_snapshot_is_a_copy():
    s = WorldState()
    s.apply(tel(1, 0, 1.0), now=0.0)
    nodes, _ = s.snapshot()
    nodes[1].telemetry.x = 99.0
    assert s.node(1).telemetry.x == 1.0


def test_pings_by_owner_sorted_by_arrival():
    s = WorldState()
    for i, t in [(3, 2.0), (1, 0.0), (2, 1.0)]:
        s.apply(Ping(owner=5, ping_id=i, ttl_ms=10000), now=t)
    s.apply(Ping(owner=6, ping_id=1, ttl_ms=10000), now=0.5)
    assert [p.ping.ping_id for p in s.pings_by_owner(5)] == [1, 2, 3]
