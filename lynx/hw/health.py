"""Head-tracker link health: rate, staleness, sensor accuracy, frame loss, resets.

:meth:`HealthMonitor.report` condenses everything into an :class:`ImuHealth` whose
``state`` drives the HUD and whose ``degraded`` flag maps onto the Phase 1 telemetry flag
``TelemetryFlags.IMU_DEGRADED``.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import List, Optional

from .protocol import DecoderStats, ImuFlags, ImuSample, Report, Status


class LinkState(enum.Enum):
    NO_DATA = "no-data"  # nothing received yet
    STALE = "stale"  # samples stopped arriving
    DEGRADED = "degraded"  # streaming but untrustworthy (rate, accuracy, calibration, untared)
    OK = "ok"


@dataclass
class HealthThresholds:
    stale_after_s: float = 0.25
    min_rate_fraction: float = 0.8  # of the nominal rate
    max_accuracy_deg: float = 10.0  # Rotation Vector heading-accuracy estimate
    min_cal_status: int = 2  # 0 unreliable, 1 low, 2 medium, 3 high
    max_loss_fraction: float = 0.05  # sequence gaps over the rate window


@dataclass
class ImuHealth:
    state: LinkState
    reasons: List[str] = field(default_factory=list)
    rate_hz: float = 0.0
    age_s: float = math.inf
    accuracy_deg: float = math.nan
    cal_status: int = 0
    report: Optional[Report] = None
    loss_fraction: float = 0.0
    frames: int = 0
    bad_frames: int = 0
    seq_gaps: int = 0
    device_resets: int = 0
    device_tx_dropped: int = 0

    @property
    def ok(self) -> bool:
        return self.state is LinkState.OK

    @property
    def usable(self) -> bool:
        """Pose may be used for the HUD (possibly with an IMU-degraded warning)."""
        return self.state in (LinkState.OK, LinkState.DEGRADED)

    @property
    def degraded(self) -> bool:
        return not self.ok

    def summary(self) -> str:
        acc = "" if math.isnan(self.accuracy_deg) else f" acc {self.accuracy_deg:.1f}deg"
        why = f" ({'; '.join(self.reasons)})" if self.reasons else ""
        return (f"IMU {self.state.value} {self.rate_hz:.0f}Hz{acc} cal {self.cal_status}"
                f" loss {100 * self.loss_fraction:.1f}%{why}")


class HealthMonitor:
    """Feed it every sample/status (with the host monotonic receive time) and read :meth:`report`."""

    def __init__(self, nominal_rate_hz: float = 100.0, thresholds: Optional[HealthThresholds] = None,
                 window_s: float = 1.0) -> None:
        self.nominal_rate_hz = nominal_rate_hz
        self.th = thresholds or HealthThresholds()
        self.window_s = window_s
        self._recv_times: List[float] = []
        self._last_sample: Optional[ImuSample] = None
        self._last_rx: Optional[float] = None
        self._last_seq: Optional[int] = None
        self._gap_events: List[tuple[float, int]] = []
        self.seq_gaps = 0
        self.last_status: Optional[Status] = None
        self.tared = False

    def on_frame_seq(self, seq: int, now: float) -> None:
        """Track the device's per-frame sequence counter (all frame types share it)."""
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) & 0xFF
            # A huge apparent gap is a device reboot (seq restarts at 0), not 200+ lost frames.
            if 0 < gap < 128:
                self.seq_gaps += gap
                self._gap_events.append((now, gap))
        self._last_seq = seq

    def on_sample(self, sample: ImuSample, now: float) -> None:
        self._last_sample = sample
        self._last_rx = now
        self._recv_times.append(now)
        self._trim(now)

    def on_status(self, status: Status) -> None:
        self.last_status = status

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        i = 0
        while i < len(self._recv_times) and self._recv_times[i] < cutoff:
            i += 1
        if i:
            del self._recv_times[:i]
        while self._gap_events and self._gap_events[0][0] < cutoff:
            self._gap_events.pop(0)

    def report(self, now: float, decoder: Optional[DecoderStats] = None) -> ImuHealth:
        self._trim(now)
        h = ImuHealth(state=LinkState.OK, seq_gaps=self.seq_gaps)
        if decoder is not None:
            h.frames = decoder.frames
            h.bad_frames = decoder.bad_frames
        st = self.last_status
        if st is not None:
            h.device_resets = st.sensor_resets + st.watchdog_resets
            h.device_tx_dropped = st.tx_dropped
        s = self._last_sample
        if s is None or self._last_rx is None:
            h.state = LinkState.NO_DATA
            if st is not None and not (st.imu_flags & ImuFlags.PRESENT):
                h.reasons.append("BNO085 not detected by ESP32")
            else:
                h.reasons.append("no IMU samples")
            return h
        h.age_s = now - self._last_rx
        h.cal_status = s.cal_status
        h.report = Report(s.report) if s.report in (1, 2) else None
        h.accuracy_deg = math.degrees(s.accuracy_rad) if not math.isnan(s.accuracy_rad) else math.nan
        n = len(self._recv_times)
        span = min(self.window_s, max(1e-6, now - self._recv_times[0])) if n else self.window_s
        h.rate_hz = (n - 1) / span if n > 1 else 0.0
        lost = sum(g for _, g in self._gap_events)
        h.loss_fraction = lost / (lost + n) if (lost + n) else 0.0

        if h.age_s > self.th.stale_after_s:
            h.state = LinkState.STALE
            h.reasons.append(f"no sample for {h.age_s:.2f}s")
            return h
        reasons = h.reasons
        if n > 1 and h.rate_hz < self.th.min_rate_fraction * self.nominal_rate_hz:
            reasons.append(f"rate {h.rate_hz:.0f}/{self.nominal_rate_hz:.0f}Hz")
        if h.loss_fraction > self.th.max_loss_fraction:
            reasons.append(f"frame loss {100 * h.loss_fraction:.0f}%")
        if h.report is Report.ROTATION_VECTOR:
            if not math.isnan(h.accuracy_deg) and h.accuracy_deg > self.th.max_accuracy_deg:
                reasons.append(f"heading accuracy {h.accuracy_deg:.0f}deg")
            if s.cal_status < self.th.min_cal_status:
                reasons.append(f"mag calibration {s.cal_status}/3")
        elif h.report is Report.GAME_ROTATION_VECTOR and not self.tared:
            reasons.append("game rotation vector not tared (heading arbitrary)")
        if reasons:
            h.state = LinkState.DEGRADED
        return h
