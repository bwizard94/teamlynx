# Bench test procedure (Phase 3)

Run this procedure after building the rig per [wiring.md](wiring.md), and again after any wiring
change. Record the results in the table at the end. Steps marked **auto** are covered by
`python -m lynx.hw bench`.

You need the assembled head tracker, a data-capable USB cable, a multimeter (a scope is optional),
a laptop with the repo installed (`pip install -e ".[dev,hw]"`), and a phone compass or a known
bearing reference.

Replace `P` below with your port: `/dev/ttyUSB0` (DevKitC), `/dev/ttyACM0` (S3) or `COM5`. On
Linux, add yourself to the `dialout` group once and log in again:
`sudo usermod -aG dialout $USER`.

## 1. Pre-power checks (USB unplugged)

| # | Check | Pass |
|---|---|---|
| 1.1 | Resistance 3V3 ↔ GND | > 1 kΩ (not a short) |
| 1.2 | No wire from any 5 V point to the BNO085 (mandatory with a SparkFun 3.3 V-only board) | visual |
| 1.3 | Rail jack: tip ↔ GND with the switch released / pressed | open / < 1 Ω |
| 1.4 | Flex the switch cable at both ends while metering | no intermittent opens |

## 2. Flash and power

```bash
cd firmware
pio run                              # builds esp32dev + esp32s3
pio run -e esp32dev -t upload        # or -e esp32s3 (hold BOOT and tap RST if upload does not start)
pio test -e native                   # host-side unit tests of the protocol and rail logic
```

| # | Check | Pass |
|---|---|---|
| 2.1 | 3V3 rail under load | 3.25–3.35 V |
| 2.2 | SDA, SCL idle; RST | ≈ 3.3 V each |
| 2.3 | INT (scope, optional) | low pulses at ≈ 100 Hz |
| 2.4 | Status LED | solid (DevKitC) / green (S3) within 2 s of boot. A 2 Hz blink means the BNO085 was not found: check wiring and address |

## 3. Link

```bash
python -m lynx.hw hello --port P
```

| # | Check | Pass |
|---|---|---|
| 3.1 | `fw 1.0.0 board ESP32_DEVKITC ...` and `hello: OK` | as shown |
| 3.2 | `imu_flags 0x03` (present and streaming) | 0x03 |
| 3.3 | `device log: BNO085 at 0x4A sw ...` (or 0x4B) | address matches your board |

## 4. Automated stream test (**auto**)

Put the sensor flat on the bench, untouched and away from metal:

```bash
python -m lynx.hw bench --port P --seconds 10
```

| # | Check | Pass |
|---|---|---|
| 4.1 | rate | ≥ 90 Hz (nominal 100) |
| 4.2 | bad frames, seq gaps, device tx dropped | 0 |
| 4.3 | sensor/watchdog resets during the test | 0 |
| 4.4 | still noise std (heading, pitch, roll) | < 0.5° each |
| 4.5 | prints `BENCH PASS` | – |

## 5. Axis and sign verification (Phase 1 conventions)

```bash
python -m lynx.hw monitor --port P --mount top-flat     # use your real mount spec
```

Hold the tracker as it will sit on the head (for `top-flat`: flat, x arrow forward):

| # | Action | Expected change |
|---|---|---|
| 5.1 | Turn right (clockwise seen from above) by about 90° | heading **increases** by about 90 (wraps 359 → 0) |
| 5.2 | Point the line of sight at magnetic North with the phone compass | heading ≈ declination (0 with `--declination` unset) |
| 5.3 | Nose up about 30° | pitch ≈ **+30** |
| 5.4 | Tilt the right ear down about 20° | roll ≈ **+20** |
| 5.5 | Return to level | pitch, roll ≈ 0 ± 1 |

If 5.1–5.4 show a wrong sign or a swapped axis, the `mount` spec is wrong. The math is tested
(`tests/hw/test_hw_orientation.py`), so fix the spec, not the code
([imu-calibration.md §3](imu-calibration.md)).

## 6. Calibration

```bash
python -m lynx.hw cal --port P --sensors accel,gyro,mag --autosave on
python -m lynx.hw monitor --port P          # figure-8s / rolls for ~30 s until "cal 3", acc < 5deg
python -m lynx.hw save-dcd --port P
python -m lynx.hw tare --port P --bearing <B> --cal imu.json   # facing a reference of bearing B
```

| # | Check | Pass |
|---|---|---|
| 6.1 | `cal 3` and `acc` < 5° reached | within 60 s |
| 6.2 | `save DCD: OK` | OK |
| 6.3 | power-cycle, then `monitor` | starts at `cal ≥ 2` without re-waving |
| 6.4 | tare, then turn away and back to the reference | heading returns to B ± 1° |

## 7. Rail switch (**auto** with `--require-gestures`)

```bash
python -m lynx.hw bench --port P --seconds 15 --require-gestures
```

During the 15 s: one click, one double-click, one hold longer than 1 s.

| # | Check | Pass |
|---|---|---|
| 7.1 | events printed: SINGLE, DOUBLE, LONG, each with `aim=yes` | all three |
| 7.2 | 10 deliberate single clicks about 1 s apart | exactly 10 SINGLE, 0 DOUBLE |
| 7.3 | wiggle the plug in the jack without pressing | no events (otherwise fix the jack, cable or filter) |

## 8. End-to-end ping (sim)

```bash
lynx-relay &
lynx-sim --node 1 --callsign ALPHA --imu-port P --imu-cal imu.json
lynx-sim --node 2 --callsign BRAVO --team green --y 20 --heading 180     # observer window
```

| # | Check | Pass |
|---|---|---|
| 8.1 | moving the tracker moves ALPHA's view smoothly, with no visible lag at 60 fps | – |
| 8.2 | pitch down about 10°, single click | ALPHA's status line reads about 9.6 m ground (eye height 1.7 m); the ping appears in BRAVO's window |
| 8.3 | aim, click, then immediately swing away | the ping lands where you aimed, not where you swung to |
| 8.4 | double-click | a CONTACT ping |
| 8.5 | hold | your last ping is cancelled in both windows |

Repeat 8.2–8.5 with the headset client (`lynx-headset --pose "serial:P?cal=imu.json"`). It uses
the same gesture map; see [../headset.md](../headset.md).

## 9. Fault injection

| # | Fault | Expected |
|---|---|---|
| 9.1 | Disconnect INT | streaming continues (20 ms fallback polling); rate ≥ 45 Hz |
| 9.2 | Disconnect SDA for 3 s, then reconnect | `monitor` shows `stale`; device LOG `IMU stalled`; recovers by itself; reset counter increments |
| 9.3 | Unplug USB for 3 s, then replug | host reconnects within about 2 s; no restart needed |
| 9.4 | Hold a steel tool 5 cm from the sensor (RV mode) | heading pulls, `acc` grows, health → `degraded` (IMU_DEGRADED telemetry flag) |

## 10. Results record

| Date | Board / IMU | fw | 4.1 rate | 4.4 noise h/p/r | 5 signs OK | 6.4 tare repeat | 7 gestures | 8 ping | 9 faults | Tester |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |
