"""Datum calibration math: sightings, station / GNSS tare, resection, error propagation, drift."""

import math
import random

import pytest

from lynx.field.calibrate import (
    CalibrationError,
    DriftCheck,
    DriftMonitor,
    FieldCalibration,
    check_error_deg,
    sighting_from_quats,
    solve_known_position,
    solve_resection,
)
from lynx.field.site import Site, SiteError, bearing_deg, elevation_deg
from lynx.hw import ImuCalibration, OrientationConverter, TareError, sensor_quat_for_body
from lynx.spatial.rotations import euler_to_quat, wrap_deg_180

SITE = {
    "name": "test",
    "datum": {"lat": 51.5, "lon": -1.25, "h": 80.0},
    "declination_deg": 1.5,
    "eye_height_m": 1.7,
    "stations": {"S1": {"e": 0.0, "n": 0.0}, "S2": {"e": 4.0, "n": -2.0}},
    "markers": {
        "FLAG-N": {"e": 0.0, "n": 120.0, "u": 2.0},
        "MAST-E": {"e": 140.0, "n": 35.0, "u": 6.0},
        "GATE-W": {"e": -110.0, "n": -20.0, "u": 1.5},
        "NEAR": {"e": 20.0, "n": 25.0, "u": 1.0},
    },
}

SENSOR_ERR = 7.3  # unknown sensor heading bias the tare must remove


def site() -> Site:
    return Site.from_dict(SITE)


def imu_cal(mount: str = "left-side") -> ImuCalibration:
    return ImuCalibration(mount=mount, declination_deg=SITE["declination_deg"])


def quats_facing(heading: float, pitch: float, cal: ImuCalibration, n: int = 40, jitter: float = 0.15, seed: int = 0):
    rng = random.Random(seed)
    return [sensor_quat_for_body(euler_to_quat(heading + rng.gauss(0, jitter), pitch + rng.gauss(0, jitter),
                                               rng.gauss(0, jitter)), cal, SENSOR_ERR) for _ in range(n)]


def sighting_on(st: Site, marker: str, frm, cal: ImuCalibration, seed: int = 0, bias: float = 0.0):
    m = st.marker(marker)
    b = bearing_deg(frm, m.en) + bias
    p = elevation_deg((frm[0], frm[1], st.eye_height_m), (m.e, m.n, m.u))
    return sighting_from_quats(marker, quats_facing(b, p, cal, seed=seed), cal)


def tared_heading(cal: ImuCalibration, offset: float, true_heading: float) -> float:
    c = ImuCalibration(**{**cal.__dict__, "heading_offset_deg": offset, "tared": True})
    q = sensor_quat_for_body(euler_to_quat(true_heading, 0.0, 0.0), cal, SENSOR_ERR)
    return OrientationConverter(c).euler(q)[0]


def test_site_parsing_and_lat_lon_points():
    d = dict(SITE, markers={**SITE["markers"], "LL": {"lat": 51.5009, "lon": -1.25}})
    st = Site.from_dict(d)
    assert st.marker("LL").n == pytest.approx(100.1, abs=0.2) and abs(st.marker("LL").e) < 0.01
    with pytest.raises(SiteError):
        Site.from_dict({"markers": {}})
    with pytest.raises(SiteError):
        Site.from_dict({**SITE, "bogus": 1})
    with pytest.raises(SiteError):
        st.marker("NOPE")
    assert Site.from_dict(st.to_dict()).markers.keys() == st.markers.keys()


def test_sighting_rejects_motion_and_steep_pitch():
    cal = imu_cal()
    with pytest.raises(TareError, match="moved"):
        sighting_from_quats("X", quats_facing(10.0, 0.0, cal, jitter=4.0), cal)
    with pytest.raises(TareError, match="pitched"):
        sighting_from_quats("X", quats_facing(10.0, 60.0, cal), cal)
    with pytest.raises(TareError, match="samples"):
        sighting_from_quats("X", quats_facing(10.0, 0.0, cal, n=2), cal)


@pytest.mark.parametrize("mount", ["top-flat", "left-side", "rear"])
def test_station_tare_removes_sensor_heading_error(mount):
    st, cal = site(), imu_cal(mount)
    s = sighting_on(st, "FLAG-N", (0.0, 0.0), cal)
    sol = solve_known_position(st, 0.0, 0.0, [s], station="S1")
    for true_h in (0.0, 47.0, 181.0, 300.0):
        assert wrap_deg_180(tared_heading(cal, sol.heading_offset_deg, true_h) - true_h) == pytest.approx(0, abs=0.1)
    r = sol.results[0]
    assert r.residual_deg == pytest.approx(0.0, abs=1e-9)
    assert r.pitch_residual_deg == pytest.approx(0.0, abs=0.2)  # expected elevation of the flag top
    assert sol.heading_std_deg < 0.6 and sol.station == "S1"


def test_multi_marker_consistency_and_disagreement():
    st, cal = site(), imu_cal()
    good = [sighting_on(st, m, (4.0, -2.0), cal, seed=i) for i, m in enumerate(["FLAG-N", "MAST-E", "GATE-W"])]
    sol = solve_known_position(st, 4.0, -2.0, good, station="S2")
    assert sol.max_residual_deg < 0.3
    assert sol.heading_std_deg < solve_known_position(st, 4.0, -2.0, good[:1]).heading_std_deg
    # operator stood on S1 but the profile says S2: bearings disagree by ~2-5 degrees
    wrong = [sighting_on(st, m, (0.0, 0.0), cal, seed=i) for i, m in enumerate(["FLAG-N", "MAST-E", "GATE-W"])]
    with pytest.raises(CalibrationError, match="disagree"):
        solve_known_position(st, 4.0, -2.0, wrong, station="S2", max_residual_deg=1.0)


def test_marker_too_close_rejected():
    st, cal = site(), imu_cal()
    with pytest.raises(CalibrationError, match="away"):
        solve_known_position(st, 18.0, 20.0, [sighting_on(st, "NEAR", (18.0, 20.0), cal)])


def test_position_error_propagation_scales_with_inverse_range():
    st, cal = site(), imu_cal()
    near = solve_known_position(st, 0.0, 0.0, [sighting_on(st, "NEAR", (0.0, 0.0), cal)],
                                position_std_m=(1.5, 1.5), sigma_imu_deg=0.0, sigma_marker_m=0.0)
    far = solve_known_position(st, 0.0, 0.0, [sighting_on(st, "FLAG-N", (0.0, 0.0), cal)],
                               position_std_m=(1.5, 1.5), sigma_imu_deg=0.0, sigma_marker_m=0.0)
    # only the across-bearing component matters: sigma = 1.5 m / range
    assert near.heading_std_deg == pytest.approx(math.degrees(1.5 / math.hypot(20, 25)), rel=1e-4)
    assert far.heading_std_deg == pytest.approx(math.degrees(1.5 / 120.0), rel=1e-4)


def test_resection_recovers_position_and_offset():
    st, cal = site(), imu_cal()
    truth = (12.0, 8.0)
    obs = [sighting_on(st, m, truth, cal, seed=i) for i, m in enumerate(["FLAG-N", "MAST-E", "GATE-W"])]
    sol = solve_resection(st, obs, initial=(0.0, 0.0))
    assert (sol.e, sol.n) == pytest.approx(truth, abs=0.5)
    ref = solve_known_position(st, *truth, obs)
    assert wrap_deg_180(sol.heading_offset_deg - ref.heading_offset_deg) == pytest.approx(0.0, abs=0.3)
    # 0.5 deg pointing at ~125 m is ~1 m across each line of sight
    assert sol.method == "resection" and 0.5 < math.hypot(*sol.position_std_m) < 2.0


def test_resection_needs_three_markers_and_good_geometry():
    st, cal = site(), imu_cal()
    obs = [sighting_on(st, m, (0.0, 0.0), cal) for m in ["FLAG-N", "MAST-E"]]
    with pytest.raises(CalibrationError, match="at least 3"):
        solve_resection(st, obs)
    # three markers on a circle through the operator: the danger circle
    r = 100.0
    circle = Site.from_dict({"markers": {f"M{i}": {"e": r * math.cos(a), "n": r + r * math.sin(a)}
                                         for i, a in enumerate((0.3, 1.6, 2.9))}})
    obs = [sighting_on(circle, f"M{i}", (0.0, 0.0), cal, seed=i) for i in range(3)]
    with pytest.raises(CalibrationError):
        solve_resection(circle, obs, initial=(1.0, 1.0))


def test_field_calibration_round_trip_and_check(tmp_path):
    st, cal = site(), imu_cal()
    sol = solve_known_position(st, 0.0, 0.0, [sighting_on(st, "FLAG-N", (0.0, 0.0), cal)], station="S1")
    fc = FieldCalibration.from_solution(sol, 3, cal, 1.72, "test", now=1_790_000_000.0)
    fc_path, imu_path = fc.save(tmp_path)
    loaded = FieldCalibration.load(tmp_path)
    assert loaded.position == pytest.approx((0.0, 0.0, 1.72)) and loaded.node == 3
    assert ImuCalibration.load(imu_path).heading_offset_deg == pytest.approx(sol.heading_offset_deg)
    assert loaded.imu.tared and loaded.time_iso.startswith("2026-")
    # 2 degrees of heading drift later: the check sighting reads 2 degrees clockwise of truth
    drifted = sighting_on(st, "MAST-E", (0.0, 0.0), cal, bias=2.0)
    assert check_error_deg(st, loaded, drifted) == pytest.approx(2.0, abs=0.1)


def test_drift_monitor_slope_prior_and_alerts():
    mon = DriftMonitor(1000.0, prior_rate_deg_per_min=0.5)
    est = mon.estimate(1000.0 + 600.0)
    assert est.source == "prior" and est.predicted_error_deg == pytest.approx(5.0) and est.status == "warn"
    mon.add_check(DriftCheck(1000.0 + 300.0, "FLAG-N", 1.0))
    mon.add_check(DriftCheck(1000.0 + 600.0, "FLAG-N", 2.1))
    est = mon.estimate(1000.0 + 1200.0)
    assert est.source == "checks"
    assert est.rate_deg_per_min == pytest.approx((5 * 1.0 + 10 * 2.1) / (25 + 100))
    assert est.status == "warn" and est.alert().startswith("! HDG DRIFT")
    assert mon.estimate(1000.0 + 3600.0).status == "retare"
    assert "RE-TARE" in mon.estimate(1000.0 + 3600.0).alert()
    assert DriftMonitor(0.0).estimate(10_000.0).alert() is None
    assert DriftMonitor(0.0).estimate(60.0, imu_accuracy_deg=4.0).status == "warn"


def test_drift_monitor_course_over_ground_advisory():
    mon = DriftMonitor(0.0, cog_min_samples=10)
    for i in range(20):
        mon.add_course_sample(float(i), 95.0, 0.0, 87.0, 1.6)
    mon.add_course_sample(21.0, 95.0, 0.0, 10.0, 0.4)  # standing still: ignored
    assert mon.estimate(30.0).cog_bias_deg == pytest.approx(8.0)
