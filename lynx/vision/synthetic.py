"""Deterministic synthetic scene for CI, benchmarks and the offline demo.

Renders a low-light first-person view of a small compound (buildings, a
vehicle, teammates and unknown persons walking patrol paths) from a slowly
panning operator, using the same pinhole model as the IFF / HUD stack.

Ground-truth boxes are converted into :class:`Detection` objects with
configurable jitter, confidence and dropout, emulating a detector's output
statistics. They are *simulated sensor data*, not a detector substitute:
real runs go through :class:`lynx.vision.detect.YoloDetector`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from lynx.vision.geometry import project_point, world_to_camera
from lynx.vision.types import CameraModel, Detection, OperatorPose, TeammateTrack

EYE_HEIGHT = 1.70
PERSON_HEIGHT = 1.75


@dataclass
class SimPerson:
    name: str
    friendly: bool
    path: Sequence[Tuple[float, float]]  # closed patrol polygon (x, y)
    speed: float = 1.2
    node_id: int = 0
    team_color: str = "green"
    phase: float = 0.0

    def position(self, t: float) -> Tuple[float, float]:
        pts = list(self.path)
        if len(pts) == 1:
            return pts[0]
        segs = list(zip(pts, pts[1:] + pts[:1]))
        lengths = [math.dist(a, b) for a, b in segs]
        total = sum(lengths)
        s = (self.phase + self.speed * t) % total
        for (a, b), L in zip(segs, lengths):
            if s <= L:
                k = s / L if L > 0 else 0.0
                return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
            s -= L
        return pts[0]


@dataclass
class SimBox:
    """Axis-aligned box (building or vehicle): centre x, y; size w (E), d (N), h."""

    cx: float
    cy: float
    w: float
    d: float
    h: float
    shade: int = 70
    kind: str = "building"

    def corners(self) -> np.ndarray:
        x0, x1 = self.cx - self.w / 2, self.cx + self.w / 2
        y0, y1 = self.cy - self.d / 2, self.cy + self.d / 2
        return np.array(
            [[x, y, z] for z in (0.0, self.h) for (x, y) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        )

    def faces(self) -> List[List[int]]:
        return [[0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7], [4, 5, 6, 7]]


@dataclass
class SimPing:
    ping_id: int
    x: float
    y: float
    z: float
    label: str
    owner: str


@dataclass
class SyntheticFrame:
    frame: np.ndarray
    pose: OperatorPose
    camera: CameraModel
    detections: List[Detection]
    teammates: List[TeammateTrack]
    pings: List[SimPing]
    truth: List[Tuple[str, bool, Tuple[float, float, float, float]]]
    t: float


def default_people() -> List[SimPerson]:
    return [
        SimPerson("VIPER", True, [(-6, 18), (6, 20), (8, 30), (-4, 28)], 1.1, node_id=2),
        SimPerson("GHOST", True, [(14, 10), (18, 22)], 0.9, node_id=3, team_color="blue"),
        SimPerson("RAVEN", True, [(-20, -6), (-14, 6)], 1.0, node_id=4),
        SimPerson("U1", False, [(2, 42), (16, 44), (14, 36)], 1.3),
        SimPerson("U2", False, [(-18, 30), (-10, 38)], 0.8),
        SimPerson("U3", False, [(24, -14), (30, -4)], 1.0),
    ]


def default_boxes() -> List[SimBox]:
    return [
        SimBox(-12, 48, 14, 8, 7, 65),
        SimBox(10, 56, 10, 10, 9, 80),
        SimBox(28, 30, 8, 12, 5, 72),
        SimBox(-30, 20, 10, 14, 6, 60),
        SimBox(-26, -24, 12, 8, 8, 75),
        SimBox(30, -30, 9, 9, 4, 68),
        SimBox(4, 34, 4.5, 1.9, 1.6, 95, kind="vehicle"),
    ]


class SyntheticScene:
    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        hfov_deg: float = 78.0,
        seed: int = 0,
        low_light: float = 0.45,
        sensor_noise: float = 6.0,
        det_jitter: float = 0.03,
        det_dropout: float = 0.06,
        telemetry_noise_m: float = 0.4,
        pan_rate_dps: float = 9.0,
        people: Optional[List[SimPerson]] = None,
        boxes: Optional[List[SimBox]] = None,
    ):
        self.camera = CameraModel(width, height, hfov_deg)
        self.rng = np.random.default_rng(seed)
        self.low_light = low_light
        self.sensor_noise = sensor_noise
        self.det_jitter = det_jitter
        self.det_dropout = det_dropout
        self.telemetry_noise_m = telemetry_noise_m
        self.pan_rate_dps = pan_rate_dps
        self.people = people if people is not None else default_people()
        self.boxes = boxes if boxes is not None else default_boxes()
        self.pings = [
            SimPing(1, 10.0, 52.0, 0.0, "OBJ", "VIPER"),
            SimPing(2, -24.0, -22.0, 0.0, "RALLY", "LYNX-1"),
            SimPing(3, 26.0, 26.0, 2.5, "SNIPER?", "GHOST"),
        ]
        self.t = 0.0
        self.yaw0 = -20.0
        self._sky, self._ground = self._backdrops()

    def _backdrops(self) -> Tuple[np.ndarray, np.ndarray]:
        h, w = self.camera.height, self.camera.width
        grad = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        sky = np.dstack([60 + 40 * grad, 45 + 30 * grad, 35 + 20 * grad]) * np.ones((1, w, 1), np.float32)
        tex = self.rng.normal(0, 10, (h, w)).astype(np.float32)
        tex = cv2.GaussianBlur(tex, (0, 0), 3)
        ground = np.dstack([45 + tex, 60 + tex, 55 + tex])
        return np.clip(sky, 0, 255).astype(np.uint8), np.clip(ground, 0, 255).astype(np.uint8)

    def pose_at(self, t: float) -> OperatorPose:
        yaw = (self.yaw0 + 40.0 * math.sin(math.radians(self.pan_rate_dps * t))) % 360.0
        pitch = -2.0 + 1.5 * math.sin(t * 0.7)
        roll = 1.0 * math.sin(t * 0.5)
        return OperatorPose(0.0, 0.0, EYE_HEIGHT, yaw, pitch, roll, node_id=1, callsign="LYNX-1")

    # ------------------------------------------------------------------ render
    def _project(self, pose: OperatorPose, pts: np.ndarray) -> Optional[np.ndarray]:
        p_c = world_to_camera(pose, pts)
        if (p_c[:, 2] <= 0.2).any():
            return None
        intr = self.camera.intrinsics
        return np.array([intr.project_camera_point(p) for p in p_c])

    def _draw_horizon(self, img: np.ndarray, pose: OperatorPose) -> None:
        h, w = img.shape[:2]
        img[:] = self._sky
        pts = []
        for da in (-80.0, 80.0):
            b = math.radians(pose.yaw + da)
            far = (pose.x + 2000 * math.sin(b), pose.y + 2000 * math.cos(b), 0.0)
            p = project_point(pose, self.camera, far)
            if p.pixel is None:
                return
            pts.append(p.pixel)
        (u0, v0), (u1, v1) = pts
        slope = (v1 - v0) / (u1 - u0) if abs(u1 - u0) > 1e-6 else 0.0
        yl = v0 + slope * (0 - u0)
        yr = v0 + slope * (w - u0)
        mask = np.zeros((h, w), np.uint8)
        poly = np.array([[0, yl], [w, yr], [w, h], [0, h]], np.int32)
        cv2.fillPoly(mask, [poly], 255)
        img[mask > 0] = self._ground[mask > 0]

    def _draw_boxes(self, img: np.ndarray, pose: OperatorPose, dets: list, truth: list) -> None:
        order = sorted(self.boxes, key=lambda b: -math.hypot(b.cx - pose.x, b.cy - pose.y))
        for b in order:
            corners = b.corners()
            faces = []
            for f in b.faces():
                q = self._project(pose, corners[f])
                if q is None:
                    continue
                centre = corners[f].mean(axis=0)
                faces.append((float(np.linalg.norm(centre - np.array(pose.position))), f, q))
            faces.sort(key=lambda x: -x[0])
            for k, (_, f, q) in enumerate(faces):
                normal_shade = b.shade + 12 * (f[0] % 3) - (10 if f == [4, 5, 6, 7] else 0)
                col = (normal_shade, normal_shade + 4, normal_shade + 8)
                cv2.fillConvexPoly(img, q.astype(np.int32), col, cv2.LINE_AA)
                cv2.polylines(img, [q.astype(np.int32)], True, (col[0] + 30,) * 3, 1, cv2.LINE_AA)
                if b.kind == "building" and f != [4, 5, 6, 7]:
                    self._draw_windows(img, q, b)
            if b.kind == "vehicle":
                q = self._project(pose, corners)
                if q is not None:
                    x1, y1 = q.min(axis=0)
                    x2, y2 = q.max(axis=0)
                    box = (float(x1), float(y1), float(x2), float(y2))
                    truth.append(("VEHICLE", False, box))
                    self._emit(dets, box, "vehicle", "car")

    def _draw_windows(self, img: np.ndarray, q: np.ndarray, b: SimBox) -> None:
        # bilinear interpolation across the face quad: q = [bl, br, tr, tl]
        bl, br, tr, tl = q
        rows = max(1, int(b.h // 3))
        for r in range(rows):
            for cfrac in (0.2, 0.5, 0.8):
                vf = (r + 0.4) / rows
                def lerp(a, c, k):
                    return a + (c - a) * k
                p_bottom = lerp(bl, br, cfrac)
                p_top = lerp(tl, tr, cfrac)
                centre = lerp(p_bottom, p_top, vf)
                size = max(1, int(abs(p_top[1] - p_bottom[1]) / (rows * 5)))
                lit = (hash((b.cx, b.cy, r, cfrac)) % 7) == 0
                col = (90, 170, 210) if lit else (25, 25, 30)
                cv2.rectangle(
                    img,
                    (int(centre[0] - size), int(centre[1] - size)),
                    (int(centre[0] + size), int(centre[1] + size)),
                    col,
                    -1,
                )

    def _emit(self, dets: list, box: Tuple[float, float, float, float], cat: str, name: str) -> None:
        cam = self.camera
        x1, y1, x2, y2 = box
        if x2 < 0 or y2 < 0 or x1 >= cam.width or y1 >= cam.height:
            return
        if self.rng.random() < self.det_dropout:
            return
        hgt = max(1.0, y2 - y1)
        j = self.rng.normal(0, self.det_jitter * hgt, 4)
        x1, y1, x2, y2 = x1 + j[0], y1 + j[1], x2 + j[2], y2 + j[3]
        x1, x2 = max(0.0, x1), min(cam.width - 1.0, x2)
        y1, y2 = max(0.0, y1), min(cam.height - 1.0, y2)
        if x2 - x1 < 3 or y2 - y1 < 6:
            return
        conf = float(np.clip(self.rng.normal(0.8, 0.08), 0.36, 0.99))
        dets.append(Detection(x1, y1, x2, y2, conf, cat, name))

    def people_at(self, t: float) -> List[Tuple[str, bool, float, float]]:
        """``(name, friendly, x, y)`` ground positions of the scripted people at time ``t``."""
        return [(p.name, p.friendly, *p.position(t)) for p in self.people]

    def _draw_people(
        self, img: np.ndarray, pose: OperatorPose, people: Sequence[Tuple[str, bool, float, float]],
        dets: list, truth: list,
    ) -> None:
        items = [(math.hypot(x - pose.x, y - pose.y), name, friendly, x, y) for name, friendly, x, y in people]
        for _, name, friendly, x, y in sorted(items, key=lambda it: -it[0]):
            foot = project_point(pose, self.camera, (x, y, 0.0))
            top = project_point(pose, self.camera, (x, y, PERSON_HEIGHT))
            if foot.pixel is None or top.pixel is None:
                continue
            (uf, vf), (ut, vt) = foot.pixel, top.pixel
            hgt = vf - vt
            if hgt < 4:
                continue
            u = (uf + ut) / 2.0
            w = 0.32 * hgt
            body = (118, 130, 124)  # IR-illuminated fabric; friend and foe look alike
            head_r = max(2, int(hgt * 0.075))
            cv2.circle(img, (int(ut), int(vt + head_r)), head_r, body, -1, cv2.LINE_AA)
            torso = np.array(
                [[u - w * 0.45, vt + hgt * 0.16], [u + w * 0.45, vt + hgt * 0.16],
                 [u + w * 0.35, vt + hgt * 0.55], [u - w * 0.35, vt + hgt * 0.55]], np.int32)
            cv2.fillConvexPoly(img, torso, body, cv2.LINE_AA)
            for s in (-1, 1):
                cv2.line(img, (int(u + s * w * 0.15), int(vt + hgt * 0.55)),
                         (int(uf + s * w * 0.3), int(vf)), body, max(1, int(w * 0.22)), cv2.LINE_AA)
                cv2.line(img, (int(u + s * w * 0.45), int(vt + hgt * 0.2)),
                         (int(u + s * w * 0.6), int(vt + hgt * 0.5)), body, max(1, int(w * 0.14)), cv2.LINE_AA)
            box = (u - w / 2 - 0.05 * hgt, vt, u + w / 2 + 0.05 * hgt, vf)
            truth.append((name, friendly, box))
            self._emit(dets, box, "person", "person")

    def render_topdown(
        self,
        size: Tuple[int, int] = (320, 240),
        span_m: float = 110.0,
        people: Optional[Sequence[Tuple[str, bool, float, float]]] = None,
        center: Tuple[float, float] = (0.0, 0.0),
    ) -> np.ndarray:
        """North-up overhead view (stand-in for a UAV / chokepoint aux camera)."""
        w, h = size
        img = np.full((h, w, 3), (38, 44, 40), np.uint8)
        k = min(w, h) / span_m

        def px(x: float, y: float) -> Tuple[int, int]:
            return int(w / 2 + (x - center[0]) * k), int(h / 2 - (y - center[1]) * k)

        for b in self.boxes:
            p1 = px(b.cx - b.w / 2, b.cy + b.d / 2)
            p2 = px(b.cx + b.w / 2, b.cy - b.d / 2)
            cv2.rectangle(img, p1, p2, (b.shade + 40,) * 3, -1)
        for _, _, x, y in (self.people_at(self.t) if people is None else people):
            cv2.circle(img, px(x, y), 3, (190, 200, 190), -1, cv2.LINE_AA)
        cv2.circle(img, px(*center), 4, (255, 255, 255), 1, cv2.LINE_AA)
        noise = self.rng.normal(0, 5, img.shape).astype(np.int16)
        return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    def render_view(
        self, pose: OperatorPose, people: Sequence[Tuple[str, bool, float, float]]
    ) -> Tuple[np.ndarray, List[Detection], list]:
        """Render the low-light view from ``pose`` with ``people`` standing at ``(x, y)``.

        Returns ``(frame, simulated_detections, truth)``.
        """
        img = np.empty((self.camera.height, self.camera.width, 3), np.uint8)
        self._draw_horizon(img, pose)
        dets: List[Detection] = []
        truth: list = []
        self._draw_boxes(img, pose, dets, truth)
        self._draw_people(img, pose, people, dets, truth)
        f = img.astype(np.float32) * self.low_light
        f += self.rng.normal(0, self.sensor_noise, f.shape).astype(np.float32)
        return np.clip(f, 0, 255).astype(np.uint8), dets, truth

    def step(self, dt: float = 1.0 / 30.0) -> SyntheticFrame:
        self.t += dt
        t = self.t
        pose = self.pose_at(t)
        frame, dets, truth = self.render_view(pose, self.people_at(t))
        teammates = []
        for p in self.people:
            if not p.friendly:
                continue
            x, y = p.position(t)
            n = self.rng.normal(0, self.telemetry_noise_m, 3)
            teammates.append(
                TeammateTrack(p.node_id, p.name, x + n[0], y + n[1], 1.65 + 0.3 * n[2],
                              p.team_color, timestamp=t)
            )
        return SyntheticFrame(frame, pose, self.camera, dets, teammates, list(self.pings), truth, t)
