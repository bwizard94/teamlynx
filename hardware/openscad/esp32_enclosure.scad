// TeamLynx Phase 4: helmet ESP32 head-tracker enclosure (left side rail).
// Holds the ESP32-DevKitC (env esp32dev) or ESP32-S3-DevKitC-1 (env esp32s3) plugged into
// a 70 x 30 mm perfboard carrier that also carries the Phase 3 passives (R1-R4, C1-C3,
// D1; docs/hardware/wiring.md). Panel parts:
//   +X end (toward the pod):     GX12-6 socket J2  <- pod IMU pigtail
//   +Y wall, rear bay (faces down when fitted): 3.5 mm TRS jack J1 <- rail switch (the breakaway)
//   -X end, lid seam:            USB cable to the Jetson, clamped in a TPU grommet
// Local frame: base on XY, lid toward +Z (away from the helmet), long axis X = fore-aft.
// Fitted on the LEFT side rail: local X = head X, local Z = head Y (out), local Y = head -Z
// (down). Plug the DevKit in with its USB end toward -X.
// Parts: base lid mount jaw grommet | assembly
//   base  : the box, printed floor-down without supports
//   mount : 4 mm plate bolted under the box (4x M3 x 8 from inside the end bays, M3 nuts in
//           traps): Picatinny clamp body or strap wings, per mount_style
include <lynx_common.scad>

part = "assembly";
board = "esp32s3";          // "esp32dev" | "esp32s3"
mount_style = "picatinny";  // "picatinny" (ARC-to-1913 adapter) | "strap" (hook-and-loop + 25 mm webbing)

// ---------------------------------------------------------------- parameters
wall = 2.4;
floor_t = 2.4;
lid_t = 2.4;
carrier = [70, 30, 1.6];          // 7 x 3 cm perfboard
carrier_holes = [66, 26];         // M2 hole pattern
carrier_standoff = 4;
header_h = 8.5;                   // 2.54 mm female header body
devkit = board == "esp32dev" ? [54.4, 27.9, 1.6] : [62.7, 25.5, 1.6];
devkit_top = 3.6;                 // module shield / connectors above the devkit PCB
bay = 13;                         // connector bays at both ends (GX12 / USB plug + J1 bodies)
inner = [carrier[0] + 6 + 2 * bay, carrier[1] + 6,
         carrier_standoff + carrier[2] + header_h + devkit[2] + devkit_top + 2.5];
outer = [inner[0] + 2 * wall, inner[1] + 2 * wall, floor_t + inner[2]];
gx12_hole = 12.2;                 // GX12 panel socket, M12 x 1 thread
trs_hole = 6.2;                   // panel 3.5 mm jack, M6 x 0.5 thread
usb_cable_d = 4.5;                // USB 2.0 cable OD; grommet bore
corner_inset = wall + 2.2;
mount_t = 4;
mount_screws = [for (x = [wall + bay / 2, outer[0] - wall - bay / 2], y = [outer[1] / 2 - 9, outer[1] / 2 + 9]) [x, y]];
lid_screws = [for (x = [corner_inset, outer[0] - corner_inset], y = [corner_inset, outer[1] - corner_inset]) [x, y]];

echo(str("ESP32 box outer ", outer[0], " x ", outer[1], " x ", outer[2] + lid_t, " mm (", board, ", ", mount_style, ")"));

module base_part() {
    difference() {
        rbox(outer, 3);
        translate([wall, wall, floor_t]) rbox([inner[0], inner[1], inner[2] + 1], 1.5);
        for (p = mount_screws) translate([p[0], p[1], floor_t + 0.01]) clear_hole(CLEAR_M3, floor_t + 1);
        // lid inserts
        for (p = lid_screws) translate([p[0], p[1], outer[2]]) insert_hole(INSERT_M3);
        // J2 GX12 on the +X end, centred
        translate([outer[0] - wall - 1, outer[1] / 2, floor_t + inner[2] / 2]) rotate([0, 90, 0]) cylinder(d = gx12_hole + HOLE_COMP, h = wall + 2);
        // J1 TRS jack in the +Y wall of the rear bay
        translate([wall + bay - 4, outer[1] - wall - 1, floor_t + 8]) rotate([-90, 0, 0]) cylinder(d = trs_hole + HOLE_COMP, h = wall + 2);
        // USB exit notch at the top edge of the -X end (lid tongue closes it)
        translate([-1, outer[1] / 2 + 7, outer[2] - (usb_cable_d + 3) / 2]) rotate([0, 90, 0])
            cylinder(d = usb_cable_d + 3.0, h = wall + 2);
        translate([-1, outer[1] / 2 + 7 - (usb_cable_d + 3) / 2, outer[2] - (usb_cable_d + 3) / 2]) cube([wall + 2, usb_cable_d + 3, 5]);
    }
    // carrier standoffs with M2 inserts
    for (dx = [-1, 1], dy = [-1, 1]) translate([outer[0] / 2 + dx * carrier_holes[0] / 2, outer[1] / 2 + dy * carrier_holes[1] / 2, floor_t - 0.01])
        difference() {
            cylinder(d = 5.5, h = carrier_standoff + 0.01);
            translate([0, 0, carrier_standoff + 0.01]) insert_hole(INSERT_M2);
        }
    // corner columns for the lid inserts
    for (p = lid_screws) translate([p[0], p[1], 0]) difference() {
        cylinder(d = insert_boss_d(INSERT_M3), h = outer[2]);
        translate([0, 0, outer[2] + 0.01]) insert_hole(INSERT_M3);
    }
    // cable-tie anchor bar at the inner edge of the rear bay, for the USB and J1 leads
    translate([wall + bay - 1, outer[1] / 2 - 7, floor_t - 0.01]) difference() {
        cube([3, 14, 6]);
        translate([-1, 2.5, 1.5]) cube([5, 9, 3]);
    }
}

module lid_part() {
    difference() {
        union() {
            rbox([outer[0], outer[1], lid_t], 3);
            // tongue that closes the USB notch and presses the grommet
            translate([wall + 0.3, outer[1] / 2 + 7 - (usb_cable_d + 3) / 2 + CLR_SLIDE, -((usb_cable_d + 3) / 2) + 0.01])
                cube([wall - 0.3 + 4, usb_cable_d + 3 - 2 * CLR_SLIDE, (usb_cable_d + 3) / 2]);
            // locating lip inside the walls
            translate([wall + CLR_SLIDE, wall + CLR_SLIDE, -1.5]) difference() {
                rbox([inner[0] - 2 * CLR_SLIDE, inner[1] - 2 * CLR_SLIDE, 1.51], 1.5);
                translate([1.2, 1.2, -1]) rbox([inner[0] - 2 * CLR_SLIDE - 2.4, inner[1] - 2 * CLR_SLIDE - 2.4, 4], 1);
                for (p = lid_screws) translate([p[0] - wall - CLR_SLIDE, p[1] - wall - CLR_SLIDE, -1]) cylinder(d = insert_boss_d(INSERT_M3) + 1, h = 4);
            }
        }
        for (p = lid_screws) translate([p[0], p[1], lid_t + 0.01]) clear_hole(CLEAR_M3, lid_t + 3, [HEAD_M3_BUTTON[0], 0.8]);
        // grommet seat in the tongue
        translate([-1, outer[1] / 2 + 7, 0]) rotate([0, 90, 0]) cylinder(d = usb_cable_d + 3.0, h = wall + 6);
        // label recess: "LYNX HT" + firmware env
        translate([outer[0] / 2, outer[1] / 2, lid_t - 0.6]) linear_extrude(1)
            text(board == "esp32dev" ? "LYNX HT esp32dev" : "LYNX HT esp32s3", size = 4.2, halign = "center", valign = "center");
    }
}

// TPU 95A split grommet: cable bore usb_cable_d, OD usb_cable_d + 3, flanged both sides of
// the wall. Slit it along one side with a knife, wrap it on the cable, then seat it.
module grommet_part() {
    difference() {
        union() {
            cylinder(d = usb_cable_d + 3.0 - 0.1, h = wall);
            translate([0, 0, -1.5]) cylinder(d = usb_cable_d + 6, h = 1.5);
            translate([0, 0, wall]) cylinder(d = usb_cable_d + 6, h = 1.5);
        }
        translate([0, 0, -2]) cylinder(d = usb_cable_d - 0.3, h = wall + 4);
    }
}

// Origin as the box; occupies z in [-mount_t, 0] plus the clamp below it.
module mount_part() {
    difference() {
        union() {
            translate([0, 0, -mount_t]) rbox([outer[0], outer[1], mount_t], 3);
            if (mount_style == "picatinny")
                translate([outer[0] / 2, outer[1] / 2, -mount_t + 0.01]) picatinny_clamp_body(PJ_LEN + 8);
            else
                for (x = [-16, outer[0] - 4]) translate([x, 0, -mount_t]) rbox([20, outer[1], mount_t], 3);
        }
        if (mount_style == "picatinny") translate([outer[0] / 2, outer[1] / 2, -mount_t + 0.01]) picatinny_clamp_cut(PJ_LEN + 8);
        else for (x = [-9, outer[0] + 9]) translate([x, outer[1] / 2, -mount_t / 2]) web_slot(WEB_25, mount_t + 2);
        for (p = mount_screws) translate([p[0], p[1], 0.01]) {
            clear_hole(CLEAR_M3, mount_t + 1);
            translate([0, 0, -mount_t + 2.6]) nut_trap(NUT_M3, 2.61);
        }
    }
}

module jaw_part() translate([0, 0, -mount_t]) picatinny_jaw(PJ_LEN + 8);

module ref_contents() {
    color([0.1, 0.4, 0.15]) translate([(outer[0] - carrier[0]) / 2, (outer[1] - carrier[1]) / 2, floor_t + carrier_standoff]) cube(carrier);
    color([0.1, 0.1, 0.1]) for (dy = [-1, 1]) translate([outer[0] / 2 - devkit[0] / 2 + 2, outer[1] / 2 + dy * 12.7 - 1.27, floor_t + carrier_standoff + carrier[2]])
        cube([devkit[0] - 4, 2.54, header_h]);
    color([0.15, 0.15, 0.2]) translate([outer[0] / 2 - devkit[0] / 2, outer[1] / 2 - devkit[1] / 2, floor_t + carrier_standoff + carrier[2] + header_h]) cube(devkit);
    color([0.75, 0.75, 0.78]) translate([outer[0] / 2 - devkit[0] / 2 + devkit[0] - 20, outer[1] / 2 - 9, floor_t + carrier_standoff + carrier[2] + header_h + devkit[2]]) cube([18, 18, 3.2]);
    color([0.7, 0.7, 0.72]) translate([outer[0], outer[1] / 2, floor_t + inner[2] / 2]) rotate([0, 90, 0]) cylinder(d = 15, h = 3);
    color([0.2, 0.2, 0.2]) translate([wall + bay - 4, outer[1], floor_t + 8]) rotate([-90, 0, 0]) cylinder(d = 8, h = 2);
}

module esp32_layout_proxy() assembly();   // used by helmet_layout.scad

module assembly() {
    color([0.35, 0.38, 0.3]) base_part();
    ref_contents();
    color([0.3, 0.32, 0.26]) mount_part();
    if (mount_style == "picatinny") color([0.25, 0.27, 0.22]) translate([outer[0] / 2, outer[1] / 2, 0.01]) jaw_part();
    color([0.35, 0.38, 0.3, 0.35]) translate([0, 0, outer[2] + 0.2]) lid_part();
}

if (part == "base") base_part();
else if (part == "lid") rotate([180, 0, 0]) lid_part();
else if (part == "mount") rotate([180, 0, 0]) mount_part();   // box face down, clamp up
else if (part == "jaw") rotate([-90, 0, 0]) jaw_part();
else if (part == "grommet") grommet_part();
else if (part == "assembly") assembly();
