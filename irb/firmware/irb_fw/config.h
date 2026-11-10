// MAPQuest IRB firmware configuration (irb/DESIGN.md).
//
// Board: ESP32-WROOM-32D dev board, Arduino board "ESP32 Dev Module"
// (core esp32:esp32). Everything tunable lives here and is echoed by INFO.

#pragma once

#define FIRMWARE_VERSION "mapquest-irb-fw-0.2.1"

// ---------------------------------------------------------------------------
// Pins. Avoid on the WROOM: GPIO 0, 2, 5, 12, 15 (boot strapping), 6-11
// (flash), 34-39 (input only).
// ---------------------------------------------------------------------------

static const int PIN_SDA   = 21;
static const int PIN_SCL   = 22;
// MOSFET gate via 220 ohm, 10k gate-to-GND pull-down. The valve (Olimex
// AIR-E-VALVE-3V) is NORMALLY OPEN: HIGH energises it and CLOSES the cuff.
// While the ESP32 is in reset the pin floats, the pull-down holds the MOSFET
// off, and the cuff vents.
static const int PIN_VALVE = 25;

// ---------------------------------------------------------------------------
// I2C
// ---------------------------------------------------------------------------

static const uint32_t I2C_CLOCK_HZ   = 400000;  // drop to 100000 if a band cable errors
static const uint16_t I2C_TIMEOUT_MS = 10;      // a stuck transaction fails fast

// TCA9548A mux. Each band (MAX30101 + BMI270) sits on its own channel; the
// MPRLS stays on the main bus, upstream of the mux.
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

static const uint32_t PPG_DRAIN_MS     = 10;    // FIFO is 320 ms deep at 100 Hz

// ---------------------------------------------------------------------------
// BMI270. Initialised by the SparkFun BMI270 Arduino Library (it loads the
// 8 KB config blob); data is read raw from the registers.
// ---------------------------------------------------------------------------

static const uint8_t  IMU_ADDR       = 0x68;  // SDO low
static const uint8_t  IMU_CHIP_ID    = 0x24;
static const uint8_t  IMU_REG_STATUS = 0x03;  // bit 7: new accel data
static const uint8_t  IMU_REG_DATA   = 0x0C;  // acc x,y,z then gyr x,y,z, int16 LE
static const uint16_t IMU_ODR_HZ        = 50;
static const uint16_t IMU_ACC_RANGE_G   = 4;     // -> 8192 LSB/g
static const uint16_t IMU_GYR_RANGE_DPS = 500;   // -> 65.536 LSB/(deg/s)
static const uint32_t IMU_POLL_MS       = 10;

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
static const float    DEFAULT_P_RUN_MIN   = 140.0f;   // the host sets it per subject

static const uint8_t  RATE_HISTORY        = 10;      // windows in the 1 s slope

// ---------------------------------------------------------------------------
// Hard safety limits. SET may tighten these, never loosen them.
// ---------------------------------------------------------------------------

static const float    P_MAX_HARD_MMHG      = 280.0f;
static const float    HOLD_TIMEOUT_HARD_S  = 120.0f;
static const float    HOLD_TIMEOUT_ABOVE   = 20.0f;   // the hold clock only counts above this
static const float    VENTED_BELOW_MMHG    = 10.0f;
static const uint32_t DUMP_SETTLE_MAX_MS   = 15000;   // leave DUMP even if pressure is unknown
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
