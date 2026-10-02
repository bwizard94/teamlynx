"""Rotation primitives: elementary rotations, quaternions and TeamLynx Euler angles.

Conventions (see docs/spatial-math.md for the full derivation):

* World frame W: local ENU (x = East, y = North, z = Up), origin at the staging datum.
* Body frame B: FLU (x = Forward, y = Left, z = Up), rigidly attached to the operator's head.
* ``R_wb`` maps body-frame coordinates to world-frame coordinates: ``v_w = R_wb @ v_b``.
  Its columns are the body axes expressed in the world frame.
* Quaternions are Hamilton, scalar-first ``(w, x, y, z)``, unit-norm, and represent the same
  rotation as ``R_wb``: ``v_w = q * v_b * conj(q)``.

Operator-facing Euler angles (what goes on the wire and on the HUD), all in degrees:

* ``heading`` (a.k.a. yaw): compass heading, clockwise from grid North, in [0, 360).
* ``pitch``: positive nose-up, in [-90, 90].
* ``roll``: positive right-side-down (right ear towards the right shoulder), in (-180, 180].

These map onto the classic intrinsic Z-Y'-X'' (yaw-pitch-roll) sequence about the FLU body
axes as::

    R_wb = Rz(psi) @ Ry(beta) @ Rx(phi)
    psi  = pi/2 - heading      (math yaw, counter-clockwise from East)
    beta = -pitch              (right-hand rotation about +y_left is nose-DOWN)
    phi  = roll                (right-hand rotation about +x_forward lifts the left side)
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

Quat = np.ndarray  # shape (4,), (w, x, y, z)

_GIMBAL_EPS = 1e-9


def rot_x(angle_rad: float) -> np.ndarray:
    """Right-handed rotation about +x by ``angle_rad`` (active, column-vector convention)."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def rot_y(angle_rad: float) -> np.ndarray:
    """Right-handed rotation about +y by ``angle_rad``."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def rot_z(angle_rad: float) -> np.ndarray:
    """Right-handed rotation about +z by ``angle_rad``."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def wrap_deg_360(angle_deg: float) -> float:
    """Wrap an angle to [0, 360)."""
    a = math.fmod(angle_deg, 360.0)
    if a < 0.0:
        a += 360.0
    # fmod can return exactly 360.0 - tiny; guard the closed upper bound.
    return 0.0 if a >= 360.0 else a


def wrap_deg_180(angle_deg: float) -> float:
    """Wrap an angle to (-180, 180]."""
    a = wrap_deg_360(angle_deg)
    return a - 360.0 if a > 180.0 else a


# ---------------------------------------------------------------------------
# Euler (heading, pitch, roll) <-> rotation matrix
# ---------------------------------------------------------------------------


def euler_to_matrix(heading_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Body->world rotation ``R_wb`` from operator Euler angles (degrees)."""
    psi = math.radians(90.0 - heading_deg)
    beta = math.radians(-pitch_deg)
    phi = math.radians(roll_deg)
    return rot_z(psi) @ rot_y(beta) @ rot_x(phi)


def matrix_to_euler(R_wb: np.ndarray) -> Tuple[float, float, float]:
    """Inverse of :func:`euler_to_matrix`; returns ``(heading, pitch, roll)`` in degrees.

    With ``R = Rz(psi) Ry(beta) Rx(phi)``::

        R[2,0] = -sin(beta)
        R[1,0] =  cos(beta) sin(psi),   R[0,0] = cos(beta) cos(psi)
        R[2,1] =  cos(beta) sin(phi),   R[2,2] = cos(beta) cos(phi)

    At gimbal lock (|pitch| = 90 deg) heading and roll are not separable; roll is set to 0 and
    the combined angle is assigned to heading.
    """
    R = np.asarray(R_wb, dtype=float)
    s_beta = -float(R[2, 0])
    s_beta = max(-1.0, min(1.0, s_beta))
    beta = math.asin(s_beta)
    cb = math.cos(beta)
    if cb > _GIMBAL_EPS and math.hypot(R[0, 0], R[1, 0]) > _GIMBAL_EPS:
        psi = math.atan2(R[1, 0], R[0, 0])
        phi = math.atan2(R[2, 1], R[2, 2])
    else:
        # Gimbal lock: R = Rz(psi) Ry(+-90) Rx(phi). Only psi -+ phi is observable.
        # With phi := 0 the body y-axis (column 1) is (-sin psi, cos psi, 0).
        phi = 0.0
        psi = math.atan2(-R[0, 1], R[1, 1])
    heading = wrap_deg_360(90.0 - math.degrees(psi))
    pitch = -math.degrees(beta)
    roll = wrap_deg_180(math.degrees(phi))
    return heading, pitch, roll


# ---------------------------------------------------------------------------
# Quaternions (Hamilton, scalar-first)
# ---------------------------------------------------------------------------


def quat_normalize(q: np.ndarray) -> Quat:
    q = np.asarray(q, dtype=float)
    n = float(np.linalg.norm(q))
    if n < 1e-12:
        raise ValueError("cannot normalize a zero quaternion")
    q = q / n
    # Canonical hemisphere (w >= 0) so q and -q compare equal after normalisation.
    if q[0] < 0.0:
        q = -q
    return q


def quat_multiply(a: np.ndarray, b: np.ndarray) -> Quat:
    """Hamilton product ``a * b`` (apply ``b`` first, then ``a``)."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ]
    )


def quat_conjugate(q: np.ndarray) -> Quat:
    return np.array([q[0], -q[1], -q[2], -q[3]], dtype=float)


def quat_from_axis_angle(axis: np.ndarray, angle_rad: float) -> Quat:
    axis = np.asarray(axis, dtype=float)
    n = float(np.linalg.norm(axis))
    if n < 1e-12:
        raise ValueError("rotation axis must be non-zero")
    axis = axis / n
    h = 0.5 * angle_rad
    return np.concatenate(([math.cos(h)], math.sin(h) * axis))


def quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate vector ``v`` by unit quaternion ``q``: ``q * (0, v) * conj(q)``."""
    qv = np.concatenate(([0.0], np.asarray(v, dtype=float)))
    return quat_multiply(quat_multiply(q, qv), quat_conjugate(q))[1:]


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """Rotation matrix equivalent of unit quaternion ``q`` (same active rotation)."""
    w, x, y, z = quat_normalize(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_to_quat(R: np.ndarray) -> Quat:
    """Shepperd's method: numerically stable for all rotations."""
    R = np.asarray(R, dtype=float)
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0.0:
        s = 2.0 * math.sqrt(1.0 + tr)
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return quat_normalize(np.array([w, x, y, z]))


def euler_to_quat(heading_deg: float, pitch_deg: float, roll_deg: float) -> Quat:
    """``q_wb = qz(psi) * qy(beta) * qx(phi)``, the quaternion form of :func:`euler_to_matrix`."""
    psi = math.radians(90.0 - heading_deg)
    beta = math.radians(-pitch_deg)
    phi = math.radians(roll_deg)
    qz = quat_from_axis_angle(np.array([0.0, 0.0, 1.0]), psi)
    qy = quat_from_axis_angle(np.array([0.0, 1.0, 0.0]), beta)
    qx = quat_from_axis_angle(np.array([1.0, 0.0, 0.0]), phi)
    return quat_normalize(quat_multiply(quat_multiply(qz, qy), qx))


def quat_to_euler(q: np.ndarray) -> Tuple[float, float, float]:
    return matrix_to_euler(quat_to_matrix(q))


def quat_angle_between(a: np.ndarray, b: np.ndarray) -> float:
    """Smallest rotation angle (radians) taking ``a`` to ``b``."""
    d = abs(float(np.dot(quat_normalize(a), quat_normalize(b))))
    return 2.0 * math.acos(min(1.0, d))
