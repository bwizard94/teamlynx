# IR illuminator: eye safety and opfor detectability (850 vs 940 nm)

This extends `docs/hardware/camera-illuminator.md` (Phase 3) to the field build: **2 × ams
OSRAM SFH 4715AS** (850 nm, OSLON Black, 90°) in series at **350 mA** from a Mean Well
LDD-350L. They sit behind a 60° holographic diffuser and a 3 mm polycarbonate window, with the
fail-safe interlock in [hw-compute-power.md §5](hw-compute-power.md#5-power-distribution-board-pdb-and-ir-interlock).

## 1. Radiant intensity of the build

The datasheet gives \(I_e\) = 780 mW/sr typical (500–1000) at 1 A for the 90° part; the newer
80° revision is 900 mW/sr. Intensity is close to linear in current, so at 350 mA:

\[ I_e \approx 0.35 \times 0.78\text{–}0.90 = 0.27\text{–}0.32\ \text{W/sr per emitter} \]

| Term | Value |
|---|---|
| 2 emitters, on axis (21 mm apart, far field) | 0.55–0.63 W/sr |
| × window transmission at 850 nm (~0.88) | **0.48–0.56 W/sr** |
| design value (worst-case bin, no credit for the diffuser) | **0.6 W/sr** |

## 2. Eye-safety check (IEC 62471)

### 2.1 Corneal / lens IR hazard (780–3000 nm), irradiance \(E = I/d^2\)

| Distance | E (W/m²) | IEC 62471 group for that exposure |
|---|---:|---|
| 1 m | 0.6 | Exempt (limit 100 W/m², t > 1000 s) |
| 0.3 m | 6.7 | Exempt |
| 0.2 m | 15 | Exempt |
| 0.1 m (someone inspecting the helmet) | 60 | Exempt |
| 0.05 m | 240 | over the Exempt limit; inside RG1 (570 W/m², 100 s) |
| 0.02 m (eye pressed to the window) | 1500 | RG2 (3200 W/m², 10 s) |

At very short range the point-source formula overestimates, because the two emitters stop
adding on axis. The table is conservative.

### 2.2 Retinal thermal hazard, weak visual stimulus (780–1400 nm), radiance

The limit is \(L_{IR} \le 6000/\alpha\) W/(m²·sr), with \(\alpha \ge 0.011\) rad.

| Case | Apparent source | Radiance | α at 0.2 m | Limit | Result |
|---|---|---|---|---|---|
| Without diffuser | ~1.5 mm | ≈ 0.3 / 1.8 × 10⁻⁶ = 1.6 × 10⁵ W/(m²·sr) | 0.011 (floor) | 5.4 × 10⁵ | Exempt, 3× margin |
| With the diffuser | ≥ 10 mm patch | ≈ 3.6 × 10³ W/(m²·sr) | 0.05 | 1.2 × 10⁵ | Exempt, 30× margin |

**Conclusion.** The build is Exempt at every distance of 10 cm or more. The only real hazard
is someone putting an eye within a few centimetres of a live illuminator. NIR triggers no
blink reflex, so the controls below are mandatory, not advisory.

### 2.3 Controls

1. **Current is set in hardware.** The LDD-350L is a fixed 350 mA constant-current driver.
   No software bug can raise it. Do not substitute a higher-current LDD or add emitters
   without redoing §1–2.
2. **The diffuser is always fitted** (`pod_ir_diffuser_film.dxf`). It is the radiance margin.
3. **S1 SAFE/ARM**, guarded toggle on the rear cassette. SAFE during handling, transport,
   kitting and maintenance, and whenever the helmet is off.
4. **Fail-safe DIM interlock.** Jetson off, booting, crashed or unplugged all mean IR off
   (Q1/Q2 network).
5. **Software interlock** (requirements for the host software; see hw-compute-power.md §5):
   * IR only in night/edge mode
   * IR off when the pod is pitched up to stow (−45…+30° window) or IMU health is degraded
   * re-arm timeout of 120 s
6. **Never** check the illuminator by looking at it, or with a phone camera at close range.
   Check it with the rig's own camera and HUD, or with a card at 1 m viewed through the HUD.
7. Label the pod: "IR EMITTER — INVISIBLE — DO NOT STARE" on the bezel's top rim.
8. Z87.1 eyewear is an **impact** standard, not an NIR filter. It does not protect other
   players.

## 3. Detectability by opfor: 850 vs 940 nm

| | 850 nm (SFH 4715AS) | 940 nm (SFH 4725AS, same OSLON Black footprint) |
|---|---|---|
| Our camera's response (IMX462, relative) | 1.0 | ≈ 0.45–0.55 |
| Our illuminated range (same current; range ∝ √signal) | 1.0 | ≈ 0.7 |
| Naked eye, dark-adapted, looking at the emitter | **faint red glow**, visible at short range (metres to tens of metres) | effectively invisible |
| Gen 2/3 image-intensifier NV (GaAs photocathode, response falling past ~900 nm) | **bright beacon** at hundreds of metres | still visible, several times dimmer |
| Digital NV (CMOS, e.g. Pard / Sightmark / ATN; most airsoft opfor NV) | bright | **bright**: these often ship their own 850/940 nm illuminators and see both |
| Phone cameras (front cameras often lack an IR-cut filter) | visible | visible |

Doctrine:

* **Any active IR is a beacon to anyone with NV.** 940 nm removes the naked-eye glow, not the
  NV signature. Since most airsoft opfor NV is digital, 940 nm buys little and costs ~30 % of
  range.
* **Default: 850 nm, illuminator OFF.** Use the IMX462's starlight sensitivity passively
  first. Use IR in short bursts, dimmed by PWM duty, angled at the ground ahead rather than at
  head height, and never while static in a hide.
* Use 940 nm (swap to SFH 4725AS and re-check the radiant intensity in §1) only for a game
  where the opfor's NV is known to be intensifier-tube (Gen 2/3), and accept the range loss.
* A sealed camera bay with the IR on the other side of the divider means no IR leaks through
  the camera window. Black gaskets block light-piping around the window edges.
* Check field rules. Some sites restrict IR illuminators or any IR emitter on a player.
