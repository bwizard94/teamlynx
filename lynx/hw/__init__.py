"""Phase 3 bench-rig hardware: ESP32 + BNO085 head tracker and weapon-rail switch.

* :mod:`lynx.hw.protocol`     framed binary serial protocol v1 (COBS + CRC-16)
* :mod:`lynx.hw.orientation`  sensor quaternion -> Phase 1 heading/pitch/roll (mount, declination, tare)
* :mod:`lynx.hw.health`       link/sensor health monitoring
* :mod:`lynx.hw.serial_link`  threaded, async-friendly serial reader + command client
* :mod:`lynx.hw.mock`         in-process mock device speaking the real protocol
* :mod:`lynx.hw.bridge`       head pose + rail gestures -> ping actions for a HUD/sim loop

CLI: ``python -m lynx.hw --help``. Hardware docs: ``docs/hardware/``.
"""

from .bridge import DEFAULT_GESTURES, ImuHeadSource, RailAction, RailCommand, rail_commands, raycast_ping
from .health import HealthMonitor, HealthThresholds, ImuHealth, LinkState
from .mock import MockImuDevice
from .orientation import (
    MOUNT_PRESETS,
    ImuCalibration,
    MountError,
    OrientationConverter,
    TareError,
    mount_matrix,
    sensor_quat_for_body,
    trim_matrix,
)
from .protocol import AckResult, ButtonEvent, FrameDecoder, Report
from .serial_link import HeadPose, ImuLink, RailEvent, open_transport

# Registers the "serial" pose source for `lynx-headset --pose serial:PORT`.
from .headset_source import SerialImuPoseSource, SerialPoseSample  # noqa: E402

__all__ = [
    "AckResult",
    "ButtonEvent",
    "DEFAULT_GESTURES",
    "FrameDecoder",
    "HeadPose",
    "HealthMonitor",
    "HealthThresholds",
    "ImuCalibration",
    "ImuHeadSource",
    "ImuHealth",
    "ImuLink",
    "LinkState",
    "MOUNT_PRESETS",
    "MockImuDevice",
    "MountError",
    "OrientationConverter",
    "RailAction",
    "RailCommand",
    "RailEvent",
    "Report",
    "SerialImuPoseSource",
    "SerialPoseSample",
    "TareError",
    "mount_matrix",
    "open_transport",
    "rail_commands",
    "raycast_ping",
    "sensor_quat_for_body",
    "trim_matrix",
]
