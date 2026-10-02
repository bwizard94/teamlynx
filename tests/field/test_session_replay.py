"""Session logging (decimation, gzip, crash tolerance, merge) and the after-action review."""

import json

import pytest

from lynx.field.session import SessionRecorder, iter_records, load_records, session_filename
from lynx.net.schema import Ping, PingCancel, PingType, Team, Telemetry


class Mono:
    t = 0.0

    def __call__(self):
        return self.t


def tel(node, x, y, heading=0.0, cs="A", team=Team.BLUE):
    return Telemetry(node_id=node, seq=1, ts_us=1, team=team, callsign=cs, x=x, y=y, z=1.7, heading=heading)


def test_recorder_decimates_telemetry_not_events(tmp_path):
    mono = Mono()
    rec = SessionRecorder(tmp_path / "s.jsonl", "relay", telemetry_hz=5.0, mono=mono, wall=lambda: 1000.0 + mono.t)
    for i in range(100):  # 1 s at 100 Hz for two nodes
        mono.t = i * 0.01
        rec.record_message(tel(1, i, 0))
        rec.record_message(tel(2, 0, i))
    rec.record_message(Ping(node_id=1, ping_id=4, owner=1, ping_type=PingType.RALLY, x=1, y=2, z=0, ttl_ms=5000))
    rec.record_event("disconnected", url="ws://x")
    rec.close()
    recs = list(iter_records(tmp_path / "s.jsonl"))
    tel_recs = [r for r in recs if r.get("msg", {}).get("type") == "telemetry"]
    assert len([r for r in tel_recs if r["msg"]["node_id"] == 1]) == 5
    assert recs[0]["kind"] == "meta" and recs[-1]["event"] == "disconnected"
    assert any(r.get("msg", {}).get("ping_type") == "rally" for r in recs)


def test_gzip_and_truncated_tails_are_tolerated(tmp_path):
    rec = SessionRecorder(tmp_path, "node:2", telemetry_hz=0, compress=True)
    for i in range(50):
        rec.record_pose(2, "BRAVO", "blue", i, 0, 1.7, 0, 0, 0)
    rec.close()
    path = rec.path
    assert path.name.startswith("node-2-") and path.suffix == ".gz"
    assert sum(1 for r in iter_records(path) if r["kind"] == "pose") == 50
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(path.read_bytes()[:-30])  # power cut mid-write
    assert 0 < sum(1 for _ in iter_records(cut)) <= 51
    plain = tmp_path / "plain.jsonl"
    plain.write_text('{"kind":"meta","t":1}\n{"kind":"pose","t":2,"node":1,"x":0,"y":0,"z":0,"heading":0}\n{"kind":"po')
    assert [r["kind"] for r in iter_records(plain)] == ["meta", "pose"]
    assert session_filename("node:3", 0.0) == "node-3-19700101-000000.jsonl"


def test_log_path_without_suffix_is_a_directory(tmp_path):
    a = SessionRecorder(tmp_path / "logs", "relay")
    b = SessionRecorder(tmp_path / "logs", "node:2")
    a.close()
    b.close()
    assert (tmp_path / "logs").is_dir() and a.path != b.path and a.path.parent == b.path.parent


def test_load_merges_sources_with_offsets(tmp_path):
    for name, src, t in (("a.jsonl", "relay", 10.0), ("b.jsonl", "node:2", 5.0)):
        (tmp_path / name).write_text(json.dumps({"kind": "event", "t": t, "src": src, "event": "x"}) + "\n")
    log = load_records([tmp_path], offsets={"node:2": 10.0})
    assert [r["src"] for r in log.records] == ["relay", "node:2"]
    assert [r["t"] for r in log.records] == [10.0, 15.0]


def synthetic_session(tmp_path):
    """Relay log + one headset log covering a link loss."""
    mono = Mono()
    wall = lambda: 1_790_000_000.0 + mono.t  # noqa: E731
    relay = SessionRecorder(tmp_path / "relay.jsonl", "relay", telemetry_hz=0, mono=mono, wall=wall)
    node = SessionRecorder(tmp_path / "node2.jsonl", "node:2", telemetry_hz=0, mono=mono, wall=wall)
    for i in range(61):
        mono.t = float(i)
        relay.record_message(tel(1, i * 1.5, 10.0, 90.0, "ALPHA"))
        if i < 20 or i > 40:
            relay.record_message(tel(2, 0.0, i * 1.0, 0.0, "BRAVO", Team.GREEN))
        else:
            node.record_pose(2, "BRAVO", "green", 0.0, i * 1.0, 1.7, 0.0, 0.0, 0.0, link=False)
        if i == 10:
            relay.record_message(Ping(node_id=1, ping_id=1, owner=1, ping_type=PingType.CONTACT, x=40, y=30, z=0,
                                      ttl_ms=20_000))
        if i == 15:
            relay.record_message(Ping(node_id=2, ping_id=1, owner=2, ping_type=PingType.RALLY, x=-10, y=50, z=0,
                                      ttl_ms=600_000))
        if i == 20:
            node.record_event("disconnected", url="ws://relay.lynx:8765")
        if i == 25:
            relay.record_message(PingCancel(node_id=1, ping_id=1, owner=1))
        if i == 40:
            node.record_event("connected", url="ws://relay2.lynx:8765")
    relay.close()
    node.close()
    return tmp_path


def test_build_session_tracks_pings_and_link_intervals(tmp_path):
    from lynx.field.replay import build_session, summarize

    s = build_session(load_records([synthetic_session(tmp_path)]))
    assert sorted(s.tracks) == [1, 2] and s.duration_s == pytest.approx(60.0)
    bravo = s.tracks[2]
    assert len(bravo.points) == 61 and bravo.callsign == "BRAVO" and bravo.team == "green"
    assert not bravo.points[30].link and bravo.points[50].link
    assert bravo.distance_m() == pytest.approx(60.0)
    assert s.tracks[1].at(s.t0 + 10.5).x == pytest.approx(15.75)
    contact, rally = sorted(s.pings, key=lambda p: p.ping_type)
    assert contact.ping_type == "contact" and contact.t_end - contact.t_start == pytest.approx(15.0)
    assert contact.end == "owner"
    assert rally.end == "open"
    assert s.link_down_intervals() == {"node:2": [(s.t0 + 20.0, s.t0 + 40.0)]}
    text = summarize(s)
    assert "BRAVO" in text and "pings: 2" in text and "link down node:2: 1x, 20.0 s" in text


def test_render_overview_and_video(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from lynx.field.replay import build_session, make_view, node_color, render_overview, write_video
    from lynx.field.site import Site

    from .test_calibrate import SITE

    site = Site.from_dict(SITE)
    s = build_session(load_records([synthetic_session(tmp_path)]))
    img = render_overview(s, site, size=(1200, 900))
    assert img.shape == (900, 1200, 3)
    timeline_h = 40 + 22 * 2
    view = make_view(s, (1200, 900 - timeline_h), site)
    # ALPHA's track colour is present along its path (E 30, N 10)
    u, v = view.px(30.0, 10.0)
    patch = img[v - 3:v + 4, u - 3:u + 4].reshape(-1, 3)
    assert np.abs(patch.astype(int) - node_color(s, 1)).sum(axis=1).min() < 40
    out = tmp_path / "aar.mp4"
    frames = write_video(s, str(out), site, size=(640, 480), fps=5.0, speed=30.0)
    assert frames == 11 and out.stat().st_size > 1000
    cap = cv2.VideoCapture(str(out))
    ok, frame = cap.read()
    cap.release()
    assert ok and frame.shape == (480, 640, 3)


def test_replay_cli_end_to_end(tmp_path, capsys):
    pytest.importorskip("cv2")
    from lynx.field.cli import main

    logs = synthetic_session(tmp_path / "logs")
    out = tmp_path / "aar.png"
    site = tmp_path / "site.json"
    from .test_calibrate import SITE

    site.write_text(json.dumps(SITE))
    assert main(["replay", str(logs), "--site", str(site), "--out", str(out)]) == 0
    assert out.stat().st_size > 10_000
    assert "wrote" in capsys.readouterr().out
