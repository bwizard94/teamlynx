"""Zero-config relay discovery for the squad LAN.

Headsets find the relay without any configuration, trying in order:

1. explicit ``--url`` values (operator override);
2. **UDP beacon**: each relay process answers queries and announces itself once a second on
   UDP 8766 (subnet broadcast). The announcement proves the *relay process* is alive, not just the
   host, and carries its role and priority, so primary/secondary ordering needs no config;
3. **mDNS** ``_lynx-relay._tcp`` (``zeroconf`` package if installed; OpenWrt announces it via
   umdns, Jetsons via avahi or the relay itself);
4. DNS names served by the router's dnsmasq: ``relay.lynx`` then ``relay2.lynx``;
5. the default gateway on port 8765 (relay on the travel router).

Beacon wire format (one UDP datagram, UTF-8 JSON, < 256 bytes)::

    query:         {"svc": "lynx-relay", "q": 1, "squad": "LYNX"}
    announcement:  {"svc": "lynx-relay", "v": 1, "port": 8765, "role": "primary", "prio": 0,
                    "squad": "LYNX", "id": "glinet-mt3000", "clients": 7}

The relay's address is the datagram's *source* address, never a field in the payload, so a
multi-homed relay is reached on the interface the headset actually heard it on.

Standard library only: this module runs inside the relay on an OpenWrt router.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("lynx.discovery")

SERVICE = "lynx-relay"
BEACON_PORT = 8766
RELAY_PORT = 8765
MDNS_TYPE = "_lynx-relay._tcp.local."
DNS_NAMES = ("relay.lynx", "relay2.lynx")
ROLE_RANK = {"primary": 0, "secondary": 1}
MAX_DATAGRAM = 512

Address = Tuple[str, int]


@dataclass(frozen=True)
class RelayAnnouncement:
    host: str
    port: int = RELAY_PORT
    role: str = "primary"
    priority: int = 0
    squad: str = ""
    relay_id: str = ""
    clients: int = 0
    source: str = "beacon"

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"ws://{host}:{self.port}"

    @property
    def sort_key(self) -> tuple:
        return (self.priority, ROLE_RANK.get(self.role, 9), self.host, self.port)


def encode_query(squad: str = "") -> bytes:
    return json.dumps({"svc": SERVICE, "q": 1, "squad": squad}, separators=(",", ":")).encode()


def encode_announcement(port: int, role: str, priority: int, squad: str, relay_id: str, clients: int) -> bytes:
    return json.dumps({"svc": SERVICE, "v": 1, "port": port, "role": role, "prio": priority, "squad": squad,
                       "id": relay_id, "clients": clients}, separators=(",", ":")).encode()


def _decode(data: bytes) -> Optional[dict]:
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("svc") != SERVICE:
        return None
    return obj


def parse_announcement(data: bytes, addr: Address) -> Optional[RelayAnnouncement]:
    obj = _decode(data)
    if obj is None or obj.get("q") or obj.get("v") != 1:
        return None
    try:
        port = int(obj.get("port", RELAY_PORT))
        if not 0 < port < 65536:
            return None
        return RelayAnnouncement(addr[0], port, str(obj.get("role", "primary")), int(obj.get("prio", 0)),
                                 str(obj.get("squad", "")), str(obj.get("id", "")), int(obj.get("clients", 0)))
    except (TypeError, ValueError):
        return None


def is_query(data: bytes) -> Optional[str]:
    """The squad filter of a query (``""`` = any), or None if ``data`` is not a query."""
    obj = _decode(data)
    if obj is None or obj.get("q") != 1:
        return None
    return str(obj.get("squad", ""))


# ------------------------------------------------------------------------------- interfaces


def linux_broadcast_addresses() -> List[str]:
    """IPv4 broadcast address of every up, non-loopback interface (Linux ``SIOCGIFBRDADDR``)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return []
    out: List[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            if name == "lo":
                continue
            req = struct.pack("256s", name.encode()[:15])
            try:
                flags = struct.unpack("H", fcntl.ioctl(s.fileno(), 0x8913, req)[16:18])[0]  # SIOCGIFFLAGS
                if not flags & 0x1 or not flags & 0x2:  # IFF_UP, IFF_BROADCAST
                    continue
                brd = fcntl.ioctl(s.fileno(), 0x8919, req)  # SIOCGIFBRDADDR
            except OSError:
                continue
            addr = socket.inet_ntoa(brd[20:24])
            if addr != "0.0.0.0" and addr not in out:
                out.append(addr)
    finally:
        s.close()
    return out


def default_broadcast_targets(port: int = BEACON_PORT) -> List[Address]:
    addrs = linux_broadcast_addresses() or []
    return [(a, port) for a in addrs] + [("255.255.255.255", port)]


def default_gateway() -> Optional[str]:
    """IPv4 default gateway from ``/proc/net/route`` (Linux), else None."""
    try:
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000" and int(parts[3], 16) & 0x2:
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError, StopIteration):
        return None
    return None


# ------------------------------------------------------------------------------- relay side


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, beacon: "RelayBeacon") -> None:
        self.beacon = beacon
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        squad = is_query(data)
        if squad is None or (squad and self.beacon.squad and squad != self.beacon.squad):
            return
        self.beacon.queries += 1
        self.beacon._send(addr)

    def error_received(self, exc) -> None:  # pragma: no cover - ICMP noise
        log.debug("beacon socket error: %s", exc)


class RelayBeacon:
    """Announces a relay on UDP and answers discovery queries (runs inside the relay's loop)."""

    def __init__(self, relay_port: int, role: str = "primary", priority: int = 0, squad: str = "",
                 relay_id: str = "", bind: Address = ("0.0.0.0", BEACON_PORT),
                 targets: Optional[Sequence[Address]] = None, interval_s: float = 1.0,
                 clients_fn: Callable[[], int] = lambda: 0) -> None:
        self.relay_port = relay_port
        self.role = role
        self.priority = priority
        self.squad = squad
        self.relay_id = relay_id or socket.gethostname()
        self.bind = bind
        self.targets = list(targets) if targets is not None else default_broadcast_targets(bind[1] or BEACON_PORT)
        self.interval_s = interval_s
        self.clients_fn = clients_fn
        self.sent = 0
        self.queries = 0
        self._proto: Optional[_BeaconProtocol] = None
        self._task: Optional[asyncio.Task] = None

    def payload(self) -> bytes:
        return encode_announcement(self.relay_port, self.role, self.priority, self.squad, self.relay_id,
                                   self.clients_fn())

    def _send(self, addr: Address) -> None:
        if self._proto is None or self._proto.transport is None:
            return
        try:
            self._proto.transport.sendto(self.payload(), addr)
            self.sent += 1
        except OSError as exc:
            log.debug("beacon send to %s failed: %s", addr, exc)

    @property
    def port(self) -> int:
        if self._proto is None or self._proto.transport is None:
            return self.bind[1]
        return self._proto.transport.get_extra_info("sockname")[1]

    async def start(self) -> "RelayBeacon":
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(self.bind)
        sock.setblocking(False)
        _, proto = await loop.create_datagram_endpoint(lambda: _BeaconProtocol(self), sock=sock)
        self._proto = proto
        self._task = asyncio.create_task(self._announce_loop(), name="lynx-beacon")
        log.info("beacon on udp %s:%d -> %s", self.bind[0], self.port, ", ".join(f"{h}:{p}" for h, p in self.targets))
        return self

    async def _announce_loop(self) -> None:
        while True:
            for t in self.targets:
                self._send(t)
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._proto is not None and self._proto.transport is not None:
            self._proto.transport.close()
        self._proto = None


# ------------------------------------------------------------------------------- headset side


def discover_beacons(timeout_s: float = 1.0, squad: str = "", targets: Optional[Sequence[Address]] = None,
                     listen_port: Optional[int] = BEACON_PORT) -> List[RelayAnnouncement]:
    """Query for relays and collect answers (plus passive announcements) for ``timeout_s``.

    Returns unique relays sorted by (priority, role, host). Blocking; call from a thread if needed.
    """
    targets = list(targets) if targets is not None else default_broadcast_targets()
    socks: List[socket.socket] = []
    q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    q.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    q.bind(("0.0.0.0", 0))
    socks.append(q)
    if listen_port:
        p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        p.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            p.bind(("0.0.0.0", listen_port))
            socks.append(p)
        except OSError:
            p.close()
    found = {}
    try:
        query = encode_query(squad)
        for t in targets:
            try:
                q.sendto(query, t)
            except OSError as exc:
                log.debug("query to %s failed: %s", t, exc)
        end = time.monotonic() + timeout_s
        import selectors

        sel = selectors.DefaultSelector()
        for s in socks:
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ)
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                for key, _ in sel.select(left):
                    try:
                        data, addr = key.fileobj.recvfrom(MAX_DATAGRAM + 1)
                    except OSError:
                        continue
                    ann = parse_announcement(data, addr)
                    if ann is None or (squad and ann.squad and ann.squad != squad):
                        continue
                    found[(ann.host, ann.port)] = ann
        finally:
            sel.close()
    finally:
        for s in socks:
            s.close()
    return sorted(found.values(), key=lambda a: a.sort_key)


def mdns_browse(timeout_s: float = 1.5) -> List[RelayAnnouncement]:
    """Browse ``_lynx-relay._tcp`` with python-zeroconf if installed (else empty)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    out: List[RelayAnnouncement] = []
    zc = Zeroconf()
    names: List[str] = []

    class _L:
        def add_service(self, zc_, type_, name):
            names.append(name)

        def update_service(self, zc_, type_, name):
            pass

        def remove_service(self, zc_, type_, name):
            pass

    try:
        ServiceBrowser(zc, MDNS_TYPE, _L())
        time.sleep(timeout_s)
        for name in names:
            info = zc.get_service_info(MDNS_TYPE, name, timeout=500)
            if info is None:
                continue
            props = {k.decode(): (v.decode() if isinstance(v, bytes) else "") for k, v in info.properties.items()}
            for addr in info.parsed_addresses():
                out.append(RelayAnnouncement(addr, info.port, props.get("role", "primary"),
                                             int(props.get("prio", "0") or 0), props.get("squad", ""),
                                             name.split(".")[0], 0, "mdns"))
    finally:
        zc.close()
    return sorted(out, key=lambda a: a.sort_key)


class MdnsAdvertiser:
    """Registers ``_lynx-relay._tcp`` via python-zeroconf (no-op if the package is missing)."""

    def __init__(self, port: int, role: str, priority: int, squad: str, name: str = "") -> None:
        self.port, self.role, self.priority, self.squad = port, role, priority, squad
        self.name = name or socket.gethostname()
        self._zc = None
        self._info = None

    def start(self) -> bool:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.info("zeroconf not installed; mDNS left to avahi/umdns")
            return False
        self._info = ServiceInfo(MDNS_TYPE, f"{self.name}.{MDNS_TYPE}", port=self.port,
                                 properties={"role": self.role, "prio": str(self.priority), "squad": self.squad},
                                 server=f"{self.name}.local.")
        self._zc = Zeroconf()
        self._zc.register_service(self._info)
        return True

    def stop(self) -> None:
        if self._zc is not None:
            self._zc.unregister_service(self._info)
            self._zc.close()
            self._zc = None


def _resolves(name: str, timeout_s: float) -> bool:
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(socket.getaddrinfo, name, RELAY_PORT, socket.AF_INET, socket.SOCK_STREAM)
    try:
        return bool(fut.result(timeout=timeout_s))
    except Exception:
        return False
    finally:
        ex.shutdown(wait=False)


def resolve_relay_urls(explicit: Iterable[str] = (), *, beacon: bool = True, beacon_timeout_s: float = 1.0,
                       squad: str = "", mdns: bool = True, mdns_timeout_s: float = 1.5,
                       dns_names: Sequence[str] = DNS_NAMES, gateway: bool = True,
                       beacon_targets: Optional[Sequence[Address]] = None,
                       beacon_listen_port: Optional[int] = BEACON_PORT, dns_timeout_s: float = 0.5) -> List[str]:
    """Ordered, de-duplicated candidate relay URLs (see the module docstring for the order)."""
    urls: List[str] = []

    def add(u: str) -> None:
        if u not in urls:
            urls.append(u)

    for u in explicit:
        add(u)
    if beacon:
        for a in discover_beacons(beacon_timeout_s, squad, beacon_targets, beacon_listen_port):
            add(a.url)
    if mdns and len(urls) < 2:
        for a in mdns_browse(mdns_timeout_s):
            if not squad or not a.squad or a.squad == squad:
                add(a.url)
    for name in dns_names:
        if _resolves(name, dns_timeout_s):
            add(f"ws://{name}:{RELAY_PORT}")
    if gateway:
        gw = default_gateway()
        if gw:
            add(f"ws://{gw}:{RELAY_PORT}")
    return urls
