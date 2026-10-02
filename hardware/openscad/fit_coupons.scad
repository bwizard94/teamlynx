// TeamLynx Phase 4: print these BEFORE the real parts, in the same material and settings.
//   dovetail_gauge  three shoes at DT_FIT -0.2 / 0 / +0.2 mm (1, 2, 3 notches). Try each in
//                   your mount; set DT_FIT in lynx_common.scad to the one that clicks in with
//                   light hand force and does not rock. The gib screw then takes up the rest.
//   insert_coupon   M2 / M2.5 / M3 insert holes at -0.1 / 0 / +0.1 mm. Install inserts and
//                   pull-test; adjust INSERT_* hole_d if they spin (too big) or bulge (too small).
//   window_coupon   one camera-window pocket with its four screw inserts at 1:1, to test the
//                   Lexan cut, the gasket compression and the bezel lip.
//   rail_coupon     20 mm Picatinny clamp body + jaw: check the fit on your rail adapter.
// Parts: dovetail_gauge insert_coupon window_coupon window_bezel rail_coupon rail_jaw
include <lynx_common.scad>

part = "dovetail_gauge";

module dovetail_gauge() {
    for (i = [0 : 2]) {
        trim = (i - 1) * 0.2;
        translate([i * (DT_W_TOP + 8), 0, 0]) difference() {
            union() {
                translate([-(DT_W_TOP + 6) / 2, -DT_LEN / 2 - 4, 0]) cube([DT_W_TOP + 6, DT_LEN + 8, 3]);
                translate([0, 0, 2.99]) rotate([0, 0, 90]) {
                    wn = dt_w_neck(DT_W_TOP + trim, DT_H, DT_ANGLE);
                    translate([-DT_LEN / 2, -(wn + 2) / 2, 0]) cube([DT_LEN, wn + 2, DT_RISER + 0.01]);
                    translate([0, 0, DT_RISER]) rotate([90, 0, 90]) translate([0, 0, -DT_LEN / 2])
                        linear_extrude(DT_LEN) dovetail_profile(DT_W_TOP, DT_H, DT_ANGLE, trim);
                }
            }
            for (n = [0 : i]) translate([-(DT_W_TOP + 6) / 2 + 3 + n * 3, -DT_LEN / 2 - 4 - 0.01, -0.01]) cube([1.2, 2, 3.02]);
        }
    }
}

module insert_coupon() {
    sets = [[INSERT_M2, "M2"], [INSERT_M25, "M2.5"], [INSERT_M3, "M3"]];
    difference() {
        cube([3 * 14, 3 * 12 + 6, 8]);
        for (r = [0 : 2], c = [0 : 2]) {
            ins = sets[r][0];
            translate([7 + c * 14, 8 + r * 12, 8.01]) insert_hole([ins[0] + (c - 1) * 0.1, ins[1], ins[2]]);
        }
        for (r = [0 : 2]) translate([1, 6 + r * 12, 8 - 0.5]) cube([0.8, 4, 1]);
    }
}

// one camera-window pocket and four inserts, same numbers as sensor_pod.scad
win = [42, 38];
pocket = 3.7;
module window_coupon() {
    size = win + [14, 14];
    difference() {
        translate([-size[0] / 2, -size[1] / 2, 0]) rbox([size[0], size[1], 8.5], 3);
        translate([0, 0, 8.5 - pocket]) linear_extrude(pocket + 1) rrect(win + [2 * CLR_LOOSE, 2 * CLR_LOOSE], 2.3);
        translate([0, 0, -1]) linear_extrude(12) rrect([34, 30], 1.5);
        for (sx = [-1, 1], sy = [-1, 1]) translate([sx * (size[0] / 2 - 3.6), sy * (size[1] / 2 - 3.6), 8.51]) insert_hole(INSERT_M3);
    }
}
module window_bezel() {
    size = win + [14, 14];
    difference() {
        translate([-size[0] / 2, -size[1] / 2, 0]) rbox([size[0], size[1], 2.5], 3);
        translate([0, 0, -1]) linear_extrude(5) rrect(win - [6, 6], 1.5);
        for (sx = [-1, 1], sy = [-1, 1]) translate([sx * (size[0] / 2 - 3.6), sy * (size[1] / 2 - 3.6), -1]) cylinder(d = CLEAR_M3 + HOLE_COMP, h = 5);
    }
}

module rail_coupon() difference() {
    translate([0, 0, 0]) union() {
        picatinny_clamp_body(20 + 2 * PIC_SLOT_PITCH - 10);
        translate([-15, -PJ_W / 2, 0]) cube([30, PJ_W, 3]);
    }
    picatinny_clamp_cut(20 + 2 * PIC_SLOT_PITCH - 10);
}

if (part == "dovetail_gauge") dovetail_gauge();
else if (part == "insert_coupon") insert_coupon();
else if (part == "window_coupon") window_coupon();
else if (part == "window_bezel") window_bezel();
else if (part == "rail_coupon") rotate([180, 0, 0]) rail_coupon();
else if (part == "rail_jaw") rotate([-90, 0, 0]) picatinny_jaw(20 + 2 * PIC_SLOT_PITCH - 10);
