"""Convenience launcher: relay + N operator windows as separate processes.

    lynx-sim-launch                 # relay + 3 windows (ALPHA, BRAVO, CHARLIE)
    lynx-sim-launch --clients 2
    lynx-sim-launch --no-relay      # windows only, relay already running elsewhere

Every process is an independent program talking over real WebSockets, exactly as separate
headsets would. Close any window or press Ctrl+C here to stop everything.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from typing import List, Optional

from .scenario import DEFAULT_SCENARIO


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Launch the TeamLynx relay and operator windows")
    p.add_argument("--clients", type=int, default=3, choices=[1, 2, 3])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-relay", action="store_true")
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=600)
    p.add_argument("--tile", action="store_true", help="tile windows in a 2-column grid")
    args = p.parse_args(argv)

    procs: List[subprocess.Popen] = []
    py = sys.executable
    try:
        if not args.no_relay:
            procs.append(subprocess.Popen([py, "-m", "lynx.net.server", "--host", args.host, "--port", str(args.port)]))
            if not wait_for_port(args.host, args.port):
                raise SystemExit("relay did not start")
        url = f"ws://{args.host}:{args.port}"
        for i, spec in enumerate(DEFAULT_SCENARIO[: args.clients]):
            cmd = [
                py, "-m", "lynx.sim.app",
                "--url", url,
                "--node", str(spec.node),
                "--callsign", spec.callsign,
                "--team", spec.team,
                "--x", str(spec.x),
                "--y", str(spec.y),
                "--heading", str(spec.heading),
                "--pitch", str(spec.pitch),
                "--width", str(args.width),
                "--height", str(args.height),
            ]
            if args.tile:
                col, row = i % 2, i // 2
                cmd += ["--window-pos", f"{col * (args.width + 10)},{30 + row * (args.height + 40)}"]
            procs.append(subprocess.Popen(cmd))
        windows = procs[0 if args.no_relay else 1:]
        while all(w.poll() is None for w in windows):
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
