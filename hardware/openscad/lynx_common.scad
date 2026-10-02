// TeamLynx Phase 4 shared parameters and helpers.
// Frame convention for every helmet part: X forward, Y left, Z up (head body frame FLU),
// units mm. This file only defines modules/functions; it renders nothing.

// ---------------------------------------------------------------- print tolerances
// Tuned for a 0.4 mm nozzle, 0.2 mm layers, calibrated e-steps/flow. Print
// hardware/openscad/fit_coupons.scad first and adjust these if the coupons say so.
CLR_SLIDE = 0.20;   // per side, parts that must slide (sled, plates in grooves)
CLR_LOOSE = 0.35;   // per side, drop-in parts (boards, windows in pockets)
CLR_PRESS = 0.05;   // per side, press fits
HOLE_COMP = 0.20;   // added to vertical hole diameters (FDM holes print undersize)

// ---------------------------------------------------------------- fasteners
// Brass heat-set inserts (ruthex / CNC Kitchen pattern, knurled, tapered lead-in).
//   [hole_d, hole_depth, insert_len]
INSERT_M2 = [3.2, 4.5, 4.0];
INSERT_M25 = [3.6, 5.0, 4.0];
INSERT_M3 = [4.0, 6.5, 5.7];
// Clearance holes (ISO 273 medium) and head counterbores.
CLEAR_M2 = 2.4;
CLEAR_M25 = 2.9;
CLEAR_M3 = 3.4;
HEAD_M3_SHCS = [5.9, 3.2];    // ISO 4762 head d, k (+clearance)
HEAD_M3_BUTTON = [6.0, 1.8];  // ISO 7380
NUT_M3 = [5.5 + 2*CLR_LOOSE, 2.6]; // across flats, thickness (nyloc DIN 985 is 4.0)
NYLOC_M3_H = 4.0;

// Minimum wall around an insert hole: 1.5 mm. Boss diameter = hole_d + 3.
function insert_boss_d(ins) = ins[0] + 3.0;

// ---------------------------------------------------------------- MIL-STD-1913 rail
PIC_W = 21.20;       // across the V points
PIC_TOP = 15.60;     // top flat
PIC_V = 2.80;        // depth from top flat to V point
PIC_HEAD = 5.60;     // top flat to bottom of the lower 45 deg flank
PIC_SLOT_W = 5.35;   // recoil groove width
PIC_SLOT_PITCH = 10.01;
PIC_SLOT_D = 3.0;    // groove depth from top flat

// ---------------------------------------------------------------- webbing
WEB_25 = [26.0, 3.6];  // slot for 25 mm (1") webbing / hook-and-loop strap

$fa = 4;
$fs = 0.4;

// ---------------------------------------------------------------- primitives

// Box with vertical edges rounded (r) and its min corner at the origin.
module rbox(size, r = 2) {
    r2 = min(r, size[0] / 2 - 0.01, size[1] / 2 - 0.01);
    if (r2 <= 0) cube(size);
    else hull() for (x = [r2, size[0] - r2], y = [r2, size[1] - r2])
        translate([x, y, 0]) cylinder(r = r2, h = size[2]);
}

// Same, centred in X and Y, Z from 0.
module rbox_c(size, r = 2) {
    translate([-size[0] / 2, -size[1] / 2, 0]) rbox(size, r);
}

// 2D rounded rectangle centred on the origin.
module rrect(size, r = 2) {
    r2 = min(r, size[0] / 2 - 0.01, size[1] / 2 - 0.01);
    if (r2 <= 0) square(size, center = true);
    else hull() for (x = [-1, 1], y = [-1, 1])
        translate([x * (size[0] / 2 - r2), y * (size[1] / 2 - r2)]) circle(r = r2);
}

// Heat-set insert hole pointing down -Z from z=0 (subtract it). Adds a 0.4 mm lead-in
// chamfer and 1 mm of extra depth so displaced melt has somewhere to go.
module insert_hole(ins = INSERT_M3) {
    d = ins[0] + HOLE_COMP;
    translate([0, 0, -ins[1] - 1.0]) cylinder(d = d, h = ins[1] + 1.0 + 0.01);
    translate([0, 0, -0.4]) cylinder(d1 = d, d2 = d + 0.8, h = 0.41);
}

// Clearance hole along -Z from z=0, length l, optional counterbore at the top.
module clear_hole(d = CLEAR_M3, l = 10, head = undef) {
    translate([0, 0, -l]) cylinder(d = d + HOLE_COMP, h = l + 0.01);
    if (head != undef) translate([0, 0, -head[1]]) cylinder(d = head[0] + HOLE_COMP, h = head[1] + 0.01);
}

// Hex nut trap along -Z from z=0.
module nut_trap(nut = NUT_M3, depth = NYLOC_M3_H) {
    translate([0, 0, -depth]) cylinder(d = nut[0] / cos(30), h = depth + 0.01, $fn = 6);
}

// Slot for webbing / strap, long axis along Y, through Z.
module web_slot(slot = WEB_25, h = 20) {
    translate([0, 0, -h / 2]) linear_extrude(h) rrect([slot[1], slot[0]], slot[1] / 2 - 0.01);
}

// Elongated hole (screw slot) along X of length l, through Z (centred on z=0, height h).
module slot_hole(d, l, h) {
    translate([0, 0, -h / 2]) hull() for (x = [-l / 2, l / 2])
        translate([x, 0, 0]) cylinder(d = d + HOLE_COMP, h = h);
}

// ---------------------------------------------------------------- NVG dovetail shoe
// Male dovetail shoe as fitted to NVGs for Wilcox G24/L4-style receivers (and the repro
// mounts that copy them). No dimensional standard is published, so these are nominal
// starting values: print fit_coupons.scad (dovetail gauge), measure your receiver and set
// them. The adjustable gib (an M3 set screw pushing a flexure flank) takes up the last
// 0.0-0.6 mm, so a slightly loose print still locks up solid.
DT_W_TOP = 24.0;     // widest width, at the top surface of the shoe
DT_H = 5.0;          // dovetail height
DT_ANGLE = 60;       // flank angle from the base plane
DT_LEN = 30.0;       // length along the insertion axis
DT_RISER = 3.0;      // plain riser under the dovetail so the receiver lips clear the host
DT_LATCH_D = 4.2;    // detent hole for the receiver's spring latch (blind, in the top face)
DT_LATCH_DEPTH = 2.5;
DT_LATCH_POS = 9.0;  // from the leading (first-in) end
DT_GIB_SLOT = 1.2;   // flexure slot width
DT_FIT = 0.0;        // global +/- width trim from the coupon (negative = tighter)

function dt_w_neck(w_top = DT_W_TOP, h = DT_H, a = DT_ANGLE) = w_top - 2 * h / tan(a);

// 2D trapezoid in the plane normal to the insertion axis (u across, v up).
module dovetail_profile(w_top = DT_W_TOP, h = DT_H, a = DT_ANGLE, trim = DT_FIT) {
    wt = w_top + trim;
    wn = dt_w_neck(wt, h, a);
    polygon([[-wn / 2, 0], [wn / 2, 0], [wt / 2, h], [-wt / 2, h]]);
}

// The shoe sits on z=0 (host top surface) and extends up; insertion axis along +X
// (the leading end is at +X) when axis="x", or along +Y when axis="y".
module dovetail_shoe(axis = "x", len = DT_LEN, w_top = DT_W_TOP, h = DT_H, a = DT_ANGLE,
                     riser = DT_RISER, gib = true) {
    wn = dt_w_neck(w_top + DT_FIT, h, a);
    rot = axis == "x" ? [0, 0, 0] : [0, 0, 90];
    rotate(rot) difference() {
        union() {
            // riser block, as wide as the neck plus 2 mm so it carries load into the host
            translate([-len / 2, -(wn + 2) / 2, 0]) cube([len, wn + 2, riser + 0.01]);
            translate([0, 0, riser]) rotate([90, 0, 90]) translate([0, 0, -len / 2])
                linear_extrude(len) dovetail_profile(w_top, h, a);
        }
        // latch detent
        translate([len / 2 - DT_LATCH_POS, 0, riser + h - DT_LATCH_DEPTH])
            cylinder(d = DT_LATCH_D + HOLE_COMP, h = DT_LATCH_DEPTH + 0.1);
        // lead-in chamfers on the leading end
        for (s = [-1, 1]) translate([len / 2, s * (w_top / 2), riser + h])
            rotate([0, 0, 45]) cube([2.0, 2.0, 2 * (h + 0.2)], center = true);
        if (gib) {
            // flexure: slot parallel to the -Y flank, open at the trailing end, and an M3
            // insert + set screw pressing the flank outward from the +Y side.
            fx = len - 6;
            translate([-len / 2 - 0.01, -wn / 2 + 2.2, riser + 0.8])
                cube([fx, DT_GIB_SLOT, h + 0.5]);
            translate([-len / 2 + fx / 2, wn / 2 + 1.0, riser + h / 2 + 0.4]) rotate([90, 0, 0])
                cylinder(d = INSERT_M3[0] + HOLE_COMP, h = wn / 2 + 1.0 - (-wn / 2 + 2.2 + DT_GIB_SLOT) + 0.2);
        }
    }
}

// ---------------------------------------------------------------- Picatinny clamp
// Clamp body that sits ON TOP of a MIL-STD-1913 rail section (rail runs along X).
// Returns the solid to be unioned; call picatinny_clamp_cut() to remove the rail
// channel. The movable jaw is a separate part (picatinny_jaw). Two M3 cross bolts sit in
// recoil grooves two pitches apart, which also stops the part sliding along the rail.
PJ_LEN = 32;
PJ_H = 8.0;           // clamp body height below the host's base plane (7 mm below the top flat)
PJ_W = PIC_W + 9.0;   // body width across the rail
module picatinny_clamp_body(len = PJ_LEN) {
    translate([-len / 2, -PJ_W / 2, -PJ_H]) cube([len, PJ_W, PJ_H]);
}

// rail head envelope (with clearance), in YZ, the top flat touching z=-1.0 (1 mm roof)
module _pic_head_2d(c = CLR_SLIDE) {
    polygon([
        [-PIC_TOP / 2 - c, 0], [PIC_TOP / 2 + c, 0],
        [PIC_W / 2 + c, -PIC_V], [PIC_TOP / 2 + c, -PIC_HEAD - c],
        [PIC_TOP / 2 + c, -PIC_HEAD - 10], [-PIC_TOP / 2 - c, -PIC_HEAD - 10],
        [-PIC_TOP / 2 - c, -PIC_HEAD - c], [-PIC_W / 2 - c, -PIC_V]]);
}

module picatinny_clamp_cut(len = PJ_LEN) {
    // rail channel
    translate([0, 0, -1.0]) rotate([90, 0, 90]) translate([0, 0, -len / 2 - 1])
        linear_extrude(len + 2) _pic_head_2d();
    // jaw pocket: the +Y side under the roof is removed and replaced by the jaw
    translate([-len / 2 - 1, PIC_TOP / 2 - 1.0, -PJ_H - 0.01]) cube([len + 2, PJ_W, PJ_H - 1.0 + 0.01]);
    // cross bolts in two recoil grooves (M3 x 30 SHCS head on -Y, nyloc on the jaw side)
    for (x = [-PIC_SLOT_PITCH, PIC_SLOT_PITCH]) translate([x, 0, -1.0 - PIC_SLOT_D / 2]) {
        rotate([90, 0, 0]) cylinder(d = CLEAR_M3 + HOLE_COMP, h = PJ_W, center = true);
        translate([0, -PJ_W / 2 + 3.4, 0]) rotate([90, 0, 0]) cylinder(d = HEAD_M3_SHCS[0] + HOLE_COMP, h = 4);
    }
}

// Movable jaw, printed separately. Origin as in the body. Print on its +Y face.
module picatinny_jaw(len = PJ_LEN) {
    c = CLR_SLIDE;
    difference() {
        translate([-len / 2 + c, PIC_TOP / 2 - 1.0 + c, -PJ_H]) cube([len - 2 * c, PJ_W / 2 - PIC_TOP / 2 + 1.0 - c, PJ_H - 1.0 - c]);
        translate([0, 0, -1.0]) rotate([90, 0, 90]) translate([0, 0, -len / 2 - 1])
            linear_extrude(len + 2) _pic_head_2d();
        for (x = [-PIC_SLOT_PITCH, PIC_SLOT_PITCH]) translate([x, 0, -1.0 - PIC_SLOT_D / 2]) {
            rotate([90, 0, 0]) cylinder(d = CLEAR_M3 + HOLE_COMP, h = PJ_W + 2, center = true);
            translate([0, PJ_W / 2 + 0.01, 0]) rotate([90, 0, 0]) rotate([0, 0, 30]) nut_trap(NUT_M3, NYLOC_M3_H);
        }
    }
}

// Snap-on clip around the rail head (no tools), rail along X, top flat at z=0. The side
// walls must open 2.8 mm to pass the V points: that is ~14 % bending strain at these
// proportions, so print it in TPU 95A. PETG/nylon versions crack; slide those on from the
// rail end instead.
module picatinny_snap(len = 12, t = 2.2) {
    c = CLR_SLIDE;
    difference() {
        translate([-len / 2, -(PIC_W / 2 + t + c), -(PIC_HEAD + 1.0 + c)])
            cube([len, PIC_W + 2 * (t + c), PIC_HEAD + 1.0 + c + t]);
        rotate([90, 0, 90]) translate([0, 0, -len / 2 - 1]) linear_extrude(len + 2) _pic_head_2d(c);
    }
}

// ---------------------------------------------------------------- reference parts (colour
// stand-ins used only in assembly renders, never exported as STL)
module ref_board(size, c = "green") color(c) cube(size);
