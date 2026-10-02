#pragma once

#include <stddef.h>
#include <stdint.h>

namespace lynx {

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final XOR.
// Check value: crc16("123456789") == 0x29B1. Python: binascii.crc_hqx(data, 0xFFFF).
uint16_t crc16(const uint8_t* data, size_t len, uint16_t crc = 0xFFFF);

}  // namespace lynx
