"""Launch the cheap two-operator proof-of-concept (not the ~$1,080 field kit).

    lynx-poc                         # tier A: relay + two synthetic headsets
    lynx-poc --tier bench            # webcam defaults; override --alpha-source / --bravo-source
    lynx-poc --dry-run               # print the exact commands and exit
    lynx-poc --no-relay --url ws://192.168.8.1:8765
    lynx-poc --bravo-pose serial:/dev/ttyUSB0

Configs live in ``deploy/poc/``. Close any headset window or press Ctrl+C here to stop everything.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from lynx.sim.launch import wait_for_port

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PACKAGE_ROOT / "deploy" / "poc"
TIER_FILES = {
    "laptop": "laptop.json",
    "a": "laptop.json",
    "synthetic": "laptop.json",
    "bench": "bench.json",
    "b": "bench.json",
}


@dataclass
class OperatorSpec:
    node: int
    callsign: str
    team: str = "blue"
    x: float = 0.0
    y: float = 0.0
    eye_height: float = 1.7
    heading: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    source: str = "synthetic"
    pose: str = "keyboard"
    hfov: float = 78.0
    width: int = 1280
    height: int = 720
    extra: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OperatorSpec":
        extra = list(d.get("extra") or [])
        known = {k: v for k, v in d.items() if k != "extra"}
        unknown = set(known) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown operator fields {sorted(unknown)}")
        return cls(extra=extra, **known)


@dataclass
class PocPlan:
    name: str
    host: str
    port: int
    url: str
    start_relay: bool
    operators: List[OperatorSpec]
    headset_common: List[str] = field(default_factory=list)

    def relay_cmd(self, py: str) -> List[str]:
        return [py, "-m", "lynx.net.server", "--host", self.host, "--port", str(self.port)]

    def headset_cmd(self, spec: OperatorSpec, py: str) -> List[str]:
        cmd = [
            py, "-m", "lynx.headset.app",
            "--url", self.url,
            "--node", str(spec.node),
            "--callsign", spec.callsign,
            "--team", spec.team,
            "--x", _fmt(spec.x),
            "--y", _fmt(spec.y),
            "--eye-height", _fmt(spec.eye_height),
            "--heading", _fmt(spec.heading),
            "--pitch", _fmt(spec.pitch),
            "--roll", _fmt(spec.roll),
            "--pose", spec.pose,
            "--source", spec.source,
            "--hfov", _fmt(spec.hfov),
            "--width", str(spec.width),
            "--height", str(spec.height),
            *self.headset_common,
            *spec.extra,
        ]
        return cmd

    def commands(self, py: str = sys.executable) -> List[List[str]]:
        cmds: List[List[str]] = []
        if self.start_relay:
            cmds.append(self.relay_cmd(py))
        for spec in self.operators:
            cmds.append(self.headset_cmd(spec, py))
        return cmds


def _fmt(value: float) -> str:
    return format(value, "g")


def _port_from_url(url: str, fallback: int) -> int:
    parsed = urlparse(url)
    return parsed.port or fallback


def load_session(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    if "operators" not in data:
        raise ValueError(f"{path} must contain an 'operators' list")
    return data


def resolve_config(tier: str, config: Optional[str]) -> Path:
    if config:
        path = Path(config)
        if not path.is_file():
            raise SystemExit(f"config not found: {path}")
        return path
    key = tier.lower()
    if key not in TIER_FILES:
        raise SystemExit(f"unknown tier {tier!r}; use laptop/a or bench/b")
    path = DEFAULT_CONFIG_DIR / TIER_FILES[key]
    if not path.is_file():
        raise SystemExit(f"missing bundled config {path}")
    return path


def build_plan(args: argparse.Namespace) -> PocPlan:
    data = load_session(resolve_config(args.tier, args.config))
    relay = dict(data.get("relay") or {})
    bind_host = args.host or relay.get("host") or "127.0.0.1"
    port = args.port if args.port is not None else int(relay.get("port") or 8765)
    if args.role == "bravo":
        start_relay = False
    elif args.role == "relay":
        start_relay = True
    else:
        start_relay = not args.no_relay
    client_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host
    url = args.url or f"ws://{client_host}:{port}"
    bind = bind_host if start_relay else client_host

    operators = [OperatorSpec.from_dict(d) for d in data["operators"]]
    if len(operators) < 2:
        raise SystemExit("POC session needs two operators (ALPHA and BRAVO)")

    if args.alpha_source is not None:
        operators[0].source = args.alpha_source
    if args.bravo_source is not None:
        operators[1].source = args.bravo_source
    if args.alpha_pose is not None:
        operators[0].pose = args.alpha_pose
    if args.bravo_pose is not None:
        operators[1].pose = args.bravo_pose
    if args.hfov is not None:
        for spec in operators:
            spec.hfov = args.hfov

    role = args.role
    if role == "alpha":
        operators = operators[:1]
    elif role == "bravo":
        operators = operators[1:2]
    elif role == "relay":
        operators = []
    elif role != "both":
        raise SystemExit(f"unknown role {role!r}")

    common: List[str] = list(data.get("headset_common") or [])
    if args.edge and "--edge" not in common:
        common.append("--edge")
    if args.allow_no_detector and "--allow-no-detector" not in common:
        common.append("--allow-no-detector")
    if args.headless and "--headless" not in common:
        common.append("--headless")
    if args.frames:
        common += ["--frames", str(args.frames)]
    if args.record and operators:
        operators[0].extra = list(operators[0].extra) + ["--record", args.record]
    if args.verbose:
        common.append("--verbose")
    common += list(args.headset_arg or [])

    return PocPlan(
        name=str(data.get("name") or args.tier),
        host=bind,
        port=_port_from_url(url, port),
        url=url,
        start_relay=start_relay,
        operators=operators,
        headset_common=common,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tier", default="laptop",
                   help="laptop/a (synthetic) or bench/b (USB cameras). Ignored if --config is set")
    p.add_argument("--config", default=None, help="path to a deploy/poc session JSON")
    p.add_argument("--host", default=None, help="relay bind host (default from config or 127.0.0.1)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--url", default=None, help="headset WebSocket URL (default ws://host:port)")
    p.add_argument("--no-relay", action="store_true", help="do not start a relay; join --url")
    p.add_argument(
        "--role",
        default="both",
        choices=("both", "alpha", "bravo", "relay"),
        help="both = relay+two HUDs (one machine). On two laptops: ALPHA machine "
             "--role alpha (starts relay), BRAVO machine --role bravo --no-relay --url ws://IP:8765",
    )
    p.add_argument("--alpha-source", default=None, help="override ALPHA --source")
    p.add_argument("--bravo-source", default=None, help="override BRAVO --source")
    p.add_argument("--alpha-pose", default=None, help="override ALPHA --pose")
    p.add_argument("--bravo-pose", default=None, help="override BRAVO --pose")
    p.add_argument("--hfov", type=float, default=None, help="override both cameras' HFOV (deg)")
    p.add_argument("--edge", action="store_true", help="start both HUDs in EagleEye edge mode")
    p.add_argument("--allow-no-detector", action="store_true",
                   help="webcam demo may run without YOLO (IFF outlines need detections)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--frames", type=int, default=0)
    p.add_argument("--record", default="", help="write ALPHA's HUD to this mp4")
    p.add_argument("--headset-arg", action="append", default=[],
                   help="extra lynx-headset flag, repeatable (applied to both)")
    p.add_argument("--dry-run", action="store_true", help="print commands and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def format_plan(plan: PocPlan, py: str = sys.executable) -> str:
    lines = [f"# {plan.name}  relay={plan.url}  start_relay={plan.start_relay}"]
    for cmd in plan.commands(py):
        lines.append(" ".join(_shell(a) for a in cmd))
    return "\n".join(lines) + "\n"


def _shell(token: str) -> str:
    if not token or any(c.isspace() for c in token):
        return json.dumps(token)
    return token


def run_plan(plan: PocPlan, py: str = sys.executable) -> int:
    procs: List[subprocess.Popen] = []
    try:
        if plan.start_relay:
            procs.append(subprocess.Popen(plan.relay_cmd(py)))
            listen = "127.0.0.1" if plan.host in ("0.0.0.0", "::") else plan.host
            if not wait_for_port(listen, plan.port):
                # Binding 0.0.0.0: try localhost; if that fails, try the URL host.
                url_host = urlparse(plan.url).hostname or listen
                if not wait_for_port(url_host, plan.port):
                    raise SystemExit("relay did not start")
        for spec in plan.operators:
            procs.append(subprocess.Popen(plan.headset_cmd(spec, py)))
        watch = procs[1:] if plan.start_relay else procs
        if not watch:
            watch = procs
        while all(w.poll() is None for w in watch):
            time.sleep(0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    plan = build_plan(args)
    text = format_plan(plan)
    if args.dry_run or args.verbose:
        sys.stdout.write(text)
    if args.dry_run:
        return 0
    return run_plan(plan)


# re-export for tests that mock the socket
def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
