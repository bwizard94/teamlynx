// TeamLynx Phase 4: rear helmet counterweight bracket + compute/power cassette.
//   base     : curved plate for the helmet's rear loop panel (hook-and-loop) plus two 25 mm
//              webbing slots for straps to the side rails; two female dovetail grooves.
//   cassette : Jetson Orin Nano (Super) Developer Kit bay + power bay (USB-PD trigger at
//              15 V, fuses, Mean Well LDD-350L IR driver, IR interlock, IR SAFE/ARM toggle).
//              Slides DOWN onto the base and is locked by one M3 thumb screw at the top tab.
//   lid      : fan intake grille over the Jetson bay, toggle hole + guard over the power bay.
// Local frame: X across the back of the helmet (head -Y, i.e. wearer's right), Y up the
// helmet (head +Z), Z outward (head -X). The Jetson I/O edge faces -Y (down): cables leave
// downward, rain is shed.
// Parts: base cassette rail lid | assembly
//   rail : 2x male dovetail rails, PA-CF, screwed under the cassette floor with 2x M2.5 x 8
//          countersunk screws each (the cassette then prints floor-down without supports)
include <lynx_common.scad>

part = "assembly";

// ---------------------------------------------------------------- parameters
helmet_r = 115;        // rear shell radius about the vertical axis (FAST/MICH-type ~105-125)
base_t = 4.0;
base_half = [75, 54];  // half extents X, Y
saddle_half = [45, 52];
saddle_top = 6;        // flat top of the saddle, above the shell apex

wall = 2.4;
floor_t = 2.4;
lid_t = 2.4;
jet_bay = [104, 83];   // Jetson dev kit: 100 x 79 carrier + 2 mm all round
pwr_bay = [34, 83];
inner_h = 44;          // dev kit stack ~31 mm above the standoffs + fan intake gap
jet_board = [100, 79];
jet_holes = [86, 58];  // carrier mounting-hole pattern: VERIFY on your carrier with calipers
jet_hole_offset = [0, 0];
jet_standoff = 5;
pdb_holes = [26, 66];  // 3 x 7 cm perfboard, long side along Y
pdb_y = 4;
pdb_standoff = 10;
pd_board = [12.0, 22.0, 1.6];   // CH224K-type fixed-voltage PD trigger (W, L, T)
pd_cut = [9.6, 3.8];            // USB-C receptacle face + clearance
gx12_hole = 12.2;
toggle_hole = 6.5;     // 1/4-40 bushing mini toggle (C&K 7101 type)

tongue_w = 12;         // cassette-to-base dovetail tongues
tongue_h = 4;
tongue_len = 80;
tongue_x = 30;
rail_screw_y = [-12, 14];
tie_x = [6, 40, 70];      // from the left end of the Jetson bay; move the first pair to your DC jack

// ---------------------------------------------------------------- derived
cas = [2 * wall + jet_bay[0] + 2 + pwr_bay[0], 2 * wall + jet_bay[1], floor_t + inner_h];
x0 = -cas[0] / 2;
jet_x = x0 + wall + jet_bay[0] / 2;
pwr_x = x0 + wall + jet_bay[0] + 2 + pwr_bay[0] / 2;
y_in0 = -cas[1] / 2 + wall;
jet_c = [jet_x, y_in0 + 1.5 + jet_board[1] / 2] + jet_hole_offset;
col = [for (x = [x0 + wall + 2, x0 + wall + jet_bay[0] + 1, cas[0] / 2 - wall - 2], y = [-cas[1] / 2 + wall + 2, cas[1] / 2 - wall - 2]) [x, y]];
tab_y = cas[1] / 2 + 5;

echo(str("REAR cassette ", cas[0], " x ", cas[1], " x ", cas[2] + lid_t, " mm; base ", 2 * base_half[0], " x ", 2 * base_half[1]));

function shell_z(x) = sqrt(helmet_r * helmet_r - x * x) - helmet_r;

// ---------------------------------------------------------------- base
module helmet_cyl(r) translate([0, 0, -helmet_r]) rotate([90, 0, 0]) cylinder(r = r, h = 400, center = true, $fn = 360);

module base_part() {
    difference() {
        union() {
            // curved shell
            intersection() {
                difference() { helmet_cyl(helmet_r + base_t); helmet_cyl(helmet_r); }
                translate([-base_half[0], -base_half[1], -60]) cube([2 * base_half[0], 2 * base_half[1], 80]);
            }
            // saddle up to a flat top
            difference() {
                translate([-saddle_half[0], -saddle_half[1], -30]) rbox([2 * saddle_half[0], 2 * saddle_half[1], 30 + saddle_top], 4);
                helmet_cyl(helmet_r);
            }
        }
        // female dovetail grooves, open at the top (+Y), closed 2 mm before the bottom
        for (s = [-1, 1]) translate([s * tongue_x, -tongue_len / 2 - 1 - CLR_SLIDE, saddle_top + 0.01]) rotate([-90, 0, 0])
            linear_extrude(200) offset(delta = CLR_SLIDE + 0.05) dovetail_profile(tongue_w, tongue_h, 60, 0);
        // lightening pocket between the grooves, from the top; leaves the shell + 1 mm
        difference() {
            w = 2 * tongue_x - tongue_w - 8;
            translate([-w / 2, -saddle_half[1] + 6, -30]) rbox([w, 2 * saddle_half[1] - 20, 30 + saddle_top + 1], 2);
            helmet_cyl(helmet_r + base_t + 1.0);
        }
        // thumb-screw insert at the top tab
        translate([0, tab_y, saddle_top]) insert_hole(INSERT_M3);
        // webbing slots on the wings
        for (s = [-1, 1]) translate([s * (base_half[0] - 8), 0, shell_z(base_half[0] - 8) + base_t / 2]) rotate([0, s * asin((base_half[0] - 8) / helmet_r), 0])
            web_slot(WEB_25, 20);
    }
    // thumb-screw boss (the saddle may be pocketed under it)
    translate([0, tab_y, -12]) difference() {
        cylinder(d = insert_boss_d(INSERT_M3) + 2, h = 12 + saddle_top);
        translate([0, 0, 12 + saddle_top + 0.01]) insert_hole(INSERT_M3);
    }
}

// ---------------------------------------------------------------- cassette
module cassette_part() {
    difference() {
        union() {
            translate([x0, -cas[1] / 2, 0]) rbox(cas, 3);
            // thumb-screw tab
            translate([-9, cas[1] / 2 - 3, 0]) rbox([18, 3 + 9, 4], 3);
            // drip hood over the top-wall exhaust slots
            translate([jet_x - 45, cas[1] / 2 - 0.01, floor_t + 36]) cube([90, 7, 2]);
        }
        // bays
        translate([x0 + wall, y_in0, floor_t]) rbox([jet_bay[0], jet_bay[1], inner_h + 1], 1.5);
        translate([x0 + wall + jet_bay[0] + 2, y_in0, floor_t]) rbox([pwr_bay[0], pwr_bay[1], inner_h + 1], 1.5);
        // Jetson I/O opening, bottom wall
        translate([jet_x - jet_bay[0] / 2 + 2, -cas[1] / 2 - 1, floor_t + 3]) cube([jet_bay[0] - 4, wall + 2, inner_h - 7]);
        // exhaust slots, top wall (under the hood)
        for (i = [0 : 13]) translate([jet_x - 42 + i * 6.3, cas[1] / 2 - wall - 1, floor_t + 18]) cube([3, wall + 2, 17]);
        // tie slots in the floor along the I/O edge: barrel plug, USB-A stacks, DP plug
        for (x = tie_x, dx = [0, 6]) translate([jet_x - jet_bay[0] / 2 + x + dx, y_in0 + 3, -1]) cube([2.2, 4.5, floor_t + 2]);
        // rail screw inserts, from below
        for (s = [-1, 1], y = rail_screw_y) translate([s * tongue_x, y, 0]) mirror([0, 0, 1]) insert_hole(INSERT_M25);
        // power bay: PD input cut-out and GX12-2 IR socket in the bottom wall
        translate([pwr_x - 8, -cas[1] / 2 - 1, floor_t + 6 + pd_board[2] + 1.6]) rotate([-90, 0, 0])
            linear_extrude(wall + 2) rrect(pd_cut, 1.6);
        translate([pwr_x + 8, -cas[1] / 2 - 1, floor_t + 26]) rotate([-90, 0, 0]) cylinder(d = gx12_hole + HOLE_COMP, h = wall + 2);
        // divider pass-through for the Jetson DC lead
        translate([x0 + wall + jet_bay[0] - 1, y_in0 + 6, floor_t + 4]) cube([4, 8, 6]);
        // lid inserts
        for (p = col) translate([p[0], p[1], cas[2]]) insert_hole(INSERT_M3);
        // tab clearance hole (M3 thumb screw)
        translate([0, tab_y, 4.01]) clear_hole(CLEAR_M3, 6);
    }
    // lid columns
    for (p = col) translate([p[0], p[1], 0]) difference() {
        cylinder(d = insert_boss_d(INSERT_M3), h = cas[2]);
        translate([0, 0, cas[2] + 0.01]) insert_hole(INSERT_M3);
    }
    // rail screw bosses (under the carrier, 1 mm clear of it)
    for (s = [-1, 1], y = rail_screw_y) translate([s * tongue_x, y, floor_t - 0.01]) difference() {
        cylinder(d = 7, h = jet_standoff - 1);
        translate([0, 0, -floor_t - 0.01]) cylinder(d = INSERT_M25[0] + HOLE_COMP, h = INSERT_M25[1] + 1.02);
    }
    // Jetson standoffs, M2.5 inserts
    for (dx = [-1, 1], dy = [-1, 1]) translate([jet_c[0] + dx * jet_holes[0] / 2, jet_c[1] + dy * jet_holes[1] / 2, floor_t - 0.01])
        difference() {
            cylinder(d = 6.5, h = jet_standoff + 0.01);
            translate([0, 0, jet_standoff + 0.01]) insert_hole(INSERT_M25);
        }
    // PDB perfboard standoffs, M2 inserts (board 30 x 70, long side along Y)
    for (dx = [-1, 1], dy = [-1, 1]) translate([pwr_x + dx * pdb_holes[0] / 2, pdb_y + dy * pdb_holes[1] / 2, floor_t - 0.01])
        difference() {
            cylinder(d = 5.5, h = pdb_standoff + 0.01);
            translate([0, 0, pdb_standoff + 0.01]) insert_hole(INSERT_M2);
        }
    // PD trigger cradle: two grooved rails; the board slides toward -Y until its receptacle
    // sits in the cut-out. A 5 mm EVA pad between it and the PDB above holds it down.
    pc = pwr_x - 8;
    gap = pd_board[0] / 2 - 0.5;
    for (s = [-1, 1]) translate([s < 0 ? pc - gap - 2.4 : pc + gap, y_in0 - 0.01, floor_t - 0.01]) difference() {
        cube([2.4, pd_board[1] + 1, 6 + pd_board[2] + 2]);
        translate([s < 0 ? 2.4 - 0.9 : -0.01, -1, 6]) cube([0.91, pd_board[1] + 3, pd_board[2] + 0.3]);
    }
}

// ---------------------------------------------------------------- rails
// Printed wide face down. Origin: rail centre on the cassette's bottom face, as fitted.
module rail_part() {
    difference() {
        translate([0, -tongue_len / 2, 0.01]) rotate([90, 0, 0]) mirror([0, 0, 1])
            linear_extrude(tongue_len) mirror([0, 1, 0]) dovetail_profile(tongue_w, tongue_h, 60, 0);
        for (y = rail_screw_y) translate([0, y, 0]) {
            translate([0, 0, 1]) cylinder(d = CLEAR_M25 + HOLE_COMP, h = 10, center = true);
            // ISO 10642 M2.5 countersink at the wide (bottom) face
            translate([0, 0, -tongue_h - 0.01]) cylinder(d1 = 5.2, d2 = 2.6, h = 1.4);
        }
        // lead-in at the bottom end
        translate([-tongue_w, -tongue_len / 2 - 0.01, -tongue_h - 0.01]) rotate([45, 0, 0]) cube([2 * tongue_w, 2, 2]);
    }
}

// ---------------------------------------------------------------- lid
module lid_part() {
    difference() {
        union() {
            translate([x0, -cas[1] / 2, 0]) rbox([cas[0], cas[1], lid_t], 3);
            // toggle guard ring
            translate([pwr_x, 22, lid_t - 0.01]) difference() {
                cylinder(d = 22, h = 9);
                translate([0, 0, -1]) cylinder(d = 17, h = 11);
                translate([-3, -12, 3]) cube([6, 24, 7]);
            }
        }
        // fan intake grille over the Jetson bay
        for (i = [0 : 11]) translate([jet_x - 41 + i * 7, -cas[1] / 2 + 16, -1]) cube([3.2, cas[1] - 30, lid_t + 2]);
        translate([pwr_x, 22, -1]) cylinder(d = toggle_hole + HOLE_COMP, h = lid_t + 2);
        for (p = col) translate([p[0], p[1], lid_t + 0.01]) clear_hole(CLEAR_M3, lid_t + 3, [HEAD_M3_BUTTON[0], 0.8]);
        translate([pwr_x, -10, lid_t - 0.6]) rotate(90) linear_extrude(1) text("IR SAFE  ARM", size = 4.0, halign = "center", valign = "center");
    }
}

// ---------------------------------------------------------------- reference parts
module ref_jetson() {
    zb = floor_t + jet_standoff;
    color([0.1, 0.35, 0.15]) translate([jet_c[0] - jet_board[0] / 2, jet_c[1] - jet_board[1] / 2, zb]) cube([jet_board[0], jet_board[1], 1.6]);
    color([0.12, 0.12, 0.12]) translate([jet_c[0] - 35, jet_c[1] - 25, zb + 1.6]) cube([70, 50, 10]);
    color([0.55, 0.57, 0.6]) translate([jet_c[0] - 35, jet_c[1] - 25, zb + 11.6]) difference() {
        cube([70, 50, 16]);
        for (i = [0 : 15]) translate([2 + i * 4.3, -1, 3]) cube([2.2, 52, 14]);
    }
    color([0.1, 0.1, 0.1]) translate([jet_c[0], jet_c[1], zb + 27.6]) cylinder(d = 40, h = 3);
    color([0.65, 0.65, 0.68]) for (x = [-30, -14, 2, 18]) translate([jet_c[0] + x, jet_c[1] - jet_board[1] / 2 - 1, zb + 1.6]) cube([14, 16, 14]);
}
module ref_power() {
    color([0.1, 0.4, 0.15]) translate([pwr_x - 15, pdb_y - 35, floor_t + pdb_standoff]) cube([30, 70, 1.6]);
    color([0.2, 0.2, 0.3]) translate([pwr_x - 8 - pd_board[0] / 2, y_in0, floor_t + 6]) cube(pd_board);
    color([0.15, 0.15, 0.15]) translate([pwr_x - 11, 0, floor_t + pdb_standoff + 1.6]) cube([22, 9.5, 8.9]);
}

module rear_layout_proxy() assembly();   // used by helmet_layout.scad

module assembly() {
    color([0.35, 0.38, 0.3]) base_part();
    color([0.3, 0.33, 0.27]) translate([0, 0, saddle_top + tongue_h + 0.2]) {
        cassette_part();
        for (s = [-1, 1]) color([0.2, 0.2, 0.2]) translate([s * tongue_x, 0, 0]) rail_part();
        ref_jetson();
        ref_power();
    }
    color([0.3, 0.33, 0.27, 0.35]) translate([0, 0, saddle_top + tongue_h + 0.2 + cas[2] + 0.2]) lid_part();
}

if (part == "base") base_part();
else if (part == "cassette") cassette_part();
else if (part == "rail") rail_part();   // wide face on the bed
else if (part == "lid") lid_part();
else if (part == "assembly") assembly();
