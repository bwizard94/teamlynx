import math

import pytest

from lynx.vision.geometry import (
    edge_arrow_position,
    pixel_to_angles,
    project_point,
    rotation_world_to_camera,
    world_bearing_deg,
)
from lynx.vision.types import CameraModel, OperatorPose

CAM = CameraModel(1280, 720, 90.0)


def pose(yaw=0.0, pitch=0.0, roll=0.0):
    return OperatorPose(0.0, 0.0, 0.0, yaw, pitch, roll)


def test_rotation_is_orthonormal():
    import numpy as np

    R = rotation_world_to_camera(37.0, -12.0, 8.0)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-12)
    assert np.isclose(np.linalg.det(R), 1.0)


@pytest.mark.parametrize("yaw,point", [(0, (0, 10, 0)), (90, (10, 0, 0)), (180, (0, -10, 0)), (270, (-10, 0, 0))])
def test_boresight_projects_to_principal_point(yaw, point):
    p = project_point(pose(yaw), CAM, point)
    assert p.in_view
    assert p.pixel == pytest.approx((640.0, 360.0), abs=1e-6)
    assert p.range_m == pytest.approx(10.0)


def test_right_and_up_map_to_image_axes():
    right = project_point(pose(), CAM, (10, 10, 0))  # 45 deg right == HFOV/2
    assert right.azimuth_deg == pytest.approx(45.0)
    assert right.pixel[0] == pytest.approx(1280.0)
    up = project_point(pose(), CAM, (0, 10, 1))
    assert up.pixel[1] < 360 and up.elevation_deg > 0


def test_behind_has_no_pixel():
    p = project_point(pose(), CAM, (0, -10, 0))
    assert p.pixel is None and not p.in_front and abs(p.azimuth_deg) == pytest.approx(180.0)


def test_pitch_up_moves_horizon_down():
    p = project_point(pose(pitch=10), CAM, (0, 100, 0))
    assert p.pixel[1] > 360
    assert p.elevation_deg == pytest.approx(-10.0)


def test_roll_right_raises_right_side_targets():
    # Rolling right-side-down rotates the scene counter-clockwise in the image,
    # so a target to the right on the horizon appears above the centre row.
    p = project_point(pose(roll=10), CAM, (5, 10, 0))
    assert p.pixel[0] > 640 and p.pixel[1] < 360


def test_pixel_to_angles_inverts_projection():
    for pt in [(3, 20, 2), (-7, 15, -1), (1, 5, 0.5)]:
        p = project_point(pose(), CAM, pt)
        az, el = pixel_to_angles(CAM, *p.pixel)
        assert az == pytest.approx(p.azimuth_deg, abs=1e-9)
        assert el == pytest.approx(p.elevation_deg, abs=1e-9)


def test_world_bearing_clockwise_from_north():
    o = pose()
    assert world_bearing_deg(o, (0, 10, 0)) == pytest.approx(0.0)
    assert world_bearing_deg(o, (10, 0, 0)) == pytest.approx(90.0)
    assert world_bearing_deg(o, (-10, 0, 0)) == pytest.approx(270.0)


def test_edge_arrow_for_target_behind_left_points_left():
    p = project_point(pose(), CAM, (-5, -10, 0))
    (x, y), ang = edge_arrow_position(CAM, p.cam_xyz, margin=40)
    assert x == pytest.approx(40.0)
    assert abs(math.degrees(ang)) == pytest.approx(180.0, abs=1e-6)
    assert 40 <= y <= 680
