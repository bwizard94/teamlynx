"""Async WebSocket relay for the squad network.

Designed to run on a battery-powered offline travel router (OpenWrt + Python) or any laptop on
the squad LAN. One process, no internet, no external broker.

Responsibilities
----------------
* Rebroadcast telemetry, pings and cancels to every other connected client.
* Keep authoritative state (latest telemetry per node, active pings) and send a snapshot to
  late joiners, with ping TTLs rewritten to the *remaining* time.
* Evict stale nodes (no telemetry for ``stale_timeout_s``) and expire pings whose TTL elapsed,
  broadcasting NODE_LEAVE / PING_CANCEL so every HUD converges.
* Rate handling:
    - inbound: per-node token buckets for telemetry and pings (excess silently dropped);
    - outbound: telemetry is *latest-value-wins* per (client, node) so a slow client never
      builds a backlog of stale poses; pings/cancels/leaves are a reliable FIFO per client;
      if that FIFO overflows the client is disconnected and will resync from the snapshot
      on reconnect.
* Node-id ownership: the first connection to send as a node id owns it until it disconnects or
  goes stale; frames claiming that id from other connections are dropped.

Clients choose their downlink encoding with the URL query ``?encoding=json`` (default binary).
Either encoding is accepted on the uplink regardless.

Run:  ``lynx-relay --host 0.0.0.0 --port 8765``  (or ``python -m lynx.net.server``)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Set, Union
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .schema import (
    SERVER_NODE_ID,
    AnyMessage,
    CancelReason,
    LeaveReason,
    Message,
    NodeLeave,
    Ping,
    PingCancel,
    SchemaError,
    SequenceCounter,
    Telemetry,
    decode,
    encode,
    now_us,
)
from .state import WorldState

log = logging.getLogger("lynx.relay")

DEFAULT_PORT = 8765


@dataclass
class RelayConfig:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    stale_timeout_s: float = 5.0
    sweep_interval_s: float = 0.2
    telemetry_rate_hz: float = 60.0
    telemetry_burst: float = 20.0
    ping_rate_hz: float = 2.0
    ping_burst: float = 5.0
    default_ttl_ms: int = 60_000
    max_ttl_ms: int = 600_000
    max_pings_per_node: int = 8
    max_frame_bytes: int = 1024
    max_event_queue: int = 512
    max_consecutive_errors: int = 50


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/s, capacity ``burst``."""

    def __init__(self, rate: float, burst: float, now: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = now

    def allow(self, now: float, cost: float = 1.0) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


@dataclass
class _Session:
    ws: ServerConnection
    encoding: str
    sid: int
    nodes: Set[int] = field(default_factory=set)
    pending_telemetry: Dict[int, Telemetry] = field(default_factory=dict)
    events: Deque[Message] = field(default_factory=collections.deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    errors: int = 0


@dataclass
class RelayStats:
    connections_total: int = 0
    frames_in: int = 0
    frames_out: int = 0
    decode_errors: int = 0
    rate_limited: int = 0
    rejected: int = 0
    stale_seq: int = 0
    nodes_evicted: int = 0
    pings_expired: int = 0
    slow_consumers: int = 0


class RelayServer:
    def __init__(self, config: Optional[RelayConfig] = None) -> None:
        self.config = config or RelayConfig()
        self.state = WorldState()
        self.stats = RelayStats()
        self._sessions: Dict[int, _Session] = {}
        self._owner: Dict[int, int] = {}  # node_id -> session id
        self._tele_buckets: Dict[int, TokenBucket] = {}
        self._ping_buckets: Dict[int, TokenBucket] = {}
        self._seq = SequenceCounter()
        self._next_sid = 1
        self._server: Optional[Server] = None
        self._sweeper: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        cfg = self.config
        self._server = await serve(
            self._handle,
            cfg.host,
            cfg.port,
            max_size=cfg.max_frame_bytes,
            compression=None,  # frames are tiny; deflate costs CPU on the router for no gain
            ping_interval=5.0,
            ping_timeout=10.0,
        )
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="lynx-relay-sweeper")
        log.info("relay listening on ws://%s:%d", cfg.host, self.port)

    @property
    def port(self) -> int:
        if self._server is None:
            return self.config.port
        for sock in self._server.sockets:
            return sock.getsockname()[1]
        return self.config.port

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "RelayServer":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    @property
    def client_count(self) -> int:
        return len(self._sessions)

    # -- connection handling -----------------------------------------------------
    async def _handle(self, ws: ServerConnection) -> None:
        encoding = "binary"
        if ws.request is not None:
            q = parse_qs(urlsplit(ws.request.path).query)
            if q.get("encoding", ["binary"])[0].lower() == "json":
                encoding = "json"
        sess = _Session(ws=ws, encoding=encoding, sid=self._next_sid)
        self._next_sid += 1
        self._sessions[sess.sid] = sess
        self.stats.connections_total += 1
        log.info("client %d connected from %s (%s)", sess.sid, ws.remote_address, encoding)
        self._send_snapshot(sess)
        sender = asyncio.create_task(self._sender(sess), name=f"lynx-relay-tx-{sess.sid}")
        try:
            async for frame in ws:
                self._on_frame(sess, frame)
                if sess.closed:
                    break
        except ConnectionClosed:
            pass
        finally:
            sess.closed = True
            sess.wake.set()
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, ConnectionClosed):
                pass
            self._sessions.pop(sess.sid, None)
            for node in list(sess.nodes):
                self._drop_node(node, LeaveReason.DISCONNECT)
            log.info("client %d disconnected", sess.sid)

    def _send_snapshot(self, sess: _Session) -> None:
        now = time.monotonic()
        nodes, pings = self.state.snapshot()
        for node_state in nodes.values():
            sess.pending_telemetry[node_state.telemetry.node_id] = node_state.telemetry
        for ps in sorted(pings.values(), key=lambda p: p.received):
            sess.events.append(dataclasses.replace(ps.ping, ttl_ms=max(1, ps.remaining_ms(now))))
        sess.wake.set()

    async def _sender(self, sess: _Session) -> None:
        while not sess.closed:
            await sess.wake.wait()
            sess.wake.clear()
            while (sess.events or sess.pending_telemetry) and not sess.closed:
                if sess.events:
                    msg: Message = sess.events.popleft()
                else:
                    node = next(iter(sess.pending_telemetry))
                    msg = sess.pending_telemetry.pop(node)
                await sess.ws.send(encode(msg, sess.encoding))
                self.stats.frames_out += 1

    # -- fan-out -----------------------------------------------------------------
    def _enqueue(self, sess: _Session, msg: Message) -> None:
        if sess.closed:
            return
        if isinstance(msg, Telemetry):
            sess.pending_telemetry[msg.node_id] = msg
        else:
            if isinstance(msg, NodeLeave):
                sess.pending_telemetry.pop(msg.node, None)
            if len(sess.events) >= self.config.max_event_queue:
                self.stats.slow_consumers += 1
                log.warning("client %d event queue overflow; disconnecting", sess.sid)
                sess.closed = True
                asyncio.ensure_future(sess.ws.close(code=1013, reason="slow consumer"))
                return
            sess.events.append(msg)
        sess.wake.set()

    def _broadcast(self, msg: Message, exclude: Optional[int] = None) -> None:
        for sid, sess in list(self._sessions.items()):
            if sid != exclude:
                self._enqueue(sess, msg)

    def _server_msg(self, msg: Message) -> Message:
        msg.node_id = SERVER_NODE_ID
        msg.seq = self._seq.next()
        msg.ts_us = now_us()
        return msg

    def _drop_node(self, node: int, reason: LeaveReason) -> None:
        if self._owner.get(node) is not None:
            owner_sess = self._sessions.get(self._owner[node])
            if owner_sess is not None:
                owner_sess.nodes.discard(node)
            del self._owner[node]
        self._tele_buckets.pop(node, None)
        self._ping_buckets.pop(node, None)
        if self.state.remove_node(node):
            self.stats.nodes_evicted += reason == LeaveReason.STALE
            log.info("node %d left (%s)", node, reason.name.lower())
            self._broadcast(self._server_msg(NodeLeave(node=node, reason=reason)))

    # -- inbound -----------------------------------------------------------------
    def _on_frame(self, sess: _Session, frame: Union[bytes, str]) -> None:
        self.stats.frames_in += 1
        try:
            msg = decode(frame)
        except SchemaError as exc:
            self.stats.decode_errors += 1
            sess.errors += 1
            log.debug("client %d bad frame: %s", sess.sid, exc)
            if sess.errors >= self.config.max_consecutive_errors:
                sess.closed = True
                asyncio.ensure_future(sess.ws.close(code=1007, reason="too many bad frames"))
            return
        sess.errors = 0
        self.handle_message(sess, msg)

    def _claim(self, sess: _Session, node: int) -> Optional[bool]:
        """Bind ``node`` to ``sess``. Returns None if owned elsewhere, True if newly bound."""
        if node == SERVER_NODE_ID:
            return None
        owner = self._owner.get(node)
        if owner is None:
            self._owner[node] = sess.sid
            sess.nodes.add(node)
            return True
        if owner != sess.sid:
            return None
        return False

    def handle_message(self, sess: _Session, msg: AnyMessage) -> None:
        cfg = self.config
        now = time.monotonic()
        claimed = self._claim(sess, msg.node_id)
        if claimed is None:
            self.stats.rejected += 1
            return

        if isinstance(msg, Telemetry):
            bucket = self._tele_buckets.setdefault(
                msg.node_id, TokenBucket(cfg.telemetry_rate_hz, cfg.telemetry_burst, now)
            )
            if not bucket.allow(now):
                self.stats.rate_limited += 1
                return
            if not self.state.apply(msg, now, force=bool(claimed)):
                self.stats.stale_seq += 1
                return
            self._broadcast(msg, exclude=sess.sid)

        elif isinstance(msg, Ping):
            if msg.owner != msg.node_id:
                self.stats.rejected += 1
                return
            bucket = self._ping_buckets.setdefault(
                msg.node_id, TokenBucket(cfg.ping_rate_hz, cfg.ping_burst, now)
            )
            if not bucket.allow(now):
                self.stats.rate_limited += 1
                return
            if msg.ttl_ms == 0:
                msg.ttl_ms = cfg.default_ttl_ms
            msg.ttl_ms = min(msg.ttl_ms, cfg.max_ttl_ms)
            is_update = self.state.ping(msg.key) is not None
            self.state.apply(msg, now)
            if not is_update:
                active = self.state.pings_by_owner(msg.owner)
                for old in active[: max(0, len(active) - cfg.max_pings_per_node)]:
                    self.state.remove_ping(old.ping.key)
                    self._broadcast(
                        self._server_msg(
                            PingCancel(
                                ping_id=old.ping.ping_id,
                                owner=old.ping.owner,
                                reason=CancelReason.REPLACED,
                            )
                        )
                    )
            self._broadcast(msg, exclude=sess.sid)

        elif isinstance(msg, PingCancel):
            if msg.owner != msg.node_id:
                self.stats.rejected += 1
                return
            if self.state.apply(msg, now):
                self._broadcast(msg, exclude=sess.sid)

        elif isinstance(msg, NodeLeave):
            if msg.node != msg.node_id:
                self.stats.rejected += 1
                return
            self._drop_node(msg.node, LeaveReason.DISCONNECT)

    # -- housekeeping ---------------------------------------------------------------
    def sweep(self, now: Optional[float] = None) -> None:
        """Evict stale nodes and expire pings. Called periodically; public for testing."""
        now = time.monotonic() if now is None else now
        for node, ns in self.state.snapshot()[0].items():
            if now - ns.last_seen > self.config.stale_timeout_s:
                self._drop_node(node, LeaveReason.STALE)
        for ps in self.state.expire_pings(now):
            self.stats.pings_expired += 1
            self._broadcast(
                self._server_msg(
                    PingCancel(ping_id=ps.ping.ping_id, owner=ps.ping.owner, reason=CancelReason.EXPIRED)
                )
            )

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.sweep_interval_s)
            try:
                self.sweep()
            except Exception:  # never let housekeeping die silently
                log.exception("sweep failed")


async def run_relay(config: RelayConfig, stats_interval_s: float = 0.0) -> None:
    relay = RelayServer(config)
    await relay.start()
    stop = asyncio.Event()
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
                s = relay.stats
                log.info(
                    "clients=%d nodes=%d pings=%d in=%d out=%d dropped(rate=%d seq=%d bad=%d)",
                    relay.client_count,
                    len(relay.state),
                    len(relay.state.ping_keys),
                    s.frames_in,
                    s.frames_out,
                    s.rate_limited,
                    s.stale_seq,
                    s.decode_errors,
                )
    finally:
        await relay.stop()


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="TeamLynx squad WebSocket relay")
    p.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--stale-timeout", type=float, default=5.0, help="seconds without telemetry before eviction")
    p.add_argument("--max-ttl", type=float, default=600.0, help="maximum ping TTL in seconds")
    p.add_argument("--telemetry-hz", type=float, default=60.0, help="per-node inbound telemetry rate cap")
    p.add_argument("--stats-interval", type=float, default=10.0, help="log stats every N s (0 = off)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = RelayConfig(
        host=args.host,
        port=args.port,
        stale_timeout_s=args.stale_timeout,
        max_ttl_ms=int(args.max_ttl * 1000),
        telemetry_rate_hz=args.telemetry_hz,
    )
    try:
        asyncio.run(run_relay(cfg, args.stats_interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
