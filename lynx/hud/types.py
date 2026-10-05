"""HUD input types. Relay messages map onto these in :mod:`lynx.headset.adapters`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from lynx.vision.types import CameraModel, ExpectedTeammate, IffTrack, OperatorPose, TeammateTrack


@dataclass(frozen=True)
class WorldPing:
    """A shared 3D ping in the world frame (from the relay)."""

    ping_id: int
    x: float
    y: float
    z: float
    label: str = "PING"
    owner: str = ""
    color: Optional[Tuple[int, int, int]] = None


@dataclass(frozen=True)
class ScreenPing:
    """A ping already projected to pixels by the caller (e.g. ``lynx.spatial``).

    Pixels outside the frame, or ``behind=True``, render as an edge arrow
    pointing from screen centre toward ``(u, v)`` (for ``behind`` targets
    pass the camera-frame lateral direction scaled to pixels).
    """

    u: float
    v: float
    label: str = "PING"
    range_m: Optional[float] = None
    behind: bool = False
    color: Optional[Tuple[int, int, int]] = None


@dataclass(frozen=True)
class RenderedPing:
    """Where the renderer drew a ping: a chevron at ``(u, v)`` or an edge arrow anchored there.

    ``angle_rad`` is the arrow direction in image coordinates (``atan2(dv, du)``, v down).
    """

    ping_id: Optional[int]
    label: str
    u: float
    v: float
    on_screen: bool
    angle_rad: float
    range_m: Optional[float]
    color: Optional[Tuple[int, int, int]] = None


@dataclass
class HudState:
    pose: OperatorPose
    camera: CameraModel
    tracks: Sequence[IffTrack] = ()
    expected: Sequence[ExpectedTeammate] = ()
    teammates: Sequence[TeammateTrack] = ()
    pings: Sequence[WorldPing] = ()
    screen_pings: Sequence[ScreenPing] = ()
    telemetry: Dict[str, str] = field(default_factory=dict)
    aux_frame: Optional[np.ndarray] = None
    aux_label: str = "AUX"
    mode: str = "DAY"
    alerts: List[str] = field(default_factory=list)
