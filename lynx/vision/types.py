"""Self-contained data types for the vision / HUD pipeline.

These mirror the Phase 1 telemetry schema (``lynx.net`` / ``lynx.spatial``)
without importing it, so the vision stack can be developed and tested in
isolation. Wiring them in later is a field-for-field copy; see
``docs/vision-pipeline.md`` ("Interface contract").

World frame: local ENU tangent plane anchored at the staging-area datum.
    +X = east, +Y = north, +Z = up, metres.
Attitude (degrees, intrinsic Z-X'-Y'' as flown):
    yaw   = compass heading, 0 = north, positive clockwise (toward east)
    pitch = nose up positive
    roll  = right side down positive
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


@dataclass(frozen=True)
class OperatorPose:
    """6-DoF pose of the local headset camera in the shared world frame."""

    x: float
    y: float
    z: float
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    node_id: int = 0
    callsign: str = "LYNX-0"
    team_color: str = "green"

    @property
    def position(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class TeammateTrack:
    """Latest telemetry for one teammate (headset position, world frame).

    ``timestamp`` is seconds on the same monotonic clock passed to the IFF
    associator's ``update(now=...)``; stale entries are ignored for Friendly
    assignment but still suppress Tango classification near their last
    known bearing.
    """

    node_id: int
    callsign: str
    x: float
    y: float
    z: float
    team_color: str = "green"
    timestamp: float = 0.0
    yaw: float = 0.0

    @property
    def position(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class CameraModel:
    """Ideal pinhole camera. VFOV is derived from HFOV and aspect if omitted."""

    width: int = 1280
    height: int = 720
    hfov_deg: float = 78.0
    vfov_deg: Optional[float] = None

    @property
    def fx(self) -> float:
        return (self.width / 2.0) / math.tan(math.radians(self.hfov_deg) / 2.0)

    @property
    def fy(self) -> float:
        if self.vfov_deg is None:
            return self.fx
        return (self.height / 2.0) / math.tan(math.radians(self.vfov_deg) / 2.0)

    @property
    def cx(self) -> float:
        return self.width / 2.0

    @property
    def cy(self) -> float:
        return self.height / 2.0

    @property
    def vfov(self) -> float:
        return math.degrees(2.0 * math.atan((self.height / 2.0) / self.fy))

    def resized(self, width: int, height: int) -> "CameraModel":
        return CameraModel(width, height, self.hfov_deg, self.vfov_deg)


@dataclass(frozen=True)
class Detection:
    """One detector output in pixel coordinates (x1, y1, x2, y2)."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    category: str  # "person" | "vehicle"
    class_name: str = ""

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def as_xyxy(self) -> Tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


class IffStatus(str, Enum):
    FRIENDLY = "FRIENDLY"
    UNVERIFIED = "UNVERIFIED"
    TANGO = "TANGO"


@dataclass
class IffTrack:
    """A tracked contact as published to the HUD."""

    track_id: int
    bbox: Tuple[float, float, float, float]
    category: str
    status: IffStatus
    confidence: float
    node_id: Optional[int] = None
    callsign: Optional[str] = None
    team_color: Optional[str] = None
    range_m: Optional[float] = None
    friendly_score: float = 0.0
    age: int = 0
    misses: int = 0


@dataclass
class ExpectedTeammate:
    """Where a teammate *should* appear in the current camera view."""

    node_id: int
    callsign: str
    team_color: str
    range_m: float
    azimuth_deg: float  # relative to boresight, + right
    elevation_deg: float  # relative to boresight, + up
    pixel: Optional[Tuple[float, float]]  # None if behind the camera
    in_view: bool
    expected_height_px: float
    stale: bool = False
    world_bearing_deg: float = 0.0
    extras: dict = field(default_factory=dict)
