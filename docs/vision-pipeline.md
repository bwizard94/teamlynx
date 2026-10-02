# TeamLynx Phase 2 — Vision & HUD Pipeline

Phase 2 covers the on-headset software path from the camera frame to the
rendered HUD: the EagleEye edge mode, YOLO person/vehicle detection, the IFF
step that matches detections to squad telemetry, and the overlay renderer.
None of it needs hardware. A synthetic scene generator stands in for the
camera and detector in CI.

```
camera ──► EdgeFilter (optional) ─────────────────────────────┐
   │                                                          ▼
   └────► YoloDetector ─► IffAssociator ◄── teammate telemetry │
                              │  tracks + expected teammates   │
                              ▼                                ▼
               HudRenderer(frame, HudState) ◄── pose, pings, aux feed
```

## Module map

| Path | Purpose |
|---|---|
| `lynx/vision/types.py` | Self-contained dataclasses: `OperatorPose`, `TeammateTrack`, `CameraModel`, `Detection`, `IffTrack`, `ExpectedTeammate`, `IffStatus` |
| `lynx/vision/geometry.py` | Rotation from the ENU world frame to the camera frame, pinhole projection, pixel→angle, edge-arrow placement |
| `lynx/vision/edge.py` | EagleEye edge filter (CPU and `cv2.cuda`) |
| `lynx/vision/bench_edge.py` | Edge-filter benchmark (ms/frame) |
| `lynx/vision/detect.py` | Ultralytics YOLOv8n / YOLO11n wrapper, CLI, ONNX/TensorRT export |
| `lynx/vision/iff.py` | Tracker, teammate gating, Hungarian assignment, hysteresis |
| `lynx/vision/synthetic.py` | Deterministic low-light scene with simulated detector output |
| `lynx/hud/` | `HudRenderer`, `HudState`, widgets, palette, `demo.py` |
| `tests/vision`, `tests/hud` | pytest suite (69 tests) |

## Interface contract (wiring to `lynx.spatial` / `lynx.net`)

The vision and HUD packages never import `lynx.spatial` or `lynx.net`, so
both could be built at the same time. Once Phase 1 lands, each relay message
maps field-for-field onto these types:

| Phase 2 type | Fields | Source |
|---|---|---|
| `OperatorPose` | `x, y, z` (m), `yaw, pitch, roll` (deg), `node_id`, `callsign`, `team_color` | local IMU + datum position |
| `TeammateTrack` | `node_id, callsign, x, y, z` (headset position, m), `team_color`, `timestamp` (s, same clock as `now`), `yaw` | relay telemetry |
| `WorldPing` | `ping_id, x, y, z, label, owner, color` | relay ping events |
| `ScreenPing` | `u, v` (px), `label`, `range_m`, `behind` | for when `lynx.spatial` has already projected the ping |

Frame conventions. If `lynx.spatial` uses something different, convert at the adapter:

- **World:** local ENU tangent plane at the staging-area datum: +X east, +Y north, +Z up, in metres.
- **Attitude:** `yaw` is the compass heading (0 = north, clockwise positive), `pitch` is nose-up positive, `roll` is right-side-down positive. All in degrees.
- **Camera:** the OpenCV optical frame (+x right, +y down, +z forward), with the camera centre at the pose position.

With heading ψ, pitch θ and roll φ, the body axes expressed in world coordinates are:

```
f  = ( sinψ cosθ,  cosψ cosθ,  sinθ)        forward
r0 = ( cosψ,      -sinψ,       0   )
u0 = (-sinψ sinθ, -cosψ sinθ,  cosθ)
r  = r0 cosφ − u0 sinφ                      right
u  = u0 cosφ + r0 sinφ                      up
R_wc = [ r ; −u ; f ]                       p_cam = R_wc (p_world − o)
u_px = c_x + f_x x_c / z_c,   v_px = c_y + f_y y_c / z_c,   f_x = (W/2) / tan(HFOV/2)
```

`f_y` comes from `vfov_deg` when it is given; otherwise `f_y = f_x` (square pixels).

## 1. EagleEye edge mode (`lynx/vision/edge.py`)

Each frame goes through these steps in order:

1. Convert to grayscale.
2. Boost contrast with CLAHE (clip 2.5, 8×8 tiles), a 1–99 percentile linear stretch, or neither.
3. Gaussian blur: 5×5 kernel, σ = 1.4.
4. Edge operator: either Sobel magnitude (the default) or the absolute Laplacian.
5. Threshold, fixed or Otsu.
6. Tint the edge mask (white phosphor by default, so green/blue friendly
   outlines stay distinct from scene edges).
7. Blend it with `addWeighted` onto a base layer. The base can be the
   denoised mono image (default), the dimmed colour frame, or black.

The 3×3 Sobel response is scaled by ½ (`SOBEL_NORM`), so the same
`threshold` (default 26) works for both operators. Sobel is the default
because a second-derivative Laplacian picks up more sensor noise once CLAHE
has stretched it. On pure low-light noise the false-edge rate is under 1%
for Sobel and under 5% for Laplacian (both covered by tests).

**CUDA:** `EdgeFilter(backend="auto")` uses `cv2.cuda` when three things are
true: OpenCV exposes the CUDA filter constructors (`createCLAHE`,
`createGaussianFilter`, `createSobelFilter`, `createLaplacianFilter`,
`threshold`, `addWeighted`, `magnitude`), a CUDA device is present, and
construction succeeds. If a `cv2.error` occurs at init or at runtime, it
switches to CPU for good and logs a warning, so the HUD never stalls. The
percentile and Otsu statistics are computed on the host (they are scalar
reductions); all filtering stays on the GPU.

**Benchmark:**

```bash
python -m lynx.vision.bench_edge --frames 300                      # 720p, both operators
python -m lynx.vision.bench_edge --width 1920 --height 1080 --operator sobel
python -m lynx.vision.bench_edge --backend cuda                    # on Jetson / CUDA OpenCV
python -m lynx.vision.bench_edge --source clip.mp4 --json
```

Reference numbers: 4-thread Intel Xeon VM, OpenCV 5.0.0, CPU path,
synthetic low-light frames, 300 frames after 10 warm-up frames.

| Resolution | Operator | Contrast | Mean ms | p95 ms | FPS |
|---|---|---|---|---|---|
| 640×480 | Laplacian | CLAHE | 1.42 | 1.58 | 705 |
| 640×480 | Sobel | CLAHE | 2.07 | 2.39 | 484 |
| 1280×720 | Laplacian | CLAHE | 4.01 | 4.52 | 249 |
| 1280×720 | Sobel | CLAHE | 5.95 | 6.71 | 168 |
| 1280×720 | Sobel | stretch | 5.23 | 6.62 | 191 |
| 1920×1080 | Sobel | CLAHE | 10.36 | 11.88 | 97 |

On the same machine, the other per-frame costs at 720p are:

| Stage | Mean | p95 |
|---|---|---|
| IFF update | 0.45 ms | 0.62 ms |
| Full HUD render | 3.4 ms | 4.6 ms |
| YOLO11n, PyTorch CPU, imgsz 640 | 29.6 ms/frame | |
| YOLO11n, PyTorch CPU, imgsz 320 | 12.0 ms/frame | |
| YOLO11n, ONNX Runtime CPU, imgsz 640 | 40.2 ms/frame | |

## 2. Detection (`lynx/vision/detect.py`)

`YoloDetector(weights="yolo11n.pt" | "yolov8n.pt" | "*.onnx" | "*.engine")`
returns `Detection` objects with `category` set to `person` or `vehicle`:

- **person:** COCO `person`.
- **vehicle:** COCO `bicycle`, `car`, `motorcycle`, `bus`, `train`, `truck`, `boat`.

Classes are resolved by name from `model.names`, so exported models keep
working. Inference is limited to those classes (`classes=`) so it does no
extra NMS work.

There is no fallback detector. If `ultralytics` is missing or the weights
cannot be loaded (for example, offline with nothing pre-staged),
construction raises `DetectorUnavailableError` with steps to fix it. The
demo exits with that message unless you pass `--detector none` or
`--allow-no-detector`.

```bash
python -m lynx.vision.detect run path/to/image_or_video --weights yolo11n.pt --frames 100
python -m lynx.vision.detect export --format onnx           # → yolo11n.onnx
python -m lynx.vision.detect export --format engine --half  # run ON the Jetson
```

### Jetson export notes (Orin Nano / NX, JetPack 6)

1. Install the JetPack PyTorch and torchvision wheels from NVIDIA's Jetson
   index, then `pip install ultralytics`. Do not let pip replace torch with
   a generic wheel.
2. Use the JetPack OpenCV, or build OpenCV with `-D WITH_CUDA=ON
   -D OPENCV_DNN_CUDA=ON`, so that `cv2.cuda` exists. PyPI `opencv-python`
   has no CUDA support.
3. Build the TensorRT engine on the target device. Engines are tied to the
   GPU architecture and to the TensorRT/JetPack versions.
   - `export --format engine --half` gives FP16 and is the right default.
   - `--int8` needs a calibration set, and `data=` must point at it via the
     ultralytics API.
   - `imgsz=416` or `320` trades small-target recall for latency. People
     past ~60 m become a few pixels tall at 320.
4. Run `sudo nvpmodel -m 0 && sudo jetson_clocks` for repeatable benchmarks.
   For field use, choose the power mode that fits the battery budget.
5. To use ONNX instead of TensorRT, export with `export --format onnx`
   (opset chosen by ultralytics, `simplify=True`) and run it with
   onnxruntime-gpu (TensorRT or CUDA execution providers).

## 3. IFF association (`lynx/vision/iff.py`)

The IFF step turns raw detections into labelled contacts.

### Tracking

A constant-velocity box tracker matches detections to existing tracks by
solving a Hungarian assignment on (1 − IoU), with an IoU gate of 0.15 and
category matching. Box positions are smoothed with EMA weight 0.6. A track
can coast through up to 8 missed frames and is then deleted. Labels belong
to tracks, not to single detections.

### Teammate prediction

For each teammate (excluding self), the box centre is assumed to sit 0.8 m
below the headset (`head_to_center_m`). That centre point is projected
through the pinhole model to get:

- the boresight-relative azimuth and elevation, the pixel position and the slant range;
- the expected box height, `f_y · 1.75 m / z_c`.

### Gating and cost

```
σ_az² = σ_ang² + atan(σ_pos / R)² + (w_box_ang / 4)²       (σ_ang = 1.5°, σ_pos = 1.0 m)
σ_el² = σ_ang² + atan(σ_pos,z / R)² + (h_box_ang / 4)²     (σ_pos,z = 0.6 m)
e_az = Δaz / σ_az,  e_el = Δel / σ_el,  e_size = ln(h_box / h_exp) / 0.45
gate: |e_az|, |e_el| ≤ 3, and 0.30 ≤ h_box / h_exp ≤ 2.2
cost = e_az² + e_el² + e_size²
```

- The lower size bound (0.30) allows for prone, crouched and partially
  occluded teammates.
- Boxes cut off by the top or bottom of the frame skip the lower size
  bound.
- The one-to-one teammate↔track assignment is solved with
  `scipy.optimize.linear_sum_assignment`. This correctly handles the
  "crossing" case that greedy nearest-bearing matching gets wrong, and
  separates teammates on the same bearing by range/size. Both cases are
  tested.

### Evidence and hysteresis

Each (track, node) pair keeps a score:

| Event | Score change |
|---|---|
| Matched this frame | +1.0 (+1.0 more if cost < 1, so a clean match is Friendly on its first frame) |
| Track detected, but this pair not matched | −0.6 |
| Track coasting (no detection) | −0.15 |

The score is capped at 6. A track becomes FRIENDLY at score ≥ 1.5 and
stays FRIENDLY until the score drops below 0.5. Each node can label at most
one track; if two tracks compete, the higher score wins.

### Status

There is no dead or neutralised state: a lost track is deleted.

| Status | Rendered | When |
|---|---|---|
| FRIENDLY | team colour (green/blue) corner box + callsign + telemetry range | confirmed teammate |
| UNVERIFIED | amber `UNK` / `VEH?` | Any of: a new track (< 5 hits); a vehicle; inside some teammate's gate but not assigned; previously Friendly; within ±15° of a teammate whose telemetry is stale (> 2 s) |
| TANGO | red box + diamond + `TANGO` | an established person track that no live teammate can explain |

These rules are deliberately conservative to avoid fratricide. A track
that was ever Friendly never escalates to TANGO, and missing telemetry only
pushes contacts toward UNVERIFIED. TANGO is an advisory cue only: positive
visual ID is still required.

Teammates that should be in view but have no matching box (for example,
behind cover) are drawn as hollow team-colour **BFT diamonds** at their
predicted pixel. Stale ones are drawn dimmed and marked `STALE`.

**Synthetic validation:** the test
`test_end_to_end_synthetic_no_blue_on_blue` pans the operator across three
teammates and three unknowns. The simulation adds 3% box jitter, 6%
dropout and 0.4 m telemetry noise. The test asserts that no teammate is
ever shown as TANGO, that no unknown is ever shown as FRIENDLY, and that
more than 90% of teammate-frames carry the correct callsign. A 300-frame
run scored 100% (450/450).

## 4. HUD (`lynx/hud/`)

`HudRenderer().render(frame, HudState(...))` draws onto any BGR or mono
frame. All sizes scale with `frame_height / 720`.

| Element | Location | Content |
|---|---|---|
| Telemetry block | top-left | callsign, node, mode, HDG/PIT/ROL, ENU position, plus any `state.telemetry` lines (link, pipeline latency, FPS, detector) |
| Compass tape | top-centre | 360° wrapping, 120° visible span, 5° and 15° ticks, cardinal letters, heading box. Teammate bearings are triangles; pings are diamonds; markers outside the span are drawn hollow and pinned to the tape ends |
| PiP aux feed | top-right | UAV/chokepoint camera, edge view or raw view, with a label |
| Reticle | centre | gapped crosshair, centre dot, stadia ticks |
| IFF boxes | in scene | corner brackets, dashed while coasting, label above (vehicles below) |
| Pings | in scene / screen border | on screen: double chevron + label + range. Off screen or behind: a border arrow placed along the camera-frame (x_c, y_c) direction, which stays correct for targets behind the operator |
| Mini radar | bottom-left | heading-up, 3 range rings, FOV wedge, rotating N, teammates (dots), pings (diamonds), unverified/tango contacts (×) at their estimated range |
| Summary | bottom-centre | FRND / UNK / TGO counts and the TANGO alert |

## 5. Demo

```bash
pip install -r requirements-vision.txt

# Synthetic (CI, no camera): writes a PNG and an MP4
python -m lynx.hud.demo --source synthetic --frames 300 --headless \
    --save-frame hud.png --save-at 90 --record hud.mp4
python -m lynx.hud.demo --source synthetic --edge          # windowed, EagleEye mode

# Webcam 0 + YOLO11n, static teammate telemetry and a ping
python -m lynx.hud.demo --source 0 --hfov 78 \
    --teammate VIPER:2:3,25,1.65 --teammate GHOST:3:-8,15,1.65:blue --ping OBJ:0,60,0

# Video file, edge mode, a second video in the PiP
python -m lynx.hud.demo --source clip.mp4 --edge --pip drone.mp4
```

Keys:

| Key | Action |
|---|---|
| `q` / Esc | quit |
| `e` | toggle edge mode |
| `p` | cycle PiP |
| `a` / `d` | yaw ±5° |
| `w` / `s` | pitch ±2° |
| space | pause |
| `c` | screenshot |

With a headless OpenCV build the demo detects the missing GUI and keeps
running headless.

`--detector sim` is used only with `--source synthetic`. It feeds the
scene's ground-truth boxes, with detector-like jitter, dropout and
confidence, into the IFF step, so CI exercises the whole pipeline without
weights. Real camera and video sources always use YOLO.

## 6. Tests

```bash
python -m pytest tests/vision tests/hud -q      # 69 tests, ~20 s on CPU
```

| Area | Covered |
|---|---|
| Edge filter | step-edge detection with clean flat regions, noise rejection, mono/BGR/BGRA input, tint, base modes, Otsu, stretch, CLAHE, config validation, CUDA→CPU fallback, benchmark output |
| Geometry | orthonormal rotation, boresight at the four headings, right/up/pitch/roll image directions, behind-camera handling, pixel↔angle inverse, edge arrows |
| IFF | immediate Friendly, TANGO after min hits, size gate, same-bearing separation, Hungarian vs greedy, one node per track, dropout persistence, hysteresis, stale telemetry, vehicles, behind-camera, track deletion, self-node, end-to-end synthetic |
| HUD | copy vs in-place rendering, 4 resolutions, grayscale input, camera rescale, reticle, compass wrap, radar, status colours, chevrons and edge arrows (world and pixel), PiP, full synthetic pipeline, demo CLI |

## Known limitations / next steps

- The CUDA edge path follows the `cv2.cuda` API but has only been run in
  fallback mode here: the VM has no GPU and PyPI OpenCV has no CUDA. It
  needs validating on a Jetson with a CUDA OpenCV build, using
  `bench_edge --backend cuda`.
- The synthetic renderer does not model occlusion by buildings.
- Radar contact bearings ignore roll. That is acceptable for |roll| ≲ 10°.
- IFF currently uses headset position only. Teammate yaw, and per-node
  covariance from Phase 3 UWB/IMU, can replace the fixed σ values.
- The static `--teammate` and `--pose` flags in the camera demo are
  temporary. Replace them with the Phase 1 relay client feed by mapping
  messages to `TeammateTrack` / `OperatorPose` / `WorldPing` as described in
  the interface contract above.
