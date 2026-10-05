"""Load test: N simulated headsets against a relay; latency percentiles, drops and bandwidth.

Each simulated headset is a raw WebSocket client (no ``LynxClient`` state copies, so the load
generator stays cheap and its own scheduling adds little latency). Per headset:

* telemetry at ``rate_hz`` with a random phase, walking a 20 m circle;
* a ping every ``ping_interval_s`` (random phase), cancelled again after 2 s;
* a receive loop that decodes every frame.

Measurement window: frames *sent* during ``[warmup, warmup + duration]`` after all headsets are
connected; frames still in flight are collected for ``drain_s`` afterwards.

* **latency** = receive time - sender ``ts_us``. All headsets share the generator host's clock,
  so this is true one-way relay latency (encode -> relay -> decode), including the relay's tick.
* **telemetry delivery** = received / ((N - 1) x sent) per sender. Shortfall is either relay
  coalescing (latest-value-wins under backlog: the receiver got a *newer* pose instead) or loss.
* **ping delivery** must be 100 %: pings are a reliable FIFO.
* **bytes**: application payload bytes in/out of the relay, measured at the headsets.
* **generator lag**: p99 oversleep of a 10 ms timer in the generator's loop; if it is large the
  generator, not the relay, is the bottleneck.
* With ``spawn`` the relay runs in a child process, and its CPU use (``/proc``) and the host's
  TCP segment rate (``/proc/net/snmp``; on loopback both directions count) are reported too.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from websockets.asyncio.client import connect

from lynx.net.schema import Ping, PingCancel, PingType, SequenceCounter, Team, Telemetry, decode, encode, now_us

from .bandwidth import link_budget


@dataclass
class LoadTestConfig:
    url: str = "ws://127.0.0.1:8765"
    nodes: int = 10
    rate_hz: float = 20.0
    duration_s: float = 10.0
    warmup_s: float = 1.0
    drain_s: float = 0.5
    ping_interval_s: float = 3.0
    encoding: str = "binary"
    seed: int = 1
    first_node: int = 101


def percentile(values: List[float], q: float) -> float:
    if not values:
        return math.nan
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class _Sim:
    node: int
    sent_tel: int = 0
    sent_pings: Dict[int, int] = field(default_factory=dict)  # ping_id -> ts_us
    bytes_tx: int = 0
    bytes_rx: int = 0
    frames_rx: int = 0
    tel_rx: Dict[int, int] = field(default_factory=dict)  # sender -> frames received (sent in window)
    tel_lat_ms: List[float] = field(default_factory=list)
    ping_lat_ms: List[float] = field(default_factory=list)
    pings_rx: set = field(default_factory=set)
    errors: List[str] = field(default_factory=list)


@dataclass
class LoadReport:
    nodes: int
    rate_hz: float
    duration_s: float
    encoding: str
    url: str
    telemetry_sent: int
    telemetry_expected: int
    telemetry_received: int
    telemetry_delivery: float
    telemetry_dropped: int
    latency_ms: Dict[str, float]
    pings_sent: int
    ping_deliveries_expected: int
    ping_deliveries: int
    ping_latency_ms: Dict[str, float]
    relay_in_Bps: float
    relay_out_Bps: float
    frames_out_per_s: float
    generator_lag_p99_ms: float
    relay_cpu_pct: Optional[float] = None
    host_tcp_segments_per_s: Optional[float] = None
    model_airtime_mcs3_pct: float = 0.0
    errors: List[str] = field(default_factory=list)
    label: str = ""

    @property
    def ping_loss(self) -> int:
        return self.ping_deliveries_expected - self.ping_deliveries

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ping_loss"] = self.ping_loss
        return d

    def format(self) -> str:
        L = self.latency_ms
        P = self.ping_latency_ms
        lines = [
            f"load test {self.label or self.url}: {self.nodes} headsets x {self.rate_hz:g} Hz {self.encoding}, "
            f"{self.duration_s:g} s window",
            f"  telemetry  sent {self.telemetry_sent}  delivered {self.telemetry_received}/{self.telemetry_expected} "
            f"({100 * self.telemetry_delivery:.2f} %)  dropped/coalesced {self.telemetry_dropped}",
            f"  latency ms p50 {L['p50']:.1f}  p90 {L['p90']:.1f}  p99 {L['p99']:.1f}  max {L['max']:.1f}",
            f"  pings      sent {self.pings_sent}  delivered {self.ping_deliveries}/{self.ping_deliveries_expected} "
            f"(loss {self.ping_loss})  p50 {P['p50']:.1f} ms  p99 {P['p99']:.1f} ms",
            f"  relay app bytes in {self.relay_in_Bps / 1000:.1f} kB/s  out {self.relay_out_Bps / 1000:.1f} kB/s  "
            f"frames out {self.frames_out_per_s:.0f}/s",
            f"  generator lag p99 {self.generator_lag_p99_ms:.1f} ms"
            + (f"  relay CPU {self.relay_cpu_pct:.1f} %" if self.relay_cpu_pct is not None else "")
            + (f"  host TCP segs {self.host_tcp_segments_per_s:.0f}/s"
               if self.host_tcp_segments_per_s is not None else ""),
        ]
        if self.errors:
            lines.append(f"  errors: {len(self.errors)} (first: {self.errors[0]})")
        return "\n".join(lines)


def _summary(values: List[float]) -> Dict[str, float]:
    return {"p50": percentile(values, 50), "p90": percentile(values, 90), "p99": percentile(values, 99),
            "max": max(values) if values else math.nan, "mean": sum(values) / len(values) if values else math.nan,
            "n": float(len(values))}


def read_tcp_out_segs() -> Optional[int]:
    try:
        with open("/proc/net/snmp") as fh:
            rows = [line.split() for line in fh if line.startswith("Tcp:")]
        return int(rows[1][rows[0].index("OutSegs")])
    except (OSError, ValueError, IndexError):
        return None


def read_proc_cpu_s(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


async def run_loadtest(cfg: LoadTestConfig, relay_pid: Optional[int] = None) -> LoadReport:
    rng = random.Random(cfg.seed)
    sims = [_Sim(cfg.first_node + i) for i in range(cfg.nodes)]
    url = cfg.url + ("?encoding=json" if cfg.encoding == "json" else "")
    conns = []
    for s in sims:
        conns.append(await connect(url, compression=None, max_size=4096, proxy=None, open_timeout=5.0,
                                   ping_interval=None))
    loop = asyncio.get_running_loop()
    t_conn = loop.time()
    t0 = t_conn + cfg.warmup_s
    t1 = t0 + cfg.duration_s
    win0_us = now_us() + int(cfg.warmup_s * 1e6)
    win1_us = win0_us + int(cfg.duration_s * 1e6)
    lag: List[float] = []
    stop = asyncio.Event()
    ping_owner_ts: Dict[Tuple[int, int], int] = {}

    async def lag_monitor() -> None:
        while not stop.is_set():
            a = loop.time()
            await asyncio.sleep(0.01)
            lag.append(max(0.0, (loop.time() - a - 0.01) * 1000.0))

    async def sender(s: _Sim, ws) -> None:
        seq = SequenceCounter(rng.randrange(1 << 20))
        period = 1.0 / cfg.rate_hz
        nxt = t_conn + rng.uniform(0, period)
        next_ping = t0 + rng.uniform(0, cfg.ping_interval_s) if cfg.ping_interval_s > 0 else math.inf
        ping_ids = SequenceCounter(1)
        cancels: List[Tuple[float, int]] = []
        phase = rng.uniform(0, 2 * math.pi)
        cs = f"N{s.node}"[:8]
        while True:
            now = loop.time()
            if now >= t1:
                return
            await asyncio.sleep(max(0.0, nxt - now))
            now = loop.time()
            a = phase + 0.1 * now
            msgs = [Telemetry(team=Team.BLUE, callsign=cs, x=20 * math.cos(a), y=20 * math.sin(a), z=1.7,
                              heading=math.degrees(a) % 360.0, pitch=-5.0, roll=0.0)]
            if now >= next_ping and now < t1:
                pid = ping_ids.next()
                msgs.append(Ping(ping_id=pid, owner=s.node, ping_type=PingType.MARK, x=10.0, y=5.0, z=0.0,
                                 ttl_ms=10_000))
                cancels.append((now + 2.0, pid))
                next_ping += cfg.ping_interval_s
            while cancels and cancels[0][0] <= now:
                _, pid = cancels.pop(0)
                msgs.append(PingCancel(ping_id=pid, owner=s.node))
            for m in msgs:
                m.node_id = s.node
                m.seq = seq.next()
                m.ts_us = now_us()
                data = encode(m, cfg.encoding)
                try:
                    await ws.send(data)
                except Exception as exc:  # connection dropped by the relay
                    s.errors.append(f"send: {exc!r}")
                    return
                s.bytes_tx += len(data)
                if isinstance(m, Telemetry) and win0_us <= m.ts_us <= win1_us:
                    s.sent_tel += 1
                elif isinstance(m, Ping):
                    s.sent_pings[m.ping_id] = m.ts_us
                    ping_owner_ts[(s.node, m.ping_id)] = m.ts_us
            nxt += period
            if nxt < loop.time() - period:
                nxt = loop.time()

    async def receiver(s: _Sim, ws) -> None:
        try:
            async for frame in ws:
                t_rx = now_us()
                s.frames_rx += 1
                s.bytes_rx += len(frame)
                try:
                    msg = decode(frame)
                except Exception as exc:
                    s.errors.append(f"decode: {exc!r}")
                    continue
                if isinstance(msg, Telemetry):
                    if win0_us <= msg.ts_us <= win1_us and msg.node_id != s.node:
                        s.tel_rx[msg.node_id] = s.tel_rx.get(msg.node_id, 0) + 1
                        s.tel_lat_ms.append((t_rx - msg.ts_us) / 1000.0)
                elif isinstance(msg, Ping) and msg.owner != s.node:
                    key = (msg.owner, msg.ping_id)
                    if key not in s.pings_rx:
                        s.pings_rx.add(key)
                        s.ping_lat_ms.append((t_rx - msg.ts_us) / 1000.0)
        except Exception as exc:
            if not stop.is_set():
                s.errors.append(f"recv: {exc!r}")

    cpu0 = cpu1 = segs0 = segs1 = None
    mon = asyncio.create_task(lag_monitor())
    rx = [asyncio.create_task(receiver(s, ws)) for s, ws in zip(sims, conns)]
    tx = [asyncio.create_task(sender(s, ws)) for s, ws in zip(sims, conns)]
    await asyncio.sleep(max(0.0, t0 - loop.time()))
    if relay_pid:
        cpu0 = read_proc_cpu_s(relay_pid)
    segs0 = read_tcp_out_segs()
    await asyncio.gather(*tx)
    if relay_pid:
        cpu1 = read_proc_cpu_s(relay_pid)
    segs1 = read_tcp_out_segs()
    await asyncio.sleep(cfg.drain_s)
    stop.set()
    for ws in conns:
        await ws.close()
    await asyncio.gather(*rx, return_exceptions=True)
    mon.cancel()
    try:
        await mon
    except asyncio.CancelledError:
        pass

    n = cfg.nodes
    sent = sum(s.sent_tel for s in sims)
    expected = sent * (n - 1)
    received = sum(sum(s.tel_rx.values()) for s in sims)
    lat = [v for s in sims for v in s.tel_lat_ms]
    plat = [v for s in sims for v in s.ping_lat_ms]
    pings_sent = len(ping_owner_ts)
    ping_rx = sum(len(s.pings_rx) for s in sims)
    window = cfg.duration_s
    up = sum(s.bytes_tx for s in sims) / (window + cfg.warmup_s)
    down = sum(s.bytes_rx for s in sims) / (window + cfg.warmup_s + cfg.drain_s)
    frames_out = sum(s.frames_rx for s in sims) / (window + cfg.warmup_s + cfg.drain_s)
    model = link_budget(n, cfg.rate_hz, "mcs3", "vi", 40.0, True, cfg.encoding)
    return LoadReport(
        nodes=n, rate_hz=cfg.rate_hz, duration_s=window, encoding=cfg.encoding, url=cfg.url,
        telemetry_sent=sent, telemetry_expected=expected, telemetry_received=received,
        telemetry_delivery=received / expected if expected else 1.0, telemetry_dropped=expected - received,
        latency_ms=_summary(lat), pings_sent=pings_sent, ping_deliveries_expected=pings_sent * (n - 1),
        ping_deliveries=ping_rx, ping_latency_ms=_summary(plat), relay_in_Bps=up, relay_out_Bps=down,
        frames_out_per_s=frames_out, generator_lag_p99_ms=percentile(lag, 99),
        relay_cpu_pct=100.0 * (cpu1 - cpu0) / window if cpu0 is not None and cpu1 is not None else None,
        host_tcp_segments_per_s=(segs1 - segs0) / window if segs0 is not None and segs1 is not None else None,
        model_airtime_mcs3_pct=100.0 * model.airtime_fraction,
        errors=[e for s in sims for e in s.errors])


# ------------------------------------------------------------------------------- spawned relay


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SpawnedRelay:
    """``lynx-field relay`` in a child process on 127.0.0.1 (no beacon)."""

    def __init__(self, tick_ms: float = 25.0, port: Optional[int] = None, extra: Optional[List[str]] = None) -> None:
        self.port = port or free_port()
        self.tick_ms = tick_ms
        self.extra = extra or []
        self.proc: Optional[subprocess.Popen] = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self, timeout_s: float = 10.0) -> "SpawnedRelay":
        cmd = [sys.executable, "-m", "lynx.field", "relay", "--host", "127.0.0.1", "--port", str(self.port),
               "--no-beacon", "--tick-ms", str(self.tick_ms), "--stats-interval", "0", *self.extra]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
                raise RuntimeError(f"relay exited early: {err[-500:]}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("relay did not start listening")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> "SpawnedRelay":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def run_spawned(cfg: LoadTestConfig, tick_ms: float) -> LoadReport:
    with SpawnedRelay(tick_ms) as relay:
        cfg = LoadTestConfig(**{**asdict(cfg), "url": relay.url})
        report = asyncio.run(run_loadtest(cfg, relay.proc.pid if relay.proc else None))
    report.label = f"spawned relay, tick {tick_ms:g} ms" if tick_ms > 0 else "spawned relay, per-frame downlink"
    return report


def reports_json(reports: List[LoadReport]) -> str:
    return json.dumps([r.to_dict() for r in reports], indent=2, allow_nan=True)
