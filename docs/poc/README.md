# Cheap two-operator proof of concept

This is the **filmable 2-person demo**, not the ~$1,080/player field kit in
[docs/field/hw-bom.md](../field/hw-bom.md). Hardware choices, the demo script,
safety rules, and the cost table live in the project plan. This folder is the
repo-side runbook for today's code.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[vision,dev]"
# optional: real USB NoIR cameras + YOLO
#   pip install torch --index-url https://download.pytorch.org/whl/cpu
#   pip install -e ".[vision,yolo]"
# optional: BNO085 → ESP32 serial pose (Phase 3 firmware)
#   pip install -e ".[vision,hw]"
```

## One-command demo

```bash
lynx-poc --dry-run                  # print the exact processes
lynx-poc                            # Tier A: relay + two synthetic lynx-headset windows
lynx-poc --tier bench --edge        # Tier B: USB cameras 0 and 1, start in edge mode
```

`lynx-poc` is a thin launcher. It does not replace `lynx-headset` or `lynx-field`.

### Two laptops on a phone hotspot / travel-router SSID

On the ALPHA machine (also runs the relay):

```bash
lynx-relay --host 0.0.0.0 --port 8765          # or: lynx-poc --tier bench --role alpha --host 0.0.0.0
```

On the BRAVO machine, after both have joined the offline SSID:

```bash
lynx-poc --tier bench --role bravo --url ws://192.168.8.1:8765 --bravo-source 0
```

Replace `192.168.8.1` with the ALPHA laptop's LAN IP (`ip -4 addr`) if the relay
is not on a travel router.

### One BNO085 on BRAVO

Flash Phase 3 firmware (`firmware/`, env `esp32dev`), then:

```bash
python -m lynx.hw hello --port /dev/ttyUSB0
lynx-poc --config deploy/poc/bravo-serial.example.json --host 0.0.0.0
# or: lynx-poc --tier bench --bravo-pose serial:/dev/ttyUSB0
```

Keyboard still walks position (`i/k/j/l`). The IMU supplies heading/pitch/roll.
Space still drops a ping if you skip the rail switch.

## Manual commands (no launcher)

These are what `lynx-poc` runs for Tier A. They match
[docs/headset.md](../headset.md):

```bash
lynx-relay
lynx-headset --node 1 --callsign ALPHA --team blue
lynx-headset --node 2 --callsign BRAVO --team blue --y 20 --heading 180 --pitch -10
```

Tier B on one machine, cameras `/dev/video0` and `/dev/video2` (OpenCV indices
`0` and `1` once other devices are ignored):

```bash
lynx-relay --host 0.0.0.0
lynx-headset --node 1 --callsign ALPHA --source 0 --hfov 78 --allow-no-detector --edge
lynx-headset --node 2 --callsign BRAVO --source 1 --hfov 78 --y 8 --heading 180 --allow-no-detector --edge
```

`--hfov` must be the lens HFOV at 1280×720. Measure it: two marks at the image
edges, `HFOV = 2·atan((half-width) / distance)`.

Without YOLO, webcam IFF boxes will not appear (synthetic IFF still works).
Pass `--detector yolo` after installing `[yolo]`, or `--allow-no-detector` to
show the HUD and shared pings only.

## What this folder is not

Do not use `deploy/jetson/`, `deploy/openwrt/`, or `lynx-field` for the cheap
kit. Those belong to the later 10-player field net (PR #6).
