"""Camera sources for the headset client: webcam, video file, or synthetic scene.

Every source returns a :class:`CameraFrame` per call to :meth:`CameraSource.read`. Sources that
know scene ground truth (only :class:`SyntheticSource`) also return simulated detector output so
CI exercises IFF without YOLO weights; real cameras always go through the YOLO detector.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Tuple

import cv2
import numpy as np

from lynx.vision.types import CameraModel, Detection, OperatorPose, TeammateTrack


@dataclass
class CameraFrame:
    image: np.ndarray
    camera: CameraModel
    sim_detections: Optional[List[Detection]] = None
    aux: Optional[np.ndarray] = None
    aux_label: str = ""


class CameraSource(Protocol):
    name: str

    def read(self, pose: OperatorPose, teammates: Sequence[TeammateTrack]) -> Optional[CameraFrame]: ...

    def close(self) -> None: ...


class CaptureSource:
    """OpenCV ``VideoCapture`` on a camera index or a video path.

    ``hfov_deg`` is the lens HFOV at the delivered resolution; it is the only intrinsic the HUD
    needs for an ideal (undistorted, centred) pinhole. Video files loop when ``loop`` is set.
    """

    def __init__(
        self,
        source: str,
        hfov_deg: float,
        vfov_deg: Optional[float] = None,
        width: int = 0,
        height: int = 0,
        loop: bool = True,
    ) -> None:
        self.is_device = source.isdigit()
        self.name = f"cam{source}" if self.is_device else "video"
        self.cap = cv2.VideoCapture(int(source) if self.is_device else source)
        if not self.cap.isOpened():
            raise SystemExit(f"cannot open video source {source!r}")
        if width and height:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.hfov_deg = hfov_deg
        self.vfov_deg = vfov_deg
        self.loop = loop and not self.is_device
        self.camera: Optional[CameraModel] = None

    def read(self, pose: OperatorPose, teammates: Sequence[TeammateTrack]) -> Optional[CameraFrame]:
        ok, frame = self.cap.read()
        if not ok and self.loop:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if not ok:
            return None
        h, w = frame.shape[:2]
        if self.camera is None or (self.camera.width, self.camera.height) != (w, h):
            self.camera = CameraModel(w, h, self.hfov_deg, self.vfov_deg)
        return CameraFrame(frame, self.camera)

    def close(self) -> None:
        self.cap.release()


class SyntheticSource:
    """Low-light synthetic compound rendered from the live headset pose.

    Teammates are drawn standing where their relay telemetry puts them (telemetry ``z`` is eye
    height; figures stand on the ground plane). The scene's scripted non-friendly patrols are
    added on top, so the HUD shows both FRIENDLY and UNVERIFIED/TANGO contacts.
    """

    name = "synthetic"

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        hfov_deg: float = 78.0,
        seed: int = 0,
        unknowns: bool = True,
        det_jitter: float = 0.03,
        det_dropout: float = 0.06,
    ) -> None:
        from lynx.vision.synthetic import SyntheticScene, default_people

        people = [p for p in default_people() if not p.friendly] if unknowns else []
        self.scene = SyntheticScene(
            width, height, hfov_deg, seed=seed, people=people, det_jitter=det_jitter, det_dropout=det_dropout
        )
        self.camera = self.scene.camera
        self._t0 = time.monotonic()

    def people(self, teammates: Sequence[TeammateTrack]) -> List[Tuple[str, bool, float, float]]:
        out = self.scene.people_at(self.scene.t)
        out += [(tm.callsign, True, tm.x, tm.y) for tm in teammates]
        return out

    def read(self, pose: OperatorPose, teammates: Sequence[TeammateTrack]) -> Optional[CameraFrame]:
        self.scene.t = time.monotonic() - self._t0
        people = self.people(teammates)
        frame, dets, _ = self.scene.render_view(pose, people)
        aux = self.scene.render_topdown((320, 240), people=people, center=(pose.x, pose.y))
        return CameraFrame(frame, self.camera, dets, aux, "AUX: UAV")

    def close(self) -> None:
        pass


def open_camera_source(
    source: str,
    width: int,
    height: int,
    hfov_deg: float,
    vfov_deg: Optional[float] = None,
    seed: int = 0,
    unknowns: bool = True,
) -> CameraSource:
    if source == "synthetic":
        return SyntheticSource(width, height, hfov_deg, seed=seed, unknowns=unknowns)
    return CaptureSource(source, hfov_deg, vfov_deg, width, height)
