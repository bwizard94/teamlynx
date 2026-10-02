// TeamLynx Phase 4: whole-helmet layout (visual check of placement and cable runs).
// Head frame: X forward, Y left, Z up, origin at the head+helmet CG. The part positions here
// are the ones used by hardware/scripts/mass_report.py (POS table); keep the two in step.
// The helmet, shroud, mount and goggles are phantoms, not printable parts.
use <sensor_pod.scad>
use <esp32_enclosure.scad>
use <rear_bracket.scad>
include <lynx_common.scad>

part = "layout";

module phantom_helmet() {
    color([0.45, 0.42, 0.33, 1]) intersection() {
        translate([-5, 0, -5]) difference() {
            scale([118, 102, 118]) sphere(r = 1, $fn = 96);
            scale([110, 94, 110]) sphere(r = 1, $fn = 96);
        }
        // high-cut edge: higher at the brow than at the nape
        rotate([0, -14, 0]) translate([-300, -300, 18]) cube([600, 600, 300]);
        // ear cut-outs
        difference() { cube(600, center = true); for (s = [-1, 1]) translate([5, s * 100, 5]) rotate([90, 0, 0]) cylinder(r = 52, h = 40, center = true); }
    }
    // side rails (ARC-type stand-ins)
    for (s = [-1, 1]) color([0.2, 0.2, 0.2]) translate([-35, s * 96 - (s > 0 ? 0 : 8), 22]) cube([110, 8, 22]);
}

module phantom_mount() {
    color([0.25, 0.25, 0.25]) {
        translate([98, -22, 48]) rotate([0, -20, 0]) cube([10, 44, 36]);       // shroud
        translate([108, -18, 58]) cube([30, 36, 22]);                          // mount arm
        translate([118, -16, 66]) cube([22, 32, 14]);                          // receiver
    }
}

module phantom_goggle() {
    color([0.15, 0.15, 0.15]) translate([92, 0, -12]) difference() {
        scale([16, 80, 30]) sphere(r = 1, $fn = 64);
        scale([13, 77, 27]) sphere(r = 1, $fn = 64);
        translate([-40, -100, -100]) cube([40, 200, 200]);
    }
    color([0.6, 0.75, 0.9, 0.35]) translate([104, 0, -12]) scale([3, 78, 28]) sphere(r = 1, $fn = 64);
    color([0.1, 0.1, 0.12]) translate([86, -38, -14]) cube([12, 14, 12]);          // micro-OLED engine (right eye)
}

module layout() {
    // pod: origin = rear-bottom-centre of its body; CG ~ +34 mm X, +24 mm Z from there
    translate([116, 0, 22]) pod_assembly_for_layout();
    // ESP32 box on the left rail: local X = head X, local Z = head +Y, local Y = head -Z
    translate([-34, 104, 62]) rotate([-90, 0, 0]) esp32_for_layout();
    // rear cassette: local X = head -Y, local Y = head +Z, local Z = head -X
    translate([-114, 0, 40]) multmatrix([[0, 0, -1, 0], [-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]]) rear_for_layout();
    phantom_helmet();
    phantom_mount();
    phantom_goggle();
}

module pod_assembly_for_layout() { pod_layout_proxy(); }
module esp32_for_layout() { esp32_layout_proxy(); }
module rear_for_layout() { rear_layout_proxy(); }

if (part == "layout") layout();
