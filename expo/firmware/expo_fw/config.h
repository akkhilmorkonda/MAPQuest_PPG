// MAPQuest expo firmware configuration (expo/README.md).
//
// Board: Seeed XIAO ESP32S3 (the rig), or an ESP32-WROOM-32D dev board
// (core esp32:esp32; pins below). Everything tunable lives here and is echoed
// by INFO.
//
// Forked from irb/firmware/irb_fw 0.2.1. Sensor, valve and safety settings
// are unchanged; the Wi-Fi section at the end is new.

#pragma once

#define FIRMWARE_VERSION "mapquest-expo-fw-0.1.0"

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

// ---------------------------------------------------------------------------
// Pins, per board. The build picks them from the board selected in the IDE
// (or arduino-cli's --fqbn):
//   ESP32 Dev Module (WROOM-32D)  esp32:esp32:esp32
//   Seeed XIAO ESP32S3            esp32:esp32:XIAO_ESP32S3
//
// The valve: MOSFET gate via 220 ohm, 10k gate-to-GND pull-down. The valve
// (Olimex AIR-E-VALVE-3V) is NORMALLY OPEN: HIGH energises it and CLOSES the
// cuff. While the chip is in reset the pin floats, the pull-down holds the
// MOSFET off, and the cuff vents.
// ---------------------------------------------------------------------------

#if defined(CONFIG_IDF_TARGET_ESP32)
// WROOM-32D. Avoid GPIO 0, 2, 5, 12, 15 (boot strapping), 6-11 (flash),
// 34-39 (input only).
#define BOARD_NAME "esp32-wroom-32d"
static const int PIN_SDA   = 21;
static const int PIN_SCL   = 22;
static const int PIN_VALVE = 25;
// The MPRLS sits on the main bus, upstream of the mux (the IRB wiring).
static const int8_t MUX_CH_PRESSURE = -1;
// The dev board's blue LED. GPIO2 is a strapping pin but only matters during
// reset, when it is not driven.
static const int  PIN_STATUS_LED   = 2;
static const bool STATUS_LED_ON_HIGH = true;

#elif defined(CONFIG_IDF_TARGET_ESP32S3)
// Seeed XIAO ESP32S3. Pads D0-D10 are GPIO 1, 2, 3, 4, 5, 6, 43, 44, 7, 8, 9.
// Avoid D2 (GPIO3, a boot strapping pin) and D6/D7 (GPIO43/44, the UART).
// I2C on D4/D5, the board's default and the wiring of the 2026-11-18 rig.
#define BOARD_NAME "xiao-esp32s3"
static const int PIN_SDA   = 5;    // D4
static const int PIN_SCL   = 6;    // D5
static const int PIN_VALVE = 4;    // D3: not a strapping pin, input (off) through reset
// The MPRLS is behind the mux on channel 7 (SD7/SC7; moved from 2 on
// 2026-11-21), the bands on channels 0 and 1. A mux fault then also loses
// pressure, which dumps the cuff.
static const int8_t MUX_CH_PRESSURE = 7;
// The XIAO's orange user LED, wired to 3V3: LOW lights it.
static const int  PIN_STATUS_LED   = 21;
static const bool STATUS_LED_ON_HIGH = false;

#else
#error "expo_fw has pins for the ESP32 Dev Module and the XIAO ESP32S3 only (config.h)"
#endif

// ---------------------------------------------------------------------------
// I2C
// ---------------------------------------------------------------------------

static const uint32_t I2C_CLOCK_HZ   = 400000;  // drop to 100000 if a band cable errors
// A 120-byte PPG chunk takes ~3 ms at 400 kHz, ~11 ms at 100 kHz: at 100 kHz
// raise this to 25, or full-FIFO reads time out.
static const uint16_t I2C_TIMEOUT_MS = 10;      // a stuck transaction fails fast

// TCA9548A mux. Each band (MAX30101 + BMI270) sits on its own channel; the
// MPRLS is on MUX_CH_PRESSURE (above: channel 7 on the XIAO).
static const uint8_t MUX_ADDR        = 0x70;
static const uint8_t MUX_CH_DISTAL   = 0;
static const uint8_t MUX_CH_PROXIMAL = 1;

// ---------------------------------------------------------------------------
// MAX30101 (same settings as firmware/mapquest_ppg, SPEC 3.1)
// ---------------------------------------------------------------------------

static const uint8_t  MAX30101_ADDR    = 0x57;
static const uint16_t SAMPLE_RATE_SPS  = 400;   // internal
static const uint8_t  SAMPLE_AVERAGING = 4;     // -> 100 Hz output
static const uint16_t PULSE_WIDTH_US   = 411;   // 18-bit
static const uint16_t ADC_RANGE_NA     = 4096;  // 15.63 pA/count
static const uint8_t  DEFAULT_IR_AMP   = 0x1F;  // 6.2 mA, register units of 0.2 mA
static const uint8_t  DEFAULT_RED_AMP  = 0x1F;
static const uint16_t DARK_SAMPLES     = 200;

// expo: automatic LED level (LEVEL, and part of R):
// sets each band's IR and red current so its light sits near LEVEL_TARGET of
// full scale, minus dark. Never at START: a change between the pre-roll and
// the hold would skew the "flow fully stopped" check. Fit the bands, then
// Refresh, then Start.
static const float    LEVEL_TARGET     = 0.50f;   // of full scale
static const float    LEVEL_TOLERANCE  = 0.15f;   // done when within +-15 % of full scale
static const float    LEVEL_RAIL       = 0.97f;   // at or above: saturated, cut hard
static const float    LEVEL_DIM        = 0.10f;   // below this at LEVEL_AMP_MAX: not on skin?
static const uint8_t  LEVEL_AMP_MIN    = 0x04;    // 0.8 mA
static const uint8_t  LEVEL_AMP_MAX    = 0x7F;    // 25.4 mA
static const uint8_t  LEVEL_SAMPLES    = 10;      // per measurement, 0.1 s
static const uint8_t  LEVEL_STEPS      = 5;

static const uint32_t PPG_DRAIN_MS     = 10;    // FIFO is 320 ms deep at 100 Hz

// ---------------------------------------------------------------------------
// BMI270. Initialised by the SparkFun BMI270 Arduino Library (it loads the
// 8 KB config blob); data is read raw from its FIFO (headerless frames of
// gyro x,y,z then accel x,y,z, int16 LE), so no sample is missed at 200 Hz.
// expo 2026-11-23: 200 Hz and +-2 g (was 50 Hz, +-4 g) to try sensing the
// pulse mechanically; the result's "arm moved" check uses the gyro only.
// ---------------------------------------------------------------------------

static const uint8_t  IMU_ADDR       = 0x68;  // SDO low
static const uint8_t  IMU_CHIP_ID    = 0x24;
static const uint8_t  IMU_REG_FIFO_LEN  = 0x24;  // 14-bit byte count, LE
static const uint8_t  IMU_REG_FIFO_DATA = 0x26;
static const uint8_t  IMU_REG_FIFO_CFG1 = 0x49;  // bit 7 gyr, bit 6 acc, bit 4 header
static const uint8_t  IMU_REG_CMD       = 0x7E;  // 0xB0: flush the FIFO
static const uint8_t  IMU_FRAME_BYTES   = 12;
static const uint16_t IMU_ODR_HZ        = 200;   // 50, 100, 200 or 400
static const uint16_t IMU_ACC_RANGE_G   = 2;     // 2, 4, 8 or 16 -> 16384 LSB/g at 2
static const uint16_t IMU_GYR_RANGE_DPS = 500;   // -> 65.536 LSB/(deg/s)
static const uint32_t IMU_POLL_MS       = 10;
static const uint16_t IMU_FIFO_MAX_FRAMES = 160; // the BMI270 holds ~170 (2 KB): more is an overflow

// expo: a pressure sensor missing in IDLE (missed at boot, or unplugged) is
// probed this often and brought up, and the cuff zeroed, when it answers.
static const uint32_t PRESSURE_RETRY_MS = 2000;

// expo: a band plugged in while the board runs (or missed at boot) is found
// in IDLE without R: each missing PPG, and each missing IMU on a working
// band, gets one address probe this often (silent when absent). When it
// answers it is brought up and, for a PPG, its LED level set. Not while
// streaming on USB serial (a bench recording must not get a hole; the bench
// GUI's Refresh does it there).
static const uint32_t BAND_RETRY_MS = 3000;

// expo: the result (host/mapquest, refill onset) reads the DISTAL band only.
// START needs the distal PPG; the proximal band is shown when present but
// neither blocks START nor dumps the cuff if it drops out (a warning only).
// Set true when a model that uses the proximal band is in use.
static const bool REQUIRE_PROXIMAL_BAND = false;

// Bench override for the MPRLS memory-integrity flag (status bit 0x04: the
// sensor's factory calibration fails its own check, so its readings can't be
// trusted). false: the flag refuses START and dumps the cuff. true: it only
// logs a warning on each START; every other limit still applies. Keep false.
// It was true 2026-11-18..19 for a faulty sensor (status 0x44), since replaced.
static const bool IGNORE_PRESSURE_INTEGRITY = false;

// Consecutive I2C failures before a sensor is declared lost. One glitch on a
// long band cable should not end a run; a dead sensor should.
static const uint8_t I2C_FAIL_LIMIT = 3;

// ---------------------------------------------------------------------------
// MPRLS cuff pressure, Adafruit 3965 (Honeywell MPRLS0025PA00001A)
// ---------------------------------------------------------------------------

static const uint8_t  MPRLS_ADDR        = 0x18;
static const uint32_t PRESSURE_RATE_HZ  = 100;      // one conversion per PPG sample
static const uint32_t MPRLS_TIMEOUT_US  = 20000;    // datasheet max conversion ~5 ms
static const uint32_t MPRLS_OUTPUT_MIN  = 1677722;  // transfer function A: 10 % of 2^24
static const uint32_t MPRLS_OUTPUT_MAX  = 15099494; // 90 % of 2^24
static const float    MPRLS_RANGE_PSI   = 25.0f;
static const float    MMHG_PER_PSI      = 51.71493f;
static const uint16_t ZERO_SAMPLES      = 50;

// ---------------------------------------------------------------------------
// Valve control. Defaults from valve_code/cuff_deflation_test.ino, already
// tuned for this cuff and valve. SET changes them in IDLE.
// ---------------------------------------------------------------------------

static const uint32_t VALVE_WINDOW_MS    = 100;
static const uint32_t VALVE_MIN_PULSE_MS = 5;
static const float    DEFAULT_RATE_MMHG_S = 2.0f;
static const float    DEFAULT_KP          = 0.08f;
static const float    DEFAULT_KI          = 0.04f;
static const float    DEFAULT_KD          = 0.0f;
static const float    DEFAULT_U0          = 0.04f;
static const float    DEFAULT_HOLD_S      = 10.0f;
static const float    DEFAULT_P_STOP      = 40.0f;
static const float    DEFAULT_P_RUN_MIN   = 150.0f;   // expo profile (DESIGN 4.2): pump to 160, Run at 150

static const uint8_t  RATE_HISTORY        = 10;      // windows in the 1 s slope

// ---------------------------------------------------------------------------
// Hard safety limits. SET may tighten these, never loosen them.
// ---------------------------------------------------------------------------

static const float    P_MAX_HARD_MMHG      = 280.0f;
static const float    HOLD_TIMEOUT_HARD_S  = 120.0f;
static const float    HOLD_TIMEOUT_ABOVE   = 20.0f;   // the hold clock only counts above this
static const float    VENTED_BELOW_MMHG    = 10.0f;
static const uint32_t DUMP_SETTLE_MAX_MS   = 15000;   // leave DUMP even if pressure is unknown

// expo: the valve is held CLOSED in IDLE once the cuff is empty (it is open
// only to deflate and to dump). It opens in IDLE whenever the pressure sensor
// is missing, unzeroed, stale or flagged, or the cuff holds more than
// IDLE_VENT_ABOVE_MMHG outside a run, and closes again only after reading
// below VENTED_BELOW_MMHG for IDLE_CLOSE_SETTLE_MS. A normally-open valve
// held closed is energised the whole time the board idles.
static const float    IDLE_VENT_ABOVE_MMHG = 20.0f;
static const uint32_t IDLE_CLOSE_SETTLE_MS = 3000;
static const uint32_t PRESSURE_DROPOUT_MS  = 250;
static const uint32_t PPG_DROPOUT_MS       = 500;
static const uint32_t IMU_DROPOUT_MS       = 500;

// Task watchdog: a hung loop resets the chip, which opens the valve.
static const uint32_t WDT_TIMEOUT_MS = 1000;

// ---------------------------------------------------------------------------
// Host link. The WROOM's USB-serial bridge makes the baud rate real; the full
// stream needs ~12 kB/s, more than 115200 carries (DESIGN 4.4).
// ---------------------------------------------------------------------------

static const uint32_t SERIAL_BAUD = 921600;

// ---------------------------------------------------------------------------
// Wi-Fi (expo). The board is its own WPA2 access point; the iPad and the
// presentation laptop join it and open http://192.168.4.1/. Data goes out
// over a WebSocket at /ws as the same text lines the serial port carries.
// ---------------------------------------------------------------------------

// Pick a quiet channel at the venue (1, 6 or 11 overlap least).
static const uint8_t  WIFI_CHANNEL       = 6;
// Stations allowed to join: the controlling iPad, the laptop, a spare.
static const uint8_t  WIFI_MAX_STATIONS  = 4;
// Transmit power in units of 0.25 dBm (34 = 8.5 dBm, ~7 mW). Plenty for a
// booth, and smaller current spikes on the 3V3 rail the sensors and valve
// share. Raise toward 78 (19.5 dBm) only if the range is too short.
static const int8_t   WIFI_TX_POWER_QDBM = 34;

// WebSocket clients (iPad, laptop) at once. Same as the station limit.
static const uint8_t  WS_MAX_CLIENTS     = 4;
// Lines are batched into one WebSocket frame this often.
static const uint32_t WS_FLUSH_MS        = 40;
// Largest frame sent; a batch is ~500 B at the full stream rate.
static const size_t   WS_FRAME_BYTES     = 2048;
// Outgoing buffer between the sensor loop and the sender task: ~1.3 s of the
// full stream. When it is full, whole lines are dropped and counted.
static const size_t   WS_RING_BYTES      = 16384;
// A Wi-Fi send that blocks this long closes that client (seconds).
static const uint16_t WS_SEND_TIMEOUT_S  = 1;
// Commands waiting for the main loop.
static const uint8_t  WS_CMD_QUEUE       = 16;

// The controlling device must be heard from within this window (the page
// sends PING every 250 ms). If it is lost during INFLATE or HOLD of a run it
// started, the cuff is dumped; during DEFLATE the run finishes on the board.
static const uint32_t CONTROLLER_TIMEOUT_MS = 2000;

// Compact status line (ST) for the iPad, this often.
static const uint32_t STATUS_PERIOD_MS = 500;

// A main-loop pass longer than this is reported (slow_loop event). The Wi-Fi
// stack runs on the other core and must never stall the sensor loop. Passes
// that run a blocking bench command (ZERO, DARK, ...) are not counted.
static const uint32_t LOOP_WARN_US = 20000;

// The status LED (pins above): lit = valve closed; BLINK flashes it so the
// iPad can tell which board it is talking to.
