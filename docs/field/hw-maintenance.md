# Field maintenance checklist

Tick every line. A failed line means that headset does not go out until it is fixed or swapped
from the squad spares kit (§4).

## 1. Pre-op (staging area, every game)

**Power**

- [ ] Bank charged ≥ 95 %. Inline PD tester shows **15.0 V** negotiated at J10 (14.5–15.75 V under load).
- [ ] Nothing else is plugged into the bank.
- [ ] M1 magnetic breakaway: clean faces (no grit, no ferrous dust), snaps home, and separates with a firm tug.
- [ ] Coiled cable and shoulder anchor in place, with no load on J10.

**IR**

- [ ] **S1 on SAFE** while handling.
- [ ] Interlock test, done in this order:
  1. Arm S1 with the Jetson off: IR must be **off**. Check on another rig's HUD at 1 m, never with the naked eye up close.
  2. Boot the Jetson with IR_EN low: still off.
  3. Arm in software: on.
  4. Flip the pod up: off.
  5. S1 to SAFE: off.

**Sensor pod**

- [ ] Windows: no cracks, crazing or haze. Replace if the edge-mode image shows veiling glare or halos around lights.
- [ ] Bezel screws seated (the bezel bottoms on the flange).
- [ ] Glands snug. Desiccant indicator still blue/orange (not saturated).
- [ ] Witness marks on the sled screws and lens ring unbroken. If broken, redo the boresight check before the op.
- [ ] Dovetail shoe: clicks into the mount, no rocking, gib witness mark aligned. Latch detent edge sharp, not rounded.
- [ ] 5 kg pull test on the pod. One-hand release works.

**Cables**

- [ ] Flex test: wiggle each pigtail at the gland and the plug while watching the HUD. No video dropouts, no `IMU_DEGRADED`, no phantom rail clicks.
- [ ] GX12 rings hand-tight.
- [ ] 60 mm service loop present at the mount hinge.
- [ ] Rail switch:
  - single click → ping
  - double click → CONTACT ping
  - hold ≥ 0.7 s → cancel
  - unplug the plug at J1 with the HUD running: no event fires

**Compute and display**

- [ ] Jetson boots to the HUD in 15 W mode (`nvpmodel -q`).
- [ ] Fan spins and the grille is clear.
- [ ] Display: diopter set for the operator, brightness at the night minimum, eyecup on.
- [ ] Goggle: lens intact, Z87+ marking legible, anti-fog not wiped, vent foam clear, FPC under the vent foam undisturbed.

**Calibration**

- [ ] Power on and wait 5 min (thermal soak), then IMU tare and datum calibration at the staging datum. Follow the staging-area procedure in the field networking/calibration docs.
- [ ] Heading cross-check against a known bearing: within ±3°.

**Soak**

- [ ] 20 min soak while kitting. `tegrastats` shows no throttling, and VDD_IN is ~10–13 W.

## 2. During the op

- [ ] Hourly: battery percentage on the HUD. Swap the bank at a hard break if it is below 30 %. The Jetson reboots on swap (~40 s), so do it out of contact.
- [ ] After any fall or helmet strike:
  - check the window and the shoe click
  - check the heading against a known bearing
  - re-tare if it is off by more than 3°
- [ ] If the window fogs inside, the desiccant is saturated. If it fogs outside, wipe with a microfibre cloth only.

## 3. Post-op (same night)

- [ ] **S1 SAFE.** Disconnect the bank.
- [ ] Wet gear: open the ESP32 box and cassette lids and dry for 12 h at room temperature. Do not open the pod camera bay unless water is visible inside.
- [ ] Wipe the windows, bezel and goggle lens with water and mild soap only. **No alcohol, ammonia, acetone or solvent-based anti-fog** on polycarbonate.
- [ ] Recharge banks to 100 %. Store them at 50–60 % if they will not be used for more than 2 weeks.
- [ ] Log faults, swaps and parts used in the squad log (date, headset ID, part, symptom).

**Every 5 ops or monthly**

- [ ] Replace desiccant sachets (or regenerate at 120 °C for 2 h).
- [ ] Re-torque the fasteners: bezel 0.4 N·m (to the stop), sled 0.5 N·m, lids 0.4 N·m.
- [ ] Inspect inserts for spinning.
- [ ] Re-check the dovetail with the gauge coupon. Reprint the PA-CF lid if the latch detent is worn.
- [ ] Blow dust out of the heatsink fins (pod) and the Jetson fan/fins (cassette).
- [ ] Continuity-test every pigtail while flexing it.
- [ ] Replace any cable with a cracked jacket at the gland.
- [ ] Bank health: time a full 15 V, 1 A discharge. Retire the bank when it delivers < 80 % of rated energy.

## 4. Squad spares kit (for 10 headsets)

| Item | Qty |
|---|---|
| Pre-cut 3 mm PC windows (cam + IR), gaskets, diffuser films | 6 sets |
| Bezel and lid screws (M3 × 8 / × 10 button), set screws, sled screws + washers | 20 / 4 / 6 |
| F1 3 A mini blade fuses; F2 0.5 A PTC | 10 / 3 |
| Rail pressure switch with 3.5 mm plug | 2 |
| Pod pigtail set (GX12-6, GX12-2 plugs with leads), USB-A camera extension | 2 |
| ESP32-S3-DevKitC-1, pre-flashed (record the firmware env on the lid) | 2 |
| Printed pod lid (shoe), bezel, ESP32 jaw, TPU grommets and boots | 2 each |
| Silica gel 1 g sachets | 20 |
| Charged spare PD bank (15 V PDO verified) | 2 |
| Inline USB-C PD tester | 1 |
| Microfibre cloths; paint pen; hex keys 1.5 / 2 / 2.5; small Phillips | 1 set |
| Magnetic USB-C breakaway | 2 |
