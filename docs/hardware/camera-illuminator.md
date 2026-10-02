# NoIR camera and 850 nm illuminator: selection and eye safety

## 1. Camera

| Requirement | Why | How to check |
|---|---|---|
| **No IR-cut filter** ("NoIR", or day/night with the filter removable) | normal cameras block wavelengths above about 650 nm, and the illuminator is useless behind a filter | point a TV remote at the lens: the LED must look bright white or purple |
| Sensor with high NIR quantum efficiency | the sensor's QE at 850 nm sets range per watt of illuminator | Sony **IMX462** > IMX291 > IMX219/OV2710. IMX462 ("Starlight") roughly doubles the 850 nm response of IMX291 |
| UVC (USB Video Class) | OpenCV opens it with no driver (`--source 0` in `lynx-headset`) | `v4l2-ctl --list-formats-ext -d /dev/video0` |
| MJPEG ≥ 1280×720 at 30 fps (60 preferred) | USB 2.0 bandwidth. Raw YUYV 720p tops out at about 10 fps | same command, under `MJPG` |
| Known, fixed HFOV | the pinhole model's only intrinsic is HFOV at the delivered resolution (`--hfov`). For real lenses, calibrate (spatial-math §4) | measure: put two marks at the image edges, then HFOV = 2·atan(half-width / distance) |
| Fixed focus, M12 mount | swappable lens, no autofocus hunting in the dark | – |
| Rolling shutter acceptable | head-motion skew is small at 60 fps. Global shutter (OV9281, mono NIR) is an upgrade, not a requirement | – |

Mount the camera boresighted with the head frame: optical axis = body x, image right = −body y.
The residual misalignment goes into `CameraMount` (spatial-math §1.3). Put the camera and the IMU
on the **same rigid bracket**, so the boresight calibration stays valid.

## 2. Illuminator wavelength

| | 850 nm | 940 nm |
|---|---|---|
| Sensor QE (silicon) | about 2× the 940 nm value | low |
| Visible glow | faint red dot visible to the naked eye at night | effectively invisible |
| Seen by opponents' NV (Gen 2/3 tubes, digital NV) | **yes, bright** | yes (digital and Gen 3 see 940 nm too, somewhat less) |
| Choice | **850 nm** for range and image quality | use only when stealth matters more than range |

Any active NIR illuminator is a beacon to anyone with night vision. Use it in short bursts, dim
it (PWM through the LDD-350L `DIM` input), and check your field's rules on IR illuminators.

## 3. Eye safety

NIR (780–1400 nm) reaches the retina but triggers **no blink or aversion reflex**, and you cannot
see how bright it is. LED sources are assessed under **IEC 62471** (photobiological safety of
lamps and lamp systems). Two hazards apply:

| Hazard (IEC 62471) | Quantity | Exempt-group limit (long exposure) |
|---|---|---|
| Infrared radiation hazard to the cornea and lens (780–3000 nm) | irradiance \(E_{IR}\) at the eye | 100 W/m² for t > 1000 s |
| Retinal thermal hazard, weak visual stimulus (780–1400 nm) | radiance \(L_{IR}\) | \(6000/\alpha\) W/(m²·sr), with α = the source's angular subtense in rad (α ≥ 0.011) |

Verify these limits against the edition of the standard you use. Ask the LED or module vendor for
the **IEC 62471 risk group** at your drive current, and design for **Exempt or RG1**.

**Worked check (corneal irradiance).** An emitter with radiant intensity \(I\) [W/sr] produces
\(E = I/d^2\) on axis at distance \(d\). At 350 mA a single SFH 4715AS-class 90° emitter gives
roughly \(I \approx 0.3\) W/sr; take the real figure from the datasheet at your drive current.

| d | E = I/d² |
|---|---|
| 1 m | 0.3 W/m² |
| 0.3 m | 3.3 W/m² |
| 0.1 m (someone inspecting the helmet) | 30 W/m² |

These are below 100 W/m² with margin. Four emitters at 1 A each would put 0.1 m at about
4 × 3 × 30 W/m² = 360 W/m², over the limit. **Keep the drive current low, use a diffuser, and
never stack emitters in a narrow beam.**

Rules for the rig:

1. Use a diffuser. It increases the apparent source size α and so lowers the radiance, which is
   the retinal-hazard metric.
2. Limit the current in hardware. A fixed 350 mA driver cannot be "turned up" by a software bug.
3. Use a software and switch interlock. The illuminator turns on only in night/edge mode, and is
   off while the helmet is being handled. A tilt or "helmet removed" check from the IMU is a cheap
   Phase 4 addition.
4. Never look into the emitter, and never check it with a phone camera at close range. Use the
   rig's own camera and the HUD.
5. Z87.1 eyewear is an impact standard, not an NIR filter. Do not count on it to protect other
   players' eyes from your illuminator.

## 4. Behind the ballistic window

* Polycarbonate transmits about 85–90 % at 850 nm. Some anti-fog, anti-scratch or tinted coatings
  block NIR, so test the actual window with the camera.
* **Optically separate the illuminator from the camera.** NIR reflecting off the inside of a
  shared window floods the image (veiling glare). Use separate windows, or an opaque foam baffle
  pressed against the window between the emitter and the lens.
* Treat the window as sacrificial. Scratches scatter NIR far more than visible light; replace the
  window when the edge-mode image hazes.
