import cv2
import numpy as np
import pytest

from lynx.vision.bench_edge import run_benchmark
from lynx.vision.edge import EdgeConfig, EdgeFilter, cuda_available, percentile_stretch


def step_image(h=120, w=160, lo=40, hi=90):
    img = np.full((h, w), lo, np.uint8)
    img[:, w // 2 :] = hi
    return img


@pytest.mark.parametrize("op", ["laplacian", "sobel"])
def test_step_edge_detected_and_flat_regions_clean(op):
    filt = EdgeFilter(EdgeConfig(operator=op, contrast="none"), backend="cpu")
    mask = filt.edge_mask(step_image())
    assert mask.dtype == np.uint8 and set(np.unique(mask)) <= {0, 255}
    cols = (mask > 0).any(axis=0)
    assert cols[76:84].any(), "edge at the step column must be detected"
    assert not cols[:60].any() and not cols[100:].any(), "flat regions must stay clean"


@pytest.mark.parametrize("op,max_frac", [("sobel", 0.01), ("laplacian", 0.05)])
def test_sensor_noise_rejected(op, max_frac):
    # Featureless low-light noise is the worst case for CLAHE (it stretches the
    # noise itself); the second-derivative Laplacian is expected to be noisier.
    rng = np.random.default_rng(1)
    noisy = np.clip(rng.normal(30, 6, (360, 640)), 0, 255).astype(np.uint8)
    mask = EdgeFilter(EdgeConfig(operator=op), backend="cpu").edge_mask(noisy)
    assert (mask > 0).mean() < max_frac


@pytest.mark.parametrize("shape", [(90, 120), (90, 120, 3), (90, 120, 4)])
def test_process_accepts_mono_bgr_bgra(shape):
    frame = np.zeros(shape, np.uint8)
    frame[..., 40:] = 120
    out = EdgeFilter(backend="cpu").process(frame)
    assert out.shape == (90, 120, 3) and out.dtype == np.uint8


def test_tint_and_black_base():
    cfg = EdgeConfig(contrast="none", base_mode="black", tint_bgr=(0, 255, 0), edge_alpha=1.0)
    out = EdgeFilter(cfg, backend="cpu").process(cv2.cvtColor(step_image(), cv2.COLOR_GRAY2BGR))
    edge_px = out[out.sum(axis=2) > 0]
    assert len(edge_px) > 0
    assert (edge_px[:, 1] == 255).all() and (edge_px[:, 0] == 0).all() and (edge_px[:, 2] == 0).all()
    assert out[:, :60].sum() == 0


def test_mono_base_overlay_keeps_scene_visible():
    frame = cv2.cvtColor(step_image(), cv2.COLOR_GRAY2BGR)
    out = EdgeFilter(EdgeConfig(base_mode="mono"), backend="cpu").process(frame)
    assert out[:, 120:].mean() > out[:, :40].mean()  # brighter half stays brighter


def test_otsu_threshold_mode():
    mask = EdgeFilter(EdgeConfig(threshold_mode="otsu", contrast="none"), backend="cpu").edge_mask(step_image())
    assert (mask > 0).any()


def test_percentile_stretch_expands_range():
    rng = np.random.default_rng(0)
    g = rng.integers(100, 140, (100, 100)).astype(np.uint8)
    out = percentile_stretch(g, 1, 99)
    assert out.min() <= 5 and out.max() >= 250


def test_clahe_boosts_low_light_contrast():
    dark = (step_image(lo=10, hi=25)).astype(np.uint8)
    f = EdgeFilter(EdgeConfig(contrast="clahe"), backend="cpu")
    enhanced = f.enhance(dark)
    assert int(enhanced.max()) - int(enhanced.min()) > 15


@pytest.mark.parametrize(
    "bad",
    [dict(operator="canny"), dict(contrast="gamma"), dict(blur_ksize=4), dict(threshold=300), dict(base_mode="x")],
)
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        EdgeFilter(EdgeConfig(**bad))


def test_cuda_request_falls_back_to_cpu_when_unavailable():
    f = EdgeFilter(backend="cuda")
    if not cuda_available():
        assert f.backend == "cpu"
    out = f.process(np.zeros((48, 64, 3), np.uint8))
    assert out.shape == (48, 64, 3)


def test_empty_frame_raises():
    with pytest.raises(ValueError):
        EdgeFilter(backend="cpu").process(np.zeros((0, 0, 3), np.uint8))


def test_benchmark_reports_ms_per_frame():
    r = run_benchmark(320, 180, 5, "cpu", EdgeConfig(), warmup=1)
    assert r["backend"] == "cpu" and r["mean_ms"] > 0 and r["fps"] > 0
    assert r["p50_ms"] <= r["p95_ms"] <= r["max_ms"]
