// Session state and settings (irb/DESIGN.md sections 2 and 4).
//
// In a header for the same reason as band.h: the Arduino builder puts its
// generated prototypes above any type defined in the .ino.

#pragma once

#include <stdint.h>

// IDLE: valve closed once the cuff has read empty for 3 s, open otherwise
// (expo idleValve). INFLATE: valve closed, operator pumps. HOLD: valve closed
// for hold_s. DEFLATE: PI-controlled bleed. DUMP: valve open until the cuff
// reads vented, then IDLE. expo/CHECKS.md has the checks in each.
enum SessState : uint8_t { ST_IDLE, ST_INFLATE, ST_HOLD, ST_DEFLATE, ST_DUMP };

struct Settings {
  float rate;            // mmHg/s, deflation target (positive)
  float kp, ki, kd;      // duty per mmHg, per mmHg*s, per mmHg/s
  float u0;              // starting integrator (feed-forward duty)
  float hold_s;          // HOLD length
  float p_stop;          // DEFLATE ends and dumps at or below this
  float p_run_min;       // RUN accepted only at or above this
  float p_max;           // over-pressure dump; never above P_MAX_HARD_MMHG
  float hold_timeout_s;  // valve closed with p > 20 for longer than this; never above the hard limit
};
