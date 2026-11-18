// MAPQuest IRB firmware (irb/DESIGN.md)
//
// Board   : ESP32-WROOM-32D dev board, Arduino board "ESP32 Dev Module"
// Sensors : TCA9548A mux 0x70
//             ch0 distal band   : MAX30101 0x57 + BMI270 0x68
//             ch1 proximal band : MAX30101 0x57 + BMI270 0x68
//           MPRLS 0x18 on the main bus (not behind the mux)
// Valve   : Olimex AIR-E-VALVE-3V, normally open, GPIO25 -> MOSFET
// Library : SparkFun BMI270 Arduino Library (Library Manager)
//
// Phase 2: streaming, the session state machine, PI deflation control and
// the safety layer. NOT YET RUN ON HARDWARE: compile-checked only.
//
// Serial 921600 baud, newline-terminated commands (DESIGN 4.4):
//   INFO                    settings, limits and every sensor's status
//   STREAM 1|0              data lines on / off
//   START | RUN | DUMP      session: close valve / hold then deflate / vent now
//   ZERO                    pressure zero (IDLE, not streaming, cuff vented)
//   DARK D|P                LED dark offsets for one band (IDLE, not streaming)
//   AMP D|P IR|RED <0-255>  LED current (IDLE)
//   SET <key> <value>       rate kp ki kd u0 hold_s p_stop p_run_min p_max hold_timeout_s
//                           (IDLE; p_run_min also in INFLATE)
//   R                       re-probe every sensor (IDLE, not streaming)
//   SCAN, PPG D|P, P        bring-up checks
//   VALVE TEST, TEST HANG   valve click test; watchdog test (IDLE, not streaming)
//
// Device -> host lines: '#' metadata, S distal + pressure, SP proximal,
// M IMU, C controller (10 Hz), EV events, E errors. Columns are in INFO.

#include <WiFi.h>
#include <Wire.h>
#include <driver/gpio.h>
#include <esp_bt.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <esp_timer.h>
#include <SparkFun_BMI270_Arduino_Library.h>

#include "band.h"
#include "config.h"
#include "session.h"

static bool g_streaming = false;

// ===========================================================================
// Events. EV lines go out whether or not data is streaming: a dump must
// always be visible.
// ===========================================================================

static void event(const char *name, const char *detail) {
  Out.printf("EV,%lu,%s,%s\n", (unsigned long)micros(), name, detail ? detail : "");
}

// ===========================================================================
// Valve. NORMALLY OPEN: low = de-energised = open = cuff vents.
//
// The PWM close comes from an esp_timer callback, which runs in another task
// (possibly on the other core). A dump could otherwise race it: the dump
// opens the valve, then a callback already in flight closes it again. So
// every write goes through one spinlock, and a close is only honoured while
// g_valve_may_close is set. A dump clears that flag and opens the valve in
// the same critical section, and loop() re-asserts "open" on every pass
// whenever closing is not allowed.
// ===========================================================================

static portMUX_TYPE       g_valve_lock      = portMUX_INITIALIZER_UNLOCKED;
static volatile bool      g_valve_may_close = false;
static volatile bool      g_valve_closed    = false;
static esp_timer_handle_t g_close_timer     = nullptr;
static float              g_carry_us        = 0.0f;   // sub-minimum open time carried forward
static float              g_duty            = 1.0f;   // fraction of the window open, last applied

static void valveWrite(bool closed) {
  portENTER_CRITICAL(&g_valve_lock);
  if (closed && g_valve_may_close) {
    gpio_set_level((gpio_num_t)PIN_VALVE, 1);
    g_valve_closed = true;
  } else if (!closed) {
    gpio_set_level((gpio_num_t)PIN_VALVE, 0);
    g_valve_closed = false;
  }
  portEXIT_CRITICAL(&g_valve_lock);
}

// Open now, and refuse every close until valveAllowClose().
static void valveLockOpen() {
  portENTER_CRITICAL(&g_valve_lock);
  g_valve_may_close = false;
  gpio_set_level((gpio_num_t)PIN_VALVE, 0);
  g_valve_closed = false;
  portEXIT_CRITICAL(&g_valve_lock);
  if (g_close_timer) esp_timer_stop(g_close_timer);   // error if idle: harmless
  g_carry_us = 0.0f;
  g_duty = 1.0f;
}

static void valveAllowClose() {
  portENTER_CRITICAL(&g_valve_lock);
  g_valve_may_close = true;
  portEXIT_CRITICAL(&g_valve_lock);
}

static void onCloseTimer(void *) { valveWrite(true); }

// Open for u * window this window, then the timer closes it. Openings shorter
// than the valve's minimum pulse carry over to the next window
// (from valve_code/cuff_deflation_test.ino).
static void applyDuty(float u) {
  esp_timer_stop(g_close_timer);
  g_duty = u;
  const float winUs = VALVE_WINDOW_MS * 1000.0f;
  const float minUs = VALVE_MIN_PULSE_MS * 1000.0f;
  const float want = u * winUs + g_carry_us;
  if (want < minUs) {                 // too short to actuate: save it
    g_carry_us = want;
    valveWrite(true);
    return;
  }
  if (want > winUs - minUs) {         // effectively open the whole window
    g_carry_us = fminf(fmaxf(0.0f, want - winUs), winUs);
    valveWrite(false);
    return;
  }
  g_carry_us = 0.0f;
  valveWrite(false);
  esp_timer_start_once(g_close_timer, (uint64_t)want);
}

// ===========================================================================
// Watchdog
// ===========================================================================

static bool g_wdt_ok = false;

static void startWatchdog() {
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = WDT_TIMEOUT_MS;
  cfg.idle_core_mask = 0;     // watch the loop task only
  cfg.trigger_panic = true;   // panic -> reset -> valve opens
  esp_err_t e = esp_task_wdt_reconfigure(&cfg);
  if (e == ESP_ERR_INVALID_STATE) e = esp_task_wdt_init(&cfg);
  if (e == ESP_OK) e = esp_task_wdt_add(NULL);
  g_wdt_ok = (e == ESP_OK);
}

static void feedWatchdog() {
  if (g_wdt_ok) esp_task_wdt_reset();
}

static void safeDelay(uint32_t ms) {
  const uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    feedWatchdog();
    delay(ms - (millis() - t0) > 20 ? 20 : 1);
  }
}

static const char *resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "power_on";
    case ESP_RST_EXT:       return "external_pin";
    case ESP_RST_SW:        return "software";
    case ESP_RST_PANIC:     return "panic";
    case ESP_RST_INT_WDT:   return "interrupt_watchdog";
    case ESP_RST_TASK_WDT:  return "task_watchdog";
    case ESP_RST_WDT:       return "other_watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_SDIO:      return "sdio";
    default:                return "unknown";
  }
}

// ===========================================================================
// I2C helpers and the TCA9548A mux
// ===========================================================================

static bool i2cProbe(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

static bool i2cWrite8(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool i2cReadN(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, (size_t)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool i2cRead8(uint8_t addr, uint8_t reg, uint8_t &value) {
  return i2cReadN(addr, reg, &value, 1);
}

// The selected channel is cached so the mux is only written when it changes.
// Exactly one channel is ever open: both bands use the same addresses.
static const int16_t MUX_UNKNOWN = -1;
static const int16_t MUX_NONE    = -2;

static bool    g_mux_ok  = false;
static int16_t g_mux_sel = MUX_UNKNOWN;

static bool muxWrite(uint8_t mask) {
  Wire.beginTransmission(MUX_ADDR);
  Wire.write(mask);
  return Wire.endTransmission() == 0;
}

static bool muxSelect(uint8_t ch) {
  if (g_mux_sel == ch) return true;
  if (!muxWrite((uint8_t)(1u << ch))) { g_mux_sel = MUX_UNKNOWN; return false; }
  g_mux_sel = ch;
  return true;
}

static bool muxNone() {
  if (g_mux_sel == MUX_NONE) return true;
  if (!muxWrite(0x00)) { g_mux_sel = MUX_UNKNOWN; return false; }
  g_mux_sel = MUX_NONE;
  return true;
}

static void bringUpMux() {
  g_mux_sel = MUX_UNKNOWN;
  g_mux_ok = i2cProbe(MUX_ADDR) && muxNone();
  if (!g_mux_ok) Out.println(F("E,no TCA9548A at 0x70: check SDA=GPIO21, SCL=GPIO22, 3V3, GND"));
}

// ===========================================================================
// Bands
// ===========================================================================

static Band g_bands[2] = {
  {"distal",   'D', MUX_CH_DISTAL,   false, 0, 0, DEFAULT_IR_AMP, DEFAULT_RED_AMP,
   false, 0, 0, 0, 0, 0, 0, false, 0, false, 0, 0, 0, 0, false},
  {"proximal", 'P', MUX_CH_PROXIMAL, false, 0, 0, DEFAULT_IR_AMP, DEFAULT_RED_AMP,
   false, 0, 0, 0, 0, 0, 0, false, 0, false, 0, 0, 0, 0, false},
};
static Band   &g_distal   = g_bands[0];
static BMI270  g_imu[2];

static Band *bandByTag(const char *arg) {
  if (!arg) return nullptr;
  for (Band &b : g_bands) {
    if (arg[0] == b.tag || arg[0] == (char)(b.tag + 32)) return &b;   // D/d, P/p
  }
  return nullptr;
}

static int bandIndex(const Band &b) { return &b == &g_bands[0] ? 0 : 1; }

// ===========================================================================
// MAX30101 (register map and settings as firmware/mapquest_ppg)
// ===========================================================================

enum : uint8_t {
  REG_FIFO_WR_PTR  = 0x04,
  REG_OVF_COUNTER  = 0x05,
  REG_FIFO_RD_PTR  = 0x06,
  REG_FIFO_DATA    = 0x07,
  REG_FIFO_CONFIG  = 0x08,
  REG_MODE_CONFIG  = 0x09,
  REG_SPO2_CONFIG  = 0x0A,
  REG_LED1_PA      = 0x0C,  // red
  REG_LED2_PA      = 0x0D,  // ir
  REG_LED3_PA      = 0x0E,  // green (unused)
  REG_LED4_PA      = 0x0F,  // green (unused)
  REG_REV_ID       = 0xFE,
  REG_PART_ID      = 0xFF,
};

static const uint8_t  MODE_SPO2        = 0x03;
static const uint8_t  MODE_RESET_BIT   = 0x40;
static const uint8_t  EXPECTED_PART_ID = 0x15;
static const uint8_t  BYTES_PER_SAMPLE = 6;          // red + ir, 3 bytes each
static const size_t   I2C_CHUNK_BYTES  = 120;        // ESP32 Wire buffer is 128
static const uint32_t FULL_SCALE       = 262143;     // 18 bits

static uint8_t encodeSampleRate(uint16_t sps) {
  switch (sps) {
    case 50: return 0;   case 100:  return 1;  case 200:  return 2;  case 400:  return 3;
    case 800: return 4;  case 1000: return 5;  case 1600: return 6;  case 3200: return 7;
    default: return 3;
  }
}

static uint8_t encodePulseWidth(uint16_t us) {
  switch (us) {
    case 69: return 0; case 118: return 1; case 215: return 2; case 411: return 3;
    default: return 3;
  }
}

static uint8_t resolutionBits(uint16_t us) {
  switch (us) {
    case 69: return 15; case 118: return 16; case 215: return 17; default: return 18;
  }
}

static uint8_t encodeAdcRange(uint16_t na) {
  switch (na) {
    case 2048: return 0; case 4096: return 1; case 8192: return 2; case 16384: return 3;
    default: return 1;
  }
}

static uint8_t encodeAveraging(uint8_t n) {
  switch (n) {
    case 1: return 0; case 2: return 1; case 4:  return 2;
    case 8: return 3; case 16: return 4; case 32: return 5;
    default: return 0;
  }
}

static bool ppgWrite(Band &b, uint8_t reg, uint8_t v) {
  return muxSelect(b.ch) && i2cWrite8(MAX30101_ADDR, reg, v);
}

static bool ppgRead(Band &b, uint8_t reg, uint8_t &v) {
  return muxSelect(b.ch) && i2cRead8(MAX30101_ADDR, reg, v);
}

static void ppgClearFifo(Band &b) {
  ppgWrite(b, REG_FIFO_WR_PTR, 0x00);
  ppgWrite(b, REG_OVF_COUNTER, 0x00);
  ppgWrite(b, REG_FIFO_RD_PTR, 0x00);
}

// Clear both FIFOs and restart liveness clocks: after any blocking command,
// and when streaming starts.
static void ppgResyncAll() {
  const uint32_t now = millis();
  for (Band &b : g_bands) {
    if (b.ppg_ok) ppgClearFifo(b);
    b.ppg_last_ms = now;
    b.ppg_next_ms = now;
    b.imu_last_ms = now;
    b.imu_next_ms = now;
  }
}

static bool ppgConfigure(Band &b) {
  if (!ppgWrite(b, REG_MODE_CONFIG, MODE_RESET_BIT)) return false;
  for (int i = 0; i < 100; i++) {
    uint8_t m;
    if (ppgRead(b, REG_MODE_CONFIG, m) && (m & MODE_RESET_BIT) == 0) break;
    safeDelay(5);
  }
  // Averaging + FIFO rollover (a stall shows up in OVF_COUNTER, not a wedge).
  const uint8_t fifoCfg = (encodeAveraging(SAMPLE_AVERAGING) << 5) | (1 << 4) | 0x0F;
  const uint8_t spo2Cfg = (encodeAdcRange(ADC_RANGE_NA) << 5) |
                          (encodeSampleRate(SAMPLE_RATE_SPS) << 2) |
                          encodePulseWidth(PULSE_WIDTH_US);
  if (!ppgWrite(b, REG_FIFO_CONFIG, fifoCfg)) return false;
  if (!ppgWrite(b, REG_SPO2_CONFIG, spo2Cfg)) return false;
  if (!ppgWrite(b, REG_LED1_PA, b.red_amp)) return false;
  if (!ppgWrite(b, REG_LED2_PA, b.ir_amp)) return false;
  if (!ppgWrite(b, REG_LED3_PA, 0x00)) return false;
  if (!ppgWrite(b, REG_LED4_PA, 0x00)) return false;
  if (!ppgWrite(b, REG_MODE_CONFIG, MODE_SPO2)) return false;
  ppgClearFifo(b);
  return true;
}

static bool ppgBringUp(Band &b) {
  b.ppg_ok = false;
  b.part_id = 0;
  b.rev_id = 0;
  b.ppg_fails = 0;
  if (!g_mux_ok) return false;
  if (!ppgRead(b, REG_PART_ID, b.part_id)) {
    Out.printf("E,band %c (%s): no MAX30101 reply at 0x57 on mux channel %u\n",
                  b.tag, b.name, b.ch);
    return false;
  }
  if (b.part_id != EXPECTED_PART_ID) {
    Out.printf("E,band %c (%s): unexpected MAX30101 part id 0x%02X (expected 0x%02X)\n",
                  b.tag, b.name, b.part_id, EXPECTED_PART_ID);
    return false;
  }
  ppgRead(b, REG_REV_ID, b.rev_id);
  if (!ppgConfigure(b)) {
    Out.printf("E,band %c (%s): MAX30101 configuration failed\n", b.tag, b.name);
    return false;
  }
  b.ppg_ok = true;
  b.ppg_last_ms = millis();
  return true;
}

static void ppgFailure(Band &b, const char *why) {
  if (++b.ppg_fails < I2C_FAIL_LIMIT || !b.ppg_ok) return;
  b.ppg_ok = false;
  Out.printf("E,band %c (%s) PPG lost: %s; send R in IDLE to retry\n", b.tag, b.name, why);
  char d[24];
  snprintf(d, sizeof(d), "%c", b.tag);
  event("ppg_lost", d);
}

// Pressure sample-and-hold for the S line, filled by the pressure service.
static uint32_t g_p_raw    = 0;
static uint8_t  g_p_status = 0;
static uint32_t g_p_idx    = 0;

static void ppgDrain(Band &b) {
  if (!b.ppg_ok) return;
  uint8_t wr = 0, rd = 0;
  if (!ppgRead(b, REG_FIFO_WR_PTR, wr) || !ppgRead(b, REG_FIFO_RD_PTR, rd)) {
    ppgFailure(b, "FIFO pointer read failed");
    return;
  }
  uint8_t nSamples = (uint8_t)((wr - rd) & 0x1F);
  if (nSamples == 0) return;

  uint8_t ovf = 0;
  if (ppgRead(b, REG_OVF_COUNTER, ovf) && ovf > 0) {
    // Lost before we got here. Advance the index past them so idx / fs stays
    // the true time, and say so: a gap must never be spliced shut.
    Out.printf("E,band %c FIFO overflow, %u samples lost\n", b.tag, ovf);
    char d[32];
    snprintf(d, sizeof(d), "%c lost=%u", b.tag, ovf);
    event("ppg_overflow", d);
    b.ppg_idx += ovf;
  }

  const uint32_t t_us = micros();
  const uint8_t perChunk = (uint8_t)(I2C_CHUNK_BYTES / BYTES_PER_SAMPLE);
  while (nSamples > 0) {
    const uint8_t n = nSamples < perChunk ? nSamples : perChunk;
    const uint8_t want = n * BYTES_PER_SAMPLE;
    Wire.beginTransmission(MAX30101_ADDR);
    Wire.write(REG_FIFO_DATA);
    if (Wire.endTransmission(false) != 0 || Wire.requestFrom(MAX30101_ADDR, (size_t)want) != want) {
      ppgFailure(b, "FIFO data read failed");
      return;
    }
    for (uint8_t s = 0; s < n; s++) {
      // SpO2 mode FIFO order: red, then IR; 18 bits, left-aligned.
      const uint32_t r0 = Wire.read(), r1 = Wire.read(), r2 = Wire.read();
      const uint32_t i0 = Wire.read(), i1 = Wire.read(), i2 = Wire.read();
      const uint32_t red = ((r0 << 16) | (r1 << 8) | r2) & 0x03FFFF;
      const uint32_t ir  = ((i0 << 16) | (i1 << 8) | i2) & 0x03FFFF;
      if (g_streaming) {
        if (&b == &g_distal) {
          Out.printf("S,%lu,%lu,%lu,%lu,%lu,%lu,%u\n", (unsigned long)b.ppg_idx,
                        (unsigned long)t_us, (unsigned long)ir, (unsigned long)red,
                        (unsigned long)g_p_idx, (unsigned long)g_p_raw, g_p_status);
        } else {
          Out.printf("SP,%lu,%lu,%lu,%lu\n", (unsigned long)b.ppg_idx,
                        (unsigned long)t_us, (unsigned long)ir, (unsigned long)red);
        }
      }
      b.ppg_idx++;
    }
    nSamples -= n;
  }
  b.ppg_fails = 0;
  b.ppg_last_ms = millis();
}

// Blocking single read, for DARK and the PPG check (IDLE, not streaming).
static bool ppgReadOne(Band &b, uint32_t &ir, uint32_t &red, uint32_t timeout_ms) {
  const uint32_t t0 = millis();
  while (millis() - t0 < timeout_ms) {
    feedWatchdog();
    uint8_t wr = 0, rd = 0;
    if (!ppgRead(b, REG_FIFO_WR_PTR, wr) || !ppgRead(b, REG_FIFO_RD_PTR, rd)) return false;
    if (((wr - rd) & 0x1F) == 0) { delay(1); continue; }
    uint8_t buf[BYTES_PER_SAMPLE];
    if (!i2cReadN(MAX30101_ADDR, REG_FIFO_DATA, buf, BYTES_PER_SAMPLE)) return false;
    red = (((uint32_t)buf[0] << 16) | ((uint32_t)buf[1] << 8) | buf[2]) & 0x03FFFF;
    ir  = (((uint32_t)buf[3] << 16) | ((uint32_t)buf[4] << 8) | buf[5]) & 0x03FFFF;
    return true;
  }
  return false;
}

static void measureDark(Band &b) {
  if (!b.ppg_ok) { Out.printf("E,DARK: band %c PPG not initialised\n", b.tag); return; }
  ppgWrite(b, REG_LED1_PA, 0x00);
  ppgWrite(b, REG_LED2_PA, 0x00);
  safeDelay(50);
  ppgClearFifo(b);
  uint64_t sIr = 0, sRed = 0;
  uint16_t n = 0;
  for (uint16_t i = 0; i < DARK_SAMPLES; i++) {
    uint32_t ir, red;
    if (!ppgReadOne(b, ir, red, 200)) break;
    sIr += ir; sRed += red; n++;
  }
  ppgWrite(b, REG_LED1_PA, b.red_amp);
  ppgWrite(b, REG_LED2_PA, b.ir_amp);
  safeDelay(50);
  ppgClearFifo(b);
  if (!n) { Out.printf("E,DARK failed on band %c: no samples\n", b.tag); return; }
  b.dark_ir = (uint32_t)(sIr / n);
  b.dark_red = (uint32_t)(sRed / n);
  b.dark_valid = true;
  Out.printf("# band=%c dark_ir=%lu dark_red=%lu dark_n=%u\n", b.tag,
                (unsigned long)b.dark_ir, (unsigned long)b.dark_red, n);
}

static void ppgCheck(Band *b) {
  if (!b) { Out.println(F("E,PPG needs a band: D or P")); return; }
  if (!b->ppg_ok) { Out.printf("E,band %c PPG not initialised: send R\n", b->tag); return; }
  ppgClearFifo(*b);
  uint64_t sIr = 0, sRed = 0;
  uint16_t n = 0;
  for (uint16_t i = 0; i < 20; i++) {
    uint32_t ir, red;
    if (!ppgReadOne(*b, ir, red, 100)) break;
    sIr += ir; sRed += red; n++;
  }
  if (!n) { Out.printf("E,band %c: no PPG samples\n", b->tag); return; }
  const float ir = (float)sIr / n, red = (float)sRed / n;
  Out.printf("# band=%c ppg_ir=%.0f ppg_red=%.0f ir_pct=%.1f red_pct=%.1f n=%u\n",
                b->tag, ir, red, 100.0f * ir / FULL_SCALE, 100.0f * red / FULL_SCALE, n);
}

// ===========================================================================
// BMI270: initialised by the SparkFun library, read raw from the registers
// ===========================================================================

static bool imuBringUp(Band &b) {
  b.imu_ok = false;
  b.imu_configured = false;
  b.imu_chip_id = 0;
  b.imu_fails = 0;
  b.imu_warned = false;
  if (!g_mux_ok || !muxSelect(b.ch)) return false;
  if (!i2cRead8(IMU_ADDR, 0x00, b.imu_chip_id)) {
    Out.printf("E,band %c (%s): no BMI270 reply at 0x68 on mux channel %u\n",
                  b.tag, b.name, b.ch);
    return false;
  }
  if (b.imu_chip_id != IMU_CHIP_ID) {
    Out.printf("E,band %c (%s): unexpected BMI270 chip id 0x%02X (expected 0x%02X)\n",
                  b.tag, b.name, b.imu_chip_id, IMU_CHIP_ID);
    return false;
  }
  b.imu_ok = true;

  BMI270 &imu = g_imu[bandIndex(b)];
  feedWatchdog();
  int8_t err = imu.beginI2C(IMU_ADDR, Wire);     // loads the 8 KB config blob
  feedWatchdog();
  if (err != BMI2_OK) {
    Out.printf("E,band %c (%s): BMI270 init failed (%d)\n", b.tag, b.name, err);
    return false;
  }
  bmi2_sens_config cfg[2];
  cfg[0].type = BMI2_ACCEL;
  cfg[1].type = BMI2_GYRO;
  err = imu.getConfigs(cfg, 2);
  if (err == BMI2_OK) {
    cfg[0].cfg.acc.odr   = BMI2_ACC_ODR_50HZ;
    cfg[0].cfg.acc.range = BMI2_ACC_RANGE_4G;
    cfg[1].cfg.gyr.odr   = BMI2_GYR_ODR_50HZ;
    cfg[1].cfg.gyr.range = BMI2_GYR_RANGE_500;
    err = imu.setConfigs(cfg, 2);
  }
  if (err != BMI2_OK) {
    Out.printf("E,band %c (%s): BMI270 configuration failed (%d)\n", b.tag, b.name, err);
    return false;
  }
  b.imu_configured = true;
  b.imu_last_ms = millis();
  return true;
}

static void imuPoll(Band &b) {
  if (!b.imu_configured) return;
  uint8_t st = 0;
  if (!muxSelect(b.ch) || !i2cRead8(IMU_ADDR, IMU_REG_STATUS, st)) {
    if (++b.imu_fails >= I2C_FAIL_LIMIT && b.imu_ok) {
      b.imu_ok = false;
      Out.printf("E,band %c (%s) IMU lost; send R in IDLE to retry\n", b.tag, b.name);
      char d[8];
      snprintf(d, sizeof(d), "%c", b.tag);
      event("imu_lost", d);
    }
    return;
  }
  if (!(st & 0x80)) { b.imu_fails = 0; return; }   // no new accel sample yet
  uint8_t raw[12];
  if (!i2cReadN(IMU_ADDR, IMU_REG_DATA, raw, 12)) { b.imu_fails++; return; }
  b.imu_fails = 0;
  b.imu_ok = true;
  const uint32_t t_us = micros();
  int16_t v[6];
  for (int i = 0; i < 6; i++) v[i] = (int16_t)((uint16_t)raw[2 * i] | ((uint16_t)raw[2 * i + 1] << 8));
  if (g_streaming) {
    Out.printf("M,%lu,%lu,%c,%d,%d,%d,%d,%d,%d\n", (unsigned long)b.imu_idx,
                  (unsigned long)t_us, b.tag, v[0], v[1], v[2], v[3], v[4], v[5]);
  }
  b.imu_idx++;
  b.imu_last_ms = millis();
  if (b.imu_warned) {
    b.imu_warned = false;
    char d[8];
    snprintf(d, sizeof(d), "%c", b.tag);
    event("imu_resumed", d);
  }
}

// ===========================================================================
// MPRLS cuff pressure: start a conversion, come back for the busy bit, read
// out when clear (from firmware/mapquest_ppg). Never blocks the loop.
// Status: bit 6 powered, bit 5 busy, bit 2 memory integrity, bit 0 saturation.
// ===========================================================================

static const uint8_t MPRLS_ST_POWERED   = 0x40;
static const uint8_t MPRLS_ST_BUSY      = 0x20;
static const uint8_t MPRLS_ST_INTEGRITY = 0x04;
static const uint8_t MPRLS_ST_SATURATED = 0x01;

static bool     g_p_ok            = false;
static bool     g_p_busy          = false;
static uint32_t g_p_next_us       = 0;
static uint32_t g_p_start_us      = 0;
static uint8_t  g_p_fails         = 0;
static uint32_t g_p_last_good_ms  = 0;
static bool     g_p_integrity_bad = false;
static bool     g_p_zeroed        = false;
static float    g_p_zero_abs      = 0.0f;   // absolute mmHg with the cuff vented
static float    g_p_mmhg          = NAN;    // latest good reading, cuff-relative
static double   g_win_sum         = 0.0;    // this control window's good readings
static uint16_t g_win_n           = 0;

static float countsToAbsMmhg(uint32_t counts) {
  const float psi = ((float)counts - (float)MPRLS_OUTPUT_MIN) * MPRLS_RANGE_PSI /
                    (float)(MPRLS_OUTPUT_MAX - MPRLS_OUTPUT_MIN);
  return psi * MMHG_PER_PSI;
}

static bool mprlsStart() {
  Wire.beginTransmission(MPRLS_ADDR);
  Wire.write(0xAA);
  Wire.write(0x00);
  Wire.write(0x00);
  return Wire.endTransmission() == 0;
}

static void pressureFailure(const char *why) {
  g_p_busy = false;
  if (++g_p_fails < I2C_FAIL_LIMIT || !g_p_ok) return;
  g_p_ok = false;
  g_p_status = 0;
  Out.printf("E,pressure sensor lost: %s; send R in IDLE to retry\n", why);
  event("pressure_lost", why);
}

static void servicePressure() {
  if (!g_p_ok) return;
  const uint32_t now = micros();

  if (!g_p_busy) {
    if ((int32_t)(now - g_p_next_us) < 0) return;
    g_p_next_us += 1000000UL / PRESSURE_RATE_HZ;
    if ((int32_t)(now - g_p_next_us) > 0) g_p_next_us = now;   // fell behind
    if (!mprlsStart()) { pressureFailure("no ACK on start"); return; }
    g_p_start_us = now;
    g_p_busy = true;
    return;
  }

  if (now - g_p_start_us < 1000) return;      // not worth polling before ~1 ms
  if (Wire.requestFrom(MPRLS_ADDR, (size_t)1) != 1) { pressureFailure("no reply"); return; }
  if (Wire.read() & MPRLS_ST_BUSY) {
    if (now - g_p_start_us > MPRLS_TIMEOUT_US) pressureFailure("conversion timeout");
    return;
  }
  if (Wire.requestFrom(MPRLS_ADDR, (size_t)4) != 4) { pressureFailure("short read"); return; }
  const uint8_t st = Wire.read();
  uint32_t c = (uint32_t)Wire.read() << 16;
  c |= (uint32_t)Wire.read() << 8;
  c |= (uint32_t)Wire.read();
  g_p_busy = false;
  g_p_fails = 0;
  g_p_status = st;
  g_p_raw = c;
  g_p_idx++;
  if (st & MPRLS_ST_INTEGRITY) g_p_integrity_bad = true;
  // A good reading is powered and not saturated. Anything else does not count
  // as fresh, so a sensor stuck reporting saturation trips the dropout dump.
  if ((st & MPRLS_ST_POWERED) && !(st & MPRLS_ST_SATURATED) && g_p_zeroed) {
    g_p_mmhg = countsToAbsMmhg(c) - g_p_zero_abs;
    g_p_last_good_ms = millis();
    g_win_sum += g_p_mmhg;
    g_win_n++;
  }
}

static bool mprlsReadBlocking(uint32_t &counts, uint8_t &status) {
  if (!mprlsStart()) return false;
  const uint32_t t0 = micros();
  while (true) {
    delayMicroseconds(500);
    if (Wire.requestFrom(MPRLS_ADDR, (size_t)1) != 1) return false;
    if (!(Wire.read() & MPRLS_ST_BUSY)) break;
    if (micros() - t0 > MPRLS_TIMEOUT_US) return false;
  }
  if (Wire.requestFrom(MPRLS_ADDR, (size_t)4) != 4) return false;
  status = Wire.read();
  counts  = (uint32_t)Wire.read() << 16;
  counts |= (uint32_t)Wire.read() << 8;
  counts |= (uint32_t)Wire.read();
  return true;
}

static void pressureBringUp() {
  g_p_ok = false;
  g_p_busy = false;
  g_p_fails = 0;
  g_p_status = 0;
  g_p_integrity_bad = false;
  if (!i2cProbe(MPRLS_ADDR)) {
    Out.println(F("E,no MPRLS at 0x18 on the main bus: check VIN=3V3, GND, SDA, SCL"));
    return;
  }
  if (Wire.requestFrom(MPRLS_ADDR, (size_t)1) == 1) g_p_status = Wire.read();
  g_p_ok = true;
  g_p_next_us = micros();
  if (g_p_status & MPRLS_ST_INTEGRITY) {
    g_p_integrity_bad = true;
    Out.println(F("E,MPRLS memory-integrity flag set: the sensor failed its own "
                     "calibration check. Power-cycle fully; if it persists, replace it. "
                     "START is refused while it is set."));
  }
}

// Blocking: IDLE and not streaming only. Opens the valve first.
static void zeroPressure() {
  if (!g_p_ok) { Out.println(F("E,ZERO: no pressure sensor")); return; }
  valveLockOpen();
  g_p_busy = false;
  Out.println(F("# zeroing: valve open, cuff must be fully vented"));
  safeDelay(2000);
  double sum = 0;
  uint16_t n = 0;
  for (uint16_t i = 0; i < ZERO_SAMPLES; i++) {
    uint32_t c; uint8_t st;
    if (mprlsReadBlocking(c, st) && (st & MPRLS_ST_POWERED) && !(st & MPRLS_ST_SATURATED)) {
      sum += countsToAbsMmhg(c);
      n++;
    }
    safeDelay(10);
  }
  g_p_next_us = micros();
  if (n < ZERO_SAMPLES / 2) {
    Out.printf("E,ZERO failed: only %u of %u readings good\n", n, ZERO_SAMPLES);
    return;
  }
  g_p_zero_abs = (float)(sum / n);
  g_p_zeroed = true;
  g_p_mmhg = 0.0f;
  g_p_last_good_ms = millis();
  Out.printf("# cuff_zero_abs_mmhg=%.3f zero_n=%u\n", g_p_zero_abs, n);
}

// ===========================================================================
// Session state machine and PI deflation control
// ===========================================================================

static Settings S = {
  DEFAULT_RATE_MMHG_S, DEFAULT_KP, DEFAULT_KI, DEFAULT_KD, DEFAULT_U0,
  DEFAULT_HOLD_S, DEFAULT_P_STOP, DEFAULT_P_RUN_MIN, P_MAX_HARD_MMHG, HOLD_TIMEOUT_HARD_S,
};

static SessState g_state        = ST_IDLE;
static uint32_t  g_close_ms     = 0;     // valve closed at START
static uint32_t  g_hold_ms      = 0;     // RUN accepted
static uint32_t  g_run_ms       = 0;     // DEFLATE started
static uint32_t  g_dump_ms      = 0;
static float     g_p_run_start  = 0.0f;
static float     g_integ        = 0.0f;
static double    g_sq_err_sum   = 0.0;
static uint32_t  g_err_n        = 0;
static float     g_p_window     = NAN;   // last control window's mean pressure
static float     g_rate         = NAN;   // 1 s least-squares slope, mmHg/s
static float     g_pref         = NAN;
static uint32_t  g_c_idx        = 0;
static uint32_t  g_next_win_ms  = 0;
static bool      g_overp_warned = false;

static float g_hist_t[RATE_HISTORY], g_hist_p[RATE_HISTORY];
static uint8_t g_hist_i = 0, g_hist_n = 0;

static const char *stateName(SessState s) {
  switch (s) {
    case ST_IDLE:    return "IDLE";
    case ST_INFLATE: return "INFLATE";
    case ST_HOLD:    return "HOLD";
    case ST_DEFLATE: return "DEFLATE";
    case ST_DUMP:    return "DUMP";
  }
  return "?";
}

static bool sessionActive() {
  return g_state == ST_INFLATE || g_state == ST_HOLD || g_state == ST_DEFLATE;
}

static void setState(SessState s) {
  g_state = s;
  event("state", stateName(s));
}

static void dump(const char *reason) {
  valveLockOpen();
  const bool wasActive = sessionActive();
  g_dump_ms = millis();
  if (g_state != ST_IDLE) g_state = ST_DUMP;
  event("dump", reason);
  if (wasActive) event("state", "DUMP");
}

static void pushHistory(float t, float p) {
  g_hist_t[g_hist_i] = t;
  g_hist_p[g_hist_i] = p;
  g_hist_i = (g_hist_i + 1) % RATE_HISTORY;
  if (g_hist_n < RATE_HISTORY) g_hist_n++;
}

static float slope() {
  if (g_hist_n < 3) return NAN;
  const float t0 = g_hist_t[(g_hist_i + RATE_HISTORY - 1) % RATE_HISTORY];
  double st = 0, sp = 0, stt = 0, stp = 0;
  for (uint8_t i = 0; i < g_hist_n; i++) {
    const double t = g_hist_t[i] - t0, p = g_hist_p[i];
    st += t; sp += p; stt += t * t; stp += t * p;
  }
  const double d = g_hist_n * stt - st * st;
  return d == 0 ? NAN : (float)((g_hist_n * stp - st * sp) / d);
}

static void startDeflate(float p) {
  g_run_ms = millis();
  g_p_run_start = p;
  g_integ = S.u0;
  g_sq_err_sum = 0.0;
  g_err_n = 0;
  setState(ST_DEFLATE);
}

static void finishRun(float t, float p) {
  char d[160];
  const float rms = g_err_n ? sqrtf((float)(g_sq_err_sum / g_err_n)) : NAN;
  snprintf(d, sizeof(d), "p_start=%.1f p_end=%.1f t_s=%.1f mean_rate=%.2f target=%.2f rate_rms_err=%.2f",
           g_p_run_start, p, t, t > 0 ? (g_p_run_start - p) / t : NAN, S.rate, rms);
  event("run_summary", d);
  dump("reached stop pressure");
}

static void controlWindow(uint32_t now) {
  const float pAvg = g_win_n ? (float)(g_win_sum / g_win_n) : NAN;
  g_win_sum = 0.0;
  g_win_n = 0;
  if (!isnan(pAvg)) pushHistory(now / 1000.0f, pAvg);
  g_p_window = pAvg;
  g_rate = slope();
  g_pref = NAN;

  switch (g_state) {
    case ST_IDLE:
      break;

    case ST_INFLATE:
    case ST_HOLD: {
      const float closedS = (now - g_close_ms) / 1000.0f;
      if (!isnan(pAvg) && pAvg > HOLD_TIMEOUT_ABOVE && closedS > S.hold_timeout_s) {
        dump("hold timeout");
        break;
      }
      if (g_state == ST_HOLD && (now - g_hold_ms) / 1000.0f >= S.hold_s && !isnan(pAvg)) {
        startDeflate(pAvg);
      }
      break;
    }

    case ST_DEFLATE: {
      const float t = (now - g_run_ms) / 1000.0f;
      g_pref = g_p_run_start - S.rate * t;
      if (isnan(pAvg)) {
        // No good reading this window: vent rather than guess. The dropout
        // check dumps within PRESSURE_DROPOUT_MS if it persists.
        applyDuty(1.0f);
        break;
      }
      const float e  = pAvg - g_pref;                 // > 0: above the ramp, open more
      const float dt = VALVE_WINDOW_MS / 1000.0f;
      const float iNew = constrain(g_integ + S.ki * e * dt, 0.0f, 1.0f);
      const float dErr = isnan(g_rate) ? 0.0f : (g_rate + S.rate);
      float u = S.kp * e + iNew + S.kd * dErr;
      const bool satHigh = (u >= 1.0f && e > 0);
      const bool satLow  = (u <= 0.0f && e < 0);
      if (!satHigh && !satLow) g_integ = iNew;      // conditional integration (anti-windup)
      applyDuty(constrain(u, 0.0f, 1.0f));

      if (t > 2.0f && !isnan(g_rate)) {             // skip the start-up transient
        const float er = -g_rate - S.rate;
        g_sq_err_sum += er * er;
        g_err_n++;
      }
      const float maxRunS = (g_p_run_start - S.p_stop) / S.rate * 2.0f + 15.0f;
      if (pAvg <= S.p_stop)  finishRun(t, pAvg);
      else if (t > maxRunS)  dump("run timeout");
      break;
    }

    case ST_DUMP:
      if ((!isnan(pAvg) && pAvg < VENTED_BELOW_MMHG) || now - g_dump_ms > DUMP_SETTLE_MAX_MS) {
        if (isnan(pAvg) || pAvg >= VENTED_BELOW_MMHG) event("warning", "left DUMP without a vented reading");
        g_state = ST_IDLE;
        event("state", "IDLE");
      }
      break;
  }

  if (g_streaming) {
    const float dutyPct = (g_state == ST_DEFLATE) ? g_duty * 100.0f : (g_valve_closed ? 0.0f : 100.0f);
    Out.printf("C,%lu,%lu,%s,%.2f,%.2f,%.3f,%.1f\n", (unsigned long)g_c_idx,
                  (unsigned long)micros(), stateName(g_state), g_p_window, g_pref, g_rate, dutyPct);
  }
  g_c_idx++;
}

// Every loop pass. Each check names its reason in the EV line.
static void safetyChecks(uint32_t now) {
  if (sessionActive()) {
    if (!g_p_ok || now - g_p_last_good_ms > PRESSURE_DROPOUT_MS) { dump("pressure dropout"); return; }
    if (g_p_integrity_bad) { dump("pressure sensor integrity flag"); return; }
    if (g_p_mmhg > S.p_max) { dump("overpressure"); return; }
    for (Band &b : g_bands) {
      if (!b.ppg_ok || now - b.ppg_last_ms > PPG_DROPOUT_MS) {
        char r[24];
        snprintf(r, sizeof(r), "ppg dropout %c", b.tag);
        dump(r);
        return;
      }
    }
  } else if (g_p_ok && g_p_zeroed && g_p_mmhg > S.p_max && !g_overp_warned) {
    g_overp_warned = true;        // valve already open; say so once
    event("warning", "over-pressure reading while the valve is open");
  } else if (g_p_mmhg < S.p_max) {
    g_overp_warned = false;
  }

  for (Band &b : g_bands) {       // IMU dropout warns, never dumps
    if (b.imu_configured && !b.imu_warned && now - b.imu_last_ms > IMU_DROPOUT_MS) {
      b.imu_warned = true;
      char d[8];
      snprintf(d, sizeof(d), "%c", b.tag);
      event("imu_dropout", d);
    }
  }

  // Invariant: outside INFLATE / HOLD / DEFLATE the valve is open. Re-assert
  // it every pass: a register write, and the timer callback cannot close the
  // valve while g_valve_may_close is clear.
  if (!g_valve_may_close) {
    if (g_valve_closed) valveLockOpen();
    else gpio_set_level((gpio_num_t)PIN_VALVE, 0);
  }
}

// ===========================================================================
// Reports
// ===========================================================================

static int sensorsOk() {
  return (g_p_ok ? 1 : 0) + (g_bands[0].ppg_ok ? 1 : 0) + (g_bands[1].ppg_ok ? 1 : 0) +
         (g_bands[0].imu_ok ? 1 : 0) + (g_bands[1].imu_ok ? 1 : 0);
}

static void printInfo() {
  Out.printf("# firmware=%s\n", FIRMWARE_VERSION);
  Out.println(F("# board=esp32-wroom-32d"));
  Out.printf("# reset_reason=%s\n", resetReasonName(esp_reset_reason()));
  Out.printf("# watchdog_ok=%d watchdog_ms=%lu\n", g_wdt_ok ? 1 : 0, (unsigned long)WDT_TIMEOUT_MS);
  Out.printf("# i2c_hz=%lu sda=%d scl=%d\n", (unsigned long)I2C_CLOCK_HZ, PIN_SDA, PIN_SCL);
  Out.printf("# mux_ok=%d mux_addr=0x%02X\n", g_mux_ok ? 1 : 0, MUX_ADDR);
  for (Band &b : g_bands) {
    Out.printf("# band=%c name=%s mux_ch=%u ppg_ok=%d part_id=0x%02X rev=0x%02X "
                  "ir_amp=%u red_amp=%u ir_amp_ma=%.1f red_amp_ma=%.1f imu_ok=%d "
                  "imu_chip_id=0x%02X imu_configured=%d\n",
                  b.tag, b.name, b.ch, b.ppg_ok ? 1 : 0, b.part_id, b.rev_id, b.ir_amp,
                  b.red_amp, b.ir_amp * 0.2f, b.red_amp * 0.2f, b.imu_ok ? 1 : 0,
                  b.imu_chip_id, b.imu_configured ? 1 : 0);
    if (b.dark_valid) {
      Out.printf("# band=%c dark_ir=%lu dark_red=%lu\n", b.tag,
                    (unsigned long)b.dark_ir, (unsigned long)b.dark_red);
    }
  }
  Out.printf("# mode=spo2 channels=ir,red fs=%.2f internal_sps=%u smp_ave=%u "
                "pulse_width_us=%u resolution_bits=%u adc_range_na=%u\n",
                (float)SAMPLE_RATE_SPS / SAMPLE_AVERAGING, SAMPLE_RATE_SPS, SAMPLE_AVERAGING,
                PULSE_WIDTH_US, resolutionBits(PULSE_WIDTH_US), ADC_RANGE_NA);
  Out.printf("# imu=bmi270 imu_odr_hz=%u imu_acc_range_g=%u imu_gyr_range_dps=%u "
                "imu_acc_lsb_per_g=%.1f imu_gyr_lsb_per_dps=%.3f\n",
                IMU_ODR_HZ, IMU_ACC_RANGE_G, IMU_GYR_RANGE_DPS,
                32768.0f / IMU_ACC_RANGE_G, 32768.0f / IMU_GYR_RANGE_DPS);
  Out.printf("# pressure=mprls pressure_ok=%d pressure_addr=0x%02X pressure_range_psi=0-25 "
                "pressure_transfer=A pressure_output_min=%lu pressure_output_max=%lu "
                "pressure_rate_hz=%lu pressure_status=0x%02X pressure_integrity=%s\n",
                g_p_ok ? 1 : 0, MPRLS_ADDR, (unsigned long)MPRLS_OUTPUT_MIN,
                (unsigned long)MPRLS_OUTPUT_MAX, (unsigned long)PRESSURE_RATE_HZ, g_p_status,
                g_p_integrity_bad ? "FAIL" : "ok");
  if (g_p_zeroed) Out.printf("# cuff_zero_abs_mmhg=%.3f\n", g_p_zero_abs);
  Out.printf("# set rate=%.2f kp=%.4f ki=%.4f kd=%.4f u0=%.3f hold_s=%.1f p_stop=%.1f "
                "p_run_min=%.1f p_max=%.1f hold_timeout_s=%.1f\n",
                S.rate, S.kp, S.ki, S.kd, S.u0, S.hold_s, S.p_stop, S.p_run_min, S.p_max,
                S.hold_timeout_s);
  Out.printf("# limits p_max_hard=%.0f hold_timeout_hard_s=%.0f vented_below=%.0f "
                "pressure_dropout_ms=%lu ppg_dropout_ms=%lu imu_dropout_ms=%lu "
                "valve_window_ms=%lu valve_min_pulse_ms=%lu\n",
                P_MAX_HARD_MMHG, HOLD_TIMEOUT_HARD_S, VENTED_BELOW_MMHG,
                (unsigned long)PRESSURE_DROPOUT_MS, (unsigned long)PPG_DROPOUT_MS,
                (unsigned long)IMU_DROPOUT_MS, (unsigned long)VALVE_WINDOW_MS,
                (unsigned long)VALVE_MIN_PULSE_MS);
  Out.println(F("# columns_S=idx,t_us,ir,red,p_idx,p_raw,p_status"));
  Out.println(F("# columns_SP=idx,t_us,ir,red"));
  Out.println(F("# columns_M=m_idx,t_us,band,ax,ay,az,gx,gy,gz"));
  Out.println(F("# columns_C=c_idx,t_us,state,p_mmhg,pref_mmhg,rate_mmhg_s,duty_pct"));
  Out.println(F("# columns_EV=t_us,name,detail"));
  Out.printf("# valve_pin=%d valve_type=normally_open valve=%s\n",
                PIN_VALVE, g_valve_closed ? "CLOSED" : "open");
  Out.printf("# state=%s streaming=%d sensors_ok=%d/5\n", stateName(g_state),
                g_streaming ? 1 : 0, sensorsOk());
}

static void scanBus(const char *label) {
  Out.printf("# scan %s:", label);
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    if (a == MUX_ADDR) continue;
    if (i2cProbe(a)) { Out.printf(" 0x%02X", a); n++; }
    feedWatchdog();
  }
  Out.println(n ? "" : " (none)");
}

static void scanAll() {
  Out.printf("# scan mux 0x%02X: %s\n", MUX_ADDR, i2cProbe(MUX_ADDR) ? "present" : "ABSENT");
  if (g_mux_ok && muxNone()) {
    scanBus("main bus (mux closed)");
    for (uint8_t ch = 0; ch < 8; ch++) {
      if (!muxSelect(ch)) continue;
      char label[56];
      const char *who = ch == MUX_CH_DISTAL ? " distal" : ch == MUX_CH_PROXIMAL ? " proximal" : "";
      snprintf(label, sizeof(label), "mux ch%u%s (main-bus devices also show)", ch, who);
      scanBus(label);
    }
    muxNone();
  } else {
    scanBus("main bus");
  }
}

static void valveTest() {
  Out.println(F("# valve click test: 5x close / open"));
  for (int i = 0; i < 5; i++) {
    valveAllowClose();
    valveWrite(true);
    safeDelay(400);
    valveLockOpen();
    safeDelay(400);
  }
  Out.println(F("# valve click test done (valve left open)"));
}

static void bringUpAll() {
  muxNone();
  bringUpMux();
  for (Band &b : g_bands) {
    ppgBringUp(b);
    imuBringUp(b);
    feedWatchdog();
  }
  muxNone();
  pressureBringUp();
  ppgResyncAll();
}

// ===========================================================================
// Commands
// ===========================================================================

static bool requireIdle(const char *cmd, bool alsoNotStreaming) {
  if (g_state != ST_IDLE) {
    Out.printf("E,%s refused: state is %s\n", cmd, stateName(g_state));
    return false;
  }
  if (alsoNotStreaming && g_streaming) {
    Out.printf("E,%s refused while streaming: send STREAM 0 first\n", cmd);
    return false;
  }
  return true;
}

static void cmdStart() {
  if (!requireIdle("START", false)) return;
  const char *why = nullptr;
  if (!g_p_ok)                                 why = "no pressure sensor";
  else if (g_p_integrity_bad)                  why = "pressure sensor integrity flag set";
  else if (!g_p_zeroed)                        why = "pressure not zeroed: send ZERO";
  else if (millis() - g_p_last_good_ms > PRESSURE_DROPOUT_MS) why = "no fresh pressure reading";
  else if (g_p_mmhg >= VENTED_BELOW_MMHG)      why = "cuff not vented";
  else if (!g_bands[0].ppg_ok || !g_bands[1].ppg_ok) why = "a PPG band is not OK";
  if (why) {
    Out.printf("E,START refused: %s\n", why);
    event("start_refused", why);
    return;
  }
  g_close_ms = millis();
  g_hist_n = 0;
  valveAllowClose();
  valveWrite(true);
  setState(ST_INFLATE);
}

static void cmdRun() {
  if (g_state != ST_INFLATE) {
    Out.printf("E,RUN refused: state is %s (needs INFLATE)\n", stateName(g_state));
    return;
  }
  if (!(g_p_mmhg >= S.p_run_min)) {
    char d[48];
    snprintf(d, sizeof(d), "p=%.1f below p_run_min=%.1f", g_p_mmhg, S.p_run_min);
    Out.printf("E,RUN refused: %s\n", d);
    event("run_refused", d);
    return;
  }
  g_hold_ms = millis();
  setState(ST_HOLD);
}

static void cmdSet(char *arg) {
  char *val = arg ? strchr(arg, ' ') : nullptr;
  if (!arg || !val) { Out.println(F("E,SET needs <key> <value>")); return; }
  *val++ = '\0';
  // Everything is set in IDLE, except the Run gate: the operator's Doppler
  // mark during INFLATE moves it (DESIGN 2.1). It is a gate, not a safety
  // limit, and its range check below still applies.
  const bool runGateDuringInflate = g_state == ST_INFLATE && strcmp(arg, "p_run_min") == 0;
  if (!runGateDuringInflate && !requireIdle("SET", false)) return;
  char *end = nullptr;
  const float v = strtof(val, &end);
  if (end == val || isnan(v)) { Out.println(F("E,SET value is not a number")); return; }

  struct Rule { const char *key; float *field; float lo, hi; };
  const Rule rules[] = {
    {"rate",           &S.rate,           0.5f, 5.0f},
    {"kp",             &S.kp,             0.0f, 1.0f},
    {"ki",             &S.ki,             0.0f, 1.0f},
    {"kd",             &S.kd,             0.0f, 1.0f},
    {"u0",             &S.u0,             0.0f, 1.0f},
    {"hold_s",         &S.hold_s,         0.0f, 60.0f},
    {"p_stop",         &S.p_stop,         20.0f, 100.0f},
    {"p_run_min",      &S.p_run_min,      S.p_stop + 10.0f, S.p_max - 10.0f},
    {"p_max",          &S.p_max,          S.p_run_min + 10.0f, P_MAX_HARD_MMHG},   // tighten only
    {"hold_timeout_s", &S.hold_timeout_s, S.hold_s + 10.0f, HOLD_TIMEOUT_HARD_S},  // tighten only
  };
  for (const Rule &r : rules) {
    if (strcmp(arg, r.key) != 0) continue;
    if (v < r.lo || v > r.hi) {
      Out.printf("E,SET %s=%.3f out of range [%.3f, %.3f]\n", r.key, v, r.lo, r.hi);
      return;
    }
    *r.field = v;
    Out.printf("# set %s=%.4f\n", r.key, v);
    char d[48];
    snprintf(d, sizeof(d), "%s=%.4f", r.key, v);
    event("set", d);
    return;
  }
  Out.printf("E,SET unknown key: %s\n", arg);
}

static void cmdAmp(char *arg) {
  if (!requireIdle("AMP", false)) return;
  // AMP <D|P> <IR|RED> <0-255>
  Band *b = bandByTag(arg);
  char *ch = arg ? strchr(arg, ' ') : nullptr;
  char *num = ch ? strchr(ch + 1, ' ') : nullptr;
  if (!b || !ch || !num) { Out.println(F("E,AMP needs <D|P> <IR|RED> <0-255>")); return; }
  ch++;
  *num++ = '\0';
  char *end = nullptr;
  const long v = strtol(num, &end, 10);
  if (end == num || v < 0 || v > 255) { Out.println(F("E,amplitude must be 0-255")); return; }
  const bool isIr = strcasecmp(ch, "IR") == 0;
  if (!isIr && strcasecmp(ch, "RED") != 0) { Out.println(F("E,AMP channel must be IR or RED")); return; }
  if (isIr) b->ir_amp = (uint8_t)v; else b->red_amp = (uint8_t)v;
  if (b->ppg_ok) ppgWrite(*b, isIr ? REG_LED2_PA : REG_LED1_PA, (uint8_t)v);
  Out.printf("# band=%c %s_amp=%ld %s_amp_ma=%.1f\n", b->tag, isIr ? "ir" : "red", v,
                isIr ? "ir" : "red", v * 0.2f);
  char d[32];
  snprintf(d, sizeof(d), "%c %s=%ld", b->tag, isIr ? "ir" : "red", v);
  event("amp", d);   // counts change with no change in blood: the host must re-zero
}

static void cmdStream(const char *arg) {
  const bool on = arg && arg[0] == '1';
  if (on) {
    for (Band &b : g_bands) { b.ppg_idx = 0; b.imu_idx = 0; }
    g_c_idx = 0;
    ppgResyncAll();
    printInfo();
    Out.println(F("# streaming=1"));
    g_streaming = true;
  } else {
    g_streaming = false;
    Out.println(F("# streaming=0"));
  }
}

static char    g_cmd[64];
static uint8_t g_cmd_len = 0;

static void handleCommand(char *line) {
  char *arg = strchr(line, ' ');
  if (arg) { *arg = '\0'; arg++; while (*arg == ' ') arg++; }

  if (strcmp(line, "DUMP") == 0) {
    dump("user");
  } else if (strcmp(line, "START") == 0) {
    cmdStart();
  } else if (strcmp(line, "RUN") == 0) {
    cmdRun();
  } else if (strcmp(line, "INFO") == 0) {
    printInfo();
  } else if (strcmp(line, "STREAM") == 0) {
    cmdStream(arg);
  } else if (strcmp(line, "SET") == 0) {
    cmdSet(arg);
  } else if (strcmp(line, "AMP") == 0) {
    cmdAmp(arg);
  } else if (strcmp(line, "P") == 0) {
    Out.printf("# p_mmhg=%.2f p_raw=%lu p_status=0x%02X zeroed=%d\n", g_p_mmhg,
                  (unsigned long)g_p_raw, g_p_status, g_p_zeroed ? 1 : 0);
  } else if (strcmp(line, "ZERO") == 0) {
    if (requireIdle("ZERO", true)) { zeroPressure(); ppgResyncAll(); }
  } else if (strcmp(line, "DARK") == 0) {
    Band *b = bandByTag(arg);
    if (!b) Out.println(F("E,DARK needs a band: D or P"));
    else if (requireIdle("DARK", true)) { measureDark(*b); ppgResyncAll(); }
  } else if (strcmp(line, "R") == 0) {
    if (requireIdle("R", true)) { bringUpAll(); printInfo(); }
  } else if (strcmp(line, "SCAN") == 0) {
    if (requireIdle("SCAN", true)) { scanAll(); ppgResyncAll(); }
  } else if (strcmp(line, "PPG") == 0) {
    if (requireIdle("PPG", true)) { ppgCheck(bandByTag(arg)); ppgResyncAll(); }
  } else if (strcmp(line, "VALVE") == 0 && arg && strcmp(arg, "TEST") == 0) {
    if (requireIdle("VALVE TEST", true)) valveTest();
  } else if (strcmp(line, "TEST") == 0 && arg && strcmp(arg, "HANG") == 0) {
    if (requireIdle("TEST HANG", true)) {
      valveLockOpen();
      Out.printf("# hanging without feeding the watchdog: expect a reset within %lu ms\n",
                    (unsigned long)WDT_TIMEOUT_MS);
      Serial.flush();
      while (true) { delay(10); }   // delay() does not feed the task watchdog
    }
  } else if (line[0] != '\0') {
    Out.printf("E,unknown command: %s%s%s\n", line, arg ? " " : "", arg ? arg : "");
  }
}

static void pollSerial() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_cmd_len > 0) {
        g_cmd[g_cmd_len] = '\0';
        handleCommand(g_cmd);
        g_cmd_len = 0;
      }
    } else if (g_cmd_len < sizeof(g_cmd) - 1) {
      g_cmd[g_cmd_len++] = c;
    }
  }
}

// ===========================================================================

void setup() {
  pinMode(PIN_VALVE, OUTPUT);
  gpio_set_level((gpio_num_t)PIN_VALVE, 0);   // fail-safe before anything else: vented

  Serial.setTxBufferSize(8192);               // printing never stalls the loop
  Serial.begin(SERIAL_BAUD);

  esp_timer_create_args_t args = {};
  args.callback = &onCloseTimer;
  args.name = "valve_close";
  esp_timer_create(&args, &g_close_timer);

  WiFi.mode(WIFI_OFF);                        // radios add nothing but noise
  btStop();

  Wire.begin(PIN_SDA, PIN_SCL, I2C_CLOCK_HZ);
  Wire.setTimeOut(I2C_TIMEOUT_MS);
  delay(200);                                 // let the sensors finish powering up

  Out.println();
  Out.printf("# %s booting, reset_reason=%s\n", FIRMWARE_VERSION,
                resetReasonName(esp_reset_reason()));
  // Bring-up runs before the watchdog starts: the two BMI270 config loads
  // take a few hundred ms, and the valve is open throughout.
  bringUpAll();
  startWatchdog();
  if (g_p_ok) zeroPressure();
  ppgResyncAll();
  g_next_win_ms = millis();
  printInfo();
  Out.println(sensorsOk() == 5 ? F("# ready: all 5 sensors found")
                                  : F("# NOT ready: fix the wiring and send R, or SCAN to see the bus"));
}

void loop() {
  feedWatchdog();
  pollSerial();
  servicePressure();

  const uint32_t now = millis();
  for (Band &b : g_bands) {
    if ((int32_t)(now - b.ppg_next_ms) >= 0) {
      b.ppg_next_ms = now + PPG_DRAIN_MS;
      ppgDrain(b);
      servicePressure();                      // keep conversions moving
    }
    if ((int32_t)(now - b.imu_next_ms) >= 0) {
      b.imu_next_ms = now + IMU_POLL_MS;
      imuPoll(b);
    }
  }

  if ((int32_t)(now - g_next_win_ms) >= 0) {
    // Fixed cadence; resynchronise rather than burst if the loop fell behind.
    g_next_win_ms = (now - g_next_win_ms >= VALVE_WINDOW_MS) ? now + VALVE_WINDOW_MS
                                                             : g_next_win_ms + VALVE_WINDOW_MS;
    controlWindow(now);
  }

  safetyChecks(millis());
}
