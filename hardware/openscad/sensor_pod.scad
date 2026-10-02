// TeamLynx Phase 4: NVG-shroud sensor pod.
//   NoIR UVC board camera (38 x 38 mm, M12 lens) + BNO085 on ONE rigid sled (boresight
//   stays valid), 2 x SFH 4715AS 850 nm on 20 mm stars + 40x40x11 heatsink in a separate,
//   vented compartment, two replaceable 3 mm polycarbonate windows behind a screwed bezel,
//   and a dovetail shoe for Wilcox G24/L4-pattern NVG mounts.
// Frame: X forward (optical axis), Y left, Z up. Origin: rear-bottom-centre of the body.
//
// Render one part:  openscad -D 'part="body"' -o body.stl sensor_pod.scad
// Parts: body lid bezel sled | 2D (DXF): window_cam window_ir gasket diffuser led_plate
//        | assembly (colour preview, not for STL)
include <lynx_common.scad>

part = "assembly";

// ---------------------------------------------------------------- parameters
wall = 2.4;
floor_t = 2.4;
comp_w = 42;          // inner width (Y) of each compartment
div_t = 2.4;          // camera / illuminator divider (also the optical baffle)
inner_h = 43;
pod_d = 68;           // body depth incl. the front flange
flange_t = 8.5;       // front flange depth (X)
fl_side = 6;          // flange overhang beyond the body, left/right
fl_bot = 4;           // flange overhang below the floor
fl_top = 3;           // flange brow above the lid
lid_t = 4.0;

// window stack: gasket under the window (inside), bezel on top (outside)
win_t = 3.0;          // polycarbonate (Lexan 9034 / Makrolon GP), NOT IR-blocking grades
gasket_t = 1.0;       // closed-cell neoprene/EPDM foam tape, black
gasket_c = 0.7;       // compressed thickness (30 %)
win_proud = 0.3;      // window stands proud of the flange face before the bezel goes on
win_land = 4;         // window overlap onto the land, each side
bezel_lip = 3;        // bezel overlap onto the window, each side
bezel_t = 2.5;
bezel_rim = 1.5;      // raised bump rim height
bezel_rim_w = 1.8;

cam_ap = [34, 30];    // camera aperture (Y, Z)
ir_ap = [34, 30];

// camera board (typical IMX462 / IMX291 UVC M12 board: 38 x 38, M2 holes 34 mm square)
cam_board = 38;
cam_board_t = 1.6;
cam_hole_pitch = 34;
cam_standoff = 5;     // clearance for parts on the board back
lens_len = 22;        // board front face to lens front (measure yours; sled adjusts +/-4)
lens_gap = 1.5;       // lens front to window inner face
lens_d = 16;          // M12 holder / barrel envelope

// BNO085 breakout (Adafruit 4754: 25.4 x 17.8, holes on a 20.32 x 12.7 grid)
bno = [17.8, 25.4];   // mounted with its long side across (Y); sensor X arrow -> pod +X
bno_holes = [12.7, 20.32];
bno_standoff = 5.5;

// illuminator
star_d = 20;
star_t = 1.6;
led_dome = 2.3;       // OSLON Black package height above the star
led_pitch = 21;       // star centres, along Y
led_plate_t = 2.0;    // 5052/6061 aluminium
hs = [40, 40, 11];    // heatsink base Y x Z, fin depth X

// PG7 cable gland (M12.5 thread)
gland_hole = 12.7;
gland_nut_af = 15;
gland_nut_t = 5;
vent_hole = 6.0;      // adhesive ePTFE breather (Gore PolyVent-type, 10 mm patch)

// shoe
shoe_axis = "x";      // insertion direction: "x" (fore-aft) or "y" (lateral). Match your mount.

// ---------------------------------------------------------------- derived
pod_w = 2 * wall + 2 * comp_w + div_t;
body_h = floor_t + inner_h;
y_cam = div_t / 2 + comp_w / 2;
y_ir = -y_cam;
z_oc = floor_t + inner_h / 2;                      // optical centre height
x_fl = pod_d - flange_t;                            // flange back face
fz0 = -fl_bot;
fz1 = body_h + lid_t + fl_top;
fw = pod_w + 2 * fl_side;
pocket = win_t + gasket_t - win_proud;              // window pocket depth
x_wi = pod_d - pocket + gasket_c;                   // window inner face, installed
win_cam = cam_ap + [2 * win_land, 2 * win_land];
win_ir = ir_ap + [2 * win_land, 2 * win_land];
x_lens_front = x_wi - lens_gap;
x_board_front = x_lens_front - lens_len;
x_plate_front = x_board_front - cam_board_t - cam_standoff;
sled_plate_t = 3;
foot_len = 20;
foot_t = 4;
foot_boss_h = 4.5;
foot_screw_x = [x_plate_front - sled_plate_t - foot_len + 3.5, x_plate_front - sled_plate_t - 3.0];
x_star_front = x_fl - 0.3;
x_plate_al = x_star_front - star_t - led_plate_t;   // aluminium plate rear face
face_screws = [for (y = [-(fw / 2 - 5), 0, fw / 2 - 5], z = [fz0 + 5, fz1 - 5]) [y, z]];
lid_screws = [for (x = [wall + 2.0, x_fl - 3.0], y = [-(pod_w / 2 - wall - 2.0), 0, pod_w / 2 - wall - 2.0]) [x, y]];
shoe_x = x_fl / 2 + 2;
gland_cam_usb = [y_cam, 33];
gland_cam_imu = [y_cam, 13.5];
gland_ir = [y_ir, 33];
vent_pos = [y_cam + 13, 8];

echo(str("POD body ", pod_d, " x ", pod_w, " x ", body_h + lid_t, " mm; face ", fw, " x ", fz1 - fz0,
         "; windows cam ", win_cam, " ir ", win_ir, " x ", win_t, " mm"));
echo(str("POD lens front x=", x_lens_front, " window inner x=", x_wi, " sled plate front x=", x_plate_front));

// ---------------------------------------------------------------- body
module body_solid() {
    difference() {
        union() {
            translate([0, -pod_w / 2, 0]) rbox([x_fl + 0.01, pod_w, body_h], 3);
            translate([x_fl, -fw / 2, fz0]) rbox([flange_t, fw, fz1 - fz0], 4);
            // 45 deg wedge under the bottom lip so the body prints floor-down without support
            hull() {
                translate([x_fl, -pod_w / 2, fz0]) cube([0.01, pod_w, -fz0 + 0.01]);
                translate([x_fl + fz0 - 0.01, -pod_w / 2, 0]) cube([0.01, pod_w, 0.01]);
            }
        }
        // compartments
        for (yc = [y_cam, y_ir]) translate([wall, yc - comp_w / 2, floor_t]) cube([x_fl - wall + 0.01, comp_w, inner_h + 1]);
    }
    // lid screw columns (merged into corners and the divider)
    for (p = lid_screws) translate([p[0], p[1], 0]) cylinder(d = insert_boss_d(INSERT_M3) + 0.5, h = body_h);
}

module body_part() {
    difference() {
        body_solid();
        // the region above the body top behind the flange stays open for the lid
        // window pockets + apertures
        for (w = [[y_cam, win_cam, cam_ap], [y_ir, win_ir, ir_ap]]) {
            translate([pod_d - pocket, w[0], z_oc]) rotate([0, 90, 0]) linear_extrude(pocket + 1)
                rotate(90) rrect(w[1] + [2 * CLR_LOOSE, 2 * CLR_LOOSE], 2.3);
            translate([x_fl - 1, w[0], z_oc]) rotate([0, 90, 0]) linear_extrude(flange_t + 2)
                rotate(90) rrect(w[2], 1.5);
        }
        // bezel inserts in the flange face
        for (p = face_screws) translate([pod_d, p[0], p[1]]) rotate([0, 90, 0]) insert_hole(INSERT_M3);
        // lid inserts
        for (p = lid_screws) translate([p[0], p[1], body_h]) insert_hole(INSERT_M3);
        // LED plate grooves (illuminator compartment walls)
        translate([x_plate_al - CLR_SLIDE, y_ir - comp_w / 2 - 1.2, floor_t])
            cube([led_plate_t + 2 * CLR_SLIDE, comp_w + 2.4, inner_h + 1]);
        // sled screw slots through the floor (+/-4 mm focus travel), M3 x 10 from below
        for (x = foot_screw_x) translate([x, y_cam, floor_t / 2]) slot_hole(CLEAR_M3, 8, floor_t + 2);
        // rear wall: glands and breather
        for (g = [gland_cam_usb, gland_cam_imu, gland_ir]) translate([-1, g[0], g[1]]) rotate([0, 90, 0])
            cylinder(d = gland_hole + HOLE_COMP, h = wall + 2);
        translate([-1, vent_pos[0], vent_pos[1]]) rotate([0, 90, 0]) cylinder(d = vent_hole, h = wall + 2);
        // illuminator chimney: floor slots under the heatsink, side-wall slots high up
        for (i = [0 : 4]) {
            translate([x_plate_al - hs[2] + 1 + i * 2.2, y_ir - 15, -1]) cube([1.4, 30, floor_t + 2]);
        }
        for (i = [0 : 3]) translate([x_plate_al - hs[2] + 0.5 + i * 2.8, -pod_w / 2 - 1, 26]) cube([1.6, wall + 2, 15]);
    }
}

// ---------------------------------------------------------------- lid with shoe
module lid_part() {
    difference() {
        union() {
            translate([0, -pod_w / 2, 0]) rbox([x_fl - CLR_SLIDE, pod_w, lid_t], 3);
            translate([shoe_x, 0, lid_t - 0.01]) dovetail_shoe(axis = shoe_axis);
            // stiffening pad under the shoe footprint
            translate([shoe_x - DT_LEN / 2 - 4, -16, lid_t - 0.01]) rbox([DT_LEN + 8, 32, 1.2], 3);
        }
        for (p = lid_screws) translate([p[0], p[1], lid_t + 1.2 + 0.01]) clear_hole(CLEAR_M3, lid_t + 3, [HEAD_M3_BUTTON[0], 1.2 + 0.01]);
        // gland nut clearance under the lid is not needed (nuts stop 3.7 mm below)
    }
}

// ---------------------------------------------------------------- bezel
module bezel_part() {
    difference() {
        union() {
            rotate([0, 90, 0]) linear_extrude(bezel_t) rotate(90) translate([0, (fz0 + fz1) / 2])
                rrect([fw, fz1 - fz0], 4);
            // bump rim: keeps the windows off the ground when the helmet is set down face-first
            rotate([0, 90, 0]) linear_extrude(bezel_t + bezel_rim) rotate(90) translate([0, (fz0 + fz1) / 2])
                difference() { rrect([fw, fz1 - fz0], 4); rrect([fw - 2 * bezel_rim_w, fz1 - fz0 - 2 * bezel_rim_w], 2.5); }
        }
        for (w = [[y_cam, win_cam], [y_ir, win_ir]]) translate([-1, w[0], z_oc]) rotate([0, 90, 0])
            linear_extrude(bezel_t + bezel_rim + 2) rotate(90) rrect(w[1] - [2 * bezel_lip, 2 * bezel_lip], 1.5);
        for (p = face_screws) translate([-1, p[0], p[1]]) rotate([0, 90, 0])
            cylinder(d = CLEAR_M3 + HOLE_COMP, h = bezel_t + bezel_rim + 2);
    }
}
// bezel is modelled with its back face at x=0; in the assembly it sits at x=pod_d.

// ---------------------------------------------------------------- camera + IMU sled
// Origin: same as the body. Printed lying on its foot. M2 inserts for camera and BNO,
// M3 inserts (from below) for the two slotted mounting screws.
module sled_part() {
    yw = comp_w - 2 * CLR_SLIDE;
    xb = x_plate_front - sled_plate_t;   // plate back face
    difference() {
        union() {
            // camera plate
            translate([xb, y_cam - yw / 2, floor_t]) cube([sled_plate_t, yw, inner_h - 0.6]);
            // camera standoffs
            for (dy = [-1, 1], dz = [-1, 1]) translate([x_plate_front - 0.01, y_cam + dy * cam_hole_pitch / 2, z_oc + dz * cam_hole_pitch / 2])
                rotate([0, 90, 0]) cylinder(d = 6, h = cam_standoff + 0.01);
            // foot
            translate([xb - foot_len, y_cam - yw / 2, floor_t]) cube([foot_len + 0.01, yw, foot_t]);
            // foot screw bosses
            for (x = foot_screw_x) translate([x, y_cam, floor_t]) cylinder(d = insert_boss_d(INSERT_M3) + 0.5, h = foot_t + foot_boss_h);
            // BNO standoffs
            for (dx = [-1, 1], dy = [-1, 1]) translate([xb - foot_len / 2 + dx * bno_holes[0] / 2, y_cam + dy * bno_holes[1] / 2, floor_t])
                cylinder(d = 6, h = foot_t + bno_standoff);
            // gussets plate <-> foot
            for (dy = [-1, 1]) translate([xb - 8, y_cam + dy * (yw / 2 - 1.5) - 1.5, floor_t]) cube([8.01, 3, 12]);
        }
        // camera back-side cut-out (connector, cable)
        translate([xb - 1, y_cam - 13, z_oc - 13]) cube([sled_plate_t + 2, 26, 26]);
        // camera M2 inserts (from the standoff faces)
        for (dy = [-1, 1], dz = [-1, 1]) translate([x_plate_front + cam_standoff, y_cam + dy * cam_hole_pitch / 2, z_oc + dz * cam_hole_pitch / 2])
            rotate([0, 90, 0]) insert_hole(INSERT_M2);
        // foot M3 inserts from below
        for (x = foot_screw_x) translate([x, y_cam, floor_t]) mirror([0, 0, 1]) insert_hole(INSERT_M3);
        // BNO M2 inserts from the top of the standoffs
        for (dx = [-1, 1], dy = [-1, 1]) translate([xb - foot_len / 2 + dx * bno_holes[0] / 2, y_cam + dy * bno_holes[1] / 2, floor_t + foot_t + bno_standoff])
            insert_hole(INSERT_M2);
        // cable tie bar slot in the foot
        translate([xb - foot_len + 2, y_cam + yw / 2 - 7, floor_t - 1]) cube([3, 4, foot_t + 2]);
    }
}

// ---------------------------------------------------------------- 2D cut files
module window_2d(sz) rrect(sz, 2);
module gasket_2d(sz) difference() { rrect(sz - [0.6, 0.6], 2); rrect(sz - [0.6 + 6, 0.6 + 6], 1); }
module diffuser_2d() rrect(ir_ap + [2, 2], 1);
module led_plate_2d() {
    w = comp_w + 2.4 - 2 * CLR_SLIDE;
    h = inner_h - 0.5;
    difference() {
        square([w, h], center = true);
        // wire notches at the top corners
        for (s = [-1, 1]) translate([s * (w / 2 - 6), h / 2 - 1.5]) square([4, 3.01], center = true);
    }
}

// ---------------------------------------------------------------- reference parts
module ref_camera() {
    color([0.1, 0.45, 0.2]) translate([x_board_front - cam_board_t, y_cam - cam_board / 2, z_oc - cam_board / 2]) cube([cam_board_t, cam_board, cam_board]);
    color([0.12, 0.12, 0.12]) translate([x_board_front, y_cam - 7, z_oc - 7]) cube([6, 14, 14]);
    color([0.08, 0.08, 0.08]) translate([x_board_front + 6, y_cam, z_oc]) rotate([0, 90, 0]) cylinder(d = 14, h = lens_len - 6);
    color([0.3, 0.3, 0.5]) translate([x_lens_front - 0.2, y_cam, z_oc]) rotate([0, 90, 0]) cylinder(d = 10, h = 0.2);
}
module ref_bno() {
    xb = x_plate_front - sled_plate_t;
    color([0.15, 0.25, 0.75]) translate([xb - foot_len / 2 - bno[0] / 2, y_cam - bno[1] / 2, floor_t + foot_t + bno_standoff]) cube([bno[0], bno[1], 1.6]);
    color([0.05, 0.05, 0.05]) translate([xb - foot_len / 2 - 2.6, y_cam - 2.6, floor_t + foot_t + bno_standoff + 1.6]) cube([5.2, 5.2, 1.1]);
}
module ref_illuminator() {
    color([0.75, 0.77, 0.8]) translate([x_plate_al, y_ir, z_oc]) rotate([0, 90, 0]) linear_extrude(led_plate_t) rotate(90) led_plate_2d();
    for (s = [-1, 1]) {
        color([0.95, 0.95, 0.92]) translate([x_star_front - star_t, y_ir + s * led_pitch / 2, z_oc]) rotate([0, 90, 0]) cylinder(d = star_d, h = star_t);
        color([0.55, 0.1, 0.1]) translate([x_star_front, y_ir + s * led_pitch / 2 - 1.9, z_oc - 1.9]) cube([led_dome, 3.8, 3.8]);
    }
    color([0.35, 0.35, 0.38]) translate([x_plate_al - hs[2], y_ir - hs[0] / 2, z_oc - hs[1] / 2]) difference() {
        cube([hs[2], hs[0], hs[1]]);
        for (i = [0 : 8]) translate([-0.01, 2.2 + i * 4.3, -1]) cube([hs[2] - 2, 2.2, hs[1] + 2]);
    }
}
module ref_glands() for (g = [gland_cam_usb, gland_cam_imu, gland_ir]) color([0.1, 0.1, 0.1])
    translate([-9, g[0], g[1]]) rotate([0, 90, 0]) cylinder(d = 15, h = 9, $fn = 6);

// ---------------------------------------------------------------- assembly
module pod_layout_proxy() assembly(0, 1.0);   // used by helmet_layout.scad
// Opaque parts first: OpenCSG writes depth for translucent parts too, so anything drawn
// after a translucent part and behind it disappears from the preview.
module assembly(explode = 0, lid_alpha = 0.3) {
    color([0.35, 0.38, 0.3]) body_part();
    color([0.62, 0.55, 0.42]) sled_part();
    ref_camera();
    ref_bno();
    ref_illuminator();
    ref_glands();
    color([0.2, 0.22, 0.2]) translate([pod_d + 2 * explode, 0, 0]) bezel_part();
    for (w = [[y_cam, win_cam], [y_ir, win_ir]]) color([0.65, 0.8, 0.95, 0.35])
        translate([x_wi + explode, w[0], z_oc]) rotate([0, 90, 0]) linear_extrude(win_t) rotate(90) window_2d(w[1]);
    color([0.35, 0.38, 0.3, lid_alpha]) translate([0, 0, body_h + explode]) lid_part();
}

if (part == "body") body_part();
else if (part == "lid") lid_part();
else if (part == "bezel") rotate([0, -90, 0]) bezel_part();       // print face-down
else if (part == "sled") sled_part();
else if (part == "window_cam") window_2d(win_cam);
else if (part == "window_ir") window_2d(win_ir);
else if (part == "gasket") gasket_2d(win_cam);
else if (part == "diffuser") diffuser_2d();
else if (part == "led_plate") led_plate_2d();
else if (part == "assembly") assembly();
else if (part == "assembly_exploded") assembly(12);
else if (part == "hero") {
    assembly(0, 0.22);
    translate([0, -135, 0]) assembly(14, 0.55);
}
