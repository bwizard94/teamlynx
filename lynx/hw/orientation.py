"""BNO085 quaternion -> TeamLynx head attitude (heading / pitch / roll, ``q_wb``).

Frames (see ``docs/spatial-math.md`` and ``docs/hardware/imu-calibration.md``):

* ``S``  sensor frame: the BNO085's x/y/z axes as printed on the breakout.
* ``M``  sensor world: ENU in which the BNO085 reports. Rotation Vector: y = *magnetic* North.
  Game Rotation Vector: y = arbitrary (wherever the sensor faced at power-up).
* ``W``  TeamLynx world: ENU from the staging datum, y = grid North (Phase 1 convention).
* ``B``  head body frame: FLU (x Forward, y Left, z Up).

The sensor reports ``q_ms`` (active rotation, ``v_m = q_ms * v_s * q_ms^*``). The head attitude is

    q_wb = q_z(-c) * q_ms * q_sb * q_t

* ``q_sb``  fixed mounting rotation, ``R_sb = R_bs^T``; the columns of ``R_bs`` are the sensor
  axes expressed in the body frame (``mount="FLU"`` means sensor x->Forward, y->Left, z->Up).
* ``q_t``   small boresight trim in the body frame (see :func:`trim_matrix`).
* ``c``     total heading correction in degrees: ``declination - convergence + heading_offset``.
  A rotation of the world about Up by ``-c`` adds ``c`` to the compass heading and leaves pitch
  and roll unchanged, because ``R_z(-c) R_z(90 - h) = R_z(90 - (h + c))``.

``declination`` (east positive) turns magnetic North into true North, ``convergence`` (grid
convergence, east positive) turns true North into grid North, and ``heading_offset`` is the tare
taken at the staging datum. With the Game Rotation Vector only ``heading_offset`` is meaningful.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence, Tuple, Union

import numpy as np

from lynx.spatial.rotations import (
    matrix_to_euler,
    matrix_to_quat,
    quat_multiply,
    quat_normalize,
    quat_to_matrix,
    rot_x,
    rot_y,
    rot_z,
    wrap_deg_180,
)

BODY_AXES = {
    "F": np.array([1.0, 0.0, 0.0]),
    "B": np.array([-1.0, 0.0, 0.0]),
    "L": np.array([0.0, 1.0, 0.0]),
    "R": np.array([0.0, -1.0, 0.0]),
    "U": np.array([0.0, 0.0, 1.0]),
    "D": np.array([0.0, 0.0, -1.0]),
}

MOUNT_PRESETS = {
    # Breakout flat on top of the helmet, component side up, x arrow forward.
    "top-flat": "FLU",
    # Breakout flat on top, x arrow pointing to the right ear (rotated 90 deg CW seen from above).
    "top-flat-x-right": "RFU",
    # Breakout vertical on the LEFT side of the helmet / NVG shroud, component side facing out
    # (z -> Left), x arrow forward, so y = z cross x points Down.
    "left-side": "FDL",
    # Breakout vertical on the RIGHT side, component side facing out (z -> Right), x forward.
    "right-side": "FUR",
    # Breakout vertical on the back of the helmet, component side facing aft (z -> Back),
    # x arrow to the right ear, so y = z cross x points Up.
    "rear": "RUB",
}


class MountError(ValueError):
    pass


class TareError(RuntimeError):
    pass


def mount_matrix(spec: str) -> np.ndarray:
    """``R_bs`` from a 3-letter axis spec or a preset name.

    Letter *i* says where sensor axis *i* (x, y, z) points in the head frame: F/B forward/back,
    L/R left/right, U/D up/down. The result must be a proper rotation (right-handed).
    """
    key = MOUNT_PRESETS.get(spec, spec).upper()
    if len(key) != 3 or any(c not in BODY_AXES for c in key):
        raise MountError(f"mount spec {spec!r}: expected 3 letters from FBLRUD or one of {sorted(MOUNT_PRESETS)}")
    R_bs = np.column_stack([BODY_AXES[c] for c in key])
    if abs(abs(np.linalg.det(R_bs)) - 1.0) > 1e-9:
        raise MountError(f"mount spec {spec!r}: two sensor axes point the same way")
    if np.linalg.det(R_bs) < 0.0:
        raise MountError(f"mount spec {spec!r} is left-handed; flip one axis (z = x cross y)")
    return R_bs


def trim_matrix(yaw_right_deg: float, pitch_up_deg: float, roll_right_deg: float) -> np.ndarray:
    """Boresight trim ``R_t`` applied in the body frame: ``R_wb = R_wb_measured @ R_t``.

    For a level head, a trim of (+dh, +dp, +dr) raises the reported heading, pitch and roll by
    exactly dh, dp, dr (same signs as the Phase 1 heading/pitch/roll convention).
    """
    return (rot_z(math.radians(-yaw_right_deg)) @ rot_y(math.radians(-pitch_up_deg))
            @ rot_x(math.radians(roll_right_deg)))


@dataclass
class ImuCalibration:
    """Everything needed to turn sensor quaternions into Phase 1 head attitude. JSON-serialisable."""

    mount: str = "top-flat"
    trim_deg: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    declination_deg: float = 0.0
    convergence_deg: float = 0.0
    heading_offset_deg: float = 0.0
    tared: bool = False

    def heading_correction_deg(self) -> float:
        return self.declination_deg - self.convergence_deg + self.heading_offset_deg

    def to_json(self) -> str:
        d = asdict(self)
        d["trim_deg"] = list(self.trim_deg)
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ImuCalibration":
        d = json.loads(text)
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown calibration fields: {sorted(unknown)}")
        if "trim_deg" in d:
            d["trim_deg"] = tuple(float(v) for v in d["trim_deg"])
        cal = cls(**d)
        mount_matrix(cal.mount)
        return cal

    def save(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json() + "\n")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ImuCalibration":
        return cls.from_json(Path(path).read_text())


@dataclass
class OrientationConverter:
    """Applies :class:`ImuCalibration` to raw sensor quaternions."""

    cal: ImuCalibration = field(default_factory=ImuCalibration)

    def __post_init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Recompute cached rotations after editing :attr:`cal`."""
        R_bs = mount_matrix(self.cal.mount)
        self._q_sb_t = matrix_to_quat(R_bs.T @ trim_matrix(*self.cal.trim_deg))
        self._q_c = self._yaw_quat(self.cal.heading_correction_deg())
        self._q_c_untared = self._yaw_quat(self.cal.declination_deg - self.cal.convergence_deg)

    @staticmethod
    def _yaw_quat(heading_correction_deg: float) -> np.ndarray:
        half = 0.5 * math.radians(-heading_correction_deg)
        return np.array([math.cos(half), 0.0, 0.0, math.sin(half)])

    def body_quat(self, q_ms: Sequence[float]) -> np.ndarray:
        """``q_wb`` (scalar-first, w >= 0) for a sensor quaternion ``q_ms = (w, x, y, z)``."""
        q = quat_normalize(np.asarray(q_ms, dtype=float))
        return quat_normalize(quat_multiply(quat_multiply(self._q_c, q), self._q_sb_t))

    def _untared_body_quat(self, q_ms: Sequence[float]) -> np.ndarray:
        q = quat_normalize(np.asarray(q_ms, dtype=float))
        return quat_normalize(quat_multiply(quat_multiply(self._q_c_untared, q), self._q_sb_t))

    def euler(self, q_ms: Sequence[float]) -> Tuple[float, float, float]:
        """``(heading, pitch, roll)`` in degrees, Phase 1 convention."""
        return matrix_to_euler(quat_to_matrix(self.body_quat(q_ms)))

    def tare(self, samples: Iterable[Sequence[float]], datum_bearing_deg: float,
             max_pitch_deg: float = 45.0, max_spread_deg: float = 2.0) -> float:
        """Set :attr:`ImuCalibration.heading_offset_deg` so the head reads ``datum_bearing_deg``.

        ``samples`` are raw sensor quaternions collected while the operator holds still facing the
        datum reference. Heading is the circular mean of the samples' line-of-sight azimuth; it is
        rejected if the head is pitched too steeply (heading ill-conditioned) or was moving.
        Returns the new offset in degrees.
        """
        sx = sy = 0.0
        n = 0
        for q in samples:
            f = quat_to_matrix(self._untared_body_quat(q))[:, 0]
            pitch = math.degrees(math.asin(max(-1.0, min(1.0, f[2]))))
            if abs(pitch) > max_pitch_deg:
                raise TareError(f"head pitched {pitch:.0f} deg; tare needs |pitch| <= {max_pitch_deg:.0f}")
            az = math.atan2(f[0], f[1])
            sx += math.sin(az)
            sy += math.cos(az)
            n += 1
        if n == 0:
            raise TareError("no IMU samples to tare with")
        r = math.hypot(sx, sy) / n
        spread = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(r, 1e-12)))))
        if spread > max_spread_deg:
            raise TareError(f"head moved during tare (circular std {spread:.1f} deg > {max_spread_deg} deg)")
        measured = math.degrees(math.atan2(sx, sy))
        self.cal.heading_offset_deg = wrap_deg_180(datum_bearing_deg - measured)
        self.cal.tared = True
        self.refresh()
        return self.cal.heading_offset_deg

    def clear_tare(self) -> None:
        self.cal.heading_offset_deg = 0.0
        self.cal.tared = False
        self.refresh()


def sensor_quat_for_body(q_wb: Sequence[float], cal: ImuCalibration,
                         sensor_heading_error_deg: float = 0.0) -> np.ndarray:
    """Inverse model: the ``q_ms`` a BNO085 mounted per ``cal`` would report for head attitude
    ``q_wb``. ``sensor_heading_error_deg`` adds an unknown heading bias (what tare removes).

    Used by the mock device and the tests. Ignores ``heading_offset_deg`` (a host-side quantity).
    """
    R_bs = mount_matrix(cal.mount)
    R_t = trim_matrix(*cal.trim_deg)
    c = cal.declination_deg - cal.convergence_deg - sensor_heading_error_deg
    R_ms = rot_z(math.radians(c)) @ quat_to_matrix(q_wb) @ R_t.T @ R_bs
    return matrix_to_quat(R_ms)
