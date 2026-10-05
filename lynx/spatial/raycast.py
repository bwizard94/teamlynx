"""Forward raycasting used to place world pings.

A ping is generated when the operator presses the rail switch: we cast a ray from the eye
(camera centre) along the line of sight and take the first intersection with the ground plane
z = ground_z. If the ray does not hit the ground within ``max_range`` (looking level or up, or
the hit is too far to be meaningful) the ping is placed at ``fallback_range`` along the ray.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .frames import CameraMount, CameraPose, Pose

DEFAULT_MAX_RANGE = 150.0
DEFAULT_FALLBACK_RANGE = 50.0
_PARALLEL_EPS = 1e-9


class HitKind(enum.Enum):
    GROUND = "ground"
    MAX_RANGE = "max_range"


@dataclass(frozen=True)
class RayHit:
    point: np.ndarray
    distance: float
    kind: HitKind


def intersect_ground(
    origin: np.ndarray, direction: np.ndarray, ground_z: float = 0.0
) -> Optional[float]:
    """Ray parameter t >= 0 where ``origin + t * direction`` meets the plane z = ground_z.

    Solves ``o_z + t d_z = ground_z``  =>  ``t = (ground_z - o_z) / d_z``. Returns ``None`` if the
    ray is parallel to the plane or the intersection lies behind the origin.
    """
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    if abs(d[2]) < _PARALLEL_EPS:
        return None
    t = (ground_z - o[2]) / d[2]
    if t < 0.0:
        return None
    return float(t)


def raycast(
    origin: np.ndarray,
    direction: np.ndarray,
    ground_z: float = 0.0,
    max_range: float = DEFAULT_MAX_RANGE,
    fallback_range: float = DEFAULT_FALLBACK_RANGE,
) -> RayHit:
    """Cast a ray against the ground plane with a max-range fallback.

    ``direction`` need not be normalised; distances are reported in metres along the unit ray.
    """
    if max_range <= 0.0 or fallback_range <= 0.0:
        raise ValueError("ranges must be positive")
    o = np.asarray(origin, dtype=float).reshape(3)
    d = np.asarray(direction, dtype=float).reshape(3)
    n = float(np.linalg.norm(d))
    if n < 1e-12 or not math.isfinite(n):
        raise ValueError("ray direction must be a finite non-zero vector")
    d = d / n
    t = intersect_ground(o, d, ground_z)
    if t is not None and t <= max_range:
        p = o + t * d
        p[2] = ground_z  # remove round-off so the ping sits exactly on the plane
        return RayHit(p, t, HitKind.GROUND)
    r = min(fallback_range, max_range)
    return RayHit(o + r * d, r, HitKind.MAX_RANGE)


def raycast_from_pose(
    pose: Pose,
    mount: Optional[CameraMount] = None,
    ground_z: float = 0.0,
    max_range: float = DEFAULT_MAX_RANGE,
    fallback_range: float = DEFAULT_FALLBACK_RANGE,
) -> RayHit:
    """Cast along the camera optical axis (= body forward for a boresighted mount).

    Using the optical axis rather than raw body forward guarantees that a ping lands under the
    centre reticle of the HUD, i.e. it re-projects exactly to (cx, cy) for the pinging operator.
    """
    cam = CameraPose.from_body_pose(pose, mount)
    return raycast(cam.C_w, cam.optical_axis_w, ground_z, max_range, fallback_range)
