#pragma once

#include <stddef.h>
#include <stdint.h>

namespace lynx {

// Worst-case COBS output size for an input of n bytes (no trailing delimiter).
constexpr size_t cobs_max_encoded(size_t n) { return n + n / 254 + 1; }

// Consistent Overhead Byte Stuffing (Cheshire & Baker 1999). The output contains no 0x00 bytes.
// Returns the number of bytes written, or 0 if out_cap is too small.
size_t cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap);

// Inverse of cobs_encode on one frame (delimiter already stripped).
// Returns false on a malformed frame (embedded zero, code overruns input) or overflow.
bool cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_cap, size_t* out_len);

}  // namespace lynx
