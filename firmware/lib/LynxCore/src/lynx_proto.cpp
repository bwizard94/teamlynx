#include "lynx_proto.h"

#include <string.h>

#include "lynx_crc.h"

namespace lynx {

Writer& Writer::u8(uint8_t v) {
  if (len_ + 1 > cap_) {
    ok_ = false;
    return *this;
  }
  buf_[len_++] = v;
  return *this;
}

Writer& Writer::u16(uint16_t v) { return u8(v & 0xFF).u8(v >> 8); }

Writer& Writer::u32(uint32_t v) { return u16(v & 0xFFFF).u16(v >> 16); }

Writer& Writer::u64(uint64_t v) {
  return u32(static_cast<uint32_t>(v & 0xFFFFFFFFu)).u32(static_cast<uint32_t>(v >> 32));
}

Writer& Writer::f32(float v) {
  uint32_t bits;
  memcpy(&bits, &v, sizeof bits);
  return u32(bits);
}

Writer& Writer::bytes(const uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; ++i) {
    u8(p[i]);
  }
  return *this;
}

uint8_t Reader::u8() {
  if (pos_ + 1 > len_) {
    ok_ = false;
    return 0;
  }
  return buf_[pos_++];
}

uint16_t Reader::u16() {
  const uint16_t lo = u8();
  const uint16_t hi = u8();
  return static_cast<uint16_t>(lo | (hi << 8));
}

uint32_t Reader::u32() {
  const uint32_t lo = u16();
  const uint32_t hi = u16();
  return lo | (hi << 16);
}

uint64_t Reader::u64() {
  const uint64_t lo = u32();
  const uint64_t hi = u32();
  return lo | (hi << 32);
}

float Reader::f32() {
  const uint32_t bits = u32();
  float v;
  memcpy(&v, &bits, sizeof v);
  return v;
}

size_t build_frame(uint8_t type, uint8_t seq, const uint8_t* body, size_t body_len, uint8_t* out,
                   size_t out_cap) {
  if (body_len > kMaxBody) {
    return 0;
  }
  uint8_t payload[kMaxPayload];
  payload[0] = kProtocolVersion;
  payload[1] = type;
  payload[2] = seq;
  if (body_len > 0) {
    memcpy(payload + kHeaderLen, body, body_len);
  }
  const size_t n = kHeaderLen + body_len;
  const uint16_t crc = crc16(payload, n);
  payload[n] = static_cast<uint8_t>(crc & 0xFF);
  payload[n + 1] = static_cast<uint8_t>(crc >> 8);
  if (out_cap < 2) {
    return 0;
  }
  out[0] = 0x00;
  const size_t enc = cobs_encode(payload, n + kCrcLen, out + 1, out_cap - 2);
  if (enc == 0) {
    return 0;
  }
  out[1 + enc] = 0x00;
  return enc + 2;
}

ParseResult parse_payload(const uint8_t* payload, size_t len, Packet* out) {
  if (len < kHeaderLen + kCrcLen) {
    return ParseResult::TooShort;
  }
  const size_t n = len - kCrcLen;
  const uint16_t want = static_cast<uint16_t>(payload[n] | (payload[n + 1] << 8));
  if (crc16(payload, n) != want) {
    return ParseResult::BadCrc;
  }
  if (payload[0] != kProtocolVersion) {
    return ParseResult::BadVersion;
  }
  const size_t body_len = n - kHeaderLen;
  if (body_len > kMaxBody) {
    return ParseResult::TooLong;
  }
  out->type = payload[1];
  out->seq = payload[2];
  out->body_len = body_len;
  if (body_len > 0) {
    memcpy(out->body, payload + kHeaderLen, body_len);
  }
  return ParseResult::Ok;
}

FrameReceiver::Status FrameReceiver::feed(uint8_t byte, Packet* out) {
  if (byte != 0x00) {
    if (len_ < sizeof buf_) {
      buf_[len_++] = byte;
    } else {
      overflow_ = true;
    }
    return Status::Pending;
  }
  // Delimiter: an empty chunk (back-to-back delimiters) is not an error.
  const size_t n = len_;
  const bool overflow = overflow_;
  len_ = 0;
  overflow_ = false;
  if (n == 0 && !overflow) {
    return Status::Pending;
  }
  uint8_t payload[kMaxPayload];
  size_t plen = 0;
  if (overflow || !cobs_decode(buf_, n, payload, sizeof payload, &plen) ||
      parse_payload(payload, plen, out) != ParseResult::Ok) {
    ++errors_;
    return Status::Error;
  }
  return Status::Packet;
}

}  // namespace lynx
