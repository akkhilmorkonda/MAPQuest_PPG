// Wi-Fi link for the expo firmware (expo/README.md, "How the link works").
//
// The board is a WPA2 access point. ESP-IDF's HTTP server (built into the
// esp32 core, no extra library) serves the phone page at / and a WebSocket
// at /ws. Every WebSocket client receives the same text lines as the serial
// port, batched into one frame every WS_FLUSH_MS.
//
// Threading. The sensor loop is the only writer of outgoing data
// (wifiWrite) and the only reader of commands (wifiPollCommand). The HTTP
// server task receives frames; one sender task per client slot does every
// WebSocket send, each from its own read position in a shared ring. So a slow
// or vanished client delays only itself: it falls behind, and once it is a
// whole ring (~1.3 s) behind it is closed. It can never delay another
// client's data, the sensor loop or the safety checks.

#pragma once

#include <Arduino.h>

// Command source: USB serial is 0; WebSocket clients are numbered from 1 in
// the order they connect (never reused while the board runs).
static const uint32_t SRC_SERIAL = 0;

struct WifiCommand {
  uint32_t client;
  char     line[64];
};

// setup(): start the access point and the server. False if either failed;
// the board still runs on USB serial.
bool     wifiBegin();
bool     wifiOk();

// Loop task only. Appends bytes to the outgoing ring if any WebSocket client
// is connected. Never blocks and never drops data for a client that keeps up;
// a client this write would overrun is closed instead.
void     wifiWrite(const uint8_t *data, size_t n);
bool     wifiStreaming();            // at least one WebSocket client
uint8_t  wifiClientCount();
uint32_t wifiDroppedBytes();         // bytes lost to connected clients since boot (should stay 0)
uint32_t wifiSlowClosures();         // clients closed for falling a whole ring behind
uint32_t wifiSendFailures();         // clients closed after a failed or timed-out send

// Loop task only. Next command line from a client, if any.
bool     wifiPollCommand(WifiCommand &cmd);

// A DUMP from any client, taken out of band so a full command queue can
// never delay it. Returns the client id, or 0 if none is pending.
uint32_t wifiTakeDumpRequest();

// Liveness of one client: connected, and millis() of its last frame.
bool     wifiConnected(uint32_t client);
uint32_t wifiLastSeenMs(uint32_t client);

void     wifiPrintInfo(Print &out);  // '#' metadata for INFO
