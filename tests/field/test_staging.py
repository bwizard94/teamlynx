"""Rail-switch staging calibration end to end: mock head tracker -> ImuLink -> StagingCalibrator."""

import math

import pytest

from lynx.field.calibrate import CalibrationError, FieldCalibration
from lynx.field.gnss import GnssReader, MockGnssReceiver
from lynx.field.site import Site, bearing_deg, elevation_deg
from lynx.field.staging import StagingCalibrator, StagingOptions
from lynx.hw import ButtonEvent, ImuCalibration, ImuLink, MockImuDevice, OrientationConverter, sensor_quat_for_body
from lynx.spatial.rotations import euler_to_quat, wrap_deg_180

from .test_calibrate import SENSOR_ERR, SITE


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Rig:
    """Scripted operator: faces ``schedule`` [(t_from, heading, pitch, wobble_deg)] and taps at ``taps``."""

    def __init__(self, schedule, taps, station=(0.0, 0.0), gnss_track=None):
        self.site = Site.from_dict(SITE)
        self.clock = Clock()
        self.schedule = sorted(schedule)
        self.cal = ImuCalibration(mount="left-side", declination_deg=self.site.declination_deg)
        self.dev = MockImuDevice(pose_fn=self.pose, cal=self.cal, sensor_heading_error_deg=SENSOR_ERR,
                                 clock=self.clock, realtime=False)
        self.link = ImuLink(self.dev, OrientationConverter(ImuCalibration(**self.cal.__dict__)), clock=self.clock)
        self.taps = sorted(taps)  # (press time s, event)
        self.lines = []
        self.gnss = None
        if gnss_track is not None:
            self.rx = MockGnssReceiver(self.site.frame, track=gnss_track, realtime=False, std_m=0.3)
            self.gnss = GnssReader(clock=self.clock)
            self._next_fix = 0.0

    def pose(self, t):
        h, p, wob = 0.0, 0.0, 0.0
        for t0, hh, pp, ww in self.schedule:
            if t >= t0:
                h, p, wob = hh, pp, ww
        return (h + wob * math.sin(40.0 * t), p, 0.0)

    def pump(self):
        self.clock.t += 0.01
        self.link.feed(self.dev.read(65536))
        t = self.clock.t - 100.0
        while self.taps and t >= self.taps[0][0] + 0.35:
            press, event = self.taps.pop(0)
            self.dev.gesture(event, press_t_us=int(press * 1e6))
            self.link.feed(self.dev.read(65536))
        if self.gnss is not None and t >= self._next_fix:
            for line in self.rx.epoch_lines(t):
                self.gnss.feed_line(line, now=self.clock.t)
            self._next_fix += 0.2

    def calibrator(self, markers, station="S1", **kw):
        opts = StagingOptions(node=4, markers=markers, station=station, tap_timeout_s=30.0, gnss_seconds=3.0, **kw)
        return StagingCalibrator(self.site, self.link, opts, gnss=self.gnss, prompt=self.lines.append,
                                 pump=self.pump, clock=self.clock, wall=lambda: 1_790_000_000.0 + self.clock.t)

    def aim(self, marker, frm=(0.0, 0.0)):
        m = self.site.marker(marker)
        return (bearing_deg(frm, m.en), elevation_deg((*frm, self.site.eye_height_m), (m.e, m.n, m.u)))


def tared_error(cal: FieldCalibration, true_heading: float) -> float:
    q = sensor_quat_for_body(euler_to_quat(true_heading, 0.0, 0.0), cal.imu, SENSOR_ERR)
    return wrap_deg_180(OrientationConverter(cal.imu).euler(q)[0] - true_heading)


def test_station_two_markers_hands_free():
    rig = Rig([], [])
    hn, pn = rig.aim("FLAG-N")
    he, pe = rig.aim("MAST-E")
    rig.schedule = [(0.0, hn, pn, 0.0), (3.0, he, pe, 0.0)]
    rig.taps = [(2.0, ButtonEvent.SINGLE), (5.0, ButtonEvent.SINGLE)]
    cal = rig.calibrator(["FLAG-N", "MAST-E"]).run()
    assert cal.method == "station" and cal.station == "S1" and cal.node == 4
    assert cal.position == pytest.approx((0.0, 0.0, 1.7))
    for h in (0.0, 90.0, 222.0):
        assert tared_error(cal, h) == pytest.approx(0.0, abs=0.15)
    assert any(line.startswith("FACE FLAG-N BRG 000.0") for line in rig.lines)
    assert any(line.startswith("TARE") for line in rig.lines)
    assert max(abs(s["residual_deg"]) for s in cal.sightings) < 0.2


def test_moving_head_is_rejected_then_retaken_and_long_press_redoes():
    rig = Rig([], [])
    hn, pn = rig.aim("FLAG-N")
    he, pe = rig.aim("MAST-E")
    rig.schedule = [(0.0, hn, pn, 6.0), (1.5, hn, pn, 0.0), (6.0, he, pe, 0.0), (9.0, hn, pn, 0.0),
                    (12.0, he, pe, 0.0)]
    rig.taps = [(1.0, ButtonEvent.SINGLE),   # wobbling: rejected
                (3.0, ButtonEvent.SINGLE),   # FLAG-N ok
                (7.5, ButtonEvent.LONG),     # "redo": back to FLAG-N
                (10.5, ButtonEvent.SINGLE),  # FLAG-N again
                (13.5, ButtonEvent.SINGLE)]  # MAST-E
    cal = rig.calibrator(["FLAG-N", "MAST-E"]).run()
    assert any(line.startswith("REJECTED") for line in rig.lines)
    assert "REDO" in rig.lines
    assert [s["marker"] for s in cal.sightings] == ["FLAG-N", "MAST-E"]
    assert tared_error(cal, 45.0) == pytest.approx(0.0, abs=0.15)


def test_gnss_position_mode():
    truth = (30.0, -15.0)
    rig = Rig([], [], gnss_track=lambda t: truth)
    hm, pm = rig.aim("MAST-E", truth)
    hg, pg = rig.aim("GATE-W", truth)
    rig.schedule = [(0.0, hm, pm, 0.0), (6.0, hg, pg, 0.0)]
    rig.taps = [(5.0, ButtonEvent.SINGLE), (8.0, ButtonEvent.SINGLE)]
    cal = rig.calibrator(["MAST-E", "GATE-W"], station=None).run()
    assert cal.method == "gnss"
    assert cal.position[:2] == pytest.approx(truth, abs=0.05)
    assert tared_error(cal, 10.0) == pytest.approx(0.0, abs=0.2)
    assert any("GNSS AVERAGING" in line for line in rig.lines)


def test_no_station_no_gnss_needs_three_markers():
    rig = Rig([], [])
    with pytest.raises(CalibrationError, match="station"):
        rig.calibrator(["FLAG-N"], station=None).run()


def test_drift_check_records_error(tmp_path):
    rig = Rig([], [])
    hn, pn = rig.aim("FLAG-N")
    rig.schedule = [(0.0, hn, pn, 0.0)]
    rig.taps = [(2.0, ButtonEvent.SINGLE)]
    cal = rig.calibrator(["FLAG-N"]).run()
    cal.save(tmp_path)
    loaded = FieldCalibration.load(tmp_path)
    # later the IMU has drifted: the head truly faces MAST-E but the sensor bias grew by 3 degrees
    rig.dev.sensor_heading_error_deg = SENSOR_ERR - 3.0
    he, pe = rig.aim("MAST-E")
    rig.schedule = [(0.0, he, pe, 0.0)]
    t = rig.clock.t - 100.0
    rig.taps = [(t + 1.0, ButtonEvent.SINGLE)]
    rig.link.converter = OrientationConverter(loaded.imu)
    chk = rig.calibrator(["MAST-E"]).check(loaded, "MAST-E")
    assert abs(chk.error_deg) == pytest.approx(3.0, abs=0.2)
    assert loaded.checks == [chk]
