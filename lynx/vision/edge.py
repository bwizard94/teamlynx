"""EagleEye edge mode: low-light structure enhancement for the HUD feed.

Pipeline (per frame):
    BGR/mono -> gray -> contrast (CLAHE | percentile stretch | none)
             -> Gaussian blur -> edge operator (Laplacian | Sobel magnitude)
             -> threshold (fixed | Otsu) -> tactical tint
             -> weighted overlay onto the (optionally dimmed / mono) base.

A CUDA path (``cv2.cuda``) is used when OpenCV was built with CUDA and a
device is present (Jetson, desktop NVIDIA). Any missing CUDA symbol or
runtime error during initialisation or processing falls back to the CPU path
permanently, so the HUD never drops frames because of the accelerator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger(__name__)

CONTRAST_MODES = ("clahe", "stretch", "none")
EDGE_OPS = ("laplacian", "sobel")
THRESH_MODES = ("fixed", "otsu")
BASE_MODES = ("color", "mono", "black")
# A 3x3 Sobel responds ~2x stronger than the 3x3 Laplacian on blurred step
# edges; scaling its magnitude by 1/2 lets one threshold serve both operators.
SOBEL_NORM = 0.5


@dataclass(frozen=True)
class EdgeConfig:
    contrast: str = "clahe"
    clahe_clip: float = 2.5
    clahe_tiles: int = 8
    stretch_low_pct: float = 1.0
    stretch_high_pct: float = 99.0
    blur_ksize: int = 5
    blur_sigma: float = 1.4
    operator: str = "sobel"
    laplacian_ksize: int = 3
    sobel_ksize: int = 3
    edge_gain: float = 1.0
    threshold_mode: str = "fixed"
    threshold: int = 26  # on the gain-normalised response (Sobel scaled by 1/2)
    tint_bgr: Tuple[int, int, int] = (215, 230, 205)  # white phosphor; keeps team colours distinct
    edge_alpha: float = 0.9
    base_mode: str = "mono"
    base_alpha: float = 0.55
    base_denoised: bool = True  # build the mono base from the blurred image

    def validate(self) -> "EdgeConfig":
        if self.contrast not in CONTRAST_MODES:
            raise ValueError(f"contrast must be one of {CONTRAST_MODES}")
        if self.operator not in EDGE_OPS:
            raise ValueError(f"operator must be one of {EDGE_OPS}")
        if self.threshold_mode not in THRESH_MODES:
            raise ValueError(f"threshold_mode must be one of {THRESH_MODES}")
        if self.base_mode not in BASE_MODES:
            raise ValueError(f"base_mode must be one of {BASE_MODES}")
        if self.blur_ksize < 1 or self.blur_ksize % 2 == 0:
            raise ValueError("blur_ksize must be a positive odd integer")
        if self.laplacian_ksize not in (1, 3, 5, 7):
            raise ValueError("laplacian_ksize must be 1, 3, 5 or 7")
        if self.sobel_ksize not in (1, 3, 5, 7):
            raise ValueError("sobel_ksize must be 1, 3, 5 or 7")
        if not 0 <= self.threshold <= 255:
            raise ValueError("threshold must be in [0, 255]")
        return self


def cuda_available() -> bool:
    """True if this OpenCV build exposes the CUDA filters and sees a device."""
    cuda = getattr(cv2, "cuda", None)
    if cuda is None:
        return False
    needed = (
        "getCudaEnabledDeviceCount",
        "createCLAHE",
        "createGaussianFilter",
        "createLaplacianFilter",
        "createSobelFilter",
        "cvtColor",
        "threshold",
        "addWeighted",
        "magnitude",
    )
    if not all(hasattr(cuda, n) for n in needed) or not hasattr(cv2, "cuda_GpuMat"):
        return False
    try:
        return cuda.getCudaEnabledDeviceCount() > 0
    except cv2.error:
        return False


def _to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _to_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def percentile_stretch(gray: np.ndarray, low_pct: float, high_pct: float) -> np.ndarray:
    """Linear contrast stretch mapping [p_low, p_high] to [0, 255]."""
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    cdf = np.cumsum(hist)
    total = cdf[-1]
    lo = int(np.searchsorted(cdf, total * low_pct / 100.0))
    hi = int(np.searchsorted(cdf, total * high_pct / 100.0))
    if hi <= lo:
        return gray.copy()
    lut = np.clip((np.arange(256, dtype=np.float32) - lo) * (255.0 / (hi - lo)), 0, 255)
    return cv2.LUT(gray, lut.astype(np.uint8))


class EdgeFilter:
    """Stateful edge-mode processor. Reuse one instance per video stream."""

    def __init__(self, config: Optional[EdgeConfig] = None, backend: str = "auto"):
        if backend not in ("auto", "cpu", "cuda"):
            raise ValueError("backend must be 'auto', 'cpu' or 'cuda'")
        self.config = (config or EdgeConfig()).validate()
        self._requested = backend
        self._clahe_cpu = cv2.createCLAHE(
            clipLimit=self.config.clahe_clip,
            tileGridSize=(self.config.clahe_tiles, self.config.clahe_tiles),
        )
        self._tint = np.array(self.config.tint_bgr, dtype=np.float32) / 255.0
        self._gpu = None
        self.backend = "cpu"
        if backend in ("auto", "cuda"):
            if cuda_available():
                try:
                    self._gpu = _CudaEdgeFilter(self.config)
                    self.backend = "cuda"
                except cv2.error as exc:
                    log.warning("CUDA edge filter init failed (%s); using CPU", exc)
            elif backend == "cuda":
                log.warning("CUDA requested but unavailable in this OpenCV build; using CPU")

    def with_config(self, **changes) -> "EdgeFilter":
        return EdgeFilter(replace(self.config, **changes), backend=self._requested)

    # -- stages (CPU) -----------------------------------------------------
    def enhance(self, gray: np.ndarray) -> np.ndarray:
        c = self.config
        if c.contrast == "clahe":
            return self._clahe_cpu.apply(gray)
        if c.contrast == "stretch":
            return percentile_stretch(gray, c.stretch_low_pct, c.stretch_high_pct)
        return gray

    def edge_mask(self, frame: np.ndarray) -> np.ndarray:
        """Binary uint8 edge mask (0/255) for a BGR or mono frame."""
        if self._gpu is not None:
            try:
                return self._gpu.edge_mask(_to_gray(frame))
            except cv2.error as exc:
                self._disable_gpu(exc)
        return self._stages_cpu(_to_gray(frame))[2]

    def _stages_cpu(self, gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (enhanced, blurred, mask)."""
        c = self.config
        enhanced = self.enhance(gray)
        g = enhanced
        if c.blur_ksize > 1:
            g = cv2.GaussianBlur(enhanced, (c.blur_ksize, c.blur_ksize), c.blur_sigma)
        if c.operator == "laplacian":
            resp = cv2.Laplacian(g, cv2.CV_16S, ksize=c.laplacian_ksize)
            mag = cv2.convertScaleAbs(resp, alpha=c.edge_gain)
        else:
            gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=c.sobel_ksize)
            gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=c.sobel_ksize)
            mag = cv2.convertScaleAbs(cv2.magnitude(gx, gy), alpha=c.edge_gain * SOBEL_NORM)
        if c.threshold_mode == "otsu":
            _, mask = cv2.threshold(mag, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        else:
            _, mask = cv2.threshold(mag, c.threshold, 255, cv2.THRESH_BINARY)
        return enhanced, g, mask

    def _base_cpu(self, frame: np.ndarray, enhanced: np.ndarray, blurred: np.ndarray) -> np.ndarray:
        c = self.config
        h, w = frame.shape[:2]
        if c.base_mode == "black":
            return np.zeros((h, w, 3), np.uint8)
        if c.base_mode == "color":
            return cv2.convertScaleAbs(_to_bgr(frame), alpha=c.base_alpha)
        mono = blurred if c.base_denoised else enhanced
        return cv2.convertScaleAbs(cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR), alpha=c.base_alpha)

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Full edge-mode composite (BGR uint8, same size as input)."""
        if frame is None or frame.size == 0:
            raise ValueError("empty frame")
        if frame.dtype != np.uint8:
            frame = cv2.convertScaleAbs(frame)
        if self._gpu is not None:
            try:
                return self._gpu.process(frame)
            except cv2.error as exc:
                self._disable_gpu(exc)
        enhanced, blurred, mask = self._stages_cpu(_to_gray(frame))
        return self.composite(self._base_cpu(frame, enhanced, blurred), mask)

    def composite(self, base: np.ndarray, mask: np.ndarray) -> np.ndarray:
        c = self.config
        tinted = cv2.merge([cv2.convertScaleAbs(mask, alpha=float(t)) for t in self._tint])
        return cv2.addWeighted(base, 1.0, tinted, c.edge_alpha, 0.0)

    def _disable_gpu(self, exc: Exception) -> None:
        log.warning("CUDA edge path failed at runtime (%s); falling back to CPU", exc)
        self._gpu = None
        self.backend = "cpu"


class _CudaEdgeFilter:
    """cv2.cuda implementation of the same pipeline. Created only if available."""

    def __init__(self, config: EdgeConfig):
        self.c = config
        cuda = cv2.cuda
        self.stream = cuda.Stream()
        self.clahe = cuda.createCLAHE(
            clipLimit=config.clahe_clip, tileGridSize=(config.clahe_tiles, config.clahe_tiles)
        )
        k = config.blur_ksize
        self.blur = (
            cuda.createGaussianFilter(cv2.CV_8UC1, cv2.CV_8UC1, (k, k), config.blur_sigma)
            if k > 1
            else None
        )
        if config.operator == "laplacian":
            self.lap = cuda.createLaplacianFilter(
                cv2.CV_32FC1, cv2.CV_32FC1, ksize=config.laplacian_ksize
            )
        else:
            self.sx = cuda.createSobelFilter(cv2.CV_32FC1, cv2.CV_32FC1, 1, 0, config.sobel_ksize)
            self.sy = cuda.createSobelFilter(cv2.CV_32FC1, cv2.CV_32FC1, 0, 1, config.sobel_ksize)
        self.g_in = cv2.cuda_GpuMat()
        tint = np.array(config.tint_bgr, dtype=np.float64) / 255.0
        self.tint = tint

    def _enhance(self, g_gray):
        c = self.c
        if c.contrast == "clahe":
            return self.clahe.apply(g_gray, self.stream)
        if c.contrast == "stretch":
            # Percentile computation needs a histogram; do it on the host and
            # apply the LUT on device to keep the heavy filters on the GPU.
            host = g_gray.download(self.stream)
            self.stream.waitForCompletion()
            out = cv2.cuda_GpuMat()
            out.upload(percentile_stretch(host, c.stretch_low_pct, c.stretch_high_pct), self.stream)
            return out
        return g_gray

    def _mask(self, g_gray):
        c = self.c
        enhanced = self._enhance(g_gray)
        g = enhanced
        if self.blur is not None:
            g = self.blur.apply(g, stream=self.stream)
        gf = g.convertTo(cv2.CV_32FC1, stream=self.stream)
        if c.operator == "laplacian":
            resp = self.lap.apply(gf, stream=self.stream)
            mag = cv2.cuda.abs(resp, stream=self.stream)
        else:
            gx = self.sx.apply(gf, stream=self.stream)
            gy = self.sy.apply(gf, stream=self.stream)
            mag = cv2.cuda.magnitude(gx, gy, stream=self.stream)
        gain = c.edge_gain * (SOBEL_NORM if c.operator == "sobel" else 1.0)
        mag8 = mag.convertTo(cv2.CV_8UC1, alpha=gain, stream=self.stream)
        if c.threshold_mode == "otsu":
            host = mag8.download(self.stream)
            self.stream.waitForCompletion()
            t, _ = cv2.threshold(host, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        else:
            t = c.threshold
        _, mask = cv2.cuda.threshold(mag8, float(t), 255.0, cv2.THRESH_BINARY, stream=self.stream)
        return enhanced, g, mask

    def edge_mask(self, gray: np.ndarray) -> np.ndarray:
        self.g_in.upload(gray, self.stream)
        _, _, mask = self._mask(self.g_in)
        out = mask.download(self.stream)
        self.stream.waitForCompletion()
        return out

    def process(self, frame: np.ndarray) -> np.ndarray:
        c = self.c
        self.g_in.upload(frame, self.stream)
        if frame.ndim == 2:
            g_gray, g_bgr = self.g_in, cv2.cuda.cvtColor(self.g_in, cv2.COLOR_GRAY2BGR, stream=self.stream)
        else:
            code = cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY
            g_gray = cv2.cuda.cvtColor(self.g_in, code, stream=self.stream)
            g_bgr = (
                cv2.cuda.cvtColor(self.g_in, cv2.COLOR_BGRA2BGR, stream=self.stream)
                if frame.shape[2] == 4
                else self.g_in
            )
        enhanced, blurred, mask = self._mask(g_gray)
        if c.base_mode == "black":
            base = cv2.cuda_GpuMat(mask.size(), cv2.CV_8UC3, (0, 0, 0))
        elif c.base_mode == "color":
            base = g_bgr.convertTo(cv2.CV_8UC3, alpha=c.base_alpha, stream=self.stream)
        else:
            src = blurred if c.base_denoised else enhanced
            mono = cv2.cuda.cvtColor(src, cv2.COLOR_GRAY2BGR, stream=self.stream)
            base = mono.convertTo(cv2.CV_8UC3, alpha=c.base_alpha, stream=self.stream)
        mask3 = cv2.cuda.cvtColor(mask, cv2.COLOR_GRAY2BGR, stream=self.stream)
        tinted = cv2.cuda.multiply(
            mask3, (float(self.tint[0]), float(self.tint[1]), float(self.tint[2]), 0.0),
            stream=self.stream,
        )
        out = cv2.cuda.addWeighted(base, 1.0, tinted, c.edge_alpha, 0.0, stream=self.stream)
        host = out.download(self.stream)
        self.stream.waitForCompletion()
        return host
