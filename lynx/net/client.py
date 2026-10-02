"""Async client library for the TeamLynx relay, plus a thread-hosted wrapper for render loops.

``LynxClient`` (asyncio):
    * connects with exponential-backoff reconnect;
    * keeps a :class:`~lynx.net.state.WorldState` of the squad (other nodes + all pings,
      including this node's own pings) updated from the relay;
    * expires pings locally on its own monotonic clock;
    * re-publishes its own still-active pings after a reconnect, so the squad picture survives
      a relay restart (the relay holds state only in RAM).

``BackgroundClient`` runs a ``LynxClient`` on a private event loop in a daemon thread and exposes
non-blocking, thread-safe methods for synchronous code (pygame loop, Phase 3 serial reader).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple, Union

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .schema import (
    AnyMessage,
    CancelReason,
    NodeLeave,
    Ping,
    PingCancel,
    PingType,
    SchemaError,
    SequenceCounter,
    Team,
    Telemetry,
    decode,
    encode,
    now_us,
)
from .state import NodeState, PingKey, PingState, WorldState

log = logging.getLogger("lynx.client")

MessageCallback = Callable[[AnyMessage], None]


class LynxClient:
    def __init__(
        self,
        url: str,
        node_id: int,
        callsign: str,
        team: Union[Team, int] = Team.BLUE,
        encoding: str = "binary",
        on_message: Optional[MessageCallback] = None,
        reconnect: bool = True,
        backoff_initial_s: float = 0.25,
        backoff_max_s: float = 5.0,
        stale_timeout_s: float = 6.0,
    ) -> None:
        if not 1 <= node_id <= 0xFFFF:
            raise ValueError("node_id must be in 1..65535 (0 is reserved for the relay)")
        if encoding not in ("binary", "json"):
            raise ValueError("encoding must be 'binary' or 'json'")
        self.url = url
        self.node_id = node_id
        self.callsign = callsign
        self.team = Team(team)
        self.encoding = encoding
        self.on_message = on_message
        self.reconnect = reconnect
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.stale_timeout_s = stale_timeout_s

        self.state = WorldState()
        self.connected = asyncio.Event()
        self.last_rx_monotonic: float = 0.0
        self.rx_count = 0
        self.tx_count = 0

        self._seq = SequenceCounter()
        self._ping_ids = SequenceCounter(1)
        self._ws: Optional[ClientConnection] = None
        self._stopping = False
        self._send_lock = asyncio.Lock()

    # -- connection ------------------------------------------------------------
    def _full_url(self) -> str:
        if self.encoding == "json":
            sep = "&" if "?" in self.url else "?"
            return f"{self.url}{sep}encoding=json"
        return self.url

    async def connect(self) -> None:
        """Open a single connection (no retry). ``run()`` handles reconnects."""
        self._ws = await connect(self._full_url(), compression=None, open_timeout=5.0, max_size=4096)
        self.connected.set()
        log.info("node %d connected to %s", self.node_id, self.url)
        await self._republish_own_pings()

    async def run(self) -> None:
        """Connect and process inbound messages until :meth:`close`; reconnects if enabled."""
        backoff = self.backoff_initial_s
        self._stopping = False
        while not self._stopping:
            try:
                if self._ws is None:
                    await self.connect()
                backoff = self.backoff_initial_s
                await self._recv_loop()
            except (OSError, ConnectionClosed, InvalidHandshake, asyncio.TimeoutError) as exc:
                log.debug("node %d connection error: %s", self.node_id, exc)
            except InvalidURI:
                raise
            finally:
                self.connected.clear()
                self._ws = None
            if self._stopping or not self.reconnect:
                break
            await asyncio.sleep(backoff)
            backoff = min(self.backoff_max_s, backoff * 2.0)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for frame in self._ws:
            try:
                msg = decode(frame)
            except SchemaError as exc:
                log.debug("node %d dropped bad frame: %s", self.node_id, exc)
                continue
            self.handle_message(msg)

    def handle_message(self, msg: AnyMessage) -> None:
        now = time.monotonic()
        self.last_rx_monotonic = now
        self.rx_count += 1
        if isinstance(msg, Telemetry) and msg.node_id == self.node_id:
            return  # never track ourselves as a remote node
        self.state.apply(msg, now)
        if self.on_message is not None:
            try:
                self.on_message(msg)
            except Exception:
                log.exception("on_message callback failed")

    async def close(self, leave: bool = True) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            if leave:
                try:
                    await self._send(NodeLeave(node=self.node_id))
                except ConnectionClosed:
                    pass
            await ws.close()

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.connected.wait(), timeout)

    # -- sending ---------------------------------------------------------------
    async def _send(self, msg: AnyMessage) -> bool:
        ws = self._ws
        if ws is None:
            return False
        msg.node_id = self.node_id
        msg.seq = self._seq.next()
        msg.ts_us = now_us()
        data = encode(msg, self.encoding)
        async with self._send_lock:
            try:
                await ws.send(data)
            except ConnectionClosed:
                return False
        self.tx_count += 1
        return True

    async def send_telemetry(
        self,
        x: float,
        y: float,
        z: float,
        heading: float,
        pitch: float,
        roll: float,
        flags: int = 0,
    ) -> bool:
        """Publish this node's pose. Returns False if not connected (telemetry is not queued)."""
        return await self._send(
            Telemetry(
                team=self.team,
                flags=flags,
                callsign=self.callsign,
                x=float(x),
                y=float(y),
                z=float(z),
                heading=float(heading),
                pitch=float(pitch),
                roll=float(roll),
            )
        )

    async def send_ping(
        self,
        x: float,
        y: float,
        z: float,
        ping_type: Union[PingType, int] = PingType.MARK,
        ttl_s: float = 60.0,
        ping_id: Optional[int] = None,
    ) -> Ping:
        """Create (or move, if ``ping_id`` is given) one of this node's pings.

        The ping is recorded locally even while disconnected and is published on reconnect.
        """
        if ping_id is None:
            ping_id = self._ping_ids.next()
            if ping_id == 0:
                ping_id = self._ping_ids.next()
        ping = Ping(
            ping_id=ping_id,
            owner=self.node_id,
            ping_type=PingType(ping_type),
            x=float(x),
            y=float(y),
            z=float(z),
            ttl_ms=max(1, int(round(ttl_s * 1000.0))),
        )
        ping.node_id = self.node_id
        self.state.apply(ping, time.monotonic())
        await self._send(ping)
        return ping

    async def cancel_ping(self, ping_id: int) -> bool:
        removed = self.state.remove_ping((self.node_id, ping_id)) is not None
        await self._send(PingCancel(ping_id=ping_id, owner=self.node_id, reason=CancelReason.OWNER))
        return removed

    async def _republish_own_pings(self) -> None:
        now = time.monotonic()
        for ps in self.state.pings_by_owner(self.node_id):
            remaining = ps.remaining_ms(now)
            if remaining > 0:
                await self._send(dataclasses.replace(ps.ping, ttl_ms=remaining))

    # -- state -----------------------------------------------------------------
    def expire(self, now: Optional[float] = None) -> None:
        """Drop locally expired pings and remote nodes silent for ``stale_timeout_s``.

        The relay normally announces both; doing it locally too keeps the HUD honest if the
        relay itself disappears.
        """
        now = time.monotonic() if now is None else now
        self.state.expire_pings(now)
        self.state.evict_stale(now, self.stale_timeout_s)

    def own_pings(self) -> list[PingState]:
        return self.state.pings_by_owner(self.node_id)


class BackgroundClient:
    """Run a :class:`LynxClient` in a daemon thread; all methods are thread-safe and non-blocking."""

    def __init__(self, client: LynxClient) -> None:
        self.client = client
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=f"lynx-net-{client.node_id}", daemon=True)
        self._task: Optional[asyncio.Future] = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self.client.run())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("network thread crashed")
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> "BackgroundClient":
        self._thread.start()
        return self

    def _submit(self, coro) -> Optional["asyncio.Future"]:
        if self._loop.is_closed():
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            coro.close()
            return None

    @property
    def connected(self) -> bool:
        return self.client.connected.is_set()

    def send_telemetry(self, *args, **kwargs) -> None:
        self._submit(self.client.send_telemetry(*args, **kwargs))

    def send_ping(self, *args, **kwargs) -> Optional[Ping]:
        """Fire-and-forget; blocks briefly (<=0.5 s) only to return the created ping."""
        fut = self._submit(self.client.send_ping(*args, **kwargs))
        if fut is None:
            return None
        try:
            return fut.result(timeout=0.5)
        except Exception:
            return None

    def cancel_ping(self, ping_id: int) -> None:
        self._submit(self.client.cancel_ping(ping_id))

    def snapshot(self) -> Tuple[Dict[int, NodeState], Dict[PingKey, PingState]]:
        self.client.expire()
        return self.client.state.snapshot()

    def stop(self, timeout: float = 2.0) -> None:
        fut = self._submit(self.client.close())
        if fut is not None:
            try:
                fut.result(timeout=timeout)
            except Exception:
                pass
        self._thread.join(timeout)
