"""Pluggable head-pose sources for the headset client.

A pose source owns the operator's 6-DoF pose and the ping trigger. The headset loop calls
:meth:`PoseSource.read` once per frame and never cares where the pose came from:

* ``keyboard``     discrete key steps (desktop stand-in for the IMU, default);
* ``static``       fixed pose, e.g. a tripod bench camera;
* ``serial:PORT``  BNO085 -> ESP32 -> USB serial quaternion stream. Provided by ``lynx.hw``,
                   which must call :func:`register_pose_source` ("serial", factory) at import time.
                   :func:`create_pose_source` imports ``lynx.hw`` lazily the first time a
                   ``serial:`` spec is requested.

Contract for implementers (e.g. the serial IMU source):

* ``read(now)`` must not block for longer than one frame; return the latest pose.
* ``PoseSample.pose`` is body (FLU, head) -> world (ENU) as :class:`lynx.spatial.Pose`. An
  orientation-only sensor keeps the position it was constructed with (``initial.position``,
  i.e. the datum-relative eye position).
* ``PoseSample.trigger`` is True exactly once per debounced rail-switch press (rising edge);
  the headset raycasts and publishes one ping per trigger.
* ``PoseSample.flags`` is OR-ed into the outgoing telemetry flags
  (:class:`lynx.net.schema.TelemetryFlags`: ``PING_SWITCH`` while held, ``IMU_DEGRADED`` when the
  sensor reports low calibration accuracy).
* ``handle_key(key)`` receives OpenCV ``waitKey`` codes; return True if consumed. Hardware sources
  normally return False.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Dict, Protocol, runtime_checkable

import numpy as np

from lynx.sim.operator import ControlInput, SimOperator
from lynx.spatial import Pose

KEY_SPACE = 32


@dataclass(frozen=True)
class PoseSample:
    pose: Pose
    trigger: bool = False
    flags: int = 0


@runtime_checkable
class PoseSource(Protocol):
    name: str

    def read(self, now: float) -> PoseSample: ...

    def handle_key(self, key: int) -> bool: ...

    def close(self) -> None: ...


class StaticPoseSource:
    name = "static"

    def __init__(self, pose: Pose) -> None:
        self.pose = pose

    def read(self, now: float) -> PoseSample:
        return PoseSample(self.pose)

    def handle_key(self, key: int) -> bool:
        return False

    def close(self) -> None:
        pass


class KeyboardPoseSource:
    """Discrete key steps on top of the testbench :class:`SimOperator` kinematics.

    OpenCV reports one key code per press (no key-up events), so each press is one step:

    ====== ===========================  ====== ==========================
    a / d  yaw -/+ ``yaw_step`` deg     i / k  forward / back ``move_step`` m
    w / s  pitch +/- ``pitch_step``     j / l  strafe left / right
    [ / ]  roll -/+ ``roll_step``       r      level pitch and roll
    space  ping trigger
    ====== ===========================  ====== ==========================
    """

    name = "keyboard"

    def __init__(
        self,
        initial: Pose,
        yaw_step: float = 5.0,
        pitch_step: float = 2.0,
        roll_step: float = 2.0,
        move_step: float = 1.0,
    ) -> None:
        heading, pitch, roll = initial.euler
        x, y, z = (float(c) for c in initial.position)
        self.operator = SimOperator(x=x, y=y, z=z, heading=heading, pitch=pitch, roll=roll)
        self.steps = {"yaw": yaw_step, "pitch": pitch_step, "roll": roll_step, "move": move_step}
        self._trigger = False

    def _nudge(self, axis: str, sign: float) -> None:
        op = self.operator
        if axis == "yaw":
            op.step(self.steps["yaw"] / op.turn_rate, ControlInput(turn=sign))
        elif axis == "pitch":
            op.step(self.steps["pitch"] / op.pitch_rate, ControlInput(pitch=sign))
        elif axis == "roll":
            op.step(self.steps["roll"] / op.roll_rate, ControlInput(roll=sign))
        elif axis == "forward":
            op.step(self.steps["move"] / op.walk_speed, ControlInput(forward=sign))
        elif axis == "strafe":
            op.step(self.steps["move"] / op.walk_speed, ControlInput(strafe=sign))

    _KEYMAP = {
        ord("a"): ("yaw", -1.0),
        ord("d"): ("yaw", 1.0),
        ord("w"): ("pitch", 1.0),
        ord("s"): ("pitch", -1.0),
        ord("["): ("roll", -1.0),
        ord("]"): ("roll", 1.0),
        ord("i"): ("forward", 1.0),
        ord("k"): ("forward", -1.0),
        ord("j"): ("strafe", -1.0),
        ord("l"): ("strafe", 1.0),
    }

    def handle_key(self, key: int) -> bool:
        if key in self._KEYMAP:
            self._nudge(*self._KEYMAP[key])
            return True
        if key == ord("r"):
            self.operator.level()
            return True
        if key == KEY_SPACE:
            self._trigger = True
            return True
        return False

    def press_trigger(self) -> None:
        self._trigger = True

    def read(self, now: float) -> PoseSample:
        trigger, self._trigger = self._trigger, False
        return PoseSample(self.operator.pose, trigger)

    def close(self) -> None:
        pass


PoseSourceFactory = Callable[[str, Pose], PoseSource]
"""``factory(argument, initial_pose)``; ``argument`` is the text after ``scheme:`` in the spec."""

_REGISTRY: Dict[str, PoseSourceFactory] = {}
_PLUGIN_MODULES: Dict[str, str] = {"serial": "lynx.hw"}


def register_pose_source(scheme: str, factory: PoseSourceFactory) -> None:
    _REGISTRY[scheme] = factory


def available_pose_sources() -> list[str]:
    return sorted(set(_REGISTRY) | set(_PLUGIN_MODULES))


class PoseSourceUnavailableError(RuntimeError):
    pass


def _parse_static(arg: str, initial: Pose) -> PoseSource:
    if not arg:
        return StaticPoseSource(initial)
    vals = [float(v) for v in arg.split(",")]
    if len(vals) != 6:
        raise ValueError("static pose format is static:x,y,z,heading,pitch,roll")
    return StaticPoseSource(Pose.from_euler(*vals))


register_pose_source("keyboard", lambda arg, initial: KeyboardPoseSource(initial))
register_pose_source("static", _parse_static)


def create_pose_source(spec: str, initial: Pose) -> PoseSource:
    """Build a pose source from ``scheme[:argument]`` (``keyboard``, ``static:...``, ``serial:PORT``)."""
    scheme, _, arg = spec.partition(":")
    if scheme not in _REGISTRY and scheme in _PLUGIN_MODULES:
        module = _PLUGIN_MODULES[scheme]
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name is not None and module.startswith(exc.name):
                raise PoseSourceUnavailableError(
                    f"pose source {scheme!r} is provided by {module}, which is not installed in this "
                    f"checkout; use --pose keyboard (or static:...) until the Phase 3 serial IMU "
                    f"reader lands"
                ) from exc
            raise
        if scheme not in _REGISTRY:
            raise PoseSourceUnavailableError(f"{module} did not register a {scheme!r} pose source")
    if scheme not in _REGISTRY:
        raise PoseSourceUnavailableError(
            f"unknown pose source {scheme!r}; available: {', '.join(available_pose_sources())}"
        )
    return _REGISTRY[scheme](arg, Pose(np.asarray(initial.position, float).copy(), initial.R_wb.copy()))
