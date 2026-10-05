// TeamLynx head tracker: BNO085 (I2C) + weapon-rail switch -> framed binary over USB serial.
// Protocol: docs/hardware/serial-protocol.md. Wiring: docs/hardware/wiring.md.

#include <Adafruit_BNO08x.h>
#include <Arduino.h>
#include <Wire.h>
#include <esp_timer.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "board_pins.h"
#include "lynx_proto.h"
#include "rail_switch.h"

#ifndef LYNX_I2C_HZ
#define LYNX_I2C_HZ I2C_CLOCK_HZ
#endif

namespace {

constexpr uint16_t kDefaultRateHz = 100;
constexpr uint16_t kMinRateHz = 1;
constexpr uint16_t kMaxRateHz = 400;
constexpr uint32_t kStatusPeriodMs = 1000;
constexpr uint32_t kImuRetryMs = 1000;
constexpr uint32_t kStreamTimeoutMs = 200;
constexpr uint32_t kWatchdogMs = 1000;
constexpr uint8_t kWatchdogStrikesBeforeReinit = 3;
// Service the SH-2 link at least this often even without H_INTN (INT not wired, or a missed edge).
constexpr uint64_t kIntFallbackUs = 20000;
constexpr uint32_t kLedFlashMs = 80;

Adafruit_BNO08x bno(PIN_BNO_RST);
sh2_SensorValue_t sensor_value;
lynx::RailSwitch rail;
lynx::FrameReceiver rx;
lynx::Packet rx_packet;

struct DeviceState {
  bool imu_present = false;
  uint8_t bno_addr = 0;
  uint8_t report = lynx::REPORT_ROTATION_VECTOR;
  uint16_t rate_hz = kDefaultRateHz;
  bool sample_since_init = false;
  uint32_t last_sample_ms = 0;
  uint32_t last_init_attempt_ms = 0;
  uint64_t last_service_us = 0;
  uint32_t samples_this_period = 0;
  uint16_t rate_x10 = 0;
  uint16_t sensor_resets = 0;
  uint16_t watchdog_resets = 0;
  uint8_t watchdog_strikes = 0;
  uint16_t tx_dropped = 0;
  uint16_t rx_bad = 0;
  uint32_t last_status_ms = 0;
  uint8_t tx_seq = 0;
  uint32_t led_flash_until_ms = 0;
};

DeviceState st;

uint64_t now_us() { return static_cast<uint64_t>(esp_timer_get_time()); }

void bump(uint16_t& counter) {
  if (counter != 0xFFFF) {
    ++counter;
  }
}

uint8_t imu_flags() {
  uint8_t f = 0;
  if (st.imu_present) {
    f |= lynx::IMU_PRESENT;
    if (st.sample_since_init && millis() - st.last_sample_ms <= kStreamTimeoutMs) {
      f |= lynx::IMU_STREAMING;
    }
  }
  return f;
}

// Never blocks: if the host is not draining the port the frame is dropped and counted.
// The sequence number advances regardless so the host sees the gap.
void send_frame(uint8_t type, const uint8_t* body, size_t len) {
  uint8_t frame[lynx::kMaxWireFrame];
  const size_t n = lynx::build_frame(type, st.tx_seq++, body, len, frame, sizeof frame);
  if (n == 0) {
    return;
  }
  if (Serial.availableForWrite() < static_cast<int>(n)) {
    bump(st.tx_dropped);
    return;
  }
  Serial.write(frame, n);
}

void send_log(const char* fmt, ...) {
  char text[lynx::kMaxBody + 1];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(text, sizeof text, fmt, ap);
  va_end(ap);
  if (n < 0) {
    return;
  }
  if (static_cast<size_t>(n) > lynx::kMaxBody) {
    n = lynx::kMaxBody;
  }
  send_frame(lynx::MSG_LOG, reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(n));
}

void send_hello() {
  uint8_t body[16];
  lynx::Writer w(body, sizeof body);
  w.u8(LYNX_FW_MAJOR).u8(LYNX_FW_MINOR).u8(LYNX_FW_PATCH).u8(BOARD_ID);
  w.u8(st.report).u8(imu_flags()).u16(st.rate_hz).u32(millis());
  send_frame(lynx::MSG_HELLO, body, w.size());
}

void send_status() {
  uint8_t body[20];
  lynx::Writer w(body, sizeof body);
  w.u32(millis()).u8(imu_flags()).u8(st.report).u16(st.rate_x10);
  w.u16(st.sensor_resets).u16(st.watchdog_resets).u16(st.tx_dropped).u16(st.rx_bad);
  send_frame(lynx::MSG_STATUS, body, w.size());
}

void send_ack(uint8_t cmd_type, uint8_t cmd_seq, uint8_t result) {
  const uint8_t body[3] = {cmd_type, cmd_seq, result};
  send_frame(lynx::MSG_ACK, body, sizeof body);
}

// ---------------------------------------------------------------------------------------------
// LED: solid = streaming, 2 Hz blink = IMU missing/stalled, short flash = rail switch event.
// ---------------------------------------------------------------------------------------------

enum class LedState : uint8_t { Unset, Streaming, MissingOn, MissingOff, Flash };

void update_led(uint32_t now_ms) {
  static LedState last = LedState::Unset;
  LedState s;
  if (now_ms < st.led_flash_until_ms) {
    s = LedState::Flash;
  } else if (imu_flags() & lynx::IMU_STREAMING) {
    s = LedState::Streaming;
  } else {
    s = (now_ms / 250) % 2 == 0 ? LedState::MissingOn : LedState::MissingOff;
  }
  if (s == last) {
    return;
  }
  last = s;
#if defined(RGB_BUILTIN)
  // RGB: green streaming, blinking red IMU missing, blue flash on a gesture.
  switch (s) {
    case LedState::Streaming: neopixelWrite(RGB_BUILTIN, 0, 20, 0); break;
    case LedState::MissingOn: neopixelWrite(RGB_BUILTIN, 40, 0, 0); break;
    case LedState::Flash: neopixelWrite(RGB_BUILTIN, 0, 0, 40); break;
    default: neopixelWrite(RGB_BUILTIN, 0, 0, 0); break;
  }
#else
  // Single LED: solid streaming, blinking IMU missing, dark flash on a gesture.
  if (PIN_LED >= 0) {
    digitalWrite(PIN_LED, (s == LedState::Streaming || s == LedState::MissingOn) ? HIGH : LOW);
  }
#endif
}

// ---------------------------------------------------------------------------------------------
// BNO085
// ---------------------------------------------------------------------------------------------

sh2_SensorId_t sensor_for(uint8_t report) {
  return report == lynx::REPORT_GAME_ROTATION_VECTOR ? SH2_GAME_ROTATION_VECTOR : SH2_ROTATION_VECTOR;
}

bool enable_reports() {
  const uint32_t interval_us = 1000000UL / st.rate_hz;
  const uint8_t other = st.report == lynx::REPORT_ROTATION_VECTOR ? lynx::REPORT_GAME_ROTATION_VECTOR
                                                                  : lynx::REPORT_ROTATION_VECTOR;
  bno.enableReport(sensor_for(other), 0);
  return bno.enableReport(sensor_for(st.report), interval_us);
}

bool imu_init() {
  st.last_init_attempt_ms = millis();
  const uint8_t addrs[2] = {BNO_ADDR_PRIMARY, BNO_ADDR_SECONDARY};
  for (uint8_t addr : addrs) {
    if (bno.begin_I2C(addr, &Wire)) {
      st.imu_present = true;
      st.bno_addr = addr;
      st.sample_since_init = false;
      st.last_sample_ms = millis();
      st.watchdog_strikes = 0;
      if (!enable_reports()) {
        send_log("BNO085 0x%02X: enableReport failed", addr);
      }
      send_log("BNO085 at 0x%02X sw %u.%u.%u", addr, bno.prodIds.entry[0].swVersionMajor,
               bno.prodIds.entry[0].swVersionMinor, bno.prodIds.entry[0].swVersionPatch);
      return true;
    }
  }
  st.imu_present = false;
  send_log("BNO085 not found at 0x%02X/0x%02X", BNO_ADDR_PRIMARY, BNO_ADDR_SECONDARY);
  return false;
}

void handle_sample(uint64_t t_us) {
  float w, x, y, z, accuracy;
  uint8_t report;
  switch (sensor_value.sensorId) {
    case SH2_ROTATION_VECTOR: {
      const sh2_RotationVectorWAcc_t& q = sensor_value.un.rotationVector;
      w = q.real;
      x = q.i;
      y = q.j;
      z = q.k;
      accuracy = q.accuracy;
      report = lynx::REPORT_ROTATION_VECTOR;
      break;
    }
    case SH2_GAME_ROTATION_VECTOR: {
      const sh2_RotationVector_t& q = sensor_value.un.gameRotationVector;
      w = q.real;
      x = q.i;
      y = q.j;
      z = q.k;
      accuracy = NAN;
      report = lynx::REPORT_GAME_ROTATION_VECTOR;
      break;
    }
    default:
      return;
  }
  st.sample_since_init = true;
  st.last_sample_ms = millis();
  st.watchdog_strikes = 0;
  ++st.samples_this_period;

  uint8_t body[32];
  lynx::Writer wr(body, sizeof body);
  wr.u64(t_us).f32(w).f32(x).f32(y).f32(z).f32(accuracy);
  wr.u8(report).u8(sensor_value.status & 0x03);
  send_frame(lynx::MSG_IMU, body, wr.size());
}

void service_imu(uint64_t t_us) {
  const uint32_t now_ms = millis();
  if (!st.imu_present) {
    if (now_ms - st.last_init_attempt_ms >= kImuRetryMs) {
      imu_init();
    }
    return;
  }

  if (bno.wasReset()) {
    // The sensor reboots once during begin_I2C(); only count resets after streaming started.
    if (st.sample_since_init) {
      bump(st.sensor_resets);
      send_log("BNO085 reset; re-enabling reports");
    }
    st.sample_since_init = false;
    enable_reports();
  }

  const bool int_asserted = digitalRead(PIN_BNO_INT) == LOW;
  if (int_asserted || t_us - st.last_service_us >= kIntFallbackUs) {
    st.last_service_us = t_us;
    if (bno.getSensorEvent(&sensor_value)) {
      handle_sample(now_us());
    }
  }

  if (millis() - st.last_sample_ms > kWatchdogMs) {
    bump(st.watchdog_resets);
    st.last_sample_ms = millis();
    if (++st.watchdog_strikes >= kWatchdogStrikesBeforeReinit) {
      send_log("IMU stalled: full re-init");
      imu_init();
    } else {
      send_log("IMU stalled: hardware reset");
      bno.hardwareReset();
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Rail switch
// ---------------------------------------------------------------------------------------------

void service_rail(uint64_t t_us) {
  lynx::RailEventRecord events[lynx::RailSwitch::kMaxEventsPerUpdate];
  const bool raw_pressed = digitalRead(PIN_RAIL) == LOW;
  const size_t n = rail.update(raw_pressed, t_us, events, lynx::RailSwitch::kMaxEventsPerUpdate);
  for (size_t i = 0; i < n; ++i) {
    const lynx::RailEventRecord& e = events[i];
    uint8_t body[24];
    lynx::Writer w(body, sizeof body);
    w.u64(e.t_us).u64(e.press_t_us).u8(static_cast<uint8_t>(e.type)).u8(e.clicks).u16(e.hold_ms);
    send_frame(lynx::MSG_BUTTON, body, w.size());
    if (e.type == lynx::RailEvent::Single || e.type == lynx::RailEvent::Double ||
        e.type == lynx::RailEvent::Long) {
      st.led_flash_until_ms = millis() + kLedFlashMs;
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Host commands
// ---------------------------------------------------------------------------------------------

uint8_t sh2_result(int rc) { return rc == SH2_OK ? lynx::ACK_OK : lynx::ACK_SENSOR_ERROR; }

void handle_command(const lynx::Packet& p) {
  lynx::Reader r(p.body, p.body_len);
  uint8_t result = lynx::ACK_OK;
  switch (p.type) {
    case lynx::CMD_HELLO:
      if (p.body_len != 0) {
        result = lynx::ACK_BAD_LENGTH;
      } else {
        send_hello();
      }
      break;

    case lynx::CMD_SET_REPORT: {
      if (p.body_len != 3) {
        result = lynx::ACK_BAD_LENGTH;
        break;
      }
      const uint8_t report = r.u8();
      const uint16_t rate = r.u16();
      if ((report != lynx::REPORT_ROTATION_VECTOR && report != lynx::REPORT_GAME_ROTATION_VECTOR) ||
          rate < kMinRateHz || rate > kMaxRateHz) {
        result = lynx::ACK_BAD_ARG;
        break;
      }
      st.report = report;
      st.rate_hz = rate;
      if (st.imu_present && !enable_reports()) {
        result = lynx::ACK_SENSOR_ERROR;
      }
      break;
    }

    case lynx::CMD_TARE: {
      if (p.body_len != 2) {
        result = lynx::ACK_BAD_LENGTH;
        break;
      }
      const uint8_t axes = r.u8();
      const uint8_t basis = r.u8();
      if (axes == 0 || axes > 0x07 || basis > SH2_TARE_BASIS_GEOMAGNETIC_ROTATION_VECTOR) {
        result = lynx::ACK_BAD_ARG;
      } else if (!st.imu_present) {
        result = lynx::ACK_SENSOR_ERROR;
      } else {
        result = sh2_result(sh2_setTareNow(axes, static_cast<sh2_TareBasis_t>(basis)));
      }
      break;
    }

    case lynx::CMD_TARE_PERSIST:
    case lynx::CMD_TARE_CLEAR:
    case lynx::CMD_SAVE_DCD:
      if (p.body_len != 0) {
        result = lynx::ACK_BAD_LENGTH;
      } else if (!st.imu_present) {
        result = lynx::ACK_SENSOR_ERROR;
      } else if (p.type == lynx::CMD_TARE_PERSIST) {
        result = sh2_result(sh2_persistTare());
      } else if (p.type == lynx::CMD_TARE_CLEAR) {
        result = sh2_result(sh2_clearTare());
      } else {
        result = sh2_result(sh2_saveDcdNow());
      }
      break;

    case lynx::CMD_SET_CAL: {
      if (p.body_len != 2) {
        result = lynx::ACK_BAD_LENGTH;
        break;
      }
      const uint8_t sensors = r.u8();
      const uint8_t autosave = r.u8();
      if ((sensors & ~(SH2_CAL_ACCEL | SH2_CAL_GYRO | SH2_CAL_MAG)) != 0 || autosave > 1) {
        result = lynx::ACK_BAD_ARG;
      } else if (!st.imu_present) {
        result = lynx::ACK_SENSOR_ERROR;
      } else {
        result = sh2_result(sh2_setCalConfig(sensors));
        if (result == lynx::ACK_OK) {
          result = sh2_result(sh2_setDcdAutoSave(autosave != 0));
        }
      }
      break;
    }

    case lynx::CMD_RESET_IMU:
      if (p.body_len != 0) {
        result = lynx::ACK_BAD_LENGTH;
      } else if (!imu_init()) {
        result = lynx::ACK_SENSOR_ERROR;
      }
      break;

    default:
      result = lynx::ACK_UNKNOWN_CMD;
      break;
  }
  send_ack(p.type, p.seq, result);
}

void service_serial_rx() {
  int budget = 256;
  while (budget-- > 0 && Serial.available() > 0) {
    const int c = Serial.read();
    if (c < 0) {
      break;
    }
    const lynx::FrameReceiver::Status s = rx.feed(static_cast<uint8_t>(c), &rx_packet);
    if (s == lynx::FrameReceiver::Status::Packet) {
      handle_command(rx_packet);
    } else if (s == lynx::FrameReceiver::Status::Error) {
      bump(st.rx_bad);
    }
  }
}

void service_status(uint32_t now_ms) {
  const uint32_t elapsed = now_ms - st.last_status_ms;
  if (elapsed < kStatusPeriodMs) {
    return;
  }
  const uint32_t rate_x10 = st.samples_this_period * 10000UL / elapsed;
  st.rate_x10 = rate_x10 > 0xFFFF ? 0xFFFF : static_cast<uint16_t>(rate_x10);
  st.samples_this_period = 0;
  st.last_status_ms = now_ms;
  send_status();
}

}  // namespace

void setup() {
  Serial.setRxBufferSize(512);
  Serial.setTxBufferSize(2048);
  Serial.begin(SERIAL_BAUD);
#if ARDUINO_USB_CDC_ON_BOOT
  // Native USB: never stall the loop when no host is reading.
  Serial.setTxTimeoutMs(0);
#endif

  pinMode(PIN_RAIL, INPUT_PULLUP);
  pinMode(PIN_BNO_INT, INPUT_PULLUP);
  if (PIN_LED >= 0) {
    pinMode(PIN_LED, OUTPUT);
  }

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, LYNX_I2C_HZ);
  Wire.setTimeOut(50);

  send_hello();
  imu_init();
  st.last_status_ms = millis();
}

void loop() {
  const uint64_t t = now_us();
  service_serial_rx();
  service_rail(t);
  service_imu(t);
  const uint32_t now_ms = millis();
  service_status(now_ms);
  update_led(now_ms);
  // 1 ms tick: rail sampling and SH-2 servicing at ~1 kHz, still far above the 400 Hz max rate.
  delay(1);
}
