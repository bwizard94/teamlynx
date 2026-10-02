"""Squad link budget: application bytes, wire bytes and 802.11 airtime for N nodes.

Bytes alone are misleading on Wi-Fi: 10 nodes x 20 Hz of 56-byte telemetry is ~11 KB/s up and
~100 KB/s down, yet every small frame costs a full channel access (contention, preamble, SIFS,
ACK). Airtime per frame transmission (802.11n 2.4 GHz, 20 MHz, long GI, no aggregation):

    T = AIFS + E[backoff] + T_preamble + ceil((16 + 8 L + 6) / N_dbps) * 4 us + SIFS + T_ack

* ``AIFS = SIFS + AIFSN * slot`` (SIFS 16 us, slot 9 us); ``E[backoff] = CWmin / 2 * slot``.
  AC_BE: AIFSN 3, CWmin 15; AC_VI: AIFSN 2, CWmin 7; AC_VO: AIFSN 2, CWmin 3.
* HT-mixed preamble 36 us; ``N_dbps`` = data bits per 4 us symbol (26 for MCS0 ... 260 for MCS7).
* ACK: 14 bytes at the highest basic rate <= the data rate (6 or 24 Mbps; 802.11b rates disabled),
  legacy preamble 20 us.
* ``L`` = MPDU length: payload + WebSocket header (6 B client->relay with mask, 2 B relay->client)
  + TCP/IPv4 with timestamps (52 B) + 802.11 QoS header, LLC/SNAP, CCMP, FCS (46 B).

Per node and second, with telemetry rate ``R`` and ``N`` nodes:

* uplink: ``R`` telemetry segments, plus ``k`` keepalive pings (and the pong back);
* downlink: ``(N - 1) R`` telemetry frames, as that many segments without batching, or
  ``min(tick_hz, (N - 1) R)`` larger segments with the field relay's tick batching;
* TCP pure ACKs for ``ack_ratio`` of the data segments in each direction (delayed ACK ~0.5).

If the relay runs on a Wi-Fi *client* (squad-leader Jetson) instead of the AP, every frame between
two stations crosses the air twice (STA -> AP -> relay STA), doubling airtime. Beacons (10/s,
~250 B at 6 Mbps) are added once.

The model ignores retries, so treat its airtime as a floor: keep the predicted total under ~50 %
to leave room for retransmissions at range and for other traffic. Standard library only.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

SIFS_US = 16.0
SLOT_US = 9.0
HT_PREAMBLE_US = 36.0
LEGACY_PREAMBLE_US = 20.0
SYMBOL_US = 4.0
MAC_OVERHEAD_B = 26 + 8 + 8 + 4
TCPIP_OVERHEAD_B = 20 + 20 + 12
WS_UP_HEADER_B = 6
WS_DOWN_HEADER_B = 2
ACK_FRAME_B = 14
BEACON_B = 250
BEACONS_PER_S = 10.0

TELEMETRY_BINARY_B = 56
TELEMETRY_JSON_B = 187  # typical JSON debug encoding (lynx.net.schema), callsign 5 chars

ACCESS_CATEGORIES = {"be": (3, 15), "vi": (2, 7), "vo": (2, 3)}

# name -> (data rate Mbps, data bits per symbol, ACK rate Mbps)
PHY_RATES = {
    "mcs0": (6.5, 26, 6.0),
    "mcs1": (13.0, 52, 12.0),
    "mcs2": (19.5, 78, 12.0),
    "mcs3": (26.0, 104, 24.0),
    "mcs4": (39.0, 156, 24.0),
    "mcs5": (52.0, 208, 24.0),
    "mcs7": (65.0, 260, 24.0),
}


def frame_airtime_us(mpdu_bytes: float, phy: str = "mcs3", ac: str = "vi") -> float:
    _, ndbps, ack_mbps = PHY_RATES[phy]
    aifsn, cwmin = ACCESS_CATEGORIES[ac]
    data = HT_PREAMBLE_US + math.ceil((16 + 8 * mpdu_bytes + 6) / ndbps) * SYMBOL_US
    ack = LEGACY_PREAMBLE_US + math.ceil((16 + 8 * ACK_FRAME_B + 6) / (ack_mbps * SYMBOL_US)) * SYMBOL_US
    return SIFS_US + aifsn * SLOT_US + cwmin / 2.0 * SLOT_US + data + SIFS_US + ack


def beacon_airtime_us() -> float:
    return LEGACY_PREAMBLE_US + math.ceil((16 + 8 * BEACON_B + 6) / 24.0) * SYMBOL_US + SIFS_US + 2 * SLOT_US


@dataclass(frozen=True)
class LinkBudget:
    nodes: int
    rate_hz: float
    phy: str
    ac: str
    tick_hz: Optional[float]
    relay_on_ap: bool
    encoding: str
    app_up_Bps: float  # per node
    app_down_Bps: float  # per node
    relay_in_Bps: float  # total application bytes into the relay
    relay_out_Bps: float
    wire_Bps: float  # all MPDU bytes on the air
    frames_per_s: float  # transmissions on the air (data + pure ACK + keepalive)
    airtime_fraction: float
    downlink_frames_per_node: float
    mean_down_batch: float

    def to_dict(self) -> dict:
        return asdict(self)


def link_budget(nodes: int = 10, rate_hz: float = 20.0, phy: str = "mcs3", ac: str = "vi",
                tick_hz: Optional[float] = 40.0, relay_on_ap: bool = True, encoding: str = "binary",
                ack_ratio: float = 0.5, keepalive_hz: float = 1.0, frame_bytes: Optional[int] = None) -> LinkBudget:
    if nodes < 1:
        raise ValueError("nodes must be >= 1")
    payload = frame_bytes or (TELEMETRY_BINARY_B if encoding == "binary" else TELEMETRY_JSON_B)
    others = nodes - 1
    down_msgs = others * rate_hz
    if tick_hz and down_msgs > 0:
        down_segs = min(tick_hz, down_msgs)
    else:
        down_segs = down_msgs
    batch = down_msgs / down_segs if down_segs else 0.0
    up_mpdu = payload + WS_UP_HEADER_B + TCPIP_OVERHEAD_B + MAC_OVERHEAD_B
    down_mpdu = batch * (payload + WS_DOWN_HEADER_B) + TCPIP_OVERHEAD_B + MAC_OVERHEAD_B
    ack_mpdu = TCPIP_OVERHEAD_B + MAC_OVERHEAD_B
    ka_mpdu = 6 + 4 + TCPIP_OVERHEAD_B + MAC_OVERHEAD_B  # 4-byte ping payload, masked
    hops = 1 if relay_on_ap else 2

    t_up = frame_airtime_us(up_mpdu, phy, ac)
    t_down = frame_airtime_us(down_mpdu, phy, ac) if down_segs else 0.0
    t_ack = frame_airtime_us(ack_mpdu, phy, ac)
    t_ka = frame_airtime_us(ka_mpdu, phy, ac)
    per_node_frames = {
        "up": rate_hz, "down": down_segs, "ack": ack_ratio * (rate_hz + down_segs), "ka": 2.0 * keepalive_hz,
    }
    per_node_us = (rate_hz * t_up + down_segs * t_down + per_node_frames["ack"] * t_ack
                   + per_node_frames["ka"] * t_ka)
    airtime = (nodes * per_node_us * hops + BEACONS_PER_S * beacon_airtime_us()) / 1e6
    frames = nodes * sum(per_node_frames.values()) * hops
    wire = nodes * hops * (rate_hz * up_mpdu + down_segs * down_mpdu + per_node_frames["ack"] * ack_mpdu
                           + per_node_frames["ka"] * ka_mpdu)
    return LinkBudget(nodes, rate_hz, phy, ac, tick_hz, relay_on_ap, encoding,
                      app_up_Bps=rate_hz * payload, app_down_Bps=down_msgs * payload,
                      relay_in_Bps=nodes * rate_hz * payload, relay_out_Bps=nodes * down_msgs * payload,
                      wire_Bps=wire, frames_per_s=frames, airtime_fraction=airtime,
                      downlink_frames_per_node=down_segs, mean_down_batch=batch)


def budget_table(nodes: int = 10, rate_hz: float = 20.0, phys: Sequence[str] = ("mcs0", "mcs3", "mcs7"),
                 ticks: Sequence[Optional[float]] = (None, 40.0, 20.0), relay_on_ap: Sequence[bool] = (True, False),
                 ac: str = "vi") -> List[LinkBudget]:
    return [link_budget(nodes, rate_hz, phy, ac, tick, on_ap)
            for on_ap in relay_on_ap for tick in ticks for phy in phys]


def format_table(rows: Sequence[LinkBudget]) -> str:
    head = f"{'relay':6} {'downlink':12} {'PHY':5} {'relay in':>9} {'relay out':>10} {'frames/s':>9} {'airtime':>8}"
    lines = [head, "-" * len(head)]
    for b in rows:
        mode = "per-frame" if not b.tick_hz else f"tick {1000 / b.tick_hz:.0f} ms"
        lines.append(f"{'AP' if b.relay_on_ap else 'STA':6} {mode:12} {b.phy:5} "
                     f"{b.relay_in_Bps / 1000:7.1f}kB {b.relay_out_Bps / 1000:8.1f}kB "
                     f"{b.frames_per_s:9.0f} {100 * b.airtime_fraction:7.1f}%")
    return "\n".join(lines)


def summary(nodes: int = 10, rate_hz: float = 20.0) -> Dict[str, float]:
    b = link_budget(nodes, rate_hz)
    return {"app_up_kBps_per_node": b.app_up_Bps / 1000, "app_down_kBps_per_node": b.app_down_Bps / 1000,
            "relay_out_kBps": b.relay_out_Bps / 1000, "airtime_pct": 100 * b.airtime_fraction}
