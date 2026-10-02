import math

import numpy as np
import pytest

from lynx.spatial import (
    Camera,
    CameraMount,
    HitKind,
    Intrinsics,
    Pose,
    euler_to_matrix,
    intersect_ground,
    raycast,
    raycast_from_pose,
)

RNG = np.random.default_rng(7)


@pytest.mark.parametrize("pitch", [-5.0, -10.0, -30.0, -60.0, -89.0])
def test_ground_hit_distance_matches_trig(pitch):
    h = 1.7
    hit = raycast_from_pose(Pose.from_euler(0, 0, h, 0, pitch, 0))
    assert hit.kind is HitKind.GROUND
    a = math.radians(-pitch)
    assert hit.point[1] == pytest.approx(h / math.tan(a), rel=1e-12)
    assert hit.distance == pytest.approx(h / math.sin(a), rel=1e-12)
    assert hit.point[2] == 0.0


def test_heading_rotates_hit_point():
    hit = raycast_from_pose(Pose.from_euler(10, 20, 2.0, 90, -45, 0))
    np.testing.assert_allclose(hit.point, [12.0, 20.0, 0.0], atol=1e-12)


@pytest.mark.parametrize("pitch", [0.0, 15.0, 89.0])
def test_level_or_up_falls_back_to_fixed_range(pitch):
    pose = Pose.from_euler(0, 0, 1.7, 45, pitch, 0)
    hit = raycast_from_pose(pose, fallback_range=50.0)
    assert hit.kind is HitKind.MAX_RANGE
    assert hit.distance == 50.0
    np.testing.assert_allclose(hit.point, pose.position + 50.0 * pose.forward, atol=1e-12)


def test_hit_beyond_max_range_falls_back():
    hit = raycast(np.array([0, 0, 1.7]), np.array([0, 1, -0.001]), max_range=150.0, fallback_range=40.0)
    assert hit.kind is HitKind.MAX_RANGE
    assert hit.distance == pytest.approx(40.0)


def test_fallback_never_exceeds_max_range():
    hit = raycast(np.array([0, 0, 1.7]), np.array([0, 1, 0]), max_range=20.0, fallback_range=50.0)
    assert hit.distance == 20.0


def test_origin_below_ground_looking_up_hits_from_below():
    t = intersect_ground(np.array([0, 0, -1.0]), np.array([0, 0, 1.0]))
    assert t == pytest.approx(1.0)


def test_intersect_ground_parallel_and_behind():
    assert intersect_ground(np.array([0, 0, 1.0]), np.array([1, 0, 0.0])) is None
    assert intersect_ground(np.array([0, 0, 1.0]), np.array([0, 0, 1.0])) is None


def test_raised_ground_plane():
    hit = raycast(np.array([0, 0, 5.0]), np.array([0, 1.0, -1.0]), ground_z=2.0)
    np.testing.assert_allclose(hit.point, [0, 3, 2], atol=1e-12)


def test_invalid_rays():
    with pytest.raises(ValueError):
        raycast(np.zeros(3), np.zeros(3))
    with pytest.raises(ValueError):
        raycast(np.zeros(3), np.array([0, 0, -1.0]), max_range=0)


def test_ping_reprojects_to_reticle_for_random_poses_and_mounts():
    """A ping raycast from the optical axis must re-project to (cx, cy) for its creator."""
    K = Intrinsics.from_fov(1280, 720, 100.0, 62.0)
    for _ in range(300):
        pose = Pose.from_euler(*RNG.uniform(-100, 100, 2), RNG.uniform(0.3, 10), RNG.uniform(0, 360),
                               RNG.uniform(-85, 30), RNG.uniform(-60, 60))
        mount = CameraMount(euler_to_matrix(90 + RNG.normal(), RNG.normal(), RNG.normal()),
                            RNG.normal(size=3) * 0.05)
        hit = raycast_from_pose(pose, mount)
        p = Camera(K, pose, mount).project(hit.point)
        assert p.on_screen
        assert (p.u, p.v) == pytest.approx((K.cx, K.cy), abs=1e-6)
        assert p.distance == pytest.approx(hit.distance, rel=1e-9)
