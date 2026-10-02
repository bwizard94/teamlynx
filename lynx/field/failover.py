"""Relay client with failover across several relays, fast dead-link detection and failback.

:class:`FailoverClient` is a drop-in :class:`lynx.net.client.LynxClient` (same state, sending
and ``BackgroundClient`` hosting) with a different connection policy:

* **Candidates in priority order** (``urls[0]`` is the primary, normally the relay on the
  router). Every connection attempt cycle starts from the primary.
* **Full-jitter exponential backoff** after a cycle in which no candidate answered:
  ``sleep = U(0, min(backoff_max, backoff_initial * 2^k))``. Ten headsets that lost the router at
  the same instant do not reconnect in lock-step when it comes back.
* **Fast dead-link detection**: WebSocket keepalive every ``keepalive_interval_s`` with a
  ``keepalive_timeout_s`` deadline (defaults 1 s / 2 s), so a silent Wi-Fi drop is detected in
  <= 3 s instead of the library default 40 s.
* **Failback**: while on a secondary, the primary is probed every ``probe_interval_s``; after
  ``failback_after`` consecutive successful probes the client drops the secondary and returns,
  so the squad re-converges on one relay without flapping.
* **Rediscovery**: if a whole cycle fails and ``rediscover`` is given (e.g.
  :func:`lynx.field.discovery.resolve_relay_urls`), the candidate list is refreshed off-loop.
* **DSCP EF** on the socket (see :mod:`lynx.field.sockopts`).

Own pings are recorded locally and re-published with their remaining TTL on every (re)connect,
whichever relay that is (inherited behaviour), so a failover carries the squad's pings across.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Sequence, Union

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from lynx.net.client import LynxClient
from lynx.net.schema import Team

from .sockopts import DSCP_EF, set_dscp

log = logging.getLogger("lynx.failover")

CONNECT_ERRORS = (OSError, InvalidHandshake, asyncio.TimeoutError, ConnectionClosed)


@dataclass(frozen=True)
class LinkEvent:
    t: float  # monotonic
    kind: str  # connected | disconnected | connect-failed | failback | rediscovered
    url: str
    detail: str = ""


async def probe_relay(url: str, timeout_s: float = 1.0) -> bool:
    """True if a WebSocket handshake with ``url`` completes within ``timeout_s``."""
    try:
        async with connect(url, open_timeout=timeout_s, ping_interval=None, compression=None, proxy=None,
                           close_timeout=0.5):
            return True
    except (*CONNECT_ERRORS, InvalidURI):
        return False


class FailoverClient(LynxClient):
    def __init__(
        self,
        urls: Sequence[str],
        node_id: int,
        callsign: str,
        team: Union[Team, int] = Team.BLUE,
        *,
        encoding: str = "binary",
        on_message=None,
        rediscover: Optional[Callable[[], Sequence[str]]] = None,
        backoff_initial_s: float = 0.25,
        backoff_max_s: float = 4.0,
        stale_timeout_s: float = 6.0,
        keepalive_interval_s: float = 1.0,
        keepalive_timeout_s: float = 2.0,
        open_timeout_s: float = 2.0,
        failback: bool = True,
        probe_interval_s: float = 5.0,
        failback_after: int = 2,
        dscp: Optional[int] = DSCP_EF,
        rng: Optional[random.Random] = None,
    ) -> None:
        urls = [u for u in urls if u]
        if not urls:
            raise ValueError("FailoverClient needs at least one relay URL")
        super().__init__(urls[0], node_id, callsign, team, encoding=encoding, on_message=on_message,
                         reconnect=True, backoff_initial_s=backoff_initial_s, backoff_max_s=backoff_max_s,
                         stale_timeout_s=stale_timeout_s)
        self.urls: List[str] = list(urls)
        self.rediscover = rediscover
        self.keepalive_interval_s = keepalive_interval_s
        self.keepalive_timeout_s = keepalive_timeout_s
        self.open_timeout_s = open_timeout_s
        self.failback = failback
        self.probe_interval_s = probe_interval_s
        self.failback_after = failback_after
        self.dscp = dscp
        self.rng = rng or random.Random()
        self.events: Deque[LinkEvent] = collections.deque(maxlen=256)
        self.current_url: Optional[str] = None
        self.down_since: Optional[float] = time.monotonic()
        self.connects = 0
        self.failovers = 0
        self.failbacks = 0
        self.listeners: List[Callable[[LinkEvent], None]] = []
        self._failback_now = False
        self._wake: Optional[asyncio.Event] = None

    # -- state ----------------------------------------------------------------------------------

    @property
    def on_primary(self) -> bool:
        return self.current_url is not None and self.current_url == self.urls[0]

    def link_down_s(self, now: Optional[float] = None) -> float:
        if self.down_since is None:
            return 0.0
        return (time.monotonic() if now is None else now) - self.down_since

    def _event(self, kind: str, url: str, detail: str = "") -> None:
        ev = LinkEvent(time.monotonic(), kind, url, detail)
        self.events.append(ev)
        log.info("node %d %s %s %s", self.node_id, kind, url, detail)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("link listener failed")

    # -- connection -----------------------------------------------------------------------------

    async def connect(self) -> None:
        ws = await connect(self._full_url(), compression=None, open_timeout=self.open_timeout_s, max_size=4096,
                           ping_interval=self.keepalive_interval_s, ping_timeout=self.keepalive_timeout_s,
                           close_timeout=1.0, proxy=None)
        if self.dscp is not None:
            set_dscp(ws, self.dscp)
        self._ws = ws
        previous = self.current_url
        self.current_url = self.url
        self.down_since = None
        self.connects += 1
        if previous is not None and previous != self.url:
            if self.url == self.urls[0]:
                self.failbacks += 1
            else:
                self.failovers += 1
        self.connected.set()
        self._event("connected", self.url)
        await self._republish_own_pings()

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _refresh(self) -> None:
        if self.rediscover is None:
            return
        try:
            fresh = await asyncio.to_thread(self.rediscover)
        except Exception:
            log.exception("relay rediscovery failed")
            return
        fresh = [u for u in fresh if u]
        if fresh and fresh != self.urls:
            self.urls = list(fresh)
            self._event("rediscovered", self.urls[0], " ".join(self.urls))

    async def _probe_primary(self, ws) -> None:
        ok = 0
        while True:
            await asyncio.sleep(self.probe_interval_s)
            if await probe_relay(self.urls[0], self.open_timeout_s):
                ok += 1
            else:
                ok = 0
            if ok >= self.failback_after:
                self._failback_now = True
                self._event("failback", self.urls[0], f"leaving {self.url}")
                await ws.close(code=1001, reason="failback to primary")
                return

    async def run(self) -> None:
        self._stopping = False
        self._wake = asyncio.Event()
        failed_cycles = 0
        while not self._stopping:
            healthy = False
            for url in list(self.urls):
                if self._stopping:
                    break
                self.url = url
                try:
                    await self.connect()
                except InvalidURI:
                    raise
                except CONNECT_ERRORS as exc:
                    self._event("connect-failed", url, type(exc).__name__)
                    continue
                started = time.monotonic()
                ws = self._ws
                probe = None
                if self.failback and url != self.urls[0]:
                    probe = asyncio.create_task(self._probe_primary(ws), name=f"lynx-probe-{self.node_id}")
                try:
                    await self._recv_loop()
                except ConnectionClosed:
                    pass
                except OSError as exc:
                    log.debug("node %d link error: %s", self.node_id, exc)
                finally:
                    if probe is not None:
                        probe.cancel()
                    self.connected.clear()
                    self._ws = None
                    self.down_since = time.monotonic()
                    self._event("disconnected", url)
                # A relay that accepts and immediately drops us (overload, 1013) must not cause a
                # tight reconnect loop, so only a session that lasted resets the backoff.
                healthy = time.monotonic() - started >= 2.0 or self._failback_now
                break
            if self._stopping:
                break
            if healthy:
                failed_cycles = 0
                self._failback_now = False
                continue
            failed_cycles += 1
            await self._refresh()
            cap = min(self.backoff_max_s, self.backoff_initial_s * (2.0 ** (failed_cycles - 1)))
            await self._sleep(self.rng.uniform(0.0, cap))

    async def close(self, leave: bool = True) -> None:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        await super().close(leave)
