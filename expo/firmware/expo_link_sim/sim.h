// Simulated session state and settings.
//
// In a header because the Arduino builder puts its generated prototypes above
// any type defined in the .ino.

#pragma once

#include <stdint.h>

// Same states and meaning as irb/firmware/irb_fw/session.h.
enum SessState : uint8_t { ST_IDLE, ST_INFLATE, ST_HOLD, ST_DEFLATE, ST_DUMP };

struct Settings {
  float rate;            // mmHg/s, deflation target (positive)
  float hold_s;          // HOLD length
  float p_stop;          // DEFLATE ends and dumps at or below this
  float p_run_min;       // RUN accepted only at or above this
  float p_max;           // over-pressure dump
  float hold_timeout_s;  // valve closed with p > 20 for longer than this
};
