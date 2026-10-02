# Staging-area datum calibration

Every headset must agree on one world frame W: ENU metres from the staging **datum**, ground at
z = 0 ([spatial-math.md §1.1](../spatial-math.md)). Calibration gives each operator two things:

1. **position**: where the eye is in W at game start (station, GNSS, or resection);
2. **heading tare**: the offset that turns the head tracker's heading into a W heading.

Pitch and roll come from gravity and need no tare. Implementation:
[calibrate.py](../../lynx/field/calibrate.py) (math), [staging.py](../../lynx/field/staging.py)
(rail-switch workflow), [gnss.py](../../lynx/field/gnss.py) and
[geodesy.py](../../lynx/field/geodesy.py).

## Why heading accuracy matters

A heading error \(\delta\) displaces everything by \(\rho\,\delta\) at range \(\rho\):

| heading error | ping at 50 m off by | teammate at 100 m off by | on the HUD (f = 480 px) |
|---|---|---|---|
| 0.5° | 0.44 m | 0.87 m | 4 px |
| 1° | 0.87 m | 1.75 m | 8 px |
| 3° | 2.6 m | 5.2 m | 25 px |

The IFF associator assumes `angle_sigma_deg = 1.5`, so the tare should be good to well under 1°.

## Site file

[`deploy/lynx/site.example.json`](../../deploy/lynx/site.example.json):

```json
{
  "name": "Compound A staging",
  "datum": {"lat": 51.501234, "lon": -1.234567, "h": 85.2},
  "north_reference": "true",
  "declination_deg": 0.9,
  "eye_height_m": 1.7,
  "stations": {"S1": {"e": 0.0, "n": 0.0}, "S2": {"e": 3.0, "n": 0.0}, "S3": {"e": 6.0, "n": 0.0}},
  "markers": {
    "FLAG-N": {"e": 0.0, "n": 120.0, "u": 2.0},
    "MAST-E": {"e": 140.0, "n": 35.0, "u": 6.0},
    "GATE-W": {"e": -110.0, "n": -20.0, "u": 1.5}
  }
}
```

* **Datum:** a stake in the staging area. `lat`/`lon`/`h` (WGS84 ellipsoidal height) are needed
  only for GNSS mode. `lynx-field survey --gnss /dev/lynx-gnss --site site.json --write` averages
  the receiver over the stake for 120 s.
* **Stations:** painted spots where an operator stands to tare, surveyed relative to the datum
  with a tape. Several stations let operators tare in parallel.
* **Markers:** distinct, distant objects such as a flag pole, a mast or a gate post, with E/N and
  height `u` above the datum plane. Points may also be given as `lat`/`lon`/`h`.
* **`declination_deg`:** magnetic declination, east positive, from the NOAA/BGS calculator for
  the site. It is copied into the IMU calibration, so the BNO085's magnetic North becomes true
  North.
* **`north_reference`:** `"true"` makes the frame GNSS-native ENU. `"grid"` is for a site plan
  drawn on UTM or OSGB; set `convergence_deg` (east positive) and both the GNSS conversion
  (`LocalFrame` rotates ENU by −γ) and the IMU (`heading - convergence`) use grid North.

Lay markers out by the error budget below: the further the marker, the less the station or GNSS
position error matters.

## Operator workflow (hands-free)

`lynx-headset.service` runs the field launcher, which calibrates first when the profile says so
(`calibrate: if-stale`, default 12 h). It can also be run by hand:

```bash
lynx-field calibrate --site /etc/lynx/site.json --imu /dev/lynx-imu --cal /var/lib/lynx/imu.json \
    --station S1 --marker FLAG-N --marker MAST-E --out /var/lib/lynx --node 3 --display
```

1. The operator, wearing the headset, stands on station **S1** at the staging area. The helmet
   display shows `FACE FLAG-N BRG 000.0 - HOLD RETICLE ON IT AND TAP` with the live heading.
2. The operator puts the reticle on the flag, holds still, and **taps the rail switch** (SINGLE).
3. The tool averages the head tracker's attitude history from 0.56 s to 0.06 s **before** the
   press edge (the firmware timestamps the press), so the thumb's jolt never enters the tare. A
   sighting is rejected and re-prompted if the head moved (circular std > 1.5°) or was pitched
   more than 45°, where heading is ill-conditioned.
4. Repeat for the next marker. A **LONG** press redoes the previous marker.
5. The display shows `TARE +7.3 DEG +-0.3 POS E+0.0 N+0.0 (STATION)` and the tool writes
   `/var/lib/lynx/field-cal.json` (the full record) and `/var/lib/lynx/imu.json` (the
   `ImuCalibration` that `--pose serial:PORT?cal=` loads).

`--keyboard` uses Enter instead of the rail switch on the bench, and `--imu mock://still` runs
without hardware.

## Math

### Sighting

For each sample the tool computes the line-of-sight azimuth with the site's declination and
convergence but **heading offset 0**:

$$ m = \operatorname{atan2}(f_E, f_N), \qquad f = R_{WB}\,\hat x_B $$

The samples are averaged as a circular mean (std \(\sqrt{-2\ln \bar R}\)). The marker's true
bearing from the eye \((E, N)\) is \(b = \operatorname{atan2}(E_m - E,\; N_m - N)\), and the tare
is

$$ \theta = \operatorname{wrap}(b - m). $$

This is `OrientationConverter.tare` from Phase 3 applied to a surveyed bearing.

### Several markers, known position

Offsets are combined as a weighted circular mean, with per-sighting variance
\(\sigma_i^2 = \sigma_{imu}^2 + (\sigma_{marker}/\rho_i)^2\) (pointing 0.5°, marker 5 cm). The
residuals \(r_i = \operatorname{wrap}(m_i + \theta - b_i)\) must agree within 2°. A larger spread
means a wrong station, a mis-surveyed marker, or magnetic disturbance from a vehicle or steel
container, and the tare is refused with that message.

The **station or GNSS position error is common to all sightings**, so it does not average down.
It propagates linearly:

$$
\frac{\partial\theta}{\partial E} = \frac{\sum_i w_i\,(-\Delta N_i/\rho_i^2)}{\sum_i w_i},\quad
\frac{\partial\theta}{\partial N} = \frac{\sum_i w_i\,(\Delta E_i/\rho_i^2)}{\sum_i w_i},\quad
\sigma_\theta^2 = \frac{1}{\sum_i w_i} + \sigma_E^2\Big(\frac{\partial\theta}{\partial E}\Big)^2
                + \sigma_N^2\Big(\frac{\partial\theta}{\partial N}\Big)^2 .
$$

For one marker this reduces to \(\sigma_\theta = \sigma_\perp/\rho\), the across-bearing position
error over range (`test_position_error_propagation_scales_with_inverse_range`).

| position source | σ position | marker at 30 m | 120 m | 250 m |
|---|---|---|---|---|
| taped station | 2 cm | 0.04° | 0.01° | 0.005° |
| consumer GNSS (M8/M10), 30 s average | ~1.5 m | **2.9°** | 0.72° | 0.34° |
| RTK GNSS (F9P with a base on the datum) | 2 cm | 0.04° | 0.01° | 0.005° |

The last two rows add to the 0.5° pointing term in quadrature. **GNSS-positioned tares need
markers at 150 m or more**, or RTK.

### Resection (no station, no GNSS)

With ≥ 3 markers, the tool solves for \((E, N, \theta)\) by Gauss–Newton on
\(r_i = \operatorname{wrap}(m_i + \theta - b_i(E, N))\), with Jacobian rows

$$ \Big[\;\frac{\Delta N_i}{\rho_i^2},\; -\frac{\Delta E_i}{\rho_i^2},\; 1\;\Big] $$

and covariance \(\sigma^2 (J^\top J)^{-1}\), where \(\sigma\) is the larger of the pointing sigma
and the residual RMS. A solution is refused if the position sigma exceeds 2 m, or if \(J^\top J\)
is singular because the operator stands on the circle through the three markers (the classic
danger circle). With 0.5° pointing and markers at about 125 m, expect about 1 m position and 0.3°
heading (`test_resection_recovers_position_and_offset`). Resection works better with markers at
30–60 m spread around the horizon.

### Pitch check (diagnostic)

The expected elevation to the marker top, \(\operatorname{atan2}(u_m - h_{eye}, \rho)\), is
compared with the measured pitch. A residual above 2° is reported as a boresight-trim hint
(`python -m lynx.hw` trim, [imu-calibration.md](../hardware/imu-calibration.md)). It is never
applied automatically.

## GNSS mode

`--gnss /dev/lynx-gnss` (or a profile `gnss:` port) takes the **position** from a u-blox receiver
instead of a station:

* Setup frames at open: UBX-CFG-RATE to 5 Hz (legacy CFG on M8; CFG-VALSET `CFG-RATE-MEAS` on
  M10) and NMEA output trimmed to GGA + RMC + GST. The CFG-RATE frame matches u-blox's reference
  bytes (`b5 62 06 08 06 00 c8 00 01 00 01 00 de 6a`).
* GGA gives position, fix quality, satellites, HDOP and MSL height plus the geoid separation,
  which together give the ellipsoidal height. RMC gives speed and course. GST gives the per-axis
  1σ. Without GST, \(\sigma_E = \sigma_N = \mathrm{HDOP}\cdot\mathrm{UERE}/\sqrt2\) with
  UERE 2.5 m.
* **Averaging is correlation-aware.** GNSS errors wander over tens of seconds, so the standard
  error of a T-second mean uses \(N_{eff} = \max(1, T/60\,s)\), not N samples. A 30 s average is
  honestly about 1.5 m, not 1.5 m / √150. Fixes beyond 3σ of the median, quality 0, or HDOP > 4
  are rejected.
* Conversion: WGS84 geodetic → ECEF → ENU at the datum (Bowring + fixed-point inverse, exact to
  well below 1 mm; the tests round-trip at the equator, mid-latitudes and the poles), then
  optionally rotated to grid North.

The **`gnss:` pose source** ([pose_source.py](../../lynx/field/pose_source.py)) uses the same
receiver at runtime. Attitude comes from the IMU, position from the latest fix in W, and height
is the fixed eye height, because GNSS altitude is 2–3× noisier and the HUD assumes ground z = 0:

```bash
lynx-headset --pose "gnss:/dev/lynx-imu?gnss=/dev/lynx-gnss&site=/etc/lynx/site.json&cal=/var/lib/lynx/imu.json"
```

With no fix for 2 s, the last position is held and the HUD shows `! GNSS NO FIX - POSITION HELD`.
Without GNSS, an orientation-only head tracker keeps the calibrated station position for the
whole game. That is fine for static positions (a defended building, an overwatch) but not for
manoeuvre, which needs GNSS.

## Drift monitoring

Heading drifts after the tare. The Game Rotation Vector (gyro only) drifts by up to about
0.5°/min, and the magnetometer-referenced Rotation Vector does not drift but is pulled by nearby
steel. `DriftMonitor` estimates the current error:

1. **Marker checks** (best). `lynx-field calibrate --check FLAG-N` has the operator tap a marker
   with the *saved* calibration and records the signed error \(e_k\) at \(\tau_k\) minutes after
   the tare. The error at the tare is zero by construction, so the drift rate is the
   through-origin least-squares slope \(a = \sum e_k\tau_k / \sum \tau_k^2\), and the predicted
   error is \(|a|\,\tau\).
2. **Prior** without checks: 0.5°/min for GRV (`imu_report: grv` in the profile), 0 for RV,
   combined in quadrature with the BNO085's own accuracy estimate when available.
3. **Walking heading vs GNSS course over ground** (advisory only): the median of
   wrap(heading − COG) over the last 120 s while walking (≥ 1.2 m/s, head within 20° of level).
   Operators look around while walking, so this shows in the readout (`HDG ... COG +8`) but never
   raises an alert on its own.

The HUD alerts at 3° (`! HDG DRIFT ~4 DEG (25 MIN)`) and 6° (`! RE-TARE ~7 DEG (40 MIN)`).
Re-tare at the nearest marker by running the calibrate flow again, or by restarting the headset
service with `calibrate: always`.

## Files

`/var/lib/lynx/field-cal.json` contains:

```json
{
  "version": 1, "node": 3, "site": "Compound A staging", "method": "station", "station": "S1",
  "position": [0.0, 0.0, 1.7], "position_std_m": [0.02, 0.02],
  "heading_offset_deg": 7.31, "heading_std_deg": 0.29,
  "time_unix": 1790000000.0, "time_utc": "2026-09-21T14:13:20+00:00",
  "imu": {"mount": "left-side", "trim_deg": [0, 0, 0], "declination_deg": 0.9, "convergence_deg": 0.0,
          "heading_offset_deg": 7.31, "tared": true},
  "sightings": [{"marker": "FLAG-N", "measured_deg": 353.6, "true_bearing_deg": 0.0, "residual_deg": 0.02,
                 "range_m": 120.0, "pitch_deg": 0.1, "expected_pitch_deg": 0.14, "pitch_residual_deg": -0.04}],
  "checks": [{"time_unix": 1790001500.0, "marker": "MAST-E", "error_deg": 1.2}]
}
```

The launcher passes `position` to the headset (`--x --y --eye-height`) and `imu.json` to the
pose source, and seeds the drift monitor from `time_unix` and `checks`.
