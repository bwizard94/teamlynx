"""After-action review: rebuild a session from logs and render it top-down.

    lynx-field replay /var/log/lynx/ --site /etc/lynx/site.json --out aar.png [--video aar.mp4 --speed 10]

Inputs are one or more session logs (relay log, observer log, headset logs; files or directories),
merged on wall-clock time. Telemetry and headset ``pose`` records become per-node tracks; pings
become events with their creation time, end time (cancel, replace, or TTL expiry) and owner;
``event`` records (link up/down, failover) become timeline marks.

The overview image is north-up in the site frame: grid with metre labels, datum, stations and
bearing markers from the site file, one colour per node with start/end markers, gaps longer than
``gap_s`` drawn dashed (link loss or headset off), pings by type with owner and clock time, and a
timeline strip per node (telemetry coverage, link-down intervals, pings). Requires OpenCV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .session import LoadedLog

TEAM_BGR = {"blue": (255, 170, 40), "green": (60, 220, 90), "red": (60, 60, 240), "amber": (40, 180, 255)}
NODE_PALETTE = [(255, 170, 40), (60, 220, 90), (0, 200, 255), (255, 90, 200), (90, 255, 255),
                (255, 255, 120), (160, 120, 255), (120, 200, 120), (60, 140, 255), (200, 200, 200)]
PING_BGR = {"mark": (0, 235, 255), "contact": (0, 140, 255), "move": (255, 255, 0), "danger": (60, 60, 255),
            "rally": (120, 255, 140)}
BG = (22, 24, 22)
GRID = (52, 60, 52)
TEXT = (210, 230, 210)


@dataclass
class TrackPoint:
    t: float
    x: float
    y: float
    heading: float
    link: bool = True


@dataclass
class NodeTrack:
    node: int
    callsign: str = ""
    team: str = "blue"
    points: List[TrackPoint] = field(default_factory=list)

    def at(self, t: float) -> Optional[TrackPoint]:
        """Linear interpolation (None before the first point; holds the last one)."""
        if not self.points or t < self.points[0].t:
            return None
        ts = [p.t for p in self.points]
        i = int(np.searchsorted(ts, t, side="right"))
        if i >= len(self.points):
            return self.points[-1]
        a, b = self.points[i - 1], self.points[i]
        if b.t - a.t > 3.0:
            return a
        f = (t - a.t) / max(b.t - a.t, 1e-9)
        dh = (b.heading - a.heading + 180.0) % 360.0 - 180.0
        return TrackPoint(t, a.x + f * (b.x - a.x), a.y + f * (b.y - a.y), (a.heading + f * dh) % 360.0, a.link)

    def distance_m(self, gap_s: float = 3.0, max_jump_m: float = 25.0) -> float:
        d = 0.0
        for a, b in zip(self.points, self.points[1:]):
            step = math.hypot(b.x - a.x, b.y - a.y)
            if b.t - a.t <= gap_s and step <= max_jump_m:
                d += step
        return d


@dataclass
class PingEvent:
    owner: int
    ping_id: int
    ping_type: str
    x: float
    y: float
    z: float
    t_start: float
    t_end: float
    end: str = "expired"  # owner | expired | replaced | open


@dataclass
class TimelineEvent:
    t: float
    src: str
    kind: str
    detail: str = ""


@dataclass
class Session:
    tracks: Dict[int, NodeTrack] = field(default_factory=dict)
    pings: List[PingEvent] = field(default_factory=list)
    events: List[TimelineEvent] = field(default_factory=list)
    t0: float = 0.0
    t1: float = 0.0
    sources: List[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def link_down_intervals(self) -> Dict[str, List[Tuple[float, float]]]:
        out: Dict[str, List[Tuple[float, float]]] = {}
        down: Dict[str, float] = {}
        for ev in self.events:
            if ev.kind == "disconnected" and ev.src not in down:
                down[ev.src] = ev.t
            elif ev.kind == "connected" and ev.src in down:
                out.setdefault(ev.src, []).append((down.pop(ev.src), ev.t))
        for src, t in down.items():
            out.setdefault(src, []).append((t, self.t1))
        return out


def build_session(log: LoadedLog) -> Session:
    s = Session(sources=list(log.sources))
    active: Dict[Tuple[int, int], PingEvent] = {}
    times: List[float] = []
    for rec in log.records:
        t = float(rec["t"])
        kind = rec.get("kind")
        if kind == "pose":
            tr = s.tracks.setdefault(int(rec["node"]), NodeTrack(int(rec["node"])))
            tr.callsign = rec.get("callsign", tr.callsign) or tr.callsign
            tr.team = rec.get("team", tr.team) or tr.team
            tr.points.append(TrackPoint(t, float(rec["x"]), float(rec["y"]), float(rec["heading"]),
                                        bool(rec.get("link", True))))
            times.append(t)
        elif kind == "msg":
            m = rec.get("msg") or {}
            mtype = m.get("type")
            times.append(t)
            if mtype == "telemetry":
                nid = int(m["node_id"])
                tr = s.tracks.setdefault(nid, NodeTrack(nid))
                tr.callsign = m.get("callsign") or tr.callsign
                tr.team = str(m.get("team", tr.team))
                tr.points.append(TrackPoint(t, float(m["x"]), float(m["y"]), float(m["heading"])))
            elif mtype == "ping":
                key = (int(m["owner"]), int(m["ping_id"]))
                ttl = float(m.get("ttl_ms", 60_000)) / 1000.0 or 60.0
                pe = active.get(key)
                if pe is not None and pe.t_end >= t:
                    pe.x, pe.y, pe.z = float(m["x"]), float(m["y"]), float(m["z"])
                    pe.t_end = max(pe.t_end, t + ttl)
                else:
                    pe = PingEvent(key[0], key[1], str(m.get("ping_type", "mark")), float(m["x"]), float(m["y"]),
                                   float(m["z"]), t, t + ttl)
                    active[key] = pe
                    s.pings.append(pe)
            elif mtype == "ping_cancel":
                key = (int(m["owner"]), int(m["ping_id"]))
                pe = active.get(key)
                if pe is not None and pe.t_start <= t <= pe.t_end + 1.0:
                    pe.t_end = t
                    pe.end = str(m.get("reason", "owner"))
                    del active[key]
            elif mtype == "node_leave":
                s.events.append(TimelineEvent(t, f"node:{m.get('node')}", "leave", str(m.get("reason", ""))))
        elif kind == "event":
            s.events.append(TimelineEvent(t, str(rec.get("src", "")), str(rec.get("event", "")),
                                          str(rec.get("url", "") or rec.get("detail", ""))))
            times.append(t)
    for tr in s.tracks.values():
        tr.points.sort(key=lambda p: p.t)
        dedup: List[TrackPoint] = []
        for p in tr.points:
            if dedup and abs(p.t - dedup[-1].t) < 1e-3:
                continue
            dedup.append(p)
        tr.points = dedup
    if times:
        s.t0, s.t1 = min(times), max(times)
    for pe in s.pings:
        if pe.t_end > s.t1 and pe.end == "expired":
            pe.end = "open"
    return s


def summarize(s: Session) -> str:
    def clock(t: float) -> str:
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")

    lines = [f"session {clock(s.t0)}-{clock(s.t1)} UTC  ({s.duration_s / 60:.1f} min, sources: {', '.join(s.sources)})"]
    for nid in sorted(s.tracks):
        tr = s.tracks[nid]
        if not tr.points:
            continue
        gaps = [b.t - a.t for a, b in zip(tr.points, tr.points[1:])]
        lines.append(f"  N{nid:<3} {tr.callsign:8} {tr.team:6} {len(tr.points):6} pts  "
                     f"{tr.distance_m():7.0f} m  longest gap {max(gaps, default=0.0):5.1f} s")
    by_type: Dict[str, int] = {}
    for pe in s.pings:
        by_type[pe.ping_type] = by_type.get(pe.ping_type, 0) + 1
    lines.append(f"  pings: {len(s.pings)} " + " ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    for src, ivals in sorted(s.link_down_intervals().items()):
        total = sum(b - a for a, b in ivals)
        lines.append(f"  link down {src}: {len(ivals)}x, {total:.1f} s total")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- drawing


@dataclass
class MapView:
    emin: float
    nmax: float
    scale: float
    origin: Tuple[int, int]
    size: Tuple[int, int]

    def px(self, e: float, n: float) -> Tuple[int, int]:
        return (int(round(self.origin[0] + (e - self.emin) * self.scale)),
                int(round(self.origin[1] + (self.nmax - n) * self.scale)))


def nice_step(span: float, target_lines: int = 8) -> float:
    raw = max(span, 1.0) / target_lines
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def _bounds(s: Session, site=None) -> Tuple[float, float, float, float]:
    es: List[float] = [0.0]
    ns: List[float] = [0.0]
    for tr in s.tracks.values():
        es += [p.x for p in tr.points]
        ns += [p.y for p in tr.points]
    for pe in s.pings:
        es.append(pe.x)
        ns.append(pe.y)
    if site is not None:
        for p in list(site.stations.values()) + list(site.markers.values()):
            es.append(p.e)
            ns.append(p.n)
    e0, e1, n0, n1 = min(es), max(es), min(ns), max(ns)
    pad = max(10.0, 0.08 * max(e1 - e0, n1 - n0))
    return e0 - pad, e1 + pad, n0 - pad, n1 + pad


def make_view(s: Session, size: Tuple[int, int], site=None, map_h: Optional[int] = None) -> MapView:
    w, h = size
    map_h = map_h or h
    e0, e1, n0, n1 = _bounds(s, site)
    scale = min((w - 80) / (e1 - e0), (map_h - 80) / (n1 - n0))
    ox = int((w - (e1 - e0) * scale) / 2)
    oy = int((map_h - (n1 - n0) * scale) / 2)
    return MapView(e0, n1, scale, (ox, oy), (w, map_h))


def _text(img, txt: str, org, color=TEXT, scale: float = 0.45, thick: int = 1) -> None:
    import cv2

    cv2.putText(img, txt, (int(org[0]) + 1, int(org[1]) + 1), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 1,
                cv2.LINE_AA)
    cv2.putText(img, txt, (int(org[0]), int(org[1])), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def _dashed(img, a, b, color, dash: int = 6) -> None:
    import cv2

    d = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(d // dash))
    for i in range(0, n, 2):
        p = (int(a[0] + (b[0] - a[0]) * i / n), int(a[1] + (b[1] - a[1]) * i / n))
        q = (int(a[0] + (b[0] - a[0]) * min(i + 1, n) / n), int(a[1] + (b[1] - a[1]) * min(i + 1, n) / n))
        cv2.line(img, p, q, color, 1, cv2.LINE_AA)


def draw_map_base(img, view: MapView, site=None) -> None:
    import cv2

    w, h = view.size
    e1 = view.emin + (w - 2 * view.origin[0]) / view.scale
    n0 = view.nmax - (h - 2 * view.origin[1]) / view.scale
    step = nice_step(max(e1 - view.emin, view.nmax - n0))
    e = math.floor(view.emin / step) * step
    while e <= e1 + 1e-9:
        x = view.px(e, 0)[0]
        cv2.line(img, (x, 0), (x, h), GRID, 1)
        _text(img, f"E{e:+.0f}", (x + 3, h - 6), (120, 140, 120), 0.38)
        e += step
    n = math.floor(n0 / step) * step
    while n <= view.nmax + 1e-9:
        y = view.px(0, n)[1]
        cv2.line(img, (0, y), (w, y), GRID, 1)
        _text(img, f"N{n:+.0f}", (4, y - 3), (120, 140, 120), 0.38)
        n += step
    x0, y0 = view.px(0, 0)
    cv2.drawMarker(img, (x0, y0), (230, 230, 230), cv2.MARKER_CROSS, 18, 2)
    _text(img, "DATUM", (x0 + 8, y0 + 16), (230, 230, 230), 0.42)
    if site is not None:
        for p in site.stations.values():
            q = view.px(p.e, p.n)
            cv2.rectangle(img, (q[0] - 4, q[1] - 4), (q[0] + 4, q[1] + 4), (200, 200, 200), 1)
            _text(img, p.name, (q[0] + 6, q[1] - 6), (200, 200, 200), 0.38)
        for p in site.markers.values():
            q = view.px(p.e, p.n)
            pts = np.array([[q[0], q[1] - 7], [q[0] + 6, q[1] + 5], [q[0] - 6, q[1] + 5]], np.int32)
            cv2.polylines(img, [pts], True, (255, 255, 255), 1, cv2.LINE_AA)
            _text(img, p.name, (q[0] + 8, q[1] + 4), (255, 255, 255), 0.4)
    # north arrow + scale bar
    ax, ay = w - 40, 50
    cv2.arrowedLine(img, (ax, ay + 30), (ax, ay - 10), (230, 230, 230), 2, cv2.LINE_AA, tipLength=0.35)
    _text(img, "N", (ax - 6, ay + 50), (230, 230, 230), 0.55, 2)
    bar = step
    bx, by = w - 60 - int(bar * view.scale), h - 30
    cv2.line(img, (bx, by), (bx + int(bar * view.scale), by), (230, 230, 230), 2)
    _text(img, f"{bar:.0f} m", (bx, by - 8), (230, 230, 230), 0.42)


def node_color(s: Session, nid: int) -> Tuple[int, int, int]:
    order = sorted(s.tracks)
    return NODE_PALETTE[order.index(nid) % len(NODE_PALETTE)] if nid in order else (200, 200, 200)


def draw_ping(img, view: MapView, pe: PingEvent, label: Optional[str], alpha: bool = False) -> None:
    import cv2

    c = PING_BGR.get(pe.ping_type, PING_BGR["mark"])
    p = view.px(pe.x, pe.y)
    if pe.ping_type == "contact":
        cv2.drawMarker(img, p, c, cv2.MARKER_TILTED_CROSS, 14, 2)
    elif pe.ping_type == "danger":
        cv2.drawMarker(img, p, c, cv2.MARKER_TRIANGLE_UP, 14, 2)
    elif pe.ping_type == "rally":
        cv2.circle(img, p, 7, c, 2, cv2.LINE_AA)
    elif pe.ping_type == "move":
        cv2.drawMarker(img, p, c, cv2.MARKER_SQUARE, 12, 2)
    else:
        cv2.drawMarker(img, p, c, cv2.MARKER_DIAMOND, 14, 2)
    if label:
        _text(img, label, (p[0] + 9, p[1] - 6), c, 0.38)


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")


def render_overview(s: Session, site=None, size: Tuple[int, int] = (1600, 1200), gap_s: float = 3.0,
                    title: str = "") -> np.ndarray:
    import cv2

    w, h = size
    timeline_h = 40 + 22 * max(1, len(s.tracks))
    map_h = h - timeline_h
    img = np.full((h, w, 3), BG, np.uint8)
    mapimg = img[:map_h]
    view = make_view(s, (w, map_h), site)
    draw_map_base(mapimg, view, site)
    callsign = {nid: tr.callsign or f"N{nid}" for nid, tr in s.tracks.items()}
    for nid in sorted(s.tracks):
        tr = s.tracks[nid]
        if not tr.points:
            continue
        col = node_color(s, nid)
        pts = [view.px(p.x, p.y) for p in tr.points]
        for (a, b), (pa, pb) in zip(zip(tr.points, tr.points[1:]), zip(pts, pts[1:])):
            if b.t - a.t > gap_s:
                _dashed(mapimg, pa, pb, col)
            else:
                cv2.line(mapimg, pa, pb, col, 2, cv2.LINE_AA)
        cv2.circle(mapimg, pts[0], 5, col, 1, cv2.LINE_AA)
        end = tr.points[-1]
        cv2.circle(mapimg, pts[-1], 6, col, -1, cv2.LINE_AA)
        hx = pts[-1][0] + int(16 * math.sin(math.radians(end.heading)))
        hy = pts[-1][1] - int(16 * math.cos(math.radians(end.heading)))
        cv2.line(mapimg, pts[-1], (hx, hy), col, 2, cv2.LINE_AA)
        _text(mapimg, callsign[nid], (pts[-1][0] + 9, pts[-1][1] + 16), col, 0.5)
    for pe in s.pings:
        owner = callsign.get(pe.owner, f"N{pe.owner}")
        draw_ping(mapimg, view, pe, f"{pe.ping_type.upper()} {owner} {_clock(pe.t_start)}")
    head = title or f"TeamLynx AAR  {_clock(s.t0)}-{_clock(s.t1)} UTC  {s.duration_s / 60:.1f} min"
    _text(mapimg, head, (14, 26), (235, 255, 235), 0.6, 1)
    y = 48
    for nid in sorted(s.tracks):
        tr = s.tracks[nid]
        _text(mapimg, f"N{nid:02d} {callsign[nid]:8} {tr.team:5} {tr.distance_m():5.0f} m", (14, y),
              node_color(s, nid), 0.42)
        y += 18
    _text(mapimg, f"pings {len(s.pings)}", (14, y), PING_BGR["mark"], 0.42)
    draw_timeline(img[map_h:], s)
    return img


def draw_timeline(img, s: Session) -> None:
    import cv2

    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), (40, 46, 40), 1)
    if s.duration_s <= 0:
        return
    x0, x1 = 120, w - 20

    def tx(t: float) -> int:
        return int(x0 + (t - s.t0) / s.duration_s * (x1 - x0))

    down = s.link_down_intervals()
    for i, nid in enumerate(sorted(s.tracks)):
        y = 24 + 22 * i
        tr = s.tracks[nid]
        col = node_color(s, nid)
        _text(img, f"{tr.callsign or nid}", (8, y + 5), col, 0.42)
        for a, b in zip(tr.points, tr.points[1:]):
            if b.t - a.t <= 3.0:
                cv2.line(img, (tx(a.t), y), (tx(b.t), y), col, 6)
        for a, b in down.get(f"node:{nid}", []):
            cv2.rectangle(img, (tx(a), y - 6), (tx(b), y + 6), (60, 60, 230), 1)
        for pe in s.pings:
            if pe.owner == nid:
                cv2.drawMarker(img, (tx(pe.t_start), y), PING_BGR.get(pe.ping_type, PING_BGR["mark"]),
                               cv2.MARKER_DIAMOND, 10, 2)
    _text(img, _clock(s.t0), (x0, h - 6), TEXT, 0.38)
    _text(img, _clock(s.t1), (x1 - 60, h - 6), TEXT, 0.38)


def render_frame(s: Session, t: float, view: MapView, site=None, trail_s: float = 60.0) -> np.ndarray:
    import cv2

    w, h = view.size
    img = np.full((h, w, 3), BG, np.uint8)
    draw_map_base(img, view, site)
    callsign = {nid: tr.callsign or f"N{nid}" for nid, tr in s.tracks.items()}
    for pe in s.pings:
        if pe.t_start <= t <= pe.t_end:
            draw_ping(img, view, pe, f"{pe.ping_type.upper()} {callsign.get(pe.owner, pe.owner)}")
    for nid, tr in sorted(s.tracks.items()):
        col = node_color(s, nid)
        trail = [p for p in tr.points if t - trail_s <= p.t <= t]
        for a, b in zip(trail, trail[1:]):
            if b.t - a.t <= 3.0:
                cv2.line(img, view.px(a.x, a.y), view.px(b.x, b.y), col, 1, cv2.LINE_AA)
        cur = tr.at(t)
        if cur is None:
            continue
        age = t - max((p.t for p in tr.points if p.t <= t), default=t)
        stale = age > 2.0
        draw_col = tuple(int(c * 0.5) for c in col) if stale else col
        q = view.px(cur.x, cur.y)
        cv2.circle(img, q, 7, draw_col, -1 if not stale else 1, cv2.LINE_AA)
        hx = q[0] + int(18 * math.sin(math.radians(cur.heading)))
        hy = q[1] - int(18 * math.cos(math.radians(cur.heading)))
        cv2.line(img, q, (hx, hy), draw_col, 2, cv2.LINE_AA)
        _text(img, callsign[nid] + (f" {age:.0f}s" if stale else ""), (q[0] + 9, q[1] + 16), draw_col, 0.48)
    _text(img, f"{_clock(t)} UTC  T+{t - s.t0:6.1f} s", (14, 26), (235, 255, 235), 0.6)
    return img


def write_video(s: Session, path: str, site=None, size: Tuple[int, int] = (1280, 960), fps: float = 10.0,
                speed: float = 10.0) -> int:
    import cv2

    view = make_view(s, size, site)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not writer.isOpened():
        raise RuntimeError(f"cannot open video writer for {path}")
    frames = 0
    t = s.t0
    step = speed / fps
    try:
        while t <= s.t1 + 1e-9:
            writer.write(render_frame(s, t, view, site))
            frames += 1
            t += step
    finally:
        writer.release()
    return frames


def replay(paths: Sequence[str], out_png: Optional[str] = None, site_path: Optional[str] = None,
           video: Optional[str] = None, speed: float = 10.0, offsets: Optional[Dict[str, float]] = None) -> Session:
    import cv2

    from .session import load_records
    from .site import Site

    site = Site.load(site_path) if site_path else None
    sess = build_session(load_records(paths, offsets))
    if out_png:
        cv2.imwrite(out_png, render_overview(sess, site))
    if video:
        write_video(sess, video, site, speed=speed)
    return sess
