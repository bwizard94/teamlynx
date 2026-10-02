"""Link-loss holdover, the field launcher (profile -> headset) and the gnss: pose source."""

import json
import socket
import time

import pytest

pytest.importorskip("cv2", reason="install the [vision] or [vision-headless] extra")
pytest.importorskip("scipy", reason="install the [vision] or [vision-headless] extra")

from lynx.field.calibrate import FieldCalibration, TareSolution  # noqa: E402
from lynx.field.failover import FailoverClient  # noqa: E402
from lynx.field.gnss import GnssReader, MockGnssReceiver  # noqa: E402
from lynx.field.holdover import FriendlyHoldover, HoldoverConfig  # noqa: E402
from lynx.field.launcher import (  # noqa: E402
    FieldHook,
    NodeProfile,
    needs_calibration,
    plan,
    pose_spec,
    run_headset,
    sd_notify,
)
from lynx.field.pose_source import GnssImuPoseSource, parse_gnss_spec  # noqa: E402
from lynx.field.session import load_records  # noqa: E402
from lynx.field.site import Site  # noqa: E402
from lynx.headset.app import HeadsetClient, HeadsetConfig  # noqa: E402
from lynx.headset.pose import StaticPoseSource, create_pose_source  # noqa: E402
from lynx.headset.sources import SyntheticSource  # noqa: E402
from lynx.hw import ImuCalibration, ImuLink, MockImuDevice, OrientationConverter, SerialImuPoseSource  # noqa: E402
from lynx.net.client import BackgroundClient, LynxClient  # noqa: E402
from lynx.net.schema import LeaveReason, NodeLeave, Team  # noqa: E402
from lynx.spatial import Pose  # noqa: E402
from lynx.vision.types import TeammateTrack  # noqa: E402

from .conftest import FieldRelayThread, wait_for  # noqa: E402
from .test_calibrate import SITE  # noqa: E402


def tm(t, x, y=0.0, node=2, cs="BRAVO"):
    return TeammateTrack(node, cs, x, y, 1.7, "blue", t, 0.0)


# ------------------------------------------------------------------------------------ holdover


def test_holdover_live_passthrough_then_dead_reckon_and_clamp():
    h = FriendlyHoldover(HoldoverConfig(dr_horizon_s=3.0, max_dr_m=5.0))
    for i in range(11):  # walking east at 2 m/s, fixes at 5 Hz
        out = h.update([tm(i * 0.2, i * 0.4)], i * 0.2)
    assert out == [tm(2.0, 4.0)] and h.held == []
    # link drops: no more teammates in the snapshot
    out = h.update([], 3.0)  # age 1 s: still live
    assert out[0].team_color == "blue" and out[0].x == 4.0
    out = h.update([], 4.5)  # age 2.5 s: stale, dead-reckoned 2.5 s at ~2 m/s
    assert out[0].team_color == "white" and out[0].callsign == "BRAVO 2s"
    assert out[0].x == pytest.approx(4.0 + 2.0 * 2.5, abs=0.4) and out[0].timestamp == 2.0
    out = h.update([], 30.0)  # beyond the DR horizon: held, displacement clamped to max_dr_m
    assert out[0].x == pytest.approx(4.0 + 5.0, abs=1e-6) and h.held == [2]
    assert h.update([], 200.0) == []  # forgotten after hold_s


def test_holdover_resumes_and_leave_shortens_hold():
    h = FriendlyHoldover(HoldoverConfig(hold_s=100.0, leave_hold_s=10.0))
    h.update([tm(0.0, 0.0), tm(0.0, 5.0, node=3, cs="C")], 0.0)
    h.on_message(NodeLeave(node_id=0, node=3, reason=LeaveReason.DISCONNECT))
    out = h.update([], 5.0)
    assert {t.node_id for t in out} == {2, 3}
    out = h.update([tm(11.0, 1.0)], 11.0)
    assert [t.node_id for t in out] == [2] and out[0].team_color == "blue"  # back live, C forgotten
    h.on_message(NodeLeave(node_id=0, node=2, reason=LeaveReason.STALE))  # stale leave keeps the full hold
    assert [t.node_id for t in h.update([], 50.0)] == [2]


def test_holdover_annotates_link_down():
    h = FriendlyHoldover()
    h.update([tm(0.0, 0.0)], 0.0)
    h.update([], 5.0, connected=False)

    class Net:
        connected = False

    class Client:
        net = Net()

    alerts, telem = ["! RELAY LINK DOWN"], {}
    h.update([], 9.0, connected=False)
    h.annotate(Client(), None, 9.0, alerts, telem)
    assert alerts == ["! LINK DOWN 4s - 1 FRIENDLIES HELD"]
    assert telem["HOLD"].startswith("1 STALE  OLDEST 9s")


def test_headset_holds_friendly_through_relay_loss():
    relay = FieldRelayThread(sweep_interval_s=0.05).start()
    fc = FailoverClient([relay.url], 1, "ALPHA", Team.BLUE, keepalive_interval_s=0.2, keepalive_timeout_s=0.4,
                        backoff_initial_s=0.05, backoff_max_s=0.2)
    hold = FriendlyHoldover(link_down_s=fc.link_down_s)
    fc.on_message = hold.on_message
    cfg = HeadsetConfig(url=relay.url, node_id=1, callsign="ALPHA", team=Team.BLUE)
    pose = Pose.from_euler(0.0, 0.0, 1.7, 0.0, 0.0, 0.0)
    client = HeadsetClient(cfg, SyntheticSource(320, 180, 78.0, seed=1, unknowns=False), StaticPoseSource(pose),
                           BackgroundClient(fc), hooks=[FieldHook(hold, pose_source=None)]).start()
    bravo = BackgroundClient(LynxClient(relay.url, 2, "BRAVO")).start()
    try:
        assert client.wait_connected(5.0) and wait_for(lambda: bravo.connected)
        for i in range(20):  # BRAVO 20 m ahead, walking east at 1 m/s
            bravo.send_telemetry(i * 0.1, 20.0, 1.7, 180.0, 0.0, 0.0)
            time.sleep(0.1)
            client.step()
        live = {t.node_id: t for t in client.last_teammates}
        assert live[2].team_color == "blue" and live[2].callsign == "BRAVO"
        bravo.stop()
        relay.stop()
        assert wait_for(lambda: not client.net.connected, 3.0)
        client.step(now=time.monotonic() + 3.5)  # 3.5 s later, link still down
        held = {t.node_id: t for t in client.last_teammates}[2]
        assert held.team_color == "white" and held.callsign.startswith("BRAVO ")
        last_x = 1.9
        assert last_x + 1.5 < held.x < last_x + 3.5  # dead-reckoned forward, at most the 3 s horizon at ~1 m/s
        assert client.last_state.alerts[0].startswith("! LINK DOWN")
        exp = {e.node_id: e for e in client.last_state.expected}[2]
        assert exp.stale  # IFF never confirms a FRIENDLY box on it, but suppresses TANGO near it
        assert client.last_hud is not None
    finally:
        client.stop()
        bravo.stop()
        relay.stop()


# ------------------------------------------------------------------------------------ launcher


def write_cal(cal_dir, node=3, pos=(2.0, -1.0), when=None):
    sol = TareSolution("station", pos[0], pos[1], 4.5, 0.4, (0.02, 0.02), [], "S1")
    fc = FieldCalibration.from_solution(sol, node, ImuCalibration(), 1.75, "test",
                                        now=time.time() if when is None else when)
    fc.save(cal_dir)
    return fc


def profile(tmp_path, **kw):
    site = tmp_path / "site.json"
    site.write_text(json.dumps(SITE))
    d = {"node": 3, "callsign": "CHARLIE", "imu": "mock://", "site": str(site), "cal_dir": str(tmp_path / "cal"),
         "log_dir": str(tmp_path / "log"), "calibrate": "never", "camera": {"source": "synthetic", "hfov": 78.0,
                                                                           "width": 320, "height": 180}}
    d.update(kw)
    return NodeProfile.from_dict(d)


def test_profile_validation(tmp_path):
    with pytest.raises(ValueError):
        profile(tmp_path, callsign="TOO-LONG-NAME")
    with pytest.raises(ValueError):
        profile(tmp_path, calibrate="sometimes")
    with pytest.raises(ValueError):
        NodeProfile.from_dict({"node": 1, "callsign": "A", "bogus": True})


def test_needs_calibration_modes(tmp_path):
    p = profile(tmp_path, calibrate="if-stale", calibrate_max_age_h=12.0)
    assert needs_calibration(p, None)
    fresh = write_cal(p.cal_dir, when=1000.0)
    assert not needs_calibration(p, fresh, now=1000.0 + 3600)
    assert needs_calibration(p, fresh, now=1000.0 + 13 * 3600)
    assert not needs_calibration(profile(tmp_path, calibrate="if-missing"), fresh)
    assert needs_calibration(profile(tmp_path, calibrate="always"), fresh)
    assert not needs_calibration(profile(tmp_path, calibrate="always", imu=None), fresh)


def test_plan_uses_calibration_and_picks_pose_source(tmp_path):
    p = profile(tmp_path)
    write_cal(p.cal_dir)
    lp = plan(p, urls=["ws://relay.lynx:8765", "ws://relay2.lynx:8765"])
    argv = lp.argv
    assert argv[argv.index("--url") + 1] == "ws://relay.lynx:8765"
    assert argv[argv.index("--pose") + 1] == f"serial:mock://?cal={tmp_path / 'cal' / 'imu.json'}"
    assert argv[argv.index("--x") + 1] == "2.000" and argv[argv.index("--eye-height") + 1] == "1.750"
    assert lp.calibration["node"] == 3 and not lp.calibrate
    g = profile(tmp_path, gnss="mock://")
    assert pose_spec(g, None, True) == f"gnss:mock://?gnss=mock://&site={g.site}"
    assert pose_spec(g, None, False).startswith("serial:")
    assert pose_spec(profile(tmp_path, imu=None), None, True) == "keyboard"
    other = profile(tmp_path, node=4)
    assert plan(other, urls=["ws://x:1"]).calibration is None  # a calibration for another node is ignored


def test_sd_notify_and_field_hook_watchdog(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        assert sd_notify("STATUS=hello")
        assert sock.recv(64) == b"STATUS=hello"
        hook = FieldHook(watchdog_s=0.0)
        hook.annotate(None, None, 1.0, [], {})
        got = {sock.recv(64), sock.recv(64)}
        assert got == {b"READY=1", b"WATCHDOG=1"}
    finally:
        sock.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert not sd_notify("READY=1")


def test_field_launcher_runs_headless_with_log(tmp_path):
    relay = FieldRelayThread(sweep_interval_s=0.05).start()
    try:
        p = profile(tmp_path)
        write_cal(p.cal_dir)
        assert run_headset(p, urls=[relay.url], headless=True, frames=8) == 0
    finally:
        relay.stop()
    log = load_records([tmp_path / "log"])
    assert log.sources == ["node:3"]
    poses = [r for r in log.records if r["kind"] == "pose"]
    assert poses and poses[0]["x"] == pytest.approx(2.0) and poses[0]["callsign"] == "CHARLIE"
    assert any(r["kind"] == "event" and r["event"] == "connected" for r in log.records)


# ------------------------------------------------------------------------------------ gnss pose source


class Clock:
    t = 50.0

    def __call__(self):
        return self.t


def test_gnss_pose_source_position_from_fix(tmp_path):
    site = Site.from_dict(SITE)
    clock = Clock()
    dev = MockImuDevice(pose_fn=lambda t: (30.0, -5.0, 0.0), clock=clock, realtime=False)
    link = ImuLink(dev, OrientationConverter(ImuCalibration()), clock=clock)
    imu = SerialImuPoseSource("mock://", Pose.from_euler(0, 0, 1.7, 0, 0, 0), link=link)
    reader = GnssReader(clock=clock)
    rx = MockGnssReceiver(site.frame, track=lambda t: (12.0, -7.0), realtime=False)
    src = GnssImuPoseSource("mock://?gnss=mock://&site=injected.json&eye=1.8", Pose.from_euler(0, 0, 1.7, 0, 0, 0),
                            imu=imu, gnss=reader, site=site)
    for _ in range(5):
        clock.t += 0.01
        link.feed(dev.read(65536))
    sample = src.read(clock.t)
    assert not src.gnss_ok
    for line in rx.epoch_lines(1.0):
        reader.feed_line(line, now=clock.t)
    sample = src.read(clock.t)
    assert src.gnss_ok and tuple(sample.pose.position) == pytest.approx((12.0, -7.0, 1.8), abs=0.02)
    assert sample.pose.euler[0] == pytest.approx(30.0, abs=0.1)
    clock.t += 5.0  # fix goes stale: position held
    link.feed(dev.read(65536))
    sample = src.read(clock.t)
    assert not src.gnss_ok and tuple(sample.pose.position) == pytest.approx((12.0, -7.0, 1.8), abs=0.02)


def test_gnss_spec_parsing_and_lazy_plugin(tmp_path):
    imu_spec, opts = parse_gnss_spec("/dev/ttyUSB0?gnss=/dev/ttyACM0&site=s.json&cal=imu.json&mount=rear")
    assert imu_spec == "/dev/ttyUSB0?cal=imu.json&mount=rear" and opts["gnss"] == "/dev/ttyACM0"
    with pytest.raises(ValueError, match="site"):
        parse_gnss_spec("mock://?gnss=mock://")
    with pytest.raises(ValueError, match="unknown"):
        parse_gnss_spec("mock://?gnss=a&site=b&bogus=1")
    site = tmp_path / "site.json"
    site.write_text(json.dumps(SITE))
    src = create_pose_source(f"gnss:mock://still?gnss=mock://&site={site}", Pose.from_euler(0, 0, 1.7, 0, 0, 0))
    try:
        assert src.name == "gnss"
        assert wait_for(lambda: src.read(time.monotonic()) is not None and src.gnss_ok, 3.0)
        pos = src.read(time.monotonic()).pose.position
        assert tuple(pos) == pytest.approx((0.0, 0.0, 1.7), abs=0.05)  # the mock receiver sits on the site datum
    finally:
        src.close()
