"""TeamLynx Phase 2 demo: edge mode + YOLO + IFF + HUD on a live or synthetic feed.

Examples:
    # CI / no hardware: synthetic scene with simulated detector output
    python -m lynx.hud.demo --source synthetic --frames 300 --headless \
        --save-frame hud.png --record hud.mp4

    # Webcam 0 with YOLO11n, two static teammates and a ping
    python -m lynx.hud.demo --source 0 --weights yolo11n.pt \
        --teammate VIPER:2:3,25,1.65 --teammate GHOST:3:-8,15,1.65:blue \
        --ping OBJ:0,60,0

    # Video file in edge mode, edge view in the PiP
    python -m lynx.hud.demo --source clip.mp4 --edge --pip edge

Keys (when a display is available):
    q / Esc quit    e toggle edge mode    p cycle PiP    a/d yaw -/+5 deg
    w/s pitch +/-2 deg    space pause    c save screenshot
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np

from lynx.hud.renderer import HudRenderer
from lynx.hud.types import HudState, WorldPing
from lynx.vision.edge import EdgeConfig, EdgeFilter
from lynx.vision.iff import IffAssociator
from lynx.vision.types import CameraModel, OperatorPose, TeammateTrack

log = logging.getLogger("lynx.demo")
PIP_MODES = ("auto", "edge", "raw", "topdown", "none")


def parse_teammate(spec: str) -> TeammateTrack:
    """CALLSIGN:NODE:x,y,z[:color]"""
    parts = spec.split(":")
    if len(parts) not in (3, 4):
        raise argparse.ArgumentTypeError("teammate format is CALLSIGN:NODE:x,y,z[:color]")
    x, y, z = (float(v) for v in parts[2].split(","))
    color = parts[3] if len(parts) == 4 else "green"
    return TeammateTrack(int(parts[1]), parts[0], x, y, z, color)


def parse_ping(spec: str) -> Tuple[str, float, float, float]:
    """LABEL:x,y,z"""
    label, xyz = spec.split(":", 1)
    x, y, z = (float(v) for v in xyz.split(","))
    return label, x, y, z


def parse_pose(spec: str) -> OperatorPose:
    vals = [float(v) for v in spec.split(",")]
    if len(vals) != 6:
        raise argparse.ArgumentTypeError("pose format is x,y,z,yaw,pitch,roll")
    return OperatorPose(*vals, node_id=1, callsign="LYNX-1")


def open_capture(src: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
    if not cap.isOpened():
        raise SystemExit(f"cannot open video source {src!r}")
    return cap


class Demo:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.synthetic = args.source == "synthetic"
        self.scene = None
        self.cap = None
        self.aux_cap = None
        if self.synthetic:
            from lynx.vision.synthetic import SyntheticScene

            self.scene = SyntheticScene(args.width, args.height, args.hfov, seed=args.seed)
            self.camera = self.scene.camera
        else:
            self.cap = open_capture(args.source)
            if args.width and args.height:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
            self.camera = None  # set from the first frame
        if args.pip not in PIP_MODES:
            self.aux_cap = open_capture(args.pip)
            self.pip_mode = "source"
        else:
            self.pip_mode = args.pip
        self.edge_mode = args.edge
        self.edge = EdgeFilter(EdgeConfig(operator=args.edge_op), backend=args.backend)
        self.edge_pip = EdgeFilter(EdgeConfig(operator=args.edge_op, base_mode="black"), backend=args.backend)
        self.detector = self._make_detector()
        self.hud = HudRenderer()
        self.iff: Optional[IffAssociator] = None
        self.pose = args.pose or OperatorPose(0.0, 0.0, 1.7, 0.0, 0.0, 0.0, node_id=1, callsign="LYNX-1")
        self.static_teammates: List[TeammateTrack] = list(args.teammate or [])
        self.static_pings = [WorldPing(i + 1, x, y, z, lab, "LYNX-1") for i, (lab, x, y, z) in enumerate(args.ping or [])]
        self.fps = 0.0
        self.writer = None
        self.display = not args.headless
        self.last_hud: Optional[np.ndarray] = None

    def _make_detector(self):
        mode = self.args.detector
        if mode == "auto":
            mode = "sim" if self.synthetic else "yolo"
        if mode == "sim" and not self.synthetic:
            raise SystemExit("--detector sim only applies to --source synthetic")
        self.detector_mode = mode
        if mode != "yolo":
            return None
        from lynx.vision.detect import DetectorUnavailableError, YoloDetector

        try:
            return YoloDetector(self.args.weights, conf=self.args.conf, imgsz=self.args.imgsz, device=self.args.device)
        except DetectorUnavailableError as exc:
            if self.args.allow_no_detector:
                log.error("YOLO unavailable, continuing without detection: %s", exc)
                self.detector_mode = "none"
                return None
            raise SystemExit(f"YOLO unavailable: {exc}\n(pass --detector none or --allow-no-detector to run without it)")

    def next_inputs(self):
        if self.synthetic:
            sf = self.scene.step(1.0 / 30.0)
            pings = [WorldPing(p.ping_id, p.x, p.y, p.z, p.label, p.owner) for p in sf.pings]
            dets = sf.detections if self.detector_mode == "sim" else None
            return sf.frame, sf.pose, sf.teammates, pings, dets, sf.t
        ok, frame = self.cap.read()
        if not ok:
            return None
        if self.camera is None:
            h, w = frame.shape[:2]
            self.camera = CameraModel(w, h, self.args.hfov)
        now = time.monotonic()
        tms = [TeammateTrack(t.node_id, t.callsign, t.x, t.y, t.z, t.team_color, now) for t in self.static_teammates]
        return frame, self.pose, tms, self.static_pings, None, now

    def aux_frame(self, raw: np.ndarray) -> Tuple[Optional[np.ndarray], str]:
        mode = self.pip_mode
        if mode == "auto":
            mode = "topdown" if self.synthetic else ("raw" if self.edge_mode else "edge")
        if mode == "none":
            return None, ""
        if mode == "topdown" and self.synthetic:
            return self.scene.render_topdown((320, 240)), "AUX: UAV"
        if mode == "source" and self.aux_cap is not None:
            ok, f = self.aux_cap.read()
            if not ok:
                self.aux_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, f = self.aux_cap.read()
            return (f if ok else None), "AUX: FEED"
        small = cv2.resize(raw, (raw.shape[1] // 3, raw.shape[0] // 3), interpolation=cv2.INTER_AREA)
        if mode == "edge":
            return self.edge_pip.process(small), "AUX: EDGE"
        return small, "AUX: RAW"

    def step(self) -> bool:
        inputs = self.next_inputs()
        if inputs is None:
            return False
        frame, pose, teammates, pings, dets, now = inputs
        if self.iff is None:
            self.iff = IffAssociator(self.camera)
        t0 = time.perf_counter()
        if dets is None:
            dets = self.detector.detect(frame) if self.detector is not None else []
        t_det = time.perf_counter()
        result = self.iff.update(dets, pose, teammates, now=now)
        base = self.edge.process(frame) if self.edge_mode else frame
        t_edge = time.perf_counter()
        aux, aux_label = self.aux_frame(frame)
        telemetry = {
            "LINK": f"{len(teammates)}/{len(teammates)} NODES",
            "PIPE": f"{(t_edge - t0) * 1000:5.1f}ms  DET {(t_det - t0) * 1000:4.1f}ms",
            "FPS": f"{self.fps:4.1f}  EDGE {self.edge.backend.upper()}",
            "DET": self.detector_mode.upper(),
        }
        tango = sum(1 for t in result.tracks if t.status.value == "TANGO")
        state = HudState(
            pose=pose,
            camera=self.camera,
            tracks=result.tracks,
            expected=result.expected,
            teammates=teammates,
            pings=pings,
            telemetry=telemetry,
            aux_frame=aux,
            aux_label=aux_label,
            mode="EDGE" if self.edge_mode else "DAY",
            alerts=[f"! {tango} TANGO IN VIEW"] if tango else [],
        )
        hud = self.hud.render(base, state, copy=not self.edge_mode)
        dt = time.perf_counter() - t0
        self.fps = 0.9 * self.fps + 0.1 * (1.0 / max(dt, 1e-6)) if self.fps else 1.0 / max(dt, 1e-6)
        self.last_hud = hud
        if self.args.record:
            if self.writer is None:
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self.writer = cv2.VideoWriter(self.args.record, fourcc, 30.0, (hud.shape[1], hud.shape[0]))
            self.writer.write(hud)
        return True

    def handle_key(self, key: int) -> bool:
        if key in (ord("q"), 27):
            return False
        if key == ord("e"):
            self.edge_mode = not self.edge_mode
        elif key == ord("p"):
            modes = list(PIP_MODES)
            cur = self.pip_mode if self.pip_mode in modes else "auto"
            self.pip_mode = modes[(modes.index(cur) + 1) % len(modes)]
        elif key == ord("c") and self.last_hud is not None:
            name = f"lynx-hud-{int(time.time())}.png"
            cv2.imwrite(name, self.last_hud)
            print(f"saved {name}")
        elif key in (ord("a"), ord("d"), ord("w"), ord("s")) and not self.synthetic:
            p = self.pose
            dyaw = {ord("a"): -5.0, ord("d"): 5.0}.get(key, 0.0)
            dpit = {ord("w"): 2.0, ord("s"): -2.0}.get(key, 0.0)
            self.pose = OperatorPose(p.x, p.y, p.z, (p.yaw + dyaw) % 360, p.pitch + dpit, p.roll,
                                     p.node_id, p.callsign, p.team_color)
        return True

    def run(self) -> int:
        n = 0
        paused = False
        try:
            while self.args.frames <= 0 or n < self.args.frames:
                if not paused:
                    if not self.step():
                        break
                    n += 1
                    if self.args.save_at and n == self.args.save_at and self.args.save_frame:
                        self._save()
                if self.display:
                    try:
                        cv2.imshow("TeamLynx HUD", self.last_hud)
                        key = cv2.waitKey(1) & 0xFF
                    except cv2.error:
                        log.warning("no GUI backend in this OpenCV build; continuing headless")
                        self.display = False
                        continue
                    if key == ord(" "):
                        paused = not paused
                    elif key != 255 and not self.handle_key(key):
                        break
        finally:
            if self.args.save_frame and not self.args.save_at:
                self._save()
            if self.writer is not None:
                self.writer.release()
            for c in (self.cap, self.aux_cap):
                if c is not None:
                    c.release()
            if self.display:
                cv2.destroyAllWindows()
        print(f"processed {n} frames, last pipeline rate {self.fps:.1f} fps")
        return 0

    def _save(self) -> None:
        if self.last_hud is None:
            return
        d = os.path.dirname(os.path.abspath(self.args.save_frame))
        os.makedirs(d, exist_ok=True)
        cv2.imwrite(self.args.save_frame, self.last_hud)
        print(f"saved HUD frame to {self.args.save_frame}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="synthetic", help="'synthetic', camera index, or video path")
    ap.add_argument("--detector", choices=("auto", "yolo", "sim", "none"), default="auto",
                    help="auto = sim for synthetic, yolo otherwise; sim = simulated detector output from scene truth")
    ap.add_argument("--allow-no-detector", action="store_true", help="continue without YOLO if unavailable")
    ap.add_argument("--weights", default="yolo11n.pt")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default=None, help="ultralytics device, e.g. 'cpu', '0'")
    ap.add_argument("--hfov", type=float, default=78.0, help="camera horizontal FOV in degrees")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--edge", action="store_true", help="start in EagleEye edge mode")
    ap.add_argument("--edge-op", choices=("laplacian", "sobel"), default="sobel")
    ap.add_argument("--backend", choices=("auto", "cpu", "cuda"), default="auto")
    ap.add_argument("--pip", default="auto", help=f"one of {PIP_MODES} or an aux video source")
    ap.add_argument("--teammate", action="append", type=parse_teammate, help="CALLSIGN:NODE:x,y,z[:color]")
    ap.add_argument("--ping", action="append", type=parse_ping, help="LABEL:x,y,z")
    ap.add_argument("--pose", type=parse_pose, default=None, help="static operator pose x,y,z,yaw,pitch,roll")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = until end / quit)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save-frame", default="", help="write a PNG of the final (or --save-at) frame")
    ap.add_argument("--save-at", type=int, default=0, help="frame index at which to write --save-frame")
    ap.add_argument("--record", default="", help="write the HUD stream to an mp4")
    ap.add_argument("--seed", type=int, default=0)
    return ap


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.source == "synthetic" and args.frames <= 0 and args.headless:
        args.frames = 300
    return Demo(args).run()


if __name__ == "__main__":
    sys.exit(main())
