"""TeamLynx headset client: camera + edge mode + YOLO + IFF on live relay telemetry + HUD.

    lynx-relay
    lynx-headset --node 1 --callsign ALPHA --team blue --source synthetic
    lynx-headset --node 2 --callsign BRAVO --team blue --source synthetic --y 20 --heading 180
    lynx-headset --node 3 --callsign CHARLIE --source 0 --hfov 78          # webcam + YOLO11n

Per frame:

1. read the head pose (and rail-switch trigger) from the pose source;
2. publish telemetry to the relay at ``--rate`` Hz; on a trigger, raycast from the reticle and
   publish a ping;
3. snapshot the relay state, adapt nodes -> ``TeammateTrack`` and pings -> ``WorldPing``;
4. grab a camera frame, detect (YOLO, or simulated output for the synthetic scene), associate
   with teammate telemetry (IFF);
5. optional EagleEye edge mode, then the HUD overlay (IFF boxes, BFT diamonds, ping chevrons and
   off-screen arrows, compass, radar, PiP).

Keys (display mode): q/Esc quit, e edge mode, p cycle PiP, 1-5 ping type
(mark/contact/move/danger/rally), space ping, x or Backspace cancel my last ping, c screenshot,
n arm/disarm the IR illuminator (with --ir-interlock),
plus the pose-source keys (keyboard: a/d yaw, w/s pitch, [/] roll, i/k/j/l move, r level).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Sequence

import cv2
import numpy as np

from lynx.headset import adapters
from lynx.headset.pose import PoseSource, PoseSourceUnavailableError, available_pose_sources, create_pose_source
from lynx.headset.sources import CameraFrame, CameraSource, open_camera_source
from lynx.hud.renderer import HudRenderer
from lynx.hud.types import HudState, WorldPing
from lynx.net.client import BackgroundClient, LynxClient
from lynx.net.schema import PingType, Team
from lynx.spatial import Pose, RayHit, raycast_from_pose
from lynx.vision.edge import EdgeConfig, EdgeFilter
from lynx.vision.iff import IffAssociator, IffResult
from lynx.vision.types import CameraModel, IffStatus, OperatorPose, TeammateTrack

if TYPE_CHECKING:
    from lynx.hw.ir_interlock import IrInterlock

log = logging.getLogger("lynx.headset")

PIP_MODES = ("auto", "edge", "raw", "topdown", "none")
PING_KEYS = {ord(str(int(t) + 1)): t for t in PingType}
KEY_BACKSPACE = (8, 127)


@dataclass
class HeadsetConfig:
    url: str = "ws://127.0.0.1:8765"
    node_id: int = 1
    callsign: str = "ALPHA"
    team: Team = Team.BLUE
    encoding: str = "binary"
    telemetry_hz: float = 20.0
    ping_ttl_s: float = 120.0
    max_range: float = 150.0
    fallback_range: float = 50.0
    detector: str = "auto"  # auto | yolo | sim | none
    allow_no_detector: bool = False
    weights: str = "yolo11n.pt"
    conf: float = 0.35
    imgsz: int = 640
    device: Optional[str] = None
    edge: bool = False
    edge_op: str = "sobel"
    backend: str = "auto"
    pip: str = "auto"
    ir_imu: str = "ok"  # IR interlock IMU requirement: "ok" (LinkState.OK) or "usable" (OK or DEGRADED)


class HeadsetHook(Protocol):
    """Per-frame extension point (``lynx.field``: link-loss holdover, drift alerts, session log).

    ``teammates`` may replace the relay-derived teammate list (e.g. hold stale friendlies);
    ``annotate`` may edit the alert lines and telemetry readout before the HUD is drawn.
    """

    def teammates(self, teammates: List[TeammateTrack], nodes, now: float, connected: bool) -> List[TeammateTrack]: ...

    def annotate(self, client: "HeadsetClient", pose: OperatorPose, now: float, alerts: List[str],
                 telemetry: Dict[str, str]) -> None: ...


class HeadsetClient:
    """One headset. ``step()`` runs one frame and returns the composited HUD image."""

    def __init__(
        self,
        config: HeadsetConfig,
        camera: CameraSource,
        pose_source: PoseSource,
        net: Optional[BackgroundClient] = None,
        ir: Optional["IrInterlock"] = None,
        hooks: Sequence[HeadsetHook] = (),
    ) -> None:
        self.config = config
        self.ir = ir
        self.camera_source = camera
        self.pose_source = pose_source
        self.hooks: List[HeadsetHook] = list(hooks)
        self.client = (
            net.client
            if net is not None
            else LynxClient(config.url, config.node_id, config.callsign, config.team, encoding=config.encoding)
        )
        self.net = net or BackgroundClient(self.client)
        self.renderer = HudRenderer()
        self.edge_mode = config.edge
        self.edge = EdgeFilter(EdgeConfig(operator=config.edge_op), backend=config.backend)
        self.edge_pip = EdgeFilter(EdgeConfig(operator=config.edge_op, base_mode="black"), backend=config.backend)
        self.pip_mode = config.pip
        self.selected_ping = PingType.MARK
        self.detector_mode, self.detector = self._make_detector()
        self.iff: Optional[IffAssociator] = None
        self._iff_camera: Optional[CameraModel] = None
        self._next_tx = 0.0
        self._pending_ping = False
        self.fps = 0.0
        self.frames = 0
        self.last_ping_msg = ""
        self.last_hit: Optional[RayHit] = None
        self.last_pose: Optional[OperatorPose] = None
        self.last_teammates: List[TeammateTrack] = []
        self.last_pings: List[WorldPing] = []
        self.last_result: Optional[IffResult] = None
        self.last_state: Optional[HudState] = None
        self.last_hud: Optional[np.ndarray] = None

    # ------------------------------------------------------------------ setup
    def _make_detector(self):
        mode = self.config.detector
        synthetic = getattr(self.camera_source, "name", "") == "synthetic"
        if mode == "auto":
            mode = "sim" if synthetic else "yolo"
        if mode == "sim" and not synthetic:
            raise SystemExit("--detector sim only applies to --source synthetic")
        if mode != "yolo":
            return mode, None
        from lynx.vision.detect import DetectorUnavailableError, YoloDetector

        try:
            return "yolo", YoloDetector(self.config.weights, conf=self.config.conf, imgsz=self.config.imgsz,
                                        device=self.config.device)
        except DetectorUnavailableError as exc:
            if self.config.allow_no_detector:
                log.error("YOLO unavailable, continuing without detection: %s", exc)
                return "none", None
            raise SystemExit(f"YOLO unavailable: {exc}\n(pass --detector none or --allow-no-detector)")

    def start(self) -> "HeadsetClient":
        self.net.start()
        return self

    def wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.net.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.net.connected

    def stop(self) -> None:
        if self.ir is not None:
            self.ir.close()
        self.net.stop()
        self.camera_source.close()
        self.pose_source.close()

    # ------------------------------------------------------------------ pings
    def request_ping(self) -> None:
        """Queue a ping for the next frame (same path as a rail-switch trigger)."""
        self._pending_ping = True

    def drop_ping(self, pose: Pose, ping_type: Optional[PingType] = None) -> Optional[RayHit]:
        hit = raycast_from_pose(pose, max_range=self.config.max_range, fallback_range=self.config.fallback_range)
        x, y, z = (float(c) for c in hit.point)
        ping_type = self.selected_ping if ping_type is None else ping_type
        ping = self.net.send_ping(x, y, z, ping_type, self.config.ping_ttl_s)
        self.last_hit = hit
        self.last_ping_msg = f"#{ping.ping_id if ping else '?'} {hit.distance:.0f}m {hit.kind.value}"
        log.info("ping %s at (%.1f, %.1f, %.1f)", self.last_ping_msg, x, y, z)
        return hit

    def handle_rail(self, commands, pose: Pose) -> None:
        """Rail-switch gestures from a hardware pose source (``lynx.hw`` ``SerialPoseSample.rail``).

        single -> ping of the selected type, double -> CONTACT ping, long -> cancel my last ping.
        Pings raycast from the head attitude at the press (``cmd.aim``) when the source has it.
        """
        for cmd in commands:
            action = getattr(cmd.action, "value", cmd.action)
            if action == "cancel-last":
                self.cancel_last_ping()
                continue
            if action not in ("ping", "ping-contact"):
                continue
            aim = cmd.aim.to_pose(pose.position) if getattr(cmd, "aim", None) is not None else pose
            self.drop_ping(aim, PingType.CONTACT if action == "ping-contact" else None)

    def cancel_last_ping(self) -> None:
        mine = self.client.own_pings()
        if mine:
            self.net.cancel_ping(mine[-1].ping.ping_id)

    # ------------------------------------------------------------------ IR interlock
    def _imu_health(self):
        link = getattr(self.pose_source, "link", None)
        health = getattr(link, "health", None)
        return health() if callable(health) else None

    def update_ir(self, pose: Pose):
        """Feed the IR interlock (if any) with this frame's mode, attitude and IMU health."""
        if self.ir is None:
            return None
        try:
            _, pitch, roll = pose.euler
            health = self._imu_health()
            if health is None:
                imu_ok, why = False, "no head tracker"
            else:
                imu_ok = health.ok if self.config.ir_imu == "ok" else health.usable
                why = health.state.value
        except Exception as exc:  # noqa: BLE001 - the interlock treats bad inputs as a trip
            pitch = roll = None
            imu_ok, why = False, f"error {exc!r}"
        return self.ir.update(self.edge_mode, pitch, roll, imu_ok, imu_reason=why)

    # ------------------------------------------------------------------ frame
    def _detect(self, cf: CameraFrame):
        if self.detector_mode == "sim":
            return cf.sim_detections or []
        if self.detector is not None:
            return self.detector.detect(cf.image)
        return []

    def _aux(self, cf: CameraFrame):
        mode = self.pip_mode
        if mode == "auto":
            mode = "topdown" if cf.aux is not None else ("raw" if self.edge_mode else "edge")
        if mode == "none":
            return None, ""
        if mode == "topdown":
            return (cf.aux, cf.aux_label) if cf.aux is not None else (None, "")
        raw = cf.image
        small = cv2.resize(raw, (raw.shape[1] // 3, raw.shape[0] // 3), interpolation=cv2.INTER_AREA)
        if mode == "edge":
            return self.edge_pip.process(small), "AUX: EDGE"
        return small, "AUX: RAW"

    def step(self, now: Optional[float] = None) -> Optional[np.ndarray]:
        t0 = time.perf_counter()
        now = time.monotonic() if now is None else now
        cfg = self.config
        sample = self.pose_source.read(now)
        ir_status = self.update_ir(sample.pose)
        pose = adapters.operator_pose(sample.pose, cfg.node_id, cfg.callsign, cfg.team)

        if now >= self._next_tx:
            self.net.send_telemetry(pose.x, pose.y, pose.z, pose.yaw, pose.pitch, pose.roll, flags=sample.flags)
            self._next_tx = now + 1.0 / cfg.telemetry_hz
        rail = getattr(sample, "rail", ())
        if rail:
            self.handle_rail(rail, sample.pose)
        if (sample.trigger and not rail) or self._pending_ping:
            self._pending_ping = False
            self.drop_ping(sample.pose)

        nodes, pings = self.net.snapshot()
        teammates = adapters.teammates_from_nodes(nodes, cfg.node_id)
        for hook in self.hooks:
            teammates = hook.teammates(teammates, nodes, now, self.net.connected)
        world_pings = adapters.world_pings_from_state(pings, nodes, cfg.node_id, cfg.callsign)

        cf = self.camera_source.read(pose, teammates)
        if cf is None:
            return None
        if self.iff is None or self._iff_camera != cf.camera:
            self.iff = IffAssociator(cf.camera)
            self._iff_camera = cf.camera
        t_cap = time.perf_counter()
        dets = self._detect(cf)
        t_det = time.perf_counter()
        result = self.iff.update(dets, pose, teammates, now=now)
        base = self.edge.process(cf.image) if self.edge_mode else cf.image
        aux, aux_label = self._aux(cf)

        connected = self.net.connected
        tango = sum(1 for t in result.tracks if t.status == IffStatus.TANGO)
        alerts = [] if connected else ["! RELAY LINK DOWN"]
        if tango:
            alerts.append(f"! {tango} TANGO IN VIEW")
        if ir_status is not None and ir_status.state.value == "lockout":
            alerts.append(f"! {ir_status.summary()}")
        telemetry = {
            "LINK": f"{'UP' if connected else 'DOWN'} {len(teammates)} NODES {len(world_pings)} PINGS",
            "PIPE": f"{(time.perf_counter() - t_cap) * 1000:5.1f}ms DET {(t_det - t_cap) * 1000:4.1f}ms",
            "FPS": f"{self.fps:4.1f} EDGE {self.edge.backend.upper()} DET {self.detector_mode.upper()}",
            "PING": f"SEL {self.selected_ping.name}" + (f"  LAST {self.last_ping_msg}" if self.last_ping_msg else ""),
        }
        if ir_status is not None:
            telemetry["IR"] = ir_status.summary()
        for hook in self.hooks:
            hook.annotate(self, pose, now, alerts, telemetry)
        state = HudState(
            pose=pose,
            camera=cf.camera,
            tracks=result.tracks,
            expected=result.expected,
            teammates=teammates,
            pings=world_pings,
            telemetry=telemetry,
            aux_frame=aux,
            aux_label=aux_label,
            mode="EDGE" if self.edge_mode else "DAY",
            alerts=alerts,
        )
        hud = self.renderer.render(base, state, copy=not self.edge_mode)

        dt = time.perf_counter() - t0
        self.fps = 0.9 * self.fps + 0.1 / max(dt, 1e-6) if self.fps else 1.0 / max(dt, 1e-6)
        self.frames += 1
        self.last_pose, self.last_teammates, self.last_pings = pose, teammates, world_pings
        self.last_result, self.last_state, self.last_hud = result, state, hud
        return hud

    # ------------------------------------------------------------------ input
    def handle_key(self, key: int) -> bool:
        """Returns False to quit."""
        if key in (ord("q"), 27):
            return False
        if key == ord("e"):
            self.edge_mode = not self.edge_mode
        elif key == ord("p"):
            cur = self.pip_mode if self.pip_mode in PIP_MODES else "auto"
            self.pip_mode = PIP_MODES[(PIP_MODES.index(cur) + 1) % len(PIP_MODES)]
        elif key in PING_KEYS:
            self.selected_ping = PING_KEYS[key]
        elif key == ord("n") and self.ir is not None:
            self.ir.toggle_arm()
        elif key == ord("x") or key in KEY_BACKSPACE:
            self.cancel_last_ping()
        elif key == ord("c") and self.last_hud is not None:
            name = f"lynx-headset-{self.config.callsign}-{int(time.time())}.png"
            cv2.imwrite(name, self.last_hud)
            log.info("saved %s", name)
        elif not self.pose_source.handle_key(key) and key == 32:
            self.request_ping()
        return True


def save_png(path: str, img: np.ndarray) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    cv2.imwrite(path, img)
    log.info("saved HUD frame to %s", path)


def run(client: HeadsetClient, args: argparse.Namespace) -> int:
    display = not args.headless
    window = f"TeamLynx headset - {client.config.callsign} [{client.config.node_id}]"
    writer = None
    ping_at = set(args.ping_at or [])
    period = 1.0 / args.fps if args.fps > 0 else 0.0
    client.start()
    if not client.wait_connected(args.wait_link):
        log.warning("relay %s not reachable yet; running with LINK DOWN", client.config.url)
    try:
        while args.frames <= 0 or client.frames < args.frames:
            t_frame = time.monotonic()
            if client.frames + 1 in ping_at:
                client.request_ping()
            hud = client.step()
            if hud is None:
                break
            if args.save_frame and args.save_at and client.frames == args.save_at:
                save_png(args.save_frame, hud)
            if args.record:
                if writer is None:
                    writer = cv2.VideoWriter(args.record, cv2.VideoWriter_fourcc(*"mp4v"), 30.0,
                                             (hud.shape[1], hud.shape[0]))
                writer.write(hud)
            key = 255
            if display:
                try:
                    cv2.imshow(window, hud)
                    key = cv2.waitKey(1) & 0xFF
                except cv2.error:
                    log.warning("no GUI backend in this OpenCV build; continuing headless")
                    display = False
            if key != 255 and not client.handle_key(key):
                break
            rest = period - (time.monotonic() - t_frame)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        pass
    finally:
        if args.save_frame and not args.save_at and client.last_hud is not None:
            save_png(args.save_frame, client.last_hud)
        if writer is not None:
            writer.release()
        client.stop()
        if display:
            cv2.destroyAllWindows()
    log.info("processed %d frames, last pipeline rate %.1f fps", client.frames, client.fps)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    net = ap.add_argument_group("network")
    net.add_argument("--url", default="ws://127.0.0.1:8765", help="relay WebSocket URL")
    net.add_argument("--node", type=int, default=1, help="node id 1..65535")
    net.add_argument("--callsign", default="ALPHA", help="up to 8 ASCII characters")
    net.add_argument("--team", default="blue", choices=[t.name.lower() for t in Team])
    net.add_argument("--encoding", default="binary", choices=["binary", "json"])
    net.add_argument("--rate", type=float, default=20.0, help="telemetry Hz")
    net.add_argument("--ping-ttl", type=float, default=120.0, help="seconds")
    net.add_argument("--wait-link", type=float, default=2.0, help="seconds to wait for the relay at start")

    pose = ap.add_argument_group("pose")
    pose.add_argument("--pose", default="keyboard",
                      help=f"pose source: {', '.join(available_pose_sources())} (e.g. serial:/dev/ttyUSB0)")
    pose.add_argument("--x", type=float, default=0.0, help="start East (m)")
    pose.add_argument("--y", type=float, default=0.0, help="start North (m)")
    pose.add_argument("--eye-height", type=float, default=1.7, help="eye height above the datum plane (m)")
    pose.add_argument("--heading", type=float, default=0.0, help="start compass heading (deg)")
    pose.add_argument("--pitch", type=float, default=0.0, help="start pitch (deg, + up)")
    pose.add_argument("--roll", type=float, default=0.0, help="start roll (deg, + right side down)")
    pose.add_argument("--max-range", type=float, default=150.0, help="ping raycast max range (m)")
    pose.add_argument("--fallback-range", type=float, default=50.0, help="ping range when no ground hit (m)")

    cam = ap.add_argument_group("camera / vision")
    cam.add_argument("--source", default="synthetic", help="'synthetic', camera index, or video path")
    cam.add_argument("--width", type=int, default=1280)
    cam.add_argument("--height", type=int, default=720)
    cam.add_argument("--hfov", type=float, default=78.0, help="camera horizontal FOV (deg)")
    cam.add_argument("--vfov", type=float, default=None, help="vertical FOV (deg); default square pixels")
    cam.add_argument("--no-unknowns", action="store_true", help="synthetic: omit the scripted unknown patrols")
    cam.add_argument("--seed", type=int, default=0)
    cam.add_argument("--detector", choices=("auto", "yolo", "sim", "none"), default="auto",
                     help="auto = sim for synthetic, yolo otherwise")
    cam.add_argument("--allow-no-detector", action="store_true", help="continue without YOLO if unavailable")
    cam.add_argument("--weights", default="yolo11n.pt")
    cam.add_argument("--conf", type=float, default=0.35)
    cam.add_argument("--imgsz", type=int, default=640)
    cam.add_argument("--device", default=None, help="ultralytics device, e.g. 'cpu', '0'")
    cam.add_argument("--edge", action="store_true", help="start in EagleEye edge mode")
    cam.add_argument("--edge-op", choices=("laplacian", "sobel"), default="sobel")
    cam.add_argument("--backend", choices=("auto", "cpu", "cuda"), default="auto")
    cam.add_argument("--pip", default="auto", choices=PIP_MODES)

    ir = ap.add_argument_group("IR illuminator interlock (docs/field/hw-compute-power.md §5)")
    ir.add_argument("--ir-interlock", action="store_true",
                    help="drive IR_EN (Jetson header pin 32): on only when armed, in edge mode, pod deployed, IMU OK")
    ir.add_argument("--ir-gpio", default="auto", choices=("auto", "jetson", "mock"),
                    help="GPIO backend; auto = Jetson.GPIO if importable, else mock")
    ir.add_argument("--ir-pin", type=int, default=32, help="BOARD pin number of IR_EN")
    ir.add_argument("--ir-arm", action="store_true", help="arm at start (otherwise press n)")
    ir.add_argument("--ir-imu", default="ok", choices=("ok", "usable"),
                    help="IMU state required: ok, or usable (also accept DEGRADED, e.g. low mag calibration)")

    out = ap.add_argument_group("output")
    out.add_argument("--headless", action="store_true", help="no window")
    out.add_argument("--fps", type=float, default=30.0, help="frame-rate cap (0 = uncapped)")
    out.add_argument("--frames", type=int, default=0, help="stop after N frames (0 = run until quit)")
    out.add_argument("--save-frame", default="", help="write a PNG of the final (or --save-at) frame")
    out.add_argument("--save-at", type=int, default=0, help="frame number at which to write --save-frame")
    out.add_argument("--record", default="", help="write the HUD stream to an mp4")
    out.add_argument("--ping-at", type=int, action="append",
                     help="drop a ping at this frame number (scripted trigger for headless runs; repeatable)")
    out.add_argument("-v", "--verbose", action="store_true")
    return ap


def config_from_args(args: argparse.Namespace) -> HeadsetConfig:
    return HeadsetConfig(
        url=args.url,
        node_id=args.node,
        callsign=args.callsign,
        team=Team[args.team.upper()],
        encoding=args.encoding,
        telemetry_hz=args.rate,
        ping_ttl_s=args.ping_ttl,
        max_range=args.max_range,
        fallback_range=args.fallback_range,
        detector=args.detector,
        allow_no_detector=args.allow_no_detector,
        weights=args.weights,
        conf=args.conf,
        imgsz=args.imgsz,
        device=args.device,
        edge=args.edge,
        edge_op=args.edge_op,
        backend=args.backend,
        pip=args.pip,
        ir_imu=args.ir_imu,
    )


def make_ir_interlock(args: argparse.Namespace) -> Optional["IrInterlock"]:
    if not args.ir_interlock:
        return None
    from lynx.hw.ir_interlock import IrInterlock, IrInterlockConfig, open_gpio

    try:
        gpio = open_gpio(args.ir_gpio)
    except RuntimeError as exc:
        raise SystemExit(f"IR interlock: {exc}")
    ir = IrInterlock(gpio, IrInterlockConfig(pin=args.ir_pin)).start()
    if args.ir_arm:
        ir.arm()
    return ir


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(args.callsign) > 8 or not args.callsign.isascii():
        raise SystemExit("callsign must be at most 8 ASCII characters")
    initial = Pose.from_euler(args.x, args.y, args.eye_height, args.heading, args.pitch, args.roll)
    try:
        pose_source = create_pose_source(args.pose, initial)
    except (PoseSourceUnavailableError, ValueError) as exc:
        raise SystemExit(str(exc))
    camera = open_camera_source(args.source, args.width, args.height, args.hfov, args.vfov,
                                seed=args.seed, unknowns=not args.no_unknowns)
    ir = make_ir_interlock(args)
    try:
        client = HeadsetClient(config_from_args(args), camera, pose_source, ir=ir)
    except BaseException:
        if ir is not None:
            ir.close()
        raise
    return run(client, args)


if __name__ == "__main__":
    sys.exit(main())
