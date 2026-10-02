# TeamLynx head-tracker firmware

The firmware runs on an ESP32 or ESP32-S3 with a BNO085 over I2C and a rail switch on a GPIO. It
streams framed binary over USB serial.

```bash
pio run                          # build esp32dev + esp32s3
pio run -e esp32dev -t upload    # flash a DevKitC (or -e esp32s3)
pio test -e native               # host unit tests: COBS, CRC, protocol golden frames, rail gestures
```

For wiring, the protocol, calibration and the bench test, see [`../docs/hardware/`](../docs/hardware/README.md).
