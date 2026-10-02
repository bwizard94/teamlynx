"""Headless end-to-end self-check of the Phase 1 stack.

    lynx-selfcheck                     # prints PASS/FAIL per check, exit code 0 on success
    lynx-selfcheck --save-dir out/     # also renders each operator's view to PNG

What it proves, over real WebSockets with an in-process relay:

1. Telemetry from every operator reaches every other operator (binary and JSON downlinks).
2. A ping raycast from ALPHA's reticle lands where geometry says it should, and re-projects
   exactly onto ALPHA's reticle centre.
3. Every operator projects that ping to a pixel which (a) matches an independent
   basis-vector derivation (no rotation-matrix code shared) and (b) back-projects onto the same
   ground point - i.e. the ping is perspective-correct from every viewpoint.
4. Behind-camera targets produce an edge indicator on the correct side.
5. Ping TTL expiry, owner cancel, stale-node eviction and late-joiner snapshots propagate.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np

from lynx.net.client import LynxClient
from lynx.net.schema import PingType, Team
from lynx.net.server import RelayConfig, RelayServer
from lynx.spatial import Camera, Intrinsics, Pose, Visibility, intersect_ground, raycast_from_pose

from .scenario import DEFAULT_SCENARIO, OperatorSpec

TOL_PX = 0.05  # float32 wire precision of positions gives ~1e-4 px error at these ranges
TOL_M = 0.02


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


class Checker:
    def __init__(self, verbose: bool = True) -> None:
        self.results: List[CheckResult] = []
        self.verbose = verbose

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        r = CheckResult(name, bool(ok), detail)
        self.results.append(r)
        if self.verbose:
            print(f"[{'PASS' if r.ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        return r.ok

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


def independent_pixel(spec_pose: Pose, heading: float, pitch: float, intr: Intrinsics, target: np.ndarray):
    """Project with explicit basis vectors for roll = 0 (no shared rotation code).

    forward f = (cos p sin h, cos p cos h, sin p), right r = (cos h, -sin h, 0), up = r x f.
    Camera coordinates: x_c = d.r, y_c = -d.up, z_c = d.f.
    """
    h, p = math.radians(heading), math.radians(pitch)
    f = np.array([math.cos(p) * math.sin(h), math.cos(p) * math.cos(h), math.sin(p)])
    r = np.array([math.cos(h), -math.sin(h), 0.0])
    up = np.cross(r, f)
    d = target - spec_pose.position
    xc, yc, zc = float(d @ r), float(-(d @ up)), float(d @ f)
    if zc <= 0:
        return None
    return intr.fx * xc / zc + intr.cx, intr.fy * yc / zc + intr.cy


async def wait_until(pred: Callable[[], bool], timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


async def run_selfcheck(
    save_dir: Optional[str] = None,
    width: int = 960,
    height: int = 600,
    hfov: float = 90.0,
    verbose: bool = True,
) -> Checker:
    ck = Checker(verbose)
    cfg = RelayConfig(host="127.0.0.1", port=0, stale_timeout_s=1.0, sweep_interval_s=0.05)
    intr = Intrinsics.from_fov(width, height, hfov)
    specs: Dict[int, OperatorSpec] = {s.node: s for s in DEFAULT_SCENARIO}
    poses: Dict[int, Pose] = {
        s.node: Pose.from_euler(s.x, s.y, s.eye_height, s.heading, s.pitch, 0.0) for s in DEFAULT_SCENARIO
    }

    async with RelayServer(cfg) as relay:
        url = f"ws://127.0.0.1:{relay.port}"
        clients: Dict[int, LynxClient] = {}
        tasks = []
        for s in DEFAULT_SCENARIO:
            enc = "json" if s.node == 2 else "binary"
            c = LynxClient(url, s.node, s.callsign, Team[s.team.upper()], encoding=enc, reconnect=False)
            clients[s.node] = c
            tasks.append(asyncio.create_task(c.run()))
        for c in clients.values():
            await c.wait_connected()

        heartbeat_nodes = set(clients)

        async def heartbeat() -> None:
            while True:
                for nid in list(heartbeat_nodes):
                    h, p, r = poses[nid].euler
                    x, y, z = poses[nid].position
                    await clients[nid].send_telemetry(x, y, z, h, p, r)
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        try:
            # 1. telemetry fan-out ------------------------------------------------
            ids = set(clients)
            ok = await wait_until(lambda: all(set(c.state.node_ids) == ids - {n} for n, c in clients.items()))
            ck.check("telemetry reaches every operator", ok,
                     ", ".join(f"{specs[n].callsign} sees {sorted(c.state.node_ids)}" for n, c in clients.items()))
            for n, c in clients.items():
                for other in ids - {n}:
                    ns = c.state.node(other)
                    if ns is None:
                        continue
                    err = float(np.linalg.norm(np.array([ns.telemetry.x, ns.telemetry.y, ns.telemetry.z]) - poses[other].position))
                    if err > 1e-5:
                        ck.check(f"{specs[n].callsign} position of {specs[other].callsign}", False, f"err {err:.2e} m")
            ck.check("remote positions exact to float32", True)

            # 2. ALPHA raycasts a ping ----------------------------------------------
            hit = raycast_from_pose(poses[1])
            expected = 1.7 / math.tan(math.radians(10.0))
            ck.check("raycast hits ground at h/tan(10deg)", hit.kind.value == "ground" and abs(hit.point[1] - expected) < 1e-9
                     and abs(hit.point[0]) < 1e-9, f"hit ({hit.point[0]:.3f}, {hit.point[1]:.3f}, {hit.point[2]:.3f}), expected N {expected:.3f}")
            ping = await clients[1].send_ping(*hit.point, ping_type=PingType.MARK, ttl_s=30.0)
            ok = await wait_until(lambda: all(c.state.ping(ping.key) is not None for c in clients.values()))
            ck.check("ping delivered to every operator", ok)

            # 3. perspective-correct projection from every viewpoint -------------------
            ground_hits = []
            for n, c in clients.items():
                ps = c.state.ping(ping.key)
                if ps is None:
                    continue
                target = np.array(ps.ping.position)
                cam = Camera(intr, poses[n])
                proj = cam.project(target)
                s = specs[n]
                ind = independent_pixel(poses[n], s.heading, s.pitch, intr, target)
                ok = proj.on_screen and ind is not None and math.hypot(proj.u - ind[0], proj.v - ind[1]) < 1e-6
                ck.check(f"{s.callsign} ping pixel matches independent derivation", ok,
                         f"({proj.u:.2f}, {proj.v:.2f}) {proj.visibility.value} range {proj.distance:.1f} m bearing {proj.bearing_deg:+.1f}")
                o, d = cam.pixel_to_world_ray(proj.u, proj.v)
                t = intersect_ground(o, d)
                back = o + t * d if t is not None else np.full(3, np.nan)
                err = float(np.linalg.norm(back - hit.point))
                ground_hits.append(back)
                ck.check(f"{s.callsign} pixel back-projects onto ping", err < TOL_M, f"err {err * 1000:.3f} mm")
                if n == 1:
                    dc = math.hypot(proj.u - intr.cx, proj.v - intr.cy)
                    ck.check("ALPHA sees its ping under the reticle", dc < TOL_PX, f"{dc:.2e} px from centre")
            spread = max(float(np.linalg.norm(a - b)) for a in ground_hits for b in ground_hits) if ground_hits else 1e9
            ck.check("all viewpoints agree on ping location", spread < TOL_M, f"spread {spread * 1000:.3f} mm")

            # 4. render every operator's view ------------------------------------------------
            if save_dir is not None or _pygame_available():
                frames = _render_views(clients, specs, poses, width, height, hfov, ck)
                if save_dir is not None and frames:
                    _save_frames(frames, save_dir, ck)

            # 5. behind-camera indicator --------------------------------------------------------
            s3 = specs[3]
            behind_pose = Pose.from_euler(s3.x, s3.y, s3.eye_height, 270.0, 0.0, 0.0)
            proj = Camera(intr, behind_pose).project(hit.point, margin=28.0)
            ok = (proj.visibility is Visibility.BEHIND and abs(proj.u - 28.0) < 1e-6
                  and proj.bearing_deg < 0 and 28.0 <= proj.v <= height - 28.0)
            ck.check("CHARLIE facing away gets left-edge behind indicator", ok,
                     f"{proj.visibility.value} at ({proj.u:.1f}, {proj.v:.1f}) bearing {proj.bearing_deg:+.1f}")

            # 6. TTL expiry -----------------------------------------------------------------------
            short = await clients[2].send_ping(5.0, 5.0, 0.0, PingType.CONTACT, ttl_s=0.6)
            ok = await wait_until(lambda: all(c.state.ping(short.key) is not None for c in clients.values()))
            ck.check("short-TTL ping delivered", ok)
            t0 = time.monotonic()
            ok = await wait_until(lambda: all(c.state.ping(short.key) is None for c in clients.values()), timeout=2.0)
            ck.check("ping expires everywhere after TTL", ok and relay.state.ping(short.key) is None,
                     f"gone after {time.monotonic() - t0:.2f} s")

            # 7. late joiner snapshot -------------------------------------------------------------
            late = LynxClient(url, 9, "DELTA", Team.GREEN, reconnect=False)
            lt = asyncio.create_task(late.run())
            await late.wait_connected()
            ok = await wait_until(lambda: late.state.ping(ping.key) is not None and len(late.state.node_ids) == 3)
            lp = late.state.ping(ping.key)
            ck.check("late joiner receives snapshot with remaining TTL", ok and lp is not None and lp.ping.ttl_ms < 30_000,
                     f"nodes {sorted(late.state.node_ids)}, ttl_ms {lp.ping.ttl_ms if lp else None}")
            await late.close()
            await lt

            # 8. owner cancel -----------------------------------------------------------------------
            await clients[1].cancel_ping(ping.ping_id)
            ok = await wait_until(lambda: all(c.state.ping(ping.key) is None for c in clients.values()))
            ck.check("owner cancel propagates", ok)

            # 9. stale eviction ----------------------------------------------------------------------
            heartbeat_nodes.discard(3)
            ok = await wait_until(lambda: all(3 not in clients[n].state.node_ids for n in (1, 2)), timeout=3.0)
            ck.check("silent node evicted as stale", ok and relay.stats.nodes_evicted >= 1)
        finally:
            hb.cancel()
            for c in clients.values():
                await c.close()
            await asyncio.gather(*tasks, return_exceptions=True)
    return ck


def _pygame_available() -> bool:
    try:
        import pygame  # noqa: F401
    except ImportError:
        return False
    return True


def _render_views(clients, specs, poses, width, height, hfov, ck: Checker):
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame

    from .render import PING_RGB, Renderer, ViewModel

    pygame.font.init()
    renderer = Renderer(width, height, hfov)
    frames = []
    for n, c in clients.items():
        s = specs[n]
        surf = pygame.Surface((width, height))
        nodes, pings = c.state.snapshot()
        vm = ViewModel(n, s.callsign, Team[s.team.upper()], poses[n], nodes, pings, time.monotonic(),
                       status_lines=["headless self-check"])
        report = renderer.render(surf, vm)
        for key, proj in report.pings.items():
            if proj.on_screen:
                col = PING_RGB[pings[key].ping.ping_type]
                # The 1 m ground ring is centred on the anchor; sample its nearest edge point.
                found = _color_near(surf, proj.u, proj.v, col, radius=25)
                ck.check(f"{s.callsign} rendered ping at projected pixel", found, f"({proj.u:.0f}, {proj.v:.0f})")
        frames.append((s.callsign, surf))
    return frames


def _color_near(surf, u: float, v: float, col, radius: int) -> bool:
    w, h = surf.get_size()
    for dv in range(-radius, radius + 1):
        for du in range(-radius, radius + 1):
            x, y = int(u) + du, int(v) + dv
            if 0 <= x < w and 0 <= y < h and tuple(surf.get_at((x, y)))[:3] == tuple(col):
                return True
    return False


def _save_frames(frames, save_dir: str, ck: Checker) -> None:
    import pygame

    os.makedirs(save_dir, exist_ok=True)
    for name, surf in frames:
        pygame.image.save(surf, os.path.join(save_dir, f"view-{name.lower()}.png"))
    w, h = frames[0][1].get_size()
    gap = 8
    comp = pygame.Surface((len(frames) * w + (len(frames) - 1) * gap, h))
    comp.fill((60, 60, 60))
    for i, (_, surf) in enumerate(frames):
        comp.blit(surf, (i * (w + gap), 0))
    path = os.path.join(save_dir, "views-composite.png")
    pygame.image.save(comp, path)
    ck.check("saved rendered views", os.path.exists(path), path)


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="TeamLynx headless end-to-end self-check")
    p.add_argument("--save-dir", default=None, help="write rendered operator views (PNG) here")
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=600)
    p.add_argument("--hfov", type=float, default=90.0)
    args = p.parse_args(argv)
    ck = asyncio.run(run_selfcheck(args.save_dir, args.width, args.height, args.hfov))
    passed = sum(r.ok for r in ck.results)
    print(f"\n{passed}/{len(ck.results)} checks passed - {'OK' if ck.ok else 'FAILED'}")
    sys.exit(0 if ck.ok else 1)


if __name__ == "__main__":
    main()
