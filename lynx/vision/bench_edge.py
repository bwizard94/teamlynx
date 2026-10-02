"""Benchmark the EagleEye edge filter in ms/frame.

Usage:
    python -m lynx.vision.bench_edge --width 1280 --height 720 --frames 300
    python -m lynx.vision.bench_edge --backend cuda --operator sobel
    python -m lynx.vision.bench_edge --source path/to/clip.mp4

Synthetic input is a rendered low-light scene so the edge density is
representative of a field feed rather than pure noise.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Dict, List

import cv2
import numpy as np

from lynx.vision.edge import EdgeConfig, EdgeFilter, cuda_available


def _synthetic_frames(width: int, height: int, n: int) -> List[np.ndarray]:
    from lynx.vision.synthetic import SyntheticScene

    scene = SyntheticScene(width=width, height=height, seed=7)
    return [scene.step(1.0 / 30.0).frame for _ in range(n)]


def _video_frames(path: str, width: int, height: int, n: int) -> List[np.ndarray]:
    cap = cv2.VideoCapture(path)
    frames = []
    while len(frames) < n:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(f, (width, height)))
    cap.release()
    if not frames:
        raise SystemExit(f"could not read frames from {path!r}")
    return frames


def run_benchmark(
    width: int,
    height: int,
    frames: int,
    backend: str,
    config: EdgeConfig,
    warmup: int = 10,
    source: str = "",
) -> Dict[str, float]:
    pool_n = min(frames, 60)
    pool = _video_frames(source, width, height, pool_n) if source else _synthetic_frames(width, height, pool_n)
    filt = EdgeFilter(config, backend=backend)
    for i in range(warmup):
        filt.process(pool[i % len(pool)])
    times = []
    for i in range(frames):
        t0 = time.perf_counter()
        filt.process(pool[i % len(pool)])
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    mean = statistics.fmean(times)
    return {
        "backend": filt.backend,
        "width": width,
        "height": height,
        "operator": config.operator,
        "contrast": config.contrast,
        "frames": frames,
        "mean_ms": mean,
        "p50_ms": times[len(times) // 2],
        "p95_ms": times[min(len(times) - 1, int(len(times) * 0.95))],
        "max_ms": times[-1],
        "fps": 1000.0 / mean if mean > 0 else float("inf"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--backend", choices=("auto", "cpu", "cuda"), default="auto")
    ap.add_argument("--operator", choices=("laplacian", "sobel", "both"), default="both")
    ap.add_argument("--contrast", choices=("clahe", "stretch", "none"), default="clahe")
    ap.add_argument("--threads", type=int, default=0, help="cv2.setNumThreads (0 = OpenCV default)")
    ap.add_argument("--source", default="", help="optional video file instead of synthetic frames")
    ap.add_argument("--json", action="store_true", help="print JSON lines")
    args = ap.parse_args(argv)

    if args.threads > 0:
        cv2.setNumThreads(args.threads)
    ops = ("laplacian", "sobel") if args.operator == "both" else (args.operator,)
    print(
        f"# OpenCV {cv2.__version__}, threads={cv2.getNumThreads()}, cuda_available={cuda_available()}",
        file=sys.stderr,
    )
    for op in ops:
        cfg = EdgeConfig(operator=op, contrast=args.contrast)
        r = run_benchmark(args.width, args.height, args.frames, args.backend, cfg, source=args.source)
        if args.json:
            print(json.dumps(r))
        else:
            print(
                f"{r['backend']:>4} {r['width']}x{r['height']} {r['operator']:<9} {r['contrast']:<7} "
                f"mean {r['mean_ms']:6.2f} ms  p50 {r['p50_ms']:6.2f}  p95 {r['p95_ms']:6.2f}  "
                f"max {r['max_ms']:6.2f}  ({r['fps']:.0f} fps)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
