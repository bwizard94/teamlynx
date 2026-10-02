# TeamLynx headset client (`lynx-headset`)

`lynx-headset` is one operator's headset: camera, EagleEye edge mode, YOLO, IFF and the HUD, all
fed by the squad relay. It joins the Phase 1 networking/spatial core
([protocol.md](protocol.md), [spatial-math.md](spatial-math.md)) to the Phase 2 vision and HUD
stack ([vision-pipeline.md](vision-pipeline.md)), using one camera model and one angle convention.

```
 pose source ──► Pose ───────┬─► telemetry ──────────────► relay ──► other headsets
 (keyboard |                 │   trigger ─► raycast ─► ping ─┘ │
  serial IMU)                ▼                                 ▼ snapshot (nodes, pings)
                     OperatorPose ◄─ adapters ─► TeammateTrack, WorldPing
                             │                     │            │
 camera source ─► frame ─► YOLO / sim dets ─► IffAssociator     │
 (synthetic |        │                             │ tracks     │
  webcam | video)    └─► EdgeFilter ─► HudRenderer ◄────────────┘
```

## Install and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[vision,dev]"     # desktop with a display
# pip install -e ".[vision-headless,dev]"   # servers / CI (no cv2 windows)
# pip install -e ".[vision,yolo]"           # + YOLO (install a torch wheel first)
```

Two synthetic headsets facing each other, 20 m apart:

```bash
lynx-relay                                                                               # terminal 1
lynx-headset --node 1 --callsign ALPHA --team blue                                       # terminal 2
lynx-headset --node 2 --callsign BRAVO --team blue --y 20 --heading 180 --pitch -10      # terminal 3
```

Each window labels the other operator FRIENDLY in team colour with its callsign, using only relay
telemetry. Press space in BRAVO's window: the ping is raycast from BRAVO's reticle to the ground and
appears as a chevron in both views, and as an edge arrow once it leaves the field of view. The
scripted unknown patrols in the synthetic compound show as UNK, then TANGO. Add `--no-unknowns` to
remove them.

Real camera input:

```bash
lynx-headset --node 3 --callsign CHARLIE --source 0 --hfov 78               # webcam 0 + YOLO11n
lynx-headset --node 3 --callsign CHARLIE --source clip.mp4 --edge --pip raw # video, edge mode
lynx-headset ... --url ws://192.168.8.1:8765                                # relay on the router
```

`--hfov` must be the lens HFOV at the delivered resolution. It is the only intrinsic the pinhole
model uses (ideal, undistorted, principal point at the centre). Real sources default to
`--detector yolo`. If YOLO is unavailable, pass `--detector none` or `--allow-no-detector`.

Headless runs (CI, screenshots):

```bash
lynx-headset --node 1 --callsign ALPHA --headless --frames 120 --ping-at 10 --save-frame hud.png
```

`--ping-at N` presses the trigger at frame N. It takes the same code path as the space key or the
rail switch. Also available: `--save-at N`, `--record hud.mp4`, `--fps 0` (uncapped).

## Keys

| Key | Action |
|---|---|
| a / d | yaw −/+ 5° (keyboard pose source) |
| w / s | pitch +/− 2° |
| [ / ] | roll −/+ 2° |
| i / k, j / l | move forward/back, strafe left/right 1 m |
| r | level pitch and roll |
| space | drop a ping (raycast from the reticle) |
| 1–5 | ping type: mark, contact, move, danger, rally |
| x / Backspace | cancel my last ping |
| e | toggle EagleEye edge mode |
| p | cycle PiP: auto, edge, raw, top-down, none |
| c | screenshot |
| q / Esc | quit |

The keyboard stands in for the IMU and the rail switch on the bench. In the field there is no
keyboard or touch input: the pose and ping trigger both come from the serial source below.

## Pose sources

`--pose` selects the source: `keyboard` (default), `static[:x,y,z,heading,pitch,roll]`, or
`serial:PORT`. The interface is in `lynx/headset/pose.py`:

```python
@dataclass(frozen=True)
class PoseSample:
    pose: Pose          # lynx.spatial.Pose: body (FLU head) -> world (ENU)
    trigger: bool = False  # one True per debounced rail-switch press (rising edge)
    flags: int = 0      # OR-ed into telemetry flags (TelemetryFlags.PING_SWITCH, IMU_DEGRADED)

class PoseSource(Protocol):
    name: str
    def read(self, now: float) -> PoseSample: ...   # latest sample, non-blocking
    def handle_key(self, key: int) -> bool: ...     # OpenCV key code; True if consumed
    def close(self) -> None: ...
```

The Phase 3 serial IMU source (BNO085 → ESP32 → USB serial) belongs in `lynx/hw/`. To plug it in,
`lynx.hw` calls this once at import time:

```python
from lynx.headset.pose import register_pose_source
register_pose_source("serial", lambda port, initial: SerialImuPoseSource(port, initial))
```

`create_pose_source("serial:/dev/ttyUSB0", initial)` imports `lynx.hw` lazily the first time it is
asked for a serial source. Until that package exists, `--pose serial:...` exits with a message
saying so. An orientation-only IMU keeps `initial.position` (the CLI's `--x --y --eye-height`) as
its position. `tests/headset/test_pose_source.py` covers this registration path with a stand-in
plugin module.

## What the client does each frame

1. `PoseSource.read()` gives the pose and the trigger.
2. Telemetry (x, y, z, heading, pitch, roll and flags) goes to the relay at `--rate` Hz.
3. On a trigger, `raycast_from_pose` casts along the optical axis, takes the ground hit (or
   `--fallback-range` along the ray), and publishes it as a `Ping` of the selected type with
   `--ping-ttl`.
4. A snapshot of the relay world state is adapted (`lynx/headset/adapters.py`):
   * nodes become `TeammateTrack`s. The timestamp is the receiver's `time.monotonic()` at the last
     accepted telemetry. IFF runs with `now=time.monotonic()`, so a teammate whose telemetry is
     more than 2 s old drops to STALE and can never be promoted to TANGO.
   * pings, including your own, become `WorldPing`s: label is the ping type, owner is the owner's
     callsign, colour comes from the type.
5. The camera source returns a frame. The synthetic source draws relay teammates standing at their
   telemetry positions and returns simulated detector output. Webcam and video frames go to YOLO.
6. `IffAssociator.update` assigns FRIENDLY, UNVERIFIED or TANGO. `HudRenderer` draws on the raw or
   edge-filtered frame. World pings are placed by `lynx.spatial.Camera.project`.

## Tests

```bash
pytest tests/headset -q
```

| Test | Checks |
|---|---|
| `test_adapters.py` | Phase 2's closed-form rotation equals `lynx.spatial`'s; vision projection, bearings and edge arrows agree with `Camera.project`; HUD ping placement; relay message adapters |
| `test_pose_source.py` | keyboard steps and one-shot trigger, static spec, lazy `serial` plugin (missing and present) |
| `test_integration.py` | real relay with two headless headsets: each labels the other FRIENDLY with the right callsign and team colour, no TANGO; BRAVO's keyboard ping reaches ALPHA, renders at the analytically predicted pixel with ping-coloured chevron pixels there, and becomes a side-edge arrow when ALPHA turns around; cancel propagates; CLI headless run; missing serial plugin |
