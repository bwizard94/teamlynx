// Host-side tests for LynxCore: `pio test -e native` (from firmware/).
// The golden frames were produced by lynx/hw/protocol.py, so passing proves byte-exact parity
// between the firmware encoder and the Python decoder.

#include <string.h>
#include <unity.h>

#include "lynx_cobs.h"
#include "lynx_crc.h"
#include "lynx_proto.h"
#include "rail_switch.h"

using namespace lynx;

void setUp() {}
void tearDown() {}

static const uint8_t kGoldenImu[] = {
    0x00, 0x08, 0x01, 0x02, 0x07, 0x15, 0xCD, 0x5B, 0x07, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x02, 0x3F, 0x01, 0x01, 0x02, 0xBF, 0x01, 0x03, 0x80, 0x3E, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x07, 0x80, 0x3D, 0x01, 0x03, 0xF5, 0x8F, 0x00};

static const uint8_t kGoldenButton[] = {
    0x00, 0x07, 0x01, 0x03, 0xFF, 0x80, 0x84, 0x1E, 0x01, 0x01, 0x01, 0x01, 0x04, 0x60,
    0xE3, 0x16, 0x01, 0x01, 0x01, 0x01, 0x04, 0x03, 0x01, 0x50, 0x03, 0x13, 0x27, 0x00};

static const uint8_t kGoldenTareCmd[] = {0x00, 0x05, 0x01, 0x82, 0x09, 0x04, 0x03, 0x58, 0xD9, 0x00};

static void test_crc16_check_value() {
  const uint8_t s[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  TEST_ASSERT_EQUAL_HEX16(0x29B1, crc16(s, sizeof s));
}

static void cobs_roundtrip(const uint8_t* in, size_t n) {
  uint8_t enc[600];
  uint8_t dec[600];
  const size_t m = cobs_encode(in, n, enc, sizeof enc);
  TEST_ASSERT_TRUE(m > 0);
  TEST_ASSERT_TRUE(m <= cobs_max_encoded(n));
  for (size_t i = 0; i < m; ++i) {
    TEST_ASSERT_NOT_EQUAL(0, enc[i]);
  }
  size_t dn = 0;
  TEST_ASSERT_TRUE(cobs_decode(enc, m, dec, sizeof dec, &dn));
  TEST_ASSERT_EQUAL_UINT32(n, dn);
  if (n) {
    TEST_ASSERT_EQUAL_MEMORY(in, dec, n);
  }
}

static void test_cobs_roundtrip_edge_cases() {
  uint8_t buf[520] = {};
  cobs_roundtrip(buf, 0);
  const uint8_t z1[] = {0};
  cobs_roundtrip(z1, 1);
  const uint8_t z2[] = {0, 0};
  cobs_roundtrip(z2, 2);
  const uint8_t mix[] = {0x11, 0x00, 0x00, 0x22, 0x00};
  cobs_roundtrip(mix, sizeof mix);
  const size_t sizes[] = {253, 254, 255, 508, 509, 510};
  for (size_t n : sizes) {
    for (size_t i = 0; i < n; ++i) buf[i] = static_cast<uint8_t>(1 + i % 255);
    cobs_roundtrip(buf, n);
    for (size_t i = 0; i < n; ++i) buf[i] = static_cast<uint8_t>(i % 7 == 0 ? 0 : i);
    cobs_roundtrip(buf, n);
  }
}

static void test_cobs_canonical_254_run() {
  uint8_t in[254];
  for (size_t i = 0; i < sizeof in; ++i) in[i] = static_cast<uint8_t>(i + 1);
  uint8_t out[300];
  TEST_ASSERT_EQUAL_UINT32(255, cobs_encode(in, sizeof in, out, sizeof out));
  TEST_ASSERT_EQUAL_HEX8(0xFF, out[0]);
  TEST_ASSERT_EQUAL_MEMORY(in, out + 1, sizeof in);
}

static void test_cobs_rejects_malformed() {
  uint8_t out[16];
  size_t n = 0;
  const uint8_t overrun[] = {0x05, 0x11, 0x22};
  TEST_ASSERT_FALSE(cobs_decode(overrun, sizeof overrun, out, sizeof out, &n));
  const uint8_t embedded_zero[] = {0x03, 0x11, 0x00};
  TEST_ASSERT_FALSE(cobs_decode(embedded_zero, sizeof embedded_zero, out, sizeof out, &n));
}

static void test_imu_frame_matches_python_golden() {
  uint8_t body[32];
  Writer w(body, sizeof body);
  w.u64(123456789ULL).f32(0.5f).f32(-0.5f).f32(0.25f).f32(0.0f).f32(0.0625f).u8(REPORT_ROTATION_VECTOR).u8(3);
  TEST_ASSERT_TRUE(w.ok());
  TEST_ASSERT_EQUAL_UINT32(30, w.size());
  uint8_t frame[kMaxWireFrame];
  const size_t n = build_frame(MSG_IMU, 7, body, w.size(), frame, sizeof frame);
  TEST_ASSERT_EQUAL_UINT32(sizeof kGoldenImu, n);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kGoldenImu, frame, n);
}

static void test_button_frame_matches_python_golden() {
  uint8_t body[24];
  Writer w(body, sizeof body);
  w.u64(2000000ULL).u64(1500000ULL).u8(BTN_SINGLE).u8(1).u16(80);
  uint8_t frame[kMaxWireFrame];
  const size_t n = build_frame(MSG_BUTTON, 255, body, w.size(), frame, sizeof frame);
  TEST_ASSERT_EQUAL_UINT32(sizeof kGoldenButton, n);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(kGoldenButton, frame, n);
}

static void test_receiver_parses_python_command_and_resyncs() {
  FrameReceiver rx;
  Packet p;
  // Garbage text (like a stray println) before the frame must not cost the frame.
  const char junk[] = "I2C address not found\r\n";
  for (size_t i = 0; i + 1 < sizeof junk; ++i) {
    TEST_ASSERT_TRUE(rx.feed(static_cast<uint8_t>(junk[i]), &p) == FrameReceiver::Status::Pending);
  }
  int packets = 0;
  int errors = 0;
  for (uint8_t b : kGoldenTareCmd) {
    const FrameReceiver::Status s = rx.feed(b, &p);
    if (s == FrameReceiver::Status::Packet) ++packets;
    if (s == FrameReceiver::Status::Error) ++errors;
  }
  TEST_ASSERT_EQUAL(1, errors);  // the junk chunk, terminated by the frame's leading delimiter
  TEST_ASSERT_EQUAL(1, packets);
  TEST_ASSERT_EQUAL_HEX8(CMD_TARE, p.type);
  TEST_ASSERT_EQUAL_UINT8(9, p.seq);
  TEST_ASSERT_EQUAL_UINT32(2, p.body_len);
  TEST_ASSERT_EQUAL_UINT8(4, p.body[0]);
  TEST_ASSERT_EQUAL_UINT8(0, p.body[1]);
}

static void test_receiver_rejects_bad_crc_and_version() {
  uint8_t frame[sizeof kGoldenTareCmd];
  memcpy(frame, kGoldenTareCmd, sizeof frame);
  frame[5] ^= 0x01;  // flip a body bit
  FrameReceiver rx;
  Packet p;
  int errors = 0;
  for (uint8_t b : frame) {
    if (rx.feed(b, &p) == FrameReceiver::Status::Error) ++errors;
  }
  TEST_ASSERT_EQUAL(1, errors);

  uint8_t v2[kMaxWireFrame];
  const uint8_t body[2] = {4, 0};
  size_t n = build_frame(CMD_TARE, 1, body, 2, v2, sizeof v2);
  // Rebuild with version 2 by hand: decode, patch, re-CRC, re-encode.
  uint8_t payload[kMaxPayload];
  size_t plen = 0;
  TEST_ASSERT_TRUE(cobs_decode(v2 + 1, n - 2, payload, sizeof payload, &plen));
  payload[0] = 2;
  const uint16_t crc = crc16(payload, plen - 2);
  payload[plen - 2] = crc & 0xFF;
  payload[plen - 1] = crc >> 8;
  TEST_ASSERT_TRUE(parse_payload(payload, plen, &p) == ParseResult::BadVersion);
}

// ---- rail switch -------------------------------------------------------------------------

struct Sim {
  RailSwitch sw;
  uint64_t t_us = 1000000;
  RailEventRecord log[32];
  size_t n = 0;

  // Hold the raw level for ms milliseconds, sampling at 1 kHz.
  void hold(bool level, uint32_t ms) {
    for (uint32_t i = 0; i < ms; ++i) {
      RailEventRecord ev[RailSwitch::kMaxEventsPerUpdate];
      const size_t k = sw.update(level, t_us, ev, RailSwitch::kMaxEventsPerUpdate);
      for (size_t j = 0; j < k && n < 32; ++j) log[n++] = ev[j];
      t_us += 1000;
    }
  }
  void bounce(bool to, int edges) {
    for (int i = 0; i < edges; ++i) hold(i % 2 == 0 ? to : !to, 1);
  }
  size_t count(RailEvent e) const {
    size_t c = 0;
    for (size_t i = 0; i < n; ++i) c += log[i].type == e;
    return c;
  }
  const RailEventRecord* find(RailEvent e) const {
    for (size_t i = 0; i < n; ++i)
      if (log[i].type == e) return &log[i];
    return nullptr;
  }
};

static void test_rail_single_with_bounce() {
  Sim s;
  s.hold(false, 50);
  const uint64_t press_start = s.t_us;
  s.bounce(true, 5);  // 5 ms of contact bounce, ending pressed
  s.hold(true, 80);
  s.bounce(false, 3);
  s.hold(false, 400);
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Press));
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Release));
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Single));
  TEST_ASSERT_EQUAL(0, s.count(RailEvent::Double));
  const RailEventRecord* e = s.find(RailEvent::Single);
  // press_t is the end of bounce (within 5 ms of first contact), not the debounce decision.
  TEST_ASSERT_TRUE(e->press_t_us >= press_start && e->press_t_us <= press_start + 5000);
  TEST_ASSERT_TRUE(e->hold_ms >= 78 && e->hold_ms <= 90);
  TEST_ASSERT_TRUE(e->t_us - e->press_t_us >= 300000);  // decided after the double-click window
}

static void test_rail_short_glitch_ignored() {
  Sim s;
  s.hold(false, 50);
  s.hold(true, 10);  // shorter than the 20 ms debounce
  s.hold(false, 500);
  TEST_ASSERT_EQUAL_UINT32(0, s.n);
}

static void test_rail_double() {
  Sim s;
  s.hold(false, 50);
  const uint64_t first = s.t_us;
  s.hold(true, 60);
  s.hold(false, 150);
  s.hold(true, 60);
  s.hold(false, 400);
  TEST_ASSERT_EQUAL(2, s.count(RailEvent::Press));
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Double));
  TEST_ASSERT_EQUAL(0, s.count(RailEvent::Single));
  const RailEventRecord* e = s.find(RailEvent::Double);
  TEST_ASSERT_EQUAL_UINT64(first, e->press_t_us);
  TEST_ASSERT_EQUAL_UINT8(2, e->clicks);
}

static void test_rail_two_slow_clicks_are_two_singles() {
  Sim s;
  s.hold(false, 50);
  s.hold(true, 60);
  s.hold(false, 500);
  s.hold(true, 60);
  s.hold(false, 500);
  TEST_ASSERT_EQUAL(2, s.count(RailEvent::Single));
  TEST_ASSERT_EQUAL(0, s.count(RailEvent::Double));
}

static void test_rail_long_fires_while_held() {
  Sim s;
  s.hold(false, 50);
  s.hold(true, 750);
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Long));
  TEST_ASSERT_EQUAL(0, s.count(RailEvent::Release));
  TEST_ASSERT_TRUE(s.sw.pressed());
  s.hold(true, 1000);
  s.hold(false, 500);
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Long));
  TEST_ASSERT_EQUAL(1, s.count(RailEvent::Release));
  TEST_ASSERT_EQUAL(0, s.count(RailEvent::Single));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_crc16_check_value);
  RUN_TEST(test_cobs_roundtrip_edge_cases);
  RUN_TEST(test_cobs_canonical_254_run);
  RUN_TEST(test_cobs_rejects_malformed);
  RUN_TEST(test_imu_frame_matches_python_golden);
  RUN_TEST(test_button_frame_matches_python_golden);
  RUN_TEST(test_receiver_parses_python_command_and_resyncs);
  RUN_TEST(test_receiver_rejects_bad_crc_and_version);
  RUN_TEST(test_rail_single_with_bounce);
  RUN_TEST(test_rail_short_glitch_ignored);
  RUN_TEST(test_rail_double);
  RUN_TEST(test_rail_two_slow_clicks_are_two_singles);
  RUN_TEST(test_rail_long_fires_while_held);
  return UNITY_END();
}
