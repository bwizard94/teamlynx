"""Headset behaviour when the squad link drops: hold last-known friendlies, styled as stale.

Policy (``docs/field/net-robustness.md``):

* **Friendlies only.** The holdover remembers *teammate telemetry* (nodes on the squad relay). It
  never stores detections, IFF tracks or TANGO positions: an unverified contact that leaves the
  camera view is gone (no dead-enemy tracking). Pings keep their own TTL as before.
* **Live** (telemetry age <= ``stale_after_s``, 2 s = ``IffConfig.max_telemetry_age_s``): the
  track passes through untouched.
* **Stale** (older, link up or down): position is dead-reckoned from the last fix with the
  velocity estimated from recent fixes, for at most ``dr_horizon_s`` and ``max_dr_m``, then held.
  The track keeps its *original* telemetry timestamp, so the IFF associator marks it stale: it can
  never confirm a FRIENDLY box, but it still suppresses TANGO near the teammate's last-known
  bearing (blue-on-blue protection survives the link loss).
* **Styling**: stale tracks are drawn in the stale colour (``white``: the HUD halves it to grey
  for BFT diamonds and adds ``STALE``) and the callsign carries the age, e.g. ``BRAVO 14s``.
* **Forget** after ``hold_s`` (120 s) without telemetry, or ``leave_hold_s`` (30 s) after the
  relay announced NODE_LEAVE(disconnect). A crashed headset and a clean shutdown look the same on
  the wire, and dropping a teammate who is still on the field is the worse error, so a leave only
  shortens the hold.

Velocity estimate: per new fix with spacing ``dt`` (ignored above 2 s), the instantaneous
velocity is clamped to ``max_speed_mps`` and smoothed with ``alpha = 1 - exp(-dt / tau)``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Mapping, Optional

from lynx.net.schema import LeaveReason, NodeLeave
from lynx.vision.types import TeammateTrack


@dataclass(frozen=True)
class HoldoverConfig:
    stale_after_s: float = 2.0
    dr_horizon_s: float = 3.0
    max_dr_m: float = 6.0
    max_speed_mps: float = 7.0
    velocity_tau_s: float = 0.6
    max_fix_gap_s: float = 2.0
    hold_s: float = 120.0
    leave_hold_s: float = 30.0
    stale_color: str = "white"
    label_age: bool = True


@dataclass
class _Memory:
    track: TeammateTrack
    vx: float = 0.0
    vy: float = 0.0
    left_at: Optional[float] = None


class FriendlyHoldover:
    """Call :meth:`update` once per frame with the relay snapshot's teammates."""

    def __init__(self, config: Optional[HoldoverConfig] = None,
                 link_down_s: Optional[Callable[[float], float]] = None) -> None:
        self.config = config or HoldoverConfig()
        self._mem: Dict[int, _Memory] = {}
        self._link_down_s = link_down_s
        self._down_since: Optional[float] = None
        self.held: List[int] = []
        self.oldest_age_s = 0.0

    # -- inputs ------------------------------------------------------------------------------

    def on_message(self, msg) -> None:
        """Relay message tap (``LynxClient.on_message``): shortens the hold after a leave."""
        if isinstance(msg, NodeLeave) and msg.reason == LeaveReason.DISCONNECT and msg.node in self._mem:
            self._mem[msg.node].left_at = time.monotonic()

    def _observe(self, tm: TeammateTrack) -> None:
        cfg = self.config
        m = self._mem.get(tm.node_id)
        if m is None:
            self._mem[tm.node_id] = _Memory(tm)
            return
        dt = tm.timestamp - m.track.timestamp
        if dt <= 1e-6:
            return
        if dt <= cfg.max_fix_gap_s:
            vx, vy = (tm.x - m.track.x) / dt, (tm.y - m.track.y) / dt
            speed = math.hypot(vx, vy)
            if speed > cfg.max_speed_mps:
                vx, vy = vx * cfg.max_speed_mps / speed, vy * cfg.max_speed_mps / speed
            a = 1.0 - math.exp(-dt / cfg.velocity_tau_s)
            m.vx += a * (vx - m.vx)
            m.vy += a * (vy - m.vy)
        else:
            m.vx = m.vy = 0.0
        m.track = tm
        m.left_at = None

    # -- output ------------------------------------------------------------------------------

    def dead_reckon(self, m: _Memory, age: float) -> tuple[float, float]:
        cfg = self.config
        dt = min(max(0.0, age), cfg.dr_horizon_s)
        dx, dy = m.vx * dt, m.vy * dt
        d = math.hypot(dx, dy)
        if d > cfg.max_dr_m:
            dx, dy = dx * cfg.max_dr_m / d, dy * cfg.max_dr_m / d
        return m.track.x + dx, m.track.y + dy

    def update(self, teammates: List[TeammateTrack], now: float, connected: bool = True) -> List[TeammateTrack]:
        cfg = self.config
        if connected:
            self._down_since = None
        elif self._down_since is None:
            self._down_since = now
        for tm in teammates:
            self._observe(tm)
        out: List[TeammateTrack] = []
        self.held = []
        self.oldest_age_s = 0.0
        for nid in sorted(self._mem):
            m = self._mem[nid]
            age = now - m.track.timestamp
            limit = cfg.leave_hold_s if m.left_at is not None else cfg.hold_s
            if age > limit:
                del self._mem[nid]
                continue
            if age <= cfg.stale_after_s:
                out.append(m.track)
                continue
            x, y = self.dead_reckon(m, age)
            label = f"{m.track.callsign} {age:.0f}s" if cfg.label_age else m.track.callsign
            out.append(replace(m.track, x=x, y=y, callsign=label, team_color=cfg.stale_color))
            self.held.append(nid)
            self.oldest_age_s = max(self.oldest_age_s, age)
        return out

    def link_down_for(self, now: float) -> float:
        if self._link_down_s is not None:
            return self._link_down_s(now)
        return 0.0 if self._down_since is None else now - self._down_since

    def forget(self, node_id: int) -> None:
        self._mem.pop(node_id, None)

    # -- HeadsetClient hook ------------------------------------------------------------------

    def teammates(self, teammates: List[TeammateTrack], nodes: Mapping, now: float,
                  connected: bool) -> List[TeammateTrack]:
        return self.update(teammates, now, connected)

    def annotate(self, client, pose, now: float, alerts: List[str], telemetry: Dict[str, str]) -> None:
        connected = client.net.connected if client is not None else True
        if not connected:
            down = self.link_down_for(now)
            alerts[:] = [a for a in alerts if a != "! RELAY LINK DOWN"]
            alerts.insert(0, f"! LINK DOWN {down:.0f}s - {len(self.held)} FRIENDLIES HELD")
        if self.held:
            telemetry["HOLD"] = f"{len(self.held)} STALE  OLDEST {self.oldest_age_s:.0f}s  (DR <= {self.config.dr_horizon_s:.0f}s)"
