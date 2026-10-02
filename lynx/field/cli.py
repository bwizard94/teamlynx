"""``lynx-field``: Phase 4 squad deployment tools.

    lynx-field relay      [--role primary|secondary] [--upstream ws://relay.lynx:8765] [--log DIR]
    lynx-field discover   list relays answering on the squad LAN
    lynx-field loadtest   [--spawn | --url URL] [--nodes 10] [--rate 20] [--duration 10]
    lynx-field bandwidth  [--nodes 10] [--rate 20]          analytic bytes + 802.11 airtime table
    lynx-field netconfig  squad.json --out build/net         OpenWrt setup script + node profiles
    lynx-field survey     --gnss /dev/lynx-gnss --site site.json --write     datum from GNSS average
    lynx-field calibrate  --site site.json --imu /dev/lynx-imu --station S1 --marker FLAG-N [--check FLAG-N]
    lynx-field headset    --profile /etc/lynx/node.json [--dry-run]
    lynx-field record     [--url URL] --out /var/log/lynx    passive session logger (observer)
    lynx-field replay     LOGS... --site site.json --out aar.png [--video aar.mp4]

Docs: docs/field/net-*.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import select
import sys
from typing import Dict, List, Optional, Sequence


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ------------------------------------------------------------------------------------- relay


def cmd_relay(args: argparse.Namespace) -> int:
    from lynx.net.server import RelayConfig

    from .relay import FieldRelayConfig, run_field_relay

    cfg = RelayConfig(host=args.host, port=args.port, stale_timeout_s=args.stale_timeout,
                      max_ttl_ms=int(args.max_ttl * 1000))
    fcfg = FieldRelayConfig(tick_s=args.tick_ms / 1000.0, cork=not args.no_cork,
                            dscp=None if args.no_dscp else 46, role=args.role, priority=args.priority,
                            squad=args.squad, relay_id=args.id, beacon=not args.no_beacon,
                            beacon_bind=("0.0.0.0", args.beacon_port), mdns=args.mdns, upstream=args.upstream,
                            log_path=args.log, log_telemetry_hz=args.log_hz, log_compress=args.log_gz)
    if fcfg.role == "secondary" and fcfg.priority == 0:
        fcfg.priority = 10
    try:
        asyncio.run(run_field_relay(cfg, fcfg, args.stats_interval))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery import discover_beacons, mdns_browse, resolve_relay_urls

    found = discover_beacons(args.timeout, args.squad)
    if not args.no_mdns:
        found += mdns_browse(args.timeout)
    if args.json:
        print(json.dumps([{**a.__dict__, "url": a.url} for a in found], indent=2))
        return 0 if found else 1
    for a in found:
        print(f"{a.url:28} {a.role:9} prio {a.priority:<3} squad {a.squad or '-':8} id {a.relay_id or '-':16} "
              f"clients {a.clients} ({a.source})")
    if not found:
        fallback = resolve_relay_urls(beacon=False, mdns=False)
        print("no relay answered; candidates from DNS / gateway:", " ".join(fallback))
        return 1
    return 0


# ------------------------------------------------------------------------------------- load / bandwidth


def cmd_loadtest(args: argparse.Namespace) -> int:
    from .loadtest import LoadTestConfig, reports_json, run_loadtest, run_spawned

    cfg = LoadTestConfig(url=args.url, nodes=args.nodes, rate_hz=args.rate, duration_s=args.duration,
                         warmup_s=args.warmup, ping_interval_s=args.ping_interval, encoding=args.encoding)
    reports = []
    if args.spawn:
        for tick in args.tick_ms or [25.0]:
            r = run_spawned(cfg, tick)
            print(r.format())
            reports.append(r)
    else:
        r = asyncio.run(run_loadtest(cfg))
        print(r.format())
        reports.append(r)
    if args.json:
        with open(args.json, "w") as fh:
            fh.write(reports_json(reports) + "\n")
    bad = [r for r in reports if r.ping_loss > 0 or r.errors]
    return 1 if bad else 0


def cmd_bandwidth(args: argparse.Namespace) -> int:
    from .bandwidth import budget_table, format_table

    rows = budget_table(args.nodes, args.rate, ticks=[None if t <= 0 else 1000.0 / t for t in args.tick_ms])
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2))
    else:
        print(f"{args.nodes} nodes x {args.rate:g} Hz telemetry (binary 56 B), AC_VI, no retries:")
        print(format_table(rows))
    return 0


# ------------------------------------------------------------------------------------- network config


def cmd_netconfig(args: argparse.Namespace) -> int:
    from .netconfig import Roster, RosterError, write

    try:
        roster = Roster.load(args.roster)
    except (RosterError, ValueError, OSError) as exc:
        print(f"roster error: {exc}", file=sys.stderr)
        return 2
    for p in write(roster, args.out):
        print(p)
    return 0


# ------------------------------------------------------------------------------------- GNSS / calibration


def cmd_survey(args: argparse.Namespace) -> int:
    from .geodesy import LocalFrame
    from .gnss import GnssReader, average_fixes, collect_fixes
    from .site import Datum, Site

    reader = GnssReader.open(args.gnss, args.baud).start()
    try:
        print(f"averaging GNSS for {args.seconds:.0f} s - keep the antenna still over the datum stake")
        fixes = collect_fixes(reader, args.seconds)
    finally:
        reader.stop()
    if not fixes:
        print("no fixes received", file=sys.stderr)
        return 1
    f0 = fixes[0]
    avg = average_fixes(fixes, LocalFrame(f0.lat, f0.lon, f0.h))
    print(f"datum lat {avg.lat:.8f} lon {avg.lon:.8f} h {avg.h:.2f} m  +-{avg.std_h:.2f} m (1 sigma, "
          f"{avg.samples} fixes, {avg.rejected} rejected, scatter {avg.scatter_e:.2f}/{avg.scatter_n:.2f} m)")
    if args.write:
        site = Site.load(args.site) if args.site else Site(markers={})
        site.datum = Datum(avg.lat, avg.lon, avg.h)
        site.save(args.site)
        print(f"wrote datum to {args.site}")
    return 0


def _stdin_enter() -> bool:
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return False
    if r:
        sys.stdin.readline()
        return True
    return False


def cmd_calibrate(args: argparse.Namespace) -> int:
    from lynx.hw import ImuCalibration, ImuLink

    from .calibrate import CalibrationError, FieldCalibration
    from .gnss import GnssReader
    from .site import Site, SiteError
    from .staging import PromptDisplay, StagingCalibrator, StagingOptions, save_calibration

    try:
        site = Site.load(args.site)
    except (SiteError, OSError) as exc:
        print(f"site error: {exc}", file=sys.stderr)
        return 2
    imu_cal = ImuCalibration.load(args.cal) if args.cal else ImuCalibration()
    imu_cal.declination_deg = site.declination_deg
    imu_cal.convergence_deg = site.convergence_deg
    if args.mount:
        imu_cal.mount = args.mount
    existing = FieldCalibration.load(args.out) if args.check else None
    if existing is not None:
        imu_cal = existing.imu
    else:
        imu_cal.heading_offset_deg, imu_cal.tared = 0.0, False
    link = ImuLink.open(args.imu, imu_cal).start()
    gnss = GnssReader.open(args.gnss).start() if args.gnss else None
    display = PromptDisplay() if args.display else None
    markers = args.marker or ([args.check] if args.check else sorted(site.markers)[:1])
    opts = StagingOptions(node=args.node, markers=markers, station=args.station, eye_height=args.eye_height,
                          gnss_seconds=args.gnss_seconds, keyboard=args.keyboard)
    cal_ui = StagingCalibrator(site, link, opts, gnss=gnss, display=display,
                               read_key=_stdin_enter if args.keyboard else None)
    if args.keyboard:
        print("keyboard mode: press Enter instead of tapping the rail switch")
    try:
        if existing is not None:
            cal_ui.check(existing, args.check)
            save_calibration(existing, args.out)
        else:
            cal = cal_ui.run()
            fc, imu = save_calibration(cal, args.out)
            print(f"saved {fc} and {imu}")
    except CalibrationError as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        link.stop()
        if gnss is not None:
            gnss.stop()
        if display is not None:
            display.close()
    return 0


# ------------------------------------------------------------------------------------- headset / logs


def cmd_headset(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .launcher import NodeProfile, plan, run_headset

    profile = NodeProfile.load(args.profile)
    urls = args.url or None
    if args.dry_run:
        print(json.dumps(asdict(plan(profile, urls)), indent=2))
        return 0
    return run_headset(profile, urls, headless=args.headless, frames=args.frames, extra_argv=args.extra)


def cmd_record(args: argparse.Namespace) -> int:
    from .discovery import resolve_relay_urls
    from .failover import FailoverClient
    from .session import SessionRecorder

    urls = resolve_relay_urls(args.url or (), squad=args.squad, mdns=False) or ["ws://relay.lynx:8765"]
    rec = SessionRecorder(args.out, "observer", telemetry_hz=args.hz, compress=args.gz, meta={"urls": urls})
    client = FailoverClient(urls, 0xFFFF, "OBSERVER", on_message=rec.record_message)
    client.listeners.append(lambda ev: rec.record_event(ev.kind, url=ev.url, detail=ev.detail))
    print(f"recording {' '.join(urls)} -> {rec.path}")

    async def main() -> None:
        task = asyncio.create_task(client.run())
        try:
            await task
        finally:
            await client.close(leave=False)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        rec.close()
    return 0


def _offsets(values: Sequence[str]) -> Dict[str, float]:
    out = {}
    for v in values:
        src, _, sec = v.rpartition("=")
        out[src] = float(sec)
    return out


def cmd_replay(args: argparse.Namespace) -> int:
    from .replay import replay, summarize

    sess = replay(args.logs, args.out, args.site, args.video, args.speed, _offsets(args.offset or []))
    print(summarize(sess))
    for p in (args.out, args.video):
        if p:
            print(f"wrote {p}")
    return 0


# ------------------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lynx-field", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("relay", help="field relay (batched downlink, beacon, logging, bridge)")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--role", choices=("primary", "secondary"), default="primary")
    sp.add_argument("--priority", type=int, default=0, help="beacon priority, lower first (secondary default 10)")
    sp.add_argument("--squad", default="LYNX")
    sp.add_argument("--id", default="", help="relay id in beacons (default hostname)")
    sp.add_argument("--upstream", default=None, help="bridge to this primary relay URL (secondary role)")
    sp.add_argument("--log", default=None, help="session log file or directory")
    sp.add_argument("--log-hz", type=float, default=5.0, help="telemetry log rate per node")
    sp.add_argument("--log-gz", action="store_true", help="gzip the session log")
    sp.add_argument("--tick-ms", type=float, default=25.0, help="downlink telemetry tick (0 = per frame)")
    sp.add_argument("--no-cork", action="store_true")
    sp.add_argument("--no-dscp", action="store_true")
    sp.add_argument("--no-beacon", action="store_true")
    sp.add_argument("--beacon-port", type=int, default=8766)
    sp.add_argument("--mdns", action="store_true", help="register _lynx-relay._tcp via python-zeroconf")
    sp.add_argument("--stale-timeout", type=float, default=5.0)
    sp.add_argument("--max-ttl", type=float, default=600.0)
    sp.add_argument("--stats-interval", type=float, default=10.0)
    sp.set_defaults(func=cmd_relay)

    sp = sub.add_parser("discover", help="list relays on the LAN")
    sp.add_argument("--timeout", type=float, default=1.5)
    sp.add_argument("--squad", default="")
    sp.add_argument("--no-mdns", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("loadtest", help="simulate N headsets against a relay")
    sp.add_argument("--url", default="ws://127.0.0.1:8765")
    sp.add_argument("--spawn", action="store_true", help="start a local relay subprocess per run")
    sp.add_argument("--tick-ms", type=float, action="append", help="relay tick(s) to compare with --spawn")
    sp.add_argument("--nodes", type=int, default=10)
    sp.add_argument("--rate", type=float, default=20.0)
    sp.add_argument("--duration", type=float, default=10.0)
    sp.add_argument("--warmup", type=float, default=1.0)
    sp.add_argument("--ping-interval", type=float, default=3.0)
    sp.add_argument("--encoding", choices=("binary", "json"), default="binary")
    sp.add_argument("--json", default=None, help="write the report(s) as JSON")
    sp.set_defaults(func=cmd_loadtest)

    sp = sub.add_parser("bandwidth", help="analytic bandwidth / airtime table")
    sp.add_argument("--nodes", type=int, default=10)
    sp.add_argument("--rate", type=float, default=20.0)
    sp.add_argument("--tick-ms", type=float, nargs="+", default=[0.0, 25.0, 50.0], help="0 = per-frame downlink")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_bandwidth)

    sp = sub.add_parser("netconfig", help="OpenWrt router script + node profiles from a roster")
    sp.add_argument("roster")
    sp.add_argument("--out", default="build/net")
    sp.set_defaults(func=cmd_netconfig)

    sp = sub.add_parser("survey", help="average GNSS over the datum stake")
    sp.add_argument("--gnss", required=True)
    sp.add_argument("--baud", type=int, default=38400)
    sp.add_argument("--seconds", type=float, default=120.0)
    sp.add_argument("--site", default=None)
    sp.add_argument("--write", action="store_true", help="store the datum in --site")
    sp.set_defaults(func=cmd_survey)

    sp = sub.add_parser("calibrate", help="staging-area datum calibration (rail-switch taps on markers)")
    sp.add_argument("--site", required=True)
    sp.add_argument("--imu", required=True, help="head tracker port (/dev/lynx-imu, mock://, mock://still)")
    sp.add_argument("--cal", default=None, help="existing imu.json (mount, trim); tare is replaced")
    sp.add_argument("--mount", default=None)
    sp.add_argument("--out", default="/var/lib/lynx", help="calibration directory")
    sp.add_argument("--node", type=int, default=1)
    sp.add_argument("--station", default=None)
    sp.add_argument("--marker", action="append", help="marker(s) to sight, in order")
    sp.add_argument("--gnss", default=None, help="GNSS port: position from a GNSS average instead of a station")
    sp.add_argument("--gnss-seconds", type=float, default=30.0)
    sp.add_argument("--eye-height", type=float, default=None)
    sp.add_argument("--check", default=None, metavar="MARKER", help="drift check against the saved calibration")
    sp.add_argument("--keyboard", action="store_true", help="Enter instead of the rail switch (bench)")
    sp.add_argument("--display", action="store_true", help="show prompts on the helmet display")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("headset", help="field launcher for one headset")
    sp.add_argument("--profile", default="/etc/lynx/node.json")
    sp.add_argument("--url", action="append", help="relay URL override (repeatable, primary first)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--frames", type=int, default=0)
    sp.add_argument("extra", nargs=argparse.REMAINDER, help="-- extra lynx-headset arguments")
    sp.set_defaults(func=cmd_headset)

    sp = sub.add_parser("record", help="passive session logger connected to the relay")
    sp.add_argument("--url", action="append")
    sp.add_argument("--squad", default="")
    sp.add_argument("--out", default="/var/log/lynx")
    sp.add_argument("--hz", type=float, default=5.0)
    sp.add_argument("--gz", action="store_true")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("replay", help="after-action review from session logs")
    sp.add_argument("logs", nargs="+")
    sp.add_argument("--site", default=None)
    sp.add_argument("--out", default="aar.png")
    sp.add_argument("--video", default=None)
    sp.add_argument("--speed", type=float, default=10.0)
    sp.add_argument("--offset", action="append", metavar="SRC=SECONDS", help="clock offset for a log source")
    sp.set_defaults(func=cmd_replay)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "extra", None) and args.extra[:1] == ["--"]:
        args.extra = args.extra[1:]
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
