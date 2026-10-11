// MAPQuest firmware configuration.
//
// Everything tunable lives here and is echoed into the serial metadata header,
// so every recording is self-describing and no host-side constant has to be
// kept in sync by hand. See docs/SPEC.md section 3.1.

#pragma once

#define FIRMWARE_VERSION "mapquest-fw-0.3"

// ---------------------------------------------------------------------------
// MAX30101 (SPEC 3.1)
// ---------------------------------------------------------------------------

// SpO2 mode drives exactly Red + IR, which is what the two-wavelength
// Beer-Lambert solve in SPEC 5.1 step 6 needs. Green is not used: it has no
// place in a red/IR extinction-coefficient pair, and at 0.3-0.8 mm penetration
// it would largely miss the forearm filling we are trying to detect.
static const uint16_t SAMPLE_RATE_SPS  = 400;  // internal; 50/100/200/400/800/1000/1600/3200
static const uint8_t  SAMPLE_AVERAGING = 4;    // 1/2/4/8/16/32 -> output = SPS / this = 100 Hz
static const uint16_t PULSE_WIDTH_US   = 411;  // 69/118/215/411 -> 15/16/17/18 bit
static const uint16_t ADC_RANGE_NA     = 4096; // 2048/4096/8192/16384 -> 15.63 pA/count

// Datasheet Table 11 (SpO2 mode): at 411 us the ceiling is 400 sps. We spend
// that ceiling on 4x hardware averaging rather than on an output rate we do
// not need -- 100 Hz out, with the noise reduction that averaging buys.

// LED drive, register units of 0.2 mA. Tune so IR DC lands in 105k-157k counts
// (40-60% of the 262143 full scale) on a forearm -- see SPEC 3.1. Adjustable at
// runtime with IRAMP / REDAMP, but locked while streaming.
static const uint8_t DEFAULT_IR_AMP  = 0x1F;  // 6.2 mA
static const uint8_t DEFAULT_RED_AMP = 0x1F;  // 6.2 mA

// ---------------------------------------------------------------------------
// Cuff pressure -- Adafruit MPRLS (Honeywell MPRLS0025PA00001A), D41
// ---------------------------------------------------------------------------

// Shares the I2C bus with the MAX30101 (0x18 vs 0x57). Replaces the HX710B +
// MPS20N0040D of SPEC 3.2, which was never fitted. Detected at boot and on R:
// when found, S lines carry p_idx,p_raw,p_status; when not, PPG only.
#define PRESSURE_ENABLED 1

static const uint8_t  MPRLS_ADDR        = 0x18;
static const uint32_t PRESSURE_RATE_HZ  = 100;     // one conversion per PPG sample
static const uint32_t MPRLS_TIMEOUT_US  = 20000;   // datasheet max conversion ~5 ms

// Transfer function A, 0-25 psi absolute. Echoed in the header; the host
// converts counts to mmHg and zeroes on the room (host/mapquest/mprls.py).
static const uint32_t MPRLS_OUTPUT_MIN  = 1677722;   // 10 % of 2^24
static const uint32_t MPRLS_OUTPUT_MAX  = 15099494;  // 90 % of 2^24

// ---------------------------------------------------------------------------
// Host link
// ---------------------------------------------------------------------------

static const uint32_t SERIAL_BAUD = 115200;  // SPEC 3.4; ignored by native USB CDC

// Samples averaged for the DARK offset measurement (SPEC 3.4).
static const uint16_t DARK_SAMPLES = 200;
