# Display integration behind ANSI Z87.1+ full-seal goggles

The hard constraint is that the eye stays behind an **intact, unmodified, Z87.1+ rated** lens
at all times. Nothing is drilled, glued or clamped to the lens. The display either lives
inside the goggle cavity (fixed to the frame), or is viewed through the goggle lens from
outside.

## 1. What keeps the ballistic rating intact

The Z87.1+ marking (high-velocity impact; for goggles, a 6.35 mm steel ball at 76 m/s) covers
the **lens + frame combination as tested**. These rules keep that true:

1. **The lens is never drilled, cut, heated, painted, engraved or bonded.**
   * No adhesive tape on the lens.
   * No solvent contact: acetone, MEK, ammonia glass cleaner, cyanoacrylate fumes,
     threadlockers and many generic anti-fog sprays cause environmental stress cracking in
     polycarbonate.
   * Clean with water, mild soap and a microfibre cloth only.
2. **Nothing is fixed to the lens, and nothing rigid sits within 3 mm of the lens's inner
   face.** On impact the lens deflects inward by several millimetres. Anything stiff behind
   it is driven toward the orbit.
3. **The frame is not modified.**
   * No holes. Strap anchors are not cut.
   * The display cable enters through the frame's **existing vent channel** (lift the vent
     foam, lay the 0.3 mm FPC under it, press the foam back), never through a new hole.
4. **The OEM lens only.** An aftermarket lens voids the rating, even if it is "the same
   thickness".
5. **The inside parts are soft-mounted and light.**
   * The engine plus carrier must weigh ≤ 25 g.
   * Use rounded edges and TPU carrier arms that fold outward under load.
   * Carry it on the frame or on the OEM prescription-insert (Rx) mount, never on the face
     foam.
6. **The goggle stays seated.** Clip the display cable to the strap with slack, so a pull on
   the cable cannot lever the goggle off the face. The strap tension is the manufacturer's.
7. **Replace the goggle** after any hit that leaves a crack, craze or chip, and after any
   solvent exposure.
8. **Airsoft fields.** Many require **ASTM F2879** (the airsoft eye-protection standard), not
   Z87.1. Use a goggle marked for both where possible (many military-spec goggles are Z87.1+
   and MIL-PRF-32432), and check the field's rule.

## 2. Options

| # | Approach | Ballistics | Optics | Fit / risk | Verdict |
|---|---|---|---|---|---|
| A | **Monocular micro-OLED engine inside an OTG / Rx-insert goggle**, carried on the Rx mount; driver board outside on the strap; FPC through the vent channel | lens and frame untouched | Sony ECX336B 0.23" 640 × 400 + 22× eyepiece: f = 250/22 ≈ 11.4 mm, active width ≈ 4.95 mm → **FOV ≈ 24°**, ~27 px/° | needs cavity depth ≥ eye relief + engine length + 3 mm (§3) | **recommended v1** |
| B | Larger 1080p engine (Enmesi R6-class: ECX335 0.7", 51° FOV, 15 mm eye relief, 18.5 × 20.3 mm, 14 g) inside the goggle | untouched | big FOV and resolution | usually too deep for a goggle cavity | only if §3 passes |
| C | Monocular 1080p engine (Sony ECX335 0.71" + Display Components HDMI board, 37.5 × 16.5 mm, 3.5–5 V) **in front of** the goggle on an arm, viewed through the lens | untouched; display not protected (sacrificial) | ~40° FOV with an EVF eyepiece | adds front mass to the NVG mount; eye relief is long (lens in between) | fallback if A does not fit |
| D | Birdbath AR glasses (XREAL / VITURE) worn inside an OTG goggle | untouched | 46–52° FOV, binocular | need USB-C DP Alt Mode (the Orin Nano dev kit has DP only); **block 70–80 % of ambient light**, which is bad at night; fit inside OTG frames is marginal | not recommended |
| E | Any display bonded to, drilled through or replacing the lens | **voids the rating** | – | – | **forbidden** |

**Eye choice.**

* Default: the **non-dominant** eye. The dominant eye stays dark-adapted and aims naturally
  through the weapon's sight.
* Use the dominant eye only if the operator aims *through* the HUD edge view (its reticle is
  then the aim point).

## 3. Fit check (do this before buying 10 kits)

Measure with a depth gauge on a head form or a volunteer:

* **D**: distance from the cornea to the lens's inner surface, at the engine's location (the
  lower-outer or upper-outer quadrant, out of the primary line of sight).
* **ER**: eye relief of the engine.
* **L**: engine length along its axis.

The requirement is \(D \ge ER + L + 3\) mm.

| Candidate | ER (typ.) | L (approx.) | Needs D ≥ |
|---|---|---|---|
| ECX336B kit, 22× eyepiece | 10–12 mm | 14–16 mm | **27–31 mm** |
| 0.7" 1080p engine (R6-class) | 15 mm | 20+ mm | 38+ mm |

OTG and Rx-insert goggles give D ≈ 25–35 mm. Candidates, each carrying Z87.1+ and
MIL-PRF-32432 markings on the SKUs we checked:

* **ESS Influx AVS**, with the U-Rx insert mount
* **Revision Desert Locust**, with an Rx carrier
* fan-ventilated goggles, for example the Smith Optics Elite Boogie Regulator class

Confirm the **`Z87+` marking on both the lens and the frame** of the exact SKU you buy. Markings
differ between lens options.

Mount the engine low and to the outside of the chosen eye, angled ~15° toward the pupil, so the
primary line of sight stays clear. The operator glances at it, like a monocle.

## 4. Signal and power chain

```
Jetson DP (DP++ dual-mode) ── A1 passive DP→HDMI adapter ── HDMI FPC ribbon kit 0.5 m (right side of helmet)
     ── HDMI driver board (ECX336B kit board; on a strap clip at the right temple, outside the goggle)
     ── panel FPC (0.3 mm) under the top vent foam ──► engine inside the goggle
Jetson USB-A #3 ── 5 V, 0.5 m ──► driver board power (≈ 1.0–1.5 W)
```

* Set the Jetson output to the driver board's EDID-preferred mode and render the HUD at the
  panel's native resolution, so the image is not scaled:
  `xrandr --query`, then `xrandr --output DP-0 --mode <preferred>`.
* The HUD (`lynx/hud/`) draws on the camera frame. In night/edge mode the micro-display shows
  the edge-mode video with the overlays, which is video see-through. The camera HFOV (80–100°)
  is wider than the display FOV (24°), so the renderer must either crop to the central 24° at
  1:1 angular scale, so world pings stay perspective-correct, or show the full camera FOV
  minified. Expose that choice as a HUD option; the default is a 1:1 crop.
* **Night brightness.** Micro-OLEDs reach 2000–3000 nits; at night the panel must run at
  **< 5 nits**. Use the driver board's brightness control at minimum, a dark HUD palette
  (green/amber, black background), and if needed an ND 1.0–2.0 filter on the eyepiece.
* A soft rubber eyecup on the eyepiece keeps the glow off the inner lens face. A goggle lit
  from inside is visible to opfor at night.

## 5. Anti-fog

* Buy a **dual-pane (thermal) lens with a factory anti-fog coating inside**. Do not wipe the
  inside dry: blot it, or the coating is destroyed.
* Fan goggles help most during exertion. Run the fan from the display's 5 V only if the fan
  draws < 100 mA. Keep it on the strap, never inside the cavity.
* The engine dissipates ~0.2–0.4 W inside the cavity. That warms the air locally and slightly
  helps the lens, but the **eyepiece glass fogs first**. Treat it with a lens-safe anti-fog
  (the same product used on camera viewfinders) and test it on a spare.
* Put the goggle on at the staging area, not after the sprint. Keep the vent foam clear and
  never tape it over.
* The pod window is a separate problem: the sealed, desiccated camera bay plus the ePTFE
  breather handle it ([hw-mechanical.md](hw-mechanical.md) §7).

## 6. Candidate parts

| Part | Example | Key spec | Price (USD) |
|---|---|---|---|
| Goggle | ESS Influx AVS (U-Rx insert) / Revision Desert Locust (Rx carrier) | Z87.1+ on lens and frame, dual-pane anti-fog, OTG or Rx space | 80–110 |
| Display engine | YX Microdisplay 0.23" Sony ECX336B kit: panel + 22× eyepiece + HDMI/CVBS driver | 640 × 400, up to 2400 nits, 5 V | ~175 |
| Alt engine (option C) | Sony ECX335 0.71" 1080p + Display Components HDMI driver board (37.5 × 16.5 mm, 3.5–5 V) + EVF eyepiece | 1920 × 1080 | 250–350 |
| DP→HDMI | passive DP++ adapter, DP male → HDMI female, compact | 1080p60 | 10 |
| HDMI ribbon | FPV "HDMI FPC ribbon" kit, 50 cm, with HDMI-A and mini/micro adapter boards | flat, 0.3 mm, ~8 g | 15 |
| Eyecup / ND | soft rubber eyecup; ND 1.0 gel cut to the eyepiece | | 5 |
