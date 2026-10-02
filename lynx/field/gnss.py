"""u-blox GNSS over serial: NMEA reader thread, mock receiver and position averaging.

The receiver is used in its default NMEA mode (GGA for position/quality/HDOP, RMC for speed and
course, GST for per-axis error estimates when enabled). :func:`lynx.field.geodesy.ublox_setup_frames`
raises the navigation rate to 5 Hz and trims the sentence set; it is sent once at open.

Accuracy model. With GST, the per-axis 1-sigma comes from the receiver. Without it, the horizontal
RMS error is ``HDOP * UERE`` (``UERE`` default 2.5 m for a single-frequency M8/M10 receiver in open
sky) split evenly over East and North: ``sigma_e = sigma_n = HDOP * UERE / sqrt(2)``.

Averaging. GNSS errors are strongly time-correlated (multipath and ionosphere wander over tens of
seconds), so the standard error of an N-sample mean is *not* ``sigma / sqrt(N)``. :func:`average_fixes`
uses ``N_eff = max(1, T / tau)`` with a correlation time ``tau`` of 60 s, which is conservative for
a stationary antenna.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

from .geodesy import Gga, Gst, LocalFrame, NmeaError, Rmc, format_gga, format_gst, format_rmc, parse_nmea, ublox_setup_frames

log = logging.getLogger("lynx.gnss")

DEFAULT_GNSS_BAUD = 38400
DEFAULT_UERE_M = 2.5


class LineTransport(Protocol):
    def readline(self) -> bytes: ...

    def write(self, data: bytes) -> Optional[int]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GnssFix:
    host_time: float
    lat: float
    lon: float
    h: float  # ellipsoidal height (m)
    quality: int
    num_sats: int
    hdop: float
    std_e: float
    std_n: float
    std_u: float
    speed_mps: Optional[float] = None
    course_deg: Optional[float] = None
    utc_s: Optional[float] = None

    @property
    def std_h(self) -> float:
        """Horizontal RMS error (m)."""
        return math.hypot(self.std_e, self.std_n)

    def enu(self, frame: LocalFrame) -> Tuple[float, float, float]:
        return frame.to_enu(self.lat, self.lon, self.h)


class GnssReader:
    """Assembles :class:`GnssFix` from GGA (+ RMC, GST) sentences; optional reader thread."""

    def __init__(self, transport: Optional[LineTransport] = None, uere_m: float = DEFAULT_UERE_M,
                 clock: Callable[[], float] = time.monotonic, stale_s: float = 2.0) -> None:
        self.transport = transport
        self.uere_m = uere_m
        self.clock = clock
        self.stale_s = stale_s
        self._lock = threading.Lock()
        self._fix: Optional[GnssFix] = None
        self._rmc: Optional[Rmc] = None
        self._gst: Optional[Gst] = None
        self._listeners: List[Callable[[GnssFix], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self.sentences = 0
        self.bad_sentences = 0
        self.fixes = 0

    @classmethod
    def open(cls, port: str, baudrate: int = DEFAULT_GNSS_BAUD, rate_hz: float = 5.0,
             generation: str = "m8", **kwargs) -> "GnssReader":
        if port.startswith("mock"):
            transport: LineTransport = MockGnssReceiver()
        else:
            try:
                import serial
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("pyserial is required for GNSS: pip install 'teamlynx[hw]'") from exc
            transport = serial.serial_for_url(port, baudrate=baudrate, timeout=0.2)
        reader = cls(transport, **kwargs)
        for frame in ublox_setup_frames(rate_hz, generation):
            transport.write(frame)
        return reader

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> "GnssReader":
        if self._thread is None and self.transport is not None:
            self._running.set()
            self._thread = threading.Thread(target=self._run, name="lynx-gnss", daemon=True)
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

    def _run(self) -> None:
        assert self.transport is not None
        while self._running.is_set():
            try:
                raw = self.transport.readline()
            except Exception as exc:  # unplugged
                log.warning("GNSS read failed: %s", exc)
                time.sleep(0.5)
                continue
            if raw:
                self.feed_line(raw.decode("ascii", errors="replace"))

    def add_listener(self, fn: Callable[[GnssFix], None]) -> None:
        self._listeners.append(fn)

    # -- parsing -------------------------------------------------------------------------------

    def feed_line(self, line: str, now: Optional[float] = None) -> Optional[GnssFix]:
        """Parse one sentence; returns the new fix when a valid GGA completed one."""
        line = line.strip()
        if not line.startswith("$"):
            return None
        self.sentences += 1
        try:
            msg = parse_nmea(line)
        except NmeaError:
            self.bad_sentences += 1
            return None
        now = self.clock() if now is None else now
        if isinstance(msg, Rmc):
            self._rmc = msg
        elif isinstance(msg, Gst):
            self._gst = msg
        elif isinstance(msg, Gga):
            return self._on_gga(msg, now)
        return None

    def _on_gga(self, g: Gga, now: float) -> Optional[GnssFix]:
        if not g.valid:
            with self._lock:
                self._fix = None
            return None
        hdop = g.hdop if g.hdop is not None else 99.0
        gst = self._gst if self._gst is not None and self._gst.utc_s == g.utc_s else None
        if gst is not None and gst.std_lat_m is not None and gst.std_lon_m is not None:
            std_n, std_e = gst.std_lat_m, gst.std_lon_m
            std_u = gst.std_alt_m if gst.std_alt_m is not None else 2.0 * std_n
        else:
            std_e = std_n = hdop * self.uere_m / math.sqrt(2.0)
            std_u = 2.0 * std_n
        rmc = self._rmc if self._rmc is not None and self._rmc.utc_s == g.utc_s and self._rmc.valid else None
        fix = GnssFix(now, float(g.lat), float(g.lon), float(g.h_ellipsoid or 0.0), g.quality, g.num_sats, hdop,
                      std_e, std_n, std_u, rmc.speed_mps if rmc else None, rmc.course_deg if rmc else None, g.utc_s)
        with self._lock:
            self._fix = fix
        self.fixes += 1
        for fn in list(self._listeners):
            try:
                fn(fix)
            except Exception:
                log.exception("GNSS listener failed")
        return fix

    # -- queries -------------------------------------------------------------------------------

    def latest(self, now: Optional[float] = None) -> Optional[GnssFix]:
        """Latest valid fix no older than ``stale_s`` (None otherwise)."""
        now = self.clock() if now is None else now
        with self._lock:
            fix = self._fix
        if fix is None or now - fix.host_time > self.stale_s:
            return None
        return fix


# ------------------------------------------------------------------------------- mock receiver


class MockGnssReceiver:
    """Generates GGA/RMC/GST sentences for a scripted ENU trajectory around a datum.

    ``track(t) -> (e, n)`` gives the true antenna position; ``noise_m`` adds white noise with that
    per-axis sigma (deterministic for a given ``seed``). ``readline`` paces output at ``rate_hz``
    in real time unless ``realtime=False``.
    """

    def __init__(self, frame: Optional[LocalFrame] = None, track: Optional[Callable[[float], Tuple[float, float]]] = None,
                 rate_hz: float = 5.0, noise_m: float = 0.0, seed: int = 0, quality: int = 1, hdop: float = 0.8,
                 std_m: float = 1.2, clock: Callable[[], float] = time.monotonic, realtime: bool = True,
                 timeout: float = 0.2) -> None:
        import random

        self.frame = frame or LocalFrame(51.5, -1.25, 80.0)
        self.track = track or (lambda t: (0.0, 0.0))
        self.rate_hz = rate_hz
        self.noise_m = noise_m
        self.quality = quality
        self.hdop = hdop
        self.std_m = std_m
        self.clock = clock
        self.realtime = realtime
        self.timeout = timeout
        self.written: List[bytes] = []
        self._rng = random.Random(seed)
        self._t0 = clock()
        self._next = self._t0
        self._queue: List[bytes] = []
        self._open = True

    def epoch_lines(self, t: float) -> List[str]:
        e, n = self.track(t)
        dt = 0.2
        e2, n2 = self.track(t + dt)
        speed = math.hypot(e2 - e, n2 - n) / dt
        course = math.degrees(math.atan2(e2 - e, n2 - n)) % 360.0
        if self.noise_m:
            e += self._rng.gauss(0.0, self.noise_m)
            n += self._rng.gauss(0.0, self.noise_m)
        lat, lon, h = self.frame.to_geodetic(e, n, 0.0)
        utc = 43200.0 + round(t * self.rate_hz) / self.rate_hz
        return [format_gga(utc, lat, lon, h, self.quality, 12, self.hdop),
                format_rmc(utc, lat, lon, speed, course),
                format_gst(utc, self.std_m, self.std_m, 2 * self.std_m)]

    def readline(self) -> bytes:
        if not self._open:
            raise OSError("mock GNSS closed")
        deadline = time.monotonic() + self.timeout
        while not self._queue:
            now = self.clock()
            if now >= self._next:
                self._queue.extend(line.encode() for line in self.epoch_lines(self._next - self._t0))
                self._next += 1.0 / self.rate_hz
                break
            if not self.realtime or time.monotonic() >= deadline:
                return b""
            time.sleep(min(0.01, self._next - now))
        return self._queue.pop(0)

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self._open = False


# ------------------------------------------------------------------------------- averaging


@dataclass(frozen=True)
class AveragedPosition:
    lat: float
    lon: float
    h: float
    e: float
    n: float
    u: float
    samples: int
    rejected: int
    duration_s: float
    scatter_e: float
    scatter_n: float
    std_e: float  # standard error of the mean (correlation-aware), per axis
    std_n: float

    @property
    def std_h(self) -> float:
        return math.hypot(self.std_e, self.std_n)


def average_fixes(fixes: Sequence[GnssFix], frame: LocalFrame, min_quality: int = 1, max_hdop: float = 4.0,
                  outlier_sigma: float = 3.0, tau_s: float = 60.0) -> AveragedPosition:
    """Robust average of stationary fixes in ENU (median-gated, correlation-aware error)."""
    good = [f for f in fixes if f.quality >= min_quality and f.hdop <= max_hdop]
    if not good:
        raise ValueError("no usable GNSS fixes (check antenna sky view / fix quality)")
    pts = [f.enu(frame) for f in good]
    me = statistics.median(p[0] for p in pts)
    mn = statistics.median(p[1] for p in pts)
    sigma = max(0.05, statistics.median(max(f.std_e, f.std_n) for f in good))
    kept = [(p, f) for p, f in zip(pts, good) if math.hypot(p[0] - me, p[1] - mn) <= outlier_sigma * sigma * math.sqrt(2)]
    if not kept:
        kept = list(zip(pts, good))
    es = [p[0] for p, _ in kept]
    ns = [p[1] for p, _ in kept]
    us = [p[2] for p, _ in kept]
    e, n, u = statistics.fmean(es), statistics.fmean(ns), statistics.fmean(us)
    sc_e = statistics.pstdev(es) if len(es) > 1 else 0.0
    sc_n = statistics.pstdev(ns) if len(ns) > 1 else 0.0
    duration = kept[-1][1].host_time - kept[0][1].host_time if len(kept) > 1 else 0.0
    n_eff = max(1.0, duration / tau_s)
    rx_e = statistics.fmean(f.std_e for _, f in kept)
    rx_n = statistics.fmean(f.std_n for _, f in kept)
    std_e = max(sc_e, rx_e) / math.sqrt(n_eff)
    std_n = max(sc_n, rx_n) / math.sqrt(n_eff)
    lat, lon, h = frame.to_geodetic(e, n, u)
    return AveragedPosition(lat, lon, h, e, n, u, len(kept), len(fixes) - len(kept), duration, sc_e, sc_n, std_e, std_n)


def collect_fixes(reader: GnssReader, seconds: float, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> List[GnssFix]:
    """Collect every new fix for ``seconds`` from a started reader."""
    out: List[GnssFix] = []
    lock = threading.Lock()

    def on_fix(f: GnssFix) -> None:
        with lock:
            out.append(f)

    reader.add_listener(on_fix)
    end = clock() + seconds
    try:
        while clock() < end:
            sleep(0.05)
    finally:
        reader._listeners.remove(on_fix)
    with lock:
        return list(out)
