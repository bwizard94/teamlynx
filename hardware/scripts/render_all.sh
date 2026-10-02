#!/usr/bin/env bash
# Render every TeamLynx Phase 4 printable part to STL, every cut part to DXF, the preview
# PNGs, and the mass/balance report. No CI needed: run it locally.
#
#   hardware/scripts/render_all.sh            # everything
#   hardware/scripts/render_all.sh --no-png   # skip images (no OpenGL / Xvfb needed)
#
# Needs OpenSCAD >= 2021.01 on PATH (or OPENSCAD=/path/to/openscad), python3, and for PNGs
# either a display or xvfb-run. Outputs go to hardware/out/ (git-ignored) and the PNGs to
# docs/field/hw-renders/. Fails on any OpenSCAD WARNING or ERROR, and on any non-manifold STL.
set -euo pipefail

HW="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAD="$HW/openscad"
OUT="$HW/out"
PNG_DIR="$HW/../docs/field/hw-renders"
OPENSCAD="${OPENSCAD:-openscad}"
DO_PNG=1
[[ "${1:-}" == "--no-png" ]] && DO_PNG=0

mkdir -p "$OUT/stl" "$OUT/dxf" "$PNG_DIR"
LOG="$OUT/render.log"
: > "$LOG"
fail=0

run() {  # run <scad> <out> [-D defs...]
    local scad="$1" out="$2"; shift 2
    local msg
    if ! msg="$("$OPENSCAD" -o "$out" "$@" "$SCAD/$scad" 2>&1)"; then
        echo "FAIL  $(basename "$out")"; echo "$msg" | tail -5; fail=1; return
    fi
    echo "== $(basename "$out")" >> "$LOG"; echo "$msg" >> "$LOG"
    if grep -qE "WARNING|ERROR" <<< "$msg"; then
        echo "WARN  $(basename "$out")"; grep -E "WARNING|ERROR" <<< "$msg" | head -5; fail=1; return
    fi
    if [[ "$out" == *.stl ]] && grep -q "Simple:" <<< "$msg" && ! grep -q "Simple: *yes" <<< "$msg"; then
        echo "NONMANIFOLD  $(basename "$out")"; fail=1; return
    fi
    echo "ok    $(basename "$out")"
}

stl() {  # stl <scad> <part> <name> [extra -D]
    local scad="$1" part="$2" name="$3"; shift 3
    run "$scad" "$OUT/stl/$name.stl" -D "part=\"$part\"" "$@"
}
dxf() {
    local scad="$1" part="$2" name="$3"; shift 3
    run "$scad" "$OUT/dxf/$name.dxf" -D "part=\"$part\"" "$@"
}

echo "OpenSCAD: $("$OPENSCAD" --version 2>&1)"

# sensor pod
for p in body lid bezel sled; do stl sensor_pod.scad "$p" "pod_$p"; done
stl sensor_pod.scad lid pod_lid_shoe_lateral -D 'shoe_axis="y"'
dxf sensor_pod.scad window_cam pod_window_cam_3mm_pc
dxf sensor_pod.scad window_ir pod_window_ir_3mm_pc
dxf sensor_pod.scad gasket pod_gasket_1mm_neoprene_x2
dxf sensor_pod.scad diffuser pod_ir_diffuser_film
dxf sensor_pod.scad led_plate pod_led_plate_2mm_al

# ESP32 enclosure (default: S3 on a Picatinny clamp) + variants
for p in base lid mount jaw grommet; do stl esp32_enclosure.scad "$p" "esp32_$p"; done
stl esp32_enclosure.scad mount esp32_mount_strap -D 'mount_style="strap"'
stl esp32_enclosure.scad base esp32_base_devkitc -D 'board="esp32dev"'
stl esp32_enclosure.scad lid esp32_lid_devkitc -D 'board="esp32dev"'

# rear bracket
for p in base cassette rail lid; do stl rear_bracket.scad "$p" "rear_$p"; done

# cable management
for p in rail_clip strap_clip helmet_saddle plug_boot; do stl cable_mgmt.scad "$p" "cable_$p"; done

# fit coupons
for p in dovetail_gauge insert_coupon window_coupon window_bezel rail_coupon rail_jaw; do
    stl fit_coupons.scad "$p" "coupon_$p"
done

if [[ $DO_PNG -eq 1 ]]; then
    XV=()
    if [[ -z "${DISPLAY:-}" ]] && command -v xvfb-run > /dev/null; then XV=(xvfb-run -a); fi
    png() {  # png <scad> <part> <name> <camera>
        local msg
        if msg="$("${XV[@]}" "$OPENSCAD" -o "$PNG_DIR/$3.png" --imgsize=1800,1100 --colorscheme=Tomorrow \
                 --camera="$4" -D "part=\"$2\"" "$SCAD/$1" 2>&1)"; then
            if grep -qE "WARNING|ERROR" <<< "$msg"; then echo "WARN  $3.png"; fail=1; else echo "ok    $3.png"; fi
        else echo "FAIL  $3.png"; fail=1; fi
    }
    png sensor_pod.scad hero phase4-helmet-pod 330,170,260,40,-62,15
    png sensor_pod.scad assembly_exploded pod-exploded-top -60,60,260,34,0,20
    png esp32_enclosure.scad assembly esp32-enclosure -90,-120,120,54,20,10
    png rear_bracket.scad assembly rear-cassette -150,-230,190,0,0,10
    png helmet_layout.scad layout helmet-layout 560,470,380,10,0,10
    png cable_mgmt.scad assembly cable-management -80,-60,90,15,50,0
fi

if command -v python3 > /dev/null; then
    python3 "$HW/scripts/mass_report.py" "$OUT/stl" > "$OUT/mass_report.md" && echo "ok    mass_report.md" || fail=1
fi

echo
if [[ $fail -ne 0 ]]; then echo "render_all: FAILED (see $LOG)"; exit 1; fi
echo "render_all: all parts rendered -> $OUT"
