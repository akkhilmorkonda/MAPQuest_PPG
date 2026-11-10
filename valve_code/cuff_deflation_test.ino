/*
  cuff_deflation_test.ino
  Feedback-controlled blood pressure cuff deflation at a target rate (default 2 mmHg/s).

  Hardware
    Board : ESP32-WROOM-32D dev board (Arduino IDE board: "ESP32 Dev Module")
    Sensor: Adafruit MPRLS breakout (I2C, addr 0x18, 0-25 psi absolute)
    Valve : Olimex AIR-E-VALVE-3V (DigiKey 1188-AIR-VALVE-3V-ND)
            NORMALLY OPEN, ~45 ohm coil  ->  energize (HIGH) to CLOSE.
            Fail-safe: if the board resets or loses power, the cuff dumps.

  Control approach
    - The valve is on/off, so it is time-proportioned: every control window
      (default 100 ms) it opens for duty*window, then a one-shot hardware
      timer closes it. This keeps pulse widths precise even though the
      pressure read blocks for a few ms.
    - A PI loop tracks a pressure RAMP, Pref(t) = Pstart - rate*t, rather than
      the raw derivative. Tracking the ramp gives the target rate without the
      noise of differentiating pressure (heartbeat pulses, valve ripple).
    - Open times shorter than the valve's minimum pulse are carried over to
      the next window (sigma-delta), so small duties still average correctly.
    - Measured rate (1 s least-squares slope) is logged for evaluation.

  Library : "Adafruit MPRLS" (Arduino Library Manager)
  Serial  : 115200 baud, line ending = Newline. Type ? for commands.
  Log     : CSV lines  t_s,P_mmHg,Pref_mmHg,rate_mmHg_s,duty_pct
            Lines starting with # are status messages.
*/

#include <Wire.h>
#include <Adafruit_MPRLS.h>
#include "esp_timer.h"

// ---------------- Pins ----------------
// Avoid on ESP32-WROOM: GPIO 0, 2, 5, 12, 15 (boot strapping), 6-11 (flash),
// 34-39 (input only).
#define PIN_VALVE    25   // -> MOSFET gate via 220 ohm (10k gate-to-GND pulldown)
#define PIN_MPR_RST  -1   // optional: wire MPRLS RST to GPIO 18 and put 18 here
#define PIN_MPR_EOC  -1   // optional: wire MPRLS EOC to GPIO 19 and put 19 here
#define PIN_SDA      21   // ESP32 default I2C SDA
#define PIN_SCL      22   // ESP32 default I2C SCL

const float HPA_TO_MMHG = 0.750062f;

// ---------------- Tunable parameters (all settable over Serial) ----------------
float    targetRate   = 2.0f;    // mmHg/s, deflation (positive number)
float    Kp           = 0.08f;   // duty per mmHg of ramp error
float    Ki           = 0.04f;   // duty per (mmHg * s)
float    Kd           = 0.0f;    // duty per (mmHg/s) of rate error (D term)
float    u0           = 0.04f;   // starting integrator = feed-forward duty guess
float    pStop        = 40.0f;   // end run and dump below this (mmHg)
float    pStartMin    = 140.0f;  // ARM mode auto-start threshold (mmHg)
float    pMax         = 280.0f;  // overpressure dump (mmHg)
float    holdTimeoutS = 120.0f;  // max time held >20 mmHg with valve closed
uint32_t windowMs     = 100;     // valve PWM / control period (ms)
uint32_t minPulseMs   = 5;       // shortest open pulse the valve reliably follows (ms)

// ---------------- State ----------------
Adafruit_MPRLS mpr(PIN_MPR_RST, PIN_MPR_EOC);
esp_timer_handle_t closeTimer;
volatile bool valveIsOpen = true;

enum Mode { IDLE, HOLD, ARMED, RUN, OPENLOOP };
Mode mode = IDLE;
const char* modeName[] = { "IDLE (valve open)", "HOLD (valve closed)", "ARMED", "RUN", "OPENLOOP" };

float    baseline_hPa = 1013.25f;
float    lastP = 0;
double   winSum = 0;
uint32_t winN = 0;
uint32_t winStartMs = 0;

// History of window-averaged pressure for the slope estimate
const int HIST = 10;             // 10 windows = 1 s at 100 ms
float histT[HIST], histP[HIST];
int   histIdx = 0, histCount = 0;

// Run state
uint32_t runStartMs = 0, holdStartMs = 0;
float    pRunStart = 0, integ = 0, fixedDuty = 0, carryUs = 0;
int      stableWindows = 0, statusCount = 0;
double   sqErrSum = 0;
uint32_t errN = 0;
bool     rawStream = false;

// ---------------- Valve ----------------
void valveClose() { digitalWrite(PIN_VALVE, HIGH); valveIsOpen = false; }  // energized
void valveOpen()  { digitalWrite(PIN_VALVE, LOW);  valveIsOpen = true;  }  // de-energized
void onCloseTimer(void*) { valveClose(); }

// Open the valve for u * window this window; carry sub-minimum remainders forward.
void applyDuty(float u) {
  esp_timer_stop(closeTimer);
  const float winUs = windowMs * 1000.0f;
  const float minUs = minPulseMs * 1000.0f;
  float want = u * winUs + carryUs;

  if (want < minUs) {                    // too short to actuate: save it for later
    carryUs = want;
    valveClose();
    return;
  }
  if (want > winUs - minUs) {            // effectively fully open this window
    carryUs = fminf(fmaxf(0.0f, want - winUs), winUs);
    valveOpen();
    return;
  }
  carryUs = 0;
  valveOpen();
  esp_timer_start_once(closeTimer, (uint64_t)want);
}

// ---------------- Modes ----------------
void setMode(Mode m) {
  esp_timer_stop(closeTimer);
  carryUs = 0;
  mode = m;
  switch (m) {
    case IDLE:  valveOpen(); break;
    case HOLD:
    case ARMED: valveClose(); holdStartMs = millis(); stableWindows = 0; break;
    default:    break;   // RUN / OPENLOOP keep current valve state until first window
  }
  Serial.printf("# mode -> %s\n", modeName[m]);
}

void dump(const char* why) {
  setMode(IDLE);
  Serial.printf("# DUMP: %s\n", why);
}

void startRun(float p, bool openLoop) {
  if (p < pStop + 10.0f) {
    Serial.printf("# pressure %.1f mmHg is too low to start (need > %.0f). Use h, then inflate.\n",
                  p, pStop + 10.0f);
    return;
  }
  runStartMs = millis();
  pRunStart = p;
  integ = u0;
  sqErrSum = 0; errN = 0;
  setMode(openLoop ? OPENLOOP : RUN);
  if (openLoop) Serial.printf("# open-loop duty %.1f %%\n", fixedDuty * 100.0f);
  Serial.println("t_s,P_mmHg,Pref_mmHg,rate_mmHg_s,duty_pct");
}

void finishRun(float t, float p) {
  float avg = (pRunStart - p) / t;
  float rms = errN ? sqrt(sqErrSum / errN) : NAN;
  dump("reached stop pressure");
  Serial.printf("# summary: %.1f -> %.1f mmHg in %.1f s | mean rate %.2f mmHg/s (target %.2f) | "
                "rate RMS error %.2f mmHg/s\n", pRunStart, p, t, avg, targetRate, rms);
}

// ---------------- Signal processing ----------------
void pushHistory(float t, float p) {
  histT[histIdx] = t;
  histP[histIdx] = p;
  histIdx = (histIdx + 1) % HIST;
  if (histCount < HIST) histCount++;
}

// Least-squares slope over the history buffer, mmHg/s (negative while deflating)
float slope() {
  if (histCount < 3) return NAN;
  float t0 = histT[(histIdx - 1 + HIST) % HIST];
  double st = 0, sp = 0, stt = 0, stp = 0;
  for (int i = 0; i < histCount; i++) {
    double t = histT[i] - t0, p = histP[i];
    st += t; sp += p; stt += t * t; stp += t * p;
  }
  double d = histCount * stt - st * st;
  return (d == 0) ? NAN : (float)((histCount * stp - st * sp) / d);
}

// ---------------- Control (once per window) ----------------
void onWindow(uint32_t now, float p) {
  float rate  = slope();
  float tHold = (now - holdStartMs) / 1000.0f;

  switch (mode) {
    case IDLE:
      return;

    case HOLD:
      if (p > 20.0f && tHold > holdTimeoutS) dump("hold timeout");
      return;

    case ARMED:
      if (p > 20.0f && tHold > holdTimeoutS) { dump("hold timeout"); return; }
      // Start once above threshold and pumping has stopped for 1.5 s
      if (p > pStartMin && !isnan(rate) && rate < 0.5f) stableWindows++;
      else stableWindows = 0;
      if (stableWindows * windowMs >= 1500) startRun(p, false);
      return;

    default:
      break;  // RUN / OPENLOOP
  }

  float t    = (now - runStartMs) / 1000.0f;
  float pRef = pRunStart - targetRate * t;
  float u;

  if (mode == RUN) {
    float e    = p - pRef;                         // > 0: above ramp, open more
    float dt   = windowMs / 1000.0f;
    float iNew = constrain(integ + Ki * e * dt, 0.0f, 1.0f);
    // D term on the error derivative: de/dt = dP/dt + targetRate (uses the 1 s
    // least-squares slope, so it is already filtered). > 0 means deflating too slowly.
    float dErr = isnan(rate) ? 0.0f : (rate + targetRate);
    u = Kp * e + iNew + Kd * dErr;
    bool satHigh = (u >= 1.0f && e > 0);
    bool satLow  = (u <= 0.0f && e < 0);
    if (!satHigh && !satLow) integ = iNew;         // conditional integration (anti-windup)
    u = constrain(u, 0.0f, 1.0f);
  } else {
    u = fixedDuty;
  }
  applyDuty(u);

  if (t > 2.0f && !isnan(rate)) {                  // skip start-up transient
    float er = -rate - targetRate;
    sqErrSum += er * er;
    errN++;
  }
  Serial.printf("%.2f,%.2f,%.2f,%.2f,%.1f\n", t, p, pRef, isnan(rate) ? 0.0f : rate, u * 100.0f);

  float maxRunS = (pRunStart - pStop) / targetRate * 2.0f + 15.0f;
  if (p <= pStop)       finishRun(t, p);
  else if (t > maxRunS) dump("run timeout");
}

void statusTick(float p) {
  if (mode != HOLD && mode != ARMED) { statusCount = 0; return; }
  if (++statusCount >= 10) {                       // ~1 Hz
    statusCount = 0;
    float r = slope();
    Serial.printf("# P = %6.1f mmHg   rate = %+5.2f mmHg/s\n", p, isnan(r) ? 0.0f : r);
  }
}

// ---------------- Pressure read ----------------
// Normally uses the Adafruit library. If the sensor reports its memory-integrity
// flag (status bit 2), the library rejects every reading, so this falls back to a
// direct read that ignores that one flag (still rejects busy / math saturation).
bool useRawRead = false;

float readPressure_hPa() {
  if (!useRawRead) return mpr.readPressure();

  Wire.beginTransmission(0x18);                    // start measurement
  Wire.write(0xAA); Wire.write(0x00); Wire.write(0x00);
  if (Wire.endTransmission() != 0) return NAN;

  uint32_t t0 = millis();
  while (true) {                                   // wait for busy bit to clear
    if (Wire.requestFrom((uint8_t)0x18, (uint8_t)1) != 1) return NAN;
    if (!(Wire.read() & 0x20)) break;
    if (millis() - t0 > 20) return NAN;
    delay(1);
  }

  if (Wire.requestFrom((uint8_t)0x18, (uint8_t)4) != 4) return NAN;
  uint8_t s  = Wire.read();
  uint8_t b2 = Wire.read();
  uint8_t b1 = Wire.read();
  uint8_t b0 = Wire.read();
  if (s & 0x01) return NAN;                        // math saturation
  uint32_t raw = ((uint32_t)b2 << 16) | ((uint32_t)b1 << 8) | b0;

  // Transfer function: 10%..90% of 2^24 counts = 0..25 psi absolute
  float psi = ((float)raw - 1677722.0f) * 25.0f / (15099494.0f - 1677722.0f);
  return psi * 68.9476f;                           // psi -> hPa
}

// ---------------- Utilities ----------------
void zeroSensor() {
  setMode(IDLE);
  Serial.println("# zeroing: valve open, cuff must be fully vented...");
  delay(2000);
  double s = 0; int n = 0;
  for (int i = 0; i < 50; i++) {
    float h = readPressure_hPa();
    if (!isnan(h)) { s += h; n++; }
    delay(10);
  }
  if (n) baseline_hPa = s / n;
  Serial.printf("# baseline = %.2f hPa (%d samples)\n", baseline_hPa, n);
}

void clickTest() {
  setMode(IDLE);
  Serial.println("# valve click test: 5x close/open");
  for (int i = 0; i < 5; i++) { valveClose(); delay(400); valveOpen(); delay(400); }
  Serial.println("# click test done (valve left open)");
}

void printHelp() {
  Serial.println(F(
    "\n# Commands (Newline-terminated):\n"
    "#   ?          help                      p   print parameters\n"
    "#   z          re-zero (valve opens; cuff must be vented)\n"
    "#   v          valve click test (5x close/open) - confirms driver wiring\n"
    "#   h          HOLD: close valve so you can inflate; prints P and rate (leak test)\n"
    "#   a          ARM: close valve; controlled deflation starts automatically\n"
    "#              once you stop pumping above the g threshold\n"
    "#   r          RUN controlled deflation now from current pressure\n"
    "#   d<pct>     open-loop deflation at fixed duty, e.g. d10 (valve characterization)\n"
    "#   s or o     STOP: open valve, dump cuff\n"
    "#   t<rate>    target rate, mmHg/s        k<kp>,<ki>[,<kd>]  gains, e.g. k0.08,0.04,0.01\n"
    "#   u<pct>     starting duty guess        e<mmHg>     stop pressure\n"
    "#   g<mmHg>    ARM start threshold        w<ms>       control window\n"
    "#   m<ms>      minimum valve pulse        x           toggle raw pressure stream\n"));
}

void printParams() {
  Serial.printf("# rate=%.2f mmHg/s  Kp=%.4f  Ki=%.4f  Kd=%.4f  u0=%.1f%%  stop=%.0f  start>=%.0f  "
                "max=%.0f mmHg  window=%lu ms  minPulse=%lu ms  holdTimeout=%.0f s\n",
                targetRate, Kp, Ki, Kd, u0 * 100.0f, pStop, pStartMin, pMax,
                (unsigned long)windowMs, (unsigned long)minPulseMs, holdTimeoutS);
}

void doCommand(String s) {
  s.trim();
  if (!s.length()) return;
  char c = s.charAt(0);
  String a = s.substring(1);
  a.trim();
  float v = a.toFloat();

  switch (c) {
    case '?': printHelp(); break;
    case 'p': printParams(); break;
    case 'z': zeroSensor(); break;
    case 'v': clickTest(); break;
    case 'h': setMode(HOLD); break;
    case 'a': setMode(ARMED);
              Serial.printf("# armed: inflate above %.0f mmHg, then stop pumping\n", pStartMin);
              break;
    case 'r': startRun(lastP, false); break;
    case 'd': fixedDuty = constrain(v / 100.0f, 0.0f, 1.0f); startRun(lastP, true); break;
    case 's':
    case 'o': dump("user"); break;
    case 't': if (v > 0) targetRate = v; printParams(); break;
    case 'k': { int i = a.indexOf(',');
                if (i > 0) {
                  Kp = a.substring(0, i).toFloat();
                  String rest = a.substring(i + 1);
                  int j = rest.indexOf(',');
                  if (j > 0) { Ki = rest.substring(0, j).toFloat(); Kd = rest.substring(j + 1).toFloat(); }
                  else       { Ki = rest.toFloat(); }
                }
                printParams(); } break;
    case 'u': u0 = constrain(v / 100.0f, 0.0f, 1.0f); printParams(); break;
    case 'e': pStop = v; printParams(); break;
    case 'g': pStartMin = v; printParams(); break;
    case 'w': if (v >= 20) windowMs = (uint32_t)v; printParams(); break;
    case 'm': minPulseMs = (uint32_t)v; printParams(); break;
    case 'x': rawStream = !rawStream; break;
    default:  Serial.println("# unknown command, ? for help");
  }
}

void handleSerial() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { if (line.length()) { doCommand(line); line = ""; } }
    else line += c;
  }
}

// ---------------- Arduino ----------------
void setup() {
  pinMode(PIN_VALVE, OUTPUT);
  valveOpen();                                     // fail-safe: cuff vented

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) {}       // native USB: wait briefly for monitor

  esp_timer_create_args_t args = {};
  args.callback = &onCloseTimer;
  args.name = "valve_close";
  esp_timer_create(&args, &closeTimer);

  Serial.println("\n# cuff_deflation_test build 4 (ESP32-WROOM, PID)");

  Wire.begin(PIN_SDA, PIN_SCL);
  delay(200);                                      // let the sensor finish power-up

  // Raw check, independent of the library: does 0x18 ACK, and what status does it report?
  Wire.beginTransmission(0x18);
  uint8_t ack = Wire.endTransmission();
  int status = -1;
  if (Wire.requestFrom((uint8_t)0x18, (uint8_t)1) == 1) status = Wire.read();
  if (status >= 0) Serial.printf("# I2C 0x18: %s, status byte = 0x%02X (0x40 = powered, idle)\n",
                                 ack == 0 ? "ACK" : "no ACK", status);
  else             Serial.printf("# I2C 0x18: %s, status byte unreadable\n", ack == 0 ? "ACK" : "no ACK");

  bool mprOk = false;
  for (int i = 0; i < 5 && !mprOk; i++) {          // retry: status can read busy at first
    mprOk = mpr.begin(0x18, &Wire);
    if (!mprOk) delay(200);
  }
  if (!mprOk && ack == 0) {
    Serial.println("# library start-up check failed, but the sensor answers - continuing.");
    mprOk = true;
  }
  if (status >= 0 && (status & 0x04)) {
    useRawRead = true;
    Serial.println("# WARNING: sensor memory-integrity flag is set (calibration check failed).");
    Serial.println("# Using direct reads that ignore it. Power-cycle fully to see if it clears;");
    Serial.println("# if it persists, treat absolute mmHg values as untrusted.");
  }
  if (!mprOk) {
    Serial.println("# MPRLS not found at 0x18 - check VIN=3V3, GND, SDA=GPIO21, SCL=GPIO22");
    while (1) delay(1000);
  }
  Serial.println("# MPRLS found");

  zeroSensor();
  printHelp();
  printParams();
  winStartMs = millis();
}

void loop() {
  handleSerial();

  float hPa = readPressure_hPa();                  // blocks ~5 ms per conversion
  if (!isnan(hPa)) {
    float p = (hPa - baseline_hPa) * HPA_TO_MMHG;
    lastP = p;
    winSum += p;
    winN++;
    if (p > pMax && mode != IDLE) dump("OVERPRESSURE");
    if (rawStream) Serial.printf("%lu,%.3f\n", (unsigned long)millis(), p);
  }

  uint32_t now = millis();
  if (now - winStartMs >= windowMs) {
    winStartMs = (now - winStartMs >= 2 * windowMs) ? now : winStartMs + windowMs;
    float pAvg = winN ? (float)(winSum / winN) : lastP;
    winSum = 0;
    winN = 0;
    pushHistory(now / 1000.0f, pAvg);
    statusTick(pAvg);
    onWindow(now, pAvg);
  }
}
