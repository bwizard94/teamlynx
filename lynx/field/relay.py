"""Field relay: the Phase 1 relay plus what a squad deployment needs.

:class:`FieldRelay` subclasses :class:`lynx.net.server.RelayServer` and keeps its semantics
(``docs/protocol.md``) unchanged on the wire. It adds:

* **Tick-batched downlink** (``tick_s``, default 25 ms). Telemetry for each client is still
  latest-value-wins, but it is flushed on a fixed tick, and every batch is written with Linux
  ``TCP_CORK`` so it leaves as one TCP segment / one 802.11 frame. With 10 nodes at 20 Hz this
  turns ~180 small downlink frames per headset per second into <= 40, which is what keeps the
  channel usable at low MCS (see :mod:`lynx.field.bandwidth`). Pings, cancels and leaves are not
  delayed: an event wakes the sender immediately and carries any pending telemetry with it.
* **DSCP EF** on every client socket (802.11 AC_VI), see :mod:`lynx.field.sockopts`.
* **UDP beacon** (:class:`lynx.field.discovery.RelayBeacon`) and optional mDNS registration, so
  headsets find the relay with no configuration and know primary from secondary.
* **Session log** (:class:`lynx.field.session.SessionRecorder`) of every message the relay
  accepted and fanned out (telemetry decimated), for after-action review.
* **Bridge** (``upstream`` URL, secondary role): the secondary relay keeps one WebSocket to the
  primary and exchanges traffic both ways, so headsets split across the two relays during a
  partial outage still see one squad picture. Loop-free by construction: the primary never echoes
  a frame back to its sender (the bridge session), and the secondary forwards upstream only
  frames that came from its own local clients. Nodes learned from upstream are owned by a pseudo
  session, so local clients cannot spoof them; a node id that is live locally wins over an
  upstream copy.

Standard library + ``websockets`` only, so it runs on an OpenWrt router with ``python3-light``.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import random
import time
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Sequence, Set, Tuple

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from lynx.net.schema import (
    SERVER_NODE_ID,
    AnyMessage,
    LeaveReason,
    Message,
    NodeLeave,
    Ping,
    PingCancel,
    SchemaError,
    Telemetry,
    decode,
    encode,
)
from lynx.net.server import RelayConfig, RelayServer, _Session

from .discovery import BEACON_PORT, MdnsAdvertiser, RelayBeacon
from .session import SessionRecorder
from .sockopts import DSCP_EF, cork, set_dscp

log = logging.getLogger("lynx.field.relay")

BRIDGE_SID = -1


@dataclass
class FieldRelayConfig:
    tick_s: float = 0.025
    cork: bool = True
    dscp: Optional[int] = DSCP_EF
    role: str = "primary"
    priority: int = 0
    squad: str = "LYNX"
    relay_id: str = ""
    beacon: bool = True
    beacon_bind: Tuple[str, int] = ("0.0.0.0", BEACON_PORT)
    beacon_targets: Optional[Sequence[Tuple[str, int]]] = None
    beacon_interval_s: float = 1.0
    mdns: bool = False
    upstream: Optional[str] = None
    log_path: Optional[str] = None
    log_telemetry_hz: float = 5.0
    log_compress: bool = False


@dataclass
class FieldStats:
    batches_out: int = 0
    batched_frames: int = 0
    bridge_in: int = 0
    bridge_out: int = 0
    bridge_connects: int = 0
    bridge_conflicts: int = 0


class FieldRelay(RelayServer):
    def __init__(self, config: Optional[RelayConfig] = None, field_config: Optional[FieldRelayConfig] = None) -> None:
        super().__init__(config)
        self.field = field_config or FieldRelayConfig()
        self.fstats = FieldStats()
        self.beacon: Optional[RelayBeacon] = None
        self.mdns: Optional[MdnsAdvertiser] = None
        self.recorder: Optional[SessionRecorder] = None
        self.bridge: Optional[RelayBridge] = None
        self._due: Set[int] = set()
        self._ticker: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        f = self.field
        if f.log_path:
            self.recorder = SessionRecorder(f.log_path, "relay", telemetry_hz=f.log_telemetry_hz,
                                            compress=f.log_compress,
                                            meta={"role": f.role, "squad": f.squad, "upstream": f.upstream})
            log.info("session log %s", self.recorder.path)
        await super().start()
        if f.tick_s > 0:
            self._ticker = asyncio.create_task(self._tick_loop(), name="lynx-relay-tick")
        if f.beacon:
            self.beacon = RelayBeacon(self.port, f.role, f.priority, f.squad, f.relay_id, bind=f.beacon_bind,
                                      targets=f.beacon_targets, interval_s=f.beacon_interval_s,
                                      clients_fn=lambda: self.client_count)
            try:
                await self.beacon.start()
            except OSError as exc:
                log.warning("beacon disabled: %s", exc)
                self.beacon = None
        if f.mdns:
            self.mdns = MdnsAdvertiser(self.port, f.role, f.priority, f.squad, f.relay_id)
            if not self.mdns.start():
                self.mdns = None
        if f.upstream:
            self.bridge = RelayBridge(self, f.upstream)
            self.bridge.start()

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
            self._ticker = None
        if self.beacon is not None:
            await self.beacon.stop()
            self.beacon = None
        if self.mdns is not None:
            self.mdns.stop()
            self.mdns = None
        await super().stop()
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None

    # -- downlink batching -----------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        if self.field.dscp is not None:
            set_dscp(ws, self.field.dscp)
        await super()._handle(ws)

    async def _tick_loop(self) -> None:
        period = self.field.tick_s
        nxt = time.monotonic()
        while True:
            nxt += period
            delay = nxt - time.monotonic()
            if delay < -period:  # fell behind (CPU starved); do not burst-catch-up
                nxt = time.monotonic()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))
            for sid, sess in list(self._sessions.items()):
                if sess.pending_telemetry and not sess.closed:
                    self._due.add(sid)
                    sess.wake.set()

    def _enqueue(self, sess: _Session, msg: Message) -> None:
        if self.field.tick_s > 0 and isinstance(msg, Telemetry) and not sess.closed:
            sess.pending_telemetry[msg.node_id] = msg
            return
        super()._enqueue(sess, msg)

    async def _sender(self, sess: _Session) -> None:
        if self.field.tick_s <= 0:
            await super()._sender(sess)
            return
        while not sess.closed:
            await sess.wake.wait()
            sess.wake.clear()
            batch = list(sess.events)
            sess.events.clear()
            if batch or sess.sid in self._due:
                self._due.discard(sess.sid)
                batch.extend(sess.pending_telemetry.values())
                sess.pending_telemetry.clear()
            if not batch:
                continue
            corked = self.field.cork and len(batch) > 1 and cork(sess.ws, True)
            try:
                for msg in batch:
                    await sess.ws.send(encode(msg, sess.encoding))
                    self.stats.frames_out += 1
            finally:
                if corked:
                    cork(sess.ws, False)
            self.fstats.batches_out += 1
            self.fstats.batched_frames += len(batch)

    # -- logging + upstream forwarding -----------------------------------------------------------

    def _broadcast(self, msg: Message, exclude: Optional[int] = None) -> None:
        if self.recorder is not None:
            self.recorder.record_message(msg)
        if (self.bridge is not None and exclude is not None and exclude != BRIDGE_SID
                and msg.node_id != SERVER_NODE_ID):
            self.bridge.forward(msg)
        super()._broadcast(msg, exclude)

    def _drop_node(self, node: int, reason: LeaveReason) -> None:
        owner = self._owner.get(node)
        if self.bridge is not None and owner is not None and owner != BRIDGE_SID and reason == LeaveReason.DISCONNECT:
            self.bridge.forward(NodeLeave(node_id=node, node=node, reason=LeaveReason.DISCONNECT))
        super()._drop_node(node, reason)

    def _claim(self, sess: _Session, node: int) -> Optional[bool]:
        if self._owner.get(node) == BRIDGE_SID:
            # A headset that failed over to this relay takes its node back from the bridge copy.
            del self._owner[node]
        return super()._claim(sess, node)

    # -- upstream injection ----------------------------------------------------------------------

    def inject_upstream(self, msg: AnyMessage) -> None:
        """Apply a message received from the upstream (primary) relay and fan it out locally."""
        now = time.monotonic()
        self.fstats.bridge_in += 1
        if msg.node_id == SERVER_NODE_ID:
            if isinstance(msg, NodeLeave):
                owner = self._owner.get(msg.node)
                if owner is not None and owner != BRIDGE_SID:
                    return  # live on this relay; the primary just has not heard from it
                self._owner.pop(msg.node, None)
                if self.state.remove_node(msg.node):
                    self._broadcast(msg, exclude=BRIDGE_SID)
            elif isinstance(msg, PingCancel):
                if self.state.apply(msg, now):
                    self._broadcast(msg, exclude=BRIDGE_SID)
            elif isinstance(msg, Ping):
                self.state.apply(msg, now)
                self._broadcast(msg, exclude=BRIDGE_SID)
            return
        owner = self._owner.get(msg.node_id)
        if owner is not None and owner != BRIDGE_SID:
            self.fstats.bridge_conflicts += 1
            return
        claimed = owner is None
        if claimed:
            self._owner[msg.node_id] = BRIDGE_SID
        if isinstance(msg, Telemetry):
            if self.state.apply(msg, now, force=claimed):
                self._broadcast(msg, exclude=BRIDGE_SID)
        elif isinstance(msg, (Ping, PingCancel)):
            if self.state.apply(msg, now):
                self._broadcast(msg, exclude=BRIDGE_SID)
        elif isinstance(msg, NodeLeave):
            self._owner.pop(msg.node_id, None)
            if self.state.remove_node(msg.node):
                self._broadcast(dataclasses.replace(msg, node_id=SERVER_NODE_ID), exclude=BRIDGE_SID)

    def local_nodes(self) -> Dict[int, int]:
        return {n: sid for n, sid in self._owner.items() if sid != BRIDGE_SID}

    def upstream_nodes(self) -> Set[int]:
        return {n for n, sid in self._owner.items() if sid == BRIDGE_SID}

    def bridge_lost(self) -> None:
        """Upstream link gone: release ownership so the sweeper ages its nodes out normally."""
        for n in self.upstream_nodes():
            self._owner.pop(n, None)


class RelayBridge:
    """Secondary -> primary WebSocket bridge with its own reconnect loop."""

    def __init__(self, relay: FieldRelay, url: str, backoff_initial_s: float = 0.25, backoff_max_s: float = 4.0,
                 keepalive_s: float = 1.0) -> None:
        self.relay = relay
        self.url = url
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.keepalive_s = keepalive_s
        self.connected = asyncio.Event()
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._pending: Dict[int, Telemetry] = {}
        self._events: Deque[Message] = collections.deque(maxlen=1024)
        self._wake = asyncio.Event()
        self._rng = random.Random()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="lynx-relay-bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, ConnectionClosed):
                pass
            self._task = None

    def forward(self, msg: Message) -> None:
        if not self.connected.is_set():
            return
        if isinstance(msg, Telemetry):
            self._pending[msg.node_id] = msg
        else:
            self._events.append(msg)
        self._wake.set()

    def _sync_up(self) -> None:
        """On (re)connect, push this relay's local nodes and their active pings to the primary."""
        now = time.monotonic()
        local = self.relay.local_nodes()
        nodes, pings = self.relay.state.snapshot()
        for nid in local:
            if nid in nodes:
                self._pending[nid] = nodes[nid].telemetry
        for ps in sorted(pings.values(), key=lambda p: p.received):
            if ps.ping.owner in local:
                self._events.append(dataclasses.replace(ps.ping, ttl_ms=max(1, ps.remaining_ms(now))))
        self._wake.set()

    async def _tx(self, ws) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            batch = list(self._events)
            self._events.clear()
            batch.extend(self._pending.values())
            self._pending.clear()
            corked = len(batch) > 1 and cork(ws, True)
            try:
                for msg in batch:
                    await ws.send(encode(msg))
                    self.relay.fstats.bridge_out += 1
            finally:
                if corked:
                    cork(ws, False)

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                async with connect(self.url, compression=None, open_timeout=2.0, ping_interval=self.keepalive_s,
                                   ping_timeout=2 * self.keepalive_s, close_timeout=1.0, max_size=4096,
                                   proxy=None) as ws:
                    set_dscp(ws, DSCP_EF)
                    failures = 0
                    self._ws = ws
                    self.connected.set()
                    self.relay.fstats.bridge_connects += 1
                    log.info("bridge up to %s", self.url)
                    self._sync_up()
                    tx = asyncio.create_task(self._tx(ws))
                    try:
                        async for frame in ws:
                            try:
                                msg = decode(frame)
                            except SchemaError:
                                continue
                            self.relay.inject_upstream(msg)
                    finally:
                        tx.cancel()
                        try:
                            await tx
                        except (asyncio.CancelledError, ConnectionClosed):
                            pass
            except (OSError, InvalidHandshake, asyncio.TimeoutError, ConnectionClosed) as exc:
                log.debug("bridge to %s: %s", self.url, exc)
            if self.connected.is_set():
                log.warning("bridge to %s lost", self.url)
            self.connected.clear()
            self._ws = None
            self._pending.clear()
            self._events.clear()
            self.relay.bridge_lost()
            failures += 1
            cap = min(self.backoff_max_s, self.backoff_initial_s * 2 ** (failures - 1))
            await asyncio.sleep(self._rng.uniform(0.1 * cap, cap))


async def run_field_relay(config: RelayConfig, field_config: FieldRelayConfig, stats_interval_s: float = 10.0,
                          stop: Optional[asyncio.Event] = None) -> None:
    import signal

    relay = FieldRelay(config, field_config)
    await relay.start()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=stats_interval_s or None)
            except asyncio.TimeoutError:
                s, f = relay.stats, relay.fstats
                avg = f.batched_frames / f.batches_out if f.batches_out else 0.0
                log.info("clients=%d nodes=%d pings=%d in=%d out=%d batches=%d (%.1f/batch) dropped(rate=%d seq=%d "
                         "bad=%d) bridge(in=%d out=%d up=%s)", relay.client_count, len(relay.state),
                         len(relay.state.ping_keys), s.frames_in, s.frames_out, f.batches_out, avg, s.rate_limited,
                         s.stale_seq, s.decode_errors, f.bridge_in, f.bridge_out,
                         relay.bridge.connected.is_set() if relay.bridge else "-")
    finally:
        await relay.stop()
