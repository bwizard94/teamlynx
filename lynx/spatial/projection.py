"""Pinhole camera model, world->pixel projection and off-screen indicator placement.

Pixel coordinates are continuous: pixel (i, j) covers u in [i, i+1), v in [j, j+1), so the
image spans u in [0, W), v in [0, H) and the geometric image centre is (W/2, H/2).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from .frames import CameraMount, CameraPose, Pose
from .rotations import wrap_deg_360

DEFAULT_NEAR = 0.05  # metres; points closer than this along the optical axis are not projected


@dataclass(frozen=True)
class Intrinsics:
    """Ideal (undistorted) pinhole intrinsics.

    ::

        K = [[fx,  0, cx],
             [ 0, fy, cy],
             [ 0,  0,  1]]
    """

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def from_fov(
        cls,
        width: int,
        height: int,
        hfov_deg: float,
        vfov_deg: Optional[float] = None,
    ) -> "Intrinsics":
        """Build intrinsics from field of view and resolution.

        The edge of the image (u = 0 or u = W) lies at angle HFOV/2 from the optical axis, so
        ``tan(HFOV/2) = (W/2) / fx``  =>  ``fx = W / (2 tan(HFOV/2))`` and likewise for fy.
        If ``vfov_deg`` is omitted, square pixels are assumed (fy = fx), which implies
        ``VFOV = 2 atan((H/W) tan(HFOV/2))``.
        """
        if width <= 0 or height <= 0:
            raise ValueError("resolution must be positive")
        if not 0.0 < hfov_deg < 180.0:
            raise ValueError("HFOV must be in (0, 180) degrees")
        fx = width / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
        if vfov_deg is None:
            fy = fx
        else:
            if not 0.0 < vfov_deg < 180.0:
                raise ValueError("VFOV must be in (0, 180) degrees")
            fy = height / (2.0 * math.tan(math.radians(vfov_deg) / 2.0))
        return cls(width, height, fx, fy, width / 2.0, height / 2.0)

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]])

    @property
    def hfov_deg(self) -> float:
        return math.degrees(2.0 * math.atan(self.width / (2.0 * self.fx)))

    @property
    def vfov_deg(self) -> float:
        return math.degrees(2.0 * math.atan(self.height / (2.0 * self.fy)))

    def project_camera_point(self, p_c: np.ndarray) -> Tuple[float, float]:
        """Perspective division for a point with z > 0 in the camera frame."""
        x, y, z = (float(c) for c in p_c)
        if z <= 0.0:
            raise ValueError("point is not in front of the camera")
        return self.fx * x / z + self.cx, self.fy * y / z + self.cy

    def pixel_to_ray(self, u: float, v: float) -> np.ndarray:
        """Unit viewing ray (camera frame) through continuous pixel coordinate (u, v)."""
        d = np.array([(u - self.cx) / self.fx, (v - self.cy) / self.fy, 1.0])
        return d / np.linalg.norm(d)

    def contains(self, u: float, v: float, margin: float = 0.0) -> bool:
        return margin <= u < self.width - margin and margin <= v < self.height - margin


class Visibility(enum.Enum):
    ON_SCREEN = "on_screen"
    OFF_SCREEN = "off_screen"  # in front of the camera but outside the image rectangle
    BEHIND = "behind"  # at or behind the near plane


@dataclass(frozen=True)
class Projection:
    """Result of projecting a world point.

    ``u, v``: the pixel position to draw at. For ON_SCREEN this is the true projection. For
    OFF_SCREEN/BEHIND it is the edge-clamped indicator position on the inset image border.
    ``edge_angle_deg``: screen-space direction from the image centre to the target, measured
    clockwise from screen-up (0 = up, 90 = right, 180 = down, 270 = left). Rotate an
    up-pointing arrow glyph by this angle to point at the target. Defined in all cases.
    ``depth``: camera-frame z (metres along the optical axis, negative when behind).
    ``distance``: Euclidean range from the camera centre (metres).
    ``bearing_deg``: horizontal bearing of the target relative to the operator's heading,
    clockwise positive, in (-180, 180]; independent of pitch and roll (for compass tapes).
    """

    visibility: Visibility
    u: float
    v: float
    edge_angle_deg: float
    depth: float
    distance: float
    bearing_deg: float
    p_cam: Tuple[float, float, float]

    @property
    def on_screen(self) -> bool:
        return self.visibility is Visibility.ON_SCREEN


def clamp_direction_to_rect(
    cx: float,
    cy: float,
    dx: float,
    dy: float,
    width: float,
    height: float,
    margin: float,
) -> Tuple[float, float]:
    """Intersect the ray (cx, cy) + t (dx, dy), t >= 0, with the image border inset by ``margin``.

    The inset rectangle is [margin, W - margin] x [margin, H - margin]. For a centre inside that
    rectangle the exit parameter is ``t* = min_i (half-extent_i / |d_i|)`` over the non-zero
    components of d, measured from the centre to the relevant edge.
    """
    norm = math.hypot(dx, dy)
    if norm < 1e-12:
        dx, dy, norm = 0.0, 1.0, 1.0  # degenerate: point straight down
    dx /= norm
    dy /= norm
    candidates = []
    if dx > 1e-12:
        candidates.append((width - margin - cx) / dx)
    elif dx < -1e-12:
        candidates.append((margin - cx) / dx)
    if dy > 1e-12:
        candidates.append((height - margin - cy) / dy)
    elif dy < -1e-12:
        candidates.append((margin - cy) / dy)
    t = max(0.0, min(candidates))
    return cx + t * dx, cy + t * dy


def screen_angle_deg(dx: float, dy: float) -> float:
    """Clockwise-from-up angle of a screen-space vector (v axis points down)."""
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return 180.0
    return wrap_deg_360(math.degrees(math.atan2(dx, -dy)))


def compass_bearing_deg(origin_w: Sequence[float], target_w: Sequence[float]) -> float:
    """Absolute compass bearing from ``origin_w`` to ``target_w``: clockwise from North, [0, 360).

    ``atan2(dE, dN)``; returns 0 when the two points are vertically aligned.
    """
    de = float(target_w[0]) - float(origin_w[0])
    dn = float(target_w[1]) - float(origin_w[1])
    if math.hypot(de, dn) < 1e-9:
        return 0.0
    return wrap_deg_360(math.degrees(math.atan2(de, dn)))


def horizontal_bearing_deg(observer: Pose, target_w: np.ndarray) -> float:
    """Bearing of ``target_w`` relative to the observer heading, clockwise, in (-180, 180]."""
    d = np.asarray(target_w, dtype=float) - observer.position
    if math.hypot(d[0], d[1]) < 1e-9:
        return 0.0
    heading = observer.euler[0]
    rel = (compass_bearing_deg(observer.position, target_w) - heading) % 360.0
    return rel - 360.0 if rel > 180.0 else rel


def indicator_direction(
    intrinsics: Intrinsics, p_cam: Sequence[float], near: float = DEFAULT_NEAR, side_hint: float = 1.0
) -> Tuple[float, float]:
    """Screen-space direction ``(du, dv)`` from the image centre towards a camera-frame point.

    In front of the near plane this is ``(fx x, fy y)``, parallel to ``(u - cx, v - cy)`` but not
    divided by z. At or behind the near plane the horizontal component becomes
    ``sign(x) fx sqrt(x^2 + z^2)`` so the indicator lands on the side edge the operator should turn
    towards (see :meth:`Camera.project`). ``side_hint`` picks the side when ``x == 0``.
    """
    x, y, z = (float(c) for c in p_cam)
    if z > near:
        return intrinsics.fx * x, intrinsics.fy * y
    side = math.copysign(1.0, x) if abs(x) > 1e-9 else math.copysign(1.0, side_hint)
    du = side * intrinsics.fx * math.hypot(x, z)
    dv = intrinsics.fy * y
    if abs(du) < 1e-9 and abs(dv) < 1e-9:
        du = side
    return du, dv


class Camera:
    """A posed pinhole camera: intrinsics + body pose + (optional) mount extrinsics."""

    def __init__(
        self,
        intrinsics: Intrinsics,
        pose: Optional[Pose] = None,
        mount: Optional[CameraMount] = None,
        near: float = DEFAULT_NEAR,
    ) -> None:
        if near <= 0.0:
            raise ValueError("near plane must be positive")
        self.intrinsics = intrinsics
        self.mount = mount or CameraMount()
        self.near = near
        self._pose = Pose()
        self._cam = CameraPose.from_body_pose(self._pose, self.mount)
        if pose is not None:
            self.pose = pose

    @property
    def pose(self) -> Pose:
        return self._pose

    @pose.setter
    def pose(self, pose: Pose) -> None:
        self._pose = pose
        self._cam = CameraPose.from_body_pose(pose, self.mount)

    @property
    def camera_pose(self) -> CameraPose:
        return self._cam

    def world_to_camera(self, p_w: np.ndarray) -> np.ndarray:
        return self._cam.world_to_camera(p_w)

    def project(self, p_w: Sequence[float], margin: float = 0.0) -> Projection:
        """Project a world point, with behind-camera and off-screen handling.

        In-front points (z_c > near) project as ``u = fx x/z + cx, v = fy y/z + cy``. If the
        result lies outside the image (or the point is behind), the indicator is placed where
        the screen-space direction ``(fx x_c, fy y_c)`` from the image centre exits the
        margin-inset border. That direction equals ``(u - cx, v - cy) * z`` for z > 0 and is
        deliberately *not* divided by z, so it does not flip sign when the target passes
        behind the camera. For BEHIND targets the horizontal component is replaced by
        ``sign(x) fx sqrt(x^2 + z^2)`` so the indicator sits on the turn-towards side edge.
        """
        intr = self.intrinsics
        p_w = np.asarray(p_w, dtype=float)
        p_c = self._cam.world_to_camera(p_w)
        x, y, z = (float(c) for c in p_c)
        distance = float(np.linalg.norm(p_c))
        bearing = horizontal_bearing_deg(self._pose, p_w)

        if z > self.near:
            u, v = intr.fx * x / z + intr.cx, intr.fy * y / z + intr.cy
            if intr.contains(u, v, margin):
                return Projection(
                    Visibility.ON_SCREEN,
                    u,
                    v,
                    screen_angle_deg(u - intr.cx, v - intr.cy),
                    z,
                    distance,
                    bearing,
                    (x, y, z),
                )
            visibility = Visibility.OFF_SCREEN
        else:
            # Behind: the point is folded into the image-plane-parallel half space, keeping its
            # elevation but treating its horizontal offset as the full sqrt(x^2 + z^2) on the side
            # of x. That is the direction of a point at the same elevation 90 deg to that side, so
            # it equals (fx x, fy y) at z = 0 (continuous with OFF_SCREEN) and puts the indicator on
            # the edge the operator should turn towards instead of the bottom.
            visibility = Visibility.BEHIND
        dir_u, dir_v = indicator_direction(intr, (x, y, z), self.near, 1.0 if bearing >= 0.0 else -1.0)

        eu, ev = clamp_direction_to_rect(
            intr.cx, intr.cy, dir_u, dir_v, intr.width, intr.height, margin
        )
        return Projection(
            visibility, eu, ev, screen_angle_deg(dir_u, dir_v), z, distance, bearing, (x, y, z)
        )

    def project_segment(
        self, a_w: Sequence[float], b_w: Sequence[float]
    ) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Project a world line segment, clipping it against the near plane.

        Returns pixel endpoints (possibly outside the image; 2D raster clipping is left to the
        renderer) or ``None`` if the segment lies entirely behind the near plane. Clipping is
        performed in camera space *before* perspective division, which is required for
        correctness: projecting endpoints with z <= 0 would mirror them through the centre.
        """
        a = self._cam.world_to_camera(np.asarray(a_w, dtype=float))
        b = self._cam.world_to_camera(np.asarray(b_w, dtype=float))
        n = self.near
        if a[2] <= n and b[2] <= n:
            return None
        if a[2] <= n or b[2] <= n:
            t = (n - a[2]) / (b[2] - a[2])
            clip = a + t * (b - a)
            if a[2] <= n:
                a = clip
            else:
                b = clip
        intr = self.intrinsics
        return intr.project_camera_point(a), intr.project_camera_point(b)

    def pixel_to_world_ray(self, u: float, v: float) -> Tuple[np.ndarray, np.ndarray]:
        """Back-project a pixel to a world-frame ray ``(origin, unit_direction)``."""
        d_c = self.intrinsics.pixel_to_ray(u, v)
        return self._cam.C_w.copy(), self._cam.R_wc @ d_c

    def ground_polygon(self, ground_z: float = 0.0) -> list[Tuple[float, float]]:
        """Screen-space polygon covering the part of the image that sees the ground plane.

        For pixel (u, v) the camera ray is ``d_c = ((u-cx)/fx, (v-cy)/fy, 1)`` and its world
        vertical component ``d_z = r3 . d_c`` (r3 = third row of R_wc) is *affine* in (u, v).
        A ray hits the plane below the camera iff ``d_z < 0`` (for a camera above the plane),
        so the ground region is the image rectangle clipped by the half-plane ``d_z < 0``,
        bounded by the horizon (vanishing) line ``d_z = 0``.
        """
        intr = self.intrinsics
        r = self._cam.R_wc[2]
        above = self._cam.C_w[2] >= ground_z
        sign = 1.0 if above else -1.0

        def f(u: float, v: float) -> float:
            return sign * (r[0] * (u - intr.cx) / intr.fx + r[1] * (v - intr.cy) / intr.fy + r[2])

        rect = [(0.0, 0.0), (float(intr.width), 0.0), (float(intr.width), float(intr.height)), (0.0, float(intr.height))]
        out: list[Tuple[float, float]] = []
        for i, p in enumerate(rect):
            q = rect[(i + 1) % len(rect)]
            fp, fq = f(*p), f(*q)
            if fp < 0.0:
                out.append(p)
            if (fp < 0.0) != (fq < 0.0):
                t = fp / (fp - fq)
                out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
        return out
