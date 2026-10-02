"""TeamLynx wire schema v1: compact fixed-size binary frames plus a JSON debug encoding.

Every binary frame is little-endian (native for x86, ARM and ESP32 - no byte swapping on any
target) and laid out as::

    +--------------------+---------------------+-----------+
    | header (18 bytes)  | body (type-specific)| CRC32 (4) |
    +--------------------+---------------------+-----------+

Header ``<2sBBHIQ``:

    offset size field
    0      2    magic      b"LX"
    2      1    version    PROTOCOL_VERSION (1)
    3      1    msg_type   MsgType
    4      2    node_id    sender node id (u16, 0 = relay server)
    6      4    seq        per-sender sequence number (u32, wraps; serial-number arithmetic)
    10     8    ts_us      sender wall-clock timestamp, microseconds since Unix epoch (u64)

The CRC32 (zlib polynomial) covers header + body. WebSocket/TCP already guarantees integrity, but
the same frames are intended for serial and UDP/radio links in later phases.

Bodies:

    TELEMETRY   <BB8s6f>  team, flags, callsign[8], x, y, z (m, ENU), heading, pitch, roll (deg)
                                                             -> 34 bytes, frame = 56 bytes
    PING        <IHBB3fI> ping_id, owner, ping_type, flags, x, y, z (m, ENU), ttl_ms
                                                             -> 24 bytes, frame = 46 bytes
    PING_CANCEL <IHB>     ping_id, owner, reason             ->  7 bytes, frame = 29 bytes
    NODE_LEAVE  <HB>      node, reason                       ->  3 bytes, frame = 25 bytes

Timestamps are informational (latency display, logging) because offline nodes have no common
clock. All liveness and TTL decisions are made on the *receiver's* monotonic clock. ``ttl_ms`` is
therefore a relative duration "remaining at the time of sending"; the relay rewrites it to the
remaining TTL when replaying active pings to late joiners.
"""

from __future__ import annotations

import enum
import json
import math
import struct
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Dict, Type, Union

PROTOCOL_VERSION = 1
MAGIC = b"LX"
CALLSIGN_LEN = 8
SEQ_MOD = 1 << 32

HEADER = struct.Struct("<2sBBHIQ")
CRC = struct.Struct("<I")

SERVER_NODE_ID = 0
MAX_NODE_ID = 0xFFFF


class SchemaError(ValueError):
    """Raised for malformed, truncated, corrupt or unsupported frames."""


class MsgType(enum.IntEnum):
    TELEMETRY = 1
    PING = 2
    PING_CANCEL = 3
    NODE_LEAVE = 4


class Team(enum.IntEnum):
    BLUE = 0
    GREEN = 1
    RED = 2
    AMBER = 3


TEAM_RGB: Dict[Team, tuple[int, int, int]] = {
    Team.BLUE: (60, 140, 255),
    Team.GREEN: (60, 220, 90),
    Team.RED: (240, 60, 60),
    Team.AMBER: (255, 180, 40),
}


class PingType(enum.IntEnum):
    MARK = 0  # generic "look here"
    CONTACT = 1  # unverified contact / suspected tango
    MOVE = 2  # move to / objective
    DANGER = 3  # danger area, avoid
    RALLY = 4  # rally point


class CancelReason(enum.IntEnum):
    OWNER = 0  # owner withdrew the ping
    EXPIRED = 1  # TTL elapsed (emitted by the relay)
    REPLACED = 2  # owner exceeded the per-node active ping limit; oldest evicted


class LeaveReason(enum.IntEnum):
    DISCONNECT = 0
    STALE = 1  # no telemetry within the stale timeout


class TelemetryFlags(enum.IntFlag):
    NONE = 0
    PING_SWITCH = 0x01  # rail switch currently held
    LOW_BATTERY = 0x02
    IMU_DEGRADED = 0x04  # IMU calibration/accuracy below threshold


def now_us() -> int:
    return time.time_ns() // 1000


def seq_newer(a: int, b: int) -> bool:
    """RFC 1982 serial-number comparison: True if ``a`` is strictly newer than ``b`` (mod 2^32)."""
    diff = (a - b) % SEQ_MOD
    return 0 < diff < (SEQ_MOD >> 1)


def _encode_callsign(callsign: str) -> bytes:
    raw = callsign.encode("ascii", errors="replace")[:CALLSIGN_LEN]
    return raw.ljust(CALLSIGN_LEN, b"\x00")


def _decode_callsign(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _check_finite(*values: float) -> None:
    for v in values:
        if not math.isfinite(v):
            raise SchemaError("non-finite float in message")


def _check_u(name: str, value: int, bits: int) -> None:
    if not 0 <= value < (1 << bits):
        raise SchemaError(f"{name}={value} out of range for u{bits}")


@dataclass
class Header:
    node_id: int
    seq: int = 0
    ts_us: int = 0


@dataclass
class Message:
    """Base class. Subclasses define ``TYPE`` and ``BODY`` and their body fields."""

    TYPE: ClassVar[MsgType]
    BODY: ClassVar[struct.Struct]

    node_id: int = 0
    seq: int = 0
    ts_us: int = 0

    # -- binary --------------------------------------------------------------
    def _body_values(self) -> tuple:
        raise NotImplementedError

    @classmethod
    def _from_body(cls, header: Header, values: tuple) -> "Message":
        raise NotImplementedError

    def validate(self) -> None:
        _check_u("node_id", self.node_id, 16)
        _check_u("seq", self.seq, 32)
        _check_u("ts_us", self.ts_us, 64)

    def to_bytes(self) -> bytes:
        self.validate()
        payload = HEADER.pack(MAGIC, PROTOCOL_VERSION, int(self.TYPE), self.node_id, self.seq, self.ts_us)
        payload += self.BODY.pack(*self._body_values())
        return payload + CRC.pack(zlib.crc32(payload) & 0xFFFFFFFF)

    @classmethod
    def frame_size(cls) -> int:
        return HEADER.size + cls.BODY.size + CRC.size

    # -- JSON ----------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        out: Dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": self.TYPE.name.lower(),
            "node_id": d.pop("node_id"),
            "seq": d.pop("seq"),
            "ts_us": d.pop("ts_us"),
        }
        for k, v in d.items():
            if isinstance(v, enum.Enum):
                v = v.name.lower()
            out[k] = v
        return out

    def to_json(self) -> str:
        self.validate()
        return json.dumps(self.to_dict(), separators=(",", ":"))


@dataclass
class Telemetry(Message):
    TYPE: ClassVar[MsgType] = MsgType.TELEMETRY
    BODY: ClassVar[struct.Struct] = struct.Struct("<BB8s6f")

    team: Team = Team.BLUE
    flags: int = 0
    callsign: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    heading: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    def validate(self) -> None:
        super().validate()
        self.team = Team(self.team)
        _check_u("flags", int(self.flags), 8)
        _check_finite(self.x, self.y, self.z, self.heading, self.pitch, self.roll)
        try:
            self.callsign.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SchemaError("callsign must be ASCII") from exc
        if len(self.callsign) > CALLSIGN_LEN:
            raise SchemaError(f"callsign longer than {CALLSIGN_LEN} characters")

    def _body_values(self) -> tuple:
        return (
            int(self.team),
            int(self.flags),
            _encode_callsign(self.callsign),
            self.x,
            self.y,
            self.z,
            self.heading,
            self.pitch,
            self.roll,
        )

    @classmethod
    def _from_body(cls, header: Header, values: tuple) -> "Telemetry":
        team, flags, cs, x, y, z, h, p, r = values
        return cls(header.node_id, header.seq, header.ts_us, Team(team), flags, _decode_callsign(cs), x, y, z, h, p, r)

    @property
    def position(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass
class Ping(Message):
    TYPE: ClassVar[MsgType] = MsgType.PING
    BODY: ClassVar[struct.Struct] = struct.Struct("<IHBB3fI")

    ping_id: int = 0
    owner: int = 0
    ping_type: PingType = PingType.MARK
    flags: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    ttl_ms: int = 30_000

    def validate(self) -> None:
        super().validate()
        self.ping_type = PingType(self.ping_type)
        _check_u("ping_id", self.ping_id, 32)
        _check_u("owner", self.owner, 16)
        _check_u("flags", int(self.flags), 8)
        _check_u("ttl_ms", self.ttl_ms, 32)
        _check_finite(self.x, self.y, self.z)

    def _body_values(self) -> tuple:
        return (self.ping_id, self.owner, int(self.ping_type), int(self.flags), self.x, self.y, self.z, self.ttl_ms)

    @classmethod
    def _from_body(cls, header: Header, values: tuple) -> "Ping":
        pid, owner, ptype, flags, x, y, z, ttl = values
        return cls(header.node_id, header.seq, header.ts_us, pid, owner, PingType(ptype), flags, x, y, z, ttl)

    @property
    def key(self) -> tuple[int, int]:
        """Globally unique ping identity: ``(owner, ping_id)``."""
        return (self.owner, self.ping_id)

    @property
    def position(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass
class PingCancel(Message):
    TYPE: ClassVar[MsgType] = MsgType.PING_CANCEL
    BODY: ClassVar[struct.Struct] = struct.Struct("<IHB")

    ping_id: int = 0
    owner: int = 0
    reason: CancelReason = CancelReason.OWNER

    def validate(self) -> None:
        super().validate()
        self.reason = CancelReason(self.reason)
        _check_u("ping_id", self.ping_id, 32)
        _check_u("owner", self.owner, 16)

    def _body_values(self) -> tuple:
        return (self.ping_id, self.owner, int(self.reason))

    @classmethod
    def _from_body(cls, header: Header, values: tuple) -> "PingCancel":
        pid, owner, reason = values
        return cls(header.node_id, header.seq, header.ts_us, pid, owner, CancelReason(reason))

    @property
    def key(self) -> tuple[int, int]:
        return (self.owner, self.ping_id)


@dataclass
class NodeLeave(Message):
    TYPE: ClassVar[MsgType] = MsgType.NODE_LEAVE
    BODY: ClassVar[struct.Struct] = struct.Struct("<HB")

    node: int = 0
    reason: LeaveReason = LeaveReason.DISCONNECT

    def validate(self) -> None:
        super().validate()
        self.reason = LeaveReason(self.reason)
        _check_u("node", self.node, 16)

    def _body_values(self) -> tuple:
        return (self.node, int(self.reason))

    @classmethod
    def _from_body(cls, header: Header, values: tuple) -> "NodeLeave":
        node, reason = values
        return cls(header.node_id, header.seq, header.ts_us, node, LeaveReason(reason))


AnyMessage = Union[Telemetry, Ping, PingCancel, NodeLeave]

MESSAGE_TYPES: Dict[MsgType, Type[Message]] = {
    MsgType.TELEMETRY: Telemetry,
    MsgType.PING: Ping,
    MsgType.PING_CANCEL: PingCancel,
    MsgType.NODE_LEAVE: NodeLeave,
}

def decode_binary(data: bytes) -> AnyMessage:
    """Decode and validate one binary frame. Raises :class:`SchemaError` on any defect."""
    if len(data) < HEADER.size + CRC.size:
        raise SchemaError(f"frame too short ({len(data)} bytes)")
    magic, version, mtype, node_id, seq, ts_us = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise SchemaError("bad magic")
    if version != PROTOCOL_VERSION:
        raise SchemaError(f"unsupported protocol version {version}")
    try:
        cls = MESSAGE_TYPES[MsgType(mtype)]
    except ValueError as exc:
        raise SchemaError(f"unknown message type {mtype}") from exc
    expected = cls.frame_size()
    if len(data) != expected:
        raise SchemaError(f"{cls.__name__} frame must be {expected} bytes, got {len(data)}")
    (crc,) = CRC.unpack_from(data, expected - CRC.size)
    if crc != (zlib.crc32(data[: expected - CRC.size]) & 0xFFFFFFFF):
        raise SchemaError("CRC mismatch")
    values = cls.BODY.unpack_from(data, HEADER.size)
    try:
        msg = cls._from_body(Header(node_id, seq, ts_us), values)
        msg.validate()
    except ValueError as exc:  # bad enum value, non-finite float
        raise SchemaError(str(exc)) from exc
    return msg  # type: ignore[return-value]


def decode_json(text: Union[str, bytes]) -> AnyMessage:
    """Decode the JSON debug encoding produced by :meth:`Message.to_json`."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SchemaError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SchemaError("JSON message must be an object")
    if obj.get("v") != PROTOCOL_VERSION:
        raise SchemaError(f"unsupported protocol version {obj.get('v')!r}")
    tname = obj.get("type")
    try:
        mtype = MsgType[str(tname).upper()]
    except KeyError as exc:
        raise SchemaError(f"unknown message type {tname!r}") from exc
    cls = MESSAGE_TYPES[mtype]
    enum_fields: Dict[str, Type[enum.IntEnum]] = {"team": Team, "ping_type": PingType}
    if cls is PingCancel:
        enum_fields["reason"] = CancelReason
    elif cls is NodeLeave:
        enum_fields["reason"] = LeaveReason
    kwargs: Dict[str, Any] = {}
    allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    for key, value in obj.items():
        if key in ("v", "type"):
            continue
        if key not in allowed:
            raise SchemaError(f"unexpected field {key!r} for {mtype.name}")
        if key in enum_fields and isinstance(value, str):
            try:
                value = enum_fields[key][value.upper()]
            except KeyError as exc:
                raise SchemaError(f"invalid {key} {value!r}") from exc
        kwargs[key] = value
    try:
        msg = cls(**kwargs)
        for name in ("x", "y", "z", "heading", "pitch", "roll"):
            if hasattr(msg, name):
                setattr(msg, name, float(getattr(msg, name)))
        for name in ("node_id", "seq", "ts_us", "ping_id", "owner", "ttl_ms", "flags", "node"):
            if hasattr(msg, name):
                val = getattr(msg, name)
                if isinstance(val, bool) or not isinstance(val, int):
                    raise SchemaError(f"{name} must be an integer")
        msg.validate()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError(str(exc)) from exc
    return msg  # type: ignore[return-value]


def decode(data: Union[bytes, bytearray, memoryview, str]) -> AnyMessage:
    """Decode either encoding: ``str`` -> JSON, bytes -> binary (or JSON if it starts with '{')."""
    if isinstance(data, str):
        return decode_json(data)
    data = bytes(data)
    if data[:1] == b"{":
        return decode_json(data)
    return decode_binary(data)


def encode(msg: Message, encoding: str = "binary") -> Union[bytes, str]:
    if encoding == "binary":
        return msg.to_bytes()
    if encoding == "json":
        return msg.to_json()
    raise ValueError(f"unknown encoding {encoding!r}")


@dataclass
class SequenceCounter:
    """Per-sender u32 sequence generator with wrap-around."""

    value: int = field(default=0)

    def next(self) -> int:
        v = self.value
        self.value = (self.value + 1) % SEQ_MOD
        return v
