// MAPQuest expo LINK SIMULATOR (expo/README.md)
//
// Board : ESP32-WROOM-32D dev board, Arduino board "ESP32 Dev Module".
// Wiring: NONE. The sensors and the cuff are simulated on the chip.
//
// Purpose: prove the wireless link before the real system runs over it.
// The board is a WPA2 access point (secrets.h). A phone opens
// http://192.168.4.1/ to control and watch; the laptop opens
// http://192.168.4.1/?view to watch, or runs expo/host/expo_receiver.py.
//
// The stream has the IRB firmware's line formats and rates (S, SP 100 Hz,
// M 2 x 50 Hz, C 10 Hz, EV), ~12 kB/s, plus ST status lines at 2 Hz. The
// session follows the IRB rules (START -> INFLATE, RUN -> HOLD -> DEFLATE
// -> dump at p_stop), except that the bulb is simulated: after START the cuff
// pumps itself up. The LED on GPIO2 shows the simulated valve (lit = closed).
// The real valve pin, GPIO25, is held low (vented) the whole time.
//
// Wireless control rules (expo/README.md, "Control rules"):
//   - any client may watch, send DUMP, INFO, P, ECHO <n>, and the display
//     commands VIEW, RESULT, ANALYSER (they never touch the cuff);
//   - one client at a time holds control (CLAIM, CLAIM FORCE when no run is
//     active, RELEASE); every other command needs control;
//   - the controller is lost if it disconnects or is silent for
//     CONTROLLER_TIMEOUT_MS. During INFLATE or HOLD of a run it started,
//     that dumps the cuff ("controller lost"); during DEFLATE it warns.
//   - USB serial is always allowed, as on the bench (source 0).
//
// Serial 921600 baud: same commands; STREAM 1 / STREAM 0 for data on serial.

#include <WiFi.h>
#include <esp_bt.h>
#include <esp_system.h>
#include <stdarg.h>

#include "config.h"
#include "sim.h"
#include "wifi_link.h"

// ===========================================================================
// Output. Out goes to serial and every Wi-Fi client: metadata, events,
// errors. dataLine() goes to serial only while serial streaming is on, and to
// Wi-Fi whenever a client is connected.
// ===========================================================================

class TeeOut : public Print {
 public:
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *buf, size_t n) override {
    Serial.write(buf, n);
    wifiWrite(buf, n);
    return n;
  }
};

static TeeOut Out;
static bool   g_serial_stream = false;

static void dataLine(const char *fmt, ...) {
  const bool toWifi = wifiStreaming();
  if (!g_serial_stream && !toWifi) return;
  char buf[192];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n <= 0) return;
  if (n >= (int)sizeof(buf)) n = sizeof(buf) - 1;
  if (g_serial_stream) Serial.write((const uint8_t *)buf, n);
  if (toWifi) wifiWrite((const uint8_t *)buf, n);
}

static void event(const char *name, const char *detail) {
  Out.printf("EV,%lu,%s,%s\n", (unsigned long)micros(), name, detail ? detail : "");
}

// ===========================================================================
// Noise
// ===========================================================================

static uint32_t g_rng = 0x2545F491u;

static float uniform01() {
  g_rng ^= g_rng << 13;
  g_rng ^= g_rng >> 17;
  g_rng ^= g_rng << 5;
  return (g_rng >> 8) * (1.0f / 16777216.0f);
}

static float gauss(float sd) {   // sum of four uniforms: close enough to normal
  return (uniform01() + uniform01() + uniform01() + uniform01() - 2.0f) * 1.732f * sd;
}

// ===========================================================================
// Simulated cuff, arm and session
// ===========================================================================

static Settings S = {DEFAULT_RATE_MMHG_S, DEFAULT_HOLD_S, DEFAULT_P_STOP,
                     DEFAULT_P_RUN_MIN, P_MAX_MMHG, HOLD_TIMEOUT_S};

static SessState g_state        = ST_IDLE;
static bool      g_valve_closed = false;
static float     g_p            = 0.0f;   // true cuff pressure, mmHg relative
static float     g_p_meas       = 0.0f;   // as the sensor would read it
static float     g_volume       = 0.0f;   // distal relative blood volume
static float     g_pump_target  = SIM_PUMP_TARGET;
static uint32_t  g_close_ms     = 0;
static uint32_t  g_hold_ms      = 0;
static uint32_t  g_run_ms       = 0;
static uint32_t  g_dump_ms      = 0;
static float     g_p_run_start  = 0.0f;
static float     g_rate         = NAN;    // measured, mmHg/s
static float     g_p_prev_win   = NAN;

static uint32_t  g_idx_d = 0, g_idx_p = 0, g_m_idx[2] = {0, 0}, g_c_idx = 0, g_p_idx = 0;
static uint32_t  g_next_ppg_us = 0, g_next_imu_us = 0, g_next_win_ms = 0, g_next_status_ms = 0;

// Who may command (expo/README.md, "Control rules"). 0 = no controller.
static uint32_t  g_controller  = 0;
static bool      g_run_by_wifi = false;

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

static void showValve() {
  if (PIN_SIM_LED >= 0) digitalWrite(PIN_SIM_LED, g_valve_closed ? HIGH : LOW);
}

static void setState(SessState s) {
  g_state = s;
  event("state", stateName(s));
}

static void dump(const char *reason) {
  const bool wasActive = sessionActive();
  g_valve_closed = false;
  showValve();
  g_dump_ms = millis();
  if (g_state != ST_IDLE) g_state = ST_DUMP;
  event("dump", reason);
  if (wasActive) event("state", "DUMP");
}

static void physics(float dt) {
  if (!g_valve_closed) {
    g_p *= expf(-dt / SIM_VENT_TAU_S);
    if (g_p < 0.01f) g_p = 0.0f;
  } else if (g_state == ST_DEFLATE) {
    g_p = fmaxf(g_p - S.rate * dt * (1.0f + gauss(0.05f)), 0.0f);
  } else if (g_state == ST_INFLATE && g_p < g_pump_target) {
    g_p = fminf(g_p + SIM_PUMP_RATE * dt, g_pump_target);
  } else {
    g_p = fmaxf(g_p - 0.05f * dt, 0.0f);          // a slow leak while held
  }
  // Distal blood volume: drains under occlusion, refills below systolic.
  if (g_p > SIM_SYSTOLIC)  g_volume -= 0.3f * dt;
  else if (g_p > 20.0f)    g_volume += 2.0f * dt * fminf(1.0f, (SIM_SYSTOLIC - g_p) / 10.0f);
  else                     g_volume *= expf(-dt / 5.0f);
  g_p_meas = g_p + gauss(0.15f);
}

// MPRLS counts for an absolute pressure (transfer function A, 0-25 psi).
static uint32_t mprlsCounts(float relMmhg) {
  const float psi = (760.0f + relMmhg) / 51.71493f;
  return (uint32_t)(1677722.0f + psi / 25.0f * (15099494.0f - 1677722.0f));
}

// One 10 ms tick: physics, then one distal and one proximal PPG sample.
static void ppgTick() {
  physics(SIM_PPG_PERIOD_US / 1e6f);
  g_p_idx++;
  const float t = micros() / 1e6f;
  const float pulse = 0.0015f * sinf(6.2831853f * SIM_HEART_HZ * t);
  const uint32_t t_us = micros();
  const long irD  = lroundf(130000.0f * (1.0f - 0.0005f * g_volume + pulse) + gauss(20));
  const long redD = lroundf(108000.0f * (1.0f - 0.0008f * g_volume + pulse) + gauss(20));
  const long irP  = lroundf(126000.0f * (1.0f + pulse) + gauss(20));
  const long redP = lroundf(104000.0f * (1.0f + pulse) + gauss(20));
  dataLine("S,%lu,%lu,%ld,%ld,%lu,%lu,64\n", (unsigned long)g_idx_d++, (unsigned long)t_us,
           irD, redD, (unsigned long)g_p_idx, (unsigned long)mprlsCounts(g_p_meas));
  dataLine("SP,%lu,%lu,%ld,%ld\n", (unsigned long)g_idx_p++, (unsigned long)t_us, irP, redP);
}

static void imuTick() {
  static const char tags[2] = {'D', 'P'};
  for (int b = 0; b < 2; b++) {
    dataLine("M,%lu,%lu,%c,%d,%d,%d,%d,%d,%d\n", (unsigned long)g_m_idx[b]++,
             (unsigned long)micros(), tags[b], (int)gauss(30), (int)gauss(30),
             8192 + (int)gauss(30), (int)gauss(20), (int)gauss(20), (int)gauss(20));
  }
}

static void startDeflate() {
  g_run_ms = millis();
  g_p_run_start = g_p_meas;
  setState(ST_DEFLATE);
}

static void controlWindow(uint32_t now) {
  const float p = g_p_meas;
  g_rate = isnan(g_p_prev_win) ? NAN : (p - g_p_prev_win) / (SIM_CTRL_PERIOD_MS / 1000.0f);
  g_p_prev_win = p;
  float pref = NAN;

  if (sessionActive() && p > S.p_max) dump("overpressure");

  switch (g_state) {
    case ST_IDLE:
      break;
    case ST_INFLATE:
    case ST_HOLD:
      if (p > 20.0f && (now - g_close_ms) / 1000.0f > S.hold_timeout_s) { dump("hold timeout"); break; }
      if (g_state == ST_HOLD && (now - g_hold_ms) / 1000.0f >= S.hold_s) startDeflate();
      break;
    case ST_DEFLATE: {
      const float t = (now - g_run_ms) / 1000.0f;
      pref = g_p_run_start - S.rate * t;
      if (p <= S.p_stop) {
        char d[120];
        snprintf(d, sizeof(d), "p_start=%.1f p_end=%.1f t_s=%.1f mean_rate=%.2f target=%.2f simulated=1",
                 g_p_run_start, p, t, t > 0 ? (g_p_run_start - p) / t : NAN, S.rate);
        event("run_summary", d);
        dump("reached stop pressure");
      } else if (t > (g_p_run_start - S.p_stop) / S.rate * 2.0f + 15.0f) {
        dump("run timeout");
      }
      break;
    }
    case ST_DUMP:
      if (p < VENTED_BELOW_MMHG || now - g_dump_ms > 15000) {
        g_state = ST_IDLE;
        event("state", "IDLE");
      }
      break;
  }
  const float duty = g_state == ST_DEFLATE ? 5.0f : (g_valve_closed ? 0.0f : 100.0f);
  dataLine("C,%lu,%lu,%s,%.2f,%.2f,%.3f,%.1f\n", (unsigned long)g_c_idx++, (unsigned long)micros(),
           stateName(g_state), p, pref, g_rate, duty);
}

// ===========================================================================
// Controller (one Wi-Fi client at a time may command)
// ===========================================================================

static void setController(uint32_t id, const char *why) {
  g_controller = id;
  char d[48];
  if (id) snprintf(d, sizeof(d), "%lu %s", (unsigned long)id, why);
  else    snprintf(d, sizeof(d), "none %s", why);
  event("controller", d);
}

static void controllerService(uint32_t now) {
  if (!g_controller) return;
  // Signed difference: last-seen is stamped on another core and can be a
  // hair later than now.
  const bool gone = !wifiConnected(g_controller) ||
                    (int32_t)(now - wifiLastSeenMs(g_controller)) > (int32_t)CONTROLLER_TIMEOUT_MS;
  if (!gone) return;
  setController(0, "lost");
  if (!g_run_by_wifi || !sessionActive()) return;
  if (g_state == ST_INFLATE || g_state == ST_HOLD) dump("controller lost");
  else event("warning", "controller lost during DEFLATE: the run finishes on the board's limits");
}

static void cmdClaim(uint32_t id, const char *arg) {
  const bool force = arg && strcmp(arg, "FORCE") == 0;
  if (g_controller == 0 || g_controller == id) { setController(id, "claimed"); return; }
  if (force && !sessionActive()) {
    char why[32];
    snprintf(why, sizeof(why), "taken from %lu", (unsigned long)g_controller);
    setController(id, why);
    return;
  }
  Out.printf("E,CLAIM refused: client %lu has control%s\n", (unsigned long)g_controller,
             force ? " and a run is active" : " (CLAIM FORCE takes it when no run is active)");
}

static void cmdRelease(uint32_t id) {
  if (g_controller != id) { Out.println(F("E,RELEASE refused: this client does not have control")); return; }
  if (sessionActive() && g_run_by_wifi) { Out.println(F("E,RELEASE refused while its run is active")); return; }
  setController(0, "released");
}

// ===========================================================================
// Display state (expo/DESIGN.md 3.1). What every screen shows: the dashboard
// mode, the latest result, whether the laptop results process is connected.
// Open to every client and never touches the cuff (no valve, no settings, no
// session state). The board keeps the latest values and repeats them in ST
// and INFO, so a screen that reconnects comes back in step.
// Same code in expo_fw (a test keeps the command lists in step).
// ===========================================================================

static bool     g_view_technical = false;
static uint32_t g_analyser       = 0;    // client id of the laptop results process
static char     g_result[48]     = "";   // latest: "run=N mmhg=V flag=F"

static const char *const kResultFlags[] = {"pending", "ok", "no_onset", "arm_moved",
                                           "pulse_present", "incomplete", "safety_stop", "stopped"};

static void setView(bool technical, const char *why) {
  g_view_technical = technical;
  char d[40];
  snprintf(d, sizeof(d), "%s %s", technical ? "TECHNICAL" : "PUBLIC", why);
  event("view", d);
}

static void cmdView(uint32_t src, const char *arg) {
  bool technical;
  if (arg && strcasecmp(arg, "PUBLIC") == 0)         technical = false;
  else if (arg && strcasecmp(arg, "TECHNICAL") == 0) technical = true;
  else { Out.println(F("E,VIEW needs PUBLIC or TECHNICAL")); return; }
  char why[24];
  if (src == SRC_SERIAL) snprintf(why, sizeof(why), "serial");
  else                   snprintf(why, sizeof(why), "client %lu", (unsigned long)src);
  setView(technical, why);
}

// RESULT <run> <mmhg|-> <flag>, from the laptop results process.
static void cmdResult(const char *arg) {
  unsigned long run = 0;
  char val[12], flag[16];
  if (!arg || sscanf(arg, "%lu %11s %15s", &run, val, flag) != 3 || run == 0) {
    Out.println(F("E,RESULT needs <run> <mmhg|-> <flag>"));
    return;
  }
  bool known = false;
  for (const char *f : kResultFlags) if (strcmp(flag, f) == 0) { known = true; break; }
  if (!known) { Out.printf("E,RESULT unknown flag: %s\n", flag); return; }
  if (strcmp(val, "-") != 0) {
    char *end = nullptr;
    const float v = strtof(val, &end);
    if (end == val || *end != '\0' || !(v >= 0.0f && v <= 300.0f)) {
      Out.printf("E,RESULT mmHg must be 0-300 or -: %s\n", val);
      return;
    }
  }
  snprintf(g_result, sizeof(g_result), "run=%lu mmhg=%s flag=%s", run, val, flag);
  event("result", g_result);
}

static void cmdAnalyser(uint32_t src) {
  if (src == SRC_SERIAL) { Out.println(F("E,ANALYSER is for Wi-Fi clients")); return; }
  g_analyser = src;
  char d[24];
  snprintf(d, sizeof(d), "%lu joined", (unsigned long)src);
  event("analyser", d);
}

// Every loop pass: the analyser is gone once it disconnects or goes quiet.
static void analyserService(uint32_t now) {
  if (!g_analyser) return;
  const bool gone = !wifiConnected(g_analyser) ||
                    (int32_t)(now - wifiLastSeenMs(g_analyser)) > (int32_t)CONTROLLER_TIMEOUT_MS;
  if (!gone) return;
  g_analyser = 0;
  event("analyser", "none lost");
}

static void printDisplayInfo() {
  Out.printf("# view=%s analyser=%lu\n", g_view_technical ? "TECHNICAL" : "PUBLIC",
             (unsigned long)g_analyser);
  if (g_result[0]) Out.printf("# result %s\n", g_result);
}

// ===========================================================================
// Reports
// ===========================================================================

static uint32_t g_loop_max_us = 0;        // since boot
static uint32_t g_loop_win_max_us = 0;    // since the last ST line
static uint32_t g_slow_passes = 0;
static uint32_t g_last_dropped = 0;
static uint32_t g_last_slow = 0, g_last_failed = 0;

static void printInfo() {
  Out.printf("# firmware=%s\n", FIRMWARE_VERSION);
  Out.println(F("# board=esp32-wroom-32d mode=link_simulator sensors=simulated"));
  Out.printf("# reset_reason=%d\n", (int)esp_reset_reason());
  wifiPrintInfo(Out);
  Out.printf("# controller=%lu controller_timeout_ms=%lu run_by_wifi=%d\n",
             (unsigned long)g_controller, (unsigned long)CONTROLLER_TIMEOUT_MS, g_run_by_wifi ? 1 : 0);
  printDisplayInfo();
  Out.println(F("# mode=spo2 channels=ir,red fs=100.00 simulated=1"));
  Out.printf("# set rate=%.2f hold_s=%.1f p_stop=%.1f p_run_min=%.1f p_max=%.1f hold_timeout_s=%.1f\n",
             S.rate, S.hold_s, S.p_stop, S.p_run_min, S.p_max, S.hold_timeout_s);
  Out.printf("# sim pump_target=%.1f pump_rate=%.1f systolic=%.1f led_pin=%d real_valve_pin=%d\n",
             SIM_PUMP_TARGET, SIM_PUMP_RATE, SIM_SYSTOLIC, PIN_SIM_LED, PIN_REAL_VALVE);
  Out.println(F("# columns_S=idx,t_us,ir,red,p_idx,p_raw,p_status"));
  Out.println(F("# columns_SP=idx,t_us,ir,red"));
  Out.println(F("# columns_M=m_idx,t_us,band,ax,ay,az,gx,gy,gz"));
  Out.println(F("# columns_C=c_idx,t_us,state,p_mmhg,pref_mmhg,rate_mmhg_s,duty_pct"));
  Out.println(F("# columns_EV=t_us,name,detail"));
  Out.println(F("# columns_ST=t_us,state,sensors,zeroed,controller,clients,p_run_min,hold_s,"
                "rate,p_max,ws_dropped_bytes,loop_max_us,view,analyser"));
  Out.printf("# loop_max_us=%lu loop_slow_passes=%lu loop_warn_us=%lu\n", (unsigned long)g_loop_max_us,
             (unsigned long)g_slow_passes, (unsigned long)LOOP_WARN_US);
  Out.printf("# state=%s streaming=%d sensors_ok=5/5 valve=%s\n", stateName(g_state),
             g_serial_stream ? 1 : 0, g_valve_closed ? "CLOSED" : "open");
}

static void statusLine() {
  // sensors: pressure, distal PPG, proximal PPG, distal IMU, proximal IMU.
  dataLine("ST,%lu,%s,11111,1,%lu,%u,%.1f,%.1f,%.2f,%.1f,%lu,%lu,%s,%lu\n", (unsigned long)micros(),
           stateName(g_state), (unsigned long)g_controller, (unsigned)wifiClientCount(),
           S.p_run_min, S.hold_s, S.rate, S.p_max, (unsigned long)wifiDroppedBytes(),
           (unsigned long)g_loop_win_max_us, g_view_technical ? "TECHNICAL" : "PUBLIC",
           (unsigned long)g_analyser);
  g_loop_win_max_us = 0;
  const uint32_t dropped = wifiDroppedBytes();
  if (dropped != g_last_dropped) {
    char d[48];
    snprintf(d, sizeof(d), "bytes=%lu total=%lu", (unsigned long)(dropped - g_last_dropped),
             (unsigned long)dropped);
    event("wifi_overflow", d);
    g_last_dropped = dropped;
  }
  // A device closed by the board (it vanished or fell behind). The others
  // lost nothing; this says why that one went.
  const uint32_t slow = wifiSlowClosures(), failed = wifiSendFailures();
  if (slow != g_last_slow || failed != g_last_failed) {
    char d[64];
    snprintf(d, sizeof(d), "too_slow=%lu send_failed=%lu", (unsigned long)(slow - g_last_slow),
             (unsigned long)(failed - g_last_failed));
    event("wifi_client_closed", d);
    g_last_slow = slow;
    g_last_failed = failed;
  }
}

// ===========================================================================
// Commands
// ===========================================================================

static uint32_t g_blink_until_ms = 0;

static bool requireIdle(const char *cmd) {
  if (g_state == ST_IDLE) return true;
  Out.printf("E,%s refused: state is %s\n", cmd, stateName(g_state));
  return false;
}

static void cmdStart(uint32_t src) {
  if (!requireIdle("START")) return;
  if (g_p_meas >= VENTED_BELOW_MMHG) {
    Out.println(F("E,START refused: cuff not vented"));
    event("start_refused", "cuff not vented");
    return;
  }
  g_close_ms = millis();
  g_run_by_wifi = src != SRC_SERIAL;
  g_pump_target = fmaxf(SIM_PUMP_TARGET, S.p_run_min + 15.0f);
  g_valve_closed = true;
  showValve();
  setState(ST_INFLATE);
  if (g_view_technical) setView(false, "start");   // every new run opens on the public screen
}

static void cmdRun() {
  if (g_state != ST_INFLATE) {
    Out.printf("E,RUN refused: state is %s (needs INFLATE)\n", stateName(g_state));
    return;
  }
  if (!(g_p_meas >= S.p_run_min)) {
    char d[48];
    snprintf(d, sizeof(d), "p=%.1f below p_run_min=%.1f", g_p_meas, S.p_run_min);
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
  const bool runGate = g_state == ST_INFLATE && strcmp(arg, "p_run_min") == 0;
  if (!runGate && !requireIdle("SET")) return;
  char *end = nullptr;
  const float v = strtof(val, &end);
  if (end == val || isnan(v)) { Out.println(F("E,SET value is not a number")); return; }
  struct Rule { const char *key; float *field; float lo, hi; };
  const Rule rules[] = {
    {"rate",      &S.rate,      0.5f, 5.0f},
    {"hold_s",    &S.hold_s,    0.0f, 60.0f},
    {"p_stop",    &S.p_stop,    20.0f, 100.0f},
    {"p_run_min", &S.p_run_min, S.p_stop + 10.0f, S.p_max - 10.0f},
  };
  for (const Rule &r : rules) {
    if (strcmp(arg, r.key) != 0) continue;
    if (v < r.lo || v > r.hi) {
      Out.printf("E,SET %s=%.3f out of range [%.3f, %.3f]\n", r.key, v, r.lo, r.hi);
      return;
    }
    *r.field = v;
    if (runGate) g_pump_target = fmaxf(g_pump_target, v + 15.0f);
    Out.printf("# set %s=%.4f\n", r.key, v);
    char d[48];
    snprintf(d, sizeof(d), "%s=%.4f", r.key, v);
    event("set", d);
    return;
  }
  Out.printf("E,SET unknown key: %s\n", arg);
}

// Stands in for the real firmware's blocking bench commands (ZERO takes
// ~2.5 s): the loop stops, and the link must keep the controller alive.
static void cmdZero() {
  if (!requireIdle("ZERO")) return;
  Out.println(F("# zeroing: valve open, cuff must be fully vented (simulated, blocks 2.5 s)"));
  delay(2500);
  g_p = 0.0f;
  // The real firmware clears the PPG FIFOs here: samples are not produced
  // during the block, so say so rather than splice the gap shut.
  const uint32_t nowUs = micros();
  g_next_ppg_us = nowUs;
  g_next_imu_us = nowUs;
  Out.println(F("# cuff_zero_abs_mmhg=760.000 zero_n=50"));
  event("stream_gap", "ZERO");
}

static void handleCommand(char *line, uint32_t src) {
  char *arg = strchr(line, ' ');
  if (arg) { *arg = '\0'; arg++; while (*arg == ' ') arg++; }

  if (strcmp(line, "DUMP") == 0)          dump("user");
  else if (strcmp(line, "START") == 0)    cmdStart(src);
  else if (strcmp(line, "RUN") == 0)      cmdRun();
  else if (strcmp(line, "INFO") == 0)     printInfo();
  else if (strcmp(line, "R") == 0)        { Out.println(F("# refresh: simulated sensors, nothing to re-probe")); printInfo(); }
  else if (strcmp(line, "SET") == 0)      cmdSet(arg);
  else if (strcmp(line, "ZERO") == 0)     cmdZero();
  else if (strcmp(line, "P") == 0)        Out.printf("# p_mmhg=%.2f zeroed=1 simulated=1\n", g_p_meas);
  else if (strcmp(line, "BLINK") == 0)    { g_blink_until_ms = millis() + 1500; Out.println(F("# blink")); }
  else if (strcmp(line, "ECHO") == 0)     Out.printf("# echo=%lu client=%lu\n", arg ? strtoul(arg, nullptr, 10) : 0UL, (unsigned long)src);
  else if (strcmp(line, "CLAIM") == 0)    { if (src == SRC_SERIAL) Out.println(F("E,CLAIM is for Wi-Fi clients")); else cmdClaim(src, arg); }
  else if (strcmp(line, "RELEASE") == 0)  { if (src == SRC_SERIAL) Out.println(F("E,RELEASE is for Wi-Fi clients")); else cmdRelease(src); }
  else if (strcmp(line, "VIEW") == 0)     cmdView(src, arg);
  else if (strcmp(line, "RESULT") == 0)   cmdResult(arg);
  else if (strcmp(line, "ANALYSER") == 0) cmdAnalyser(src);
  else if (strcmp(line, "STREAM") == 0) {
    if (src != SRC_SERIAL) { Out.println(F("E,STREAM is serial-only: Wi-Fi clients always receive the stream")); return; }
    g_serial_stream = arg && arg[0] == '1';
    if (g_serial_stream) printInfo();
    Out.printf("# streaming=%d\n", g_serial_stream ? 1 : 0);
  } else if (line[0] != '\0') {
    Out.printf("E,unknown command: %s%s%s\n", line, arg ? " " : "", arg ? arg : "");
  }
}

static bool verbIs(const char *line, const char *verb) {
  const size_t n = strlen(verb);
  return strncmp(line, verb, n) == 0 && (line[n] == '\0' || line[n] == ' ');
}

// A Wi-Fi command: watching, DUMP and the link checks are open to everyone;
// the rest needs control.
static void handleWifiCommand(WifiCommand &c) {
  static const char *const kOpenToAll[] = {"INFO", "P", "ECHO", "CLAIM", "RELEASE", "DUMP",
                                           "VIEW", "RESULT", "ANALYSER"};
  for (const char *v : kOpenToAll) {
    if (verbIs(c.line, v)) { handleCommand(c.line, c.client); return; }
  }
  if (c.client != g_controller) {
    const size_t n = strcspn(c.line, " ");
    Out.printf("E,%.*s refused: client %lu does not have control (send CLAIM)\n", (int)n, c.line,
               (unsigned long)c.client);
    return;
  }
  handleCommand(c.line, c.client);
}

static char    g_cmd[64];
static uint8_t g_cmd_len = 0;

static void pollSerial() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_cmd_len > 0) {
        g_cmd[g_cmd_len] = '\0';
        handleCommand(g_cmd, SRC_SERIAL);
        g_cmd_len = 0;
      }
    } else if (g_cmd_len < sizeof(g_cmd) - 1) {
      g_cmd[g_cmd_len++] = c;
    }
  }
}

static void pollWifi() {
  const uint32_t dumper = wifiTakeDumpRequest();
  if (dumper) {
    char d[24];
    snprintf(d, sizeof(d), "client %lu", (unsigned long)dumper);
    event("dump_request", d);
    dump("user");
  }
  WifiCommand c;
  for (int i = 0; i < 4 && wifiPollCommand(c); i++) handleWifiCommand(c);
}

// ===========================================================================

void setup() {
  pinMode(PIN_REAL_VALVE, OUTPUT);
  digitalWrite(PIN_REAL_VALVE, LOW);            // real valve, if wired: de-energised = vented
  if (PIN_SIM_LED >= 0) { pinMode(PIN_SIM_LED, OUTPUT); digitalWrite(PIN_SIM_LED, LOW); }

  Serial.setTxBufferSize(8192);
  Serial.begin(SERIAL_BAUD);
  delay(100);
  Serial.println();
  Serial.printf("# %s booting\n", FIRMWARE_VERSION);

  btStop();
  const bool ok = wifiBegin();

  const uint32_t nowUs = micros();
  g_next_ppg_us = nowUs;
  g_next_imu_us = nowUs;
  g_next_win_ms = millis();
  g_next_status_ms = millis();
  printInfo();
  if (ok) {
    Serial.printf("# ready: join Wi-Fi \"%s\" and open http://%s/\n", WIFI_SSID,
                  WiFi.softAPIP().toString().c_str());
  } else {
    Serial.println(F("# NOT ready: the access point did not start"));
  }
}

void loop() {
  const uint32_t passStart = micros();
  pollSerial();
  pollWifi();

  // Sample clocks. A loop that fell more than one FIFO (32 samples) behind
  // loses the excess, as the real sensor would; the index still advances so
  // idx / fs stays true time, and the loss is reported.
  uint32_t nowUs = micros();
  const uint32_t behind = (nowUs - g_next_ppg_us) / SIM_PPG_PERIOD_US;
  if ((int32_t)(nowUs - g_next_ppg_us) >= 0 && behind > 32) {
    const uint32_t lost = behind - 32;
    g_idx_d += lost;
    g_idx_p += lost;
    g_next_ppg_us += lost * SIM_PPG_PERIOD_US;
    char d[32];
    snprintf(d, sizeof(d), "D lost=%lu", (unsigned long)lost);
    event("ppg_overflow", d);
  }
  while ((int32_t)(nowUs - g_next_ppg_us) >= 0) { g_next_ppg_us += SIM_PPG_PERIOD_US; ppgTick(); }
  if ((int32_t)(nowUs - g_next_imu_us) >= 0) {
    if ((int32_t)(nowUs - g_next_imu_us) > (int32_t)(10 * SIM_IMU_PERIOD_US)) g_next_imu_us = nowUs;
    g_next_imu_us += SIM_IMU_PERIOD_US;
    imuTick();
  }

  const uint32_t now = millis();
  if ((int32_t)(now - g_next_win_ms) >= 0) {
    g_next_win_ms = (now - g_next_win_ms >= SIM_CTRL_PERIOD_MS) ? now + SIM_CTRL_PERIOD_MS
                                                                : g_next_win_ms + SIM_CTRL_PERIOD_MS;
    controlWindow(now);
  }
  if ((int32_t)(now - g_next_status_ms) >= 0) {
    g_next_status_ms = now + STATUS_PERIOD_MS;
    statusLine();
  }
  controllerService(millis());
  analyserService(millis());

  // LED: the simulated valve, or a blink for the BLINK command.
  if ((int32_t)(g_blink_until_ms - now) > 0) {
    if (PIN_SIM_LED >= 0) digitalWrite(PIN_SIM_LED, ((g_blink_until_ms - now) / 125) % 2 ? HIGH : LOW);
  } else if (g_blink_until_ms) {
    g_blink_until_ms = 0;
    showValve();
  }

  const uint32_t passUs = micros() - passStart;
  if (passUs > g_loop_max_us) g_loop_max_us = passUs;
  if (passUs > g_loop_win_max_us) g_loop_win_max_us = passUs;
  if (passUs > LOOP_WARN_US) {
    g_slow_passes++;
    static uint32_t lastWarnMs = 0;
    if (millis() - lastWarnMs > 1000) {
      lastWarnMs = millis();
      char d[32];
      snprintf(d, sizeof(d), "us=%lu", (unsigned long)passUs);
      event("slow_loop", d);
    }
  }
}
