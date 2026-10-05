"""Composite minimalist tactical HUD on an OpenCV BGR frame.

Layout (1280x720 reference):
    top-left     telemetry readout
    top-centre   360-degree compass tape with teammate / ping bearing markers
    top-right    PiP aux feed (UAV / chokepoint camera / edge view)
    centre       reticle
    in-scene     IFF corner boxes + callsigns, BFT diamonds for unseen
                 teammates, ping chevrons; off-screen pings as edge arrows
    bottom-left  heading-up mini radar
    bottom-centre IFF summary / alerts
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import cv2
import numpy as np

from lynx.hud import widgets as W
from lynx.hud.style import BGR, HudStyle, team_bgr
from lynx.hud.types import HudState, RenderedPing, WorldPing
from lynx.spatial import Camera
from lynx.vision.geometry import edge_arrow_position, pixel_to_angles, world_bearing_deg
from lynx.vision.types import CameraModel, IffStatus


class HudRenderer:
    def __init__(self, style: Optional[HudStyle] = None):
        self.style = style or HudStyle()
        self.last_pings: List[RenderedPing] = []

    # ------------------------------------------------------------- main entry
    def render(self, frame: np.ndarray, state: HudState, copy: bool = True) -> np.ndarray:
        if frame.ndim == 2:
            img = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        else:
            img = frame.copy() if copy else frame
        h, w = img.shape[:2]
        cam = state.camera
        if (cam.width, cam.height) != (w, h):
            cam = cam.resized(w, h)
        st = self.style
        s = W.scale_of(img)

        self.draw_iff(img, state, cam)
        self.draw_pings(img, state, cam)
        W.reticle(img, st.primary)
        W.compass_tape(img, state.pose.yaw, st, self._tape_markers(state))
        W.telemetry_block(img, self.telemetry_lines(state), st)
        if state.aux_frame is not None and state.aux_frame.size:
            W.pip(img, state.aux_frame, st, state.aux_label, top=int(12 * s))
        W.radar(img, state.pose.yaw, cam.hfov_deg, st, self._radar_blips(state, cam))
        self.draw_summary(img, state)
        return img

    # ------------------------------------------------------------- layers
    def telemetry_lines(self, state: HudState) -> List[str]:
        p = state.pose
        lines = [
            f"{p.callsign}  N{p.node_id:02d}  {state.mode}",
            f"HDG {p.yaw % 360:05.1f}  PIT {p.pitch:+05.1f}  ROL {p.roll:+05.1f}",
            f"POS E{p.x:+07.1f} N{p.y:+07.1f} U{p.z:+05.1f}",
        ]
        for k, v in state.telemetry.items():
            lines.append(f"{k} {v}")
        return lines

    def draw_iff(self, img: np.ndarray, state: HudState, cam: CameraModel) -> None:
        st = self.style
        s = W.scale_of(img)
        t = max(1, int(round(2 * s)))
        shown_nodes = set()
        for tr in state.tracks:
            if tr.status == IffStatus.FRIENDLY:
                color: BGR = team_bgr(tr.team_color)
                label = tr.callsign or f"N{tr.node_id}"
                shown_nodes.add(tr.node_id)
            elif tr.status == IffStatus.TANGO:
                color, label = st.tango, "TANGO"
            else:
                color = st.unverified
                label = "VEH?" if tr.category == "vehicle" else "UNK"
            if tr.misses > 0:
                W.dashed_rect(img, tr.bbox, color)
            else:
                W.corner_box(img, tr.bbox, color, t)
            rng = f" {tr.range_m:.0f}m" if tr.range_m is not None else ""
            x1, y1, x2, y2 = tr.bbox
            if tr.category == "vehicle":
                W.text(img, f"{label}{rng}", ((x1 + x2) / 2, y2 + 5 * s), color, 0.42, 1, "ct")
            else:
                W.text(img, f"{label}{rng}", ((x1 + x2) / 2, y1 - 5 * s), color, 0.45, 1, "cb")
            if tr.status == IffStatus.TANGO:
                cx = (x1 + x2) / 2
                d = 5 * s
                pts = np.array([[cx, y1 - 26 * s - d], [cx + d, y1 - 26 * s], [cx, y1 - 26 * s + d], [cx - d, y1 - 26 * s]])
                cv2.fillConvexPoly(img, pts.astype(np.int32), color, cv2.LINE_AA)
        if not st.show_bft_markers:
            return
        for e in state.expected:
            if e.node_id in shown_nodes or not e.in_view or e.pixel is None:
                continue
            color = team_bgr(e.team_color)
            if e.stale:
                color = tuple(int(c * 0.5) for c in color)
            x, y = e.pixel
            d = 7 * s
            pts = np.array([[x, y - d], [x + d, y], [x, y + d], [x - d, y]])
            cv2.polylines(img, [pts.astype(np.int32)], True, color, max(1, int(s)), cv2.LINE_AA)
            tag = f"{e.callsign} {e.range_m:.0f}m" + (" STALE" if e.stale else "")
            W.text(img, tag, (x, y - d - 3 * s), color, 0.38, 1, "cb")

    def layout_pings(self, state: HudState, cam: CameraModel, margin: float) -> List[RenderedPing]:
        """Screen placement of every ping: world pings via :meth:`lynx.spatial.Camera.project`."""
        out: List[RenderedPing] = []
        if state.pings:
            view = Camera(cam.intrinsics, state.pose.spatial_pose)
            for p in state.pings:
                pr = view.project((p.x, p.y, p.z))
                if not pr.on_screen:
                    pr = view.project((p.x, p.y, p.z), margin)
                angle = math.radians(pr.edge_angle_deg - 90.0)  # clockwise-from-up -> image atan2
                out.append(RenderedPing(p.ping_id, p.label, pr.u, pr.v, pr.on_screen, angle, pr.distance, p.color))
        for sp in state.screen_pings:
            on_screen = (not sp.behind) and 0 <= sp.u < cam.width and 0 <= sp.v < cam.height
            if on_screen:
                out.append(RenderedPing(None, sp.label, sp.u, sp.v, True, 0.0, sp.range_m, sp.color))
                continue
            dx, dy = sp.u - cam.cx, sp.v - cam.cy
            cam_xyz = (dx / cam.fx, dy / cam.fy, -1.0 if sp.behind else 1.0)
            (u, v), angle = edge_arrow_position(cam, cam_xyz, margin)
            out.append(RenderedPing(None, sp.label, u, v, False, angle, sp.range_m, sp.color))
        return out

    def draw_pings(self, img: np.ndarray, state: HudState, cam: CameraModel) -> None:
        s = W.scale_of(img)
        self.last_pings = self.layout_pings(state, cam, 46 * s)
        for rp in self.last_pings:
            color = rp.color or self.style.ping
            if rp.on_screen:
                W.chevron(img, (rp.u, rp.v), color, rp.label, rp.range_m)
            else:
                W.edge_arrow(img, (rp.u, rp.v), rp.angle_rad, color, rp.label, rp.range_m)

    def draw_summary(self, img: np.ndarray, state: HudState) -> None:
        h, w = img.shape[:2]
        s = W.scale_of(img)
        counts = {k: 0 for k in IffStatus}
        for tr in state.tracks:
            counts[tr.status] += 1
        parts = [
            (f"FRND {counts[IffStatus.FRIENDLY]}", self.style.primary),
            (f"UNK {counts[IffStatus.UNVERIFIED]}", self.style.unverified),
            (f"TGO {counts[IffStatus.TANGO]}", self.style.tango),
        ]
        y = h - 18 * s
        total_w = 0
        sizes = []
        for label, _ in parts:
            (tw, _), _ = cv2.getTextSize(label, W.FONT, 0.5 * s, max(1, int(s)))
            sizes.append(tw)
            total_w += tw + 18 * s
        x = w / 2 - total_w / 2
        W.panel(img, int(x - 8 * s), int(y - 20 * s), int(x + total_w), int(y + 6 * s), self.style.panel_alpha)
        for (label, color), tw in zip(parts, sizes):
            W.text(img, label, (x, y), color, 0.5, 1, "lb")
            x += tw + 18 * s
        for i, alert in enumerate(state.alerts[:3]):
            W.text(img, alert, (w / 2, y - 30 * s - i * 20 * s), self.style.tango, 0.55, 1, "cb")

    # ------------------------------------------------------------- helpers
    def _tape_markers(self, state: HudState):
        markers = []
        for tm in state.teammates:
            if tm.node_id == state.pose.node_id:
                continue
            markers.append((world_bearing_deg(state.pose, tm.position), team_bgr(tm.team_color), "friendly"))
        for p in state.pings:
            markers.append((world_bearing_deg(state.pose, (p.x, p.y, p.z)), p.color or self.style.ping, "ping"))
        return markers

    def _radar_blips(self, state: HudState, cam: CameraModel):
        pose = state.pose
        blips = []
        for tm in state.teammates:
            if tm.node_id == pose.node_id:
                continue
            rng = math.hypot(tm.x - pose.x, tm.y - pose.y)
            blips.append((world_bearing_deg(pose, tm.position), rng, team_bgr(tm.team_color), tm.callsign, "friendly"))
        for p in state.pings:
            rng = math.hypot(p.x - pose.x, p.y - pose.y)
            blips.append((world_bearing_deg(pose, (p.x, p.y, p.z)), rng, p.color or self.style.ping, p.label, "ping"))
        for tr in state.tracks:
            if tr.status == IffStatus.FRIENDLY or tr.range_m is None:
                continue
            u = (tr.bbox[0] + tr.bbox[2]) / 2.0
            v = (tr.bbox[1] + tr.bbox[3]) / 2.0
            az, _ = pixel_to_angles(cam, u, v)
            color = self.style.tango if tr.status == IffStatus.TANGO else self.style.unverified
            blips.append(((pose.yaw + az) % 360.0, tr.range_m, color, "", "contact"))
        return blips


def world_pings_from(objs) -> List[WorldPing]:
    """Adapt any objects with ping_id/x/y/z/label/owner attributes."""
    return [WorldPing(o.ping_id, o.x, o.y, o.z, o.label, getattr(o, "owner", "")) for o in objs]
