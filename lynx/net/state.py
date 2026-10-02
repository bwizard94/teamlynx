"""Shared world model (nodes + pings) maintained by both the relay and every client.

All times are receiver-side ``time.monotonic()`` seconds. The class is thread-safe so a render
loop can take snapshots while an asyncio network thread applies updates.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .schema import AnyMessage, NodeLeave, Ping, PingCancel, Telemetry, seq_newer

PingKey = Tuple[int, int]

DEFAULT_SEQ_RESET_S = 2.0
"""If a node has been silent this long, any sequence number is accepted (sender restarted)."""


@dataclass
class NodeState:
    telemetry: Telemetry
    last_seen: float


@dataclass
class PingState:
    ping: Ping
    received: float
    expires_at: float

    def remaining_s(self, now: float) -> float:
        return max(0.0, self.expires_at - now)

    def remaining_ms(self, now: float) -> int:
        return int(round(self.remaining_s(now) * 1000.0))


class WorldState:
    def __init__(self, seq_reset_s: float = DEFAULT_SEQ_RESET_S) -> None:
        self.seq_reset_s = seq_reset_s
        self._nodes: Dict[int, NodeState] = {}
        self._pings: Dict[PingKey, PingState] = {}
        self._lock = threading.RLock()

    # -- mutation --------------------------------------------------------------
    def apply(self, msg: AnyMessage, now: float, force: bool = False) -> bool:
        """Apply a message; returns True if the world changed (i.e. it should be relayed).

        Telemetry is dropped if its sequence number is not newer than the last accepted one for
        that node (duplicates / reordering), unless ``force`` is set or the node went quiet for
        longer than ``seq_reset_s`` (sender restart resets its counter).
        """
        with self._lock:
            if isinstance(msg, Telemetry):
                prev = self._nodes.get(msg.node_id)
                if (
                    prev is not None
                    and not force
                    and not seq_newer(msg.seq, prev.telemetry.seq)
                    and now - prev.last_seen < self.seq_reset_s
                ):
                    return False
                self._nodes[msg.node_id] = NodeState(msg, now)
                return True
            if isinstance(msg, Ping):
                self._pings[msg.key] = PingState(msg, now, now + msg.ttl_ms / 1000.0)
                return True
            if isinstance(msg, PingCancel):
                return self._pings.pop(msg.key, None) is not None
            if isinstance(msg, NodeLeave):
                return self._nodes.pop(msg.node, None) is not None
        return False

    def remove_node(self, node_id: int) -> bool:
        with self._lock:
            return self._nodes.pop(node_id, None) is not None

    def remove_ping(self, key: PingKey) -> Optional[PingState]:
        with self._lock:
            return self._pings.pop(key, None)

    def expire_pings(self, now: float) -> List[PingState]:
        with self._lock:
            dead = [k for k, p in self._pings.items() if p.expires_at <= now]
            return [self._pings.pop(k) for k in dead]

    def evict_stale(self, now: float, timeout_s: float) -> List[int]:
        with self._lock:
            dead = [n for n, s in self._nodes.items() if now - s.last_seen > timeout_s]
            for n in dead:
                del self._nodes[n]
            return dead

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()
            self._pings.clear()

    # -- queries ---------------------------------------------------------------
    def node(self, node_id: int) -> Optional[NodeState]:
        with self._lock:
            s = self._nodes.get(node_id)
            return copy.deepcopy(s) if s else None

    def ping(self, key: PingKey) -> Optional[PingState]:
        with self._lock:
            s = self._pings.get(key)
            return copy.deepcopy(s) if s else None

    def pings_by_owner(self, owner: int) -> List[PingState]:
        with self._lock:
            return sorted(
                (copy.deepcopy(p) for k, p in self._pings.items() if k[0] == owner),
                key=lambda p: p.received,
            )

    def snapshot(self) -> Tuple[Dict[int, NodeState], Dict[PingKey, PingState]]:
        """Deep copies, safe to use from another thread."""
        with self._lock:
            return copy.deepcopy(self._nodes), copy.deepcopy(self._pings)

    @property
    def node_ids(self) -> List[int]:
        with self._lock:
            return list(self._nodes)

    @property
    def ping_keys(self) -> List[PingKey]:
        with self._lock:
            return list(self._pings)

    def __len__(self) -> int:
        with self._lock:
            return len(self._nodes)
