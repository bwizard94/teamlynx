"""Serial protocol v1: CRC, COBS, message codecs, streaming decoder robustness."""

import math
import random

import pytest

from lynx.hw.protocol import (
    MESSAGE_TYPES,
    Ack,
    Button,
    ButtonEvent,
    CmdHello,
    CmdResetImu,
    CmdSaveDcd,
    CmdSetCal,
    CmdSetReport,
    CmdTare,
    CmdTareClear,
    CmdTarePersist,
    CobsError,
    CrcError,
    FrameDecoder,
    Hello,
    ImuSample,
    LengthError,
    Log,
    Status,
    Unknown,
    VersionError,
    cobs_decode,
    cobs_encode,
    crc16,
    decode_frame,
    encode_frame,
    encode_payload,
)

# Same bytes as firmware/test/test_core/test_main.cpp: pins Python and C++ to one wire format.
GOLDEN_IMU = bytes([
    0x00, 0x08, 0x01, 0x02, 0x07, 0x15, 0xCD, 0x5B, 0x07, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x02, 0x3F, 0x01, 0x01, 0x02, 0xBF, 0x01, 0x03, 0x80, 0x3E, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x07, 0x80, 0x3D, 0x01, 0x03, 0xF5, 0x8F, 0x00])
GOLDEN_BUTTON = bytes([
    0x00, 0x07, 0x01, 0x03, 0xFF, 0x80, 0x84, 0x1E, 0x01, 0x01, 0x01, 0x01, 0x04, 0x60,
    0xE3, 0x16, 0x01, 0x01, 0x01, 0x01, 0x04, 0x03, 0x01, 0x50, 0x03, 0x13, 0x27, 0x00])
GOLDEN_TARE = bytes([0x00, 0x05, 0x01, 0x82, 0x09, 0x04, 0x03, 0x58, 0xD9, 0x00])

SAMPLES = [
    Hello(fw_major=1, fw_minor=2, fw_patch=3, board=2, report=2, imu_flags=3, rate_hz=100, uptime_ms=123456, seq=1),
    ImuSample(t_us=2**40 + 5, w=0.7071, x=0.0, y=-0.7071, z=0.0, accuracy_rad=0.05, report=1, cal_status=2, seq=2),
    Button(t_us=10, press_t_us=5, event=ButtonEvent.DOUBLE, clicks=2, hold_ms=65535, seq=3),
    Status(uptime_ms=1, imu_flags=1, report=1, rate_x10=1000, sensor_resets=2, watchdog_resets=3,
           tx_dropped=4, rx_bad=5, seq=4),
    Ack(cmd_type=0x82, cmd_seq=200, result=1, seq=5),
    Log(text="BNO085 at 0x4A \u00b0", seq=6),
    CmdHello(seq=7),
    CmdSetReport(report=2, rate_hz=400, seq=8),
    CmdTare(axes=7, basis=1, seq=9),
    CmdTarePersist(seq=10),
    CmdTareClear(seq=11),
    CmdSaveDcd(seq=12),
    CmdSetCal(sensors=5, dcd_autosave=0, seq=13),
    CmdResetImu(seq=14),
]


def test_crc16_check_value():
    assert crc16(b"123456789") == 0x29B1


@pytest.mark.parametrize("n", [0, 1, 2, 253, 254, 255, 256, 508, 509, 510, 1000])
def test_cobs_roundtrip_and_no_zeros(n):
    rng = random.Random(n)
    for data in (bytes(n), bytes(rng.randrange(256) for _ in range(n)), bytes(1 + i % 255 for i in range(n))):
        enc = cobs_encode(data)
        assert 0 not in enc
        assert len(enc) <= n + n // 254 + 1
        assert cobs_decode(enc) == data


def test_cobs_known_vectors():
    # Vectors from the COBS paper / Wikipedia.
    assert cobs_encode(b"\x00") == b"\x01\x01"
    assert cobs_encode(b"\x00\x00") == b"\x01\x01\x01"
    assert cobs_encode(b"\x11\x22\x00\x33") == b"\x03\x11\x22\x02\x33"
    assert cobs_encode(b"\x11\x00\x00\x00") == b"\x02\x11\x01\x01\x01"
    assert cobs_encode(bytes(range(1, 255))) == b"\xff" + bytes(range(1, 255))
    assert cobs_encode(b"\x00" + bytes(range(1, 255))) == b"\x01\xff" + bytes(range(1, 255))
    assert cobs_encode(bytes(range(1, 256))) == b"\xff" + bytes(range(1, 255)) + b"\x02\xff"


def test_cobs_rejects_malformed():
    with pytest.raises(CobsError):
        cobs_decode(b"\x05\x11\x22")
    with pytest.raises(CobsError):
        cobs_decode(b"\x03\x11\x00")


def test_every_message_type_has_a_sample():
    assert {type(m) for m in SAMPLES} == set(MESSAGE_TYPES.values())


@pytest.mark.parametrize("msg", SAMPLES, ids=lambda m: type(m).__name__)
def test_roundtrip_every_message(msg):
    frame = encode_frame(msg)
    assert frame[0] == 0 and frame[-1] == 0 and 0 not in frame[1:-1]
    out = decode_frame(frame[1:-1])
    assert type(out) is type(msg)
    for k, v in vars(msg).items():
        got = getattr(out, k)
        if isinstance(v, float):
            assert got == pytest.approx(v, rel=1e-6)
        else:
            assert got == v, k


def test_golden_frames_match_firmware():
    imu = ImuSample(t_us=123456789, w=0.5, x=-0.5, y=0.25, z=0.0, accuracy_rad=0.0625, report=1, cal_status=3, seq=7)
    assert encode_frame(imu) == GOLDEN_IMU
    btn = Button(t_us=2_000_000, press_t_us=1_500_000, event=ButtonEvent.SINGLE, clicks=1, hold_ms=80, seq=255)
    assert encode_frame(btn) == GOLDEN_BUTTON
    assert encode_frame(CmdTare(axes=4, basis=0, seq=9)) == GOLDEN_TARE
    assert decode_frame(GOLDEN_IMU[1:-1]) == imu


def test_imu_nan_accuracy_survives():
    m = decode_frame(encode_frame(ImuSample(accuracy_rad=math.nan, report=2))[1:-1])
    assert math.isnan(m.accuracy_rad)


def test_decode_errors_are_typed():
    payload = bytearray(encode_payload(CmdTare(axes=4)))
    bad_crc = bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    with pytest.raises(CrcError):
        decode_frame(cobs_encode(bad_crc))
    v2 = bytearray(payload[:-2])
    v2[0] = 2
    v2 += crc16(bytes(v2)).to_bytes(2, "little")
    with pytest.raises(VersionError):
        decode_frame(cobs_encode(bytes(v2)))
    short = bytearray(payload[:-3])  # drop one body byte, re-CRC
    short += crc16(bytes(short)).to_bytes(2, "little")
    with pytest.raises(LengthError):
        decode_frame(cobs_encode(bytes(short)))


def test_unknown_type_is_forward_compatible():
    head = bytes([1, 0x42, 3, 0xAA, 0xBB])
    msg = decode_frame(cobs_encode(head + crc16(head).to_bytes(2, "little")))
    assert isinstance(msg, Unknown) and msg.msg_type == 0x42 and msg.body == b"\xaa\xbb"


def _stream():
    return b"".join(encode_frame(m) for m in SAMPLES)


def test_decoder_handles_any_chunking():
    data = _stream()
    rng = random.Random(1)
    for _ in range(20):
        dec = FrameDecoder()
        out, i = [], 0
        while i < len(data):
            n = rng.randint(1, 40)
            out += dec.feed(data[i:i + n])
            i += n
        assert [type(m) for m in out] == [type(m) for m in SAMPLES]
        assert dec.stats.bad_frames == 0


def test_decoder_resyncs_after_junk_and_corruption():
    good = [encode_frame(ImuSample(t_us=i, seq=i)) for i in range(5)]
    corrupt = bytearray(good[2])
    corrupt[10] ^= 0x40
    stream = (b"I2C address not found\r\n" + good[0] + good[1] + bytes(corrupt) + good[3]
              + good[4][: len(good[4]) // 2])
    dec = FrameDecoder()
    out = dec.feed(stream)
    assert [m.t_us for m in out] == [0, 1, 3]
    assert dec.stats.bad_frames == 2  # the text chunk and the bit-flipped frame
    # Truncated frame followed by a fresh frame: the partial one is discarded at its delimiter.
    out = dec.feed(b"\x00" + good[4])
    assert [m.t_us for m in out] == [4]


def test_decoder_overflow_recovers():
    dec = FrameDecoder()
    assert dec.feed(b"\x55" * 5000) == []
    out = dec.feed(encode_frame(CmdHello()))
    assert dec.stats.overflows == 1
    assert len(out) == 1 and isinstance(out[0], CmdHello)
