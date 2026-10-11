// MAPQuest — MPRLS cuff-pressure test firmware
//
// Target : Arduino Nano ESP32 (ESP32-S3)
// Sensor : Adafruit 3965 MPRLS ported pressure breakout
//          (Honeywell MPRLS0025PA00001A, 0-25 psi absolute, I2C 0x18)
//
// Standalone bench test for the pressure sensor, kept separate from
// mapquest_ppg so PPG testing is untouched. Streams raw 24-bit counts; the
// host converts to mmHg (host/mapquest/mprls.py) and zeroes against the room.
//
// Wiring (PPG unplugged):
//   MPRLS VIN -> 3V3      MPRLS SDA -> A4
//   MPRLS GND -> GND      MPRLS SCL -> A5
//   3Vo, RST, EOC -> not connected
//
// Serial, 115200 baud, one line per sample:
//   M,<ms>,<counts>,<status>      ms = millis() at readout, status = raw byte
// and '#' lines for information. Send 'I' to reprint the header.
//
// A reading takes ~5 ms to convert. Rather than wait inside a library call,
// the loop starts a conversion, polls the status byte for the busy bit, and
// reads out when it clears, so nothing blocks for long. The combined PPG +
// pressure firmware will need the same approach to keep the 100 Hz PPG FIFO
// serviced.
//
// No external libraries required.

#include <WiFi.h>
#include <Wire.h>
#include <esp_bt.h>

static const char*    FW_VERSION  = "mprls-test-0.1";
static const uint32_t SERIAL_BAUD = 115200;
static const uint8_t  MPRLS_ADDR  = 0x18;
static const uint32_t RATE_HZ     = 100;
static const uint32_t PERIOD_US   = 1000000UL / RATE_HZ;
static const uint32_t CONVERT_TIMEOUT_US = 20000;   // datasheet max ~5 ms
static const uint32_t RETRY_MS    = 1000;           // re-probe a missing sensor

// Status byte (Honeywell MPR datasheet): bit 6 powered, bit 5 busy,
// bit 2 memory integrity error, bit 0 math saturation.
static const uint8_t ST_POWERED   = 0x40;
static const uint8_t ST_BUSY      = 0x20;
static const uint8_t ST_INTEGRITY = 0x04;
static const uint8_t ST_SATURATED = 0x01;

enum State : uint8_t { IDLE, CONVERTING };

static State    g_state       = IDLE;
static bool     g_sensor_ok   = false;
static uint32_t g_next_us     = 0;
static uint32_t g_start_us    = 0;
static uint32_t g_last_probe  = 0;
static uint32_t g_timeouts    = 0;

static bool probe() {
  Wire.beginTransmission(MPRLS_ADDR);
  return Wire.endTransmission() == 0;
}

static void scan_bus() {
  Serial.print("# i2c devices:");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      found++;
    }
  }
  Serial.println(found ? "" : " none");
}

static void print_header() {
  Serial.printf("# firmware=%s\n", FW_VERSION);
  Serial.printf("# sensor=MPRLS0025PA00001A addr=0x%02X range_psi=0-25 absolute\n", MPRLS_ADDR);
  Serial.printf("# rate_hz=%lu\n", (unsigned long)RATE_HZ);
  Serial.println("# transfer=A output_min=1677722 output_max=15099494");
  Serial.println("# columns=M,ms,counts,status");
  scan_bus();
  Serial.printf("# sensor_ok=%d\n", g_sensor_ok ? 1 : 0);
}

static bool start_conversion() {
  Wire.beginTransmission(MPRLS_ADDR);
  Wire.write(0xAA);
  Wire.write(0x00);
  Wire.write(0x00);
  return Wire.endTransmission() == 0;
}

static int read_status() {
  if (Wire.requestFrom(MPRLS_ADDR, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

static bool read_sample(uint8_t* status, uint32_t* counts) {
  if (Wire.requestFrom(MPRLS_ADDR, (uint8_t)4) != 4) return false;
  *status = Wire.read();
  uint32_t c = (uint32_t)Wire.read() << 16;
  c |= (uint32_t)Wire.read() << 8;
  c |= (uint32_t)Wire.read();
  *counts = c;
  return true;
}

static void lost(const char* why) {
  if (g_sensor_ok) Serial.printf("# sensor lost: %s\n", why);
  g_sensor_ok = false;
  g_state = IDLE;
  g_last_probe = millis();
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) {}

  WiFi.mode(WIFI_OFF);   // radios off: less supply noise, nothing needs them
  btStop();

  Wire.begin();          // Nano ESP32 default: SDA = A4, SCL = A5
  Wire.setClock(400000);

  g_sensor_ok = probe();
  print_header();
  g_next_us = micros();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'I' || c == 'i') print_header();
  }

  if (!g_sensor_ok) {
    if (millis() - g_last_probe >= RETRY_MS) {
      g_last_probe = millis();
      if (probe()) {
        g_sensor_ok = true;
        Serial.println("# sensor found");
        g_next_us = micros();
      } else {
        Serial.println("# sensor missing: check VIN-3V3, GND, SDA-A4, SCL-A5");
      }
    }
    return;
  }

  uint32_t now = micros();
  if (g_state == IDLE) {
    if ((int32_t)(now - g_next_us) < 0) return;
    g_next_us += PERIOD_US;
    if ((int32_t)(now - g_next_us) > (int32_t)PERIOD_US) g_next_us = now + PERIOD_US;  // fell behind
    if (!start_conversion()) { lost("no ACK on start"); return; }
    g_start_us = now;
    g_state = CONVERTING;
    return;
  }

  // CONVERTING: poll the busy bit, read out when clear.
  if (now - g_start_us < 1000) return;     // no point polling before ~1 ms
  int st = read_status();
  if (st < 0) { lost("no reply to status poll"); return; }
  if (st & ST_BUSY) {
    if (now - g_start_us > CONVERT_TIMEOUT_US) {
      g_timeouts++;
      Serial.printf("# conversion timeout (%lu so far)\n", (unsigned long)g_timeouts);
      g_state = IDLE;
    }
    return;
  }
  uint8_t status;
  uint32_t counts;
  if (!read_sample(&status, &counts)) { lost("short read"); return; }
  g_state = IDLE;
  Serial.printf("M,%lu,%lu,%u\n", (unsigned long)millis(), (unsigned long)counts, status);
}
