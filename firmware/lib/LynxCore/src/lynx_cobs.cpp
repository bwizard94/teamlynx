#include "lynx_cobs.h"

namespace lynx {

size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap) {
  if (out_cap < cobs_max_encoded(len)) {
    return 0;
  }
  size_t code_idx = 0;
  size_t w = 1;
  uint8_t code = 1;
  for (size_t i = 0; i < len; ++i) {
    if (in[i] == 0) {
      out[code_idx] = code;
      code_idx = w++;
      code = 1;
    } else {
      out[w++] = in[i];
      // A full 254-byte block only opens a new block if more input follows (canonical COBS).
      if (++code == 0xFF && i + 1 < len) {
        out[code_idx] = code;
        code_idx = w++;
        code = 1;
      }
    }
  }
  out[code_idx] = code;
  return w;
}

bool cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap, size_t* out_len) {
  size_t r = 0;
  size_t w = 0;
  while (r < len) {
    const uint8_t code = in[r++];
    if (code == 0) {
      return false;
    }
    for (uint8_t i = 1; i < code; ++i) {
      if (r >= len || in[r] == 0 || w >= out_cap) {
        return false;
      }
      out[w++] = in[r++];
    }
    if (code != 0xFF && r < len) {
      if (w >= out_cap) {
        return false;
      }
      out[w++] = 0;
    }
  }
  *out_len = w;
  return true;
}

}  // namespace lynx
