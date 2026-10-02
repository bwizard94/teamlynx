"""Individual HUD widgets. Each draws in place on a BGR uint8 frame.

All sizes scale with ``s = frame_height / 720`` so the layout holds from
640x360 micro-displays to 1080p bench monitors.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

import cv2
import numpy as np

from lynx.hud.style import BGR, FONT, SHADOW, HudStyle

CARDINALS = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}


def scale_of(img: np.ndarray) -> float:
    return img.shape[0] / 720.0


def ipt(p: Sequence[float]) -> Tuple[int, int]:
    return int(round(p[0])), int(round(p[1]))


def text(
    img: np.ndarray,
    s: str,
    org: Sequence[float],
    color: BGR,
    scale: float = 0.5,
    thickness: int = 1,
    anchor: str = "lt",
) -> Tuple[int, int]:
    """Outlined text. ``anchor``: l/c/r horizontally + t/m/b vertically. Returns (w, h)."""
    sc = scale * scale_of(img)
    th = max(1, int(round(thickness * scale_of(img))))
    (w, h), base = cv2.getTextSize(s, FONT, sc, th)
    x, y = org
    if anchor[0] == "c":
        x -= w / 2
    elif anchor[0] == "r":
        x -= w
    if anchor[1] == "t":
        y += h
    elif anchor[1] == "m":
        y += h / 2
    p = ipt((x, y))
    cv2.putText(img, s, p, FONT, sc, SHADOW, th + 2, cv2.LINE_AA)
    cv2.putText(img, s, p, FONT, sc, color, th, cv2.LINE_AA)
    return w, h


def panel(img: np.ndarray, x1: int, y1: int, x2: int, y2: int, alpha: float) -> None:
    """Darken a rectangle in place (translucent backing for readability)."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = img[y1:y2, x1:x2]
    cv2.convertScaleAbs(roi, dst=roi, alpha=1.0 - alpha)


def reticle(img: np.ndarray, color: BGR, center: Optional[Tuple[float, float]] = None) -> None:
    h, w = img.shape[:2]
    s = scale_of(img)
    cx, cy = center if center else (w / 2.0, h / 2.0)
    gap, arm = 8 * s, 22 * s
    t = max(1, int(round(1.5 * s)))
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        p1 = (cx + dx * gap, cy + dy * gap)
        p2 = (cx + dx * (gap + arm), cy + dy * (gap + arm))
        cv2.line(img, ipt(p1), ipt(p2), SHADOW, t + 2, cv2.LINE_AA)
        cv2.line(img, ipt(p1), ipt(p2), color, t, cv2.LINE_AA)
    cv2.circle(img, ipt((cx, cy)), max(1, int(2 * s)), color, -1, cv2.LINE_AA)
    # stadia ticks below the crosshair (holdover / range estimation aid)
    for k in (1, 2, 3):
        y = cy + gap + arm + k * 9 * s
        half = (6 - k) * s
        cv2.line(img, ipt((cx - half, y)), ipt((cx + half, y)), color, 1, cv2.LINE_AA)


def compass_tape(
    img: np.ndarray,
    heading_deg: float,
    style: HudStyle,
    markers: Iterable[Tuple[float, BGR, str]] = (),
    y: Optional[int] = None,
) -> None:
    """360-degree wrapping heading tape. ``markers``: (bearing_deg, color, kind)."""
    h, w = img.shape[:2]
    s = scale_of(img)
    tape_w = int(w * 0.46)
    tape_h = int(34 * s)
    x0 = (w - tape_w) // 2
    y0 = int(10 * s) if y is None else y
    panel(img, x0, y0, x0 + tape_w, y0 + tape_h, style.panel_alpha)
    span = style.compass_span_deg
    ppd = tape_w / span
    heading = heading_deg % 360.0
    start = math.floor((heading - span / 2) / 5.0) * 5
    end = math.ceil((heading + span / 2) / 5.0) * 5
    for d in range(int(start), int(end) + 1, 5):
        x = x0 + tape_w / 2 + (d - heading) * ppd
        if x < x0 or x > x0 + tape_w:
            continue
        dn = d % 360
        if dn % 15 == 0:
            cv2.line(img, ipt((x, y0 + tape_h)), ipt((x, y0 + tape_h - 10 * s)), style.primary, 1, cv2.LINE_AA)
            label = CARDINALS.get(dn, f"{dn:03d}")
            col = style.primary if dn in CARDINALS else style.dim
            text(img, label, (x, y0 + 4 * s), col, 0.42 if dn in CARDINALS else 0.36, 1, "ct")
        else:
            cv2.line(img, ipt((x, y0 + tape_h)), ipt((x, y0 + tape_h - 5 * s)), style.dim, 1, cv2.LINE_AA)
    for bearing, color, kind in markers:
        rel = (bearing - heading + 180.0) % 360.0 - 180.0
        clipped = abs(rel) > span / 2
        rel = max(-span / 2, min(span / 2, rel))
        x = x0 + tape_w / 2 + rel * ppd
        yb = y0 + tape_h
        if kind == "ping":
            pts = np.array([[x, yb - 12 * s], [x + 5 * s, yb - 7 * s], [x, yb - 2 * s], [x - 5 * s, yb - 7 * s]])
        else:
            pts = np.array([[x, yb - 9 * s], [x + 5 * s, yb], [x - 5 * s, yb]])
        if clipped:
            cv2.polylines(img, [pts.astype(np.int32)], True, color, 1, cv2.LINE_AA)
        else:
            cv2.fillConvexPoly(img, pts.astype(np.int32), color, cv2.LINE_AA)
    cx = x0 + tape_w / 2
    caret = np.array([[cx, y0 + tape_h + 2 * s], [cx - 6 * s, y0 + tape_h + 10 * s], [cx + 6 * s, y0 + tape_h + 10 * s]])
    cv2.fillConvexPoly(img, caret.astype(np.int32), style.primary, cv2.LINE_AA)
    bw, bh = int(46 * s), int(20 * s)
    bx, by = int(cx - bw / 2), int(y0 + tape_h + 12 * s)
    panel(img, bx, by, bx + bw, by + bh, 0.7)
    cv2.rectangle(img, (bx, by), (bx + bw, by + bh), style.primary, 1, cv2.LINE_AA)
    text(img, f"{int(round(heading)) % 360:03d}", (cx, by + bh / 2), style.primary, 0.5, 1, "cm")


def radar(
    img: np.ndarray,
    heading_deg: float,
    hfov_deg: float,
    style: HudStyle,
    blips: Iterable[Tuple[float, float, BGR, str, str]] = (),
    center: Optional[Tuple[int, int]] = None,
    radius: Optional[int] = None,
) -> None:
    """Heading-up mini radar. ``blips``: (bearing_deg, range_m, color, label, kind)."""
    h, w = img.shape[:2]
    s = scale_of(img)
    r = radius or int(78 * s)
    cx, cy = center or (int(18 * s + r), int(h - 18 * s - r))
    mask_roi = (cx - r, cy - r, cx + r, cy + r)
    sub = img[max(0, mask_roi[1]):mask_roi[3], max(0, mask_roi[0]):mask_roi[2]]
    circle = np.zeros(sub.shape[:2], np.uint8)
    cv2.circle(circle, (cx - max(0, mask_roi[0]), cy - max(0, mask_roi[1])), r, 255, -1)
    sub[circle > 0] = (sub[circle > 0] * (1.0 - style.panel_alpha)).astype(np.uint8)
    for k in (1, 2, 3):
        cv2.circle(img, (cx, cy), int(r * k / 3), style.dim, 1, cv2.LINE_AA)
    half = math.radians(hfov_deg / 2.0)
    for sgn in (-1, 1):
        p = (cx + r * math.sin(sgn * half), cy - r * math.cos(sgn * half))
        cv2.line(img, (cx, cy), ipt(p), style.dim, 1, cv2.LINE_AA)
    # north indicator rotates around the rim
    nb = math.radians(-heading_deg)
    text(img, "N", (cx + (r + 9 * s) * math.sin(nb), cy - (r + 9 * s) * math.cos(nb)), style.primary, 0.38, 1, "cm")
    cv2.circle(img, (cx, cy), r, style.primary, 1, cv2.LINE_AA)
    text(img, f"{int(style.radar_range_m)}m", (cx + r * 0.72, cy + r * 0.92), style.dim, 0.32, 1, "lm")
    tri = np.array([[cx, cy - 6 * s], [cx + 4 * s, cy + 4 * s], [cx - 4 * s, cy + 4 * s]])
    cv2.fillConvexPoly(img, tri.astype(np.int32), style.primary, cv2.LINE_AA)
    for bearing, rng, color, label, kind in blips:
        rel = math.radians(bearing - heading_deg)
        frac = rng / style.radar_range_m
        outside = frac > 1.0
        frac = min(frac, 1.0)
        px, py = cx + r * frac * math.sin(rel), cy - r * frac * math.cos(rel)
        if kind == "ping":
            d = 5 * s
            pts = np.array([[px, py - d], [px + d, py], [px, py + d], [px - d, py]])
            cv2.polylines(img, [pts.astype(np.int32)], True, color, max(1, int(s)), cv2.LINE_AA)
        elif kind == "contact":
            d = 3.5 * s
            cv2.line(img, ipt((px - d, py - d)), ipt((px + d, py + d)), color, max(1, int(s)), cv2.LINE_AA)
            cv2.line(img, ipt((px - d, py + d)), ipt((px + d, py - d)), color, max(1, int(s)), cv2.LINE_AA)
        else:
            cv2.circle(img, ipt((px, py)), max(2, int(4 * s)), color, 1 if outside else -1, cv2.LINE_AA)
            if label:
                text(img, label[:1], (px + 6 * s, py), color, 0.34, 1, "lm")


def telemetry_block(img: np.ndarray, lines: Sequence[str], style: HudStyle, org=None) -> None:
    s = scale_of(img)
    x, y = org or (int(14 * s), int(12 * s))
    lh = int(19 * s)
    width = int(max((len(l) for l in lines), default=0) * 9.2 * s + 16 * s)
    panel(img, x - int(6 * s), y - int(4 * s), x + width, y + lh * len(lines) + int(4 * s), style.panel_alpha)
    for i, line in enumerate(lines):
        text(img, line, (x, y + i * lh), style.primary, 0.45, 1, "lt")


def corner_box(img: np.ndarray, bbox: Sequence[float], color: BGR, thickness: int, frac: float = 0.25) -> None:
    x1, y1, x2, y2 = bbox
    lx, ly = (x2 - x1) * frac, (y2 - y1) * frac
    for (ax, ay, bx, by) in (
        (x1, y1, x1 + lx, y1), (x1, y1, x1, y1 + ly),
        (x2, y1, x2 - lx, y1), (x2, y1, x2, y1 + ly),
        (x1, y2, x1 + lx, y2), (x1, y2, x1, y2 - ly),
        (x2, y2, x2 - lx, y2), (x2, y2, x2, y2 - ly),
    ):
        cv2.line(img, ipt((ax, ay)), ipt((bx, by)), SHADOW, thickness + 2, cv2.LINE_AA)
        cv2.line(img, ipt((ax, ay)), ipt((bx, by)), color, thickness, cv2.LINE_AA)


def dashed_rect(img: np.ndarray, bbox: Sequence[float], color: BGR, dash: int = 6) -> None:
    x1, y1, x2, y2 = (int(v) for v in bbox)
    for xa in range(x1, x2, dash * 2):
        xb = min(xa + dash, x2)
        cv2.line(img, (xa, y1), (xb, y1), color, 1)
        cv2.line(img, (xa, y2), (xb, y2), color, 1)
    for ya in range(y1, y2, dash * 2):
        yb = min(ya + dash, y2)
        cv2.line(img, (x1, ya), (x1, yb), color, 1)
        cv2.line(img, (x2, ya), (x2, yb), color, 1)


def chevron(img: np.ndarray, pt: Sequence[float], color: BGR, label: str, range_m: Optional[float]) -> None:
    """Downward chevron hovering above a ping location."""
    s = scale_of(img)
    x, y = pt
    a, b = 10 * s, 7 * s
    t = max(1, int(round(2 * s)))
    for k in (0, 1):
        oy = y - 6 * s - k * 9 * s
        pts = np.array([[x - a, oy - b], [x, oy], [x + a, oy - b]], np.int32)
        cv2.polylines(img, [pts], False, SHADOW, t + 2, cv2.LINE_AA)
        cv2.polylines(img, [pts], False, color, t, cv2.LINE_AA)
    rng = f" {range_m:.0f}m" if range_m is not None else ""
    text(img, f"{label}{rng}", (x, y - 34 * s), color, 0.42, 1, "cb")


def edge_arrow(
    img: np.ndarray,
    anchor: Sequence[float],
    angle: float,
    color: BGR,
    label: str,
    range_m: Optional[float],
) -> None:
    """Triangle at the screen border pointing toward an off-screen target."""
    s = scale_of(img)
    x, y = anchor
    c, sn = math.cos(angle), math.sin(angle)
    L, W = 16 * s, 9 * s
    tip = (x + c * L / 2, y + sn * L / 2)
    base = (x - c * L / 2, y - sn * L / 2)
    p1 = (base[0] - sn * W, base[1] + c * W)
    p2 = (base[0] + sn * W, base[1] - c * W)
    pts = np.array([tip, p1, p2], np.int32)
    cv2.fillConvexPoly(img, pts, color, cv2.LINE_AA)
    cv2.polylines(img, [pts], True, SHADOW, 1, cv2.LINE_AA)
    rng = f" {range_m:.0f}m" if range_m is not None else ""
    label = f"{label}{rng}"
    (tw, th), _ = cv2.getTextSize(label, FONT, 0.38 * s, max(1, int(round(s))))
    h, w = img.shape[:2]
    tx, ty = x - c * (18 * s + tw / 2), y - sn * (18 * s + th)
    tx = min(max(tx, tw / 2 + 4), w - tw / 2 - 4)
    ty = min(max(ty, th + 4), h - th - 4)
    text(img, label, (tx, ty), color, 0.38, 1, "cm")


def pip(img: np.ndarray, aux: np.ndarray, style: HudStyle, label: str, top: int) -> Tuple[int, int, int, int]:
    """Picture-in-picture aux feed in the top-right corner. Returns its rect."""
    h, w = img.shape[:2]
    s = scale_of(img)
    pw = int(w * style.pip_frac)
    ph = int(pw * aux.shape[0] / max(1, aux.shape[1]))
    ph = min(ph, int(h * 0.4))
    x2, y1 = w - int(14 * s), top
    x1, y2 = x2 - pw, y1 + ph
    aux_bgr = aux if aux.ndim == 3 else cv2.cvtColor(aux, cv2.COLOR_GRAY2BGR)
    img[y1:y2, x1:x2] = cv2.resize(aux_bgr, (pw, ph), interpolation=cv2.INTER_AREA)
    cv2.rectangle(img, (x1 - 1, y1 - 1), (x2, y2), style.primary, 1, cv2.LINE_AA)
    panel(img, x1, y1, x1 + int(len(label) * 8 * s + 12 * s), y1 + int(18 * s), 0.6)
    text(img, label, (x1 + 5 * s, y1 + 4 * s), style.primary, 0.38, 1, "lt")
    return x1, y1, x2, y2
