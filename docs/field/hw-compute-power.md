# Compute, power, harness, fusing, thermal

## 1. Compute: Jetson Orin Nano vs alternatives

The Phase 2 pipeline (contrast-stretch, Gaussian, Laplacian/Sobel edge overlay with OpenCV;
YOLOv8/v11-nano detection; HUD compositing) is written against OpenCV and a TensorRT/ONNX
detector. Pipeline parity with the bench is the deciding factor.

| Option | AI throughput | Power envelope | Display out | Port cost from Phase 2 | Price (USD, 2026) | Mass |
|---|---|---|---|---|---|---|
| **Jetson Orin Nano Super Developer Kit 8 GB** | 67 TOPS (INT8 sparse); YOLOv8n TensorRT FP16 at 640 px runs far above 30 fps | 7 / 15 / 25 W (MAXN SUPER) modes | DisplayPort 1.2 only. DP++ dual mode, so a passive DP→HDMI adapter works | none: CUDA OpenCV, TensorRT, same JetPack as the bench | 249 | 176 g |
| Orin Nano 4/8 GB module on a compact third-party carrier (Seeed reComputer J30xx, etc.) | 34–67 TOPS | 7–25 W | HDMI on most carriers | none | 300–450 with case | 150–400 g |
| Orin NX 16 GB (reComputer J4012 class) | 157 TOPS | 10–40 W | HDMI | none | 700+ | 400 g+ |
| Raspberry Pi 5 8 GB + Hailo-8 / 8L (AI HAT+) | 26 / 13 TOPS | 8–12 W | 2 × micro-HDMI | detector → Hailo HEF; edge filter on CPU (NEON) | 80 + 70–110 | ~ 90 g |
| RK3588 (Orange Pi 5 / Rock 5B) | 6 TOPS NPU | 8–12 W | HDMI | detector → RKNN; OpenCV CPU | 100–150 | ~ 70 g |
| Jetson Nano (2019) | 0.5 TFLOPS FP16 | 5–10 W | HDMI | JetPack 4 (Python 3.6), EOL | – | – |

**Decision: Jetson Orin Nano Super Developer Kit, run in the 15 W mode.**

* Zero porting from Phase 2.
* Enough headroom for edge filter + detector + HUD at 30 fps.
* 15 W gives the 4 h budget below. MAXN SUPER would cut the runtime by about a third (§3).

Select the mode by name, because the IDs change between JetPack releases:

```bash
sudo nvpmodel -q --verbose | grep -i "POWER_MODEL"   # list modes and IDs
sudo nvpmodel -m <id of the 15W mode>
sudo systemctl enable nvfancontrol                 # fan on the auto curve
tegrastats --interval 1000                         # during trials: watch VDD_IN and throttling
```

The Raspberry Pi 5 + Hailo-8 is the lighter, cheaper v2 path. It costs a detector port and a
CPU edge filter.

## 2. Power budget (per player, measured at the 15 V bus)

| Load | Fed from | Average W | Peak W | Basis |
|---|---|---:|---:|---|
| Jetson Orin Nano Super, 15 W mode: module + carrier + fan + Wi-Fi, running edge + YOLO-n + HUD at 30 fps | 15 V DC jack | 11.0 | 18.0 | the module is capped at 15 W; carrier and fan add ~1.5–3 W; peak is boot and TensorRT engine build |
| UVC IMX462 camera, 1280 × 720 MJPEG | Jetson USB-A (5 V) | 1.0 | 1.3 | ~200 mA at 5 V |
| Micro-OLED engine + HDMI driver board | Jetson USB-A (5 V) | 1.0 | 1.5 | at night brightness; peak at full white |
| ESP32-S3 + BNO085, Wi-Fi/BT off | Jetson USB-A (5 V) | 0.4 | 0.6 | Phase 3 wiring.md §4 (< 100 mA) |
| DP++ → HDMI passive adapter | DP_PWR 3.3 V | 0.1 | 0.2 | level shifter |
| Carrier 5 V conversion loss for the USB loads (≈ 88 %) | – | 0.3 | 0.4 | |
| IR illuminator, 2 × SFH 4715AS in series at 350 mA | LDD-350L on 15 V | 0.7 | 2.3 | 2 × 2.9 V × 0.35 A = 2.0 W in the LEDs, /0.88 driver efficiency = 2.3 W at 100 % duty; burst use averages ~30 % |
| PD trigger, fuses, 15 V wiring I²R | – | 0.2 | 0.5 | 1.0–1.7 A through ~0.15 Ω |
| **Helmet total** | | **14.7** | **24.8** | **1.65 A at 15 V peak** |
| *Squad router node (one per squad, its own pack)* | *5 V USB* | *4* | *6* | *travel-router class; model choice is in the networking docs* |

## 3. USB-PD bank sizing for a 4 h night op

Energy needed at the bank's 15 V output:

\[ E_{out} = 14.7\ \text{W} \times 4\ \text{h} = 58.8\ \text{Wh} \]

Bank-rated energy is quoted at the cell voltage (3.6–3.7 V). Derates:

| Factor | Value | Why |
|---|---|---|
| \(\eta\), cell → 15 V boost | 0.88 | typical for 45–100 W PD banks at 1 A out |
| cold (body-worn pack at ~10 °C) | 0.95 | Li-ion capacity at a night-time temperature |
| ageing reserve | 0.85 | still makes 4 h after ~300 cycles (~80–85 % capacity) |
| **product** | **0.71** | |

\[ E_{rated} \ge \frac{58.8}{0.71} = 83\ \text{Wh} \;\Rightarrow\; \frac{83\ \text{Wh}}{3.6\ \text{V}} = 23\ \text{Ah} \]

**Choose a 24 000–27 000 mAh bank (86–99.9 Wh)** with these properties:

* a **15 V PDO rated ≥ 3 A (45 W)**
* PD 3.0
* no low-current auto-off problem (1 A is far above any cut-off)
* under 100 Wh, which satisfies airline and most field rules

Expected runtime with a 25 Ah (90 Wh) bank:

| Condition | Usable Wh at 15 V | Runtime at 14.7 W |
|---|---:|---:|
| new pack, 20 °C (90 × 0.88) | 79 | 5.4 h |
| new pack, 10 °C | 75 | 5.1 h |
| aged pack, 10 °C (design case) | 64 | **4.3 h** |
| aged, 10 °C, MAXN SUPER (~20 W total) | 64 | 3.2 h ✗ |
| aged, 10 °C, IR on continuously (+1.6 W) | 64 | 3.9 h |

Why 15 V and not 20 V: the developer kit's DC input is specified **9–20 V**. A USB-PD 20 V
source is allowed to deliver up to 21 V (+5 %), which is over that limit. A 15 V source
(14.25–15.75 V) sits in the middle of the range and also feeds the LDD-350L (9–36 V in, which
needs Vin ≥ Vout + 3 V = 8.8 V).

Verify every bank before it is issued:

* Put an inline USB-C PD tester between the bank and the cassette. The CH224K must negotiate
  **15.0 V**, and at a 1.5 A load the voltage must stay above 14.5 V.
* Never plug a second device into the same bank during an op. Many banks renegotiate every
  port when a new sink appears, which drops the output and reboots the Jetson.
* A 20 000 mAh (72 Wh) bank gives about 3.4 h in the design case. Use one only for short
  games.

## 4. Harness

The cassette is the hub. Every cable runs to it except the rail switch (to the ESP32 box)
and the IMU pigtail (pod → ESP32 box).

```
          ┌──────────────────── SENSOR POD (on the NVG mount) ─────────────────────┐
          │  G1: camera USB          G2: IMU 6-core shielded      G3: IR pair 22 AWG │
          └──────┬──────────────────────────┬──────────────────────────────┬────────┘
                 │ 0.9 m                     │ 0.35 m                       │ 0.9 m
                 │                     P2 GX12-6 plug                 P3 GX12-2 plug
                 │                           │                              │
                 │      ┌────────── ESP32 BOX (left rail) ──────────┐       │
                 │      │ J2 GX12-6 socket ◄─────────┘               │       │
  weapon:        │      │ J1 3.5 mm TRS ◄── P0 plug ◄── 1.2 m ◄──────┼─── rail pressure switch
                 │      │                  (pull-out = breakaway)    │      (rail clips, strap clips)
                 │      └──────── USB cable, grommet ────────────────┘       │
                 │                    │ 0.45 m                               │
                 ▼                    ▼                                      ▼
  ┌───────────────────────────── REAR CASSETTE ─────────────────────────────────────────────┐
  │ Jetson USB-A #1 ◄ camera     USB-A #2 ◄ ESP32     USB-A #3 ► 5 V to display driver      │
  │ Jetson DP ► A1 DP++→HDMI ► HDMI FPC ribbon 0.5 m (right side) ► display driver           │
  │ Jetson J12 pin 32 IR_EN, pin 30 GND ► PDB interlock (Q2)                                 │
  │ PDB: J10 USB-C 15 V ─ F1 3 A ─┬─ P1 5.5×2.5 ► Jetson DC jack                             │
  │                               └─ F2 0.5 A ─ S1 SAFE/ARM ─ U1 LDD-350L ─ J11 GX12-2 ◄ P3  │
  └──────────────────────────────────────────▲──────────────────────────────────────────────┘
                                             │ J10
                         USB-C 0.6 m ────────┘
                         M1 magnetic USB-C breakaway, PD 100 W rated (shoulder strap)
                         USB-C 1 m coiled, 3 A
                         USB-PD bank 25 Ah, 15 V / 3 A PDO (plate-carrier pouch)

  GOGGLE: display driver board (right-strap clip) ── panel FPC through the top vent channel ──► micro-OLED engine (inside)
```

### Quick-disconnects and breakaways

| Point | Part | Role |
|---|---|---|
| Body ↔ helmet power | M1 magnetic USB-C adapter rated for PD 100 W (20 V / 5 A), at the shoulder | **neck-safety breakaway**: separates at a few newtons when the cable snags. The Jetson reboots (≈ 40 s); safety wins over uptime |
| Weapon ↔ helmet | 3.5 mm plug in J1 | breakaway; firmware ignores the open-circuit (no false press: open = released) |
| Pod ↔ helmet | P2 GX12-6, P3 GX12-2, camera USB-A at the Jetson | screw-locking: they do not separate on a snag. The NVG mount's own breakaway (6.5–10 lbf on a G24) lets the pod hang on the 60 mm service loop. Swap a pod in ~1 min |
| ESP32 ↔ Jetson | USB-A at the Jetson | field swap |
| Jetson ↔ display | DP adapter, HDMI ribbon at the driver | field swap |

**Never mate or unmate P3 (IR) with S1 on ARM.** An open constant-current driver rises to
Vin − 3 V, and reconnecting dumps that into the LEDs. Put S1 on SAFE first.

## 5. Power distribution board (PDB) and IR interlock

70 × 30 mm perfboard in the cassette's power bay.

```
 J10 CH224K PD trigger (strap CFG for 15 V; verify with a PD tester)
  VBUS ──F1 3 A fast (ATM mini blade in Keystone 3568)──┬──────────┬───────────────────── P1 centre (+) ──► Jetson DC jack
                                                       │          │                      5.5 × 2.5 mm, 20 AWG, 15 cm
                                                 D10 SMBJ16A   C10 470 µF 25 V low-ESR
                                                       │          │
  GND ─────────────────────────────────────────────────┴──────────┴──┬────────────────── P1 sleeve (−)
                                                                     │
  15 V (after F1) ──F2 Bourns MF-R050 (0.5 A PTC)── S1 IR SAFE/ARM ──┬── U1 LDD-350L pin 1 (+Vin)
                                                                     │   U1 pin 4 (−Vin) ── GND
                                                                   R10 20 k                U1 pin 6 (+Vout) ── J11-1 ─► LED1 ─► LED2 ─► J11-2 ── U1 pin 5 (−Vout)
                                                                     │                     (−Vout is NOT GND: never tie pin 5 to pin 4)
                                                                     ├──────────────── Q1 gate          Q1 2N7000: drain ── U1 pin 3 (DIM), source ── GND
                                                                   R11 10 k   (gate = 15 × 10/30 = 5.0 V)
                                                                     │
                                                                    GND
                                       Q2 2N7000: drain ── Q1 gate, source ── GND,
                                                  gate ──R12 1 k── Jetson 40-pin J12 pin 32 (IR_EN, 3.3 V GPIO)
                                                  gate ──R13 100 k── GND
                                       Jetson J12 pin 30 (GND) ── PDB GND
```

| IR_EN (Jetson pin 32) | Q2 | Q1 gate | DIM | Illuminator |
|---|---|---|---|---|
| low, floating, Jetson off or booting (R13 pulls low) | off | 5 V | pulled to −Vin (< 0.5 V) | **OFF** |
| high (3.3 V) | on | ~0 V | released (open → ON) | ON, 350 mA |
| PWM 1 kHz | | | | dimmed (duty) |
| S1 on SAFE | – | – | – | **OFF**: the driver is unpowered |

The LDD-350L's DIM pin turns the driver ON when it is above 3.5 V or open, and OFF below 0.5 V
(Mean Well LDD-L datasheet). PWM must stay within 100–1000 Hz. This network is **fail-safe**:
every software, cable or power fault turns the IR off, and S1 is a hardware kill.

* **PWM and the camera.** Use 1 kHz and lock the camera exposure to a whole number of
  milliseconds. Every image row then integrates the same number of PWM periods and there is
  no banding. In V4L2, `exposure_time_absolute` is in 100 µs units, so use multiples of 10.
* **Pin 32 as PWM.** Enable the pin's PWM function with `sudo /opt/nvidia/jetson-io/jetson-io.py`.
  If your JetPack's pin list differs, any 3.3 V GPIO with software PWM at 1 kHz works.
* **Required interlock logic for the host software** (owned by the software side, not this
  doc): IR_EN may go high only when all of the following hold:
  * night/edge mode is active
  * the pod pitch from the IMU is within −45°…+30° of level, so it is not flipped up to stow
  * the pose stream is healthy (no `IMU_DEGRADED`)
  * the operator has armed it in software

  It must drop to low on any fault, and after 120 s without operator re-arm.

## 6. Connectors and pinouts

| Ref | Part | Location | Pin → signal |
|---|---|---|---|
| J1 | 3.5 mm TRS panel jack, M6 × 0.5 | ESP32 box, rear bay, facing down | T → R4 100 Ω → rail GPIO (S3: GPIO7, DevKitC: GPIO27); R → GND; S → GND |
| J2 / P2 | GX12-6 socket / plug | ESP32 box +X end / pod IMU pigtail | 1 3V3 (red) · 2 GND (black) · 3 SDA (blue) · 4 SCL (yellow) · 5 INT (green) · 6 RST (white) · shield → pin 2 **inside the ESP32 box only** |
| J10 | USB-C receptacle on the CH224K board | cassette, bottom wall | VBUS / GND (CC handled by the CH224K) |
| J11 / P3 | GX12-2 socket / plug | cassette, bottom wall / pod IR pigtail | 1 LED+ (red) · 2 LED− (black) |
| P1 | 5.5 × 2.5 mm right-angle plug | PDB → Jetson J16 | centre + |
| A1 | DP male → HDMI female passive adapter (DP++) | Jetson DP port | – |
| M1 | magnetic USB-C adapter, PD 100 W rated | shoulder strap | – |

ESP32 GPIO by env (from `firmware/include/board_pins.h`): `esp32s3` SDA 8, SCL 9, INT 5,
RST 6, rail 7; `esp32dev` SDA 21, SCL 22, INT 19, RST 18, rail 27.

I2C over the 0.35 m pigtail plus internal leads (~0.45 m, ≈ 90 pF with the shield) and the
1.8 kΩ effective pull-up gives \(t_r = 0.8473 \cdot 1.8\,k\Omega \cdot 90\,pF \approx 137\) ns.
That is inside the 300 ns Fast-mode limit, so 400 kHz stays (wiring.md §2.2).

## 7. Wire, fusing, strain relief

| Circuit | Normal / peak current | Wire | Protection |
|---|---|---|---|
| bank → J10 → F1 → bus | 1.0 / 1.65 A | USB-C cable rated 3 A | bank OCP + **F1 3 A fast** |
| bus → Jetson | 0.75 / 1.2 A | 20 AWG silicone, 15 cm | F1 |
| bus → LDD-350L | 0.16 A | 22 AWG | **F2 0.5 A PTC** (hold 0.5 A, trip 1.0 A) |
| LDD → LEDs | 0.35 A | 22 AWG twisted pair, 0.9 m | LDD short-circuit protection |
| USB 5 V loads | ≤ 0.5 A each | USB cables | carrier USB load switches (AP22811, current-limited) |
| bus transient / ESD | – | – | D10 SMBJ16A (16 V standoff, above the 15.75 V maximum PDO) + C10 470 µF |

Strain relief, every cable, both ends:

* **Pod:** PG7 glands seal and grip; tighten until the seal just bites the jacket.
* **ESP32 box:** TPU grommet clamped by the lid tongue, plus a cable tie to the anchor bar.
  J1 is a panel jack, never a PCB-edge jack.
* **Cassette:** cable ties through the floor tie slots across the barrel plug and each USB-A
  and DP plug.
* **Helmet:** saddles every 8–10 cm, a 60 mm service loop at the mount hinge, and a
  hook-and-loop wrap at the shroud.
* **Body:** a coiled cable from bank to M1, and the shoulder anchor. No cable may pull
  directly on a helmet-side connector.
* **Joints:** heat-shrink boots on every soldered joint and on the 3.5 mm plug
  (`cable_plug_boot`).
* **Flex test** at assembly and maintenance: continuity while flexing each cable end through
  ±90° 20 times.

## 8. Thermal

* **Jetson (11 W average).**
  * The developer kit is rated for 0–35 °C ambient. Night ops (5–25 °C) are inside that.
  * The fan pulls air in through the lid grille, which faces away from the head. Exhaust
    leaves through the top-wall slots under the drip hood.
  * **Never cover the cassette with a pouch.**
  * Throttling check: during the 20 min soak in [hw-maintenance.md](hw-maintenance.md),
    `tegrastats` must show no thermal or over-current throttling.
* **Day storage.** A dark ASA cassette in direct sun reaches 60–70 °C at the surface. ASA
  (HDT ~95 °C) is fine there; PETG would creep. Do not leave the helmet in a closed car with
  the bank attached.
* **Helmet and wearer.** The cassette stands 10 mm off the shell on the saddle, and the EVA
  pad and shell separate it from the head. Heat leaves outward.
* **Illuminator.**
  * Heat is 2 × ~0.65 W into a 40 × 40 × 11 mm sink (≈ 10–14 K/W in the vented bay), so the
    sink sits ≈ 18 K above ambient.
  * Junction ≈ ambient + 18 + 2 (tape) + 0.65 × 9 K/W (R_th,JS) ≈ ambient + 26 K, i.e.
    about 51 °C at 25 °C ambient. T_j max is 145 °C.
  * The sealed camera bay is separated from it by the divider.
* **IMU.**
  * The camera board (~1 W) warms the sealed bay by ~5–10 K, and BNO085 gyro bias moves with
    temperature.
  * Power up and wait **5 min** before tare/calibration at the staging datum.
* **Bank.** Carry it inside the plate carrier or a jacket pocket, not on the outside, in cold
  weather.
