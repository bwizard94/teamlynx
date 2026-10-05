# Head-tracker serial protocol v1 (ESP32 ↔ host)

The firmware implementation is in `firmware/lib/LynxCore/src/lynx_proto.{h,cpp}` and the host
implementation is in `lynx/hw/protocol.py`. Golden frames in both test suites
(`firmware/test/test_core/test_main.cpp` and `tests/hw/test_hw_protocol.py`) pin the two
implementations to identical bytes.

This is the USB link between the head tracker and its own headset computer. It is unrelated to
the squad wire protocol in [../protocol.md](../protocol.md).

## Transport

| | ESP32-DevKitC | ESP32-S3-DevKitC-1 |
|---|---|---|
| Interface | USB-UART bridge (CP2102/CH340) | native USB Serial/JTAG (HW CDC) |
| Host device | `/dev/ttyUSB*`, `COMx` | `/dev/ttyACM*`, `COMx` |
| Baud | 460800 8N1 | ignored |
| Bandwidth used | 100 Hz × 38 B ≈ 3.8 kB/s (≈ 8 % of 460800 baud) | – |

The host opens the port with **DTR and RTS deasserted**. The DevKitC's auto-program circuit
resets the ESP32 when those lines toggle, and a reset would discard device-side state.

## Framing

```
0x00 | COBS( version:u8 | type:u8 | seq:u8 | body[0..64] | crc16:u16 ) | 0x00
```

* **COBS** (Cheshire & Baker) removes every 0x00 from the payload, so 0x00 delimits frames
  unambiguously. The encoding is canonical: a block of exactly 254 non-zero bytes at the end does
  not get a trailing `0x01`.
* The device sends a delimiter both **before and after** each frame. Back-to-back delimiters
  form an empty chunk, which receivers ignore silently. Stray bytes, such as the Adafruit
  library's `Serial.println` on an I2C error, then land in a chunk of their own, so they cost one
  bad-chunk count and no valid frames.
* **crc16** is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR), stored
  little-endian over `version..body`. Check value: `crc16("123456789") = 0x29B1`. In Python it is
  `binascii.crc_hqx(data, 0xFFFF)`.
* **version** is 1. A receiver drops frames with any other version and counts them.
* **seq** is a per-direction u8 counter. The device increments it for **every** frame, including
  frames it drops because the host is not draining the port, so the host can measure loss from
  gaps. Command ACKs echo the command's seq.
* Every multi-byte field is little-endian. Floats are IEEE-754 binary32. The maximum body is
  64 B.
* A receiver must ignore well-formed frames of unknown type, for forward compatibility. The host
  surfaces them as `Unknown`.

## Device → host

| type | name | body (`struct` format) | fields |
|---|---|---|---|
| 0x01 | HELLO | `<BBBBBBHI` (12 B) | fw_major, fw_minor, fw_patch, board (1 DevKitC, 2 S3), report, imu_flags, rate_hz, uptime_ms. Sent at boot and in reply to CMD_HELLO |
| 0x02 | IMU | `<Q5fBB` (30 B) | t_us, w, x, y, z, accuracy_rad, report, cal_status |
| 0x03 | BUTTON | `<QQBBH` (20 B) | t_us, press_t_us, event, clicks, hold_ms |
| 0x04 | STATUS | `<IBBHHHHH` (16 B), 1 Hz | uptime_ms, imu_flags, report, rate_x10 (measured), sensor_resets, watchdog_resets, tx_dropped, rx_bad |
| 0x05 | ACK | `<BBB` | cmd_type, cmd_seq, result |
| 0x06 | LOG | UTF-8, ≤ 64 B | human-readable diagnostics, e.g. `BNO085 at 0x4A sw 3.x.y` |

* **IMU.** `(w, x, y, z)` is the BNO085's `(real, i, j, k)`. It is the active rotation that takes
  sensor-frame vectors to the sensor's ENU world frame, magnetic-North referenced for
  `report = 1`. `accuracy_rad` is the sensor's heading-accuracy estimate, or NaN for the Game
  Rotation Vector. `cal_status` holds the SH-2 status bits 1..0: 0 unreliable, 1 low, 2 medium,
  3 high. The conversion to TeamLynx attitude is in [imu-calibration.md](imu-calibration.md).
* **Timestamps.** `t_us` and `press_t_us` come from **one clock**, `esp_timer_get_time()` (µs
  since ESP32 boot, 64-bit). IMU samples are stamped when the ESP32 services the report, at most
  about 2 ms after the sensor produced it with INT wired. The host matches a gesture's
  `press_t_us` against the IMU history, so a ping uses the head attitude at the instant of the
  press. A jump backwards in `t_us` means the device rebooted.
* **BUTTON events:** 1 PRESS, 2 RELEASE, 3 SINGLE, 4 DOUBLE, 5 LONG. `press_t_us` is the raw edge
  time of the gesture's **first** press, after the end of contact bounce. `clicks` is 1 for
  SINGLE and LONG, 2 for DOUBLE, and 0 for edges. `hold_ms` is the duration of the most recent
  press.
* **imu_flags:** bit 0 BNO085 present, bit 1 streaming (a sample arrived within the last 200 ms).

## Host → device

Every command is answered by exactly one ACK. `result` is 0 OK, 1 BAD_ARG, 2 SENSOR_ERROR (the
SH-2 call failed or no IMU is present), 3 UNKNOWN_CMD or 4 BAD_LENGTH.

| type | name | body | effect |
|---|---|---|---|
| 0x80 | CMD_HELLO | – | device sends HELLO, then ACK |
| 0x81 | CMD_SET_REPORT | `<BH` report (1 RV, 2 GRV), rate_hz (1–400) | enables that report at that rate and disables the other |
| 0x82 | CMD_TARE | `<BB` axes (bit0 X, bit1 Y, bit2 Z), basis (0 RV, 1 GRV, 2 geomagnetic RV) | `sh2_setTareNow` |
| 0x83 | CMD_TARE_PERSIST | – | `sh2_persistTare`: stores the tare in BNO085 flash |
| 0x84 | CMD_TARE_CLEAR | – | `sh2_clearTare` |
| 0x85 | CMD_SAVE_DCD | – | `sh2_saveDcdNow`: stores the dynamic calibration data in BNO085 flash |
| 0x86 | CMD_SET_CAL | `<BB` sensors (bit0 accel, bit1 gyro, bit2 mag), dcd_autosave (0/1) | `sh2_setCalConfig` + `sh2_setDcdAutoSave` |
| 0x87 | CMD_RESET_IMU | – | full BNO085 re-init (reset pulse, SH-2 reopen, reports re-enabled) |

The SH-2 calls block the firmware loop for up to about 100 ms. Do not issue calibration commands
during a game.

## Robustness behaviour

| Situation | Device | Host |
|---|---|---|
| Host not reading | drops frames (`tx_dropped`++), never blocks the loop; seq still advances | sees seq gaps → `loss_fraction` |
| Corrupt / truncated / foreign bytes | – | chunk discarded at the next 0x00 and counted (`bad_frames`) |
| Bad command frame | `rx_bad`++, no ACK | command times out (`TimeoutError`) |
| BNO085 reset itself | re-enables reports, `sensor_resets`++ | – |
| No sample for 1 s | hardware reset pulse; full re-init after 3 strikes; `watchdog_resets`++ | health `STALE` after 0.25 s |
| BNO085 absent at boot | retries every 1 s, LOG message | health `NO_DATA`, "BNO085 not detected" |
| USB unplugged | – | reader reopens the port every 1 s |
