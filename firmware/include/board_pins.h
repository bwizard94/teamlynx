#pragma once

// Pin map. Must match docs/hardware/wiring.md.
// Pins avoid strapping pins (ESP32: 0, 2*, 5, 12, 15; S3: 0, 3, 45, 46), flash/PSRAM pins
// (ESP32: 6-11, 16-17 on WROVER; S3: 26-37 on octal-PSRAM modules) and input-only pins without
// pull-ups (ESP32: 34-39). *GPIO2 is only used as an output (on-board LED), which is safe.

#include "lynx_proto.h"

#if defined(LYNX_BOARD_ESP32DEV)

constexpr int PIN_I2C_SDA = 21;
constexpr int PIN_I2C_SCL = 22;
constexpr int PIN_BNO_INT = 19;  // BNO085 H_INTN, active low, open drain -> INPUT_PULLUP
constexpr int PIN_BNO_RST = 18;  // BNO085 NRST, active low
constexpr int PIN_RAIL = 27;     // rail switch to GND, INPUT_PULLUP
constexpr int PIN_LED = 2;       // on-board blue LED, active high
constexpr uint8_t BOARD_ID = lynx::BOARD_ESP32_DEVKITC;

#elif defined(LYNX_BOARD_ESP32S3)

constexpr int PIN_I2C_SDA = 8;
constexpr int PIN_I2C_SCL = 9;
constexpr int PIN_BNO_INT = 5;
constexpr int PIN_BNO_RST = 6;
constexpr int PIN_RAIL = 7;
constexpr int PIN_LED = -1;  // the S3-DevKitC-1 RGB LED is driven through RGB_BUILTIN instead
constexpr uint8_t BOARD_ID = lynx::BOARD_ESP32_S3_DEVKITC;

#else
#error "Define LYNX_BOARD_ESP32DEV or LYNX_BOARD_ESP32S3 (see platformio.ini)"
#endif

// BNO085 7-bit I2C address: 0x4A with SA0 low (Adafruit 4754 default),
// 0x4B with SA0 high (SparkFun SEN-22857 default). Both are probed at boot.
constexpr uint8_t BNO_ADDR_PRIMARY = 0x4A;
constexpr uint8_t BNO_ADDR_SECONDARY = 0x4B;
constexpr uint32_t I2C_CLOCK_HZ = 400000;

constexpr uint32_t SERIAL_BAUD = 460800;  // ignored by native USB CDC (S3)
