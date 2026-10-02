# hardware/: Phase 4 helmet hardware (OpenSCAD)

Parametric OpenSCAD sources for the field helmet build. The docs are in
[`docs/field/hardware.md`](../docs/field/hardware.md) and the `docs/field/hw-*.md` files.

```bash
hardware/scripts/render_all.sh          # every STL, DXF and PNG, plus out/mass_report.md
hardware/scripts/render_all.sh --no-png # skip the images (no OpenGL / Xvfb needed)
```

The script needs OpenSCAD ≥ 2021.01, python3 (standard library only), and either a display
or `xvfb-run` for the PNGs. It fails on any OpenSCAD warning or error, and on any STL that is
not a simple solid.

| File | Parts (`-D 'part="..."'`) |
|---|---|
| `openscad/lynx_common.scad` | tolerances, insert/screw holes, NVG dovetail shoe, MIL-STD-1913 clamp and snap clip, helpers |
| `openscad/sensor_pod.scad` | `body` `lid` `bezel` `sled`; DXF: `window_cam` `window_ir` `gasket` `diffuser` `led_plate`; previews: `assembly` `assembly_exploded` `hero` |
| `openscad/esp32_enclosure.scad` | `base` `lid` `mount` `jaw` `grommet`; options `board="esp32s3"\|"esp32dev"`, `mount_style="picatinny"\|"strap"` |
| `openscad/rear_bracket.scad` | `base` `cassette` `rail` `lid` |
| `openscad/cable_mgmt.scad` | `rail_clip` `strap_clip` `helmet_saddle` `plug_boot` |
| `openscad/fit_coupons.scad` | `dovetail_gauge` `insert_coupon` `window_coupon` `window_bezel` `rail_coupon` `rail_jaw` |
| `openscad/helmet_layout.scad` | `layout`: whole-helmet placement preview (phantom helmet, mount and goggle) |
| `scripts/mass_report.py` | mass, CG and moments from the STL volumes |

Frame for all helmet parts: X forward, Y left, Z up (head FLU), in mm. Outputs go to
`hardware/out/` (git-ignored); the PNGs go to `docs/field/hw-renders/`.
