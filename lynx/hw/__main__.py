"""Head-tracker bench tool.

    python -m lynx.hw monitor  --port /dev/ttyUSB0
    python -m lynx.hw bench    --port /dev/ttyUSB0 --seconds 10
    python -m lynx.hw tare     --port /dev/ttyUSB0 --bearing 0 --cal imu.json
    python -m lynx.hw save-dcd --port /dev/ttyUSB0
    python -m lynx.hw report   --port /dev/ttyUSB0 --type grv --rate 100
    python -m lynx.hw cal      --port /dev/ttyUSB0 --sensors accel,gyro,mag --autosave on
    python -m lynx.hw hello    --port /dev/ttyUSB0

``--port mock://`` runs every command against the in-process mock device.
"""

from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
import time
from pathlib import Path
from typing import List, Optional

from .health import LinkState
from .orientation import MOUNT_PRESETS, ImuCalibration, TareError
from .protocol import AckResult, Board, ButtonEvent, CalSensors, Report, TareAxes, TareBasis
from .serial_link import DEFAULT_BAUD, ImuLink


def load_calibration(args: argparse.Namespace) -> ImuCalibration:
    cal = ImuCalibration()
    if args.cal and Path(args.cal).exists():
        cal = ImuCalibration.load(args.cal)
    if args.mount is not None:
        cal.mount = args.mount
    if args.declination is not None:
        cal.declination_deg = args.declination
    if args.convergence is not None:
        cal.convergence_deg = args.convergence
    return cal


def open_link(args: argparse.Namespace) -> ImuLink:
    link = ImuLink.open(args.port, load_calibration(args), baudrate=args.baud, nominal_rate_hz=args.rate_hint)
    return link.start()


def wait_for_samples(link: ImuLink, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if link.latest() is not None:
            return True
        time.sleep(0.02)
    return False


def check(res: AckResult, what: str) -> int:
    print(f"{what}: {res.name}")
    return 0 if res is AckResult.OK else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    link = open_link(args)
    try:
        next_print = 0.0
        while True:
            for ev in link.poll_events():
                aim = f" aim h{ev.aim.heading:6.1f} p{ev.aim.pitch:+5.1f}" if ev.aim else ""
                print(f"  rail {ev.event.name:<7} hold {ev.button.hold_ms:4d} ms{aim}")
            now = time.monotonic()
            if now >= next_print:
                next_print = now + 1.0 / args.hz
                p = link.latest()
                h = link.health()
                if p is None:
                    print(f"waiting for samples... {h.summary()}")
                else:
                    print(f"h {p.heading:6.1f}  p {p.pitch:+6.1f}  r {p.roll:+6.1f}   {h.summary()}")
            time.sleep(0.01)
    except KeyboardInterrupt:
        return 0
    finally:
        link.stop()


def cmd_hello(args: argparse.Namespace) -> int:
    link = open_link(args)
    try:
        res = link.hello_request()
        time.sleep(0.1)
        if link.hello is not None:
            hl = link.hello
            board = Board(hl.board).name if hl.board in Board._value2member_map_ else str(hl.board)
            print(f"fw {hl.fw_version} board {board} report {Report(hl.report).name} "
                  f"rate {hl.rate_hz} Hz imu_flags 0x{hl.imu_flags:02x} uptime {hl.uptime_ms / 1000:.1f}s")
        for line in link.logs:
            print(f"device log: {line}")
        return check(res, "hello")
    finally:
        link.stop()


def cmd_tare(args: argparse.Namespace) -> int:
    link = open_link(args)
    try:
        if not wait_for_samples(link):
            print("no IMU samples; is the BNO085 wired and streaming?", file=sys.stderr)
            return 1
        if args.device:
            basis = TareBasis.GAME_ROTATION_VECTOR if args.basis == "grv" else TareBasis.ROTATION_VECTOR
            if check(link.device_tare(TareAxes.Z, basis, persist=args.persist), "device tare") != 0:
                return 1
            time.sleep(0.3)  # let post-tare samples replace the history
        print(f"hold still facing bearing {args.bearing:.1f} deg ...")
        time.sleep(args.window)
        try:
            offset = link.tare(args.bearing, window_s=args.window)
        except TareError as exc:
            print(f"tare rejected: {exc}", file=sys.stderr)
            return 1
        print(f"heading offset {offset:+.2f} deg")
        if args.cal:
            link.converter.cal.save(args.cal)
            print(f"saved {args.cal}")
        return 0
    finally:
        link.stop()


def cmd_save_dcd(args: argparse.Namespace) -> int:
    link = open_link(args)
    try:
        return check(link.save_dcd(), "save DCD")
    finally:
        link.stop()


def cmd_report(args: argparse.Namespace) -> int:
    link = open_link(args)
    try:
        report = Report.GAME_ROTATION_VECTOR if args.type == "grv" else Report.ROTATION_VECTOR
        return check(link.set_report(report, args.rate), f"set report {report.name} @ {args.rate} Hz")
    finally:
        link.stop()


def cmd_cal(args: argparse.Namespace) -> int:
    sensors = CalSensors.NONE
    for name in filter(None, args.sensors.split(",")):
        sensors |= CalSensors[name.strip().upper()]
    link = open_link(args)
    try:
        return check(link.set_calibration(int(sensors), args.autosave == "on"),
                     f"calibration sensors={sensors!r} autosave={args.autosave}")
    finally:
        link.stop()


def cmd_bench(args: argparse.Namespace) -> int:
    """Automated part of docs/hardware/bench-test.md: link, rate, loss, still-noise, rail events."""
    link = open_link(args)
    failures: List[str] = []
    try:
        hello_res: Optional[AckResult]
        try:
            hello_res = link.hello_request()
        except TimeoutError:
            hello_res = None
        print(f"[1] HELLO/ACK: {hello_res.name if hello_res is not None else 'TIMEOUT'}"
              f"{'  fw ' + link.hello.fw_version if link.hello else ''}")
        if hello_res is not AckResult.OK:
            failures.append("no HELLO/ACK (check port, baud, firmware)")
        if not wait_for_samples(link):
            failures.append("no IMU samples (check I2C wiring, address, power)")
            print("[2] IMU samples: NONE")
            return report_failures(failures)
        print(f"[2] streaming; keep the sensor STILL for {args.seconds:.0f} s, then click/double/hold the rail switch")
        headings, pitches, rolls = [], [], []
        events: List[str] = []
        t_end = time.monotonic() + args.seconds
        while time.monotonic() < t_end:
            p = link.latest()
            if p is not None:
                headings.append(p.heading)
                pitches.append(p.pitch)
                rolls.append(p.roll)
            for ev in link.poll_events():
                if ev.event not in (ButtonEvent.PRESS, ButtonEvent.RELEASE):
                    events.append(ev.event.name)
                    print(f"    rail {ev.event.name} hold {ev.button.hold_ms} ms aim={'yes' if ev.aim else 'no'}")
            time.sleep(0.01)
        h = link.health()
        print(f"[3] {h.summary()}")
        print(f"    frames {h.frames} bad {h.bad_frames} seq gaps {h.seq_gaps} device tx dropped {h.device_tx_dropped}"
              f" resets {h.device_resets}")
        if h.rate_hz < 0.9 * link.health_monitor.nominal_rate_hz:
            failures.append(f"rate {h.rate_hz:.1f} Hz < 90% of nominal")
        if h.bad_frames or h.loss_fraction > 0.01:
            failures.append(f"frame errors: bad {h.bad_frames}, loss {100 * h.loss_fraction:.1f}%")
        if h.device_resets:
            failures.append(f"{h.device_resets} sensor resets during test (power/I2C integrity)")
        unwrapped = unwrap(headings)
        sd = [statistics.pstdev(v) if len(v) > 1 else math.nan for v in (unwrapped, pitches, rolls)]
        print(f"[4] still-noise std: heading {sd[0]:.3f}  pitch {sd[1]:.3f}  roll {sd[2]:.3f} deg")
        if any(s > args.max_noise for s in sd if not math.isnan(s)):
            failures.append(f"attitude noise above {args.max_noise} deg (sensor moved, vibration, or mag disturbance)")
        print(f"[5] rail gestures seen: {', '.join(events) if events else 'none'}")
        if args.require_gestures and not {"SINGLE", "DOUBLE", "LONG"} <= set(events):
            failures.append("did not see SINGLE, DOUBLE and LONG")
        if h.state is LinkState.DEGRADED:
            print(f"    note: link degraded: {'; '.join(h.reasons)}")
        return report_failures(failures)
    finally:
        link.stop()


def unwrap(deg: List[float]) -> List[float]:
    out: List[float] = []
    for d in deg:
        if out:
            d = out[-1] + ((d - out[-1] + 180.0) % 360.0 - 180.0)
        out.append(d)
    return out


def report_failures(failures: List[str]) -> int:
    if failures:
        print("BENCH FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("BENCH PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m lynx.hw", description="TeamLynx head-tracker bench tool")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--port", required=True, help="/dev/ttyUSB0, /dev/ttyACM0, COM5, or mock://")
        sp.add_argument("--baud", type=int, default=DEFAULT_BAUD)
        sp.add_argument("--cal", default=None, help="calibration JSON (loaded if present; tare saves to it)")
        sp.add_argument("--mount", default=None, help=f"3-letter axis spec or preset: {', '.join(MOUNT_PRESETS)}")
        sp.add_argument("--declination", type=float, default=None, help="magnetic declination, deg east +")
        sp.add_argument("--convergence", type=float, default=None, help="grid convergence, deg east +")
        sp.add_argument("--rate-hint", type=float, default=100.0, help="expected sample rate for health checks")

    sp = sub.add_parser("monitor", help="print attitude, health and rail events")
    common(sp)
    sp.add_argument("--hz", type=float, default=5.0, help="print rate")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("hello", help="query firmware version and state")
    common(sp)
    sp.set_defaults(func=cmd_hello)

    sp = sub.add_parser("tare", help="heading tare at the staging datum")
    common(sp)
    sp.add_argument("--bearing", type=float, required=True, help="true/grid bearing being faced, deg")
    sp.add_argument("--window", type=float, default=0.5, help="averaging window, s")
    sp.add_argument("--device", action="store_true", help="also run the BNO085's own Z-axis tare first")
    sp.add_argument("--basis", choices=["rv", "grv"], default="rv", help="device tare basis")
    sp.add_argument("--persist", action="store_true", help="persist the device tare to BNO085 flash")
    sp.set_defaults(func=cmd_tare)

    sp = sub.add_parser("save-dcd", help="save BNO085 dynamic calibration data to its flash")
    common(sp)
    sp.set_defaults(func=cmd_save_dcd)

    sp = sub.add_parser("report", help="select rotation vector (rv) or game rotation vector (grv)")
    common(sp)
    sp.add_argument("--type", choices=["rv", "grv"], required=True)
    sp.add_argument("--rate", type=int, default=100)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("cal", help="configure BNO085 dynamic calibration")
    common(sp)
    sp.add_argument("--sensors", default="accel,gyro,mag", help="comma list of accel,gyro,mag (empty = none)")
    sp.add_argument("--autosave", choices=["on", "off"], default="on")
    sp.set_defaults(func=cmd_cal)

    sp = sub.add_parser("bench", help="automated bench test (see docs/hardware/bench-test.md)")
    common(sp)
    sp.add_argument("--seconds", type=float, default=10.0)
    sp.add_argument("--max-noise", type=float, default=0.5, help="max still std per axis, deg")
    sp.add_argument("--require-gestures", action="store_true", help="fail unless SINGLE, DOUBLE, LONG are seen")
    sp.set_defaults(func=cmd_bench)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
