import json
import math
import struct
import zlib

import pytest

from lynx.net.schema import (
    HEADER,
    MAGIC,
    PROTOCOL_VERSION,
    CancelReason,
    LeaveReason,
    MsgType,
    NodeLeave,
    Ping,
    PingCancel,
    PingType,
    SchemaError,
    SequenceCounter,
    Team,
    Telemetry,
    decode,
    decode_binary,
    decode_json,
    encode,
    seq_newer,
)


def sample_messages():
    return [
        Telemetry(node_id=7, seq=42, ts_us=1_790_000_000_123_456, team=Team.GREEN, flags=0x05, callsign="BRAVO",
                  x=12.5, y=-3.25, z=1.75, heading=271.5, pitch=-12.0, roll=3.5),
        Ping(node_id=3, seq=2**32 - 1, ts_us=5, ping_id=0xDEADBEEF, owner=3, ping_type=PingType.DANGER,
             x=-100.5, y=250.25, z=0.0, ttl_ms=45_000),
        PingCancel(node_id=0, seq=9, ts_us=10, ping_id=17, owner=4, reason=CancelReason.EXPIRED),
        NodeLeave(node_id=0, seq=1, ts_us=11, node=65535, reason=LeaveReason.STALE),
    ]


def test_frame_sizes_are_fixed_and_compact():
    assert HEADER.size == 18
    assert Telemetry.frame_size() == 56
    assert Ping.frame_size() == 46
    assert PingCancel.frame_size() == 29
    assert NodeLeave.frame_size() == 25
    for m in sample_messages():
        assert len(m.to_bytes()) == type(m).frame_size()


def test_header_layout():
    m = sample_messages()[0]
    raw = m.to_bytes()
    magic, ver, mtype, node, seq, ts = HEADER.unpack_from(raw)
    assert (magic, ver, mtype, node, seq, ts) == (MAGIC, PROTOCOL_VERSION, MsgType.TELEMETRY, 7, 42, m.ts_us)
    (crc,) = struct.unpack_from("<I", raw, len(raw) - 4)
    assert crc == zlib.crc32(raw[:-4])


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: type(m).__name__)
def test_binary_round_trip(msg):
    out = decode_binary(msg.to_bytes())
    assert type(out) is type(msg)
    assert out == msg  # all sample floats are exactly representable in float32


@pytest.mark.parametrize("msg", sample_messages(), ids=lambda m: type(m).__name__)
def test_json_round_trip(msg):
    text = msg.to_json()
    obj = json.loads(text)
    assert obj["v"] == PROTOCOL_VERSION
    assert obj["type"] == msg.TYPE.name.lower()
    assert decode_json(text) == msg
    assert decode(text) == msg
    assert decode(text.encode()) == msg


def test_json_uses_readable_enum_names():
    obj = json.loads(sample_messages()[0].to_json())
    assert obj["team"] == "green"
    assert obj["callsign"] == "BRAVO"
    obj = json.loads(sample_messages()[1].to_json())
    assert obj["ping_type"] == "danger"


def test_json_accepts_hand_written_debug_frames():
    m = decode_json('{"v":1,"type":"ping","node_id":2,"owner":2,"ping_id":5,"ping_type":"rally","x":1,"y":2,"z":0,"ttl_ms":1000}')
    assert isinstance(m, Ping)
    assert m.ping_type is PingType.RALLY
    assert isinstance(m.x, float)
    m = decode_json('{"v":1,"type":"telemetry","node_id":4,"team":2,"callsign":"X"}')
    assert m.team is Team.RED


def test_float32_precision_of_positions():
    m = Telemetry(node_id=1, x=123.456789, y=-987.654321, z=1.7, heading=359.99)
    out = decode_binary(m.to_bytes())
    # float32 has a 24-bit mantissa: ~6e-5 m resolution at 1 km, fine for a 1-2 km field
    assert out.x == pytest.approx(m.x, abs=1e-4)
    assert out.y == pytest.approx(m.y, abs=1e-4)


@pytest.mark.parametrize(
    "mutate,err",
    [
        (lambda b: b[:-1], "Telemetry frame must be"),
        (lambda b: b[:10], "too short"),
        (lambda b: b"XX" + b[2:], "bad magic"),
        (lambda b: b[:2] + bytes([9]) + b[3:], "version"),
        (lambda b: b[:3] + bytes([99]) + b[4:], "unknown message type"),
        (lambda b: b[:30] + bytes([b[30] ^ 0xFF]) + b[31:], "CRC"),
    ],
)
def test_corrupt_binary_frames_rejected(mutate, err):
    raw = sample_messages()[0].to_bytes()
    with pytest.raises(SchemaError, match=err):
        decode_binary(mutate(raw))


def _with_valid_crc(payload: bytes) -> bytes:
    return payload + struct.pack("<I", zlib.crc32(payload))


def test_bad_enum_in_valid_frame_rejected():
    raw = bytearray(sample_messages()[0].to_bytes()[:-4])
    raw[HEADER.size] = 200  # team
    with pytest.raises(SchemaError):
        decode_binary(_with_valid_crc(bytes(raw)))


def test_nan_in_valid_frame_rejected():
    m = sample_messages()[0]
    payload = m.to_bytes()[:-4]
    body_off = HEADER.size + 2 + 8
    payload = payload[:body_off] + struct.pack("<f", math.nan) + payload[body_off + 4:]
    with pytest.raises(SchemaError, match="non-finite"):
        decode_binary(_with_valid_crc(payload))


@pytest.mark.parametrize(
    "msg",
    [
        Telemetry(node_id=1, callsign="TOOLONGNAME"),
        Telemetry(node_id=1, callsign="ÄLPHA"),
        Telemetry(node_id=1, x=math.inf),
        Telemetry(node_id=70000),
        Ping(node_id=1, owner=1, ttl_ms=-1),
        Ping(node_id=1, owner=1, ping_id=2**32),
    ],
)
def test_encode_validation(msg):
    with pytest.raises((SchemaError, ValueError)):
        msg.to_bytes()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1,2]",
        '{"v":2,"type":"ping"}',
        '{"v":1,"type":"bogus"}',
        '{"v":1,"type":"ping","nope":1}',
        '{"v":1,"type":"ping","ping_type":"laser"}',
        '{"v":1,"type":"ping","node_id":"7"}',
        '{"v":1,"type":"telemetry","node_id":1,"x":"abc"}',
        '{"v":1,"type":"telemetry","node_id":true}',
    ],
)
def test_bad_json_rejected(text):
    with pytest.raises(SchemaError):
        decode_json(text)


def test_encode_dispatch():
    m = sample_messages()[2]
    assert isinstance(encode(m, "binary"), bytes)
    assert isinstance(encode(m, "json"), str)
    with pytest.raises(ValueError):
        encode(m, "xml")


def test_callsign_padding():
    m = Telemetry(node_id=1, callsign="ABCDEFGH")
    assert decode_binary(m.to_bytes()).callsign == "ABCDEFGH"
    assert decode_binary(Telemetry(node_id=1, callsign="").to_bytes()).callsign == ""


def test_sequence_serial_arithmetic():
    assert seq_newer(1, 0)
    assert not seq_newer(0, 0)
    assert not seq_newer(0, 1)
    assert seq_newer(0, 2**32 - 1)  # wrap-around
    assert seq_newer(5, 2**32 - 10)
    assert not seq_newer(2**32 - 10, 5)


def test_sequence_counter_wraps():
    c = SequenceCounter(2**32 - 1)
    assert c.next() == 2**32 - 1
    assert c.next() == 0
