# Bench rig bill of materials

The part numbers are examples that are known to fit the design. Any equivalent part works if it
matches the "key spec" column. Prices are omitted because they move too fast to be useful.

## Head tracker

| # | Qty | Part | Example part number | Key spec | Notes |
|---|---|---|---|---|---|
| 1 | 1 | ESP32 dev board | Espressif **ESP32-DevKitC-32E** | ESP32-WROOM-32E, USB-UART bridge | env `esp32dev`. Clones with CP2102 or CH340 also work |
| 1a | (alt) | ESP32-S3 dev board | Espressif **ESP32-S3-DevKitC-1-N8** | native USB | env `esp32s3`. Use the `USB` port |
| 2 | 1 | BNO085 breakout | **Adafruit 4754** | BNO085, I2C, 3–5 V VIN, STEMMA QT | alt: SparkFun VR IMU breakout (BNO08x, Qwiic, **3.3 V only**, default addr 0x4B) |
| 3 | 1 | 4-pin JST-SH cable, 100–200 mm | Adafruit 4210 (STEMMA QT, 100 mm) | 3V3/GND/SDA/SCL | add 2 wires for INT and RST |
| 4 | 2 | I2C pull-ups R1, R2 | Yageo RC0805FR-072K2L / any 1/8 W 2.2 kΩ | 2.2 kΩ ±1 % | see wiring §2.2 |
| 5 | 1 | C1 decoupling | Murata GRM21BR71H104KA01L / any 100 nF X7R | 100 nF, ≥ 10 V | at the BNO085 |
| 6 | 1 | C2 bulk | any 10 µF X5R/X7R ceramic, ≥ 10 V | 10 µF | at the BNO085 if leads are > 10 cm |
| 7 | 1 | USB cable | – | data-capable, ≤ 1 m | charge-only cables are the #1 bring-up failure |

## Rail switch

| # | Qty | Part | Example part number | Key spec |
|---|---|---|---|---|
| 8 | 1 | Remote pressure switch with 3.5 mm (or 2.5 mm) plug | generic "tactical remote pressure switch", or DIY: **Omron D2F-01** in heat-shrink + 2-core cable | momentary, normally open |
| 9 | 1 | 3.5 mm TRS panel jack J1 | CUI Devices **SJ1-3523N** (through-hole), or any panel-mount 3.5 mm stereo jack | panel mount on the enclosure |
| 10 | 1 | R3 pull-up | 10 kΩ 1/8 W | |
| 11 | 1 | R4 series | 100 Ω 1/8 W | |
| 12 | 1 | C3 filter | 100 nF X7R | |
| 13 | 1 | D1 ESD TVS | Nexperia **PESD5V0S1BL** (SOD-323) | ≤ 6 V working, unidirectional |
| 14 | 1 | Strain relief | P-clip 6 mm + adhesive cable-tie mount, heat-shrink boot | |
| 15 | 1 | Enclosure | any ABS/PC box ~ 60×40×20 mm | holds J1, the DevKitC and the pull-up board |

## Camera and illuminator (night/edge mode)

Selection rationale and eye safety are in [camera-illuminator.md](camera-illuminator.md).

| # | Qty | Part | Example | Key spec |
|---|---|---|---|---|
| 16 | 1 | USB NIR-capable camera | UVC board camera on a **Sony IMX462** (best 850 nm response) or IMX291 sensor, **no IR-cut filter**, M12 lens | UVC/V4L2, MJPEG 1280×720 ≥ 30 fps, known HFOV |
| 16a | (alt, Jetson CSI) | Raspberry Pi **Camera Module 2 NoIR** (IMX219) | CSI-2, supported by the Jetson Nano/Orin Nano device tree | CSI, not USB |
| 17 | 1 | M12 lens | 2.8–3.6 mm, F1.2–F2.0, IR-corrected | HFOV 80–100° to match the HUD `--hfov` |
| 18 | 1–4 | 850 nm LED | ams OSRAM **SFH 4715AS** (OSLON Black, 850 nm, 90° beam) | ≈ 1 A max; run at 350 mA |
| 19 | 1 | Constant-current LED driver | Mean Well **LDD-350L** (buck, PWM/DIM input) | 350 mA, dimmable from an ESP32 GPIO |
| 20 | 1 | Diffuser / lens | 60–90° holographic or opal diffuser film | lowers radiance (eye safety), evens illumination |
| 21 | 1 | Ballistic window | 3 mm polycarbonate (Lexan) **without IR-blocking coatings** | test NIR transmission with the camera before trusting it |
