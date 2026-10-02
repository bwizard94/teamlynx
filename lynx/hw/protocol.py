"""TeamLynx head-tracker serial protocol v1 (ESP32 <-> host over USB serial).

Normative spec: ``docs/hardware/serial-protocol.md``. Firmware peer:
``firmware/lib/LynxCore/src/lynx_proto.h``.

One frame on the wire::

    0x00 | COBS( version:u8 | type:u8 | seq:u8 | body | crc16:u16le ) | 0x00

``crc16`` is CRC-16/CCITT-FALSE (``binascii.crc_hqx(data, 0xFFFF)``) over ``version..body``.
Multi-byte fields are little-endian; floats are IEEE-754 binary32. The device sends a delimiter
before *and* after every frame so that stray non-protocol bytes (e.g. a library ``println``) are
isolated in their own chunk and cost no valid frames.
"""

from __future__ import annotations

import binascii
import enum
import math
import struct
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Tuple, Type, Union

PROTOCOL_VERSION = 1
HEADER = struct.Struct("<BBB")
CRC = struct.Struct("<H")
MAX_BODY = 64
MAX_PAYLOAD = HEADER.size + MAX_BODY + CRC.size


class MsgType(enum.IntEnum):
    HELLO = 0x01
    IMU = 0x02
    BUTTON = 0x03
    STATUS = 0x04
    ACK = 0x05
    LOG = 0x06
    CMD_HELLO = 0x80
    CMD_SET_REPORT = 0x81
    CMD_TARE = 0x82
    CMD_TARE_PERSIST = 0x83
    CMD_TARE_CLEAR = 0x84
    CMD_SAVE_DCD = 0x85
    CMD_SET_CAL = 0x86
    CMD_RESET_IMU = 0x87


class Report(enum.IntEnum):
    ROTATION_VECTOR = 1  # gyro + accel + mag: absolute heading referenced to magnetic North
    GAME_ROTATION_VECTOR = 2  # gyro + accel: arbitrary heading, immune to magnetic disturbance


class ButtonEvent(enum.IntEnum):
    PRESS = 1
    RELEASE = 2
    SINGLE = 3
    DOUBLE = 4
    LONG = 5


class AckResult(enum.IntEnum):
    OK = 0
    BAD_ARG = 1
    SENSOR_ERROR = 2
    UNKNOWN_CMD = 3
    BAD_LENGTH = 4


class Board(enum.IntEnum):
    UNKNOWN = 0
    ESP32_DEVKITC = 1
    ESP32_S3_DEVKITC = 2


class ImuFlags(enum.IntFlag):
    NONE = 0
    PRESENT = 0x01
    STREAMING = 0x02


class TareAxes(enum.IntFlag):
    X = 1
    Y = 2
    Z = 4
    ALL = 7


class TareBasis(enum.IntEnum):
    ROTATION_VECTOR = 0
    GAME_ROTATION_VECTOR = 1
    GEOMAGNETIC_ROTATION_VECTOR = 2


class CalSensors(enum.IntFlag):
    NONE = 0
    ACCEL = 1
    GYRO = 2
    MAG = 4
    ALL = 7


# ---------------------------------------------------------------------------
# CRC and COBS
# ---------------------------------------------------------------------------


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE. ``crc16(b"123456789") == 0x29B1``."""
    return binascii.crc_hqx(data, 0xFFFF)


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode ``data``; the result contains no zero bytes (delimiter not included)."""
    out = bytearray(b"\x00")
    code_idx = 0
    code = 1
    last = len(data) - 1
    for i, b in enumerate(data):
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            # A full 254-byte block only opens a new block if more input follows (canonical COBS).
            if code == 0xFF and i < last:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


class CobsError(ValueError):
    pass


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        i += 1
        if code == 0:
            raise CobsError("zero byte inside COBS frame")
        end = i + code - 1
        if end > n:
            raise CobsError("COBS code overruns frame")
        block = data[i:end]
        if 0 in block:
            raise CobsError("zero byte inside COBS frame")
        out += block
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class FrameError(ValueError):
    """A received chunk that is not a valid v1 frame."""


class CrcError(FrameError):
    pass


class VersionError(FrameError):
    pass


class LengthError(FrameError):
    pass


@dataclass
class Message:
    TYPE: ClassVar[MsgType]
    BODY: ClassVar[Optional[struct.Struct]] = None
    seq: int = field(default=0, kw_only=True)

    def body_values(self) -> Tuple:
        return ()

    def pack_body(self) -> bytes:
        return self.BODY.pack(*self.body_values()) if self.BODY else b""

    @classmethod
    def unpack_body(cls, body: bytes, seq: int) -> "Message":
        if cls.BODY is None:
            if body:
                raise LengthError(f"{cls.TYPE.name}: expected empty body, got {len(body)} B")
            return cls(seq=seq)
        if len(body) != cls.BODY.size:
            raise LengthError(f"{cls.TYPE.name}: expected {cls.BODY.size} B body, got {len(body)} B")
        return cls(*cls.BODY.unpack(body), seq=seq)


@dataclass
class Hello(Message):
    TYPE: ClassVar[MsgType] = MsgType.HELLO
    BODY: ClassVar[struct.Struct] = struct.Struct("<BBBBBBHI")
    fw_major: int = 0
    fw_minor: int = 0
    fw_patch: int = 0
    board: int = Board.UNKNOWN
    report: int = Report.ROTATION_VECTOR
    imu_flags: int = 0
    rate_hz: int = 0
    uptime_ms: int = 0

    def body_values(self) -> Tuple:
        return (self.fw_major, self.fw_minor, self.fw_patch, int(self.board), int(self.report),
                int(self.imu_flags), self.rate_hz, self.uptime_ms)

    @property
    def fw_version(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}.{self.fw_patch}"


@dataclass
class ImuSample(Message):
    """One fused orientation sample: ``q`` rotates sensor-frame vectors into the sensor's world.

    ``(w, x, y, z)`` is the BNO085 ``(real, i, j, k)``. For the Rotation Vector the sensor world
    is ENU referenced to *magnetic* North; for the Game Rotation Vector it is ENU with an
    arbitrary heading. ``accuracy_rad`` is the sensor's heading-accuracy estimate (NaN for GRV).
    """

    TYPE: ClassVar[MsgType] = MsgType.IMU
    BODY: ClassVar[struct.Struct] = struct.Struct("<Q5fBB")
    t_us: int = 0
    w: float = 1.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    accuracy_rad: float = math.nan
    report: int = Report.ROTATION_VECTOR
    cal_status: int = 0

    def body_values(self) -> Tuple:
        return (self.t_us, self.w, self.x, self.y, self.z, self.accuracy_rad, int(self.report),
                self.cal_status)

    @property
    def quat(self) -> Tuple[float, float, float, float]:
        return (self.w, self.x, self.y, self.z)


@dataclass
class Button(Message):
    """Rail-switch event. ``press_t_us`` is the raw edge time of the gesture's first press."""

    TYPE: ClassVar[MsgType] = MsgType.BUTTON
    BODY: ClassVar[struct.Struct] = struct.Struct("<QQBBH")
    t_us: int = 0
    press_t_us: int = 0
    event: int = ButtonEvent.PRESS
    clicks: int = 0
    hold_ms: int = 0

    def body_values(self) -> Tuple:
        return (self.t_us, self.press_t_us, int(self.event), self.clicks, self.hold_ms)

    def __post_init__(self) -> None:
        try:
            self.event = ButtonEvent(self.event)
        except ValueError:
            pass


@dataclass
class Status(Message):
    TYPE: ClassVar[MsgType] = MsgType.STATUS
    BODY: ClassVar[struct.Struct] = struct.Struct("<IBBHHHHH")
    uptime_ms: int = 0
    imu_flags: int = 0
    report: int = Report.ROTATION_VECTOR
    rate_x10: int = 0
    sensor_resets: int = 0
    watchdog_resets: int = 0
    tx_dropped: int = 0
    rx_bad: int = 0

    def body_values(self) -> Tuple:
        return (self.uptime_ms, int(self.imu_flags), int(self.report), self.rate_x10,
                self.sensor_resets, self.watchdog_resets, self.tx_dropped, self.rx_bad)

    @property
    def rate_hz(self) -> float:
        return self.rate_x10 / 10.0


@dataclass
class Ack(Message):
    TYPE: ClassVar[MsgType] = MsgType.ACK
    BODY: ClassVar[struct.Struct] = struct.Struct("<BBB")
    cmd_type: int = 0
    cmd_seq: int = 0
    result: int = AckResult.OK

    def body_values(self) -> Tuple:
        return (int(self.cmd_type), self.cmd_seq, int(self.result))


@dataclass
class Log(Message):
    TYPE: ClassVar[MsgType] = MsgType.LOG
    text: str = ""

    def pack_body(self) -> bytes:
        return self.text.encode("utf-8")[:MAX_BODY]

    @classmethod
    def unpack_body(cls, body: bytes, seq: int) -> "Log":
        return cls(body.decode("utf-8", errors="replace"), seq=seq)


@dataclass
class CmdHello(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_HELLO


@dataclass
class CmdSetReport(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_SET_REPORT
    BODY: ClassVar[struct.Struct] = struct.Struct("<BH")
    report: int = Report.ROTATION_VECTOR
    rate_hz: int = 100

    def body_values(self) -> Tuple:
        return (int(self.report), self.rate_hz)


@dataclass
class CmdTare(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_TARE
    BODY: ClassVar[struct.Struct] = struct.Struct("<BB")
    axes: int = TareAxes.Z
    basis: int = TareBasis.ROTATION_VECTOR

    def body_values(self) -> Tuple:
        return (int(self.axes), int(self.basis))


@dataclass
class CmdTarePersist(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_TARE_PERSIST


@dataclass
class CmdTareClear(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_TARE_CLEAR


@dataclass
class CmdSaveDcd(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_SAVE_DCD


@dataclass
class CmdSetCal(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_SET_CAL
    BODY: ClassVar[struct.Struct] = struct.Struct("<BB")
    sensors: int = CalSensors.ALL
    dcd_autosave: int = 1

    def body_values(self) -> Tuple:
        return (int(self.sensors), int(bool(self.dcd_autosave)))


@dataclass
class CmdResetImu(Message):
    TYPE: ClassVar[MsgType] = MsgType.CMD_RESET_IMU


@dataclass
class Unknown(Message):
    """Well-formed v1 frame of a type this host does not know (forward compatibility)."""

    TYPE: ClassVar[MsgType] = MsgType.LOG
    msg_type: int = 0
    body: bytes = b""


AnyMessage = Union[Hello, ImuSample, Button, Status, Ack, Log, CmdHello, CmdSetReport, CmdTare,
                   CmdTarePersist, CmdTareClear, CmdSaveDcd, CmdSetCal, CmdResetImu, Unknown]

MESSAGE_TYPES: Dict[int, Type[Message]] = {
    cls.TYPE: cls
    for cls in (Hello, ImuSample, Button, Status, Ack, Log, CmdHello, CmdSetReport, CmdTare,
                CmdTarePersist, CmdTareClear, CmdSaveDcd, CmdSetCal, CmdResetImu)
}


def encode_payload(msg: Message) -> bytes:
    """``version | type | seq | body | crc16`` (before COBS)."""
    body = msg.pack_body()
    if len(body) > MAX_BODY:
        raise LengthError(f"body too long: {len(body)} B")
    head = HEADER.pack(PROTOCOL_VERSION, int(msg.TYPE), msg.seq & 0xFF) + body
    return head + CRC.pack(crc16(head))


def encode_frame(msg: Message) -> bytes:
    """Complete wire frame, leading and trailing ``0x00`` included."""
    return b"\x00" + cobs_encode(encode_payload(msg)) + b"\x00"


def decode_payload(payload: bytes) -> Message:
    if len(payload) < HEADER.size + CRC.size:
        raise LengthError(f"frame too short: {len(payload)} B")
    if len(payload) > MAX_PAYLOAD:
        raise LengthError(f"frame too long: {len(payload)} B")
    head, (want,) = payload[:-CRC.size], CRC.unpack(payload[-CRC.size:])
    if crc16(head) != want:
        raise CrcError("CRC mismatch")
    version, msg_type, seq = HEADER.unpack(head[:HEADER.size])
    if version != PROTOCOL_VERSION:
        raise VersionError(f"unsupported protocol version {version}")
    body = head[HEADER.size:]
    cls = MESSAGE_TYPES.get(msg_type)
    if cls is None:
        return Unknown(msg_type=msg_type, body=bytes(body), seq=seq)
    return cls.unpack_body(bytes(body), seq)


def decode_frame(chunk: bytes) -> Message:
    """Decode one delimiter-stripped COBS chunk."""
    try:
        payload = cobs_decode(chunk)
    except CobsError as exc:
        raise FrameError(str(exc)) from exc
    return decode_payload(payload)


@dataclass
class DecoderStats:
    frames: int = 0
    crc_errors: int = 0
    cobs_errors: int = 0
    version_errors: int = 0
    length_errors: int = 0
    overflows: int = 0
    unknown_types: int = 0

    @property
    def bad_frames(self) -> int:
        return self.crc_errors + self.cobs_errors + self.version_errors + self.length_errors + self.overflows


class FrameDecoder:
    """Streaming decoder: feed arbitrary byte chunks, get complete messages back.

    Corrupt frames are counted in :attr:`stats` and skipped; decoding resynchronises at the next
    ``0x00`` delimiter, so a corrupted or truncated frame never costs more than itself.
    """

    MAX_CHUNK = MAX_PAYLOAD + MAX_PAYLOAD // 254 + 1

    def __init__(self) -> None:
        self._buf = bytearray()
        self._overflow = False
        self.stats = DecoderStats()

    def feed(self, data: bytes) -> List[Message]:
        out: List[Message] = []
        start = 0
        while True:
            idx = data.find(b"\x00", start)
            if idx < 0:
                self._append(data[start:])
                break
            self._append(data[start:idx])
            chunk, overflow = bytes(self._buf), self._overflow
            self._buf.clear()
            self._overflow = False
            start = idx + 1
            if overflow:
                self.stats.overflows += 1
                continue
            if not chunk:
                continue
            msg = self._decode(chunk)
            if msg is not None:
                out.append(msg)
        return out

    def _append(self, part: bytes) -> None:
        if self._overflow:
            return
        if len(self._buf) + len(part) > self.MAX_CHUNK:
            self._overflow = True
            self._buf.clear()
            return
        self._buf += part

    def _decode(self, chunk: bytes) -> Optional[Message]:
        try:
            payload = cobs_decode(chunk)
        except CobsError:
            self.stats.cobs_errors += 1
            return None
        try:
            msg = decode_payload(payload)
        except CrcError:
            self.stats.crc_errors += 1
            return None
        except VersionError:
            self.stats.version_errors += 1
            return None
        except LengthError:
            self.stats.length_errors += 1
            return None
        self.stats.frames += 1
        if isinstance(msg, Unknown):
            self.stats.unknown_types += 1
        return msg
