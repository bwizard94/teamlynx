# TeamLynx spatial math

This document defines every coordinate frame and derives every transform used to place pings and
teammates on the HUD. The implementation lives in `lynx/spatial/` and each formula below maps to a
function there. Tests in `tests/test_rotations.py`, `tests/test_projection.py` and
`tests/test_raycast.py` check each result.

Notation: vectors are columns. \(R_{ab}\) maps coordinates expressed in frame *b* to frame *a*
(\(v_a = R_{ab} v_b\)), so \(R_{ba} = R_{ab}^\top\). Its columns are the axes of *b* written in *a*.
Angles are in degrees on the wire and the HUD, and in radians inside formulas.

---

## 1. Frames

### 1.1 World frame W: local ENU from the staging datum

| axis | direction |
|------|-----------|
| \(x_W\) | East |
| \(y_W\) | North (grid North of the local tangent plane) |
| \(z_W\) | Up |

The origin is the **staging datum**: a surveyed or agreed point in the staging area. Every headset
is zeroed there at game start (Phase 4: datum calibration). Units are metres. The ground is modelled
as the plane \(z_W = 0\), which is the datum height. A local tangent plane is accurate to well below
a centimetre over a 1–2 km airsoft field. The Earth-curvature drop is \(d^2 / 2R_\oplus \approx 8\)
cm at 1 km, which is far below IMU/position noise.

### 1.2 Body frame B: FLU, head-fixed

| axis | direction |
|------|-----------|
| \(x_B\) | Forward (line of sight) |
| \(y_B\) | Left |
| \(z_B\) | Up (top of the head) |

The body origin is the operator's eye point, so telemetry \(z\) is eye height. This is the ROS
REP-103 body convention. After boresight calibration the IMU (BNO085) is aligned to this frame.

### 1.3 Camera frame C: OpenCV optical

| axis | direction |
|------|-----------|
| \(x_C\) | Right (image \(+u\)) |
| \(y_C\) | Down (image \(+v\)) |
| \(z_C\) | Forward (optical axis) |

For a boresighted camera the body→camera axis permutation is

$$
x_C = -y_B,\quad y_C = -z_B,\quad z_C = x_B
\qquad\Longrightarrow\qquad
R_{CB} = \begin{bmatrix} 0 & -1 & 0 \\ 0 & 0 & -1 \\ 1 & 0 & 0 \end{bmatrix},\quad \det R_{CB} = +1 .
$$

A physical mount adds a residual rotation \(R_{BM}\) (misalignment, expressed in B) and a lever
arm \(t_B\) (optical centre relative to the eye, in B). `CameraMount` holds both.

---

## 2. Attitude: operator Euler angles

### 2.1 Definitions (wire and HUD)

* **heading** \(h\): compass heading, clockwise from North, \([0, 360)\).
* **pitch** \(p\): positive nose-up, \([-90, 90]\).
* **roll** \(r\): positive right-side-down, \((-180, 180]\).

### 2.2 Mapping to an intrinsic Z-Y′-X″ sequence

Right-handed elementary rotations:

$$
R_x(\phi)=\begin{bmatrix}1&0&0\\0&c_\phi&-s_\phi\\0&s_\phi&c_\phi\end{bmatrix},\;
R_y(\beta)=\begin{bmatrix}c_\beta&0&s_\beta\\0&1&0\\-s_\beta&0&c_\beta\end{bmatrix},\;
R_z(\psi)=\begin{bmatrix}c_\psi&-s_\psi&0\\s_\psi&c_\psi&0\\0&0&1\end{bmatrix}.
$$

The body attitude is yaw about \(z\), then pitch about the new \(y'\), then roll about the newest
\(x''\). In intrinsic form this is

$$
R_{WB} = R_z(\psi)\,R_y(\beta)\,R_x(\phi).
$$

The operator angles map onto \(\psi, \beta, \phi\) as follows:

* **Yaw.** \(R_z(\psi)\) takes \(x_B\) to \((\cos\psi, \sin\psi, 0)\), measured counter-clockwise
  from East. A compass heading \(h\) points along \((\sin h, \cos h, 0)\) in ENU. Therefore
  \(\boxed{\psi = 90^\circ - h}\).
* **Pitch.** \(R_y(\beta)\,\hat x = (\cos\beta, 0, -\sin\beta)\). A positive right-hand rotation
  about the *left* axis drops the nose. For nose-up positive pitch, \(\boxed{\beta = -p}\).
* **Roll.** \(R_x(\phi)\,\hat y = (0, \cos\phi, \sin\phi)\). A positive rotation about Forward
  raises the left ear, which lowers the right side. Therefore \(\boxed{\phi = r}\).

Check: \(h=90, p=r=0\) gives \(R_{WB}\hat x = (1,0,0)\), which is East. `test_heading_is_compass_clockwise_from_north`
covers this.

The line of sight in world coordinates, which is the first column of \(R_{WB}\), is

$$
\hat f_W = (\cos p \sin h,\; \cos p \cos h,\; \sin p).
$$

### 2.3 Inverse (matrix → Euler)

Expanding \(R = R_z(\psi)R_y(\beta)R_x(\phi)\):

$$
R_{20} = -\sin\beta,\quad
R_{10} = \cos\beta\sin\psi,\; R_{00} = \cos\beta\cos\psi,\quad
R_{21} = \cos\beta\sin\phi,\; R_{22} = \cos\beta\cos\phi
$$

$$
\beta = \arcsin(-R_{20}),\quad \psi = \operatorname{atan2}(R_{10}, R_{00}),\quad
\phi = \operatorname{atan2}(R_{21}, R_{22}).
$$

**Gimbal lock** happens at \(\cos\beta = 0\), when the operator looks straight up or down. Only
\(\psi \mp \phi\) is observable there. We set \(\phi = 0\). Column 1 of \(R\) then equals
\((-\sin\psi, \cos\psi, 0)\), so \(\psi = \operatorname{atan2}(-R_{01}, R_{11})\). The
reconstructed matrix is identical to the input (`test_gimbal_lock_reconstructs_same_rotation`).
The quaternion path never needs Euler angles, which is why Phase 3 transports IMU quaternions
internally.

### 2.4 Quaternions

The code uses Hamilton quaternions with the scalar first, \(q = (w, x, y, z)\), normalised to
\(w \ge 0\). \(q_{WB}\) and \(R_{WB}\) represent the same active rotation:

$$
(0, v_W) = q \otimes (0, v_B) \otimes q^{*},
$$

$$
R(q)=\begin{bmatrix}
1-2(y^2+z^2) & 2(xy-wz) & 2(xz+wy)\\
2(xy+wz) & 1-2(x^2+z^2) & 2(yz-wx)\\
2(xz-wy) & 2(yz+wx) & 1-2(x^2+y^2)
\end{bmatrix}.
$$

Euler to quaternion composes the axis-angle quaternions \(q(\hat n,\theta) = (\cos\tfrac\theta2, \hat n\sin\tfrac\theta2)\)
in the same order:

$$
q_{WB} = q_z(\psi)\otimes q_y(\beta)\otimes q_x(\phi).
$$

Matrix to quaternion uses Shepperd's method. It picks the largest of \(\{w, x, y, z\}\) to divide
by, so the result is well conditioned for every rotation.

**BNO085 note (Phase 3).** The BNO085 Rotation Vector report gives a quaternion of the sensor in
its own ENU-like world frame. To use it, apply the fixed sensor→body mounting rotation
\(q_{SB}\) and a heading offset \(q_z(\Delta\psi)\) taken at the staging datum:
\(q_{WB} = q_z(\Delta\psi)\otimes q_{W'S}\otimes q_{SB}\).

---

## 3. World → camera

Given body pose \((t_{WB}, R_{WB})\) and mount \((R_{BM}, t_B)\):

$$
R_{WC} = R_{WB}\,R_{BM}\,R_{BC},\qquad C_W = t_{WB} + R_{WB}\,t_B,
$$

$$
p_C = R_{CW}\,(p_W - C_W),\qquad R_{CW} = R_{WC}^\top .
$$

---

## 4. Pinhole intrinsics from FOV and resolution

The model is ideal (undistorted), with continuous pixel coordinates. Pixel \((i,j)\) covers
\([i, i+1)\times[j, j+1)\), so the image is \([0,W)\times[0,H)\) and the principal point is the
geometric centre \((c_x, c_y) = (W/2, H/2)\).

The image edge \(u = W\) lies at angle \(\mathrm{HFOV}/2\) from the axis:

$$
\tan\frac{\mathrm{HFOV}}{2} = \frac{W/2}{f_x}
\;\Longrightarrow\;
f_x = \frac{W}{2\tan(\mathrm{HFOV}/2)},\qquad
f_y = \frac{H}{2\tan(\mathrm{VFOV}/2)}.
$$

If only HFOV is known, square pixels (\(f_y = f_x\)) imply
\(\mathrm{VFOV} = 2\arctan\!\big(\tfrac HW \tan\tfrac{\mathrm{HFOV}}2\big)\).
A 90° HFOV at 960×600 gives \(f = 480\) px and VFOV ≈ 64.0°.

$$
K = \begin{bmatrix} f_x & 0 & c_x\\ 0 & f_y & c_y\\ 0&0&1\end{bmatrix}.
$$

Real lenses (Phase 3 NoIR camera) need OpenCV calibration. The distortion model then sits between
the normalised coordinates and \(K\). Everything else in this document is unchanged.

---

## 5. Projection with behind-camera and off-screen handling

Let \(p_C = (x, y, z)\) and let \(n\) be the near-plane distance (default 0.05 m).

**In front (\(z > n\)).**

$$
u = f_x \frac{x}{z} + c_x,\qquad v = f_y\frac{y}{z} + c_y .
$$

The point is **ON_SCREEN** if \(m \le u < W-m\) and \(m \le v < H-m\), where \(m\) is the
indicator margin. Otherwise it is **OFF_SCREEN**.

**Indicator direction.** Off-screen targets get an indicator on the border, inset by \(m\). It
sits where the ray from the image centre in screen direction \(d\) exits the border.

* In front: \(d = (f_x x,\; f_y y)\). For \(z>0\) this is parallel to \((u-c_x, v-c_y)\), because
  it equals that vector times \(z\). It is deliberately **not** divided by \(z\). Dividing by
  \(z\) flips the sign when the target crosses the camera plane, and the arrow would point the
  wrong way.
* **BEHIND** (\(z \le n\)): \(d = (\operatorname{sgn}(x)\, f_x\sqrt{x^2+z^2},\; f_y y)\).
  This folds the target into the image-plane-parallel half-space. It keeps the target's vertical
  offset \(y\) and replaces the horizontal offset with the full horizontal distance
  \(\sqrt{x^2+z^2}\) on the side of \(x\). The resulting direction is that of a point at the same
  height placed 90° to that side. Three properties follow:
  1. It is continuous with the in-front case at \(z = 0\), since \(\sqrt{x^2+0} = |x|\)
     (`test_indicator_continuous_across_image_plane`).
  2. A ground target behind the operator lands on the **left or right edge, whichever is the
     shorter turn**, rather than the bottom edge. Plain \((f_x x, f_y y)\) would put a ground
     target directly behind at the bottom, because \(y>0\) and \(x\approx 0\). "Look down"
     is the wrong cue for something behind you.
  3. A target dead astern (\(x = 0\)) uses the sign of the horizontal bearing.

**Edge clamp.** Normalise \(d\). For the inset rectangle \([m, W-m]\times[m, H-m]\) and centre
\(c\), compute

$$
t^* = \min_{i:\,d_i\neq 0}\; \frac{(\text{edge}_i - c_i)}{d_i},\qquad
\text{indicator} = c + t^* d,
$$

where \(\text{edge}_i\) is \(W-m\) or \(m\) (respectively \(H-m\) or \(m\)) depending on the sign of
\(d_i\). This is `clamp_direction_to_rect`.

The **screen angle** of the arrow, clockwise from screen-up with \(v\) pointing down, is
\(\operatorname{atan2}(d_u, -d_v)\).

**Compass bearing.** The compass tape needs a bearing that does not depend on pitch or roll:

$$
b = \operatorname{wrap}_{(-180,180]}\big(\operatorname{atan2}(\Delta E, \Delta N) - h\big).
$$

**Level-camera closed form (test oracle).** For \(p = r = 0\), take a target at relative bearing
\(b\), horizontal range \(\rho\), and height difference \(\Delta z\). Then
\(x = \rho\sin b,\; y = -\Delta z,\; z = \rho\cos b\), which gives

$$
u = c_x + f_x\tan b,\qquad v = c_y - f_y \frac{\Delta z}{\rho\cos b}.
$$

The self-check uses an even more independent oracle with explicit basis vectors
\(\hat f = (\cos p\sin h, \cos p\cos h, \sin p)\), \(\hat r = (\cos h, -\sin h, 0)\) and
\(\hat u = \hat r\times\hat f\). It computes \(x_C = d\cdot\hat r,\; y_C = -d\cdot\hat u,\;
z_C = d\cdot\hat f\) and shares no rotation-matrix code with the library.

### 5.1 Line segments (ground grid, operator posts)

Endpoints behind the camera cannot be projected and then joined. Perspective division mirrors
them through the principal point. Segments are therefore clipped against the near plane **in
camera space** before division:

$$
t = \frac{n - z_a}{z_b - z_a},\qquad p = a + t(b-a).
$$

2D raster clipping is done afterwards (`pygame.Rect.clipline`).

### 5.2 Ground region and horizon

For pixel \((u,v)\) the viewing ray is \(d_C = \big(\tfrac{u-c_x}{f_x}, \tfrac{v-c_y}{f_y}, 1\big)\).
Its world vertical component is

$$
d_z(u,v) = r_3\cdot d_C,
$$

where \(r_3\) is the third row of \(R_{WC}\). This is **affine** in \((u,v)\). For a camera above
the plane, the pixels that see the ground are exactly those with \(d_z < 0\). The ground region is
therefore the image rectangle clipped by a half-plane, which is one Sutherland–Hodgman pass. Its
boundary \(d_z = 0\) is the horizon (vanishing line). With zero pitch the horizon passes through
the image centre at any roll, tilted by the roll angle (`test_ground_polygon_rolled_horizon_passes_through_centre`).

---

## 6. Ping raycast

When the rail switch fires, a ray is cast from the camera centre along the optical axis:

$$
o = C_W,\qquad \hat d = R_{WC}\,(0,0,1)^\top = \text{third column of } R_{WC}.
$$

With a boresighted mount, \(\hat d = \hat f_W\) and \(o\) is the eye.

**Ground plane \(z = g\).** Solve \(o_z + t\,d_z = g\), which gives \(t = (g - o_z)/d_z\). The ray
hits the ground if \(|d_z| > \varepsilon\) and \(0 \le t \le t_{\max}\). Then
\(p = o + t\hat d\), and \(p_z\) is snapped to exactly \(g\).

For an operator at eye height \(h_e\) on flat ground pitching down by \(\alpha\), this reduces to
\(t = h_e/\sin\alpha\) at horizontal distance \(h_e/\tan\alpha\). At \(h_e = 1.7\) m and
\(\alpha = 10^\circ\) that is 9.64 m.

**Fallback.** If the ray is parallel to the ground, points upward, or hits beyond `max_range`
(150 m), the ping is placed at `min(fallback_range, max_range)` (default 50 m) along \(\hat d\).
This handles a ping aimed at a treeline or building, which the flat-ground model cannot see.

**Reticle invariant.** The ray *is* the optical axis, so for the pinging operator
\(p_C = (0, 0, t)\) and the ping projects to exactly \((c_x, c_y)\). The ping always appears under
the reticle that aimed it. `test_ping_reprojects_to_reticle_for_random_poses_and_mounts` checks
this for 300 random poses and mount misalignments.

---

## 7. Precision budget

* Positions travel as float32. The 24-bit mantissa gives about 6×10⁻⁵ m resolution at 1 km.
* Angles travel as float32 degrees, about 3×10⁻⁵° resolution at 360°.
* At \(f = 480\) px, 1 mrad of angle error moves a point by about 0.5 px. Sensor error dominates:
  the BNO085 heading accuracy is about 1–2°, which is roughly 8–17 px at this focal length.
  Phase 3 work should go into sensor fusion and calibration, not wire precision.
