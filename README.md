# TeamLynx

TeamLynx is a squad tactical AR HUD for airsoft. Each headset shares its position over an offline
squad network, shows friendly IFF markers, and draws world pings that stay perspective-correct for
every operator.

This repository covers **Phase 1 (zero hardware)** and **Phase 2 (software pipeline)**:

* the wire schema;
* an async WebSocket relay;
* the spatial math (frames, rotations, pinhole projection, raycasting);
* a multi-window desktop testbench that shows pings projecting correctly from every operator's
  perspective;
* the vision pipeline: EagleEye edge mode, YOLO person/vehicle detection, IFF association with
  squad telemetry ([docs/vision-pipeline.md](docs/vision-pipeline.md));
* the HUD overlay and **`lynx-headset`**, the integrated headset client that runs all of the above
  on live relay telemetry and pings ([docs/headset.md](docs/headset.md)).

## Quickstart

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[vision,dev]"   # numpy, websockets, opencv, scipy, pygame, pytest
pytest                           # full test suite
lynx-selfcheck                   # headless end-to-end check (relay + 3 virtual operators)
```

Optional extras: `[vision]` (OpenCV + SciPy; use `[vision-headless]` instead on servers/CI, never
both), `[yolo]` (Ultralytics; install a torch wheel first), `[onnx]`, `[sim]` (pygame only). With
only `[dev]` installed the vision, HUD and headset tests are skipped.

### Run the integrated headset

```bash
lynx-relay                                                                               # terminal 1
lynx-headset --node 1 --callsign ALPHA --team blue                                       # terminal 2
lynx-headset --node 2 --callsign BRAVO --team blue --y 20 --heading 180 --pitch -10      # terminal 3
```

Both default to the synthetic low-light camera. Use `--source 0` for a webcam (YOLO11n) or
`--source clip.mp4` for a video. See [docs/headset.md](docs/headset.md) for keys and options.

The same two-headset layout (synthetic or two USB NoIR cameras) is also
`lynx-poc` — a thin launcher for the cheap 2-person proof of concept, not the
field kit. See [docs/poc/README.md](docs/poc/README.md).

### Run the multi-window sim

You can start everything with one command:

```bash
lynx-sim-launch --tile           # relay + ALPHA, BRAVO, CHARLIE windows
```

Or run each process in its own terminal, which is closer to how separate headsets behave:

```bash
lynx-relay                                                                    # terminal 1
lynx-sim --node 1 --callsign ALPHA   --team blue  --x 0   --y 0  --heading 0   # terminal 2
lynx-sim --node 2 --callsign BRAVO   --team green --x 15  --y 25 --heading 225 # terminal 3
lynx-sim --node 3 --callsign CHARLIE --team blue  --x -20 --y 10 --heading 90  # terminal 4
```

To run the windows on other machines, point them at the relay with
`--url ws://<relay-ip>:8765`. In the field the relay runs on the travel router:
`lynx-relay --host 0.0.0.0`.

Each `lynx-*` command also has a module form: `python -m lynx.net.server`, `python -m lynx.sim.app`,
`python -m lynx.sim.launch` and `python -m lynx.sim.selfcheck`.

### Controls

| key | action |
|---|---|
| W / S | forward / back |
| A / D | strafe |
| Shift | sprint |
| Q / E or ← / → | turn |
| ↑ / ↓ | pitch |
| Z / C | roll |
| PgUp / PgDn | eye height |
| R | level pitch and roll |
| Space | drop a ping, raycast from the reticle |
| 1–5 | ping type: mark, contact, move, danger, rally |
| Backspace | cancel my last ping |
| Del | cancel all my pings |
| + / − | minimap zoom |
| H | help overlay |
| Esc | quit |

Each window shows:

* a synthetic first-person view with a ground grid (the red line is the East axis y = 0, the green
  line is the North axis x = 0);
* teammates as team-coloured posts labelled with callsign and range;
* pings as chevrons with a 1 m ground ring, a type label, distance, owner and remaining TTL;
* edge-clamped arrows for targets that are off-screen or behind you;
* a compass tape with ping and teammate bearings;
* a north-up minimap with your field-of-view wedge.

## Layout

```
lynx/
  net/       schema.py (binary + JSON wire format), state.py (world model),
             server.py (relay), client.py (async client + thread wrapper)
  spatial/   rotations.py, frames.py, projection.py, raycast.py
  sim/       app.py (operator window), render.py, operator.py, launch.py, selfcheck.py
  vision/    edge.py (EagleEye), detect.py (YOLO), iff.py, geometry.py (view onto lynx.spatial),
             synthetic.py (CI scene), bench_edge.py
  hud/       renderer.py, widgets.py, style.py, types.py, demo.py (offline demo)
  headset/   app.py (lynx-headset), adapters.py (relay -> vision/HUD types),
             pose.py (pluggable pose sources), sources.py (camera sources)
docs/
  spatial-math.md   frame conventions and full derivations
  protocol.md       wire format and relay semantics
  vision-pipeline.md edge mode, detection, IFF, HUD
  headset.md        integrated headset client, pose-source interface
  poc/              cheap 2-person demo launcher (not the field kit)
  field/            Phase 4 helmet + squad-net (later product)
tests/       pytest suite (math, schema, relay, sim, vision, hud, headset end-to-end)
```

Phase 3 adds `lynx/hw/` (serial IMU pose source) and `firmware/` (ESP32 + BNO085).

## Conventions

These are the conventions in brief. See [docs/spatial-math.md](docs/spatial-math.md) for the full
treatment.

* **World:** local ENU in metres (x East, y North, z Up), with the origin at the staging datum and
  the ground at z = 0.
* **Body:** FLU (Forward, Left, Up) at the eye.
* **Camera:** OpenCV RDF (Right, Down, Forward).
* **Attitude on the wire:** heading is the compass bearing clockwise from North, pitch is positive
  nose-up, roll is positive right-side-down. Internally these become
  `R_wb = Rz(90° − heading) · Ry(−pitch) · Rx(roll)`.
