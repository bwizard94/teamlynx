# Phase 4 field hardware: helmet build

The Phase 3 bench rig (`docs/hardware/`) becomes a helmet a player can wear for a 4 h night
op. **The firmware, serial protocol, pin map and host code do not change.** This phase is
mechanics, power, display and harness.

![Sensor pod, assembled and exploded](hw-renders/phase4-helmet-pod.png)

```
        FRONT (NVG mount)                    LEFT RAIL                    REAR (counterweight)
 ┌──────── sensor pod ────────┐        ┌──── ESP32 box ────┐        ┌────── rear cassette ──────┐
 │ IMX462 UVC + M12 ── USB ───┼────────┼───────────────────┼───────►│ Jetson Orin Nano Super    │
 │ BNO085 ── I2C (GX12-6) ────┼───────►│ ESP32-S3 (Phase 3 │──USB──►│  (15 W mode)              │
 │ 2× SFH4715AS ◄─ 350 mA ────┼────────┼── firmware as-is) │        │ PDB: 15 V PD, F1/F2,      │
 │ 2× 3 mm Lexan + bezel      │        │ J1 ◄── rail switch│        │  LDD-350L, IR interlock   │
 └─ dovetail shoe ────────────┘        └───────────────────┘        └──── DP→HDMI ──┬───────────┘
                                                                     USB-C 15 V ▲   │
      GOGGLE (Z87.1+, untouched lens): micro-OLED engine inside ◄── driver ◄────────┘
      BODY: 25 Ah USB-PD bank ── magnetic breakaway ── USB-C ─────────────────┘
```

| Doc | Contents |
|---|---|
| [hw-mechanical.md](hw-mechanical.md) | OpenSCAD models, rendering, print settings, tolerances, heat-set inserts, torque, assembly of the pod / ESP32 box / rear cassette, window fabrication, dovetail fitting |
| [hw-display.md](hw-display.md) | micro-display options inside Z87.1+ goggles, what keeps the ballistic rating intact, fit check, signal chain, anti-fog, parts |
| [hw-compute-power.md](hw-compute-power.md) | Jetson vs alternatives, power budget, USB-PD bank sizing, harness diagram, PDB and IR interlock schematic, pinouts, fusing, strain relief, quick-disconnects, thermal |
| [hw-weight-balance.md](hw-weight-balance.md) | mass and moments from the real STL volumes, sensitivities, fitting |
| [hw-ir-safety.md](hw-ir-safety.md) | IEC 62471 check of the field illuminator, controls, 850 vs 940 nm detectability |
| [hw-maintenance.md](hw-maintenance.md) | pre-op / in-op / post-op checklists, squad spares kit |
| [hw-bom.md](hw-bom.md) | per-player BOM with costs (≈ USD 1 080) |
| `hardware/openscad/`, `hardware/scripts/render_all.sh` | the models and the render script |

## Key numbers

| | |
|---|---|
| Added head-borne mass | 1 042 g (pod 194, mount 162, shroud 35, ESP32 box 92, rear cassette + base 417, display 36, harness 107) |
| Fore-aft balance | added CG 3 mm behind the head CG: −0.03 N·m, i.e. balanced. The Jetson is the counterweight |
| Power | 14.7 W average, 24.8 W peak at 15 V |
| Battery | 25 Ah (90 Wh) USB-PD bank with a 15 V PDO: 4.3 h aged and cold, 5.4 h new |
| Pod | 68 × 103 × 56 mm. Two 42 × 38 × 3 mm polycarbonate windows behind a 6-screw bezel with a hard stop. Sealed camera/IMU bay, vented IR bay |
| IR | 2 × 850 nm at 350 mA (hardware-limited), 0.6 W/sr. IEC 62471 Exempt at ≥ 10 cm. Fail-safe interlock plus a hardware SAFE/ARM switch |

## Consistency with Phase 3

* **Pins and firmware.** `firmware/include/board_pins.h` is unchanged. The ESP32 box wires
  J1/J2 to the same GPIOs as wiring.md §1, and the passives (R1–R4, C1–C3, D1) are the ones
  in wiring.md §2–3.
* **Layout.** The ESP32 moves onto the helmet with USB down to the compute, as wiring.md §2.2
  recommends for Phase 4. The I2C run is ~0.45 m (≈ 137 ns rise time), so the bus stays at
  400 kHz.
* **IMU mount.** The BNO085 sits flat, components up, X arrow forward, on the same rigid sled
  as the camera. That is mount spec `FLU` (`top-flat`, the default), and the camera's optical
  axis is body +X by construction (camera-illuminator.md §1).
* **Illuminator.** The parts are the ones in bom.md #18–21. Eye-safety numbers are extended
  in hw-ir-safety.md.

## Decisions worth reviewing

1. **The Jetson rides on the helmet as the counterweight; the battery is body-worn.** This
   balances the pod with no dead weight, and only power and the rail switch leave the
   helmet. A body-worn Jetson would need about 400 g of dead counterweight plus a 4-cable
   umbilical.
2. **15 V USB-PD, not 20 V.** The dev kit's DC input is specified 9–20 V, and a PD 20 V
   source can legally deliver 21 V.
3. **The display goes inside an OTG/Rx-insert goggle, as a monocular 0.23" 640 × 400
   micro-OLED.** Nothing touches the lens, and the FPC enters via the vent channel. The fit
   (cornea-to-lens depth ≥ 27–31 mm) must be measured on the chosen goggle before buying
   10 kits. The fallback is an external monocular in front of the lens.
4. **Dovetail dimensions are nominal.** No published standard exists. The shoe is
   parametric, has an adjustable gib, and comes with a −0.2/0/+0.2 gauge coupon. The
   insertion axis is a parameter.
5. **Two separate windows** (camera and IR), each gasketed, instead of one shared window.
   This kills NIR veiling glare (camera-illuminator.md §4). Windows are swapped by removing
   six screws.
6. **The magnetic USB-C breakaway at the shoulder is the neck-safety release.** The Jetson
   reboots when it separates.
7. **The IR interlock requires host-software logic:** IR_EN on Jetson header pin 32,
   night/edge mode, pod not stowed, IMU healthy, 120 s re-arm. The hardware is fail-safe
   without it, but the software has to be written.
8. **ESP32 box mount.** It goes on a Picatinny segment via an ARC-to-1913 adapter (ARC rail
   dimensions are not published), with a strap variant for helmets without rails.
