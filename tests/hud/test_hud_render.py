import numpy as np
import pytest

from lynx.hud import HudRenderer, HudState, ScreenPing, WorldPing
from lynx.hud import widgets as W
from lynx.hud.demo import main as demo_main
from lynx.hud.style import HudStyle, team_bgr
from lynx.vision.iff import IffAssociator
from lynx.vision.synthetic import SyntheticScene
from lynx.vision.types import CameraModel, IffStatus, IffTrack, OperatorPose, TeammateTrack

POSE = OperatorPose(0.0, 0.0, 1.7, 10.0, 0.0, 0.0, node_id=1, callsign="LYNX-1")


def blank(w=1280, h=720):
    return np.full((h, w, 3), 30, np.uint8)


def state_for(w=1280, h=720, **kw):
    return HudState(pose=POSE, camera=CameraModel(w, h, 78.0), **kw)


def test_render_returns_new_frame_and_preserves_input():
    frame = blank()
    before = frame.copy()
    out = HudRenderer().render(frame, state_for())
    assert out.shape == frame.shape and out.dtype == np.uint8
    assert np.array_equal(frame, before)
    assert (out != frame).any()


def test_render_in_place_when_copy_false():
    frame = blank()
    out = HudRenderer().render(frame, state_for(), copy=False)
    assert out is frame


@pytest.mark.parametrize("w,h", [(640, 360), (1280, 720), (1920, 1080), (800, 600)])
def test_render_scales_across_resolutions(w, h):
    tms = [TeammateTrack(2, "VIPER", 5, 30, 1.65), TeammateTrack(3, "GHOST", -40, -60, 1.65, "blue")]
    pings = [WorldPing(1, 0, 40, 0, "OBJ"), WorldPing(2, 0, -40, 0, "RALLY")]
    aux = np.full((240, 320, 3), 90, np.uint8)
    out = HudRenderer().render(blank(w, h), state_for(w, h, teammates=tms, pings=pings, aux_frame=aux, aux_label="AUX: UAV"))
    assert out.shape == (h, w, 3)


def test_grayscale_input_is_accepted():
    out = HudRenderer().render(np.zeros((360, 640), np.uint8), state_for(640, 360))
    assert out.shape == (360, 640, 3)


def test_camera_model_resized_to_frame():
    # State camera at 1280x720 but frame at 640x360 must still render.
    out = HudRenderer().render(blank(640, 360), state_for(1280, 720, pings=[WorldPing(1, 0, 40, 0)]))
    assert out.shape == (360, 640, 3)


def test_reticle_drawn_at_centre():
    img = np.zeros((720, 1280, 3), np.uint8)
    W.reticle(img, (0, 255, 0))
    assert img[360, 640].any()
    assert img[360, 640 - 20].any() and img[360 - 20, 640].any()


@pytest.mark.parametrize("heading", [0.0, 359.9, 90.0, 180.0, 271.3])
def test_compass_wraps(heading):
    img = np.zeros((720, 1280, 3), np.uint8)
    W.compass_tape(img, heading, HudStyle(), markers=[(heading + 170, (0, 255, 255), "ping"), (heading, (0, 255, 0), "friendly")])
    assert img[:80].any() and not img[200:].any()


def test_radar_plots_blips_including_out_of_range():
    img = np.zeros((720, 1280, 3), np.uint8)
    style = HudStyle(radar_range_m=50)
    W.radar(img, 0.0, 78.0, style, blips=[(0, 25, (0, 0, 255), "A", "friendly"), (90, 500, (255, 0, 0), "B", "ping")])
    r = int(78 * 1.0)
    cx, cy = int(18 + r), int(720 - 18 - r)
    assert img[cy - r // 2, cx, 2] == 255  # blip half-way up at bearing 0
    assert img[cy - r - 2 : cy + r + 2, cx - r - 2 : cx + r + 12].any()


def test_iff_boxes_use_status_colours():
    tracks = [
        IffTrack(1, (100, 200, 160, 360), "person", IffStatus.FRIENDLY, 0.9, 2, "VIPER", "blue", 20.0),
        IffTrack(2, (500, 200, 560, 360), "person", IffStatus.TANGO, 0.9, range_m=30.0),
        IffTrack(3, (900, 200, 960, 360), "person", IffStatus.UNVERIFIED, 0.9, range_m=30.0),
    ]
    st = HudStyle()
    out = HudRenderer(st).render(blank(), state_for(tracks=tracks))

    def has(color, region):
        y1, y2, x1, x2 = region
        return (out[y1:y2, x1:x2] == np.array(color, np.uint8)).all(axis=2).any()

    assert has(team_bgr("blue"), (195, 205, 95, 125))
    assert has(st.tango, (195, 205, 495, 525))
    assert has(st.unverified, (195, 205, 895, 925))


def test_on_screen_ping_chevron_and_offscreen_edge_arrow():
    st = HudStyle(ping=(0, 255, 255))
    ahead = WorldPing(1, 0.0, 40.0, 1.7, "OBJ")
    pose = OperatorPose(0, 0, 1.7, 0, 0, 0, node_id=1)
    out = HudRenderer(st).render(blank(), HudState(pose, CameraModel(1280, 720, 78.0), pings=[ahead]))
    yellow = (out == np.array(st.ping, np.uint8)).all(axis=2)
    ys, xs = np.nonzero(yellow[200:360])
    assert len(xs) and abs(xs.mean() - 640) < 60

    behind_left = WorldPing(2, -10.0, -40.0, 1.7, "RALLY")
    out = HudRenderer(st).render(blank(), HudState(pose, CameraModel(1280, 720, 78.0), pings=[behind_left]))
    yellow = (out == np.array(st.ping, np.uint8)).all(axis=2)
    assert yellow[:, :120].any(), "edge arrow must hug the left border"
    assert not yellow[250:470, 400:880].any()


def test_screen_pings_given_in_pixels():
    st = HudStyle(ping=(0, 255, 255))
    sp = [ScreenPing(300, 400, "A", 12.0), ScreenPing(5000, 360, "B", 80.0)]
    out = HudRenderer(st).render(blank(), state_for(screen_pings=sp))
    yellow = (out == np.array(st.ping, np.uint8)).all(axis=2)
    assert yellow[340:400, 280:320].any()
    assert yellow[300:420, 1180:].any()


def test_pip_inset_contents():
    aux = np.full((120, 160, 3), (10, 200, 10), np.uint8)
    out = HudRenderer().render(blank(), state_for(aux_frame=aux, aux_label="AUX: UAV"))
    pw = int(1280 * HudStyle().pip_frac)
    region = out[60:150, 1280 - 14 - pw + 10 : 1280 - 24]
    assert (region[..., 1] > 150).mean() > 0.8


def test_full_pipeline_frame_from_synthetic_scene():
    scene = SyntheticScene(640, 360, seed=1)
    iff = IffAssociator(scene.camera)
    renderer = HudRenderer()
    for _ in range(20):
        sf = scene.step()
        r = iff.update(sf.detections, sf.pose, sf.teammates, now=sf.t)
        st = HudState(sf.pose, sf.camera, r.tracks, r.expected, sf.teammates,
                      [WorldPing(p.ping_id, p.x, p.y, p.z, p.label) for p in sf.pings],
                      telemetry={"FPS": "30"}, aux_frame=scene.render_topdown())
        out = renderer.render(sf.frame, st)
    assert out.shape == (360, 640, 3)


def test_demo_synthetic_headless_writes_frame(tmp_path):
    png = tmp_path / "hud.png"
    rc = demo_main(["--source", "synthetic", "--frames", "6", "--headless", "--width", "640",
                    "--height", "360", "--save-frame", str(png), "--edge"])
    assert rc == 0 and png.exists() and png.stat().st_size > 10_000
