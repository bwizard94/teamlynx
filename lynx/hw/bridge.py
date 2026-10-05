"""Glue between the head tracker and a HUD/sim loop: head attitude + rail-switch actions.

Default gesture map (hands-free, one switch):

=========  ===============================================================
SINGLE     drop a ping of the currently selected type at the reticle
DOUBLE     drop a CONTACT ping (fast "enemy here" regardless of selection)
LONG       cancel my most recent ping
=========  ===============================================================

The ping is raycast with the head attitude at the instant of the first press
(:attr:`RailEvent.aim`), not when the gesture was classified.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from lynx.net.schema import PingType, TelemetryFlags
from lynx.spatial import RayHit, raycast_from_pose

from .protocol import ButtonEvent
from .serial_link import HeadPose, ImuLink, RailEvent


class RailAction(enum.Enum):
    PING = "ping"
    PING_CONTACT = "ping-contact"
    CANCEL_LAST = "cancel-last"


DEFAULT_GESTURES: Dict[ButtonEvent, RailAction] = {
    ButtonEvent.SINGLE: RailAction.PING,
    ButtonEvent.DOUBLE: RailAction.PING_CONTACT,
    ButtonEvent.LONG: RailAction.CANCEL_LAST,
}


@dataclass
class RailCommand:
    action: RailAction
    event: RailEvent
    aim: Optional[HeadPose]

    def ping_type(self, selected: PingType) -> PingType:
        return PingType.CONTACT if self.action is RailAction.PING_CONTACT else selected


def rail_commands(events: Sequence[RailEvent],
                  gestures: Mapping[ButtonEvent, RailAction] = DEFAULT_GESTURES) -> List[RailCommand]:
    return [RailCommand(gestures[e.event], e, e.aim) for e in events if e.event in gestures]


def raycast_ping(cmd: RailCommand, position: Sequence[float], fallback: Optional[HeadPose] = None,
                 max_range: float = 150.0, fallback_range: float = 50.0) -> Optional[RayHit]:
    """Ping hit point for a PING/PING_CONTACT command from the operator at ``position``."""
    aim = cmd.aim or fallback
    if aim is None:
        return None
    return raycast_from_pose(aim.to_pose(np.asarray(position, dtype=float)), max_range=max_range,
                             fallback_range=fallback_range)


class ImuHeadSource:
    """Polled once per frame by a render loop."""

    def __init__(self, link: ImuLink, gestures: Mapping[ButtonEvent, RailAction] = DEFAULT_GESTURES) -> None:
        self.link = link
        self.gestures = dict(gestures)
        self.held = False

    def poll(self) -> Tuple[Optional[HeadPose], List[RailCommand]]:
        events = self.link.poll_events()
        for e in events:
            if e.event is ButtonEvent.PRESS:
                self.held = True
            elif e.event is ButtonEvent.RELEASE:
                self.held = False
        pose = self.link.latest()
        if pose is not None and not self.link.health().usable:
            pose = None
        return pose, rail_commands(events, self.gestures)

    def telemetry_flags(self) -> int:
        flags = TelemetryFlags.NONE
        if self.held:
            flags |= TelemetryFlags.PING_SWITCH
        if self.link.health().degraded:
            flags |= TelemetryFlags.IMU_DEGRADED
        return int(flags)
