import math

import numpy as np
import pytest

from lynx.spatial import (
    R_CB,
    Camera,
    CameraMount,
    CameraPose,
    Intrinsics,
    Pose,
    Visibility,
    clamp_direction_to_rect,
    euler_to_matrix,
    rot_z,
    screen_angle_deg,
)

RNG = np.random.default_rng(42)
W, H = 960, 600


def level_cam(heading=0.0, pitch=0.0, roll=0.0, pos=(0, 0, 0), hfov=90.0, vfov=None):
    return Camera(Intrinsics.from_fov(W, H, hfov, vfov), Pose.from_euler(*pos, heading, pitch, roll))


# -- intrinsics ------------------------------------------------------------------------------


def test_intrinsics_from_hfov_square_pixels():
    K = Intrinsics.from_fov(W, H, 90.0)
    assert K.fx == pytest.approx(W / 2)  # tan(45) = 1
    assert K.fy == K.fx
    assert (K.cx, K.cy) == (W / 2, H / 2)
    assert K.hfov_deg == pytest.approx(90.0)
    assert K.vfov_deg == pytest.approx(math.degrees(2 * math.atan(H / W)))


def test_intrinsics_from_hfov_and_vfov():
    K = Intrinsics.from_fov(1280, 720, 100.0, 60.0)
    assert K.hfov_deg == pytest.approx(100.0)
    assert K.vfov_deg == pytest.approx(60.0)
    assert K.fy == pytest.approx(360 / math.tan(math.radians(30)))
    np.testing.assert_allclose(K.K, [[K.fx, 0, 640], [0, K.fy, 360], [0, 0, 1]])


@pytest.mark.parametrize("bad", [dict(width=0), dict(hfov_deg=0), dict(hfov_deg=180), dict(vfov_deg=200)])
def test_intrinsics_validation(bad):
    kw = dict(width=W, height=H, hfov_deg=90.0, vfov_deg=None)
    kw.update(bad)
    with pytest.raises(ValueError):
        Intrinsics.from_fov(**kw)


def test_pixel_ray_round_trip():
    K = Intrinsics.from_fov(W, H, 75.0, 50.0)
    for u, v in RNG.uniform([0, 0], [W, H], size=(50, 2)):
        d = K.pixel_to_ray(u, v)
        assert np.linalg.norm(d) == pytest.approx(1.0)
        u2, v2 = K.project_camera_point(d * 7.3)
        assert (u2, v2) == pytest.approx((u, v), abs=1e-9)


# -- frames ----------------------------------------------------------------------------------


def test_body_to_camera_axes():
    np.testing.assert_array_equal(R_CB @ [1, 0, 0], [0, 0, 1])  # forward -> optical axis
    np.testing.assert_array_equal(R_CB @ [0, 1, 0], [-1, 0, 0])  # left -> -x (image left)
    np.testing.assert_array_equal(R_CB @ [0, 0, 1], [0, -1, 0])  # up -> -y (image up)
    assert np.linalg.det(R_CB) == pytest.approx(1.0)


def test_camera_pose_inverse():
    pose = Pose.from_euler(3, -4, 1.7, 123, -12, 7)
    cp = CameraPose.from_body_pose(pose, CameraMount(rot_z(0.05), np.array([0.1, 0.02, 0.05])))
    pts = RNG.normal(size=(20, 3)) * 30
    np.testing.assert_allclose(cp.camera_to_world(cp.world_to_camera(pts)), pts, atol=1e-10)


# -- known geometry --------------------------------------------------------------------------


def test_point_straight_ahead_projects_to_centre():
    cam = level_cam(heading=90, pos=(0, 0, 1.7))
    p = cam.project([25, 0, 1.7])
    assert p.visibility is Visibility.ON_SCREEN
    assert (p.u, p.v) == pytest.approx((W / 2, H / 2))
    assert p.depth == pytest.approx(25)
    assert p.distance == pytest.approx(25)
    assert p.bearing_deg == pytest.approx(0)


def test_point_at_half_hfov_lands_on_image_edge():
    cam = level_cam(heading=0, hfov=90)
    # 45 deg right of North at same height -> exactly at u = W (just off-screen, half-open range)
    p = cam.project([10, 10, 0])
    assert p.u == pytest.approx(W)
    assert p.bearing_deg == pytest.approx(45)
    p = cam.project([-10 + 1e-6, 10, 0])
    assert p.visibility is Visibility.ON_SCREEN
    assert p.u == pytest.approx(0, abs=1e-3)


def test_level_camera_trig_formula():
    """u = cx + fx tan(b), v = cy - fy dz / (r cos b) for a level camera."""
    cam = level_cam(heading=30, pos=(5, -2, 1.7))
    K = cam.intrinsics
    for b_deg, r, dz in [(10, 20, -1.7), (-25, 8, 0.5), (40, 50, 3.0)]:
        b = math.radians(30 + b_deg)
        target = np.array([5 + r * math.sin(b), -2 + r * math.cos(b), 1.7 + dz])
        p = cam.project(target)
        assert p.u == pytest.approx(K.cx + K.fx * math.tan(math.radians(b_deg)), abs=1e-9)
        assert p.v == pytest.approx(K.cy - K.fy * dz / (r * math.cos(math.radians(b_deg))), abs=1e-9)
        assert p.bearing_deg == pytest.approx(b_deg)


def test_pitch_moves_target_down_in_image():
    target = [0, 20, 0]
    up = level_cam(pitch=10, pos=(0, 0, 1.7)).project(target)
    down = level_cam(pitch=-10, pos=(0, 0, 1.7)).project(target)
    assert up.v > down.v
    # pitching down by atan(1.7/20) centres the ground target
    p = level_cam(pitch=-math.degrees(math.atan2(1.7, 20)), pos=(0, 0, 1.7)).project(target)
    assert (p.u, p.v) == pytest.approx((W / 2, H / 2), abs=1e-9)


def test_roll_rotates_image_about_centre():
    target = np.array([3.0, 20.0, 0.0])
    p0 = level_cam(pos=(0, 0, 0)).project(target)
    p1 = level_cam(roll=30, pos=(0, 0, 0)).project(target)
    # Rolling the head right-side-down rotates the world counter-clockwise on screen by 30 deg
    # (square pixels so rotation is isotropic).
    a0 = math.atan2(p0.v - H / 2, p0.u - W / 2)
    a1 = math.atan2(p1.v - H / 2, p1.u - W / 2)
    assert math.degrees(a0 - a1) == pytest.approx(30.0, abs=1e-9)
    assert math.hypot(p0.u - W / 2, p0.v - H / 2) == pytest.approx(math.hypot(p1.u - W / 2, p1.v - H / 2))


def test_projection_back_projection_round_trip_random_poses():
    K = Intrinsics.from_fov(W, H, 85.0, 58.0)
    for _ in range(300):
        pose = Pose.from_euler(*RNG.uniform(-50, 50, 2), RNG.uniform(0.5, 3), RNG.uniform(0, 360),
                               RNG.uniform(-60, 60), RNG.uniform(-45, 45))
        mount = CameraMount(euler_to_matrix(90 + RNG.normal() * 2, RNG.normal() * 2, RNG.normal() * 2),
                            RNG.normal(size=3) * 0.05)  # heading 90 == identity: small misalignment
        cam = Camera(K, pose, mount)
        u, v = RNG.uniform([0, 0], [W, H])
        depth = RNG.uniform(1, 200)
        o, d = cam.pixel_to_world_ray(u, v)
        z_axis = cam.camera_pose.optical_axis_w
        target = o + d * depth / float(d @ z_axis)  # point at the given optical depth
        p = cam.project(target)
        assert p.visibility is Visibility.ON_SCREEN
        assert (p.u, p.v) == pytest.approx((u, v), abs=1e-6)
        assert p.depth == pytest.approx(depth, rel=1e-9)


# -- off-screen / behind ---------------------------------------------------------------------


def test_off_screen_right_is_clamped_to_right_edge():
    cam = level_cam()
    p = cam.project([30, 10, 0], margin=20)
    assert p.visibility is Visibility.OFF_SCREEN
    assert p.u == pytest.approx(W - 20)
    assert 20 <= p.v <= H - 20
    assert p.edge_angle_deg == pytest.approx(90)


def test_off_screen_above_is_clamped_to_top():
    cam = level_cam()
    p = cam.project([0, 10, 30], margin=10)
    assert p.visibility is Visibility.OFF_SCREEN
    assert p.v == pytest.approx(10)
    assert p.u == pytest.approx(W / 2)
    assert p.edge_angle_deg == pytest.approx(0)


@pytest.mark.parametrize("x,expected_side", [(5.0, "right"), (-5.0, "left")])
def test_behind_targets_go_to_turn_side_edge(x, expected_side):
    cam = level_cam(pos=(0, 0, 1.7))
    p = cam.project([x, -20, 0], margin=15)
    assert p.visibility is Visibility.BEHIND
    assert p.depth < 0
    if expected_side == "right":
        assert p.u == pytest.approx(W - 15)
        assert p.bearing_deg > 90
    else:
        assert p.u == pytest.approx(15)
        assert p.bearing_deg < -90
    assert H / 2 < p.v <= H - 15  # below eye level -> lower half


def test_dead_astern_uses_bearing_side():
    cam = level_cam(pos=(0, 0, 0))
    p = cam.project([0, -10, 0])
    assert p.visibility is Visibility.BEHIND
    assert p.u in (pytest.approx(0), pytest.approx(W))
    assert p.v == pytest.approx(H / 2)


def test_indicator_continuous_across_image_plane():
    """Edge position must not jump when a target crosses the near plane side-on."""
    cam = level_cam(pos=(0, 0, 1.7))
    eps = 1e-7
    a = cam.project([10, cam.near + eps, 0.0], margin=10)
    b = cam.project([10, cam.near - eps, 0.0], margin=10)
    assert a.visibility is Visibility.OFF_SCREEN and b.visibility is Visibility.BEHIND
    assert (a.u, a.v) == pytest.approx((b.u, b.v), abs=0.05)


def test_clamp_direction_to_rect():
    assert clamp_direction_to_rect(50, 50, 1, 0, 100, 100, 0) == pytest.approx((100, 50))
    assert clamp_direction_to_rect(50, 50, 0, -1, 100, 100, 5) == pytest.approx((50, 5))
    assert clamp_direction_to_rect(50, 50, 1, 1, 100, 100, 0) == pytest.approx((100, 100))
    assert clamp_direction_to_rect(50, 25, 2, 1, 100, 50, 0) == pytest.approx((100, 50))
    assert clamp_direction_to_rect(50, 50, 0, 0, 100, 100, 0) == pytest.approx((50, 100))


def test_screen_angle():
    assert screen_angle_deg(0, -1) == pytest.approx(0)
    assert screen_angle_deg(1, 0) == pytest.approx(90)
    assert screen_angle_deg(0, 1) == pytest.approx(180)
    assert screen_angle_deg(-1, 0) == pytest.approx(270)


# -- segments & ground region ----------------------------------------------------------------


def test_segment_clipping_against_near_plane():
    cam = level_cam(pos=(0, 0, 1.7))
    assert cam.project_segment([0, -5, 0], [3, -10, 0]) is None  # fully behind
    seg = cam.project_segment([1, -10, 0], [1, 10, 0])  # crosses the camera plane
    assert seg is not None
    (u0, v0), (u1, v1) = seg
    exact = cam.project([1, 10, 0])
    assert (u1, v1) == pytest.approx((exact.u, exact.v))
    # clipped endpoint lies on the same image line (vanishing direction) and below the horizon
    assert v0 > H / 2 and u0 > W / 2


def test_ground_polygon_horizon_for_level_camera():
    cam = level_cam(pos=(0, 0, 1.7))
    poly = cam.ground_polygon()
    vs = sorted(v for _, v in poly)
    assert vs[0] == pytest.approx(H / 2)
    assert max(vs) == pytest.approx(H)
    assert len(poly) == 4


def test_ground_polygon_full_and_empty():
    assert len(level_cam(pitch=-89, pos=(0, 0, 1.7)).ground_polygon()) == 4  # looking straight down
    assert level_cam(pitch=89, pos=(0, 0, 1.7)).ground_polygon() == []  # looking straight up


def test_ground_polygon_rolled_horizon_passes_through_centre():
    cam = level_cam(roll=20, pos=(0, 0, 1.7))
    poly = cam.ground_polygon()
    # horizon vertices: where the viewing ray is horizontal; with zero pitch it passes the centre
    horizon = [p for p in poly if abs(cam.pixel_to_world_ray(*p)[1][2]) < 1e-9]
    assert len(horizon) == 2
    (ua, va), (ub, vb) = horizon
    t = (W / 2 - ua) / (ub - ua)
    assert va + t * (vb - va) == pytest.approx(H / 2)
    assert math.degrees(math.atan2(vb - va, ub - ua)) % 180 == pytest.approx(180 - 20, abs=1e-6)


def test_camera_mount_lever_arm_shifts_projection():
    pose = Pose.from_euler(0, 0, 1.7, 0, 0, 0)
    cam = Camera(Intrinsics.from_fov(W, H, 90), pose, CameraMount(t_b=np.array([0.0, 0.0, 0.1])))
    # camera is 10 cm above the eye: a target at eye height appears slightly below centre
    p = cam.project([0, 10, 1.7])
    assert p.v == pytest.approx(H / 2 + (W / 2) * 0.1 / 10)


def test_near_plane_validation():
    with pytest.raises(ValueError):
        Camera(Intrinsics.from_fov(W, H, 90), near=0)
