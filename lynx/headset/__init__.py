"""Integrated headset client: relay telemetry/pings + vision pipeline + HUD on one camera model."""

from lynx.headset.pose import (
    KeyboardPoseSource,
    PoseSample,
    PoseSource,
    PoseSourceUnavailableError,
    StaticPoseSource,
    create_pose_source,
    register_pose_source,
)

__all__ = [
    "KeyboardPoseSource",
    "PoseSample",
    "PoseSource",
    "PoseSourceUnavailableError",
    "StaticPoseSource",
    "create_pose_source",
    "register_pose_source",
]
