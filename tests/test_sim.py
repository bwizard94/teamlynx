import math
import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
pygame = pytest.importorskip("pygame")

from lynx.net.schema import Ping, PingType, Team, Telemetry  # noqa: E402
from lynx.net.state import NodeState, PingState  # noqa: E402
from lynx.sim.operator import ControlInput, SimOperator  # noqa: E402
from lynx.sim.render import PING_RGB, Renderer, ViewModel  # noqa: E402
from lynx.sim.selfcheck import run_selfcheck  # noqa: E402
from lynx.spatial import Visibility, raycast_from_pose  # noqa: E402


def test_operator_walks_along_heading():
    op = SimOperator(heading=90.0)
    op.step(1.0, ControlInput(forward=1.0))
    assert (op.x, op.y) == pytest.approx((op.walk_speed, 0.0))
    op = SimOperator(heading=0.0)
    op.step(1.0, ControlInput(strafe=1.0))
    assert (op.x, op.y) == pytest.approx((op.walk_speed, 0.0))  # strafe right from North = East


def test_operator_diagonal_is_normalised_and_limits_hold():
    op = SimOperator()
    op.step(1.0, ControlInput(forward=1, strafe=1))
    assert math.hypot(op.x, op.y) == pytest.approx(op.walk_speed)
    op.step(10.0, ControlInput(pitch=1, roll=-1, climb=-1, turn=-1))
    assert op.pitch == op.pitch_limit
    assert op.roll == -op.roll_limit
    assert op.z == op.min_eye_height
    assert 0 <= op.heading < 360


def test_operator_pose_matches_euler():
    op = SimOperator(x=1, y=2, z=1.7, heading=200, pitch=-15, roll=5)
    h, p, r = op.pose.euler
    assert (h, p, r) == pytest.approx((200, -15, 5))


def _vm(pose, nodes=None, pings=None):
    return ViewModel(1, "ALPHA", Team.BLUE, pose, nodes or {}, pings or {}, now=0.0)


def test_renderer_draws_ping_at_projection_and_offscreen_indicator():
    r = Renderer(640, 400, 90)
    surf = pygame.Surface((640, 400))
    op = SimOperator(heading=0, pitch=-10)
    hit = raycast_from_pose(op.pose)
    ping = Ping(node_id=1, owner=1, ping_id=1, ping_type=PingType.CONTACT, x=hit.point[0], y=hit.point[1], z=0.0)
    behind = Ping(node_id=1, owner=1, ping_id=2, ping_type=PingType.RALLY, x=0.0, y=-30.0, z=0.0)
    mate = Telemetry(node_id=2, team=Team.GREEN, callsign="BRAVO", x=-5, y=20, z=1.7)
    pings = {ping.key: PingState(ping, 0, 60), behind.key: PingState(behind, 0, 60)}
    report = r.render(surf, _vm(op.pose, {2: NodeState(mate, 0)}, pings))
    p = report.pings[ping.key]
    assert p.on_screen and (p.u, p.v) == pytest.approx((320, 200))
    assert report.pings[behind.key].visibility is Visibility.BEHIND
    assert report.nodes[2].on_screen
    col = PING_RGB[PingType.CONTACT]
    # chevrons are drawn directly above the anchor along the image column
    column = [tuple(surf.get_at((320, y)))[:3] for y in range(150, 200)]
    assert col in column


def test_renderer_handles_extreme_poses():
    r = Renderer(320, 240, 100)
    surf = pygame.Surface((320, 240))
    for pitch, roll in [(85, 0), (-85, 0), (0, 60), (-40, -60)]:
        op = SimOperator(heading=33, pitch=pitch, roll=roll)
        r.render(surf, _vm(op.pose))


async def test_headless_selfcheck_passes(tmp_path):
    ck = await run_selfcheck(save_dir=str(tmp_path), verbose=False)
    failed = [f"{r.name}: {r.detail}" for r in ck.results if not r.ok]
    assert not failed, failed
    assert (tmp_path / "views-composite.png").exists()
    assert len(ck.results) >= 20
