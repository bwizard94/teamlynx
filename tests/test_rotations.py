import math

import numpy as np
import pytest

from lynx.spatial import (
    euler_to_matrix,
    euler_to_quat,
    matrix_to_euler,
    matrix_to_quat,
    quat_angle_between,
    quat_conjugate,
    quat_from_axis_angle,
    quat_multiply,
    quat_normalize,
    quat_rotate,
    quat_to_euler,
    quat_to_matrix,
    rot_x,
    rot_y,
    rot_z,
    wrap_deg_180,
    wrap_deg_360,
)

RNG = np.random.default_rng(1234)


def random_euler(n=200, pitch_limit=89.0):
    return zip(
        RNG.uniform(0, 360, n),
        RNG.uniform(-pitch_limit, pitch_limit, n),
        RNG.uniform(-179.9, 179.9, n),
    )


def assert_rotation(R):
    np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-12)
    assert np.linalg.det(R) == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("fn,axis", [(rot_x, 0), (rot_y, 1), (rot_z, 2)])
def test_elementary_rotations_are_right_handed(fn, axis):
    R = fn(math.pi / 2)
    assert_rotation(R)
    e = np.eye(3)
    # Right-hand rule: rotating axis (i+1) by +90 deg about axis i yields axis (i+2).
    np.testing.assert_allclose(R @ e[(axis + 1) % 3], e[(axis + 2) % 3], atol=1e-12)


def test_wrap():
    assert wrap_deg_360(-10) == pytest.approx(350)
    assert wrap_deg_360(720) == 0
    assert wrap_deg_360(360) == 0
    assert wrap_deg_180(190) == pytest.approx(-170)
    assert wrap_deg_180(180) == pytest.approx(180)
    assert wrap_deg_180(-180) == pytest.approx(180)


@pytest.mark.parametrize(
    "heading,forward",
    [(0, (0, 1, 0)), (90, (1, 0, 0)), (180, (0, -1, 0)), (270, (-1, 0, 0)), (45, (math.sqrt(0.5), math.sqrt(0.5), 0))],
)
def test_heading_is_compass_clockwise_from_north(heading, forward):
    R = euler_to_matrix(heading, 0, 0)
    np.testing.assert_allclose(R[:, 0], forward, atol=1e-12)
    np.testing.assert_allclose(R[:, 2], (0, 0, 1), atol=1e-12)


def test_positive_pitch_is_nose_up():
    R = euler_to_matrix(0, 30, 0)
    np.testing.assert_allclose(R[:, 0], (0, math.cos(math.radians(30)), math.sin(math.radians(30))), atol=1e-12)


def test_positive_roll_is_right_side_down():
    R = euler_to_matrix(0, 0, 30)
    left_w = R[:, 1]
    assert left_w[2] > 0  # left ear goes up => right side down
    np.testing.assert_allclose(R[:, 0], (0, 1, 0), atol=1e-12)  # roll does not move line of sight


def test_zyx_composition_order():
    h, p, r = 30.0, 20.0, 10.0
    expected = rot_z(math.radians(90 - h)) @ rot_y(math.radians(-p)) @ rot_x(math.radians(r))
    np.testing.assert_allclose(euler_to_matrix(h, p, r), expected, atol=1e-12)


def test_euler_matrix_round_trip():
    for h, p, r in random_euler():
        R = euler_to_matrix(h, p, r)
        assert_rotation(R)
        h2, p2, r2 = matrix_to_euler(R)
        assert wrap_deg_180(h2 - h) == pytest.approx(0, abs=1e-8)
        assert p2 == pytest.approx(p, abs=1e-8)
        assert wrap_deg_180(r2 - r) == pytest.approx(0, abs=1e-8)


@pytest.mark.parametrize("pitch", [90.0, -90.0])
def test_gimbal_lock_reconstructs_same_rotation(pitch):
    R = euler_to_matrix(37.0, pitch, 25.0)
    h, p, r = matrix_to_euler(R)
    assert p == pytest.approx(pitch, abs=1e-6)
    assert r == 0.0
    np.testing.assert_allclose(euler_to_matrix(h, p, r), R, atol=1e-9)


def test_quaternion_matches_matrix_for_random_angles():
    for h, p, r in random_euler():
        q = euler_to_quat(h, p, r)
        assert np.linalg.norm(q) == pytest.approx(1.0)
        R = euler_to_matrix(h, p, r)
        np.testing.assert_allclose(quat_to_matrix(q), R, atol=1e-12)
        v = RNG.normal(size=3)
        np.testing.assert_allclose(quat_rotate(q, v), R @ v, atol=1e-12)
        h2, p2, r2 = quat_to_euler(q)
        assert wrap_deg_180(h2 - h) == pytest.approx(0, abs=1e-7)
        assert p2 == pytest.approx(p, abs=1e-7)


def test_matrix_to_quat_all_shepperd_branches():
    cases = [
        np.eye(3),
        rot_x(math.radians(179)),
        rot_y(math.radians(179)),
        rot_z(math.radians(179)),
        rot_x(math.pi) @ rot_y(0.1),
    ]
    for _ in range(100):
        q = quat_normalize(RNG.normal(size=4))
        cases.append(quat_to_matrix(q))
    for R in cases:
        q = matrix_to_quat(R)
        np.testing.assert_allclose(quat_to_matrix(q), R, atol=1e-10)


def test_quaternion_algebra():
    a = quat_normalize(RNG.normal(size=4))
    b = quat_normalize(RNG.normal(size=4))
    # Hamilton product composes like matrix product (apply b first).
    np.testing.assert_allclose(quat_to_matrix(quat_multiply(a, b)), quat_to_matrix(a) @ quat_to_matrix(b), atol=1e-12)
    ident = quat_multiply(a, quat_conjugate(a))
    np.testing.assert_allclose(ident, (1, 0, 0, 0), atol=1e-12)
    q = quat_from_axis_angle(np.array([0, 0, 1.0]), math.pi / 2)
    np.testing.assert_allclose(quat_rotate(q, np.array([1.0, 0, 0])), (0, 1, 0), atol=1e-12)
    assert quat_angle_between(q, np.array([1.0, 0, 0, 0])) == pytest.approx(math.pi / 2)
    assert quat_angle_between(a, -a) == pytest.approx(0.0, abs=1e-6)


def test_degenerate_inputs_raise():
    with pytest.raises(ValueError):
        quat_normalize(np.zeros(4))
    with pytest.raises(ValueError):
        quat_from_axis_angle(np.zeros(3), 1.0)
