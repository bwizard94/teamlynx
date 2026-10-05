# IMU orientation: frames, mounting, declination, tare, sensor calibration

The implementation is in `lynx/hw/orientation.py` and the tests are in
`tests/hw/test_hw_orientation.py`. Everything here follows [../spatial-math.md](../spatial-math.md).
**heading** is the compass heading, clockwise from North. **pitch** is positive nose-up. **roll**
is positive right-side-down. The world frame is ENU from the staging datum and the head frame is
FLU. \(R_{ab}\) maps frame-*b* coordinates to frame *a*.

## 1. Frames

| Frame | Definition |
|---|---|
| S, sensor | BNO085 x/y/z as printed on the breakout |
| M, sensor world | ENU in which the BNO085 reports. **Rotation Vector**: y = *magnetic* North, z = up (gravity). **Game Rotation Vector**: z = up, heading arbitrary (set at power-up) |
| W, TeamLynx world | ENU from the staging datum, y = grid North |
| B, head | FLU: x Forward (line of sight), y Left, z Up |

The BNO085 quaternion \(q_{MS}\) rotates sensor vectors into M: \(v_M = q_{MS}\,v_S\,q_{MS}^*\).
The SH-2 world is ENU, so the **identity quaternion means sensor x points East**. With the x arrow
forward, that is compass heading 090. `test_identity_quaternion_faces_east` pins this fact.

## 2. The conversion

$$
q_{WB} \;=\; q_z(-c)\;\otimes\; q_{MS}\;\otimes\; q_{SB}\;\otimes\; q_T,
\qquad c = \delta - \gamma + \Delta
$$

* \(q_{SB}\) is the **mounting rotation**, with \(R_{SB} = R_{BS}^\top\). The columns of
  \(R_{BS}\) are the sensor axes expressed in the head frame.
* \(q_T\) is a small **boresight trim** applied in the head frame (§4).
* \(c\) is the total heading correction:
  * \(\delta\) is the magnetic declination, east positive (magnetic → true). It applies to the
    Rotation Vector only.
  * \(\gamma\) is the grid convergence, east positive (true → grid North of the datum frame).
    Use it when the field map is UTM or a national grid. Otherwise set it to 0.
  * \(\Delta\) is the **tare** offset from the staging datum (§5).

**Why a world-z rotation is a pure heading offset.** From spatial-math §2.2,
\(R_{WB} = R_z(90^\circ - h)\,R_y(-p)\,R_x(r)\). Premultiplying by \(R_z(-c)\) gives
\(R_z(90^\circ - (h + c))\,R_y(-p)\,R_x(r)\). The heading becomes \(h + c\) and pitch and roll are
unchanged (`test_heading_correction_leaves_pitch_roll_untouched`). This is the
\(q_z(\Delta\psi)\) term of spatial-math §2.4, with \(\Delta\psi = -c\).

The headset/sim gets heading/pitch/roll from \(q_{WB}\) via `matrix_to_euler`, the same Phase 1
code path. The quaternion itself is also kept (`HeadPose.q_wb`) so raycasts never pass through
Euler angles.

## 3. Mounting spec

`mount` is three letters. Letter *i* says where sensor axis *i* (x, y, z) points on the head:
F/B forward/back, L/R left/right, U/D up/down. The result must be right-handed, i.e.
z = x × y. Left-handed specs are rejected with a hint.

| Preset | Spec | Physical mount |
|---|---|---|
| `top-flat` (default) | `FLU` | flat on top of the helmet, components up, x arrow forward |
| `top-flat-x-right` | `RFU` | flat on top, x arrow toward the right ear |
| `left-side` | `FDL` | vertical on the left side / NVG shroud, components facing out, x forward |
| `right-side` | `FUR` | vertical on the right side, components facing out, x forward |
| `rear` | `RUB` | vertical on the back of the helmet, components facing aft, x toward the right ear |

Worked example for `left-side`. x → F. z points out of the left side, so z → L. Then
y = z × x = L × F = D:

$$
R_{BS} = \begin{bmatrix} \hat F & \hat D & \hat L \end{bmatrix}
= \begin{bmatrix} 1&0&0\\0&0&1\\0&-1&0 \end{bmatrix},\quad \det = +1 .
$$

`test_mount_presets_against_physical_axes` constructs the sensor quaternion independently, from
where each sensor axis physically points for a level head facing North, and checks that every
preset reads (0, 0, 0).

## 4. Boresight trim

Boresight trim corrects the residual misalignment between the nominal mount and the true line of
sight. Aim the reticle at a far landmark of known bearing and elevation and read the error. Then

$$
R_{WB} = R_{WB}^{\text{meas}}\,R_T,\qquad
R_T = R_z(-\Delta h)\,R_y(-\Delta p)\,R_x(\Delta r).
$$

For a level head, \(\text{trim\_deg} = (\Delta h, \Delta p, \Delta r)\) adds exactly
\(\Delta h, \Delta p, \Delta r\) to heading, pitch and roll, with the same signs as the Phase 1
convention. Typical values are below 2°. Above about 5°, fix the bracket instead.

## 5. Tare at the staging datum

1. Stand at the datum and face a reference with a known true/grid bearing \(B\): a surveyed
   stake, a distant landmark taken off the map, or a good compass bearing corrected for
   declination.
2. Hold still. Run `python -m lynx.hw tare --port P --bearing B --cal imu.json`, or press `T` in
   `lynx-sim --imu-port P --datum-bearing B`.
3. The host collects the raw quaternions from the last 0.5 s and computes the **circular mean**
   \(\bar a\) of the line-of-sight azimuth \(a = \operatorname{atan2}(f_E, f_N)\) of the untared
   attitude, where \(f\) is column 0 of \(R_{WB}\). It then sets
   \(\Delta = \operatorname{wrap}_{(-180,180]}(B - \bar a)\).
4. The tare is rejected if \(|p| > 45^\circ\), where heading is ill-conditioned (it is undefined
   at ±90°), or if the circular standard deviation \(\sqrt{-2\ln \bar R}\) exceeds 2°, meaning the
   head moved. The mean is wrap-safe across North (`test_tare_is_wrap_safe_near_north`).

The result is saved in the calibration JSON (`heading_offset_deg`, `tared: true`). Pass that file
to the headset with `--pose serial:PORT?cal=imu.json` or to the sim with `--imu-cal imu.json`.

**Host tare is the primary method.** It works identically for the Rotation Vector and the Game
Rotation Vector, and it is mount-aware. The BNO085's own tare (`tare --device [--persist]`, SH-2
`setTareNow`) is also available. It runs first and is followed by a host tare, so the two cannot
disagree.

## 6. Rotation Vector vs Game Rotation Vector

| | Rotation Vector (RV, default) | Game Rotation Vector (GRV) |
|---|---|---|
| Sensors | gyro + accel + **mag** | gyro + accel |
| Heading reference | magnetic North (absolute) | arbitrary at power-up |
| Heading drift | none long-term | about 0.5°/min typical; re-tare per game |
| Magnetic disturbance (steel, vehicles, rebar, battery) | heading pulls toward the disturbance | immune |
| Health | `accuracy_rad` estimate; `cal_status` | `tared` required, otherwise flagged DEGRADED |

Switch with `python -m lynx.hw report --port P --type grv`. Use GRV inside steel structures or
near vehicles, and re-tare at the datum before each game.

## 7. BNO085 dynamic calibration (DCD)

The BNO085 calibrates its accel, gyro and mag continuously. The result, the dynamic calibration
data (DCD), lives in RAM and is saved to its flash.

1. `python -m lynx.hw cal --port P --sensors accel,gyro,mag --autosave on`. These are the defaults
   after reset, but setting them explicitly is cheap.
2. Calibrate the mag: slowly rotate the assembled helmet through every orientation (figure-8s
   and rolls) for about 30 s, away from metal, until `monitor` shows `cal 3` and `acc` below
   about 5°.
3. `python -m lynx.hw save-dcd --port P` writes the calibration to flash, so the next power-up
   starts calibrated.
4. Re-do this whenever the mount changes. Hard-iron offsets come from the helmet and its
   hardware, not from the chip.

## 8. Health → telemetry

`HealthMonitor` (`lynx/hw/health.py`) classifies the link:

| State | Condition |
|---|---|
| NO_DATA | no sample yet. The reason says whether the ESP32 reported "BNO085 not detected" |
| STALE | no sample for 0.25 s. The pose is not used and the sim/headset keeps the last pose |
| DEGRADED | rate below 80 % of nominal, frame loss above 5 %, RV accuracy above 10°, mag cal below 2, or GRV not tared |
| OK | none of the above |

`DEGRADED` and `STALE` set `TelemetryFlags.IMU_DEGRADED` on the wire. A held rail switch sets
`TelemetryFlags.PING_SWITCH`. A 1–2° heading error is about 8–17 px at f = 480 px
(spatial-math §7), so the accuracy threshold is about heading trust rather than wire precision.
