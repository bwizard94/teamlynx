"""850 nm illuminator interlock: drives IR_EN (Jetson 40-pin header pin 32) for the PDB.

The PDB (``docs/field/hw-compute-power.md`` §5) is fail-safe in hardware: IR_EN low, floating
or unpowered means the LDD-350L DIM pin is pulled low and the LEDs are off, and S1 SAFE/ARM
cuts the driver's supply. This module decides when software may drive IR_EN high:

* night/edge mode is active,
* the pod is not flipped up to stow (pitch within ``pitch_min_deg``..``pitch_max_deg``, roll
  within ``±roll_max_deg``),
* the head-tracker link is healthy,
* the operator has armed the illuminator in software (S1 is the hardware arm; it cannot be read
  by the Jetson, it just removes power).

Any trip (pod stowed, attitude unknown, IMU unhealthy, inputs stale, an exception, a GPIO
error) while armed drives IR_EN low, disarms, and locks out re-arming for ``rearm_after_trip_s``
(120 s). Leaving edge mode is not a trip: the output goes low but the arm is kept. On close,
interpreter exit, SIGTERM or SIGHUP the pin is driven low. A background watchdog drives it low
if :meth:`IrInterlock.update` stops being called. SIGKILL cannot be caught and Jetson.GPIO
holds the last level, so services must also run ``python -m lynx.hw.ir_interlock off`` as a
stop hook (``ExecStopPost=``).

    with IrInterlock(open_gpio("auto")) as ir:
        ir.arm()
        while running:
            ir.update(edge_mode=True, pitch_deg=p, roll_deg=r, imu_ok=health.ok)
"""

from __future__ import annotations

import argparse
import atexit
import enum
import logging
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

log = logging.getLogger("lynx.hw.ir")

IR_EN_PIN = 32  # Jetson Orin Nano dev kit J12, BOARD numbering


class GpioBackend(Protocol):
    name: str

    def setup_output(self, pin: int) -> None: ...

    def write(self, pin: int, high: bool) -> None: ...

    def release(self, pin: int) -> None: ...


class MockGpio:
    """In-memory backend for tests and non-Jetson hosts. ``fail_writes`` simulates a bad pin."""

    name = "mock"

    def __init__(self) -> None:
        self.levels: dict[int, bool] = {}
        self.history: List[Tuple[int, bool]] = []
        self.configured: set[int] = set()
        self.released: set[int] = set()
        self.fail_writes = False

    def setup_output(self, pin: int) -> None:
        self.configured.add(pin)
        self.levels[pin] = False
        self.history.append((pin, False))

    def write(self, pin: int, high: bool) -> None:
        if pin not in self.configured:
            raise RuntimeError(f"pin {pin} not configured as an output")
        if self.fail_writes and high:
            raise OSError("simulated GPIO write failure")
        self.levels[pin] = bool(high)
        self.history.append((pin, bool(high)))

    def release(self, pin: int) -> None:
        self.levels[pin] = False
        self.released.add(pin)

    def level(self, pin: int = IR_EN_PIN) -> bool:
        return self.levels.get(pin, False)


class JetsonGpio:
    """``Jetson.GPIO`` (NVIDIA's RPi.GPIO-compatible library), BOARD pin numbering."""

    name = "jetson"

    def __init__(self) -> None:
        try:
            import Jetson.GPIO as GPIO  # type: ignore[import-not-found]
        except Exception as exc:  # ImportError, or RuntimeError when not on a Jetson
            raise RuntimeError(f"Jetson.GPIO unavailable: {exc}") from exc
        self.GPIO = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)

    def setup_output(self, pin: int) -> None:
        self.GPIO.setup(pin, self.GPIO.OUT, initial=self.GPIO.LOW)

    def write(self, pin: int, high: bool) -> None:
        self.GPIO.output(pin, self.GPIO.HIGH if high else self.GPIO.LOW)

    def release(self, pin: int) -> None:
        try:
            self.GPIO.output(pin, self.GPIO.LOW)
        finally:
            self.GPIO.cleanup(pin)


def open_gpio(kind: str = "auto") -> GpioBackend:
    """``jetson`` (must work), ``mock``, or ``auto`` (Jetson.GPIO if importable, else mock)."""
    if kind == "mock":
        return MockGpio()
    if kind == "jetson":
        return JetsonGpio()
    if kind == "auto":
        try:
            return JetsonGpio()
        except RuntimeError as exc:
            log.warning("IR interlock: %s; using the mock GPIO backend (no illuminator control)", exc)
            return MockGpio()
    raise ValueError(f"unknown GPIO backend {kind!r} (auto, jetson, mock)")


class IrState(enum.Enum):
    SAFE = "safe"  # not armed
    INHIBITED = "inhibited"  # armed, but not in night/edge mode
    ON = "on"
    LOCKOUT = "lockout"  # tripped; re-arm refused until the lockout expires
    CLOSED = "closed"


@dataclass
class IrInterlockConfig:
    pin: int = IR_EN_PIN
    pitch_min_deg: float = -45.0
    pitch_max_deg: float = 30.0
    roll_max_deg: float = 60.0
    rearm_after_trip_s: float = 120.0
    watchdog_s: float = 0.5  # max time between update() calls before the output is forced low


@dataclass
class IrStatus:
    state: IrState
    output: bool
    armed: bool
    reasons: List[str] = field(default_factory=list)
    lockout_remaining_s: float = 0.0
    last_trip: str = ""

    def summary(self) -> str:
        s = f"IR {self.state.value.upper()}"
        if self.state is IrState.LOCKOUT:
            s += f" {self.lockout_remaining_s:.0f}s ({self.last_trip})"
        elif self.reasons:
            s += f" ({'; '.join(self.reasons)})"
        return s


def attitude_reasons(pitch_deg: Optional[float], roll_deg: Optional[float], cfg: IrInterlockConfig) -> List[str]:
    """Why the pod is not in its deployed attitude ([] when it is)."""
    if pitch_deg is None or roll_deg is None or not (math.isfinite(pitch_deg) and math.isfinite(roll_deg)):
        return ["attitude unknown"]
    out = []
    if not cfg.pitch_min_deg <= pitch_deg <= cfg.pitch_max_deg:
        out.append(f"pod pitch {pitch_deg:+.0f}deg (stowed?)")
    if abs(roll_deg) > cfg.roll_max_deg:
        out.append(f"pod roll {roll_deg:+.0f}deg")
    return out


class IrInterlock:
    """Owns IR_EN. Call :meth:`update` every frame; everything else drives the pin low."""

    def __init__(self, gpio: GpioBackend, config: Optional[IrInterlockConfig] = None,
                 clock: Callable[[], float] = time.monotonic, watchdog: bool = True) -> None:
        self.gpio = gpio
        self.cfg = config or IrInterlockConfig()
        self.clock = clock
        self._lock = threading.RLock()
        self._armed = False
        self._output = False
        self._lockout_until = -math.inf
        self._last_trip = ""
        self._last_update: Optional[float] = None
        self._started = False
        self._closed = False
        self._use_watchdog = watchdog
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prev_handlers: dict[int, object] = {}
        self.status = IrStatus(IrState.SAFE, False, False)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "IrInterlock":
        with self._lock:
            if self._started:
                return self
            self.gpio.setup_output(self.cfg.pin)
            self._write(False)
            self._started = True
            atexit.register(self.close)
            self._install_signal_handlers()
            if self._use_watchdog:
                self._thread = threading.Thread(target=self._watchdog_loop, name="lynx-ir-watchdog", daemon=True)
                self._thread.start()
        log.info("IR interlock on pin %d (%s backend), output LOW", self.cfg.pin, self.gpio.name)
        return self

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._armed = False
            self._stop.set()
            try:
                if self._started:
                    self._write(False)
            except Exception:  # noqa: BLE001 - still release the pin
                log.exception("IR interlock: failed to drive IR_EN low on close")
            finally:
                if self._started:
                    try:
                        self.gpio.release(self.cfg.pin)
                    except Exception:  # noqa: BLE001
                        log.exception("IR interlock: GPIO release failed")
                self._restore_signal_handlers()
                self.status = IrStatus(IrState.CLOSED, False, False, last_trip=self._last_trip)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        try:
            atexit.unregister(self.close)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "IrInterlock":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ operator
    def arm(self, now: Optional[float] = None) -> bool:
        """Software arm. Refused while closed, not started or locked out after a trip."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._closed or not self._started:
                return False
            if now < self._lockout_until:
                log.warning("IR arm refused: lockout %.0fs after trip (%s)", self._lockout_until - now, self._last_trip)
                return False
            self._armed = True
            log.info("IR armed")
            return True

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            if self._started and not self._closed:
                self._safe_write(False)

    def toggle_arm(self, now: Optional[float] = None) -> bool:
        if self._armed:
            self.disarm()
            return False
        return self.arm(now)

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def output(self) -> bool:
        return self._output

    # ------------------------------------------------------------------ per frame
    def update(self, edge_mode: bool, pitch_deg: Optional[float], roll_deg: Optional[float], imu_ok: bool,
               now: Optional[float] = None, imu_reason: str = "") -> IrStatus:
        """Evaluate all conditions and set IR_EN. Never raises; any internal error is a trip."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._closed or not self._started:
                return self.status
            self._last_update = now
            try:
                faults = attitude_reasons(pitch_deg, roll_deg, self.cfg)
                if not imu_ok:
                    faults.append(f"IMU {imu_reason or 'not OK'}")
                if self._armed and faults:
                    self._trip("; ".join(faults), now)
                elif not self._armed:
                    self._safe_write(False)
                elif not edge_mode:
                    self._safe_write(False)
                else:
                    self._write(True)
                if not self._armed:
                    reasons = faults
                elif self._output:
                    reasons = []
                else:
                    reasons = ["edge mode off"]
                self.status = self._make_status(now, reasons)
            except Exception as exc:  # noqa: BLE001 - fail safe
                self._trip(f"error: {exc!r}", now)
                self.status = self._make_status(now, [])
            return self.status

    def check_watchdog(self, now: Optional[float] = None) -> bool:
        """Force the output low if update() is overdue. True if it tripped."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._closed or not self._output:
                return False
            if self._last_update is not None and now - self._last_update > self.cfg.watchdog_s:
                self._trip(f"no update for {now - self._last_update:.2f}s", now)
                self.status = self._make_status(now, [])
                return True
            return False

    # ------------------------------------------------------------------ internals
    def _trip(self, why: str, now: float) -> None:
        was_armed = self._armed
        self._armed = False
        self._safe_write(False)
        if was_armed or self._output:
            self._lockout_until = now + self.cfg.rearm_after_trip_s
            self._last_trip = why
            log.warning("IR TRIP: %s; IR_EN low, re-arm locked out for %.0fs", why, self.cfg.rearm_after_trip_s)

    def _write(self, high: bool) -> None:
        self.gpio.write(self.cfg.pin, high)
        self._output = high

    def _safe_write(self, high: bool) -> None:
        try:
            self._write(high)
        except Exception:  # noqa: BLE001 - level unknown: keep the old value so the watchdog retries
            log.exception("IR interlock: GPIO write failed")

    def _make_status(self, now: float, reasons: List[str]) -> IrStatus:
        remaining = max(0.0, self._lockout_until - now)
        if self._output:
            state = IrState.ON
        elif remaining > 0 and not self._armed:
            state = IrState.LOCKOUT
        elif self._armed:
            state = IrState.INHIBITED
        else:
            state = IrState.SAFE
        return IrStatus(state, self._output, self._armed, list(reasons), remaining, self._last_trip)

    def _watchdog_loop(self) -> None:
        period = max(0.02, self.cfg.watchdog_s / 4)
        while not self._stop.wait(period):
            try:
                self.check_watchdog()
            except Exception:  # noqa: BLE001
                log.exception("IR watchdog error")

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                self._prev_handlers[sig] = signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._prev_handlers.clear()
            return
        for sig, prev in self._prev_handlers.items():
            try:
                signal.signal(sig, prev)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):
                pass
        self._prev_handlers.clear()

    def _on_signal(self, signum, frame) -> None:
        prev = self._prev_handlers.get(signum)
        self.close()
        if callable(prev):
            prev(signum, frame)
        raise SystemExit(128 + signum)


def force_off(kind: str = "auto", pin: int = IR_EN_PIN) -> None:
    """Drive IR_EN low and release it (service stop hook / manual safing)."""
    gpio = open_gpio(kind)
    gpio.setup_output(pin)
    gpio.write(pin, False)
    gpio.release(pin)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lynx.hw.ir_interlock", description="IR_EN utilities")
    ap.add_argument("command", choices=["off"], help="off: drive IR_EN low and release the pin")
    ap.add_argument("--gpio", default="auto", choices=["auto", "jetson", "mock"])
    ap.add_argument("--pin", type=int, default=IR_EN_PIN)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    force_off(args.gpio, args.pin)
    print(f"IR_EN pin {args.pin} driven LOW")
    return 0


if __name__ == "__main__":
    sys.exit(main())
