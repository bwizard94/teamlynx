"""One operator window of the desktop testbench.

    lynx-sim --node 1 --callsign ALPHA --team blue --x 0 --y 0 --heading 0
    lynx-sim --node 2 --callsign BRAVO --team green --x 15 --y 25 --heading 225
    lynx-sim --node 3 --callsign CHARLIE --team blue --x -20 --y 10 --heading 90

Each window simulates one headset: keyboard drives the pose (stand-in for the IMU), telemetry is
published to the relay at ``--rate`` Hz, SPACE raycasts a ping from the reticle, and the view
renders every squad member and ping through the shared projection math.

With ``--imu-port`` (Phase 3 bench rig, or ``mock://`` for the mock device) head attitude comes
from the ESP32/BNO085 head tracker, the rail switch drives pings (single: ping, double: CONTACT
ping, long: cancel last) and T tares the heading to ``--datum-bearing``. The keyboard still walks.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Optional

import pygame

from lynx.net.client import BackgroundClient, LynxClient
from lynx.net.schema import PingType, Team
from lynx.spatial import raycast_from_pose

from .operator import ControlInput, SimOperator
from .render import Renderer, ViewModel

log = logging.getLogger("lynx.sim")

PING_KEYS = {
    pygame.K_1: PingType.MARK,
    pygame.K_2: PingType.CONTACT,
    pygame.K_3: PingType.MOVE,
    pygame.K_4: PingType.DANGER,
    pygame.K_5: PingType.RALLY,
}


def read_controls(keys: pygame.key.ScancodeWrapper) -> ControlInput:
    def axis(pos: int, neg: int, pos2: Optional[int] = None, neg2: Optional[int] = None) -> float:
        p = keys[pos] or (pos2 is not None and keys[pos2])
        n = keys[neg] or (neg2 is not None and keys[neg2])
        return float(bool(p)) - float(bool(n))

    return ControlInput(
        forward=axis(pygame.K_w, pygame.K_s),
        strafe=axis(pygame.K_d, pygame.K_a),
        turn=axis(pygame.K_e, pygame.K_q, pygame.K_RIGHT, pygame.K_LEFT),
        pitch=axis(pygame.K_UP, pygame.K_DOWN),
        roll=axis(pygame.K_c, pygame.K_z),
        climb=axis(pygame.K_PAGEUP, pygame.K_PAGEDOWN),
        sprint=bool(keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]),
    )


class SimApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.team = Team[args.team.upper()]
        self.operator = SimOperator(
            x=args.x, y=args.y, z=args.eye_height, heading=args.heading, pitch=args.pitch
        )
        self.client = LynxClient(
            args.url, args.node, args.callsign, self.team, encoding=args.encoding
        )
        self.net = BackgroundClient(self.client)
        self.selected = PingType.MARK
        self.show_help = True
        self.minimap_range = 50.0
        self.ping_ttl = args.ping_ttl
        self.last_ping_msg = ""
        self.imu = None
        if getattr(args, "imu_port", None):
            from lynx.hw import ImuCalibration, ImuHeadSource, ImuLink

            cal = ImuCalibration.load(args.imu_cal) if args.imu_cal and os.path.exists(args.imu_cal) else ImuCalibration()
            if args.imu_mount:
                cal.mount = args.imu_mount
            if args.declination is not None:
                cal.declination_deg = args.declination
            self.imu = ImuHeadSource(ImuLink.open(args.imu_port, cal, baudrate=args.imu_baud))

    def drop_ping(self, pose=None, ping_type: Optional[PingType] = None) -> None:
        ping_type = self.selected if ping_type is None else ping_type
        hit = raycast_from_pose(pose or self.operator.pose, max_range=self.args.max_range,
                                fallback_range=self.args.fallback_range)
        x, y, z = (float(c) for c in hit.point)
        ping = self.net.send_ping(x, y, z, ping_type, self.ping_ttl)
        self.last_ping_msg = (
            f"pinged {ping_type.name} #{ping.ping_id if ping else '?'} at "
            f"({x:.1f}, {y:.1f}, {z:.1f}) {hit.kind.value} {hit.distance:.1f} m"
        )
        log.info(self.last_ping_msg)

    def poll_imu(self) -> None:
        """Head attitude from the tracker; rail gestures -> the same ping path as SPACE."""
        from lynx.hw import RailAction

        pose, commands = self.imu.poll()
        op = self.operator
        if pose is not None:
            op.heading, op.pitch, op.roll = pose.heading, pose.pitch, pose.roll
        for cmd in commands:
            if cmd.action is RailAction.CANCEL_LAST:
                self.cancel_last()
            else:
                aim = cmd.aim or pose
                self.drop_ping(aim.to_pose(op.pose.position) if aim else None, cmd.ping_type(self.selected))

    def tare_imu(self) -> None:
        from lynx.hw import TareError

        try:
            off = self.imu.link.tare(self.args.datum_bearing)
            self.last_ping_msg = f"IMU tared to {self.args.datum_bearing:.1f} deg (offset {off:+.1f})"
            if self.args.imu_cal:
                self.imu.link.converter.cal.save(self.args.imu_cal)
        except TareError as exc:
            self.last_ping_msg = f"tare rejected: {exc}"
        log.info(self.last_ping_msg)

    def cancel_last(self, all_pings: bool = False) -> None:
        mine = self.client.own_pings()
        targets = mine if all_pings else mine[-1:]
        for ps in targets:
            self.net.cancel_ping(ps.ping.ping_id)

    def run(self) -> None:
        pygame.display.init()
        pygame.font.init()
        screen = pygame.display.set_mode((self.args.width, self.args.height))
        pygame.display.set_caption(f"TeamLynx sim - {self.args.callsign} [{self.args.node}]")
        renderer = Renderer(self.args.width, self.args.height, self.args.hfov, self.args.vfov)
        clock = pygame.time.Clock()
        self.net.start()
        if self.imu is not None:
            self.imu.link.start()
        tx_period = 1.0 / self.args.rate
        next_tx = 0.0
        running = True
        frames = 0
        fps_t0 = time.monotonic()
        fps = 0.0
        try:
            while running:
                dt = clock.tick(self.args.fps) / 1000.0
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        running = False
                    elif ev.type == pygame.KEYDOWN:
                        if ev.key == pygame.K_ESCAPE:
                            running = False
                        elif ev.key == pygame.K_SPACE:
                            self.drop_ping()
                        elif ev.key in PING_KEYS:
                            self.selected = PING_KEYS[ev.key]
                        elif ev.key == pygame.K_BACKSPACE:
                            self.cancel_last()
                        elif ev.key == pygame.K_DELETE:
                            self.cancel_last(all_pings=True)
                        elif ev.key == pygame.K_h:
                            self.show_help = not self.show_help
                        elif ev.key == pygame.K_r:
                            self.operator.level()
                        elif ev.key == pygame.K_t and self.imu is not None:
                            self.tare_imu()
                        elif ev.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                            self.minimap_range = max(10.0, self.minimap_range / 1.5)
                        elif ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                            self.minimap_range = min(400.0, self.minimap_range * 1.5)
                self.operator.step(dt, read_controls(pygame.key.get_pressed()))
                if self.imu is not None:
                    self.poll_imu()

                now = time.monotonic()
                if now >= next_tx:
                    op = self.operator
                    flags = self.imu.telemetry_flags() if self.imu is not None else 0
                    self.net.send_telemetry(op.x, op.y, op.z, op.heading, op.pitch, op.roll, flags)
                    next_tx = now + tx_period

                nodes, pings = self.net.snapshot()
                frames += 1
                if now - fps_t0 >= 1.0:
                    fps = frames / (now - fps_t0)
                    frames, fps_t0 = 0, now
                vm = ViewModel(
                    node_id=self.args.node,
                    callsign=self.args.callsign,
                    team=self.team,
                    pose=self.operator.pose,
                    nodes=nodes,
                    pings=pings,
                    now=now,
                    connected=self.net.connected,
                    selected_ping=self.selected,
                    status_lines=[f"{fps:4.0f} fps  tx {self.client.tx_count} rx {self.client.rx_count}  {self.args.url}",
                                  self.last_ping_msg]
                    + ([self.imu.link.health().summary()] if self.imu is not None else []),
                    show_help=self.show_help,
                    minimap_range_m=self.minimap_range,
                )
                renderer.render(screen, vm)
                pygame.display.flip()
        finally:
            if self.imu is not None:
                self.imu.link.stop()
            self.net.stop()
            pygame.quit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TeamLynx desktop testbench operator window")
    p.add_argument("--url", default="ws://127.0.0.1:8765", help="relay WebSocket URL")
    p.add_argument("--node", type=int, default=1, help="node id 1..65535")
    p.add_argument("--callsign", default="ALPHA", help="up to 8 ASCII characters")
    p.add_argument("--team", default="blue", choices=[t.name.lower() for t in Team])
    p.add_argument("--x", type=float, default=0.0, help="start East (m)")
    p.add_argument("--y", type=float, default=0.0, help="start North (m)")
    p.add_argument("--heading", type=float, default=0.0, help="start compass heading (deg)")
    p.add_argument("--pitch", type=float, default=-10.0, help="start pitch (deg, + up)")
    p.add_argument("--eye-height", type=float, default=1.7)
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=600)
    p.add_argument("--hfov", type=float, default=90.0)
    p.add_argument("--vfov", type=float, default=None, help="default: square pixels")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--rate", type=float, default=20.0, help="telemetry Hz")
    p.add_argument("--ping-ttl", type=float, default=120.0, help="seconds")
    p.add_argument("--max-range", type=float, default=150.0)
    p.add_argument("--fallback-range", type=float, default=50.0)
    p.add_argument("--encoding", default="binary", choices=["binary", "json"])
    p.add_argument("--window-pos", default=None, help="X,Y screen position of the window")
    p.add_argument("--imu-port", default=None,
                   help="head tracker serial port (/dev/ttyUSB0, COM5) or mock:// ; head pose + rail switch")
    p.add_argument("--imu-baud", type=int, default=460800)
    p.add_argument("--imu-cal", default=None, help="IMU calibration JSON (python -m lynx.hw tare --cal ...)")
    p.add_argument("--imu-mount", default=None, help="sensor mount spec/preset, e.g. top-flat, FLU, left-side")
    p.add_argument("--declination", type=float, default=None, help="magnetic declination, deg east +")
    p.add_argument("--datum-bearing", type=float, default=0.0, help="bearing faced when pressing T (tare)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    if len(args.callsign) > 8 or not args.callsign.isascii():
        raise SystemExit("callsign must be at most 8 ASCII characters")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.window_pos:
        os.environ["SDL_VIDEO_WINDOW_POS"] = args.window_pos
    SimApp(args).run()


if __name__ == "__main__":
    main()
