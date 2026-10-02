"""In-process stand-in for the ESP32 head tracker.

:class:`MockImuDevice` implements the subset of the pyserial ``Serial`` API that
:class:`lynx.hw.serial_link.ImuLink` uses (``read``, ``write``, ``close``, ``timeout``), and speaks
the real v1 wire protocol, so everything above the transport runs unchanged against it.

* Samples are generated from a *true* head attitude ``pose_fn(t) -> (heading, pitch, roll)``
  through the inverse sensor model (:func:`lynx.hw.orientation.sensor_quat_for_body`), including
  the mounting rotation, magnetic declination and an optional sensor heading bias.
* Commands are acknowledged like the firmware does. Device-side calibration commands (tare,
  save DCD, cal config) are recorded in :attr:`commands` and ACKed but do not change the output.
* Rail-switch gestures are injected with :meth:`gesture`.
* ``clock`` may be a manual clock for deterministic tests; ``realtime=False`` makes ``read``
  return immediately instead of waiting for the next sample.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, List, Optional, Tuple

from lynx.spatial.rotations import euler_to_quat

from .orientation import ImuCalibration, sensor_quat_for_body
from .protocol import (
    Ack,
    AckResult,
    Board,
    Button,
    ButtonEvent,
    CmdHello,
    CmdResetImu,
    CmdSetCal,
    CmdSetReport,
    CmdTare,
    FrameDecoder,
    Hello,
    ImuFlags,
    ImuSample,
    Log,
    Message,
    Report,
    Status,
    Unknown,
    encode_frame,
)

PoseFn = Callable[[float], Tuple[float, float, float]]


def scanning_pose(t: float) -> Tuple[float, float, float]:
    """Default head motion: slow +-40 deg scan, gentle nod, slight roll."""
    return (wrap360(40.0 * math.sin(2 * math.pi * t / 16.0)),
            -8.0 + 6.0 * math.sin(2 * math.pi * t / 7.0),
            3.0 * math.sin(2 * math.pi * t / 11.0))


def still_pose(t: float) -> Tuple[float, float, float]:
    return (0.0, 0.0, 0.0)


def wrap360(a: float) -> float:
    return a % 360.0


class MockImuDevice:
    MAX_CATCHUP = 50

    def __init__(
        self,
        pose_fn: Optional[PoseFn] = None,
        cal: Optional[ImuCalibration] = None,
        rate_hz: int = 100,
        report: Report = Report.ROTATION_VECTOR,
        sensor_heading_error_deg: float = 0.0,
        accuracy_rad: float = math.radians(3.0),
        cal_status: int = 3,
        clock: Optional[Callable[[], float]] = None,
        realtime: bool = True,
        timeout: float = 0.05,
        board: Board = Board.ESP32_DEVKITC,
    ) -> None:
        self.pose_fn = pose_fn or scanning_pose
        self.cal = cal or ImuCalibration()
        self.rate_hz = rate_hz
        self.report = Report(report)
        self.sensor_heading_error_deg = sensor_heading_error_deg
        self.accuracy_rad = accuracy_rad
        self.cal_status = cal_status
        self.clock = clock or time.monotonic
        self.realtime = realtime
        self.timeout = timeout
        self.board = board
        self.streaming = True
        self.imu_present = True
        self.commands: List[Message] = []
        self.fail_commands: set[int] = set()
        self._lock = threading.Lock()
        self._out = bytearray()
        self._rx = FrameDecoder()
        self._seq = 0
        self._t0 = self.clock()
        self._next_sample = self._t0
        self._next_status = self._t0 + 1.0
        self._samples_since_status = 0
        self._open = True
        self._emit(self._hello())

    # -- clock -----------------------------------------------------------------------------

    def now_us(self) -> int:
        return int(round((self.clock() - self._t0) * 1e6))

    def _t_us(self, t: float) -> int:
        return int(round((t - self._t0) * 1e6))

    # -- pyserial-compatible transport ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def in_waiting(self) -> int:
        with self._lock:
            self._generate()
            return len(self._out)

    def read(self, size: int = 1) -> bytes:
        if not self._open:
            raise OSError("mock port closed")
        deadline = time.monotonic() + (self.timeout or 0.0)
        while True:
            with self._lock:
                self._generate()
                if self._out:
                    data = bytes(self._out[:size])
                    del self._out[:size]
                    return data
            if not self.realtime or time.monotonic() >= deadline:
                return b""
            time.sleep(min(0.002, 1.0 / max(1, self.rate_hz)))

    def write(self, data: bytes) -> int:
        if not self._open:
            raise OSError("mock port closed")
        with self._lock:
            for msg in self._rx.feed(bytes(data)):
                self._handle_command(msg)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._out.clear()

    def close(self) -> None:
        self._open = False

    # -- scripting --------------------------------------------------------------------------

    def gesture(self, event: ButtonEvent, press_t_us: Optional[int] = None, hold_ms: int = 80) -> int:
        """Emit the frames the firmware sends for a SINGLE/DOUBLE/LONG gesture.

        ``press_t_us`` (device clock) is when the operator pressed; defaults to now. Returns it.
        """
        with self._lock:
            self._generate()
            now = self.now_us()
            p = now if press_t_us is None else int(press_t_us)
            event = ButtonEvent(event)
            self._emit(Button(t_us=p, press_t_us=p, event=ButtonEvent.PRESS))
            if event is ButtonEvent.LONG:
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.LONG, clicks=1, hold_ms=hold_ms))
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.RELEASE, hold_ms=hold_ms))
            elif event is ButtonEvent.DOUBLE:
                self._emit(Button(t_us=p, press_t_us=p, event=ButtonEvent.RELEASE, hold_ms=hold_ms))
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.PRESS))
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.RELEASE, hold_ms=hold_ms))
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.DOUBLE, clicks=2, hold_ms=hold_ms))
            elif event is ButtonEvent.SINGLE:
                self._emit(Button(t_us=p, press_t_us=p, event=ButtonEvent.RELEASE, hold_ms=hold_ms))
                self._emit(Button(t_us=now, press_t_us=p, event=ButtonEvent.SINGLE, clicks=1, hold_ms=hold_ms))
            else:
                raise ValueError(f"not a gesture: {event!r}")
            return p

    def inject_bytes(self, data: bytes) -> None:
        """Append raw bytes to the device->host stream (noise, partial frames, text)."""
        with self._lock:
            self._out += data

    def true_pose(self, t_us: int) -> Tuple[float, float, float]:
        return self.pose_fn(t_us * 1e-6)

    # -- internals --------------------------------------------------------------------------

    def _emit(self, msg: Message) -> None:
        msg.seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        self._out += encode_frame(msg)

    def _imu_flags(self) -> int:
        f = ImuFlags.NONE
        if self.imu_present:
            f |= ImuFlags.PRESENT
            if self.streaming:
                f |= ImuFlags.STREAMING
        return int(f)

    def _hello(self) -> Hello:
        return Hello(fw_major=1, fw_minor=0, fw_patch=0, board=int(self.board), report=int(self.report),
                     imu_flags=self._imu_flags(), rate_hz=self.rate_hz, uptime_ms=self.now_us() // 1000)

    def _sample(self, t: float) -> ImuSample:
        t_us = self._t_us(t)
        h, p, r = self.pose_fn(t_us * 1e-6)
        q = sensor_quat_for_body(euler_to_quat(h, p, r), self.cal, self.sensor_heading_error_deg)
        grv = self.report is Report.GAME_ROTATION_VECTOR
        return ImuSample(t_us=t_us, w=float(q[0]), x=float(q[1]), y=float(q[2]), z=float(q[3]),
                         accuracy_rad=math.nan if grv else self.accuracy_rad, report=int(self.report),
                         cal_status=self.cal_status)

    def _generate(self) -> None:
        now = self.clock()
        period = 1.0 / self.rate_hz
        if not (self.streaming and self.imu_present):
            self._next_sample = now
        else:
            if now - self._next_sample > self.MAX_CATCHUP * period:
                self._next_sample = now - self.MAX_CATCHUP * period
            while self._next_sample <= now:
                self._emit(self._sample(self._next_sample))
                self._samples_since_status += 1
                self._next_sample += period
        while self._next_status <= now:
            self._emit(Status(uptime_ms=self._t_us(self._next_status) // 1000, imu_flags=self._imu_flags(),
                              report=int(self.report), rate_x10=self._samples_since_status * 10))
            self._samples_since_status = 0
            self._next_status += 1.0

    def _ack(self, msg: Message, result: AckResult) -> None:
        self._emit(Ack(cmd_type=int(msg.TYPE), cmd_seq=msg.seq, result=int(result)))

    def _handle_command(self, msg: Message) -> None:
        if isinstance(msg, Unknown):
            self._emit(Ack(cmd_type=msg.msg_type, cmd_seq=msg.seq, result=int(AckResult.UNKNOWN_CMD)))
            return
        self.commands.append(msg)
        if int(msg.TYPE) in self.fail_commands:
            self._ack(msg, AckResult.SENSOR_ERROR)
            return
        if isinstance(msg, CmdHello):
            self._emit(self._hello())
        elif isinstance(msg, CmdSetReport):
            if msg.report not in (1, 2) or not 1 <= msg.rate_hz <= 400:
                self._ack(msg, AckResult.BAD_ARG)
                return
            self.report = Report(msg.report)
            self.rate_hz = msg.rate_hz
        elif isinstance(msg, CmdTare):
            if not 1 <= msg.axes <= 7 or msg.basis > 2:
                self._ack(msg, AckResult.BAD_ARG)
                return
        elif isinstance(msg, CmdSetCal):
            if msg.sensors & ~0x07 or msg.dcd_autosave > 1:
                self._ack(msg, AckResult.BAD_ARG)
                return
        elif isinstance(msg, CmdResetImu):
            self._emit(Log(text="BNO085 at 0x4A sw 3.12.0"))
        elif msg.TYPE < 0x80:
            self._emit(Ack(cmd_type=int(msg.TYPE), cmd_seq=msg.seq, result=int(AckResult.UNKNOWN_CMD)))
            return
        self._ack(msg, AckResult.OK)
