"""Identification Friend-or-Foe by fusing detections with squad telemetry.

Per frame:

1. **Track** detector boxes with a constant-velocity IoU tracker (Hungarian
   on 1 - IoU) so labels attach to persistent contacts, not single boxes.
2. **Predict** every teammate's appearance: relative position -> camera
   frame -> boresight azimuth / elevation, pinhole pixel, slant range, and
   the expected box height ``f_y * H_person / Z_c``.
3. **Gate + assign** teammates to person tracks with a normalised cost

       e_az   = d_az / sigma_az,  sigma_az^2 = s_ang^2 + (s_pos/R)^2 + (w_ang/4)^2
       e_el   = d_el / sigma_el   (same form with the box angular height)
       e_size = ln(h_box / h_expected) / s_ln
       cost   = e_az^2 + e_el^2 + e_size^2

   rejecting pairs with |e_az| or |e_el| > gate_sigma or an implausible size
   ratio, then solving the one-to-one assignment with
   ``scipy.optimize.linear_sum_assignment``.
4. **Accumulate evidence with hysteresis**: matched (track, node) pairs gain
   score, unmatched pairs decay, coasting tracks decay slowly so a Friendly
   label survives brief detector dropouts and short occlusions. A track is
   FRIENDLY when its best node score passes ``confirm_score`` and stays
   FRIENDLY until it falls below ``release_score``.

Status semantics (no dead-enemy state - lost tracks are simply deleted):
    FRIENDLY   : confirmed teammate, rendered in team colour + callsign.
    UNVERIFIED : new, vehicle, ambiguous, previously-friendly, or near the
                 last known bearing of a teammate with stale telemetry.
    TANGO      : established person track that no live teammate can explain.
TANGO is an advisory cue only; positive visual ID is still required.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

from lynx.vision.geometry import pixel_to_angles, project_point, world_bearing_deg
from lynx.vision.types import (
    CameraModel,
    Detection,
    ExpectedTeammate,
    IffStatus,
    IffTrack,
    OperatorPose,
    TeammateTrack,
)

INFEASIBLE = 1e6


@dataclass(frozen=True)
class IffConfig:
    person_height_m: float = 1.75
    head_to_center_m: float = 0.80  # telemetry is the headset; box centre is ~torso
    angle_sigma_deg: float = 1.5  # heading/attitude error budget (IMU + boresight)
    pos_sigma_m: float = 1.0  # horizontal relative-position error
    pos_sigma_z_m: float = 0.6
    size_log_sigma: float = 0.45
    gate_sigma: float = 3.0
    min_size_ratio: float = 0.30  # prone / crouched / partially occluded
    max_size_ratio: float = 2.2
    max_telemetry_age_s: float = 2.0
    stale_gate_deg: float = 15.0
    # evidence / hysteresis
    hit_gain: float = 1.0
    strong_match_cost: float = 1.0
    strong_match_bonus: float = 1.0
    miss_decay: float = 0.6
    coast_decay: float = 0.15
    confirm_score: float = 1.5
    release_score: float = 0.5
    max_score: float = 6.0
    tango_min_hits: int = 5
    # tracker
    track_iou_gate: float = 0.15
    max_coast_frames: int = 8
    bbox_smoothing: float = 0.6  # weight of the new measurement
    velocity_smoothing: float = 0.5


@dataclass
class _Track:
    track_id: int
    bbox: np.ndarray  # x1, y1, x2, y2 (smoothed)
    velocity: np.ndarray
    category: str
    confidence: float
    hits: int = 1
    age: int = 1
    misses: int = 0
    scores: Dict[int, float] = field(default_factory=dict)
    friendly_node: Optional[int] = None
    ever_friendly: bool = False
    status: IffStatus = IffStatus.UNVERIFIED


@dataclass
class IffResult:
    tracks: List[IffTrack]
    expected: List[ExpectedTeammate]
    assignments: List[Tuple[int, int, float]]  # (track_id, node_id, cost) this frame


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def expected_teammates(
    pose: OperatorPose,
    camera: CameraModel,
    teammates: Sequence[TeammateTrack],
    config: IffConfig,
    now: Optional[float] = None,
) -> List[ExpectedTeammate]:
    out = []
    for tm in teammates:
        if tm.node_id == pose.node_id:
            continue
        center = (tm.x, tm.y, tm.z - config.head_to_center_m)
        p = project_point(pose, camera, center)
        zc = p.cam_xyz[2]
        exp_h = camera.fy * config.person_height_m / zc if zc > 0.05 else 0.0
        stale = now is not None and (now - tm.timestamp) > config.max_telemetry_age_s
        out.append(
            ExpectedTeammate(
                node_id=tm.node_id,
                callsign=tm.callsign,
                team_color=tm.team_color,
                range_m=p.range_m,
                azimuth_deg=p.azimuth_deg,
                elevation_deg=p.elevation_deg,
                pixel=p.pixel,
                in_view=p.in_view,
                expected_height_px=exp_h,
                stale=stale,
                world_bearing_deg=world_bearing_deg(pose, tm.position),
            )
        )
    return out


def association_cost(
    bbox: Sequence[float],
    exp: ExpectedTeammate,
    camera: CameraModel,
    config: IffConfig,
) -> float:
    """Normalised squared cost of explaining ``bbox`` as teammate ``exp``.

    Returns ``INFEASIBLE`` when the pair fails the angular or size gate.
    """
    if exp.pixel is None or exp.expected_height_px <= 0.0:
        return INFEASIBLE
    x1, y1, x2, y2 = bbox
    u, v = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    az, el = pixel_to_angles(camera, u, v)
    az_l, _ = pixel_to_angles(camera, x1, v)
    az_r, _ = pixel_to_angles(camera, x2, v)
    _, el_t = pixel_to_angles(camera, u, y1)
    _, el_b = pixel_to_angles(camera, u, y2)
    w_ang, h_ang = abs(az_r - az_l), abs(el_t - el_b)

    rng = max(exp.range_m, 0.5)
    pos_ang = math.degrees(math.atan2(config.pos_sigma_m, rng))
    pos_ang_z = math.degrees(math.atan2(config.pos_sigma_z_m, rng))
    s_az = math.sqrt(config.angle_sigma_deg**2 + pos_ang**2 + (w_ang / 4.0) ** 2)
    s_el = math.sqrt(config.angle_sigma_deg**2 + pos_ang_z**2 + (h_ang / 4.0) ** 2)
    e_az = (az - exp.azimuth_deg) / s_az
    e_el = (el - exp.elevation_deg) / s_el
    if abs(e_az) > config.gate_sigma or abs(e_el) > config.gate_sigma:
        return INFEASIBLE

    h_box = max(1.0, y2 - y1)
    truncated = y1 <= 1.0 or y2 >= camera.height - 1.0
    ratio = h_box / exp.expected_height_px
    if ratio > config.max_size_ratio or (ratio < config.min_size_ratio and not truncated):
        return INFEASIBLE
    e_size = 0.0 if truncated and ratio < 1.0 else math.log(ratio) / config.size_log_sigma
    if abs(e_size) > config.gate_sigma + 1.0:
        return INFEASIBLE
    return e_az * e_az + e_el * e_el + e_size * e_size


class IffAssociator:
    """Stateful tracker + friendly association. One instance per camera stream."""

    def __init__(self, camera: CameraModel, config: Optional[IffConfig] = None):
        self.camera = camera
        self.config = config or IffConfig()
        self._tracks: List[_Track] = []
        self._next_id = 1

    @property
    def tracks(self) -> List[_Track]:
        return list(self._tracks)

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    # ------------------------------------------------------------------ tracking
    def _update_tracks(self, detections: Sequence[Detection]) -> List[_Track]:
        c = self.config
        for t in self._tracks:
            t.bbox = t.bbox + t.velocity
            t.age += 1
        matched_tracks: set = set()
        matched_dets: set = set()
        if self._tracks and detections:
            cost = np.ones((len(self._tracks), len(detections)))
            for i, t in enumerate(self._tracks):
                for j, d in enumerate(detections):
                    if t.category == d.category:
                        cost[i, j] = 1.0 - iou(t.bbox, d.as_xyxy())
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                if cost[i, j] <= 1.0 - c.track_iou_gate:
                    t, d = self._tracks[i], detections[j]
                    meas = np.array(d.as_xyxy(), dtype=np.float64)
                    prev = t.bbox - t.velocity
                    new_bbox = c.bbox_smoothing * meas + (1.0 - c.bbox_smoothing) * t.bbox
                    t.velocity = (
                        c.velocity_smoothing * (new_bbox - prev)
                        + (1.0 - c.velocity_smoothing) * t.velocity
                    )
                    t.bbox = new_bbox
                    t.confidence = d.confidence
                    t.hits += 1
                    t.misses = 0
                    matched_tracks.add(i)
                    matched_dets.add(j)
        for i, t in enumerate(self._tracks):
            if i not in matched_tracks:
                t.misses += 1
                t.velocity *= 0.5
        self._tracks = [t for t in self._tracks if t.misses <= c.max_coast_frames]
        for j, d in enumerate(detections):
            if j not in matched_dets:
                self._tracks.append(
                    _Track(
                        track_id=self._next_id,
                        bbox=np.array(d.as_xyxy(), dtype=np.float64),
                        velocity=np.zeros(4),
                        category=d.category,
                        confidence=d.confidence,
                    )
                )
                self._next_id += 1
        return [t for t in self._tracks if t.misses == 0]

    # ------------------------------------------------------------------ IFF
    def update(
        self,
        detections: Sequence[Detection],
        pose: OperatorPose,
        teammates: Sequence[TeammateTrack],
        now: Optional[float] = None,
    ) -> IffResult:
        c = self.config
        fresh = self._update_tracks(detections)
        expected = expected_teammates(pose, self.camera, teammates, c, now)
        live = [e for e in expected if not e.stale]
        stale = [e for e in expected if e.stale]
        persons = [t for t in fresh if t.category == "person"]

        assignments: List[Tuple[int, int, float]] = []
        if persons and live:
            cost = np.full((len(persons), len(live)), INFEASIBLE)
            for i, t in enumerate(persons):
                for j, e in enumerate(live):
                    cost[i, j] = association_cost(t.bbox, e, self.camera, c)
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                if cost[i, j] < INFEASIBLE:
                    assignments.append((persons[i].track_id, live[j].node_id, float(cost[i, j])))

        assigned = {tid: (nid, cst) for tid, nid, cst in assignments}
        fresh_ids = {t.track_id for t in fresh}
        live_ids = {e.node_id for e in live}
        for t in self._tracks:
            if t.track_id in fresh_ids:
                hit = assigned.get(t.track_id)
                for nid in list(t.scores):
                    if hit is None or nid != hit[0]:
                        t.scores[nid] -= c.miss_decay
                if hit is not None:
                    nid, cst = hit
                    gain = c.hit_gain + (c.strong_match_bonus if cst < c.strong_match_cost else 0.0)
                    t.scores[nid] = min(c.max_score, t.scores.get(nid, 0.0) + gain)
            else:
                for nid in list(t.scores):
                    t.scores[nid] -= c.coast_decay
            for nid in [n for n, s in t.scores.items() if s <= 0.0]:
                del t.scores[nid]

        self._resolve_friendly(live_ids)
        for t in self._tracks:
            t.status = self._classify(t, live, stale)

        by_node = {e.node_id: e for e in expected}
        out: List[IffTrack] = []
        for t in self._tracks:
            node = by_node.get(t.friendly_node) if t.status == IffStatus.FRIENDLY else None
            h = t.bbox[3] - t.bbox[1]
            if node is not None:
                rng: Optional[float] = node.range_m
            elif t.category == "person" and h > 1.0:
                rng = self.camera.fy * c.person_height_m / h
            else:
                rng = None
            out.append(
                IffTrack(
                    track_id=t.track_id,
                    bbox=tuple(float(v) for v in t.bbox),
                    category=t.category,
                    status=t.status,
                    confidence=t.confidence,
                    node_id=node.node_id if node else None,
                    callsign=node.callsign if node else None,
                    team_color=node.team_color if node else None,
                    range_m=rng,
                    friendly_score=t.scores.get(t.friendly_node, 0.0) if t.friendly_node else 0.0,
                    age=t.age,
                    misses=t.misses,
                )
            )
        return IffResult(out, expected, assignments)

    def _resolve_friendly(self, live_ids: set) -> None:
        """Apply confirm/release hysteresis and keep each node on one track."""
        c = self.config
        candidates: List[Tuple[float, _Track, int]] = []
        for t in self._tracks:
            if not t.scores:
                continue
            nid, score = max(t.scores.items(), key=lambda kv: kv[1])
            threshold = c.release_score if t.friendly_node == nid else c.confirm_score
            if score >= threshold:
                candidates.append((score, t, nid))
        for t in self._tracks:
            t.friendly_node = None
        taken: set = set()
        for score, t, nid in sorted(candidates, key=lambda x: -x[0]):
            if nid in taken:
                continue
            t.friendly_node = nid
            t.ever_friendly = True
            taken.add(nid)

    def _classify(
        self,
        t: _Track,
        live: Sequence[ExpectedTeammate],
        stale: Sequence[ExpectedTeammate],
    ) -> IffStatus:
        c = self.config
        if t.friendly_node is not None:
            return IffStatus.FRIENDLY
        if t.category != "person" or t.ever_friendly or t.scores:
            return IffStatus.UNVERIFIED
        if t.hits < c.tango_min_hits:
            return IffStatus.UNVERIFIED
        if any(association_cost(t.bbox, e, self.camera, c) < INFEASIBLE for e in live):
            return IffStatus.UNVERIFIED
        if stale:
            u = (t.bbox[0] + t.bbox[2]) / 2.0
            v = (t.bbox[1] + t.bbox[3]) / 2.0
            az, _ = pixel_to_angles(self.camera, u, v)
            if any(abs(az - e.azimuth_deg) <= c.stale_gate_deg for e in stale if e.pixel is not None):
                return IffStatus.UNVERIFIED
        return IffStatus.TANGO
