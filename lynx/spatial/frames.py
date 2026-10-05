"""Coordinate frames and rigid poses.

Frames
------
W  World   local ENU tangent plane anchored at the staging-area datum. x = East, y = North,
           z = Up, metres. The ground is modelled as the plane z = 0 (datum height).
B  Body    FLU frame attached to the operator's head (IMU after boresight calibration).
           x = Forward (line of sight), y = Left, z = Up.
C  Camera  OpenCV optical frame: x = Right (image +u), y = Down (image +v),
           z = Forward (optical axis). Pinhole projection happens in this frame.

The fixed body->camera axis permutation for a boresighted camera is::

    x_c = -y_b,   y_c = -z_b,   z_c = x_b

    R_cb = [[ 0, -1,  0],
            [ 0,  0, -1],
            [ 1,  0,  0]]

A physical camera mount adds an extra rotation ``R_bm`` (mount misalignment, expressed in the
body frame) and a lever arm ``t_b`` (camera optical centre relative to the body origin). The full
world->camera transform is then::

    p_c = R_cb @ R_bm^T @ (R_wb^T @ (p_w - t_wb) - t_b)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .rotations import (
    euler_to_matrix,
    euler_to_quat,
    matrix_to_euler,
    quat_normalize,
    quat_to_matrix,
)

R_CB = np.array([[0.0, -1.0, 0.0], [0.0, 0.0, -1.0], [1.0, 0.0, 0.0]])
"""Rotation taking body-frame (FLU) coordinates to camera-frame (RDF) coordinates."""

R_BC = R_CB.T
"""Rotation taking camera-frame coordinates to body-frame coordinates."""

BODY_FORWARD = np.array([1.0, 0.0, 0.0])
BODY_LEFT = np.array([0.0, 1.0, 0.0])
BODY_UP = np.array([0.0, 0.0, 1.0])


@dataclass
class Pose:
    """Rigid 6-DoF pose of the body frame in the world frame.

    ``position`` is the body origin in world ENU metres, ``R_wb`` the body->world rotation.
    """

    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    R_wb: np.ndarray = field(default_factory=lambda: np.eye(3))

    def __post_init__(self) -> None:
        self.position = np.asarray(self.position, dtype=float).reshape(3)
        self.R_wb = np.asarray(self.R_wb, dtype=float).reshape(3, 3)

    @classmethod
    def from_euler(
        cls, x: float, y: float, z: float, heading_deg: float, pitch_deg: float, roll_deg: float
    ) -> "Pose":
        return cls(np.array([x, y, z], dtype=float), euler_to_matrix(heading_deg, pitch_deg, roll_deg))

    @classmethod
    def from_quat(cls, position: np.ndarray, q_wb: np.ndarray) -> "Pose":
        return cls(np.asarray(position, dtype=float), quat_to_matrix(q_wb))

    @property
    def euler(self) -> tuple[float, float, float]:
        """``(heading, pitch, roll)`` in degrees."""
        return matrix_to_euler(self.R_wb)

    @property
    def quat(self) -> np.ndarray:
        h, p, r = self.euler
        return quat_normalize(euler_to_quat(h, p, r))

    @property
    def forward(self) -> np.ndarray:
        """Unit line-of-sight vector in world coordinates (first column of ``R_wb``)."""
        return self.R_wb[:, 0].copy()

    @property
    def left(self) -> np.ndarray:
        return self.R_wb[:, 1].copy()

    @property
    def up(self) -> np.ndarray:
        return self.R_wb[:, 2].copy()

    def body_to_world(self, p_b: np.ndarray) -> np.ndarray:
        return self.R_wb @ np.asarray(p_b, dtype=float) + self.position

    def world_to_body(self, p_w: np.ndarray) -> np.ndarray:
        return self.R_wb.T @ (np.asarray(p_w, dtype=float) - self.position)


@dataclass
class CameraMount:
    """Extrinsics of the camera relative to the body frame.

    ``R_bm``: residual mount rotation (body frame), identity for a perfectly boresighted camera.
    ``t_b``:  optical centre in body coordinates (metres), e.g. (0.08, 0, 0.05) for a camera
              8 cm in front of and 5 cm above the head origin.
    """

    R_bm: np.ndarray = field(default_factory=lambda: np.eye(3))
    t_b: np.ndarray = field(default_factory=lambda: np.zeros(3))

    def __post_init__(self) -> None:
        self.R_bm = np.asarray(self.R_bm, dtype=float).reshape(3, 3)
        self.t_b = np.asarray(self.t_b, dtype=float).reshape(3)


@dataclass
class CameraPose:
    """World->camera rigid transform ``p_c = R_cw @ (p_w - C_w)``."""

    R_cw: np.ndarray
    C_w: np.ndarray

    @classmethod
    def from_body_pose(cls, pose: Pose, mount: Optional[CameraMount] = None) -> "CameraPose":
        mount = mount or CameraMount()
        R_wc = pose.R_wb @ mount.R_bm @ R_BC
        C_w = pose.body_to_world(mount.t_b)
        return cls(R_cw=R_wc.T, C_w=C_w)

    @property
    def R_wc(self) -> np.ndarray:
        return self.R_cw.T

    @property
    def optical_axis_w(self) -> np.ndarray:
        return self.R_wc[:, 2].copy()

    def world_to_camera(self, p_w: np.ndarray) -> np.ndarray:
        """Accepts a single point (3,) or an array of points (N, 3)."""
        p_w = np.asarray(p_w, dtype=float)
        return (p_w - self.C_w) @ self.R_cw.T

    def camera_to_world(self, p_c: np.ndarray) -> np.ndarray:
        p_c = np.asarray(p_c, dtype=float)
        return p_c @ self.R_cw + self.C_w
