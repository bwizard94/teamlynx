"""Pinhole projection between the ENU world frame and the headset camera.

Conventions (see ``lynx.vision.types``):
    world  : ENU, +X east, +Y north, +Z up.
    body   : forward f, right r, up u unit vectors expressed in world.
    camera : OpenCV optical frame, +x right, +y down, +z forward.

With heading psi (clockwise from north), pitch theta and roll phi:

    f  = ( sin psi cos theta,  cos psi cos theta,  sin theta)
    r0 = ( cos psi,           -sin psi,            0        )
    u0 = (-sin psi sin theta, -cos psi sin theta,  cos theta)
    r  =  r0 cos phi - u0 sin phi        (roll right => right side down)
    u  =  u0 cos phi + r0 sin phi

For a world point p seen from camera centre o, d = p - o and

    x_c = d . r,   y_c = -(d . u),   z_c = d . f
    u_px = c_x + f_x x_c / z_c,   v_px = c_y + f_y y_c / z_c

which is the standard pinhole model with f_x = (W/2) / tan(HFOV/2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from lynx.vision.types import CameraModel, OperatorPose


def rotation_world_to_camera(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """3x3 matrix R such that p_cam = R @ (p_world - o)."""
    psi, theta, phi = (math.radians(a) for a in (yaw_deg, pitch_deg, roll_deg))
    sp, cp = math.sin(psi), math.cos(psi)
    st, ct = math.sin(theta), math.cos(theta)
    sr, cr = math.sin(phi), math.cos(phi)
    f = np.array([sp * ct, cp * ct, st])
    r0 = np.array([cp, -sp, 0.0])
    u0 = np.array([-sp * st, -cp * st, ct])
    r = r0 * cr - u0 * sr
    u = u0 * cr + r0 * sr
    return np.stack([r, -u, f])


@dataclass(frozen=True)
class Projection:
    cam_xyz: Tuple[float, float, float]
    range_m: float
    azimuth_deg: float  # relative to boresight, + right
    elevation_deg: float  # relative to boresight, + up
    pixel: Optional[Tuple[float, float]]  # None when behind the image plane
    in_front: bool
    in_view: bool


def world_to_camera(pose: OperatorPose, point: Sequence[float]) -> np.ndarray:
    R = rotation_world_to_camera(pose.yaw, pose.pitch, pose.roll)
    d = np.asarray(point, dtype=np.float64) - np.asarray(pose.position, dtype=np.float64)
    return R @ d


def project_point(
    pose: OperatorPose,
    camera: CameraModel,
    point: Sequence[float],
    min_depth: float = 0.05,
) -> Projection:
    xc, yc, zc = (float(v) for v in world_to_camera(pose, point))
    rng = math.sqrt(xc * xc + yc * yc + zc * zc)
    az = math.degrees(math.atan2(xc, zc))
    el = math.degrees(math.atan2(-yc, math.hypot(xc, zc)))
    in_front = zc > min_depth
    pixel = None
    in_view = False
    if in_front:
        u = camera.cx + camera.fx * xc / zc
        v = camera.cy + camera.fy * yc / zc
        pixel = (u, v)
        in_view = 0.0 <= u < camera.width and 0.0 <= v < camera.height
    return Projection((xc, yc, zc), rng, az, el, pixel, in_front, in_view)


def pixel_to_angles(camera: CameraModel, u: float, v: float) -> Tuple[float, float]:
    """Boresight-relative (azimuth, elevation) in degrees of a pixel ray."""
    x = (u - camera.cx) / camera.fx
    y = (v - camera.cy) / camera.fy
    az = math.degrees(math.atan2(x, 1.0))
    el = math.degrees(math.atan2(-y, math.hypot(x, 1.0)))
    return az, el


def world_bearing_deg(pose: OperatorPose, point: Sequence[float]) -> float:
    """Absolute compass bearing (0..360, clockwise from north) to a point."""
    dx = point[0] - pose.x
    dy = point[1] - pose.y
    return math.degrees(math.atan2(dx, dy)) % 360.0


def wrap_deg(a: float) -> float:
    """Wrap an angle to [-180, 180)."""
    return (a + 180.0) % 360.0 - 180.0


def edge_arrow_position(
    camera: CameraModel,
    cam_xyz: Sequence[float],
    margin: float = 40.0,
) -> Tuple[Tuple[float, float], float]:
    """Screen-border anchor and angle (radians, image coords) for an off-screen target.

    Uses the camera-frame lateral components (x_c, y_c) as the turn
    direction, which stays correct for targets behind the operator where
    the perspective divide would mirror the pixel.
    """
    xc, yc, zc = (float(v) for v in cam_xyz)
    dx, dy = xc * camera.fx, yc * camera.fy
    if zc > 0.0:
        dx, dy = dx / max(zc, 1e-6), dy / max(zc, 1e-6)
    if math.hypot(dx, dy) < 1e-9:
        dx, dy = 1.0, 0.0  # directly behind: cue a right turn
    angle = math.atan2(dy, dx)
    hw = camera.width / 2.0 - margin
    hh = camera.height / 2.0 - margin
    c, s = math.cos(angle), math.sin(angle)
    tx = hw / abs(c) if abs(c) > 1e-9 else float("inf")
    ty = hh / abs(s) if abs(s) > 1e-9 else float("inf")
    t = min(tx, ty)
    return (camera.cx + c * t, camera.cy + s * t), angle
