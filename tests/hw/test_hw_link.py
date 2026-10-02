"""Mock device -> ImuLink -> health / rail events -> ping path (sim hook and headset pose source)."""

import asyncio
import math
import os

import numpy as np
import pytest

from lynx.headset.pose import PoseSource, create_pose_source
from lynx.hw import (
    AckResult,
    ButtonEvent,
    ImuCalibration,
    ImuHeadSource,
    ImuLink,
    LinkState,
    MockImuDevice,
    OrientationConverter,
    RailAction,
    Report,
    SerialImuPoseSource,
    SerialPoseSample,
    rail_commands,
    raycast_ping,
)
from lynx.hw.__main__ import main as hw_main
from lynx.hw.protocol import CmdSetReport, CmdTare, encode_frame
from lynx.net.schema import PingType, TelemetryFlags
from lynx.spatial import Pose


class ManualClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def turning_head(t: float):
    """Heading 30 / pitch -10 until t = 1 s, then the operator snaps round to 120."""
    return (30.0, -10.0, 0.0) if t < 1.0 else (120.0, 0.0, 0.0)


def make(pose_fn=turning_head, cal=None, **kw):
    clock = ManualClock()
    cal = cal or ImuCalibration()
    dev = MockImuDevice(pose_fn=pose_fn, cal=cal, clock=clock, realtime=False, **kw)
    link = ImuLink(dev, OrientationConverter(ImuCalibration(**vars(cal))), clock=clock)
    return clock, dev, link


def run_for(clock, dev, link, seconds, step=0.01):
    for _ in range(int(round(seconds / step))):
        clock.t += step
        link.feed(dev.read(65536))


def test_link_recovers_true_attitude_through_mount_and_declination():
    cal = ImuCalibration(mount="left-side", declination_deg=-6.0)
    clock, dev, link = make(cal=cal)
    run_for(clock, dev, link, 0.5)
    p = link.latest()
    assert (p.heading, p.pitch, p.roll) == pytest.approx((30.0, -10.0, 0.0), abs=1e-4)
    assert link.hello is not None and link.hello.fw_version == "1.0.0"
    h = link.health()
    assert h.state is LinkState.OK and h.rate_hz == pytest.approx(100, rel=0.05)


def test_commands_are_acked_without_reader_thread():
    clock, dev, link = make()
    assert link.set_report(Report.GAME_ROTATION_VECTOR, 50) is AckResult.OK
    assert dev.report is Report.GAME_ROTATION_VECTOR and dev.rate_hz == 50
    assert link.health_monitor.nominal_rate_hz == 50
    assert link.command(CmdSetReport(report=1, rate_hz=1000)) is AckResult.BAD_ARG
    assert link.device_tare(persist=True) is AckResult.OK
    assert [type(c).__name__ for c in dev.commands][-2:] == ["CmdTare", "CmdTarePersist"]
    dev.fail_commands.add(0x85)
    assert link.save_dcd() is AckResult.SENSOR_ERROR


def test_command_timeout():
    class Silent:
        timeout = 0.01

        def read(self, n=1):
            return b""

        def write(self, data):
            return len(data)

        def close(self):
            pass

    link = ImuLink(Silent())
    with pytest.raises(TimeoutError):
        link.command(CmdTare(), timeout=0.05)


def test_health_states():
    clock, dev, link = make(pose_fn=lambda t: (0.0, 0.0, 0.0))
    assert link.health().state is LinkState.NO_DATA
    run_for(clock, dev, link, 0.3)
    assert link.health().ok
    clock.t += 1.0  # nothing read for a second
    assert link.health().state is LinkState.STALE
    dev.cal_status = 1
    run_for(clock, dev, link, 0.2)
    h = link.health()
    assert h.state is LinkState.DEGRADED and any("calibration" in r for r in h.reasons)
    assert h.usable and h.degraded


def test_health_game_rotation_vector_needs_tare_and_counts_loss():
    clock, dev, link = make(pose_fn=lambda t: (0.0, 0.0, 0.0), report=Report.GAME_ROTATION_VECTOR)
    run_for(clock, dev, link, 0.6)
    h = link.health()
    assert h.state is LinkState.DEGRADED and "not tared" in h.reasons[0]
    assert math.isnan(h.accuracy_deg)
    link.tare(0.0)
    run_for(clock, dev, link, 0.3)
    assert link.health().ok
    # Drop ~20% of frames on the floor: sequence gaps show up as loss.
    for _ in range(50):
        clock.t += 0.01
        data = dev.read(65536)
        if _ % 5:
            link.feed(data)
    h = link.health()
    assert h.seq_gaps >= 9 and h.loss_fraction > 0.05 and h.state is LinkState.DEGRADED


def test_garbage_on_the_line_costs_nothing():
    clock, dev, link = make()
    run_for(clock, dev, link, 0.1)
    dev.inject_bytes(b"BNO08x - Error decoding sensor event\r\n")
    run_for(clock, dev, link, 0.1)
    h = link.health()
    assert h.bad_frames == 1 and h.seq_gaps == 0 and h.ok


def test_single_click_pings_where_operator_aimed_at_press_time():
    clock, dev, link = make()
    run_for(clock, dev, link, 0.95)
    press = dev.now_us()  # operator presses while facing 030, pitched down 10
    run_for(clock, dev, link, 0.35)  # head swings to 120 before SINGLE is classified
    dev.gesture(ButtonEvent.SINGLE, press_t_us=press)
    run_for(clock, dev, link, 0.02)
    assert link.latest().heading == pytest.approx(120.0)
    src = ImuHeadSource(link)
    pose, cmds = src.poll()
    assert [c.action for c in cmds] == [RailAction.PING]
    assert cmds[0].aim.heading == pytest.approx(30.0) and cmds[0].aim.pitch == pytest.approx(-10.0)
    hit = raycast_ping(cmds[0], (5.0, -2.0, 1.7))
    d = 1.7 / math.tan(math.radians(10))
    assert hit.point == pytest.approx([5 + d * math.sin(math.radians(30)), -2 + d * math.cos(math.radians(30)), 0], abs=1e-3)
    assert cmds[0].ping_type(PingType.RALLY) is PingType.RALLY


def test_gesture_map_and_held_flag():
    clock, dev, link = make(pose_fn=lambda t: (0.0, 0.0, 0.0))
    run_for(clock, dev, link, 0.2)
    src = ImuHeadSource(link)
    dev.gesture(ButtonEvent.DOUBLE)
    dev.gesture(ButtonEvent.LONG)
    run_for(clock, dev, link, 0.01)
    _, cmds = src.poll()
    assert [c.action for c in cmds] == [RailAction.PING_CONTACT, RailAction.CANCEL_LAST]
    assert cmds[0].ping_type(PingType.MARK) is PingType.CONTACT
    assert src.telemetry_flags() == 0
    # A press with no release yet -> switch held.
    from lynx.hw.protocol import Button

    dev.inject_bytes(encode_frame(Button(t_us=dev.now_us(), press_t_us=dev.now_us(), event=ButtonEvent.PRESS, seq=0)))
    link.feed(dev.read(65536))
    src.poll()
    assert src.telemetry_flags() & TelemetryFlags.PING_SWITCH
    assert rail_commands([], {}) == []


def test_unaimed_event_falls_back_to_latest():
    clock, dev, link = make(pose_fn=lambda t: (0.0, -10.0, 0.0))
    run_for(clock, dev, link, 5.5)
    dev.gesture(ButtonEvent.SINGLE, press_t_us=100_000)  # older than the pose history
    run_for(clock, dev, link, 0.01)
    (ev,) = [e for e in link.poll_events() if e.event is ButtonEvent.SINGLE]
    assert ev.aim is None
    (cmd,) = rail_commands([ev])
    assert raycast_ping(cmd, (0, 0, 1.7), fallback=link.latest()).distance == pytest.approx(1.7 / math.sin(math.radians(10)))


def test_threaded_link_with_async_events():
    async def scenario():
        dev = MockImuDevice(pose_fn=lambda t: (90.0, 0.0, 0.0), timeout=0.01)
        link = ImuLink(dev).start()
        try:
            stream = link.aevents()
            first = asyncio.ensure_future(stream.__anext__())
            await asyncio.sleep(0.15)
            assert link.latest() is not None and link.latest().heading == pytest.approx(90.0, abs=1e-4)
            assert await link.acommand(CmdSetReport(report=1, rate_hz=100)) is AckResult.OK
            dev.gesture(ButtonEvent.SINGLE)
            ev = await asyncio.wait_for(first, 2.0)
            assert ev.event is ButtonEvent.PRESS
            await stream.aclose()
        finally:
            link.stop()

    asyncio.run(scenario())


# --- headset integration (lynx-headset --pose serial:PORT) -----------------------------------


START = Pose.from_euler(3.0, 4.0, 1.7, 0.0, 0.0, 0.0)


def test_lynx_hw_registers_serial_pose_source():
    src = create_pose_source("serial:mock://still", START)
    try:
        assert isinstance(src, SerialImuPoseSource) and isinstance(src, PoseSource)
        assert src.name == "serial" and src.port == "mock://still"
    finally:
        src.close()


def test_serial_pose_source_missing_port_is_a_clean_error():
    from lynx.headset.pose import PoseSourceUnavailableError

    with pytest.raises(PoseSourceUnavailableError, match="cannot open head tracker"):
        create_pose_source("serial:/dev/does-not-exist-lynx", START)


def test_serial_pose_source_spec_options(tmp_path):
    cal_path = tmp_path / "imu.json"
    ImuCalibration(mount="left-side", heading_offset_deg=5.0, tared=True).save(cal_path)
    src = create_pose_source(f"serial:mock://still?cal={cal_path}&declination=2.5", START)
    try:
        assert src.link.converter.cal.mount == "left-side"
        assert src.link.converter.cal.heading_correction_deg() == pytest.approx(7.5)
    finally:
        src.close()
    with pytest.raises(ValueError, match="unknown"):
        SerialImuPoseSource("mock://?bogus=1", START)


def test_serial_pose_source_routes_rail_to_headset_ping_path():
    clock, dev, link = make()
    src = SerialImuPoseSource("mock://", START, link=link)
    run_for(clock, dev, link, 0.95)
    s = src.read(0.0)
    assert isinstance(s, SerialPoseSample) and not s.trigger
    assert s.pose.position == pytest.approx((3.0, 4.0, 1.7))  # orientation-only: keeps the datum position
    assert s.pose.euler == pytest.approx((30.0, -10.0, 0.0), abs=1e-4)
    press = dev.now_us()
    run_for(clock, dev, link, 0.35)
    dev.gesture(ButtonEvent.SINGLE, press_t_us=press)
    run_for(clock, dev, link, 0.01)
    s = src.read(0.0)
    # Live pose for the HUD; the press-time attitude rides on the rail command.
    assert s.trigger and s.pose.euler[0] == pytest.approx(120.0)
    assert [c.action for c in s.rail] == [RailAction.PING]
    assert s.rail[0].aim.heading == pytest.approx(30.0) and s.rail[0].aim.pitch == pytest.approx(-10.0)
    s = src.read(0.0)
    assert not s.trigger and s.pose.euler[0] == pytest.approx(120.0)
    dev.gesture(ButtonEvent.DOUBLE)
    dev.gesture(ButtonEvent.LONG)
    run_for(clock, dev, link, 0.01)
    s = src.read(0.0)
    assert s.trigger  # DOUBLE triggers; LONG is only visible via .rail
    assert [c.action for c in s.rail] == [RailAction.PING_CONTACT, RailAction.CANCEL_LAST]
    dev.cal_status = 0
    run_for(clock, dev, link, 0.05)
    assert src.read(0.0).flags & TelemetryFlags.IMU_DEGRADED
    src.close()


# --- sim hook (lynx-sim --imu-port) ------------------------------------------------------------


def test_sim_imu_hook_drives_head_and_ping_path():
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pytest.importorskip("pygame")
    from lynx.sim.app import SimApp, build_parser

    app = SimApp(build_parser().parse_args(["--imu-port", "mock://", "--x", "5", "--y", "-2"]))
    clock, dev, link = make()
    app.imu = ImuHeadSource(link)

    sent = []

    class FakeNet:
        def send_ping(self, x, y, z, ping_type, ttl):
            sent.append((x, y, z, ping_type))

        def cancel_ping(self, ping_id):
            sent.append(("cancel", ping_id))

    app.net = FakeNet()
    run_for(clock, dev, link, 0.95)
    press = dev.now_us()
    run_for(clock, dev, link, 0.35)
    dev.gesture(ButtonEvent.SINGLE, press_t_us=press)
    run_for(clock, dev, link, 0.01)
    app.poll_imu()
    assert app.operator.heading == pytest.approx(120.0)
    (x, y, z, kind), = sent
    d = 1.7 / math.tan(math.radians(10))
    assert (x, y, z) == pytest.approx((5 + d * math.sin(math.radians(30)), -2 + d * math.cos(math.radians(30)), 0), abs=1e-3)
    assert kind is PingType.MARK
    dev.gesture(ButtonEvent.DOUBLE)
    run_for(clock, dev, link, 0.01)
    app.poll_imu()
    assert sent[-1][3] is PingType.CONTACT


# --- CLI -----------------------------------------------------------------------------------------


def test_cli_bench_hello_and_tare(tmp_path, capsys):
    assert hw_main(["hello", "--port", "mock://still"]) == 0
    assert hw_main(["bench", "--port", "mock://still", "--seconds", "0.5"]) == 0
    assert "BENCH PASS" in capsys.readouterr().out
    cal = tmp_path / "imu.json"
    assert hw_main(["tare", "--port", "mock://still", "--bearing", "45", "--window", "0.3", "--cal", str(cal)]) == 0
    saved = ImuCalibration.load(cal)
    assert saved.tared and saved.heading_offset_deg == pytest.approx(45.0, abs=1e-3)
