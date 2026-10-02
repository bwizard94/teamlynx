"""Vision-side view of the shared camera model.

Every transform here delegates to :mod:`lynx.spatial` (frames, Euler convention, pinhole
intrinsics, off-screen indicator direction), so the IFF associator, the synthetic scene, the HUD
and the Phase 1 testbench all project through one implementation. This module only adds the
quantities the vision stack needs on top: boresight-relative azimuth / elevation and the
"unclamped" pixel of an in-front point.

Conventions (``docs/spatial-math.md``):
    world  : ENU, +X east, +Y north, +Z up.
    camera : OpenCV optical frame, +x right, +y down, +z forward.

For a world point p, ``p_c = R_cw (p - C_w)`` with ``R_cw = R_CB R_wb^T`` and

    u = c_x + f_x x_c / z_c,   v = c_y + f_y y_c / z_c,   f_x = (W/2) / tan(HFOV/2)

    azimuth   = atan2(x_c, z_c)                 (+ right of boresight)
    elevation = atan2(-y_c, sqrt(x_c^2 + z_c^2)) (+ above boresight)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from lynx.spatial import (
    DEFAULT_NEAR,
    R_CB,
    clamp_direction_to_rect,
    compass_bearing_deg,
    euler_to_matrix,
    indicator_direction,
    wrap_deg_180,
)
from lynx.vision.types import CameraModel, OperatorPose


def rotation_world_to_camera(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """3x3 matrix ``R_cw = R_CB @ R_wb^T`` such that ``p_cam = R_cw @ (p_world - o)``."""
    return R_CB @ euler_to_matrix(yaw_deg, pitch_deg, roll_deg).T


@dataclass(frozen=True)
class Projection:
    cam_xyz: Tuple[float, float, float]
    range_m: float
    azimuth_deg: float  # relative to boresight, + right
    elevation_deg: float  # relative to boresight, + up
    pixel: Optional[Tuple[float, float]]  # None when at/behind the near plane
    in_front: bool
    in_view: bool


def world_to_camera(pose: OperatorPose, point: Sequence[float]) -> np.ndarray:
    return pose.camera_pose.world_to_camera(np.asarray(point, dtype=np.float64))


def project_point(
    pose: OperatorPose,
    camera: CameraModel,
    point: Sequence[float],
    min_depth: float = DEFAULT_NEAR,
) -> Projection:
    p_c = world_to_camera(pose, point)
    xc, yc, zc = (float(v) for v in p_c)
    rng = math.sqrt(xc * xc + yc * yc + zc * zc)
    az = math.degrees(math.atan2(xc, zc))
    el = math.degrees(math.atan2(-yc, math.hypot(xc, zc)))
    in_front = zc > min_depth
    pixel = None
    in_view = False
    if in_front:
        intr = camera.intrinsics
        pixel = intr.project_camera_point(p_c)
        in_view = intr.contains(*pixel)
    return Projection((xc, yc, zc), rng, az, el, pixel, in_front, in_view)


def pixel_to_angles(camera: CameraModel, u: float, v: float) -> Tuple[float, float]:
    """Boresight-relative (azimuth, elevation) in degrees of a pixel ray."""
    x, y, z = camera.intrinsics.pixel_to_ray(u, v)
    return math.degrees(math.atan2(x, z)), math.degrees(math.atan2(-y, math.hypot(x, z)))


def world_bearing_deg(pose: OperatorPose, point: Sequence[float]) -> float:
    """Absolute compass bearing (0..360, clockwise from north) to a point."""
    return compass_bearing_deg(pose.position, point)


def wrap_deg(a: float) -> float:
    """Wrap an angle to [-180, 180)."""
    w = wrap_deg_180(a)
    return -180.0 if w == 180.0 else w


def edge_arrow_position(
    camera: CameraModel,
    cam_xyz: Sequence[float],
    margin: float = 40.0,
) -> Tuple[Tuple[float, float], float]:
    """Screen-border anchor and angle (radians, image coords) for an off-screen target.

    The direction is :func:`lynx.spatial.indicator_direction`, the same rule
    :meth:`lynx.spatial.Camera.project` uses, so targets behind the operator land on the side edge
    to turn towards instead of being mirrored by the perspective divide.
    """
    intr = camera.intrinsics
    du, dv = indicator_direction(intr, cam_xyz)
    anchor = clamp_direction_to_rect(intr.cx, intr.cy, du, dv, intr.width, intr.height, margin)
    return anchor, math.atan2(dv, du)
