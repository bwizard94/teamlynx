#pragma once

// TeamLynx head-tracker serial protocol v1 (device <-> host over USB serial).
// Normative spec: docs/hardware/serial-protocol.md. Python peer: lynx/hw/protocol.py.
//
// Wire format of one frame:
//   0x00 | COBS( version:u8 | type:u8 | seq:u8 | body | crc16:u16le ) | 0x00
// crc16 is CRC-16/CCITT-FALSE over version..body. All multi-byte fields are little-endian,
// floats are IEEE-754 binary32.

#include <stddef.h>
#include <stdint.h>

#include "lynx_cobs.h"

namespace lynx {

constexpr uint8_t kProtocolVersion = 1;
constexpr size_t kHeaderLen = 3;
constexpr size_t kCrcLen = 2;
constexpr size_t kMaxBody = 64;
constexpr size_t kMaxPayload = kHeaderLen + kMaxBody + kCrcLen;
constexpr size_t kMaxWireFrame = cobs_max_encoded(kMaxPayload) + 2;

enum MsgType : uint8_t {
  // device -> host
  MSG_HELLO = 0x01,   // <BBBBBBHI> fw_major fw_minor fw_patch board report imu_flags rate_hz uptime_ms
  MSG_IMU = 0x02,     // <Q5fBB>    t_us w x y z accuracy_rad report cal_status
  MSG_BUTTON = 0x03,  // <QQBBH>    t_us press_t_us event clicks hold_ms
  MSG_STATUS = 0x04,  // <IBBHHHHH> uptime_ms imu_flags report rate_x10 sensor_resets watchdog_resets tx_dropped rx_bad
  MSG_ACK = 0x05,     // <BBB>      cmd_type cmd_seq result
  MSG_LOG = 0x06,     // utf-8 text
  // host -> device
  CMD_HELLO = 0x80,         // (empty)  -> HELLO + ACK
  CMD_SET_REPORT = 0x81,    // <BH> report rate_hz
  CMD_TARE = 0x82,          // <BB> axes(bit0 X, bit1 Y, bit2 Z) basis(0 RV, 1 GRV, 2 geomag RV)
  CMD_TARE_PERSIST = 0x83,  // (empty)
  CMD_TARE_CLEAR = 0x84,    // (empty)
  CMD_SAVE_DCD = 0x85,      // (empty)
  CMD_SET_CAL = 0x86,       // <BB> sensors(bit0 accel, bit1 gyro, bit2 mag) dcd_autosave
  CMD_RESET_IMU = 0x87,     // (empty)
};

enum Report : uint8_t {
  REPORT_ROTATION_VECTOR = 1,       // gyro + accel + mag, absolute heading (magnetic North)
  REPORT_GAME_ROTATION_VECTOR = 2,  // gyro + accel only, heading arbitrary but no mag disturbance
};

enum ButtonEvent : uint8_t {
  BTN_PRESS = 1,
  BTN_RELEASE = 2,
  BTN_SINGLE = 3,
  BTN_DOUBLE = 4,
  BTN_LONG = 5,
};

enum AckResult : uint8_t {
  ACK_OK = 0,
  ACK_BAD_ARG = 1,
  ACK_SENSOR_ERROR = 2,
  ACK_UNKNOWN_CMD = 3,
  ACK_BAD_LENGTH = 4,
};

enum Board : uint8_t {
  BOARD_UNKNOWN = 0,
  BOARD_ESP32_DEVKITC = 1,
  BOARD_ESP32_S3_DEVKITC = 2,
};

enum ImuFlags : uint8_t {
  IMU_PRESENT = 0x01,    // sensor answered at boot / last re-init
  IMU_STREAMING = 0x02,  // a sample arrived in the last 200 ms
};

// Little-endian body builder over a caller-owned buffer. Overflow latches ok() == false.
class Writer {
 public:
  Writer(uint8_t* buf, size_t cap) : buf_(buf), cap_(cap) {}
  Writer& u8(uint8_t v);
  Writer& u16(uint16_t v);
  Writer& u32(uint32_t v);
  Writer& u64(uint64_t v);
  Writer& f32(float v);
  Writer& bytes(const uint8_t* p, size_t n);
  size_t size() const { return len_; }
  bool ok() const { return ok_; }

 private:
  uint8_t* buf_;
  size_t cap_;
  size_t len_ = 0;
  bool ok_ = true;
};

class Reader {
 public:
  Reader(const uint8_t* buf, size_t len) : buf_(buf), len_(len) {}
  uint8_t u8();
  uint16_t u16();
  uint32_t u32();
  uint64_t u64();
  float f32();
  size_t remaining() const { return len_ - pos_; }
  bool ok() const { return ok_; }

 private:
  const uint8_t* buf_;
  size_t len_;
  size_t pos_ = 0;
  bool ok_ = true;
};

// Builds a complete wire frame (leading and trailing 0x00 included) into out.
// Returns its length, or 0 if the body is too long or out_cap too small.
size_t build_frame(uint8_t type, uint8_t seq, const uint8_t* body, size_t body_len, uint8_t* out,
                   size_t out_cap);

struct Packet {
  uint8_t type = 0;
  uint8_t seq = 0;
  uint8_t body[kMaxBody] = {};
  size_t body_len = 0;
};

enum class ParseResult : uint8_t { Ok, TooShort, BadCrc, BadVersion, TooLong };

// Validates version and CRC of one COBS-decoded payload.
ParseResult parse_payload(const uint8_t* payload, size_t len, Packet* out);

// Byte-at-a-time receiver: collects bytes until a 0x00 delimiter, COBS-decodes and parses.
class FrameReceiver {
 public:
  enum class Status : uint8_t { Pending, Packet, Error };
  // Returns Packet when *out was filled, Error for a bad frame, Pending otherwise.
  Status feed(uint8_t byte, Packet* out);
  uint32_t errors() const { return errors_; }

 private:
  uint8_t buf_[cobs_max_encoded(kMaxPayload)] = {};
  size_t len_ = 0;
  bool overflow_ = false;
  uint32_t errors_ = 0;
};

}  // namespace lynx
