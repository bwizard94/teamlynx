"""Reduced 10-headset load test against the field relay, and the airtime model."""

import asyncio
import math

import pytest

from lynx.field.bandwidth import (
    MAC_OVERHEAD_B,
    TCPIP_OVERHEAD_B,
    budget_table,
    format_table,
    frame_airtime_us,
    link_budget,
)
from lynx.field.loadtest import LoadTestConfig, percentile, run_loadtest, run_spawned
from lynx.field.relay import FieldRelayConfig

from .conftest import FieldRelayThread


@pytest.mark.parametrize("tick_s", [0.025, 0.0])
def test_ten_headsets_reduced(tick_s):
    relay = FieldRelayThread(field=FieldRelayConfig(beacon=False, tick_s=tick_s)).start()
    try:
        cfg = LoadTestConfig(url=relay.url, nodes=10, rate_hz=20.0, duration_s=2.0, warmup_s=0.5,
                             ping_interval_s=0.7)
        r = asyncio.run(run_loadtest(cfg))
    finally:
        relay.stop()
    print(r.format())
    assert not r.errors
    assert r.telemetry_sent >= 10 * 20 * 2 * 0.9
    assert r.telemetry_delivery >= 0.98
    assert r.pings_sent >= 10 and r.ping_loss == 0
    assert r.latency_ms["p99"] < 250.0  # generous for shared CI runners (desktop: ~2 ms / ~25 ms with the tick)
    if tick_s:
        assert r.latency_ms["p50"] > 2.0  # the tick adds ~tick/2 on average
    # application bytes match the model exactly (56 B frames; pings/cancels are a few extra)
    assert r.relay_in_Bps == pytest.approx(10 * 20 * 56, rel=0.15)
    assert r.relay_out_Bps == pytest.approx(10 * 9 * 20 * 56, rel=0.15)


def test_spawned_relay_cli_path():
    cfg = LoadTestConfig(nodes=4, rate_hz=10.0, duration_s=1.0, warmup_s=0.3, ping_interval_s=0.5)
    r = run_spawned(cfg, 25.0)
    assert r.ping_loss == 0 and r.telemetry_delivery >= 0.98 and not r.errors
    assert "tick 25 ms" in r.label
    d = r.to_dict()
    assert d["ping_loss"] == 0 and d["nodes"] == 4


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5.0], 99) == 5.0
    assert math.isnan(percentile([], 50))


def test_frame_airtime_closed_form():
    # 56 B telemetry uplink at MCS0, AC_BE, by hand:
    mpdu = 56 + 6 + TCPIP_OVERHEAD_B + MAC_OVERHEAD_B  # 160 B
    data = 36 + math.ceil((16 + 8 * mpdu + 6) / 26) * 4  # 36 + 50 symbols * 4 us = 236 us
    ack = 20 + math.ceil((16 + 112 + 6) / 24) * 4  # 6 Mbps ACK: 44 us
    expect = 16 + 3 * 9 + 7.5 * 9 + data + 16 + ack
    assert frame_airtime_us(mpdu, "mcs0", "be") == pytest.approx(expect)
    assert frame_airtime_us(mpdu, "mcs0", "vi") < frame_airtime_us(mpdu, "mcs0", "be")
    assert frame_airtime_us(mpdu, "mcs7", "vi") < frame_airtime_us(mpdu, "mcs0", "vi")


def test_budget_shape():
    b = link_budget(10, 20.0, "mcs3", tick_hz=None)
    assert b.app_up_Bps == 20 * 56 and b.app_down_Bps == 9 * 20 * 56
    assert b.relay_out_Bps == pytest.approx(100_800)
    batched = link_budget(10, 20.0, "mcs3", tick_hz=40.0)
    assert batched.mean_down_batch == pytest.approx(180 / 40)
    assert batched.airtime_fraction < 0.5 * b.airtime_fraction
    sta = link_budget(10, 20.0, "mcs3", tick_hz=40.0, relay_on_ap=False)
    assert sta.airtime_fraction == pytest.approx(2 * batched.airtime_fraction, rel=0.05)
    small = link_budget(1, 20.0)
    assert small.downlink_frames_per_node == 0 and small.airtime_fraction < 0.05
    rates = [link_budget(n, 20.0).airtime_fraction for n in range(2, 11)]
    assert rates == sorted(rates)
    assert link_budget(10, 20.0, encoding="json").relay_out_Bps > 3 * b.relay_out_Bps
    assert "airtime" in format_table(budget_table())
    with pytest.raises(ValueError):
        link_budget(0)
