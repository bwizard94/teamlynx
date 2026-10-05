"""``serial`` pose source for ``lynx-headset --pose serial:SPEC`` (see ``docs/headset.md``).

SPEC is ``PORT[?key=value&...]``:

    serial:/dev/ttyUSB0
    serial:COM5?cal=imu.json
    serial:/dev/ttyACM0?mount=left-side&declination=-3.5&baud=460800
    serial:mock://            (scanning mock head, no hardware)

Keys: ``cal`` (calibration JSON from ``python -m lynx.hw tare --cal``), ``mount``,
``declination``, ``convergence``, ``baud``, ``rate`` (expected Hz for health checks).

Contract mapping (``lynx.headset.pose.PoseSample``):

* ``pose``     latest head attitude from the BNO085 at ``initial.position`` (no IMU position).
* ``trigger``  True on a frame with a SINGLE or DOUBLE gesture (for contract-only consumers).
* ``flags``    ``PING_SWITCH`` while the switch is held, ``IMU_DEGRADED`` unless the link is OK.
* ``rail``     (:class:`SerialPoseSample` extension) the frame's gesture commands, each with the
               head attitude at the instant of the press (``aim``). ``HeadsetClient.handle_rail``
               consumes it: SINGLE -> selected ping, DOUBLE -> CONTACT ping, LONG -> cancel last,
               raycast from ``aim`` so classification latency never moves the ping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import parse_qsl

from lynx.headset.pose import PoseSample, PoseSourceUnavailableError, register_pose_source
from lynx.spatial import Pose

from .bridge import ImuHeadSource, RailAction, RailCommand
from .orientation import ImuCalibration
from .serial_link import DEFAULT_BAUD, ImuLink

SPEC_KEYS = {"cal", "mount", "declination", "convergence", "baud", "rate"}


@dataclass(frozen=True)
class SerialPoseSample(PoseSample):
    rail: Tuple[RailCommand, ...] = ()


def parse_spec(spec: str) -> Tuple[str, dict]:
    port, sep, query = spec.partition("?")
    if not port:
        raise ValueError("serial pose source needs a port: serial:/dev/ttyUSB0 (or serial:mock://)")
    opts = dict(parse_qsl(query, keep_blank_values=True)) if sep else {}
    unknown = set(opts) - SPEC_KEYS
    if unknown:
        raise ValueError(f"unknown serial pose option(s) {sorted(unknown)}; known: {sorted(SPEC_KEYS)}")
    return port, opts


class SerialImuPoseSource:
    name = "serial"

    def __init__(self, spec: str, initial: Pose, link: Optional[ImuLink] = None) -> None:
        self.port, opts = parse_spec(spec)
        self.position = initial.position.copy()
        self._fallback = Pose(initial.position.copy(), initial.R_wb.copy())
        owns_link = link is None
        if link is None:
            cal = ImuCalibration.load(opts["cal"]) if opts.get("cal") else ImuCalibration()
            if "mount" in opts:
                cal.mount = opts["mount"]
            if "declination" in opts:
                cal.declination_deg = float(opts["declination"])
            if "convergence" in opts:
                cal.convergence_deg = float(opts["convergence"])
            try:
                link = ImuLink.open(self.port, cal, baudrate=int(opts.get("baud", DEFAULT_BAUD)),
                                    nominal_rate_hz=float(opts.get("rate", 100.0)))
            except (OSError, RuntimeError) as exc:  # serial.SerialException is an OSError
                raise PoseSourceUnavailableError(
                    f"lynx.hw: cannot open head tracker on {self.port!r}: {exc}. Check the USB cable, "
                    f"the port name (ls /dev/ttyUSB* /dev/ttyACM*) and dialout group membership, "
                    f"or use serial:mock:// to run without hardware"
                ) from exc
        self.head = ImuHeadSource(link)
        self.link = link.start() if owns_link else link

    def read(self, now: float) -> SerialPoseSample:
        head, commands = self.head.poll()
        pose = head.to_pose(self.position) if head is not None else self._fallback
        if head is not None:
            self._fallback = pose
        trigger = any(cmd.action in (RailAction.PING, RailAction.PING_CONTACT) for cmd in commands)
        return SerialPoseSample(pose, trigger=trigger, flags=self.head.telemetry_flags(), rail=tuple(commands))

    def handle_key(self, key: int) -> bool:
        return False

    def close(self) -> None:
        self.link.stop()


register_pose_source("serial", lambda arg, initial: SerialImuPoseSource(arg, initial))
