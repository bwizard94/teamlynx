# Wiring: ESP32 ↔ BNO085, rail switch, power

The pin assignments here match `firmware/include/board_pins.h`. If you change one, change the
other.

Conventions in the diagrams: `──` is a wire, `┬`/`┴` are junctions, `[R 2k2]` is a resistor,
`||` is a capacitor, and `⏚` is GND. Every signal is 3.3 V logic. **No ESP32 GPIO is 5 V
tolerant.**

## 1. Pin tables

### 1.1 ESP32-DevKitC (WROOM-32 / 32E), env `esp32dev`

| Function | ESP32 GPIO | DevKitC header pin label | BNO085 / switch pin | Notes |
|---|---|---|---|---|
| I2C SDA | GPIO21 | `21` | SDA | 2.2 kΩ pull-up to 3V3 (see §2.2) |
| I2C SCL | GPIO22 | `22` | SCL | 2.2 kΩ pull-up to 3V3 |
| BNO085 interrupt | GPIO19 | `19` | INT (H_INTN) | active low. Firmware enables the internal pull-up. Optional: firmware polls every 20 ms without it |
| BNO085 reset | GPIO18 | `18` | RST (NRST) | active low, driven push-pull (10 ms low pulse). Optional: firmware falls back to a soft reset |
| Rail switch | GPIO27 | `27` | switch tip (via 100 Ω) | internal pull-up. Switch closes to GND. Active low |
| Status LED | GPIO2 | on-board | – | solid = streaming, 2 Hz blink = IMU missing, dark flash = gesture |
| 3.3 V | 3V3 | `3V3` | VIN (Adafruit) / 3V3 (SparkFun) | from the DevKitC's LDO |
| GND | GND | `GND` | GND, switch sleeve | one common ground |
| USB | micro-USB / USB-C | – | host | CP2102/CH340 bridge, `/dev/ttyUSB0` / `COMx`, 460800 baud |

These pins were chosen to avoid strapping pins (0, 2 as an input, 5, 12, 15), the flash pins
(6–11), the PSRAM pins on WROVER modules (16, 17), and the input-only pins 34–39, which have no
pull-ups.

### 1.2 ESP32-S3-DevKitC-1, env `esp32s3`

| Function | GPIO | BNO085 / switch | Notes |
|---|---|---|---|
| I2C SDA | GPIO8 | SDA | 2.2 kΩ pull-up |
| I2C SCL | GPIO9 | SCL | 2.2 kΩ pull-up |
| INT | GPIO5 | INT | |
| RST | GPIO6 | RST | |
| Rail switch | GPIO7 | switch tip | |
| Status LED | on-board RGB (GPIO48 on v1.0, GPIO38 on v1.1) | – | green = streaming, blinking red = IMU missing, blue flash = gesture |
| USB | **port labelled `USB`** (GPIO19/20, native) | host | `/dev/ttyACM0`. The `UART` port is not used for data |

These pins avoid the strapping pins (0, 3, 45, 46), the USB pins (19, 20) and the octal-PSRAM pins
(33–37 on N8R8/N16R8 modules).

### 1.3 BNO085 breakout specifics

| | Adafruit 4754 (BNO085) | SparkFun VR IMU (BNO08x, Qwiic) |
|---|---|---|
| Supply | **VIN 3–5 V** (on-board 3.3 V LDO). Wire it to the ESP32's 3V3 | **3.3 V only.** Never connect 5 V |
| I2C address | 0x4A default. 0x4B with DI pulled high | 0x4B default. 0x4A with the ADR jumper |
| On-board pull-ups | 10 kΩ on SDA/SCL | 2.2 kΩ (jumper; cut it if you fit your own) |
| Protocol select | P0/P1 low (default) selects I2C | PS0/PS1 low (default) selects I2C |
| Connector | STEMMA QT (JST-SH 4-pin) + 0.1" header | Qwiic (JST-SH 4-pin) + 0.1" header |

The firmware probes 0x4A and then 0x4B, so both boards work unmodified. **Exactly one** BNO085
belongs on the bus.

## 2. ESP32 ↔ BNO085 (I2C)

### 2.1 Diagram (ESP32-DevKitC + Adafruit 4754)

```
         ESP32-DevKitC                                     Adafruit BNO085 (4754)
       ┌───────────────┐                                  ┌──────────────────────┐
       │           3V3 ├──┬───────┬───────┬──────────────┤ VIN                  │
       │               │  │       │       │   C1 100nF   │                      │
       │               │ [R1     [R2      ├──||──⏚       │ 3Vo  (n/c)           │
       │               │ 2k2]    2k2]     │   C2 10µF    │                      │
       │               │  │       │       └──||──⏚       │                      │
       │        GPIO21 ├──┴───────┼──────────────────────┤ SDA   (10k on board) │
       │        GPIO22 ├──────────┴──────────────────────┤ SCL   (10k on board) │
       │        GPIO19 ├─────────────────────────────────┤ INT                  │
       │        GPIO18 ├─────────────────────────────────┤ RST                  │
       │           GND ├─────────────────────────────────┤ GND                  │
       │               │                                  │ P0, P1, DI, CS (n/c) │
       └───────────────┘                                  └──────────────────────┘
   C1/C2 sit at the BNO085 end of the cable. R1/R2 sit at the ESP32 end.
```

For a STEMMA QT/Qwiic cable, the 4-pin connector carries 3V3, GND, SDA and SCL, colour-coded
red, black, blue and yellow. Run INT and RST as two extra wires. Keep them in the same bundle as
GND.

### 2.2 Pull-up sizing (I2C Fast-mode, 400 kHz)

Fast-mode requires a rise time \(t_r \le 300\) ns. For an RC rise from 30 % to 70 % of VDD,
\(t_r = 0.8473\,R_p C_b\).

| Pull-up seen by the bus | \(C_b\) = 50 pF (≤ 10 cm) | 100 pF (~30 cm cable) | 200 pF (~1 m) |
|---|---|---|---|
| 10 kΩ (Adafruit alone) | 424 ns ✗ | 847 ns ✗ | ✗ |
| 10 k ∥ 2.2 k = 1.8 kΩ | 76 ns ✓ | 153 ns ✓ | 305 ns ≈ limit |
| 2.2 kΩ (SparkFun alone) | 93 ns ✓ | 186 ns ✓ | 373 ns ✗ |

The Adafruit board's 10 kΩ alone is out of spec at 400 kHz. It usually works on a 5 cm STEMMA
cable, but that is the wrong margin for a helmet rig. Fit **R1 = R2 = 2.2 kΩ** to 3V3. The sink
current at 1.8 kΩ is \(3.3/1800 = 1.8\) mA, inside the 3 mA Fast-mode limit.

For runs longer than about 1 m (e.g. helmet IMU to a belt-mounted ESP32), either drop to 100 kHz
(`-DLYNX_I2C_HZ=100000` in `build_flags`) or use an I2C bus extender such as the NXP P82B715 or a
differential I2C buffer. A better option is to move the ESP32 onto the helmet and run USB down
the cable instead. That is the recommended Phase 4 layout.

The BNO085 stretches the I2C clock. The ESP32 Arduino core handles this, and the firmware sets a
50 ms transaction timeout. If the status counters show resets climbing at 400 kHz, try 100 kHz
before you suspect the sensor.

### 2.3 Magnetic placement (Rotation Vector mode)

* Keep the BNO085 **≥ 10 cm** from steel, speakers, buzzers, motors, magnets, batteries and any
  wire that carries more than about 100 mA. A battery pack's current loop is a moving magnetic
  disturbance.
* Mount it rigidly. A flexing bracket shows up as pitch and roll noise.
* Note the axis arrows printed on the breakout. You need them for the `mount` spec
  ([imu-calibration.md](imu-calibration.md)).

## 3. Rail switch

### 3.1 Minimum (bench)

```
   ESP32 GPIO27 ───────────────┐ tip
   (INPUT_PULLUP ≈ 45 kΩ)      ○  momentary NO switch
   GND ────────────────────────┘ sleeve
```

The firmware's 20 ms integrating debounce rejects normal contact bounce, so this works as is.

### 3.2 Recommended (rig / field): RC filter, series protection, ESD

```
                           3V3
                            │
                          [R3 10k]  external pull-up: stiffer than the ~45k internal one
                            │
  ESP32 GPIO27 ──[R4 100Ω]──┼────────────┬───────────────── TIP ┐  3.5 mm TRS panel jack J1
                            │            │                       │      (on the enclosure)
                        C3 ═╪═ 100nF   D1 ▼ TVS PESD5V0S1BL      │
                            │            │                       ○  rail pressure switch (NO)
  GND ──────────────────────┴────────────┴───────────────── SLEEVE┘  (RING unused / tied to sleeve)
```

| Part | Value | Why |
|---|---|---|
| R3 | 10 kΩ to 3V3 | Release rise time is τ = R3·C3 = 1 ms. Gives better noise immunity on a 1 m cable than the internal ~45 kΩ (τ ≈ 4.5 ms) |
| R4 | 100 Ω series | Limits ESD and short-circuit current into the GPIO. Press discharge τ = R4·C3 = 10 µs |
| C3 | 100 nF X7R, at the ESP32 | Shunts RF pickup and cable spikes. Debounce stays in firmware |
| D1 | Nexperia PESD5V0S1BL (or any ≤ 6 V unidirectional TVS) at the jack | ESD from a gloved hand on the plug |

Measured from the raw edge, the filter adds at most 1 ms. The firmware timestamps the **raw
edge**, not the debounced decision, so the aim time is unaffected.

### 3.3 Gestures (firmware `RailSwitch`)

| Gesture | Definition | Default action (sim + headset) |
|---|---|---|
| SINGLE | press–release, then no new press within 300 ms | ping of the selected type at the reticle |
| DOUBLE | second press within 300 ms of the first release | CONTACT ping |
| LONG | held ≥ 700 ms (fires while still held) | cancel my last ping |

The parameters are `RailConfig{debounce_ms = 20, double_window_ms = 300, long_ms = 700}` in
`firmware/lib/LynxCore/src/rail_switch.h`.

### 3.4 Cable, connector and strain relief

* Use a **remote pressure switch** with a 2.5 mm or 3.5 mm plug. These are the common
  flashlight/laser tape switches. Mount it on the rail or the foregrip with its clip or Velcro.
  For DIY, an Omron D2F-01 microswitch potted in heat-shrink works.
* Put the jack (J1) **on the ESP32 enclosure, not on a PCB edge**. A snagged cable should pull the
  plug out instead of ripping the jack off the board. A plug that pulls out is the breakaway.
* Use a coiled or 2-conductor shielded cable about 0.8–1.2 m long, from the weapon to a chest or
  shoulder routing point and then to the helmet or belt enclosure. Drain the shield to GND at the
  ESP32 end only.
* Strain relief: anchor the cable to the enclosure with a P-clip or a cable-tie mount within 3 cm
  of the jack, leaving a short service loop before the jack. Anchor it again at the shoulder
  strap so weapon movement never loads the connector.
* Add a heat-shrink boot over the plug–cable joint. Plug failures in the field are almost always
  at that joint.
* Check continuity with a multimeter while you flex the cable at both ends. It must not open
  intermittently. An intermittent open looks like a double-click.

## 4. Power

```
  Host USB port (laptop / Jetson / USB-PD pack via the SBC)
      │ 5 V, ≤ 500 mA
      ▼
  ESP32-DevKitC USB ──► on-board 3.3 V LDO (AMS1117 / similar) ──► 3V3 rail ──► BNO085 VIN
                                                                     └──► R1, R2, R3 pull-ups
  GND: USB shield / GND ── ESP32 GND ── BNO085 GND ── rail jack sleeve (single star point at the ESP32)
```

| Load | Typical current |
|---|---|
| ESP32 (Wi-Fi off, loop at 1 kHz) | 40–60 mA |
| BNO085 at 100 Hz rotation vector | ≈ 10–15 mA |
| Pull-ups, LED | < 5 mA |
| **Total from USB** | **< 100 mA** |

* Power comes from USB only. Do **not** also feed 5 V into the DevKitC's `5V` pin while USB is
  plugged in; there is no ideal-diode OR on most clones.
* Wi-Fi and Bluetooth stay off. The firmware never starts them, so there is no radio noise near
  the magnetometer and the current stays low.
* Put the decoupling capacitors C1 (100 nF) and C2 (10 µF) at the BNO085 if its leads are longer
  than about 10 cm.
* Phase 4: the USB-PD pack powers the Jetson/SBC, and the ESP32 hangs off one of its USB-A ports.
  Use a short, locking USB cable or one secured with hot glue and a P-clip.

## 5. Bring-up checks before you plug in the sensor

1. With USB disconnected, measure 3V3 to GND. It must read more than 1 kΩ, not a short.
2. With USB connected and the BNO085 disconnected, 3V3 should read 3.25–3.35 V.
3. Connect the BNO085. SDA and SCL should idle at about 3.3 V, RST at 3.3 V, and INT should pulse
   low at the report rate once the firmware is running.
4. Then follow [bench-test.md](bench-test.md).
