// TeamLynx Phase 4: cable management for the rail switch and the helmet harness.
//   rail_clip     TPU 95A snap-on clip for a MIL-STD-1913 rail with a press-in channel for the
//                 rail-switch cable (route along the handguard to the stock/shoulder).
//   strap_clip    push-on C-hook for 25 mm webbing (shoulder strap, plate-carrier MOLLE) with a
//                 press-in cable channel; the cable pulls out before the plug is loaded.
//   helmet_saddle hook-and-loop backed cable saddle for the helmet shell: USB, IR pair, flat
//                 HDMI FPC side by side.
//   plug_boot     TPU sleeve over the 3.5 mm plug / cable joint (where plugs fail).
// Parts: rail_clip strap_clip helmet_saddle plug_boot | assembly
include <lynx_common.scad>

part = "assembly";

rail_cable_d = 4.0;    // remote pressure-switch cable OD (typ. 3.5-4.2 mm)
usb_cable_d = 4.5;
ir_cable_d = 4.0;      // 2 x 22 AWG twisted pair in 4 mm braided sleeve
fpc_w = 17.5;          // HDMI FPC ribbon (A/mini/micro HDMI FPV ribbon kits are 15-17 mm)
fpc_t = 0.6;

// C-channel along X for a cable of diameter d: material ring t thick, mouth 0.75 d.
module cable_channel(d, len, t = 1.8) {
    difference() {
        rotate([0, 90, 0]) cylinder(d = d + 2 * t, h = len, center = true);
        rotate([0, 90, 0]) cylinder(d = d + 0.2, h = len + 2, center = true);
        translate([-len / 2 - 1, -0.375 * d, 0]) cube([len + 2, 0.75 * d, d]);
    }
}

module rail_clip_part() {
    len = 12;
    side = PIC_W / 2 + 2.2 + CLR_SLIDE;       // outer face of the snap's side wall
    ring_r = rail_cable_d / 2 + 1.8;
    union() {
        picatinny_snap(len);
        // channel on the side of the clip, axis along the rail, mouth facing outward (+Y)
        translate([0, side + ring_r - 0.8, -PIC_V]) rotate([-90, 0, 0]) cable_channel(rail_cable_d, len);
    }
}

module strap_clip_part() {
    w = WEB_25[0];
    t = 2.4;
    gap = 2.6;         // webbing thickness 1.6-2.2 mm
    len = 14;
    difference() {
        union() {
            translate([-len / 2, -w / 2 - t, 0]) cube([len, w + 2 * t, gap + 2 * t]);
            translate([0, 0, gap + 2 * t + (rail_cable_d + 3.6) / 2 - 0.6]) rotate([0, 0, 0]) cable_channel(rail_cable_d, len);
        }
        // webbing slot, open along the +Y edge (push the clip sideways onto the strap)
        translate([-len / 2 - 1, -w / 2, t]) cube([len + 2, w + t + 1, gap]);
        // keep a 1.4 mm barb at the mouth so it does not walk off
    }
    translate([-len / 2, w / 2 - 0.01, t - 0.01]) cube([len, 1.2, 0.9]);
}

module helmet_saddle_part() {
    l = 18;
    base = [l, 46, 2.2];
    translate([-l / 2, -base[1] / 2, 0]) cube(base);
    translate([0, -14, base[2] + (usb_cable_d + 3.6) / 2 - 0.8]) cable_channel(usb_cable_d, l);
    translate([0, -3, base[2] + (ir_cable_d + 3.6) / 2 - 0.8]) cable_channel(ir_cable_d, l);
    // FPC keeper: two posts with an overhang, ribbon slides in sideways
    translate([-l / 2, 1, base[2] - 0.01]) difference() {
        cube([l, fpc_w + 4, fpc_t + 2.6]);
        translate([-1, 2, -0.01 + 0.01]) cube([l + 2, fpc_w, fpc_t + 0.8]);
        translate([-1, 2 + 3, fpc_t + 0.8 - 0.01]) cube([l + 2, fpc_w - 6, 3]);
    }
}

module plug_boot_part() {
    // ID at the plug end 6.6 mm (generic 3.5 mm plug overmould ~6 mm), at the cable end 3.8 mm
    difference() {
        cylinder(d1 = 9.4, d2 = 6.8, h = 26);
        translate([0, 0, -0.01]) cylinder(d = 6.6, h = 12);
        translate([0, 0, 11.9]) cylinder(d1 = 6.6, d2 = rail_cable_d - 0.2, h = 6);
        translate([0, 0, 17]) cylinder(d = rail_cable_d - 0.2, h = 10);
    }
}

module assembly() {
    color([0.2, 0.2, 0.2]) rail_clip_part();
    color([0.3, 0.33, 0.27]) translate([0, 50, 0]) strap_clip_part();
    color([0.35, 0.38, 0.3]) translate([0, 100, 0]) helmet_saddle_part();
    color([0.15, 0.15, 0.15]) translate([30, 0, 0]) plug_boot_part();
}

if (part == "rail_clip") rotate([0, 90, 0]) rail_clip_part();   // print on its end
else if (part == "strap_clip") rotate([0, 90, 0]) strap_clip_part();      // on its end
else if (part == "helmet_saddle") rotate([0, 90, 0]) helmet_saddle_part();
else if (part == "plug_boot") plug_boot_part();
else if (part == "assembly") assembly();
