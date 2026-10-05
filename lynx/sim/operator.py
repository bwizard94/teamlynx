"""Kinematic operator model for the desktop testbench (stands in for IMU + positioning)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from lynx.spatial import Pose, wrap_deg_180, wrap_deg_360

DEFAULT_EYE_HEIGHT = 1.7


@dataclass
class ControlInput:
    """Normalised control axes in [-1, 1]."""

    forward: float = 0.0  # +forward / -back
    strafe: float = 0.0  # +right / -left
    turn: float = 0.0  # +clockwise (right)
    pitch: float = 0.0  # +nose up
    roll: float = 0.0  # +right side down
    climb: float = 0.0  # +up (eye height)
    sprint: bool = False


@dataclass
class SimOperator:
    """Body origin = the operator's eye, so ``z`` is eye height above the datum plane."""

    x: float = 0.0
    y: float = 0.0
    z: float = DEFAULT_EYE_HEIGHT
    heading: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    walk_speed: float = 2.5  # m/s
    sprint_speed: float = 6.0  # m/s
    turn_rate: float = 90.0  # deg/s
    pitch_rate: float = 45.0  # deg/s
    roll_rate: float = 45.0  # deg/s
    climb_rate: float = 1.0  # m/s
    pitch_limit: float = 85.0
    roll_limit: float = 60.0
    min_eye_height: float = 0.3
    max_eye_height: float = 30.0

    def step(self, dt: float, ctl: ControlInput) -> None:
        if dt <= 0.0:
            return
        self.heading = wrap_deg_360(self.heading + ctl.turn * self.turn_rate * dt)
        self.pitch = max(-self.pitch_limit, min(self.pitch_limit, self.pitch + ctl.pitch * self.pitch_rate * dt))
        self.roll = max(-self.roll_limit, min(self.roll_limit, wrap_deg_180(self.roll + ctl.roll * self.roll_rate * dt)))
        speed = self.sprint_speed if ctl.sprint else self.walk_speed
        h = math.radians(self.heading)
        # Horizontal unit vectors in ENU for compass heading h: forward = (sin h, cos h),
        # right = (cos h, -sin h). Walking ignores pitch/roll (you walk on the ground).
        fwd = np.array([math.sin(h), math.cos(h)])
        right = np.array([math.cos(h), -math.sin(h)])
        move = ctl.forward * fwd + ctl.strafe * right
        n = float(np.linalg.norm(move))
        if n > 1.0:
            move /= n
        self.x += float(move[0]) * speed * dt
        self.y += float(move[1]) * speed * dt
        self.z = max(self.min_eye_height, min(self.max_eye_height, self.z + ctl.climb * self.climb_rate * dt))

    @property
    def pose(self) -> Pose:
        return Pose.from_euler(self.x, self.y, self.z, self.heading, self.pitch, self.roll)

    def level(self) -> None:
        self.pitch = 0.0
        self.roll = 0.0
