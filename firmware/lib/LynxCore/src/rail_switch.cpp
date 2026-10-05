#include "rail_switch.h"

namespace lynx {

uint16_t RailSwitch::ms_between(uint64_t a_us, uint64_t b_us) {
  if (b_us <= a_us) {
    return 0;
  }
  const uint64_t ms = (b_us - a_us) / 1000u;
  return ms > 0xFFFF ? 0xFFFF : static_cast<uint16_t>(ms);
}

void RailSwitch::emit(RailEvent type, uint64_t t_us, uint8_t clicks, uint16_t hold_ms) {
  if (n_ >= cap_) {
    return;
  }
  RailEventRecord& e = out_[n_++];
  e.type = type;
  e.t_us = t_us;
  e.press_t_us = gesture_press_t_us_;
  e.clicks = clicks;
  e.hold_ms = hold_ms;
}

size_t RailSwitch::update(bool raw_pressed, uint64_t t_us, RailEventRecord* out, size_t cap) {
  out_ = out;
  cap_ = cap;
  n_ = 0;

  // Integrating debounce: accept a new level once the raw input has held it for debounce_ms.
  // The accepted edge is time-stamped at the last raw transition (end of contact bounce), not at
  // acceptance, so debounce latency does not shift the aim time.
  if (raw_pressed != raw_last_) {
    raw_last_ = raw_pressed;
    raw_edge_t_us_ = t_us;
  }
  bool pressed_edge = false;
  bool released_edge = false;
  if (raw_pressed != stable_ && t_us - raw_edge_t_us_ >= static_cast<uint64_t>(cfg_.debounce_ms) * 1000u) {
    stable_ = raw_pressed;
    pressed_edge = stable_;
    released_edge = !stable_;
  }
  const uint64_t edge_t = raw_edge_t_us_;
  const uint64_t long_us = static_cast<uint64_t>(cfg_.long_ms) * 1000u;
  const uint64_t window_us = static_cast<uint64_t>(cfg_.double_window_ms) * 1000u;

  switch (g_) {
    case Gesture::Idle:
      if (pressed_edge) {
        gesture_press_t_us_ = edge_t;
        last_press_t_us_ = edge_t;
        emit(RailEvent::Press, t_us, 0, 0);
        g_ = Gesture::Down1;
      }
      break;

    case Gesture::Down1:
      if (released_edge) {
        first_hold_ms_ = ms_between(last_press_t_us_, edge_t);
        last_release_t_us_ = edge_t;
        emit(RailEvent::Release, t_us, 0, first_hold_ms_);
        g_ = Gesture::Wait2;
      } else if (t_us - last_press_t_us_ >= long_us) {
        emit(RailEvent::Long, t_us, 1, ms_between(last_press_t_us_, t_us));
        g_ = Gesture::LongHeld;
      }
      break;

    case Gesture::Wait2:
      if (pressed_edge) {
        last_press_t_us_ = edge_t;
        emit(RailEvent::Press, t_us, 0, 0);
        g_ = Gesture::Down2;
      } else if (t_us - last_release_t_us_ > window_us) {
        emit(RailEvent::Single, t_us, 1, first_hold_ms_);
        g_ = Gesture::Idle;
      }
      break;

    case Gesture::Down2:
      if (released_edge) {
        const uint16_t hold = ms_between(last_press_t_us_, edge_t);
        emit(RailEvent::Release, t_us, 0, hold);
        emit(RailEvent::Double, t_us, 2, hold);
        g_ = Gesture::Idle;
      }
      break;

    case Gesture::LongHeld:
      if (released_edge) {
        emit(RailEvent::Release, t_us, 0, ms_between(last_press_t_us_, edge_t));
        g_ = Gesture::Idle;
      }
      break;
  }
  return n_;
}

}  // namespace lynx
