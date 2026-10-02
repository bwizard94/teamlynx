"""Interactive staging-area calibration driven by the rail switch (``lynx-field calibrate``).

Sequence per operator, hands-free once the headset is on:

1. wait until the head tracker streams usable samples;
2. position: the named station, or a GNSS average (``--gnss``), or none (resection, >= 3 markers);
3. for each marker: prompt ``FACE <marker> BRG <bearing> - TAP``; the operator holds the reticle on
   the marker and taps the rail switch (SINGLE). The attitude history *before* the press is
   averaged (:func:`lynx.field.calibrate.capture_quats`), so the thumb's jolt never enters the
   tare. A LONG press redoes the previous marker; a rejected sighting (head moving, pitched too
   far) is simply re-prompted;
4. solve, show the result, save ``field-cal.json`` + ``imu.json`` in the calibration directory.

``check`` mode taps one marker with the *saved* calibration and records the heading error as a
drift check (no re-tare).

The prompt sink defaults to stdout; :class:`PromptDisplay` additionally renders the prompt and
live heading on the helmet display with OpenCV when ``display=True``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from lynx.hw import ButtonEvent, ImuCalibration, ImuLink, TareError

from .calibrate import (
    CalibrationError,
    DriftCheck,
    FieldCalibration,
    Sighting,
    TareSolution,
    capture_quats,
    check_error_deg,
    sighting_from_quats,
    solve_known_position,
    solve_resection,
)
from .gnss import GnssReader, average_fixes, collect_fixes
from .site import Site, bearing_deg

log = logging.getLogger("lynx.staging")


class PromptDisplay:
    """Full-screen text prompt on the helmet display (OpenCV window); no-op without a GUI."""

    def __init__(self, title: str = "TeamLynx calibration", size: Tuple[int, int] = (1280, 720)) -> None:
        self.title = title
        self.size = size
        self.ok = True
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.ok = False

    def show(self, lines: Sequence[str], heading: Optional[float] = None) -> None:
        if not self.ok:
            return
        import cv2
        import numpy as np

        w, h = self.size
        img = np.zeros((h, w, 3), np.uint8)
        y = 120
        for i, line in enumerate(lines):
            cv2.putText(img, line, (60, y), cv2.FONT_HERSHEY_SIMPLEX, 1.4 if i == 0 else 1.0,
                        (120, 255, 140), 2, cv2.LINE_AA)
            y += 70 if i == 0 else 52
        if heading is not None:
            cv2.putText(img, f"HDG {heading % 360:05.1f}", (60, h - 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                        (0, 235, 255), 2, cv2.LINE_AA)
        cv2.line(img, (w // 2 - 20, h // 2), (w // 2 + 20, h // 2), (120, 255, 140), 2)
        cv2.line(img, (w // 2, h // 2 - 20), (w // 2, h // 2 + 20), (120, 255, 140), 2)
        try:
            cv2.imshow(self.title, img)
            cv2.waitKey(1)
        except cv2.error:
            self.ok = False

    def close(self) -> None:
        if self.ok:
            import cv2

            try:
                cv2.destroyWindow(self.title)
            except cv2.error:
                pass


@dataclass
class StagingOptions:
    node: int
    markers: List[str]
    station: Optional[str] = None
    eye_height: Optional[float] = None
    gnss_seconds: float = 30.0
    window_s: float = 0.5
    max_spread_deg: float = 1.5
    tap_timeout_s: float = 120.0
    keyboard: bool = False
    sigma_imu_deg: float = 0.5
    max_residual_deg: float = 2.0


class StagingCalibrator:
    def __init__(self, site: Site, link: ImuLink, options: StagingOptions, *, gnss: Optional[GnssReader] = None,
                 prompt: Callable[[str], None] = print, display: Optional[PromptDisplay] = None,
                 pump: Optional[Callable[[], None]] = None, read_key: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.site = site
        self.link = link
        self.opt = options
        self.gnss = gnss
        self._prompt = prompt
        self.display = display
        self.pump = pump or (lambda: time.sleep(0.01))
        self.read_key = read_key
        self.clock = clock
        self.wall = wall
        self.log: List[str] = []
        self._shown: List[str] = []

    @property
    def cal(self) -> ImuCalibration:
        return self.link.converter.cal

    def say(self, *lines: str) -> None:
        for line in lines:
            self.log.append(line)
            self._prompt(line)
        self._shown = list(lines)
        if self.display is not None:
            latest = self.link.latest()
            self.display.show(lines, latest.heading if latest else None)

    # -- waiting ---------------------------------------------------------------------------------

    def wait_samples(self, timeout_s: float = 5.0) -> None:
        end = self.clock() + timeout_s
        while self.clock() < end:
            if self.link.latest() is not None and self.link.health().usable:
                return
            self.pump()
        raise CalibrationError(f"no usable head-tracker samples ({self.link.health().summary()})")

    def wait_tap(self) -> Tuple[str, int]:
        """Block until a SINGLE (``"tap"``) or LONG (``"redo"``) gesture; returns (kind, press_t_us)."""
        self.link.poll_events()
        end = self.clock() + self.opt.tap_timeout_s
        while self.clock() < end:
            for ev in self.link.poll_events():
                if ev.event is ButtonEvent.SINGLE:
                    return "tap", ev.button.press_t_us
                if ev.event is ButtonEvent.LONG:
                    return "redo", ev.button.press_t_us
            if self.read_key is not None and self.read_key():
                latest = self.link.latest()
                if latest is not None:
                    return "tap", latest.t_us + int(0.1e6)
            if self.display is not None:
                latest = self.link.latest()
                self.display.show(self._shown, latest.heading if latest else None)
            self.pump()
        raise CalibrationError("timed out waiting for the rail switch")

    # -- steps -----------------------------------------------------------------------------------

    def position(self) -> Tuple[Optional[Tuple[float, float]], Tuple[float, float], str]:
        """((E, N) or None for resection, per-axis std, method)."""
        if self.opt.station:
            st = self.site.station(self.opt.station)
            return (st.e, st.n), (0.02, 0.02), "station"
        if self.gnss is not None:
            self.say(f"HOLD STILL - GNSS AVERAGING {self.opt.gnss_seconds:.0f} S")
            fixes = collect_fixes(self.gnss, self.opt.gnss_seconds, clock=self.clock,
                                  sleep=lambda s: self.pump())
            avg = average_fixes(fixes, self.site.frame)
            self.say(f"GNSS E{avg.e:+.1f} N{avg.n:+.1f} +-{avg.std_h:.1f} M ({avg.samples} FIXES)")
            return (avg.e, avg.n), (avg.std_e, avg.std_n), "gnss"
        if len(set(self.opt.markers)) < 3:
            raise CalibrationError("no station and no GNSS: name a --station, add --gnss, or give >= 3 markers")
        return None, (0.0, 0.0), "resection"

    def sight(self, marker: str, pos: Optional[Tuple[float, float]]) -> Optional[Sighting]:
        m = self.site.marker(marker)
        brg = f" BRG {bearing_deg(pos, m.en):05.1f}" if pos is not None else ""
        while True:
            self.say(f"FACE {marker}{brg} - HOLD RETICLE ON IT AND TAP")
            kind, press = self.wait_tap()
            if kind == "redo":
                return None
            quats = capture_quats(self.link, press, self.opt.window_s)
            try:
                s = sighting_from_quats(marker, quats, self.cal, max_spread_deg=self.opt.max_spread_deg,
                                        host_time=self.clock())
            except TareError as exc:
                self.say(f"REJECTED: {exc}".upper())
                continue
            self.say(f"{marker} OK  {s.samples} SAMPLES  STD {s.spread_deg:.2f} DEG")
            return s

    def run(self) -> FieldCalibration:
        self.wait_samples()
        pos, pos_std, method = self.position()
        sightings: List[Sighting] = []
        i = 0
        while i < len(self.opt.markers):
            s = self.sight(self.opt.markers[i], pos)
            if s is None:
                if sightings:
                    sightings.pop()
                    i -= 1
                self.say("REDO")
                continue
            sightings.append(s)
            i += 1
        sol = self.solve(sightings, pos, pos_std, method)
        eye = self.site.eye_height_m if self.opt.eye_height is None else self.opt.eye_height
        cal = FieldCalibration.from_solution(sol, self.opt.node, self.cal, eye, self.site.name, now=self.wall())
        self.say(f"TARE {sol.heading_offset_deg:+.1f} DEG +-{sol.heading_std_deg:.1f}  "
                 f"POS E{sol.e:+.1f} N{sol.n:+.1f}  ({sol.method.upper()})")
        for r in sol.results:
            if abs(r.pitch_residual_deg) > 2.0:
                self.say(f"NOTE {r.marker}: PITCH {r.pitch_residual_deg:+.1f} DEG VS EXPECTED (BORESIGHT TRIM?)")
        return cal

    def solve(self, sightings: Sequence[Sighting], pos, pos_std, method: str) -> TareSolution:
        eye = self.opt.eye_height
        if pos is None:
            guess = (0.0, 0.0)
            if self.site.stations:
                st = list(self.site.stations.values())
                guess = (sum(p.e for p in st) / len(st), sum(p.n for p in st) / len(st))
            return solve_resection(self.site, sightings, guess, eye_height=eye, sigma_imu_deg=self.opt.sigma_imu_deg,
                                   max_residual_deg=self.opt.max_residual_deg)
        return solve_known_position(self.site, pos[0], pos[1], sightings, eye_height=eye, position_std_m=pos_std,
                                    method=method, station=self.opt.station, sigma_imu_deg=self.opt.sigma_imu_deg,
                                    max_residual_deg=self.opt.max_residual_deg)

    def check(self, cal: FieldCalibration, marker: str) -> DriftCheck:
        """Tap ``marker`` with the saved calibration; returns (and appends) the drift check."""
        self.wait_samples()
        s = self.sight(marker, cal.position[:2])
        if s is None:
            raise CalibrationError("check cancelled")
        err = check_error_deg(self.site, cal, s)
        chk = DriftCheck(self.wall(), marker, err)
        cal.checks.append(chk)
        self.say(f"DRIFT CHECK {marker}: {err:+.1f} DEG AFTER {(chk.time_unix - cal.time_unix) / 60:.0f} MIN")
        return chk


def save_calibration(cal: FieldCalibration, directory: str) -> Tuple[Path, Path]:
    paths = cal.save(directory)
    log.info("saved %s and %s", *paths)
    return paths
