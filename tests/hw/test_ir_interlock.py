"""IR illuminator interlock: conditions, trips, 120 s lockout, fail-safe paths, backends, headset hook."""

import os
import signal
import sys
import types

import pytest

from lynx.hw.ir_interlock import (
    IR_EN_PIN,
    IrInterlock,
    IrInterlockConfig,
    IrState,
    JetsonGpio,
    MockGpio,
    force_off,
    main as ir_main,
    open_gpio,
)


class ManualClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


DEPLOYED = dict(edge_mode=True, pitch_deg=-5.0, roll_deg=2.0, imu_ok=True)


@pytest.fixture
def rig():
    clock = ManualClock()
    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=clock, watchdog=False).start()
    yield ir, gpio, clock
    ir.close()


def test_starts_low_and_stays_low_until_armed(rig):
    ir, gpio, _ = rig
    assert gpio.configured == {IR_EN_PIN} and gpio.level() is False
    st = ir.update(**DEPLOYED)
    assert st.state is IrState.SAFE and not gpio.level()
    assert ir.arm()
    st = ir.update(**DEPLOYED)
    assert st.state is IrState.ON and gpio.level() is True


@pytest.mark.parametrize("override", [{"edge_mode": False}])
def test_edge_mode_off_inhibits_without_tripping(rig, override):
    ir, gpio, _ = rig
    ir.arm()
    ir.update(**DEPLOYED)
    st = ir.update(**{**DEPLOYED, **override})
    assert st.state is IrState.INHIBITED and st.armed and not gpio.level()
    assert ir.update(**DEPLOYED).state is IrState.ON  # back on without re-arming


@pytest.mark.parametrize("override, why", [
    ({"pitch_deg": 75.0}, "pitch"),          # flipped up to stow
    ({"pitch_deg": -60.0}, "pitch"),         # hanging / helmet face-down
    ({"roll_deg": 95.0}, "roll"),            # helmet on its side
    ({"pitch_deg": None}, "attitude unknown"),
    ({"roll_deg": float("nan")}, "attitude unknown"),
    ({"imu_ok": False}, "IMU"),
])
def test_trip_conditions_drive_low_disarm_and_lock_out(rig, override, why):
    ir, gpio, clock = rig
    ir.arm()
    assert ir.update(**DEPLOYED).output
    st = ir.update(**{**DEPLOYED, **override})
    assert not gpio.level() and not st.output and not st.armed
    assert st.state is IrState.LOCKOUT and why in st.last_trip
    assert st.lockout_remaining_s == pytest.approx(120.0)
    # conditions back to normal: still off, and arming is refused for 120 s
    assert ir.update(**DEPLOYED).state is IrState.LOCKOUT and not gpio.level()
    clock.t += 119.9
    assert not ir.arm()
    clock.t += 0.2
    assert ir.update(**DEPLOYED).state is IrState.SAFE  # lockout over, but no automatic re-arm
    assert not gpio.level()
    assert ir.arm()
    assert ir.update(**DEPLOYED).state is IrState.ON and gpio.level()


def test_bad_conditions_while_disarmed_do_not_lock_out(rig):
    ir, gpio, _ = rig
    st = ir.update(**{**DEPLOYED, "pitch_deg": 80.0})
    assert st.state is IrState.SAFE and "stowed" in st.reasons[0]
    assert ir.arm() and ir.update(**DEPLOYED).output


def test_window_limits_are_inclusive_and_configurable():
    clock, gpio = ManualClock(), MockGpio()
    cfg = IrInterlockConfig(pitch_min_deg=-30, pitch_max_deg=10, roll_max_deg=20, rearm_after_trip_s=5)
    with IrInterlock(gpio, cfg, clock=clock, watchdog=False) as ir:
        ir.arm()
        assert ir.update(edge_mode=True, pitch_deg=10.0, roll_deg=-20.0, imu_ok=True).output
        assert not ir.update(edge_mode=True, pitch_deg=10.5, roll_deg=0.0, imu_ok=True).output
        clock.t += 5.01
        assert ir.arm()


def test_disarm_and_toggle(rig):
    ir, gpio, _ = rig
    assert ir.toggle_arm() is True
    ir.update(**DEPLOYED)
    assert gpio.level()
    assert ir.toggle_arm() is False
    assert not gpio.level()  # disarm drives low immediately, not on the next frame
    assert ir.update(**DEPLOYED).state is IrState.SAFE


def test_gpio_write_failure_is_a_trip():
    clock, gpio = ManualClock(), MockGpio()
    with IrInterlock(gpio, clock=clock, watchdog=False) as ir:
        ir.arm()
        gpio.fail_writes = True
        st = ir.update(**DEPLOYED)
        assert not st.output and st.state is IrState.LOCKOUT and "error" in st.last_trip
        assert not gpio.level()


def test_internal_exception_is_a_trip(rig, monkeypatch):
    ir, gpio, _ = rig
    ir.arm()
    ir.update(**DEPLOYED)
    import lynx.hw.ir_interlock as mod

    def boom(*a, **k):
        raise ZeroDivisionError("bad input")

    monkeypatch.setattr(mod, "attitude_reasons", boom)
    st = ir.update(**DEPLOYED)  # must not raise
    assert not gpio.level() and st.state is IrState.LOCKOUT and "ZeroDivisionError" in st.last_trip


def test_watchdog_forces_low_when_updates_stop(rig):
    ir, gpio, clock = rig
    ir.arm()
    ir.update(**DEPLOYED)
    clock.t += 0.4
    assert not ir.check_watchdog() and gpio.level()
    clock.t += 0.2
    assert ir.check_watchdog()
    assert not gpio.level() and ir.status.state is IrState.LOCKOUT and "no update" in ir.status.last_trip


def test_watchdog_thread_runs_on_the_real_clock():
    import time

    gpio = MockGpio()
    with IrInterlock(gpio, IrInterlockConfig(watchdog_s=0.1)) as ir:
        ir.arm()
        ir.update(**DEPLOYED)
        assert gpio.level()
        deadline = time.monotonic() + 2.0
        while gpio.level() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not gpio.level()


def test_close_drives_low_releases_and_refuses_arm():
    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=ManualClock(), watchdog=False).start()
    ir.arm()
    ir.update(**DEPLOYED)
    ir.close()
    assert not gpio.level() and IR_EN_PIN in gpio.released
    assert ir.status.state is IrState.CLOSED
    assert not ir.arm()
    assert not ir.update(**DEPLOYED).output
    ir.close()  # idempotent


def test_context_manager_drives_low_on_exception():
    gpio = MockGpio()
    with pytest.raises(RuntimeError):
        with IrInterlock(gpio, clock=ManualClock(), watchdog=False) as ir:
            ir.arm()
            ir.update(**DEPLOYED)
            assert gpio.level()
            raise RuntimeError("frame loop crashed")
    assert not gpio.level()


def test_not_started_refuses_everything():
    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=ManualClock(), watchdog=False)
    assert not ir.arm()
    assert not ir.update(**DEPLOYED).output and gpio.history == []


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM")
def test_sigterm_drives_low_then_exits():
    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=ManualClock(), watchdog=False).start()
    try:
        ir.arm()
        ir.update(**DEPLOYED)
        with pytest.raises(SystemExit) as exc:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(1000):  # the Python-level handler runs between bytecodes
                pass
        assert exc.value.code == 128 + signal.SIGTERM
        assert not gpio.level()
    finally:
        ir.close()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


# ---------------------------------------------------------------- backends
@pytest.fixture
def fake_jetson_gpio(monkeypatch):
    calls = []
    mod = types.ModuleType("Jetson.GPIO")
    mod.BOARD, mod.OUT, mod.LOW, mod.HIGH = "BOARD", "OUT", 0, 1
    mod.setwarnings = lambda flag: calls.append(("setwarnings", flag))
    mod.setmode = lambda mode: calls.append(("setmode", mode))
    mod.setup = lambda pin, direction, initial=None: calls.append(("setup", pin, direction, initial))
    mod.output = lambda pin, value: calls.append(("output", pin, value))
    mod.cleanup = lambda pin=None: calls.append(("cleanup", pin))
    pkg = types.ModuleType("Jetson")
    pkg.GPIO = mod
    monkeypatch.setitem(sys.modules, "Jetson", pkg)
    monkeypatch.setitem(sys.modules, "Jetson.GPIO", mod)
    return calls


def test_jetson_backend_uses_board_numbering_and_initial_low(fake_jetson_gpio):
    calls = fake_jetson_gpio
    gpio = open_gpio("jetson")
    assert isinstance(gpio, JetsonGpio)
    with IrInterlock(gpio, clock=ManualClock(), watchdog=False) as ir:
        ir.arm()
        ir.update(**DEPLOYED)
    assert ("setmode", "BOARD") in calls
    assert ("setup", 32, "OUT", 0) in calls
    assert ("output", 32, 1) in calls
    assert calls[-2:] == [("output", 32, 0), ("cleanup", 32)]


def test_auto_falls_back_to_mock_without_jetson(monkeypatch):
    monkeypatch.setitem(sys.modules, "Jetson", None)
    monkeypatch.setitem(sys.modules, "Jetson.GPIO", None)
    assert isinstance(open_gpio("auto"), MockGpio)
    with pytest.raises(RuntimeError, match="Jetson.GPIO unavailable"):
        open_gpio("jetson")
    with pytest.raises(ValueError):
        open_gpio("sysfs")


def test_force_off_cli(fake_jetson_gpio, capsys):
    assert ir_main(["off", "--gpio", "jetson"]) == 0
    assert ("output", 32, 0) in fake_jetson_gpio and ("cleanup", 32) in fake_jetson_gpio
    assert "LOW" in capsys.readouterr().out
    force_off("mock")


# ---------------------------------------------------------------- headset hook
def test_headset_feeds_interlock_from_imu_and_edge_mode():
    pytest.importorskip("cv2")
    from lynx.headset.app import HeadsetClient, HeadsetConfig, build_parser, make_ir_interlock
    from lynx.headset.sources import SyntheticSource
    from lynx.hw import ImuCalibration, ImuLink, MockImuDevice, OrientationConverter, SerialImuPoseSource
    from lynx.spatial import Pose

    attitude = {"pitch": -5.0}
    clock = ManualClock(100.0)
    dev = MockImuDevice(pose_fn=lambda t: (90.0, attitude["pitch"], 0.0), clock=clock, realtime=False)
    link = ImuLink(dev, OrientationConverter(ImuCalibration()), clock=clock)
    src = SerialImuPoseSource("mock://", Pose.from_euler(0, 0, 1.7, 0, 0, 0), link=link)

    def advance(seconds):
        for _ in range(int(round(seconds / 0.01))):
            clock.t += 0.01
            link.feed(dev.read(65536))

    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=clock, watchdog=False).start()
    cfg = HeadsetConfig(url="ws://127.0.0.1:9", detector="none")
    client = HeadsetClient(cfg, SyntheticSource(160, 90, 78.0, seed=1, unknowns=False), src, ir=ir).start()
    try:
        advance(0.5)
        client.step()
        assert not gpio.level() and client.last_state.telemetry["IR"].startswith("IR SAFE")
        assert client.handle_key(ord("n")) and ir.armed
        client.step()
        assert not gpio.level()  # armed but day mode
        client.handle_key(ord("e"))
        client.step()
        assert gpio.level() and client.last_state.telemetry["IR"] == "IR ON"
        attitude["pitch"] = 80.0  # pod flipped up
        advance(0.1)
        client.step()
        assert not gpio.level() and ir.status.state is IrState.LOCKOUT
        assert any("IR LOCKOUT" in a for a in client.last_state.alerts)
        attitude["pitch"] = -5.0
        advance(0.1)
        client.step()
        assert not gpio.level()
        # link goes stale -> IMU not OK; after the lockout, arming works but IR stays off
        clock.t += 121.0
        assert ir.arm()
        client.step()
        assert not gpio.level() and ir.status.state is IrState.LOCKOUT and "IMU" in ir.status.last_trip
    finally:
        client.stop()
    assert not gpio.level() and ir.status.state is IrState.CLOSED

    args = build_parser().parse_args(["--ir-interlock", "--ir-gpio", "mock", "--ir-arm"])
    made = make_ir_interlock(args)
    try:
        assert made is not None and made.armed and made.cfg.pin == 32
    finally:
        made.close()
    assert make_ir_interlock(build_parser().parse_args([])) is None


def test_headset_without_head_tracker_never_enables_ir():
    pytest.importorskip("cv2")
    from lynx.headset.app import HeadsetClient, HeadsetConfig
    from lynx.headset.pose import create_pose_source
    from lynx.headset.sources import SyntheticSource
    from lynx.spatial import Pose

    gpio = MockGpio()
    ir = IrInterlock(gpio, clock=ManualClock(), watchdog=False).start()
    src = create_pose_source("keyboard", Pose.from_euler(0, 0, 1.7, 0, 0, 0))
    client = HeadsetClient(HeadsetConfig(url="ws://127.0.0.1:9", detector="none", edge=True),
                           SyntheticSource(160, 90, 78.0, seed=1, unknowns=False), src, ir=ir).start()
    try:
        ir.arm()
        client.step()
        assert not gpio.level() and "no head tracker" in ir.status.last_trip
    finally:
        client.stop()
