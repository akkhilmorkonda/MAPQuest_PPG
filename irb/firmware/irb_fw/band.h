// One sensor band: a MAX30101 + a BMI270 behind one TCA9548A channel.
//
// Lives in its own header because the Arduino builder generates prototypes
// for every function in the .ino and places them above the first line of
// code; a struct defined in the .ino would come after them.

#pragma once

#include <stdint.h>

struct Band {
  const char *name;
  char        tag;              // 'D' or 'P', as used in commands and data lines
  uint8_t     ch;               // mux channel

  // MAX30101
  bool        ppg_ok;
  uint8_t     part_id;          // as actually read, never the expected constant
  uint8_t     rev_id;
  uint8_t     ir_amp;
  uint8_t     red_amp;
  bool        dark_valid;
  uint32_t    dark_ir;
  uint32_t    dark_red;
  uint32_t    ppg_idx;          // this band's own sample counter (its own oscillator)
  uint32_t    ppg_last_ms;      // millis() of the last sample drained
  uint32_t    ppg_next_ms;      // next scheduled FIFO drain
  uint8_t     ppg_fails;        // consecutive I2C failures

  // BMI270
  bool        imu_ok;           // chip ID read back correctly
  uint8_t     imu_chip_id;      // as actually read
  bool        imu_configured;   // config blob loaded, data enabled
  uint32_t    imu_idx;
  uint32_t    imu_last_ms;
  uint32_t    imu_next_ms;
  uint8_t     imu_fails;
  bool        imu_warned;       // dropout warning already sent
};
