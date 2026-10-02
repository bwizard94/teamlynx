# Phase 3 bench rig: hardware

The head tracker is a BNO085 IMU on an ESP32. It reads fused orientation at 100 Hz plus the
weapon-rail switch, and sends both to the headset computer over USB in a framed binary protocol.
The host side lives in `lynx/hw/`. It feeds `lynx-sim --imu-port PORT` and
`lynx-headset --pose serial:PORT` (headset branch, [../headset.md](../headset.md)).

```
 BNO085 ──I2C 400k──► ESP32 ──USB serial, COBS+CRC16 v1──► lynx.hw.ImuLink ─► head pose (Phase 1 h/p/r)
 rail switch ──GPIO──►  (debounce, single/double/long)          │            └► rail events ─► ping path
                                                                 └ health ─► IMU_DEGRADED / PING_SWITCH flags
```

| Doc | Contents |
|---|---|
| [wiring.md](wiring.md) | pin tables, ASCII wiring for I2C, rail switch and power; pull-up sizing; cable and strain relief |
| [serial-protocol.md](serial-protocol.md) | device ↔ host frame format, messages, commands, robustness |
| [imu-calibration.md](imu-calibration.md) | quaternion → heading/pitch/roll, mounting spec, declination, tare, DCD |
| [bom.md](bom.md) | parts with example part numbers |
| [camera-illuminator.md](camera-illuminator.md) | NoIR camera and 850 nm illuminator selection, eye safety |
| [bench-test.md](bench-test.md) | step-by-step bring-up and acceptance test |

## Quickstart

```bash
pip install -e ".[dev,hw]"                     # pyserial for real ports (the mock needs nothing)

# Firmware (PlatformIO)
cd firmware && pio run && pio run -e esp32dev -t upload && cd ..

# Host
python -m lynx.hw hello  --port /dev/ttyUSB0
python -m lynx.hw bench  --port /dev/ttyUSB0
python -m lynx.hw tare   --port /dev/ttyUSB0 --bearing 0 --cal imu.json
lynx-sim --imu-port /dev/ttyUSB0 --imu-cal imu.json
lynx-headset --pose "serial:/dev/ttyUSB0?cal=imu.json"

# No hardware: the mock speaks the real protocol
lynx-sim --imu-port mock://
lynx-headset --pose serial:mock://
python -m lynx.hw bench --port mock://still
```

## Firmware layout

| Path | Contents |
|---|---|
| `firmware/platformio.ini` | envs `esp32dev`, `esp32s3` (both built by `pio run`), and `native` (`pio test -e native`) |
| `firmware/include/board_pins.h` | the pin map (must match wiring.md) |
| `firmware/src/main.cpp` | BNO085 service (INT-gated, watchdog, auto re-init), rail switch, command handler, status, LED |
| `firmware/lib/LynxCore/` | hardware-independent COBS, CRC-16, protocol and rail-switch gesture logic, unit-tested on the host |

Libraries: `adafruit/Adafruit BNO08x` (wraps the CEVA SH-2 driver; tare, DCD and cal-config
calls use `sh2_*` directly).

## Using the head tracker from code

```python
from lynx.hw import ImuLink, ImuCalibration, ImuHeadSource

link = ImuLink.open("/dev/ttyUSB0", ImuCalibration.load("imu.json")).start()
src = ImuHeadSource(link)
pose, rail_cmds = src.poll()          # once per frame: latest HeadPose + gesture commands
pose.heading, pose.pitch, pose.roll   # Phase 1 convention; pose.q_wb for raycasts
link.health().summary()
# async: async for ev in link.aevents(): ... ; await link.acommand(CmdSetReport(...))
```

## Headset integration (`--pose serial:SPEC`)

Importing `lynx.hw` registers the `serial` pose source (`lynx/hw/headset_source.py`). SPEC is
`PORT[?cal=imu.json&mount=left-side&declination=-3.5&convergence=0&baud=460800&rate=100]`.

* `pose` is the head attitude at the CLI's `--x --y --eye-height`, since the IMU gives no
  position. On a trigger frame it is the attitude **at the press**, so the headset's
  `drop_ping(sample.pose)` lands where the operator aimed.
* `trigger` fires once per SINGLE or DOUBLE. `flags` carries `PING_SWITCH` and `IMU_DEGRADED`.
* The base `PoseSample` contract cannot express "CONTACT ping" (DOUBLE) or "cancel last" (LONG).
  They are exposed as `SerialPoseSample.rail`, a tuple of `RailCommand`. A headset that reads it
  gets the same gesture map as `lynx-sim`. One that ignores it treats DOUBLE as an ordinary ping
  and ignores LONG.
