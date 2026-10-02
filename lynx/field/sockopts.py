"""Socket options for the squad link: DSCP marking (Wi-Fi WMM access category) and TCP_CORK.

DSCP -> 802.11 user priority. Linux and hostapd map the IP precedence (DSCP >> 3) onto the
802.1d user priority unless a QoS map is configured, so EF (46) -> UP 5 -> AC_VI. AC_VI contends
with AIFSN 2 and CWmin 7 instead of best effort's AIFSN 3 and CWmin 15, which cuts the mean
channel-access delay per frame (AIFS + mean backoff) from ~110 us to ~66 us and puts squad
telemetry ahead of any bulk traffic (log sync, PiP video) on the same radio.
"""

from __future__ import annotations

import socket
from typing import Any, Optional

DSCP_EF = 46
DSCP_CS0 = 0


def _socket_of(obj: Any) -> Optional[socket.socket]:
    if obj is None:
        return None
    if hasattr(obj, "setsockopt"):
        return obj
    transport = getattr(obj, "transport", obj)
    get = getattr(transport, "get_extra_info", None)
    return get("socket") if get is not None else None


def set_dscp(obj: Any, dscp: int) -> bool:
    """Mark an asyncio transport / websocket connection / socket with ``dscp``; True on success."""
    sock = _socket_of(obj)
    if sock is None or dscp is None:
        return False
    tos = (int(dscp) & 0x3F) << 2
    try:
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, tos)
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
        return True
    except (OSError, AttributeError):
        return False


def get_tos(obj: Any) -> Optional[int]:
    sock = _socket_of(obj)
    if sock is None:
        return None
    try:
        return sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
    except OSError:
        return None


TCP_CORK = getattr(socket, "TCP_CORK", None)


def cork(obj: Any, on: bool) -> bool:
    """Linux TCP_CORK: hold partial segments until uncorked so a burst leaves as one segment."""
    sock = _socket_of(obj)
    if sock is None or TCP_CORK is None:
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, TCP_CORK, 1 if on else 0)
        return True
    except OSError:
        return False
