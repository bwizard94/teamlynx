"""``gnss`` headset pose source: head attitude from the IMU, position from a u-blox GNSS.

    lynx-headset --pose "gnss:/dev/lynx-imu?gnss=/dev/lynx-gnss&site=/etc/lynx/site.json&cal=/var/lib/lynx/imu.json"
    lynx-headset --pose "gnss:mock://?gnss=mock://&site=deploy/lynx/site.example.json"

Keys: ``gnss`` (port, required), ``site`` (site file with a datum, required), ``gnss_baud``,
``gnss_rate`` (Hz), ``gnss_gen`` (``m8``/``m10``), ``eye`` (eye height above the datum plane, m;
default the site's ``eye_height_m``), plus every ``serial:`` key (``cal``, ``mount``, ``baud``, ...),
which are passed to :class:`lynx.hw.SerialImuPoseSource`.

Position is the latest GNSS fix converted to the site frame (``lynx.field.geodesy``); height is the
fixed eye height above the flat datum plane (GNSS altitude is 2-3x noisier than horizontal and the
whole HUD assumes ground z = 0). The antenna sits on the helmet within ~10 cm of the eye, which is
ignored. With no fix for ``stale_s`` the last position is held and :attr:`gnss_ok` is False.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode

import numpy as np

from lynx.headset.pose import PoseSourceUnavailableError, register_pose_source
from lynx.hw.headset_source import SPEC_KEYS as SERIAL_KEYS
from lynx.hw.headset_source import SerialImuPoseSource, SerialPoseSample
from lynx.spatial import Pose

from .gnss import GnssFix, GnssReader
from .site import Site

GNSS_KEYS = {"gnss", "site", "gnss_baud", "gnss_rate", "gnss_gen", "eye"}


def parse_gnss_spec(spec: str):
    port, sep, query = spec.partition("?")
    opts = dict(parse_qsl(query, keep_blank_values=True)) if sep else {}
    unknown = set(opts) - GNSS_KEYS - SERIAL_KEYS
    if unknown:
        raise ValueError(f"unknown gnss pose option(s) {sorted(unknown)}")
    for need in ("gnss", "site"):
        if not opts.get(need):
            raise ValueError(f"gnss pose source needs {need}=...: gnss:IMUPORT?gnss=PORT&site=site.json")
    serial_opts = {k: v for k, v in opts.items() if k in SERIAL_KEYS}
    imu_spec = port + (f"?{urlencode(serial_opts)}" if serial_opts else "")
    return imu_spec, opts


class GnssImuPoseSource:
    name = "gnss"

    def __init__(self, spec: str, initial: Pose, imu: Optional[SerialImuPoseSource] = None,
                 gnss: Optional[GnssReader] = None, site: Optional[Site] = None) -> None:
        imu_spec, opts = parse_gnss_spec(spec)
        self.site = site or Site.load(opts["site"])
        self.frame = self.site.frame
        self.eye = float(opts.get("eye", self.site.eye_height_m))
        self.imu = imu or SerialImuPoseSource(imu_spec, initial)
        if gnss is None:
            try:
                gnss = GnssReader.open(opts["gnss"], int(opts.get("gnss_baud", 38400)),
                                       float(opts.get("gnss_rate", 5.0)), opts.get("gnss_gen", "m8")).start()
            except (OSError, RuntimeError) as exc:
                self.imu.close()
                raise PoseSourceUnavailableError(f"lynx.field: cannot open GNSS on {opts['gnss']!r}: {exc}") from exc
        self.gnss = gnss
        self.last_fix: Optional[GnssFix] = None
        self.gnss_ok = False

    def read(self, now: float) -> SerialPoseSample:
        fix = self.gnss.latest()
        self.gnss_ok = fix is not None
        if fix is not None:
            self.last_fix = fix
            e, n, _ = fix.enu(self.frame)
            self.imu.position = np.array([e, n, self.eye])
        return self.imu.read(now)

    def handle_key(self, key: int) -> bool:
        return False

    def close(self) -> None:
        self.gnss.stop()
        self.imu.close()


register_pose_source("gnss", lambda arg, initial: GnssImuPoseSource(arg, initial))
