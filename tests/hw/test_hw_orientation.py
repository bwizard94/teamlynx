"""BNO085 quaternion -> Phase 1 heading/pitch/roll (compass CW from North, +nose-up, +right-down)."""

import math
import random

import numpy as np
import pytest

from lynx.hw.orientation import (
    MOUNT_PRESETS,
    ImuCalibration,
    MountError,
    OrientationConverter,
    TareError,
    mount_matrix,
    sensor_quat_for_body,
    trim_matrix,
)
from lynx.spatial import Pose, euler_to_quat, matrix_to_quat, quat_from_axis_angle, quat_to_matrix, raycast_from_pose

E, N, U = np.eye(3)


def ang_diff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def assert_euler(got, want, tol=1e-6):
    h, p, r = got
    assert abs(ang_diff(h, want[0])) < tol and p == pytest.approx(want[1], abs=tol)
    assert abs(ang_diff(r, want[2])) < tol


def sensor_quat_from_axes(x_world, y_world, z_world):
    """Independent oracle: BNO085 quaternion from where its x/y/z axes point in ENU."""
    return matrix_to_quat(np.column_stack([x_world, y_world, z_world]))


def test_identity_quaternion_faces_east():
    # BNO085 world is ENU: the identity quaternion means sensor x points East, so with the
    # sensor's x arrow forward (mount FLU) the operator is facing compass 090.
    conv = OrientationConverter(ImuCalibration(mount="FLU"))
    assert_euler(conv.euler((1, 0, 0, 0)), (90, 0, 0))


def test_elementary_sensor_rotations_map_to_phase1_signs():
    conv = OrientationConverter(ImuCalibration(mount="FLU"))
    # Yaw: rotate about Up by +30 deg (CCW from above) from East -> heading 60 (CW from North).
    assert_euler(conv.euler(quat_from_axis_angle(U, math.radians(30))), (60, 0, 0))
    # Face North (yaw +90), then the nose goes up: rotation about the LEFT axis (+y) is nose-DOWN,
    # so nose-up 10 deg is -10 deg about the sensor y axis.
    q_n = quat_from_axis_angle(U, math.radians(90))
    from lynx.spatial import quat_multiply

    q = quat_multiply(q_n, quat_from_axis_angle(np.array([0, 1, 0]), math.radians(-10)))
    assert_euler(conv.euler(q), (0, 10, 0))
    # Roll: +20 deg about Forward lifts the left ear = right side down = +20 roll.
    q = quat_multiply(q_n, quat_from_axis_angle(np.array([1, 0, 0]), math.radians(20)))
    assert_euler(conv.euler(q), (0, 0, 20))


def test_line_of_sight_matches_phase1_formula():
    rng = random.Random(3)
    conv = OrientationConverter(ImuCalibration(mount="FLU"))
    for _ in range(100):
        q = np.array([rng.gauss(0, 1) for _ in range(4)])
        h, p, _ = conv.euler(q / np.linalg.norm(q))
        f = quat_to_matrix(conv.body_quat(q))[:, 0]
        hr, pr = math.radians(h), math.radians(p)
        assert f == pytest.approx([math.cos(pr) * math.sin(hr), math.cos(pr) * math.cos(hr), math.sin(pr)], abs=1e-9)


@pytest.mark.parametrize(
    "mount, axes",
    [
        # Head level, facing North (F = N, L = West, U = Up). Each row says where the sensor's
        # x, y, z axes physically point in ENU for that mount.
        ("FLU", (N, -E, U)),
        ("top-flat-x-right", (E, N, U)),  # x -> Right ear = East, y -> Forward = North
        ("left-side", (N, -U, -E)),  # x fwd, y down, z out of the left side = West
        ("right-side", (N, U, E)),
        ("rear", (E, U, -N)),  # x -> right ear, y up, z aft = South
    ],
)
def test_mount_presets_against_physical_axes(mount, axes):
    conv = OrientationConverter(ImuCalibration(mount=mount))
    assert_euler(conv.euler(sensor_quat_from_axes(*axes)), (0, 0, 0))


@pytest.mark.parametrize("mount", list(MOUNT_PRESETS) + ["FLU", "LBU", "DRB", "UFL"])
def test_inverse_model_roundtrip_random_poses(mount):
    rng = random.Random(hash(mount) & 0xFFFF)
    cal = ImuCalibration(mount=mount, declination_deg=-7.5, convergence_deg=1.2, trim_deg=(1.0, -0.5, 0.3))
    conv = OrientationConverter(cal)
    for _ in range(200):
        want = (rng.uniform(0, 360), rng.uniform(-85, 85), rng.uniform(-170, 170))
        q_ms = sensor_quat_for_body(euler_to_quat(*want), cal)
        assert_euler(conv.euler(q_ms), want, tol=1e-6)
        assert_euler(conv.euler(-q_ms), want, tol=1e-6)  # q and -q are the same rotation


def test_declination_and_convergence():
    # Sensor x (forward) pointing at magnetic North, level.
    q = sensor_quat_from_axes(N, -E, U)
    assert_euler(OrientationConverter(ImuCalibration(declination_deg=10.0)).euler(q), (10, 0, 0))
    assert_euler(OrientationConverter(ImuCalibration(declination_deg=-3.0)).euler(q), (357, 0, 0))
    assert_euler(OrientationConverter(ImuCalibration(declination_deg=10.0, convergence_deg=2.0)).euler(q), (8, 0, 0))


def test_heading_correction_leaves_pitch_roll_untouched():
    cal = ImuCalibration(declination_deg=25.0)
    plain = OrientationConverter(ImuCalibration())
    corr = OrientationConverter(cal)
    q = sensor_quat_for_body(euler_to_quat(100, -30, 15), ImuCalibration())
    h0, p0, r0 = plain.euler(q)
    h1, p1, r1 = corr.euler(q)
    assert ang_diff(h1, h0) == pytest.approx(25.0)
    assert (p1, r1) == pytest.approx((p0, r0))


def test_trim_adds_exactly_for_level_head():
    conv = OrientationConverter(ImuCalibration(trim_deg=(2.0, -1.0, 0.5)))
    assert_euler(conv.euler(sensor_quat_from_axes(N, -E, U)), (2.0, -1.0, 0.5))
    assert trim_matrix(0, 0, 0) == pytest.approx(np.eye(3))


def test_tare_to_datum_bearing_removes_sensor_heading_error():
    truth = ImuCalibration(mount="left-side", declination_deg=4.0)
    conv = OrientationConverter(ImuCalibration(mount="left-side", declination_deg=4.0))
    bias = 37.0  # e.g. local magnetic disturbance, or GRV arbitrary start heading
    facing = [sensor_quat_for_body(euler_to_quat(123.0, -2.0, 1.0), truth, bias) for _ in range(10)]
    assert ang_diff(conv.euler(facing[0])[0], 123.0) == pytest.approx(bias)
    offset = conv.tare(facing, datum_bearing_deg=123.0)
    assert offset == pytest.approx(-bias)
    assert conv.cal.tared
    assert_euler(conv.euler(facing[0]), (123.0, -2.0, 1.0), tol=1e-6)
    later = sensor_quat_for_body(euler_to_quat(200.0, 15.0, -5.0), truth, bias)
    assert_euler(conv.euler(later), (200.0, 15.0, -5.0), tol=1e-6)
    conv.clear_tare()
    assert not conv.cal.tared and ang_diff(conv.euler(later)[0], 200.0) == pytest.approx(bias)


def test_tare_is_wrap_safe_near_north():
    cal = ImuCalibration()
    conv = OrientationConverter(cal)
    samples = [sensor_quat_for_body(euler_to_quat(h, 0, 0), cal) for h in (359.5, 0.5, 359.8, 0.2)]
    conv.tare(samples, datum_bearing_deg=90.0)
    assert_euler(conv.euler(sensor_quat_for_body(euler_to_quat(0, 0, 0), cal)), (90, 0, 0), tol=1e-6)


def test_tare_rejects_motion_steep_pitch_and_empty():
    cal = ImuCalibration()
    conv = OrientationConverter(cal)
    with pytest.raises(TareError, match="moved"):
        conv.tare([sensor_quat_for_body(euler_to_quat(h, 0, 0), cal) for h in (0, 10, 20)], 0.0)
    with pytest.raises(TareError, match="pitched"):
        conv.tare([sensor_quat_for_body(euler_to_quat(0, 70, 0), cal)], 0.0)
    with pytest.raises(TareError):
        conv.tare([], 0.0)
    assert not conv.cal.tared


def test_tared_ping_lands_on_datum_bearing():
    """End to end: biased sensor, tare facing 045, pitch down 10 deg -> ping 9.64 m out on 045."""
    cal = ImuCalibration()
    conv = OrientationConverter(ImuCalibration())
    q = sensor_quat_for_body(euler_to_quat(45.0, -10.0, 0.0), cal, sensor_heading_error_deg=-20.0)
    conv.tare([q], 45.0)
    hit = raycast_from_pose(Pose(np.array([0.0, 0.0, 1.7]), quat_to_matrix(conv.body_quat(q))))
    d = 1.7 / math.tan(math.radians(10))
    assert hit.point == pytest.approx([d * math.sin(math.radians(45)), d * math.cos(math.radians(45)), 0.0], abs=1e-6)


@pytest.mark.parametrize("bad", ["FLD", "FFU", "XYZ", "FL", "ruf?"])
def test_mount_validation(bad):
    with pytest.raises(MountError):
        mount_matrix(bad)


def test_all_presets_are_proper_rotations():
    for name in MOUNT_PRESETS:
        R = mount_matrix(name)
        assert R @ R.T == pytest.approx(np.eye(3)) and np.linalg.det(R) == pytest.approx(1.0)


def test_calibration_json_roundtrip(tmp_path):
    cal = ImuCalibration(mount="left-side", trim_deg=(0.5, -0.25, 0.0), declination_deg=-12.3,
                         convergence_deg=0.8, heading_offset_deg=3.3, tared=True)
    path = tmp_path / "imu.json"
    cal.save(path)
    assert ImuCalibration.load(path) == cal
    with pytest.raises(ValueError):
        ImuCalibration.from_json('{"bogus": 1}')
    with pytest.raises(MountError):
        ImuCalibration.from_json('{"mount": "FLD"}')
