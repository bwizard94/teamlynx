"""WGS84 geodesy for the staging datum, NMEA sentence parsing and u-blox UBX command frames.

Frames
------
* geodetic: latitude ``phi``, longitude ``lambda`` (degrees, WGS84), ellipsoidal height ``h`` (m).
* ECEF: Earth-centred, Earth-fixed Cartesian metres.
* ENU at the datum: x East, y (true) North, z Up, origin at the datum. This is the TeamLynx world
  frame W when the site's ``north_reference`` is ``"true"``. For a grid-North site plan the ENU
  axes are rotated by the grid convergence ``gamma`` (east positive): a point at true bearing
  ``beta`` has grid bearing ``beta - gamma``, i.e.

      [e_g]   [ cos g  -sin g ] [e]
      [n_g] = [ sin g   cos g ] [n]

  which is :func:`rotate_true_to_grid`. ``lynx.hw.orientation`` uses the same sign for its
  ``convergence_deg`` heading correction, so the IMU heading and the GNSS positions agree.

Formulas (``a`` semi-major axis, ``e^2 = f(2 - f)`` first eccentricity squared):

    N(phi) = a / sqrt(1 - e^2 sin^2 phi)
    X = (N + h) cos phi cos lambda,  Y = (N + h) cos phi sin lambda,  Z = (N (1 - e^2) + h) sin phi

    [e]   [ -sin l          cos l          0     ] [dX]
    [n] = [ -sin p cos l   -sin p sin l    cos p ] [dY]
    [u]   [  cos p cos l    cos p sin l    sin p ] [dZ]

The inverse ECEF -> geodetic uses Bowring's initial value followed by fixed-point iteration on
``phi`` (converges to < 1e-12 rad in 3-4 steps anywhere outside the Earth's core).

Over a 1-2 km field the ENU plane departs from the ellipsoid by d^2 / 2R ~ 8 cm at 1 km, well
below consumer GNSS error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_B = WGS84_A * (1.0 - WGS84_F)
KNOT_MPS = 1852.0 / 3600.0


# --------------------------------------------------------------------------- geodetic transforms


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h: float) -> Tuple[float, float, float]:
    phi, lam = math.radians(lat_deg), math.radians(lon_deg)
    s, c = math.sin(phi), math.cos(phi)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
    return ((n + h) * c * math.cos(lam), (n + h) * c * math.sin(lam), (n * (1.0 - WGS84_E2) + h) * s)


def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    lam = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:  # on the polar axis
        lat = math.copysign(90.0, z)
        return lat, math.degrees(lam), abs(z) - WGS84_B
    ep2 = WGS84_E2 / (1.0 - WGS84_E2)
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    phi = math.atan2(z + ep2 * WGS84_B * math.sin(theta) ** 3, p - WGS84_E2 * WGS84_A * math.cos(theta) ** 3)
    for _ in range(5):
        s = math.sin(phi)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
        h = p / math.cos(phi) - n
        nxt = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + h)))
        if abs(nxt - phi) < 1e-14:
            phi = nxt
            break
        phi = nxt
    s = math.sin(phi)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
    if abs(math.cos(phi)) > 1e-10:
        h = p / math.cos(phi) - n
    else:
        h = abs(z) / abs(s) - n * (1.0 - WGS84_E2)
    return math.degrees(phi), math.degrees(lam), h


def _enu_basis(lat_deg: float, lon_deg: float):
    phi, lam = math.radians(lat_deg), math.radians(lon_deg)
    sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
    east = (-sl, cl, 0.0)
    north = (-sp * cl, -sp * sl, cp)
    up = (cp * cl, cp * sl, sp)
    return east, north, up


def rotate_true_to_grid(e: float, n: float, convergence_deg: float) -> Tuple[float, float]:
    g = math.radians(convergence_deg)
    return (e * math.cos(g) - n * math.sin(g), e * math.sin(g) + n * math.cos(g))


def rotate_grid_to_true(e: float, n: float, convergence_deg: float) -> Tuple[float, float]:
    return rotate_true_to_grid(e, n, -convergence_deg)


@dataclass(frozen=True)
class LocalFrame:
    """Local ENU tangent frame at the staging datum (optionally rotated to grid North)."""

    lat0: float
    lon0: float
    h0: float = 0.0
    convergence_deg: float = 0.0

    def to_enu(self, lat_deg: float, lon_deg: float, h: float) -> Tuple[float, float, float]:
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.h0)
        x, y, z = geodetic_to_ecef(lat_deg, lon_deg, h)
        d = (x - x0, y - y0, z - z0)
        east, north, up = _enu_basis(self.lat0, self.lon0)
        e = sum(a * b for a, b in zip(east, d))
        n = sum(a * b for a, b in zip(north, d))
        u = sum(a * b for a, b in zip(up, d))
        if self.convergence_deg:
            e, n = rotate_true_to_grid(e, n, self.convergence_deg)
        return e, n, u

    def to_geodetic(self, e: float, n: float, u: float) -> Tuple[float, float, float]:
        if self.convergence_deg:
            e, n = rotate_grid_to_true(e, n, self.convergence_deg)
        east, north, up = _enu_basis(self.lat0, self.lon0)
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.h0)
        x = x0 + e * east[0] + n * north[0] + u * up[0]
        y = y0 + e * east[1] + n * north[1] + u * up[1]
        z = z0 + e * east[2] + n * north[2] + u * up[2]
        return ecef_to_geodetic(x, y, z)


# --------------------------------------------------------------------------- NMEA 0183


class NmeaError(ValueError):
    pass


def nmea_checksum(body: str) -> int:
    c = 0
    for ch in body.encode("ascii"):
        c ^= ch
    return c


def nmea_sentence(body: str) -> str:
    """``body`` without ``$`` and ``*``; returns the full sentence with checksum and CRLF."""
    return f"${body}*{nmea_checksum(body):02X}\r\n"


def _split(line: str) -> Tuple[str, List[str]]:
    line = line.strip()
    if not line.startswith("$"):
        raise NmeaError("not an NMEA sentence")
    body, star, cks = line[1:].partition("*")
    if not star:
        raise NmeaError("missing checksum")
    try:
        want = int(cks[:2], 16)
    except ValueError as exc:
        raise NmeaError("bad checksum field") from exc
    if nmea_checksum(body) != want:
        raise NmeaError("checksum mismatch")
    fields = body.split(",")
    if len(fields[0]) != 5:
        raise NmeaError(f"bad address field {fields[0]!r}")
    return fields[0][2:], fields


def _latlon(value: str, hemi: str, deg_digits: int) -> Optional[float]:
    if not value or not hemi:
        return None
    deg = float(value[:deg_digits])
    minutes = float(value[deg_digits:])
    out = deg + minutes / 60.0
    if hemi in ("S", "W"):
        out = -out
    elif hemi not in ("N", "E"):
        raise NmeaError(f"bad hemisphere {hemi!r}")
    return out


def _f(value: str) -> Optional[float]:
    return float(value) if value else None


def _hms(value: str) -> Optional[float]:
    if len(value) < 6:
        return None
    return int(value[0:2]) * 3600 + int(value[2:4]) * 60 + float(value[4:])


@dataclass
class Gga:
    utc_s: Optional[float]
    lat: Optional[float]
    lon: Optional[float]
    quality: int
    num_sats: int
    hdop: Optional[float]
    alt_msl: Optional[float]
    geoid_sep: Optional[float]

    @property
    def h_ellipsoid(self) -> Optional[float]:
        if self.alt_msl is None:
            return None
        return self.alt_msl + (self.geoid_sep or 0.0)

    @property
    def valid(self) -> bool:
        return self.quality > 0 and self.lat is not None and self.lon is not None


@dataclass
class Rmc:
    utc_s: Optional[float]
    valid: bool
    lat: Optional[float]
    lon: Optional[float]
    speed_mps: Optional[float]
    course_deg: Optional[float]
    date: str


@dataclass
class Gst:
    utc_s: Optional[float]
    rms: Optional[float]
    std_lat_m: Optional[float]
    std_lon_m: Optional[float]
    std_alt_m: Optional[float]


GGA_QUALITY = {0: "invalid", 1: "gps", 2: "dgps", 4: "rtk-fixed", 5: "rtk-float", 6: "dead-reckoning"}


def parse_nmea(line: str):
    """Parse GGA, RMC or GST (any talker: GP, GN, GL, GA, GB). Returns None for other sentences."""
    kind, f = _split(line)
    try:
        if kind == "GGA":
            if len(f) < 12:
                raise NmeaError("short GGA")
            return Gga(_hms(f[1]), _latlon(f[2], f[3], 2), _latlon(f[4], f[5], 3), int(f[6] or 0),
                       int(f[7] or 0), _f(f[8]), _f(f[9]), _f(f[11]))
        if kind == "RMC":
            if len(f) < 10:
                raise NmeaError("short RMC")
            spd = _f(f[7])
            return Rmc(_hms(f[1]), f[2] == "A", _latlon(f[3], f[4], 2), _latlon(f[5], f[6], 3),
                       None if spd is None else spd * KNOT_MPS, _f(f[8]), f[9])
        if kind == "GST":
            if len(f) < 9:
                raise NmeaError("short GST")
            return Gst(_hms(f[1]), _f(f[2]), _f(f[6]), _f(f[7]), _f(f[8]))
    except ValueError as exc:
        if isinstance(exc, NmeaError):
            raise
        raise NmeaError(f"bad {kind} field: {exc}") from exc
    return None


def _fmt_latlon(value: float, deg_digits: int, pos: str, neg: str) -> Tuple[str, str]:
    hemi = pos if value >= 0 else neg
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60.0
    if round(minutes, 5) >= 60.0:
        deg, minutes = deg + 1, 0.0
    return f"{deg:0{deg_digits}d}{minutes:08.5f}", hemi


def format_gga(utc_s: float, lat: float, lon: float, alt_msl: float, quality: int = 1, sats: int = 12,
               hdop: float = 0.8, geoid_sep: float = 0.0, talker: str = "GN") -> str:
    hh, rem = divmod(utc_s % 86400.0, 3600.0)
    mm, ss = divmod(rem, 60.0)
    la, lah = _fmt_latlon(lat, 2, "N", "S")
    lo, loh = _fmt_latlon(lon, 3, "E", "W")
    body = (f"{talker}GGA,{int(hh):02d}{int(mm):02d}{ss:05.2f},{la},{lah},{lo},{loh},{quality},{sats:02d},"
            f"{hdop:.2f},{alt_msl:.3f},M,{geoid_sep:.3f},M,,")
    return nmea_sentence(body)


def format_rmc(utc_s: float, lat: float, lon: float, speed_mps: float, course_deg: float,
               date: str = "021026", talker: str = "GN") -> str:
    hh, rem = divmod(utc_s % 86400.0, 3600.0)
    mm, ss = divmod(rem, 60.0)
    la, lah = _fmt_latlon(lat, 2, "N", "S")
    lo, loh = _fmt_latlon(lon, 3, "E", "W")
    body = (f"{talker}RMC,{int(hh):02d}{int(mm):02d}{ss:05.2f},A,{la},{lah},{lo},{loh},"
            f"{speed_mps / KNOT_MPS:.3f},{course_deg % 360.0:.2f},{date},,,A")
    return nmea_sentence(body)


def format_gst(utc_s: float, std_lat: float, std_lon: float, std_alt: float, talker: str = "GN") -> str:
    hh, rem = divmod(utc_s % 86400.0, 3600.0)
    mm, ss = divmod(rem, 60.0)
    rms = math.sqrt(std_lat ** 2 + std_lon ** 2)
    body = (f"{talker}GST,{int(hh):02d}{int(mm):02d}{ss:05.2f},{rms:.2f},{std_lat:.2f},{std_lon:.2f},0.0,"
            f"{std_lat:.2f},{std_lon:.2f},{std_alt:.2f}")
    return nmea_sentence(body)


# --------------------------------------------------------------------------- u-blox UBX


def ubx_frame(cls: int, msg_id: int, payload: bytes = b"") -> bytes:
    """UBX frame: sync 0xB5 0x62, class, id, little-endian length, payload, 8-bit Fletcher checksum."""
    body = bytes([cls & 0xFF, msg_id & 0xFF, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    ck_a = ck_b = 0
    for b in body:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xb5\x62" + body + bytes([ck_a, ck_b])


def ubx_cfg_rate(meas_ms: int, nav_rate: int = 1, time_ref: int = 1) -> bytes:
    """Legacy UBX-CFG-RATE (0x06 0x08): measurement period. u-blox M8 (and M9 compatibility)."""
    if not 25 <= meas_ms <= 65535:
        raise ValueError("measurement period must be 25..65535 ms")
    return ubx_frame(0x06, 0x08, meas_ms.to_bytes(2, "little") + nav_rate.to_bytes(2, "little")
                     + time_ref.to_bytes(2, "little"))


def ubx_cfg_msg_rate(msg_class: int, msg_id: int, rate: int) -> bytes:
    """Legacy UBX-CFG-MSG (0x06 0x01), 3-byte form: output rate on the current port."""
    return ubx_frame(0x06, 0x01, bytes([msg_class, msg_id, rate]))


NMEA_CLASS = 0xF0
NMEA_ID = {"GGA": 0x00, "GLL": 0x01, "GSA": 0x02, "GSV": 0x03, "RMC": 0x04, "VTG": 0x05, "GST": 0x07}

CFG_RATE_MEAS = 0x30210001  # U2, ms (UBX-CFG-VALSET key, M9/M10)


def ubx_valset_u2(key: int, value: int, layers: int = 0x01) -> bytes:
    """UBX-CFG-VALSET (0x06 0x8A) with one U2 item; layers bit 0 = RAM (default, not persisted)."""
    payload = bytes([0x00, layers, 0x00, 0x00]) + key.to_bytes(4, "little") + value.to_bytes(2, "little")
    return ubx_frame(0x06, 0x8A, payload)


def ublox_setup_frames(rate_hz: float = 5.0, generation: str = "m8") -> List[bytes]:
    """Frames that set the navigation rate and trim NMEA output to GGA + RMC + GST.

    ``generation`` "m8" uses legacy CFG-RATE/CFG-MSG; "m10" uses CFG-VALSET for the rate (M10 has
    no legacy CFG messages; enable GST there with u-center's CFG-MSGOUT-NMEA_ID_GST_* keys).
    """
    meas_ms = int(round(1000.0 / rate_hz))
    out: List[bytes] = []
    if generation == "m10":
        out.append(ubx_valset_u2(CFG_RATE_MEAS, meas_ms))
        return out
    out.append(ubx_cfg_rate(meas_ms))
    wanted = {"GGA": 1, "RMC": 1, "GST": 1, "GLL": 0, "GSA": 0, "GSV": 0, "VTG": 0}
    for name, rate in wanted.items():
        out.append(ubx_cfg_msg_rate(NMEA_CLASS, NMEA_ID[name], rate))
    return out


def circular_mean_deg(angles: Sequence[float]) -> Tuple[float, float]:
    """``(mean, circular std)`` in degrees; std = sqrt(-2 ln R)."""
    if not angles:
        raise ValueError("no angles")
    sx = sum(math.sin(math.radians(a)) for a in angles)
    cy = sum(math.cos(math.radians(a)) for a in angles)
    r = math.hypot(sx, cy) / len(angles)
    std = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(r, 1e-12)))))
    return math.degrees(math.atan2(sx, cy)) % 360.0, std


def describe_quality(q: int) -> str:
    return GGA_QUALITY.get(q, f"q{q}")
