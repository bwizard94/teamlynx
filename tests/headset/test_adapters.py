"""The vision/HUD types are views onto lynx.spatial and lynx.net: check they agree exactly."""

import math

import numpy as np
import pytest

from lynx.headset import adapters
from lynx.hud.renderer import HudRenderer
from lynx.hud.types import HudState, WorldPing
from lynx.net.schema import Ping, PingType, Team, Telemetry
from lynx.net.state import NodeState, PingState
from lynx.spatial import Camera, CameraPose, Intrinsics, Pose, compass_bearing_deg
from lynx.vision.geometry import (
    edge_arrow_position,
    pixel_to_angles,
    project_point,
    rotation_world_to_camera,
    world_bearing_deg,
)
from lynx.vision.types import CameraModel, OperatorPose

RNG = np.random.default_rng(7)
POSES = [
    OperatorPose(*RNG.uniform(-30, 30, 2), 1.7, *RNG.uniform(0, 360, 1), *RNG.uniform(-30, 30, 2))
    for _ in range(12)
]


def test_camera_model_is_spatial_intrinsics():
    cam = CameraModel(1280, 720, 78.0)
    ref = Intrinsics.from_fov(1280, 720, 78.0)
    assert cam.intrinsics == ref
    assert (cam.fx, cam.fy, cam.cx, cam.cy) == (ref.fx, ref.fy, ref.cx, ref.cy)
    assert cam.vfov == pytest.approx(ref.vfov_deg)
    assert CameraModel.from_intrinsics(ref) == cam
    aniso = Intrinsics.from_fov(640, 480, 90.0, 60.0)
    assert CameraModel.from_intrinsics(aniso).intrinsics.fy == pytest.approx(aniso.fy)


@pytest.mark.parametrize("op", POSES)
def test_operator_pose_roundtrips_through_spatial_pose(op):
    back = OperatorPose.from_pose(op.spatial_pose, op.node_id, op.callsign)
    assert back.position == pytest.approx(op.position)
    assert math.cos(math.radians(back.yaw - op.yaw)) == pytest.approx(1.0)
    assert (back.pitch, back.roll) == pytest.approx((op.pitch, op.roll))


def phase2_closed_form_R_cw(yaw, pitch, roll):
    """The rotation Phase 2 derived independently (docs/vision-pipeline.md)."""
    psi, theta, phi = (math.radians(a) for a in (yaw, pitch, roll))
    sp, cp, st, ct = math.sin(psi), math.cos(psi), math.sin(theta), math.cos(theta)
    sr, cr = math.sin(phi), math.cos(phi)
    f = np.array([sp * ct, cp * ct, st])
    r0 = np.array([cp, -sp, 0.0])
    u0 = np.array([-sp * st, -cp * st, ct])
    r = r0 * cr - u0 * sr
    u = u0 * cr + r0 * sr
    return np.stack([r, -u, f])


@pytest.mark.parametrize("op", POSES)
def test_rotation_matches_spatial_camera_pose(op):
    R = rotation_world_to_camera(op.yaw, op.pitch, op.roll)
    assert np.allclose(R, CameraPose.from_body_pose(op.spatial_pose).R_cw, atol=1e-12)


def test_phase2_angle_convention_is_phase1_convention():
    for yaw, pitch, roll in np.random.default_rng(3).uniform([-360, -89, -179], [360, 89, 179], (2000, 3)):
        assert np.allclose(phase2_closed_form_R_cw(yaw, pitch, roll), rotation_world_to_camera(yaw, pitch, roll),
                           atol=1e-12)


@pytest.mark.parametrize("op", POSES)
def test_project_point_matches_spatial_camera(op):
    cam = CameraModel(960, 540, 84.0)
    view = Camera(cam.intrinsics, op.spatial_pose)
    for p in RNG.uniform(-60, 60, (40, 3)):
        mine = project_point(op, cam, p)
        ref = view.project(p)
        assert mine.cam_xyz == pytest.approx(ref.p_cam, abs=1e-9)
        assert mine.in_view == ref.on_screen
        if ref.on_screen:
            assert mine.pixel == pytest.approx((ref.u, ref.v), abs=1e-9)
            assert pixel_to_angles(cam, *mine.pixel) == pytest.approx((mine.azimuth_deg, mine.elevation_deg))
        assert world_bearing_deg(op, p) == pytest.approx(compass_bearing_deg(op.position, p))


@pytest.mark.parametrize("op", POSES[:4])
def test_edge_arrows_match_spatial_off_screen_indicator(op):
    cam = CameraModel(1280, 720, 78.0)
    view = Camera(cam.intrinsics, op.spatial_pose)
    for p in RNG.uniform(-60, 60, (60, 3)):
        ref = view.project(p, margin=40.0)
        if ref.on_screen:
            continue
        (u, v), ang = edge_arrow_position(cam, ref.p_cam, margin=40.0)
        if abs(ref.p_cam[0]) > 1e-6:  # side_hint only differs for points dead behind
            assert (u, v) == pytest.approx((ref.u, ref.v), abs=1e-6)
            assert math.degrees(ang) % 360 == pytest.approx((ref.edge_angle_deg - 90.0) % 360, abs=1e-6)


def test_hud_places_world_ping_at_spatial_projection():
    op = OperatorPose(3.0, -2.0, 1.7, 20.0, -5.0, 3.0, node_id=1)
    cam = CameraModel(1280, 720, 78.0)
    ping = WorldPing(1, 8.0, 25.0, 0.0, "MARK")
    hud = HudRenderer()
    hud.render(np.zeros((720, 1280, 3), np.uint8), HudState(op, cam, pings=[ping]))
    ref = Camera(cam.intrinsics, op.spatial_pose).project((ping.x, ping.y, ping.z))
    assert ref.on_screen
    (rp,) = hud.last_pings
    assert rp.on_screen and (rp.u, rp.v) == pytest.approx((ref.u, ref.v))


def test_teammate_and_ping_adapters():
    tel = Telemetry(node_id=4, team=Team.GREEN, callsign="VIPER", x=5.0, y=30.0, z=1.6, heading=270.0)
    tm = adapters.teammate_from_node(NodeState(tel, last_seen=12.5))
    assert (tm.node_id, tm.callsign, tm.team_color, tm.timestamp, tm.yaw) == (4, "VIPER", "green", 12.5, 270.0)
    assert tm.position == pytest.approx((5.0, 30.0, 1.6))

    nodes = {4: NodeState(tel, 12.5), 1: NodeState(Telemetry(node_id=1, callsign="ME"), 12.0)}
    assert [t.node_id for t in adapters.teammates_from_nodes(nodes, self_node=1)] == [4]

    p1 = Ping(node_id=4, ping_id=7, owner=4, ping_type=PingType.DANGER, x=1, y=2, z=0)
    p2 = Ping(node_id=1, ping_id=7, owner=1, ping_type=PingType.MARK, x=3, y=4, z=0)
    pings = {p1.key: PingState(p1, 1.0, 61.0), p2.key: PingState(p2, 2.0, 62.0)}
    wp = adapters.world_pings_from_state(pings, nodes, self_node=1, self_callsign="ALPHA")
    assert [(w.label, w.owner) for w in wp] == [("DANGER", "VIPER"), ("MARK", "ALPHA")]
    assert len({w.ping_id for w in wp}) == 2  # same ping_id, different owners
    assert wp[0].color == adapters.PING_COLOR_BGR[PingType.DANGER] and wp[1].color is None


def test_operator_pose_adapter_carries_identity():
    op = adapters.operator_pose(Pose.from_euler(1, 2, 1.7, 90, 0, 0), 3, "CHARLIE", Team.BLUE)
    assert (op.node_id, op.callsign, op.team_color) == (3, "CHARLIE", "blue")
    assert op.yaw == pytest.approx(90.0)
