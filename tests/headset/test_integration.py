"""End to end: relay + two headless headset clients on the synthetic camera.

ALPHA stands at the datum looking North; BRAVO stands 20 m North looking back South, pitched
10 deg down. Each must label the other FRIENDLY with the right callsign purely from relay
telemetry, and a ping BRAVO drops with its keyboard trigger must render on ALPHA's HUD as a
chevron at the pixel the pinhole model predicts.
"""

import math
import time

import numpy as np
import pytest

from lynx.headset.app import HeadsetClient, HeadsetConfig, main
from lynx.headset.pose import KeyboardPoseSource
from lynx.headset.sources import SyntheticSource
from lynx.hud.style import HudStyle
from lynx.net.schema import Team
from lynx.spatial import Camera, HitKind, Pose
from lynx.vision.types import IffStatus

W, H, HFOV, EYE = 640, 360, 78.0, 1.7


def headset(url, node, callsign, x, y, heading, pitch):
    cfg = HeadsetConfig(url=url, node_id=node, callsign=callsign, team=Team.BLUE, telemetry_hz=30.0, pip="topdown")
    cam = SyntheticSource(W, H, HFOV, seed=node, unknowns=False, det_dropout=0.0)
    pose = KeyboardPoseSource(Pose.from_euler(x, y, EYE, heading, pitch, 0.0))
    client = HeadsetClient(cfg, cam, pose).start()
    assert client.wait_connected(5.0)
    return client


def run_until(clients, pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in clients:
            c.step()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def friendly(client, callsign):
    r = client.last_result
    return r is not None and any(t.status == IffStatus.FRIENDLY and t.callsign == callsign for t in r.tracks)


@pytest.fixture
def squad(relay):
    alpha = headset(relay.url, 1, "ALPHA", 0.0, 0.0, 0.0, 0.0)
    bravo = headset(relay.url, 2, "BRAVO", 0.0, 20.0, 180.0, -10.0)
    yield alpha, bravo
    alpha.stop()
    bravo.stop()


def test_two_headsets_iff_and_shared_ping(squad):
    alpha, bravo = squad

    assert run_until(squad, lambda: friendly(alpha, "BRAVO") and friendly(bravo, "ALPHA")), (
        f"ALPHA tracks {alpha.last_result.tracks}, BRAVO tracks {bravo.last_result.tracks}"
    )
    (tr,) = [t for t in alpha.last_result.tracks if t.status == IffStatus.FRIENDLY]
    assert (tr.node_id, tr.callsign, tr.team_color) == (2, "BRAVO", "blue")
    assert tr.range_m == pytest.approx(20.0, abs=1.5)
    assert not any(t.status == IffStatus.TANGO for c in squad for t in c.last_result.tracks)
    assert [tm.callsign for tm in alpha.last_teammates] == ["BRAVO"]

    # BRAVO presses the ping key; the raycast from 1.7 m at -10 deg hits the ground 9.64 m out.
    assert bravo.pose_source.handle_key(ord(" "))
    assert run_until(squad, lambda: len(alpha.last_pings) == 1)
    hit = bravo.last_hit
    assert hit.kind is HitKind.GROUND
    d_ground = EYE / math.tan(math.radians(10.0))
    assert hit.point == pytest.approx((0.0, 20.0 - d_ground, 0.0), abs=1e-6)

    (wp,) = alpha.last_pings
    assert (wp.label, wp.owner) == ("MARK", "BRAVO")
    assert (wp.x, wp.y, wp.z) == pytest.approx(tuple(hit.point), abs=1e-4)  # float32 on the wire

    # Independent pinhole check: ALPHA looks level due North, so the ping is on the vertical
    # centre line, EYE metres below the optical axis at depth y.
    intr = alpha.last_state.camera.intrinsics
    v_expected = intr.cy + intr.fy * EYE / hit.point[1]
    ref = Camera(intr, alpha.last_pose.spatial_pose).project(hit.point)
    assert (ref.u, ref.v) == pytest.approx((W / 2, v_expected), abs=1e-3)

    (rp,) = alpha.renderer.last_pings
    assert rp.on_screen and rp.label == "MARK"
    assert (rp.u, rp.v) == pytest.approx((ref.u, ref.v), abs=1e-3)

    # The chevron is drawn in the ping colour (anti-aliased) just above the projected point, and
    # nowhere else in the scene band (the compass tape and radar also carry ping markers).
    hud = alpha.last_hud.astype(np.int16)
    ping_mask = (np.abs(hud - np.array(HudStyle().ping, np.int16)) <= 30).all(axis=2)
    u, v = int(round(rp.u)), int(round(rp.v))
    assert ping_mask[v - 16 : v + 1, u - 8 : u + 9].sum() >= 6, "no ping chevron at the projected pixel"
    assert not ping_mask[H // 4 : H // 2 + 40, : W // 3].any()
    assert not ping_mask[H // 4 : H // 2 + 40, 2 * W // 3 :].any()

    # Turned around, the same ping must become an off-screen arrow on a side edge.
    alpha.pose_source.operator.heading = 180.0
    alpha.step()
    (rp,) = alpha.renderer.last_pings
    margin = 46 * H / 720
    assert not rp.on_screen
    assert min(abs(rp.u - margin), abs(rp.u - (W - margin))) < 1e-6


def test_cancelled_ping_disappears_for_teammate(squad):
    alpha, bravo = squad
    alpha.request_ping()
    assert run_until(squad, lambda: len(bravo.last_pings) == 1)
    assert bravo.last_pings[0].owner == "ALPHA"
    alpha.handle_key(ord("x"))
    assert run_until(squad, lambda: not bravo.last_pings)


def test_cli_headless_run_writes_frame(relay, tmp_path):
    png = tmp_path / "headset.png"
    rc = main(["--url", relay.url, "--node", "9", "--callsign", "CLI", "--headless", "--frames", "6",
               "--width", "320", "--height", "180", "--fps", "0", "--ping-at", "2", "--edge",
               "--save-frame", str(png)])
    assert rc == 0 and png.exists() and png.stat().st_size > 5_000


def test_cli_serial_pose_unopenable_port_exits_cleanly(relay):
    with pytest.raises(SystemExit, match="cannot open head tracker"):
        main(["--url", relay.url, "--pose", "serial:/dev/lynx-no-such-port", "--headless", "--frames", "1"])


def test_cli_serial_pose_mock_device_runs(relay):
    rc = main(["--url", relay.url, "--node", "8", "--callsign", "IMU", "--pose", "serial:mock://", "--headless",
               "--frames", "5", "--width", "320", "--height", "180", "--fps", "0", "--ping-at", "2"])
    assert rc == 0
