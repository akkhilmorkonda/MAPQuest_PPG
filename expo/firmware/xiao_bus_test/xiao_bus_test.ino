// XIAO ESP32S3 bus test: mux, PPG and IMU, nothing else (expo/README.md).
//
// Board : Seeed XIAO ESP32S3, Arduino board "XIAO_ESP32S3" (USB CDC on boot).
// Wiring: TCA9548A on the XIAO's I2C pins (D4 = GPIO5 SDA, D5 = GPIO6 SCL),
//         a breakout (MAX30101 + BMI270) on one mux channel.
// Drives no valve and no pump: safe with the cuff disconnected or connected.
//
// Send any line over USB serial to run the test again. It reports:
//   - the idle level of SDA and SCL (a line held low means a power or
//     wiring fault on the bus, as found on the first rig 2026-11-18);
//   - every address on the main bus and on each of the 8 mux channels;
//   - the MAX30101's part ID, then 1 s of real IR / red samples;
//   - the BMI270's chip ID;
//   - one RESULT line: PASS only if the mux, a PPG and an IMU all answer and
//     the PPG streams at 100 Hz without overflowing.
// If the mux does not answer on GPIO5/6 it also tries the pins swapped.

#include <Wire.h>

static const int PIN_SDA_DEFAULT = 5;   // D4
static const int PIN_SCL_DEFAULT = 6;   // D5
static const int PIN_LED = 21;          // the XIAO's user LED, active low
static const uint32_t I2C_HZ = 400000;

static const uint8_t MUX = 0x70;
static const uint8_t PPG = 0x57, PPG_PART_ID = 0x15;
static const uint8_t IMU_A = 0x68, IMU_B = 0x69, IMU_CHIP_ID = 0x24;

static int g_sda = PIN_SDA_DEFAULT, g_scl = PIN_SCL_DEFAULT;

static bool probe(uint8_t a) {
  Wire.beginTransmission(a);
  return Wire.endTransmission() == 0;
}

static bool write8(uint8_t a, uint8_t reg, uint8_t v) {
  Wire.beginTransmission(a);
  Wire.write(reg);
  Wire.write(v);
  return Wire.endTransmission() == 0;
}

static bool readN(uint8_t a, uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(a);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(a, (size_t)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool read8(uint8_t a, uint8_t reg, uint8_t &v) { return readN(a, reg, &v, 1); }

static bool muxSelect(int ch) {             // ch < 0: all channels off
  Wire.beginTransmission(MUX);
  Wire.write(ch < 0 ? 0 : (uint8_t)(1u << ch));
  return Wire.endTransmission() == 0;
}

// Idle level of the bus lines with the pins' own pull-ups, before I2C starts.
static void lineLevels(int sda, int scl) {
  Wire.end();
  pinMode(sda, INPUT_PULLUP);
  pinMode(scl, INPUT_PULLUP);
  delay(5);
  const int s = digitalRead(sda), c = digitalRead(scl);
  Serial.printf("# lines sda=GPIO%d:%s scl=GPIO%d:%s%s\n", sda, s ? "high" : "LOW", scl, c ? "high" : "LOW",
                (s && c) ? "" : "  <- a line held low: check the mux's power and the wires");
}

static void beginBus(int sda, int scl) {
  Wire.end();
  Wire.begin(sda, scl, I2C_HZ);
  Wire.setTimeOut(20);
  g_sda = sda;
  g_scl = scl;
}

static int scanInto(uint8_t *found, int cap) {
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    if (probe(a) && n < cap) found[n++] = a;
  }
  return n;
}

static void printList(const char *label, const uint8_t *a, int n) {
  Serial.printf("# scan %s:", label);
  for (int i = 0; i < n; i++) Serial.printf(" 0x%02X", a[i]);
  Serial.println(n ? "" : " (none)");
}

// MAX30101 as the expo firmware runs it: SpO2 mode, 400 sps / 4 averaged =
// 100 Hz, 411 us pulses (18 bit), 4096 nA range, 6.2 mA LEDs.
static bool ppgTest(int ch) {
  uint8_t part = 0, rev = 0;
  if (!read8(PPG, 0xFF, part)) { Serial.printf("E,PPG on ch%d: no reply to the part-ID read\n", ch); return false; }
  read8(PPG, 0xFE, rev);
  Serial.printf("# ppg ch%d part_id=0x%02X rev=0x%02X %s\n", ch, part, rev,
                part == PPG_PART_ID ? "(MAX30101)" : "<- expected 0x15");
  if (part != PPG_PART_ID) return false;

  write8(PPG, 0x09, 0x40);                          // reset
  for (int i = 0; i < 50; i++) { uint8_t m = 0x40; read8(PPG, 0x09, m); if (!(m & 0x40)) break; delay(2); }
  bool ok = write8(PPG, 0x08, (2 << 5) | (1 << 4) | 0x0F)   // average 4, rollover
         && write8(PPG, 0x0A, (1 << 5) | (3 << 2) | 3)      // 4096 nA, 400 sps, 411 us
         && write8(PPG, 0x0C, 0x1F) && write8(PPG, 0x0D, 0x1F)
         && write8(PPG, 0x0E, 0x00) && write8(PPG, 0x0F, 0x00)
         && write8(PPG, 0x04, 0) && write8(PPG, 0x05, 0) && write8(PPG, 0x06, 0)
         && write8(PPG, 0x09, 0x03);                        // SpO2 mode: red + IR
  if (!ok) { Serial.println(F("E,PPG configuration write failed")); return false; }

  uint32_t n = 0, ovfTotal = 0, irMin = 0xFFFFFFFF, irMax = 0;
  uint64_t irSum = 0, redSum = 0;
  const uint32_t t0 = millis();
  while (millis() - t0 < 1000) {
    delay(10);
    uint8_t ovf = 0, wr = 0, rd = 0;
    if (read8(PPG, 0x05, ovf) && ovf) ovfTotal += ovf;
    if (!read8(PPG, 0x04, wr) || !read8(PPG, 0x06, rd)) { Serial.println(F("E,PPG FIFO pointer read failed")); return false; }
    uint8_t k = (wr - rd) & 0x1F;
    while (k--) {
      uint8_t b[6];
      if (!readN(PPG, 0x07, b, 6)) { Serial.println(F("E,PPG FIFO data read failed")); return false; }
      const uint32_t red = (((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2]) & 0x3FFFF;
      const uint32_t ir  = (((uint32_t)b[3] << 16) | ((uint32_t)b[4] << 8) | b[5]) & 0x3FFFF;
      irSum += ir; redSum += red; n++;
      if (ir < irMin) irMin = ir;
      if (ir > irMax) irMax = ir;
    }
  }
  write8(PPG, 0x09, 0x80);                          // shut down: LEDs off
  if (!n) { Serial.println(F("E,PPG: no samples in 1 s")); return false; }
  const float ir = (float)irSum / n, red = (float)redSum / n;
  Serial.printf("# ppg ch%d samples=%lu in 1 s (expect ~100) overflow=%lu ir_mean=%.0f (%.1f %% of full scale) "
                "ir_range=%lu..%lu red_mean=%.0f\n", ch, (unsigned long)n, (unsigned long)ovfTotal, ir,
                100.0f * ir / 262143.0f, (unsigned long)irMin, (unsigned long)irMax, red);
  const char *light = ir < 0.02f * 262143 ? "very dark: LEDs off, or nothing on the sensor (normal with no finger)"
                    : ir > 0.97f * 262143 ? "saturated: too much light, lower the LED current"
                    : "in range";
  Serial.printf("# ppg ch%d light: %s\n", ch, light);
  return n >= 90 && n <= 110 && ovfTotal == 0;
}

static bool imuTest(int ch, uint8_t addr) {
  uint8_t id = 0;
  if (!read8(addr, 0x00, id)) { Serial.printf("E,IMU 0x%02X on ch%d: no reply to the chip-ID read\n", addr, ch); return false; }
  Serial.printf("# imu ch%d addr=0x%02X chip_id=0x%02X %s\n", ch, addr, id, id == IMU_CHIP_ID ? "(BMI270)" : "<- expected 0x24");
  return id == IMU_CHIP_ID;
}

static void runTest() {
  Serial.println(F("# ---- XIAO ESP32S3 bus test ----"));
  Serial.printf("# chip=%s rev=%d cpu_mhz=%lu\n", ESP.getChipModel(), ESP.getChipRevision(),
                (unsigned long)ESP.getCpuFreqMHz());

  bool muxOk = false;
  const int pins[2][2] = {{PIN_SDA_DEFAULT, PIN_SCL_DEFAULT}, {PIN_SCL_DEFAULT, PIN_SDA_DEFAULT}};
  for (int i = 0; i < 2 && !muxOk; i++) {
    lineLevels(pins[i][0], pins[i][1]);
    beginBus(pins[i][0], pins[i][1]);
    muxOk = probe(MUX);
    Serial.printf("# i2c sda=GPIO%d scl=GPIO%d hz=%lu mux_0x70=%s\n", g_sda, g_scl, (unsigned long)I2C_HZ,
                  muxOk ? "present" : "ABSENT");
    if (!muxOk && i == 0) Serial.println(F("# trying the pins swapped (SDA on GPIO6, SCL on GPIO5)"));
  }
  if (muxOk && g_sda != PIN_SDA_DEFAULT) {
    Serial.println(F("# NOTE: the bus works only with SDA and SCL swapped: SDA is wired to D5, SCL to D4"));
  }

  uint8_t found[32];
  int n;
  if (muxOk) muxSelect(-1);
  n = scanInto(found, 32);
  printList(muxOk ? "main bus (mux closed)" : "main bus", found, n);

  int ppgCh = -1, imuCh = -1;
  uint8_t imuAddr = 0;
  bool ppgOk = false, imuOk = false;
  if (muxOk) {
    for (int ch = 0; ch < 8; ch++) {
      if (!muxSelect(ch)) { Serial.printf("E,mux channel %d select failed\n", ch); continue; }
      uint8_t f[32];
      const int m = scanInto(f, 32);
      // Drop what the main bus already showed (the mux itself among them).
      uint8_t own[32];
      int k = 0;
      for (int i = 0; i < m; i++) {
        bool seen = false;
        for (int j = 0; j < n; j++) if (f[i] == found[j]) seen = true;
        if (!seen) own[k++] = f[i];
      }
      char label[24];
      snprintf(label, sizeof(label), "mux ch%d", ch);
      printList(label, own, k);
      for (int i = 0; i < k; i++) {
        if (own[i] == PPG && ppgCh < 0) ppgCh = ch;
        if ((own[i] == IMU_A || own[i] == IMU_B) && imuCh < 0) { imuCh = ch; imuAddr = own[i]; }
      }
    }
    if (ppgCh >= 0) { muxSelect(ppgCh); ppgOk = ppgTest(ppgCh); }
    else Serial.println(F("E,no PPG (0x57) on any mux channel"));
    if (imuCh >= 0) { muxSelect(imuCh); imuOk = imuTest(imuCh, imuAddr); }
    else Serial.println(F("E,no IMU (0x68/0x69) on any mux channel"));
    muxSelect(-1);
  } else {
    Serial.println(F("E,no TCA9548A at 0x70 with either pin order: check its 3V3, GND, SDA to D4 and SCL to D5"));
  }

  Serial.printf("# summary mux=%s ppg=%s imu=%s\n", muxOk ? "ok" : "missing",
                ppgCh < 0 ? "missing" : ppgOk ? "ok" : "FAULT", imuCh < 0 ? "missing" : imuOk ? "ok" : "FAULT");
  Serial.printf("RESULT: %s\n", (muxOk && ppgOk && imuOk) ? "PASS" : "FAIL");
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);                      // off (active low)
  Serial.begin(115200);
  const uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) delay(10);
  delay(200);
  runTest();
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last > 500) { last = millis(); digitalWrite(PIN_LED, !digitalRead(PIN_LED)); }   // alive
  if (Serial.available()) {
    while (Serial.available()) Serial.read();
    runTest();
  }
}
