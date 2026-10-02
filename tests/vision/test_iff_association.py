from typing import Optional

import pytest

from lynx.vision.geometry import project_point
from lynx.vision.iff import IffAssociator, IffConfig, association_cost, expected_teammates, iou
from lynx.vision.synthetic import SyntheticScene
from lynx.vision.types import CameraModel, Detection, IffStatus, OperatorPose, TeammateTrack

CAM = CameraModel(1280, 720, 78.0)
POSE = OperatorPose(0.0, 0.0, 1.7, 0.0, 0.0, 0.0, node_id=1, callsign="LYNX-1")


def person_box(x, y, pose=POSE, cam=CAM, height=1.75, du=0.0, scale=1.0, cat="person") -> Optional[Detection]:
    foot = project_point(pose, cam, (x, y, 0.0))
    top = project_point(pose, cam, (x, y, height))
    if foot.pixel is None or top.pixel is None:
        return None
    h = (foot.pixel[1] - top.pixel[1]) * scale
    u = foot.pixel[0] + du
    return Detection(u - 0.2 * h, foot.pixel[1] - h, u + 0.2 * h, foot.pixel[1], 0.85, cat, cat)


def mate(node, call, x, y, t=0.0, color="green"):
    return TeammateTrack(node, call, x, y, 1.65, color, timestamp=t)


def statuses(result):
    return {t.track_id: (t.status, t.callsign) for t in result.tracks}


def test_iou_basics():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)


def test_well_aligned_teammate_is_friendly_on_first_frame():
    iff = IffAssociator(CAM)
    r = iff.update([person_box(2, 20)], POSE, [mate(2, "VIPER", 2, 20)], now=0.0)
    (tr,) = r.tracks
    assert tr.status == IffStatus.FRIENDLY and tr.callsign == "VIPER" and tr.node_id == 2
    assert tr.range_m == pytest.approx(20.1, abs=0.5)


def test_expected_teammate_geometry():
    cfg = IffConfig()
    (e,) = expected_teammates(POSE, CAM, [mate(2, "VIPER", 0, 20)], cfg, now=0.0)
    assert e.in_view and e.pixel[0] == pytest.approx(640.0)
    assert e.expected_height_px == pytest.approx(CAM.fy * cfg.person_height_m / 20.0, rel=1e-6)
    assert e.world_bearing_deg == pytest.approx(0.0)


def test_off_bearing_contact_becomes_tango_after_min_hits():
    cfg = IffConfig()
    iff = IffAssociator(CAM, cfg)
    det = person_box(-8, 20)  # ~22 deg left of the teammate
    for k in range(cfg.tango_min_hits):
        r = iff.update([det], POSE, [mate(2, "VIPER", 2, 20, t=k)], now=k)
        expected = IffStatus.TANGO if k + 1 >= cfg.tango_min_hits else IffStatus.UNVERIFIED
        assert r.tracks[0].status == expected


def test_size_gate_rejects_wrong_range():
    # Teammate telemetry says 40 m, but the box on the same bearing is a 6 m person.
    iff = IffAssociator(CAM)
    near = person_box(0, 6)
    r = iff.update([near], POSE, [mate(2, "VIPER", 0, 40)], now=0.0)
    assert r.tracks[0].status != IffStatus.FRIENDLY
    (e,) = r.expected
    assert association_cost(near.as_xyxy(), e, CAM, IffConfig()) >= 1e6


def test_hungarian_resolves_same_bearing_by_size():
    # Two teammates almost in line (1 deg apart) at 10 m and 40 m.
    iff = IffAssociator(CAM)
    tms = [mate(2, "NEAR", 0.0, 10.0), mate(3, "FAR", 0.7, 40.0)]
    dets = [person_box(0.7, 40.0), person_box(0.0, 10.0)]
    r = iff.update(dets, POSE, tms, now=0.0)
    by_h = sorted(r.tracks, key=lambda t: t.bbox[3] - t.bbox[1])
    assert [t.callsign for t in by_h] == ["FAR", "NEAR"]
    assert all(t.status == IffStatus.FRIENDLY for t in by_h)


def test_hungarian_beats_greedy_crossing_case():
    # Greedy nearest-neighbour on azimuth would give detection A to VIPER and
    # leave GHOST unmatched; the optimal one-to-one assignment matches both.
    cfg = IffConfig(angle_sigma_deg=1.0, pos_sigma_m=0.3)
    iff = IffAssociator(CAM, cfg)
    tms = [mate(2, "VIPER", 0.0, 20.0), mate(3, "GHOST", 1.6, 20.0)]
    dets = [person_box(0.7, 20.0), person_box(-0.9, 20.0)]
    r = iff.update(dets, POSE, tms, now=0.0)
    by_track = {tid: nid for tid, nid, _ in r.assignments}
    assert by_track == {1: 3, 2: 2}  # A -> GHOST, B -> VIPER
    r = iff.update(dets, POSE, tms, now=0.1)
    names = {t.callsign for t in r.tracks if t.status == IffStatus.FRIENDLY}
    assert names == {"VIPER", "GHOST"}


def test_one_node_never_labels_two_tracks():
    iff = IffAssociator(CAM)
    tms = [mate(2, "VIPER", 0.0, 20.0)]
    dets = [person_box(0.0, 20.0), person_box(0.4, 20.0)]
    for k in range(6):
        r = iff.update(dets, POSE, tms, now=k)
        assert sum(t.callsign == "VIPER" for t in r.tracks) <= 1


def test_friendly_persists_through_detector_dropouts():
    cfg = IffConfig()
    iff = IffAssociator(CAM, cfg)
    tm = [mate(2, "VIPER", 2, 20)]
    det = person_box(2, 20)
    for k in range(4):
        iff.update([det], POSE, tm, now=k)
    for k in range(4, 4 + cfg.max_coast_frames):
        r = iff.update([], POSE, tm, now=k)
        assert r.tracks and r.tracks[0].status == IffStatus.FRIENDLY and r.tracks[0].misses > 0
    r = iff.update([det], POSE, tm, now=20)
    assert r.tracks[0].status == IffStatus.FRIENDLY and r.tracks[0].misses == 0


def test_hysteresis_survives_brief_mismatch_then_releases_to_unverified():
    iff = IffAssociator(CAM)
    det = person_box(2, 20)
    good, bad = [mate(2, "VIPER", 2, 20)], [mate(2, "VIPER", -15, 20)]
    for k in range(6):
        iff.update([det], POSE, good, now=k)
    r = iff.update([det], POSE, bad, now=6)
    assert r.tracks[0].status == IffStatus.FRIENDLY, "one bad telemetry frame must not flip the label"
    for k in range(7, 30):
        r = iff.update([det], POSE, bad, now=k)
    # Previously-friendly tracks never escalate straight to TANGO.
    assert r.tracks[0].status == IffStatus.UNVERIFIED


def test_stale_telemetry_blocks_friendly_and_tango():
    cfg = IffConfig()
    iff = IffAssociator(CAM, cfg)
    tm = [mate(2, "VIPER", 2, 20, t=0.0)]
    det = person_box(2, 20)
    for k in range(10):
        r = iff.update([det], POSE, tm, now=10.0 + k)
    assert r.expected[0].stale
    assert r.tracks[0].status == IffStatus.UNVERIFIED


def test_vehicle_is_never_friendly_or_tango():
    iff = IffAssociator(CAM)
    veh = person_box(0, 25, cat="vehicle")
    for k in range(10):
        r = iff.update([veh], POSE, [mate(2, "VIPER", 0, 25)], now=k)
    assert r.tracks[0].status == IffStatus.UNVERIFIED


def test_teammate_behind_is_not_in_view_and_not_matched():
    iff = IffAssociator(CAM)
    r = iff.update([person_box(0, 20)], POSE, [mate(2, "VIPER", 0, -20)], now=0.0)
    assert not r.expected[0].in_view and r.expected[0].pixel is None
    assert r.tracks[0].status != IffStatus.FRIENDLY


def test_lost_tracks_are_deleted_no_dead_state():
    cfg = IffConfig()
    iff = IffAssociator(CAM, cfg)
    for k in range(6):
        iff.update([person_box(-8, 20)], POSE, [], now=k)
    for k in range(cfg.max_coast_frames + 1):
        r = iff.update([], POSE, [], now=10 + k)
    assert r.tracks == []
    assert set(s.value for s in IffStatus) == {"FRIENDLY", "UNVERIFIED", "TANGO"}


def test_self_node_ignored():
    r = IffAssociator(CAM).update([], POSE, [mate(1, "LYNX-1", 0, 0)], now=0.0)
    assert r.expected == []


def test_end_to_end_synthetic_no_blue_on_blue():
    """Over a panning synthetic run no real teammate is ever shown as TANGO,
    no unknown is ever shown as FRIENDLY, and visible teammates are labelled
    with the correct callsign on the vast majority of frames."""
    scene = SyntheticScene(seed=3)
    iff = IffAssociator(scene.camera)
    blue_on_blue = wrong_friend = correct = friend_frames = 0
    for _ in range(240):
        sf = scene.step(1.0 / 30.0)
        r = iff.update(sf.detections, sf.pose, sf.teammates, now=sf.t)
        for name, friendly, box in sf.truth:
            best = max(r.tracks, key=lambda t: iou(t.bbox, box), default=None)
            if best is None or iou(best.bbox, box) < 0.5:
                continue
            if friendly:
                friend_frames += 1
                blue_on_blue += best.status == IffStatus.TANGO
                correct += best.status == IffStatus.FRIENDLY and best.callsign == name
            elif name != "VEHICLE":
                wrong_friend += best.status == IffStatus.FRIENDLY
    assert friend_frames > 100
    assert blue_on_blue == 0
    assert wrong_friend == 0
    assert correct / friend_frames > 0.9
