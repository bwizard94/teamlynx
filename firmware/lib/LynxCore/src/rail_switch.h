#pragma once

// Debounce + gesture recognition for the weapon-rail momentary switch.
// Hardware independent: feed it the raw (active = pressed) level and a microsecond clock.
//
// Gestures:
//   SINGLE  press-release, then no second press within double_window_ms of the release
//   DOUBLE  press-release-press-release, second press within double_window_ms
//   LONG    first press held >= long_ms (fires while still held, for immediate feedback)
// PRESS / RELEASE are reported for every debounced edge (drives the "switch held" telemetry flag).
//
// Every event carries press_t_us: the raw edge time of the gesture's FIRST press. That is the
// instant the operator aimed, so the host raycasts with the head pose at press_t_us rather than
// at the (up to double_window_ms later) moment SINGLE is decided.

#include <stddef.h>
#include <stdint.h>

namespace lynx {

struct RailConfig {
  uint32_t debounce_ms = 20;
  uint32_t double_window_ms = 300;
  uint32_t long_ms = 700;
};

enum class RailEvent : uint8_t {
  None = 0,
  Press = 1,
  Release = 2,
  Single = 3,
  Double = 4,
  Long = 5,
};

struct RailEventRecord {
  RailEvent type = RailEvent::None;
  uint64_t t_us = 0;        // when the event was decided
  uint64_t press_t_us = 0;  // raw edge time of the gesture's first press
  uint8_t clicks = 0;       // 1 single/long, 2 double, 0 for edges
  uint16_t hold_ms = 0;     // duration of the most recent press (so far, for LONG)
};

class RailSwitch {
 public:
  static constexpr size_t kMaxEventsPerUpdate = 3;

  explicit RailSwitch(const RailConfig& cfg = RailConfig()) : cfg_(cfg) {}

  // raw_pressed: instantaneous switch level (true = closed). Call at >= 200 Hz.
  // Writes up to kMaxEventsPerUpdate events to out and returns how many were written.
  size_t update(bool raw_pressed, uint64_t t_us, RailEventRecord* out, size_t cap);

  bool pressed() const { return stable_; }
  const RailConfig& config() const { return cfg_; }

 private:
  enum class Gesture : uint8_t { Idle, Down1, Wait2, Down2, LongHeld };

  void emit(RailEvent type, uint64_t t_us, uint8_t clicks, uint16_t hold_ms);
  static uint16_t ms_between(uint64_t a_us, uint64_t b_us);

  RailConfig cfg_;
  bool stable_ = false;
  bool raw_last_ = false;
  uint64_t raw_edge_t_us_ = 0;
  Gesture g_ = Gesture::Idle;
  uint64_t gesture_press_t_us_ = 0;
  uint64_t last_press_t_us_ = 0;
  uint64_t last_release_t_us_ = 0;
  uint16_t first_hold_ms_ = 0;

  RailEventRecord* out_ = nullptr;
  size_t cap_ = 0;
  size_t n_ = 0;
};

}  // namespace lynx
