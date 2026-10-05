"""Host side of the head-tracker link: read frames, convert attitude, track health, send commands.

:class:`ImuLink` owns a reader thread (pyserial reads block), so it works from synchronous render
loops (the sim) and from asyncio code alike:

* sync:  ``link.latest()``, ``link.poll_events()``, ``link.command(...)``
* async: ``await link.acommand(...)``, ``async for ev in link.aevents(): ...``

Rail events carry the head pose at the moment of the *press* (``RailEvent.aim``), looked up in a
short pose history by the device timestamp, so gesture-classification latency (up to the
double-click window) never moves where a ping lands.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Deque, Dict, List, Optional, Protocol, Tuple

import numpy as np

from lynx.spatial.frames import Pose
from lynx.spatial.rotations import matrix_to_euler, quat_to_matrix

from .health import HealthMonitor, ImuHealth
from .orientation import ImuCalibration, OrientationConverter
from .protocol import (
    Ack,
    AckResult,
    Button,
    ButtonEvent,
    CmdHello,
    CmdSaveDcd,
    CmdSetCal,
    CmdSetReport,
    CmdTare,
    CmdTarePersist,
    FrameDecoder,
    Hello,
    ImuSample,
    Log,
    Message,
    Report,
    Status,
    TareAxes,
    TareBasis,
    encode_frame,
)

log = logging.getLogger("lynx.hw")

DEFAULT_BAUD = 460800
AIM_MATCH_TOLERANCE_US = 50_000


class Transport(Protocol):
    timeout: Optional[float]

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> Optional[int]: ...

    def close(self) -> None: ...


def open_transport(port: str, baudrate: int = DEFAULT_BAUD, timeout: float = 0.05) -> Transport:
    """Open ``port``: a serial device (``/dev/ttyUSB0``, ``COM5``), any pyserial URL, or a mock.

    ``mock://`` is a scanning head, ``mock://still`` a motionless one facing North.
    DTR and RTS are deasserted *before* opening so the ESP32-DevKitC auto-reset circuit does not
    reboot the board (which would also discard any device-side state) on every connect.
    """
    if port.startswith("mock"):
        from .mock import MockImuDevice, still_pose

        return MockImuDevice(pose_fn=still_pose if port.rstrip("/").endswith("still") else None,
                             timeout=timeout)
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("pyserial is required for hardware: pip install 'teamlynx[hw]'") from exc
    s = serial.serial_for_url(port, baudrate=baudrate, timeout=timeout, do_not_open=True)
    s.dtr = False
    s.rts = False
    s.open()
    return s


@dataclass
class HeadPose:
    """Head attitude at device time ``t_us`` in the Phase 1 convention (ENU world, FLU body)."""

    t_us: int
    host_time: float
    q_wb: np.ndarray
    heading: float
    pitch: float
    roll: float
    q_raw: Tuple[float, float, float, float]
    accuracy_deg: float = math.nan
    cal_status: int = 0
    report: int = Report.ROTATION_VECTOR

    def to_pose(self, position) -> Pose:
        return Pose(np.asarray(position, dtype=float), quat_to_matrix(self.q_wb))


@dataclass
class RailEvent:
    event: ButtonEvent
    button: Button
    host_time: float
    aim: Optional[HeadPose] = None  # head pose at the gesture's first press, if known


@dataclass
class _PendingAck:
    cmd_type: int
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[AckResult] = None


class ImuLink:
    def __init__(
        self,
        transport: Optional[Transport] = None,
        converter: Optional[OrientationConverter] = None,
        nominal_rate_hz: float = 100.0,
        history_s: float = 2.0,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUD,
        clock: Callable[[], float] = time.monotonic,
        reconnect_s: float = 1.0,
    ) -> None:
        if transport is None and port is None:
            raise ValueError("need a transport or a port")
        self.transport = transport
        self.port = port
        self.baudrate = baudrate
        self.converter = converter or OrientationConverter()
        self.clock = clock
        self.reconnect_s = reconnect_s
        self.decoder = FrameDecoder()
        self.health_monitor = HealthMonitor(nominal_rate_hz=nominal_rate_hz)
        self.health_monitor.tared = self.converter.cal.tared
        self.hello: Optional[Hello] = None
        self.last_status: Optional[Status] = None
        self.logs: Deque[str] = collections.deque(maxlen=50)
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._history: Deque[HeadPose] = collections.deque(maxlen=max(16, int(history_s * nominal_rate_hz * 2)))
        self._events: "queue.Queue[RailEvent]" = queue.Queue()
        self._async_subscribers: List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._pending: Dict[int, _PendingAck] = {}
        self._tx_seq = 0
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

    @classmethod
    def open(cls, port: str, cal: Optional[ImuCalibration] = None, baudrate: int = DEFAULT_BAUD,
             **kwargs) -> "ImuLink":
        conv = OrientationConverter(cal or ImuCalibration())
        return cls(open_transport(port, baudrate), conv, port=port, baudrate=baudrate, **kwargs)

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> "ImuLink":
        if self._thread is None:
            self._running.set()
            self._thread = threading.Thread(target=self._run, name="lynx-imu", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "ImuLink":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        while self._running.is_set():
            if self.transport is None:
                if not self._reopen():
                    time.sleep(self.reconnect_s)
                continue
            try:
                data = self.transport.read(512)
            except Exception as exc:  # serial.SerialException on unplug
                self.error = f"read failed: {exc}"
                log.warning("IMU link %s; reconnecting", self.error)
                try:
                    self.transport.close()
                except Exception:
                    pass
                self.transport = None if self.port else self.transport
                if self.port is None:
                    return
                continue
            if data:
                self.feed(data)

    def _reopen(self) -> bool:
        assert self.port is not None
        try:
            self.transport = open_transport(self.port, self.baudrate)
        except Exception as exc:
            self.error = f"open {self.port} failed: {exc}"
            return False
        self.error = None
        self.decoder = FrameDecoder()
        log.info("IMU link reopened on %s", self.port)
        return True

    # -- receive path -----------------------------------------------------------------------

    def feed(self, data: bytes) -> List[Message]:
        """Decode bytes and dispatch the messages (called by the reader thread; public for tests)."""
        msgs = self.decoder.feed(data)
        now = self.clock()
        for msg in msgs:
            self._dispatch(msg, now)
        return msgs

    def _dispatch(self, msg: Message, now: float) -> None:
        self.health_monitor.on_frame_seq(msg.seq, now)
        if isinstance(msg, ImuSample):
            self._on_sample(msg, now)
        elif isinstance(msg, Button):
            self._on_button(msg, now)
        elif isinstance(msg, Status):
            self.last_status = msg
            self.health_monitor.on_status(msg)
        elif isinstance(msg, Ack):
            with self._lock:
                pending = self._pending.get(msg.cmd_seq)
            if pending is not None and pending.cmd_type == msg.cmd_type:
                try:
                    pending.result = AckResult(msg.result)
                except ValueError:
                    pending.result = AckResult.UNKNOWN_CMD
                pending.done.set()
        elif isinstance(msg, Hello):
            self.hello = msg
            log.info("head tracker fw %s board %d report %d @ %d Hz", msg.fw_version, msg.board,
                     msg.report, msg.rate_hz)
        elif isinstance(msg, Log):
            self.logs.append(msg.text)
            log.info("device: %s", msg.text)

    def _on_sample(self, s: ImuSample, now: float) -> None:
        if not all(math.isfinite(v) for v in s.quat) or sum(v * v for v in s.quat) < 0.25:
            return
        q_wb = self.converter.body_quat(s.quat)
        h, p, r = matrix_to_euler(quat_to_matrix(q_wb))
        acc = math.degrees(s.accuracy_rad) if math.isfinite(s.accuracy_rad) else math.nan
        pose = HeadPose(t_us=s.t_us, host_time=now, q_wb=q_wb, heading=h, pitch=p, roll=r,
                        q_raw=s.quat, accuracy_deg=acc, cal_status=s.cal_status, report=s.report)
        with self._lock:
            if self._history and s.t_us < self._history[-1].t_us:
                self._history.clear()  # device rebooted: its clock restarted
            self._history.append(pose)
        self.health_monitor.on_sample(s, now)

    def _on_button(self, b: Button, now: float) -> None:
        try:
            event = ButtonEvent(b.event)
        except ValueError:
            return
        aim = self.pose_at(b.press_t_us) if event in (ButtonEvent.SINGLE, ButtonEvent.DOUBLE,
                                                       ButtonEvent.LONG) else None
        ev = RailEvent(event=event, button=b, host_time=now, aim=aim)
        self._events.put(ev)
        for loop, q in list(self._async_subscribers):
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:
                self._async_subscribers.remove((loop, q))

    # -- queries ----------------------------------------------------------------------------

    def latest(self) -> Optional[HeadPose]:
        with self._lock:
            return self._history[-1] if self._history else None

    def pose_at(self, t_us: int, tolerance_us: int = AIM_MATCH_TOLERANCE_US) -> Optional[HeadPose]:
        """Pose whose device timestamp is nearest ``t_us`` (None if none within tolerance)."""
        with self._lock:
            hist = list(self._history)
        if not hist:
            return None
        ts = [p.t_us for p in hist]
        i = bisect.bisect_left(ts, t_us)
        best = min((c for c in (i - 1, i) if 0 <= c < len(hist)), key=lambda c: abs(ts[c] - t_us))
        return hist[best] if abs(ts[best] - t_us) <= tolerance_us else None

    def recent_raw(self, window_s: float) -> List[Tuple[float, float, float, float]]:
        with self._lock:
            hist = list(self._history)
        if not hist:
            return []
        t_end = hist[-1].t_us
        return [p.q_raw for p in hist if t_end - p.t_us <= window_s * 1e6]

    def poll_events(self) -> List[RailEvent]:
        out: List[RailEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def health(self, now: Optional[float] = None) -> ImuHealth:
        return self.health_monitor.report(self.clock() if now is None else now, self.decoder.stats)

    # -- calibration ------------------------------------------------------------------------

    def tare(self, datum_bearing_deg: float, window_s: float = 0.5) -> float:
        """Host-side heading tare: the current line of sight becomes ``datum_bearing_deg``."""
        offset = self.converter.tare(self.recent_raw(window_s), datum_bearing_deg)
        self.health_monitor.tared = True
        with self._lock:
            self._history.clear()  # old poses used the previous offset
        return offset

    # -- commands ---------------------------------------------------------------------------

    def send(self, msg: Message) -> int:
        """Send a command without waiting; returns the sequence number used."""
        if self.transport is None:
            raise ConnectionError(self.error or "IMU link not connected")
        with self._write_lock:
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFF
            msg.seq = seq
            self.transport.write(encode_frame(msg))
        return seq

    def command(self, msg: Message, timeout: float = 1.0) -> AckResult:
        """Send a command and wait for its ACK. Raises TimeoutError if none arrives."""
        with self._write_lock:
            seq = self._tx_seq
        pending = _PendingAck(cmd_type=int(msg.TYPE))
        with self._lock:
            self._pending[seq] = pending
        try:
            sent = self.send(msg)
            if sent != seq:  # another thread sent in between; re-key the waiter
                with self._lock:
                    self._pending.pop(seq, None)
                    self._pending[sent] = pending
                seq = sent
            if self._thread is None:
                self._pump_until(pending.done, timeout)
            if not pending.done.wait(timeout):
                raise TimeoutError(f"no ACK for {msg.TYPE.name} (seq {seq})")
            assert pending.result is not None
            return pending.result
        finally:
            with self._lock:
                self._pending.pop(seq, None)

    def _pump_until(self, done: threading.Event, timeout: float) -> None:
        """Without a reader thread, read inline until ``done`` (used by one-shot CLI calls)."""
        deadline = time.monotonic() + timeout
        while not done.is_set() and time.monotonic() < deadline and self.transport is not None:
            data = self.transport.read(512)
            if data:
                self.feed(data)

    async def acommand(self, msg: Message, timeout: float = 1.0) -> AckResult:
        return await asyncio.to_thread(self.command, msg, timeout)

    async def aevents(self) -> AsyncIterator[RailEvent]:
        """Async stream of rail events (each subscriber sees every event from subscription on)."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        sub = (loop, q)
        self._async_subscribers.append(sub)
        try:
            while True:
                yield await q.get()
        finally:
            if sub in self._async_subscribers:
                self._async_subscribers.remove(sub)

    def hello_request(self, timeout: float = 1.0) -> AckResult:
        return self.command(CmdHello(), timeout)

    def set_report(self, report: Report, rate_hz: int, timeout: float = 1.0) -> AckResult:
        res = self.command(CmdSetReport(report=int(report), rate_hz=rate_hz), timeout)
        if res is AckResult.OK:
            self.health_monitor.nominal_rate_hz = float(rate_hz)
        return res

    def device_tare(self, axes: TareAxes = TareAxes.Z, basis: TareBasis = TareBasis.ROTATION_VECTOR,
                    persist: bool = False, timeout: float = 2.0) -> AckResult:
        res = self.command(CmdTare(axes=int(axes), basis=int(basis)), timeout)
        if res is AckResult.OK and persist:
            res = self.command(CmdTarePersist(), timeout)
        return res

    def save_dcd(self, timeout: float = 2.0) -> AckResult:
        return self.command(CmdSaveDcd(), timeout)

    def set_calibration(self, sensors: int, dcd_autosave: bool, timeout: float = 2.0) -> AckResult:
        return self.command(CmdSetCal(sensors=sensors, dcd_autosave=int(dcd_autosave)), timeout)
