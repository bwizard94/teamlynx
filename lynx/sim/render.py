"""Synthetic first-person view + top-down minimap for the testbench (pygame).

All 3D geometry goes through :class:`lynx.spatial.Camera` - the same projection code the real HUD
will use - so what you see in the sim is a direct test of the spatial math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pygame

from lynx.net.schema import TEAM_RGB, PingType, Team
from lynx.net.state import NodeState, PingKey, PingState
from lynx.spatial import Camera, Intrinsics, Pose, Projection, Visibility

Color = Tuple[int, int, int]

SKY = (14, 18, 30)
GROUND = (22, 30, 22)
GRID_MINOR = (45, 62, 45)
GRID_MAJOR = (70, 98, 70)
AXIS_EAST = (170, 70, 70)
AXIS_NORTH = (70, 170, 70)
HUD = (150, 255, 170)
HUD_DIM = (90, 150, 105)
WHITE = (235, 235, 235)

PING_RGB: Dict[PingType, Color] = {
    PingType.MARK: (0, 230, 255),
    PingType.CONTACT: (255, 70, 70),
    PingType.MOVE: (80, 255, 120),
    PingType.DANGER: (255, 170, 0),
    PingType.RALLY: (190, 130, 255),
}

GRID_STEP = 5.0
GRID_MAJOR_EVERY = 5  # every 25 m
GRID_HALF_EXTENT = 120.0
EDGE_MARGIN = 28.0
OPERATOR_WIDTH_M = 0.6
PING_STEM_M = 2.0


@dataclass
class ViewModel:
    node_id: int
    callsign: str
    team: Team
    pose: Pose
    nodes: Dict[int, NodeState] = field(default_factory=dict)
    pings: Dict[PingKey, PingState] = field(default_factory=dict)
    now: float = 0.0
    connected: bool = True
    selected_ping: PingType = PingType.MARK
    status_lines: List[str] = field(default_factory=list)
    show_help: bool = False
    minimap_range_m: float = 50.0


@dataclass
class RenderReport:
    """Where things ended up on screen (used by the self-check and tests)."""

    pings: Dict[PingKey, Projection] = field(default_factory=dict)
    nodes: Dict[int, Projection] = field(default_factory=dict)


HELP_TEXT = [
    "W/S  forward/back      A/D  strafe       SHIFT sprint",
    "Q/E or LEFT/RIGHT  turn   UP/DOWN  pitch   Z/C  roll",
    "PGUP/PGDN  eye height   R  level pitch+roll",
    "SPACE  drop ping (raycast from reticle)   1-5  ping type",
    "BACKSPACE  cancel my last ping   DEL  cancel all mine",
    "+/-  minimap zoom   H  toggle help   ESC  quit",
]


def _callsign_for(nodes: Dict[int, NodeState], node_id: int, self_id: int, self_cs: str) -> str:
    if node_id == self_id:
        return self_cs
    ns = nodes.get(node_id)
    return ns.telemetry.callsign if ns else f"#{node_id}"


class Renderer:
    def __init__(self, width: int, height: int, hfov_deg: float = 90.0, vfov_deg: Optional[float] = None) -> None:
        if not pygame.font.get_init():
            pygame.font.init()
        self.width = width
        self.height = height
        self.intrinsics = Intrinsics.from_fov(width, height, hfov_deg, vfov_deg)
        self.camera = Camera(self.intrinsics)
        self.font = pygame.font.Font(None, 20)
        self.font_small = pygame.font.Font(None, 16)
        self.font_big = pygame.font.Font(None, 26)
        self.screen_rect = pygame.Rect(0, 0, width, height)

    # -- helpers ----------------------------------------------------------------
    def _text(self, surf: pygame.Surface, text: str, pos: Tuple[float, float], color: Color,
              font: Optional[pygame.font.Font] = None, anchor: str = "topleft", shadow: bool = True) -> pygame.Rect:
        font = font or self.font
        img = font.render(text, True, color)
        rect = img.get_rect(**{anchor: (int(pos[0]), int(pos[1]))})
        if shadow:
            surf.blit(font.render(text, True, (0, 0, 0)), rect.move(1, 1))
        surf.blit(img, rect)
        return rect

    def _line_w(self, surf: pygame.Surface, color: Color, a_w: Sequence[float], b_w: Sequence[float], width: int = 1) -> None:
        seg = self.camera.project_segment(a_w, b_w)
        if seg is None:
            return
        clipped = self.screen_rect.clipline(seg[0], seg[1])
        if clipped:
            pygame.draw.line(surf, color, clipped[0], clipped[1], width)

    # -- main entry -------------------------------------------------------------
    def render(self, surf: pygame.Surface, vm: ViewModel) -> RenderReport:
        self.camera.pose = vm.pose
        report = RenderReport()
        self._draw_background(surf)
        self._draw_grid(surf, vm.pose)

        items: List[Tuple[float, str, object]] = []
        for nid, ns in vm.nodes.items():
            if nid == vm.node_id:
                continue
            t = ns.telemetry
            proj = self.camera.project((t.x, t.y, t.z), margin=EDGE_MARGIN)
            report.nodes[nid] = proj
            items.append((proj.distance, "node", (nid, ns, proj)))
        for key, ps in vm.pings.items():
            p = ps.ping
            proj = self.camera.project((p.x, p.y, p.z), margin=EDGE_MARGIN)
            report.pings[key] = proj
            items.append((proj.distance, "ping", (key, ps, proj)))

        items.sort(key=lambda it: -it[0])  # painter's algorithm: far to near
        edge_items = []
        for _, kind, payload in items:
            proj = payload[2]  # type: ignore[index]
            if not proj.on_screen:
                edge_items.append((kind, payload))
                continue
            if kind == "node":
                self._draw_operator(surf, *payload)  # type: ignore[arg-type]
            else:
                self._draw_ping(surf, vm, *payload)  # type: ignore[arg-type]
        for kind, payload in edge_items:
            if kind == "node":
                nid, ns, proj = payload  # type: ignore[misc]
                col = TEAM_RGB[ns.telemetry.team]
                self._draw_edge_indicator(surf, proj, col, ns.telemetry.callsign, round_marker=True)
            else:
                key, ps, proj = payload  # type: ignore[misc]
                col = PING_RGB[ps.ping.ping_type]
                self._draw_edge_indicator(surf, proj, col, f"{ps.ping.ping_type.name} {proj.distance:.0f}m")

        self._draw_reticle(surf)
        self._draw_compass(surf, vm, report)
        self._draw_minimap(surf, vm)
        self._draw_status(surf, vm)
        if vm.show_help:
            self._draw_help(surf)
        return report

    # -- world ------------------------------------------------------------------
    def _draw_background(self, surf: pygame.Surface) -> None:
        surf.fill(SKY)
        poly = self.camera.ground_polygon(0.0)
        if len(poly) >= 3:
            pygame.draw.polygon(surf, GROUND, poly)

    def _draw_grid(self, surf: pygame.Surface, pose: Pose) -> None:
        cx = math.floor(pose.position[0] / GRID_STEP) * GRID_STEP
        cy = math.floor(pose.position[1] / GRID_STEP) * GRID_STEP
        n = int(GRID_HALF_EXTENT / GRID_STEP)
        x0, x1 = cx - n * GRID_STEP, cx + n * GRID_STEP
        y0, y1 = cy - n * GRID_STEP, cy + n * GRID_STEP
        for i in range(-n, n + 1):
            gx = cx + i * GRID_STEP
            gy = cy + i * GRID_STEP
            major_x = round(gx / GRID_STEP) % GRID_MAJOR_EVERY == 0
            major_y = round(gy / GRID_STEP) % GRID_MAJOR_EVERY == 0
            col_x = AXIS_NORTH if abs(gx) < 1e-6 else (GRID_MAJOR if major_x else GRID_MINOR)
            col_y = AXIS_EAST if abs(gy) < 1e-6 else (GRID_MAJOR if major_y else GRID_MINOR)
            self._line_w(surf, col_x, (gx, y0, 0.0), (gx, y1, 0.0), 2 if abs(gx) < 1e-6 else 1)
            self._line_w(surf, col_y, (x0, gy, 0.0), (x1, gy, 0.0), 2 if abs(gy) < 1e-6 else 1)
        # Datum marker: 2 m post at the staging origin.
        self._line_w(surf, WHITE, (0.0, 0.0, 0.0), (0.0, 0.0, 2.0), 2)

    def _ground_ring(self, surf: pygame.Surface, center: Sequence[float], radius: float, color: Color) -> None:
        pts = [
            (center[0] + radius * math.cos(a), center[1] + radius * math.sin(a), center[2])
            for a in np.linspace(0.0, 2.0 * math.pi, 25)
        ]
        for a, b in zip(pts[:-1], pts[1:]):
            self._line_w(surf, color, a, b, 2)

    def _draw_operator(self, surf: pygame.Surface, nid: int, ns: NodeState, proj: Projection) -> None:
        t = ns.telemetry
        col = TEAM_RGB[t.team]
        head = (t.x, t.y, t.z)
        feet = (t.x, t.y, 0.0)
        seg = self.camera.project_segment(feet, head)
        if seg is None:
            return
        (fu, fv), (hu, hv) = seg
        w = max(4.0, OPERATOR_WIDTH_M * self.intrinsics.fx / max(proj.depth, 0.1))
        h = abs(fv - hv)
        box = pygame.Rect(int(min(fu, hu) - w / 2), int(min(fv, hv)), int(w), int(max(h, 4)))
        if box.colliderect(self.screen_rect):
            pygame.draw.rect(surf, col, box, 2)
            r = max(3, int(w * 0.35))
            pygame.draw.circle(surf, col, (int(hu), int(hv) - r), r, 2)
        label_y = min(fv, hv) - 2 * max(3, int(w * 0.35)) - 4
        self._text(surf, f"{t.callsign}  {proj.distance:.0f}m", (hu, label_y), col, anchor="midbottom")

    def _draw_ping(self, surf: pygame.Surface, vm: ViewModel, key: PingKey, ps: PingState, proj: Projection) -> None:
        p = ps.ping
        col = PING_RGB[p.ping_type]
        base = (p.x, p.y, p.z)
        self._ground_ring(surf, base, 1.0, col)
        self._line_w(surf, col, base, (p.x, p.y, p.z + PING_STEM_M), 2)
        top = self.camera.project((p.x, p.y, p.z + PING_STEM_M))
        u, v = (top.u, top.v) if top.on_screen else (proj.u, proj.v)
        # Two stacked chevrons pointing down at the anchor, constant pixel size.
        for k in range(2):
            tip_y = v - 4 - k * 9
            pygame.draw.lines(surf, col, False, [(u - 11, tip_y - 10), (u, tip_y), (u + 11, tip_y - 10)], 3)
        owner = _callsign_for(vm.nodes, p.owner, vm.node_id, vm.callsign)
        remain = ps.remaining_s(vm.now)
        self._text(surf, f"{p.ping_type.name} {proj.distance:.0f}m", (u, v - 32), col, anchor="midbottom")
        self._text(surf, f"{owner}  {remain:.0f}s", (u, v - 48), col, font=self.font_small, anchor="midbottom")

    def _draw_edge_indicator(self, surf: pygame.Surface, proj: Projection, col: Color, label: str,
                             round_marker: bool = False) -> None:
        a = math.radians(proj.edge_angle_deg)
        d = np.array([math.sin(a), -math.cos(a)])  # screen direction (v down)
        nrm = np.array([-d[1], d[0]])
        c = np.array([proj.u, proj.v])
        tip = c + d * 12
        tri = [tuple(tip), tuple(c - d * 6 + nrm * 10), tuple(c - d * 6 - nrm * 10)]
        if round_marker:
            pygame.draw.circle(surf, col, (int(c[0]), int(c[1])), 7, 2)
            pygame.draw.line(surf, col, tuple(c + d * 7), tuple(tip + d * 4), 3)
        else:
            pygame.draw.polygon(surf, col, tri)
        tag = label + (" (behind)" if proj.visibility is Visibility.BEHIND else "")
        lp = c - d * 22
        img_w = self.font_small.size(tag)[0]
        lx = min(max(lp[0], img_w / 2 + 2), self.width - img_w / 2 - 2)
        ly = min(max(lp[1], 10), self.height - 10)
        self._text(surf, tag, (lx, ly), col, font=self.font_small, anchor="center")

    # -- HUD --------------------------------------------------------------------
    def _draw_reticle(self, surf: pygame.Surface) -> None:
        cx, cy = int(self.intrinsics.cx), int(self.intrinsics.cy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            pygame.draw.line(surf, HUD, (cx + dx * 6, cy + dy * 6), (cx + dx * 16, cy + dy * 16), 2)
        pygame.draw.circle(surf, HUD, (cx, cy), 2)

    def _draw_compass(self, surf: pygame.Surface, vm: ViewModel, report: RenderReport) -> None:
        heading = vm.pose.euler[0]
        tape_w, span = max(200, min(440, self.width - 2 * (self._minimap_size() + 20))), 60.0
        x0 = (self.width - tape_w) / 2
        y = 8
        ppd = tape_w / (2 * span)
        pygame.draw.rect(surf, (0, 0, 0), (x0, y, tape_w, 30))
        pygame.draw.rect(surf, HUD_DIM, (x0, y, tape_w, 30), 1)
        names = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
        start = math.ceil((heading - span) / 5.0) * 5
        for deg in range(int(start), int(heading + span) + 1, 5):
            rel = deg - heading
            px = x0 + tape_w / 2 + rel * ppd
            d = deg % 360
            major = d % 15 == 0
            pygame.draw.line(surf, HUD, (px, y + 30), (px, y + 30 - (10 if major else 5)), 1)
            if d in names:
                self._text(surf, names[d], (px, y + 2), HUD, font=self.font_small, anchor="midtop", shadow=False)
            elif major:
                self._text(surf, f"{d}", (px, y + 2), HUD_DIM, font=self.font_small, anchor="midtop", shadow=False)

        def marker(rel: float, col: Color, up: bool) -> None:
            rel = max(-span, min(span, rel))
            px = x0 + tape_w / 2 + rel * ppd
            if up:
                pygame.draw.polygon(surf, col, [(px, y + 31), (px - 5, y + 39), (px + 5, y + 39)])
            else:
                pygame.draw.circle(surf, col, (int(px), y + 42), 4)

        for key, proj in report.pings.items():
            marker(proj.bearing_deg, PING_RGB[vm.pings[key].ping.ping_type], True)
        for nid, proj in report.nodes.items():
            marker(proj.bearing_deg, TEAM_RGB[vm.nodes[nid].telemetry.team], False)
        pygame.draw.line(surf, WHITE, (x0 + tape_w / 2, y - 2), (x0 + tape_w / 2, y + 30), 2)
        self._text(surf, f"{heading:05.1f}", (x0 + tape_w / 2, y + 48), HUD, font=self.font, anchor="midtop")

    def _minimap_size(self) -> int:
        return max(120, min(210, self.height // 2 - 20))

    def _draw_minimap(self, surf: pygame.Surface, vm: ViewModel) -> None:
        size = self._minimap_size()
        rect = pygame.Rect(self.width - size - 10, 10, size, size)
        mm = pygame.Surface((size, size), pygame.SRCALPHA)
        mm.fill((0, 0, 0, 190))
        c = np.array([size / 2, size / 2])
        s = (size / 2 - 6) / vm.minimap_range_m
        ox, oy = vm.pose.position[0], vm.pose.position[1]

        def to_mm(x: float, y: float) -> Tuple[float, float]:
            return (c[0] + (x - ox) * s, c[1] - (y - oy) * s)

        step = 10.0 if vm.minimap_range_m <= 80 else 25.0
        k0 = math.floor((ox - vm.minimap_range_m * 1.5) / step)
        k1 = math.ceil((ox + vm.minimap_range_m * 1.5) / step)
        for k in range(k0, k1 + 1):
            gx = k * step
            pygame.draw.line(mm, AXIS_NORTH if k == 0 else GRID_MINOR, to_mm(gx, oy - 1e4), to_mm(gx, oy + 1e4), 1)
        k0 = math.floor((oy - vm.minimap_range_m * 1.5) / step)
        k1 = math.ceil((oy + vm.minimap_range_m * 1.5) / step)
        for k in range(k0, k1 + 1):
            gy = k * step
            pygame.draw.line(mm, AXIS_EAST if k == 0 else GRID_MINOR, to_mm(ox - 1e4, gy), to_mm(ox + 1e4, gy), 1)

        heading = vm.pose.euler[0]
        half = self.intrinsics.hfov_deg / 2
        wedge_r = size  # extends past the border, clipped by the surface
        pts = [tuple(c)]
        for a in np.linspace(heading - half, heading + half, 12):
            ar = math.radians(a)
            pts.append((c[0] + wedge_r * math.sin(ar), c[1] - wedge_r * math.cos(ar)))
        pygame.draw.polygon(mm, (28, 60, 36, 200), pts)

        for key, ps in vm.pings.items():
            p = ps.ping
            px, py = to_mm(p.x, p.y)
            col = PING_RGB[p.ping_type]
            pygame.draw.polygon(mm, col, [(px, py - 6), (px + 6, py), (px, py + 6), (px - 6, py)])
        for nid, ns in vm.nodes.items():
            if nid == vm.node_id:
                continue
            t = ns.telemetry
            px, py = to_mm(t.x, t.y)
            col = TEAM_RGB[t.team]
            hr = math.radians(t.heading)
            pygame.draw.circle(mm, col, (int(px), int(py)), 5)
            pygame.draw.line(mm, col, (px, py), (px + 11 * math.sin(hr), py - 11 * math.cos(hr)), 2)
            self._text(mm, t.callsign, (px + 7, py - 7), col, font=self.font_small, anchor="bottomleft")
        hr = math.radians(heading)
        f = np.array([math.sin(hr), -math.cos(hr)])
        r = np.array([-f[1], f[0]])
        own = TEAM_RGB[vm.team]
        pygame.draw.polygon(mm, own, [tuple(c + f * 13), tuple(c - f * 5 + r * 6), tuple(c - f * 2), tuple(c - f * 5 - r * 6)])
        self._text(mm, "N", (size / 2, 2), WHITE, font=self.font_small, anchor="midtop")
        self._text(mm, f"{vm.minimap_range_m:.0f} m", (size - 4, size - 4), HUD_DIM, font=self.font_small, anchor="bottomright")
        surf.blit(mm, rect)
        pygame.draw.rect(surf, HUD_DIM, rect, 1)

    def _draw_status(self, surf: pygame.Surface, vm: ViewModel) -> None:
        h, p, r = vm.pose.euler
        x, y, z = vm.pose.position
        link = "LINK OK" if vm.connected else "NO LINK"
        lines = [
            (f"{vm.callsign} [{vm.node_id}] {vm.team.name}", TEAM_RGB[vm.team]),
            (f"ENU  E {x:7.1f}  N {y:7.1f}  U {z:5.2f} m", HUD),
            (f"HDG {h:05.1f}  PIT {p:+05.1f}  ROL {r:+05.1f}", HUD),
            (f"{link}   squad {max(0, len([n for n in vm.nodes if n != vm.node_id]))}   pings {len(vm.pings)}",
             HUD if vm.connected else (255, 80, 80)),
            (f"PING TYPE [{int(vm.selected_ping) + 1}] {vm.selected_ping.name}", PING_RGB[vm.selected_ping]),
        ] + [(s, HUD_DIM) for s in vm.status_lines]
        y0 = self.height - 10 - 18 * len(lines)
        for i, (text, col) in enumerate(lines):
            self._text(surf, text, (10, y0 + 18 * i), col)

    def _draw_help(self, surf: pygame.Surface) -> None:
        w = 520
        h = 24 + 20 * len(HELP_TEXT)
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 200))
        surf.blit(panel, ((self.width - w) // 2, self.height // 2 + 40))
        for i, line in enumerate(HELP_TEXT):
            self._text(surf, line, ((self.width - w) // 2 + 12, self.height // 2 + 52 + 20 * i), HUD, font=self.font_small)
