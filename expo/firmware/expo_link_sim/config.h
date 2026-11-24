// MAPQuest expo link simulator: configuration (expo/README.md).
//
// Board: ESP32-WROOM-32D dev board, Arduino board "ESP32 Dev Module"
// (core esp32:esp32). NOTHING needs to be wired: the sensors and the cuff are
// simulated on the chip, so the Wi-Fi link can be tested on its own.

#pragma once

#include <stdint.h>
#include <stddef.h>

#define FIRMWARE_VERSION "mapquest-expo-linksim-0.1.0"

// The network name and password live in secrets.h, which git ignores.
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy secrets.example.h to secrets.h and set the Wi-Fi password (expo/README.md)"
#endif

static_assert(sizeof(WIFI_PASSWORD) - 1 >= 8 && sizeof(WIFI_PASSWORD) - 1 <= 63,
              "WPA2 needs a WIFI_PASSWORD of 8 to 63 characters");
static_assert(sizeof(WIFI_SSID) - 1 >= 1 && sizeof(WIFI_SSID) - 1 <= 32,
              "WIFI_SSID must be 1 to 32 characters");

static const uint32_t SERIAL_BAUD = 921600;

// ---------------------------------------------------------------------------
// Pins. The real valve pin is driven LOW (vented) and never touched, in case
// the board is still wired to the cuff. The LED shows the SIMULATED valve:
// lit = closed. Most WROOM dev boards have a blue LED on GPIO2; set -1 if
// yours has none.
// ---------------------------------------------------------------------------

static const int PIN_REAL_VALVE = 25;
static const int PIN_SIM_LED    = 2;

// ---------------------------------------------------------------------------
// Wi-Fi. The board is its own WPA2 access point; the iPad and the laptop join
// it and open http://192.168.4.1/. Data goes out over a WebSocket at /ws as
// the same text lines the IRB firmware sends over serial.
// ---------------------------------------------------------------------------

// Pick a quiet channel at the venue (1, 6 or 11 overlap least).
static const uint8_t  WIFI_CHANNEL       = 6;
// Stations allowed to join: the controlling iPad, the laptop, spares.
static const uint8_t  WIFI_MAX_STATIONS  = 4;
// Transmit power in units of 0.25 dBm (34 = 8.5 dBm, ~7 mW). Plenty for a
// booth, and smaller current spikes on the 3V3 rail the real sensors and
// valve will share. Raise toward 78 (19.5 dBm) only if the range is short.
static const int8_t   WIFI_TX_POWER_QDBM = 34;

// WebSocket clients at once. Same as the station limit.
static const uint8_t  WS_MAX_CLIENTS     = 4;
// Lines are batched into one WebSocket frame this often.
static const uint32_t WS_FLUSH_MS        = 40;
// Largest frame sent; a batch is ~500 B at the full stream rate.
static const size_t   WS_FRAME_BYTES     = 2048;
// Buffer between the main loop and the sender task: ~1.3 s of the full
// stream. When it is full, whole lines are dropped and counted.
static const size_t   WS_RING_BYTES      = 16384;
// A Wi-Fi send that blocks this long closes that client (seconds).
static const uint16_t WS_SEND_TIMEOUT_S  = 1;
// Commands waiting for the main loop.
static const uint8_t  WS_CMD_QUEUE       = 16;

// The controlling device must be heard from within this window (the page
// sends PING every 250 ms). Lost during INFLATE or HOLD of a run it started:
// the cuff is dumped. Lost during DEFLATE: the run finishes on the board.
static const uint32_t CONTROLLER_TIMEOUT_MS = 2000;

// Compact status line (ST) for the iPad, this often.
static const uint32_t STATUS_PERIOD_MS = 500;

// A main-loop pass longer than this is reported (the Wi-Fi stack must never
// stall the sensor loop; on the real board 10 ms is one PPG sample).
static const uint32_t LOOP_WARN_US = 20000;

// ---------------------------------------------------------------------------
// Simulation. Rates and line formats match the IRB firmware, so the stream
// is the real system's load. Session rules match too (START, RUN, DUMP,
// SET p_run_min), but the bulb is simulated: after START the cuff pumps
// itself up to SIM_PUMP_TARGET.
// ---------------------------------------------------------------------------

static const uint32_t SIM_PPG_PERIOD_US  = 10000;   // 100 Hz, distal (S) and proximal (SP)
static const uint32_t SIM_IMU_PERIOD_US  = 20000;   // 50 Hz per band (M)
static const uint32_t SIM_CTRL_PERIOD_MS = 100;     // 10 Hz controller window (C)

static const float SIM_PUMP_TARGET   = 160.0f;      // mmHg, where the simulated bulb stops
static const float SIM_PUMP_RATE     = 40.0f;       // mmHg/s while pumping
static const float SIM_VENT_TAU_S    = 0.5f;        // open valve: exponential vent
static const float SIM_SYSTOLIC      = 120.0f;      // simulated arm: refill starts below this
static const float SIM_HEART_HZ      = 1.2f;

// Session settings (as the IRB defaults) and the limits SET enforces.
static const float DEFAULT_RATE_MMHG_S = 2.0f;
static const float DEFAULT_HOLD_S      = 10.0f;
static const float DEFAULT_P_STOP      = 40.0f;
static const float DEFAULT_P_RUN_MIN   = 150.0f;   // expo profile (DESIGN 4.2): pump to 160, Run at 150
static const float P_MAX_MMHG          = 280.0f;
static const float VENTED_BELOW_MMHG   = 10.0f;
static const float HOLD_TIMEOUT_S      = 120.0f;
