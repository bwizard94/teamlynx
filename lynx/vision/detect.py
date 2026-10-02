"""Person / vehicle detection with Ultralytics YOLOv8n or YOLO11n.

The detector never fabricates detections: if ``ultralytics`` is not installed
or the weights cannot be loaded, construction raises
:class:`DetectorUnavailableError` with an actionable message. Callers (the
demo, the HUD client) decide whether to continue without detection.

Weights accepted by ``ultralytics.YOLO``:
    * ``yolo11n.pt`` / ``yolov8n.pt``  - auto-downloaded on first use if the
      machine has internet; on the field network pre-stage them locally.
    * ``*.onnx``                        - CPU / onnxruntime-gpu.
    * ``*.engine``                      - TensorRT on Jetson (built on-device).

See :func:`export_model` and ``docs/vision-pipeline.md`` for Jetson export.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

from lynx.vision.types import Detection

log = logging.getLogger(__name__)

PERSON_NAMES = frozenset({"person"})
VEHICLE_NAMES = frozenset({"bicycle", "car", "motorcycle", "bus", "truck", "train", "boat"})
# COCO indices for the same classes; used only if a model exposes no names.
COCO_PERSON_IDS = frozenset({0})
COCO_VEHICLE_IDS = frozenset({1, 2, 3, 5, 6, 7, 8})
AUTO_DOWNLOAD_NAMES = frozenset(
    {"yolo11n.pt", "yolov8n.pt", "yolo11s.pt", "yolov8s.pt"}
)


class DetectorUnavailableError(RuntimeError):
    """Raised when YOLO inference cannot be set up on this machine."""


def category_for(class_id: int, class_name: str) -> Optional[str]:
    name = (class_name or "").lower()
    if name in PERSON_NAMES or (not name and class_id in COCO_PERSON_IDS):
        return "person"
    if name in VEHICLE_NAMES or (not name and class_id in COCO_VEHICLE_IDS):
        return "vehicle"
    return None


def _import_yolo():
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DetectorUnavailableError(
            "ultralytics is not installed. Install with `pip install ultralytics` "
            "(on Jetson install the JetPack PyTorch wheel first, then "
            "`pip install ultralytics --no-deps` plus its pure-Python deps)."
        ) from exc
    return YOLO


class YoloDetector:
    """Thin wrapper over ``ultralytics.YOLO`` returning :class:`Detection` objects."""

    def __init__(
        self,
        weights: str = "yolo11n.pt",
        conf: float = 0.35,
        iou: float = 0.5,
        imgsz: int = 640,
        device: Optional[str] = None,
        half: bool = False,
        categories: Iterable[str] = ("person", "vehicle"),
    ):
        self.weights = weights
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self.device = device
        self.half = half
        self.categories = frozenset(categories)
        unknown = self.categories - {"person", "vehicle"}
        if unknown:
            raise ValueError(f"unsupported categories: {sorted(unknown)}")

        if not os.path.exists(weights) and os.path.basename(weights) not in AUTO_DOWNLOAD_NAMES:
            raise DetectorUnavailableError(
                f"weights file {weights!r} not found. Use an auto-downloadable name "
                f"({', '.join(sorted(AUTO_DOWNLOAD_NAMES))}) or pre-stage the file."
            )
        YOLO = _import_yolo()
        try:
            self.model = YOLO(weights, task="detect")
        except Exception as exc:  # ultralytics raises a variety of types on download/load
            raise DetectorUnavailableError(
                f"could not load YOLO weights {weights!r}: {exc}. If offline, copy the "
                "weights file onto the device and pass its path."
            ) from exc
        names = getattr(self.model, "names", None) or {}
        self.names: Dict[int, str] = {int(k): str(v) for k, v in dict(names).items()}
        self.class_ids: List[int] = [
            i for i, n in self.names.items() if category_for(i, n) in self.categories
        ]
        if self.names and not self.class_ids:
            raise DetectorUnavailableError(
                f"model {weights!r} has no person/vehicle classes (names={self.names})"
            )

    def detect(self, frame: np.ndarray) -> List[Detection]:
        """Run inference on a BGR uint8 frame."""
        kwargs = dict(conf=self.conf, iou=self.iou, imgsz=self.imgsz, verbose=False)
        if self.half:
            kwargs["half"] = True
        if self.device is not None:
            kwargs["device"] = self.device
        if self.class_ids:
            kwargs["classes"] = self.class_ids
        results = self.model.predict(frame, **kwargs)
        return self._convert(results[0]) if results else []

    def _convert(self, result) -> List[Detection]:
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = _to_numpy(boxes.xyxy)
        confs = _to_numpy(boxes.conf)
        clss = _to_numpy(boxes.cls).astype(int)
        out: List[Detection] = []
        for (x1, y1, x2, y2), c, k in zip(xyxy, confs, clss):
            name = self.names.get(int(k), "")
            cat = category_for(int(k), name)
            if cat is None or cat not in self.categories:
                continue
            out.append(Detection(float(x1), float(y1), float(x2), float(y2), float(c), cat, name))
        return out


def _to_numpy(t) -> np.ndarray:
    if hasattr(t, "cpu"):
        t = t.cpu()
    if hasattr(t, "numpy"):
        return t.numpy()
    return np.asarray(t)


def export_model(
    weights: str = "yolo11n.pt",
    fmt: str = "onnx",
    imgsz: int = 640,
    half: bool = False,
    int8: bool = False,
    dynamic: bool = False,
    workspace_gb: Optional[float] = None,
) -> str:
    """Export weights to ONNX or TensorRT (``fmt='engine'``) and return the path.

    TensorRT engines are tied to the GPU, TensorRT and JetPack version that
    built them: always run the ``engine`` export on the target Jetson.
    """
    if fmt not in ("onnx", "engine"):
        raise ValueError("fmt must be 'onnx' or 'engine'")
    YOLO = _import_yolo()
    model = YOLO(weights, task="detect")
    kwargs = dict(format=fmt, imgsz=imgsz, half=half, int8=int8, dynamic=dynamic)
    if fmt == "onnx":
        kwargs["simplify"] = True
    if workspace_gb is not None and fmt == "engine":
        kwargs["workspace"] = workspace_gb
    return str(model.export(**kwargs))


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import time

    import cv2

    ap = argparse.ArgumentParser(description="Run or export the TeamLynx YOLO detector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="detect on an image/video and print ms/frame")
    r.add_argument("source")
    r.add_argument("--weights", default="yolo11n.pt")
    r.add_argument("--device", default=None)
    r.add_argument("--imgsz", type=int, default=640)
    r.add_argument("--frames", type=int, default=100)
    e = sub.add_parser("export", help="export to ONNX / TensorRT")
    e.add_argument("--weights", default="yolo11n.pt")
    e.add_argument("--format", choices=("onnx", "engine"), default="onnx")
    e.add_argument("--imgsz", type=int, default=640)
    e.add_argument("--half", action="store_true")
    e.add_argument("--int8", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "export":
            print(export_model(args.weights, args.format, args.imgsz, args.half, args.int8))
            return 0
        det = YoloDetector(args.weights, imgsz=args.imgsz, device=args.device)
    except DetectorUnavailableError as exc:
        print(f"detector unavailable: {exc}")
        return 2

    still = cv2.imread(args.source) if os.path.isfile(args.source) else None
    cap = None if still is not None else cv2.VideoCapture(int(args.source) if args.source.isdigit() else args.source)
    times = []
    n = 0
    while n < args.frames:
        if still is not None:
            frame = still
        else:
            ok, frame = cap.read()
            if not ok:
                break
        t0 = time.perf_counter()
        dets = det.detect(frame)
        times.append((time.perf_counter() - t0) * 1000.0)
        n += 1
        if n == 1:
            for d in dets:
                print(f"  {d.category:<7} {d.class_name:<10} conf={d.confidence:.2f} box={tuple(round(v) for v in d.as_xyxy())}")
    if cap is not None:
        cap.release()
    if not times:
        print("no frames read")
        return 1
    warm = times[1:] or times
    print(f"{len(times)} frames, mean {sum(warm) / len(warm):.1f} ms/frame (excluding first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
