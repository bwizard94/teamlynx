# Phase 4 helmet hardware: bill of materials (per player)

Prices are typical 2026 USD street prices for small quantities. Expect ±30 % and check them
before ordering. "Share" lines are bought once per squad; the per-player figure is the
amortised cost. Phase 3 bench parts (`docs/hardware/bom.md`) that carry over are marked
**(P3 #n)**.

## Sensor pod

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 1 | 1 | UVC board camera, **no IR-cut**, Sony IMX462, 38 × 38 mm, M12, MJPEG 720p60 (P3 #16) | Arducam / ELP IMX462 USB module | 45 | 45 |
| 2 | 1 | M12 lens, 2.8–3.6 mm, F1.2–1.6, IR-corrected, HFOV 80–100° (P3 #17) | | 15 | 15 |
| 3 | 1 | BNO085 breakout (P3 #2) | Adafruit 4754 | 25 | 25 |
| 4 | 2 | 850 nm IR LED on a 20 mm MCPCB star (P3 #18) | ams OSRAM SFH 4715AS (940 nm alt: SFH 4725AS) | 7 | 14 |
| 5 | 1 | Heatsink 40 × 40 × 11 mm aluminium | generic | 3 | 3 |
| 6 | 1 | LED plate, 2 mm 5052 aluminium, 44 × 43 mm (DXF) | sheet offcut | 3 | 3 |
| 7 | share | Thermal adhesive tape | 3M 8810 | – | 4 |
| 8 | share | 3 mm polycarbonate, clear, uncoated / hard-coat only (P3 #21) | Makrolon GP / Lexan 9034, 300 × 300 sheet ≈ 20 windows | 20/sheet | 4 |
| 9 | share | 60° holographic diffuser film (P3 #20) | Luminit LSD 60° / Edmund equivalent | – | 5 |
| 10 | share | 1 mm black closed-cell neoprene/EPDM, adhesive one side | | – | 3 |
| 11 | 3 | PG7 nylon cable gland, IP68, 3–6.5 mm cable | | 0.7 | 2 |
| 12 | 1 | Adhesive ePTFE breather vent, 10 mm | Gore PolyVent-type / generic | 1 | 1 |
| 13 | share | Silica gel 1 g sachets | | – | 1 |
| 14 | 1 | GX12-6 aviation plug + panel socket | | 4 | 4 |
| 15 | 1 | GX12-2 aviation plug + panel socket | | 3 | 3 |
| 16 | 0.5 m | 6-core shielded cable, 26 AWG | | 4/m | 2 |
| 17 | 1 m | 2-core 22 AWG silicone, twisted, in 4 mm braided sleeve | | 2 | 2 |
| 18 | 1 | C1 100 nF, C2 10 µF at the BNO085 (P3 #5–6) | | – | 0.5 |

## NVG interface

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 19 | 1 | One-hole NVG shroud, Wilcox pattern (skip if the helmet has one) | repro, aluminium | 25 | 25 |
| 20 | 1 | NVG mount with dovetail receiver, G24 pattern | repro G24/L4G24 (airsoft); Wilcox L4G24 ≈ 560 | 85 | 85 |

## ESP32 head tracker

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 21 | 1 | ESP32-S3-DevKitC-1-N8 (P3 #1a), or ESP32-DevKitC-32E (P3 #1) with the `esp32dev` STLs | Espressif | 15 | 15 |
| 22 | 1 | 70 × 30 perfboard, 2 × 22 female headers, R1–R4, C3, D1 PESD5V0S1BL (P3 #4, #10–13) | | 5 | 5 |
| 23 | 1 | 3.5 mm TRS panel jack, M6 × 0.5 thread (J1) | Lumberg / generic | 2 | 2 |
| 24 | 1 | USB cable, DevKit → USB-A, 0.5 m, right-angle | | 6 | 6 |
| 25 | 1 | ARC-to-Picatinny helmet rail adapter (or 25 mm strap + hook-and-loop for the strap mount) | airsoft accessory | 10 | 10 |
| 26 | 1 | Remote pressure switch, NO, 3.5 mm plug, ~1.2 m (P3 #8) | | 15 | 15 |

## Rear compute and power cassette

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 27 | 1 | **NVIDIA Jetson Orin Nano Super Developer Kit** (945-13766-0000-000) | 8 GB, 9–20 V DC in | 249 | 249 |
| 28 | 1 | NVMe SSD 256 GB (M.2 2230/2280), or a 128 GB UHS-I A2 microSD | | 30 | 30 |
| 29 | 1 | USB-C PD trigger, fixed **15 V** (J10) | CH224K-based decoy board | 3 | 3 |
| 30 | 1 | Constant-current LED driver 350 mA, 9–36 V in (U1, P3 #19) | Mean Well LDD-350L | 7 | 7 |
| 31 | 1 | F1 3 A ATM mini blade + PCB holder; F2 0.5 A PTC | Keystone 3568; Bourns MF-R050 | 3 | 3 |
| 32 | 1 | Q1, Q2 2N7000; R10 20 k, R11 10 k, R12 1 k, R13 100 k; D10 SMBJ16A; C10 470 µF 25 V; 70 × 30 perfboard | | 3 | 3 |
| 33 | 1 | Mini toggle SPST, ¼-40 bushing (S1) | C&K 7101 type | 6 | 6 |
| 34 | 1 | 5.5 × 2.5 mm right-angle DC plug pigtail, 20 AWG (P1) | | 3 | 3 |
| 35 | 2 m | 20 AWG silicone wire, red + black | | 1/m | 2 |
| 36 | 1 | Adhesive hook panel 100 × 100, 6 mm EVA, 1 m × 25 mm webbing, 2 side-release buckles | | 10 | 10 |

## Display

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 37 | 1 | ANSI Z87.1+ full-seal goggle with OTG/Rx space, dual-pane anti-fog | ESS Influx AVS (U-Rx) / Revision Desert Locust | 90 | 90 |
| 38 | 1 | Micro-OLED kit: Sony ECX336B 0.23" 640 × 400 + 22× eyepiece + HDMI driver | YX Microdisplay | 175 | 175 |
| 39 | 1 | Passive DP++ → HDMI adapter, DP male → HDMI female (A1) | | 10 | 10 |
| 40 | 1 | HDMI FPC ribbon kit, 50 cm | FPV gimbal type | 15 | 15 |
| 41 | 1 | USB-A → driver-board 5 V cable, 0.5 m | | 4 | 4 |
| 42 | 1 | Soft eyecup + ND 1.0 gel | | 5 | 5 |

## Power (body)

| # | Qty | Part | Example / spec | Unit | Ext |
|---|---|---|---|---:|---:|
| 43 | 1 | USB-PD bank, 24 000–27 000 mAh (86–99.9 Wh), **15 V ≥ 3 A PDO**, PD 3.0 | e.g. Anker 737 (24K) or UGREEN Nexode 25000 class. Verify the 15 V PDO on the label and with a tester | 90 | 90 |
| 44 | 1 | Magnetic USB-C breakaway adapter, rated PD 100 W (M1) | | 15 | 15 |
| 45 | 1 | USB-C ↔ USB-C coiled cable, 3 A, 1 m | | 12 | 12 |
| 46 | 1 | USB-C ↔ USB-C, 3 A, 0.6 m (shoulder → cassette) | | 8 | 8 |

## Fasteners, inserts, harness, filament

| # | Qty | Part | Unit | Ext |
|---|---|---|---:|---:|
| 47 | share | Brass heat-set inserts: M2 × 16, M2.5 × 8, M3 × 26 per headset (ruthex-type kit) | – | 6 |
| 48 | share | Stainless screws: M3 × 8 ISO 7380 × 16, M3 × 10 ISO 7380 × 6, M3 × 10 ISO 4762 × 2 + sealing washers, M3 × 8 ISO 4762 × 4 + M3 nuts × 4 (ESP32 mount), M3 × 30 ISO 4762 × 2 + DIN 985 nyloc × 2, M3 × 6 cone set screw × 1, M3 × 10 DIN 464 thumb screw × 1, M2 × 5 × 16, M2.5 × 6 × 4, M2.5 × 8 ISO 10642 × 4 | – | 10 |
| 49 | share | Braided sleeving 6 mm × 2 m, heat-shrink, cable ties, adhesive hook dots | – | 8 |
| 50 | – | Filament: ASA ≈ 270 g, PA-CF ≈ 75 g, PETG ≈ 110 g, TPU ≈ 15 g | – | 14 |

## Totals

| Group | USD |
|---|---:|
| Sensor pod | 137 |
| NVG interface (repro mount) | 110 |
| ESP32 head tracker | 53 |
| Rear cassette (Jetson + power) | 316 |
| Display | 299 |
| Power (body) | 125 |
| Fasteners, inserts, harness, filament | 38 |
| **Per player** | **≈ 1 078** |
| Squad of 10 | ≈ 10 800 |
| Squad-shared extras: spares kit ([hw-maintenance.md](hw-maintenance.md) §4) ≈ 450, PD tester 15, insert-press tip 10 | ≈ 475 |

Options:

* Wilcox OEM mount instead of a repro: +475 per player.
* Raspberry Pi 5 + Hailo-8 instead of the Jetson (v2 path, needs a detector port): −50 to
  −90 per player.
