// Wi-Fi link: WPA2 access point, phone page, WebSocket stream and commands.
// See wifi_link.h for the threading rules.

#include "wifi_link.h"

#include <WiFi.h>
#include <esp_http_server.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <lwip/sockets.h>

#include "config.h"
#include "web_assets.h"
#include "web_page.h"

// ===========================================================================
// Client table and outgoing ring. Written by the HTTP server task (connect,
// frames, close), the loop (wifiWrite) and the per-client sender tasks. One
// spinlock guards both.
//
// The ring is shared, but every client has its own read position, so a slow
// or vanished client only falls behind by itself. Positions count bytes ever
// written (g_written, wraps at 2^32); a client that falls a whole ring behind
// is cut off and closed instead of costing anyone else data.
// ===========================================================================

static_assert((WS_RING_BYTES & (WS_RING_BYTES - 1)) == 0,
              "WS_RING_BYTES must be a power of two (positions wrap at 2^32)");

struct WsClient {
  int               fd;          // -1: free slot
  uint32_t          id;          // never reused while the board runs
  bool              welcomed;    // client_id line sent
  bool              cut;         // fell a whole ring behind: close it
  uint32_t          pos;         // next byte to send, in g_written units
  volatile uint32_t last_rx_ms;  // millis() of the last frame received
};

static portMUX_TYPE   g_lock      = portMUX_INITIALIZER_UNLOCKED;
static WsClient       g_clients[WS_MAX_CLIENTS];
static volatile uint8_t g_nclients = 0;
static uint32_t       g_next_id   = 1;

static httpd_handle_t g_server    = nullptr;
static QueueHandle_t  g_cmdq      = nullptr;
static bool           g_ok        = false;

static volatile uint32_t g_dump_req      = 0;   // client id of a pending DUMP
static volatile uint32_t g_dropped       = 0;   // outgoing bytes dropped (a write larger than the ring)
static volatile uint32_t g_cmd_dropped   = 0;   // commands dropped, queue full
static volatile uint32_t g_send_failures = 0;   // clients closed after a failed send
static volatile uint32_t g_slow_cut      = 0;   // clients closed for falling a ring behind

static uint8_t  g_ring[WS_RING_BYTES];
static uint32_t g_written = 0;                  // bytes ever appended to the ring
static uint8_t  g_frames[WS_MAX_CLIENTS][WS_FRAME_BYTES];   // one send buffer per slot

// Remove fd from the table (no-op if absent). Caller holds the lock.
static void removeLocked(int fd) {
  for (WsClient &c : g_clients) {
    if (c.fd == fd) {
      c.fd = -1;
      if (g_nclients) g_nclients = g_nclients - 1;
      return;
    }
  }
}

// Register a WebSocket that just completed its handshake. If the table is
// full, the stalest client (silent for > 3 s) makes room; otherwise refuse.
static bool addClient(int fd) {
  const uint32_t now = millis();
  int evict = -1;
  bool ok = false;
  portENTER_CRITICAL(&g_lock);
  WsClient *slot = nullptr;
  for (WsClient &c : g_clients) if (c.fd < 0) { slot = &c; break; }
  if (!slot) {
    uint32_t worst = 3000;
    for (WsClient &c : g_clients) {
      const uint32_t age = now - c.last_rx_ms;
      if (age > worst) { worst = age; slot = &c; }
    }
    if (slot) { evict = slot->fd; slot->fd = -1; g_nclients = g_nclients - 1; }
  }
  if (slot) {
    slot->fd = fd;
    slot->id = g_next_id++;
    slot->welcomed = false;
    slot->cut = false;
    slot->pos = g_written;               // starts with the next line, no backlog
    slot->last_rx_ms = now;
    g_nclients = g_nclients + 1;
    ok = true;
  }
  portEXIT_CRITICAL(&g_lock);
  if (evict >= 0) httpd_sess_trigger_close(g_server, evict);
  return ok;
}

// Note a frame from fd; returns its client id (0 if unknown).
static uint32_t touchClient(int fd) {
  uint32_t id = 0;
  const uint32_t now = millis();
  portENTER_CRITICAL(&g_lock);
  for (WsClient &c : g_clients) {
    if (c.fd == fd) { c.last_rx_ms = now; id = c.id; break; }
  }
  portEXIT_CRITICAL(&g_lock);
  return id;
}

// Close slot i if it still holds this client, and count why.
static void dropClient(uint8_t i, uint32_t id, int fd, volatile uint32_t &counter) {
  bool mine = false;
  portENTER_CRITICAL(&g_lock);
  WsClient &c = g_clients[i];
  if (c.fd == fd && c.id == id) {
    c.fd = -1;
    if (g_nclients) g_nclients = g_nclients - 1;
    mine = true;
  }
  portEXIT_CRITICAL(&g_lock);
  if (!mine) return;                    // already closed, or the slot was reused
  counter = counter + 1;
  httpd_sess_trigger_close(g_server, fd);
}

// ===========================================================================
// Outgoing ring: the loop appends whole lines; each client's sender task
// takes frames from its own position that end on a line boundary.
// ===========================================================================

void wifiWrite(const uint8_t *data, size_t n) {
  if (!g_nclients || !n) return;
  if (n > WS_RING_BYTES) { g_dropped = g_dropped + n; return; }
  portENTER_CRITICAL(&g_lock);
  if (g_nclients) {
    // Whoever this write would overrun is cut off; the others lose nothing.
    for (WsClient &c : g_clients) {
      if (c.fd >= 0 && !c.cut && g_written - c.pos + n > WS_RING_BYTES) c.cut = true;
    }
    const size_t at = g_written % WS_RING_BYTES;
    const size_t first = n < WS_RING_BYTES - at ? n : WS_RING_BYTES - at;
    memcpy(g_ring + at, data, first);
    if (n > first) memcpy(g_ring, data + first, n - first);
    g_written += n;
  }
  portEXIT_CRITICAL(&g_lock);
}

enum TakeResult : uint8_t { TAKE_OK, TAKE_GONE, TAKE_CUT };

// Copy up to cap bytes of slot i's backlog into dst, cut at the last newline.
static TakeResult clientTake(uint8_t i, uint32_t id, uint8_t *dst, size_t cap, size_t &len) {
  len = 0;
  portENTER_CRITICAL(&g_lock);
  WsClient &c = g_clients[i];
  if (c.fd < 0 || c.id != id) { portEXIT_CRITICAL(&g_lock); return TAKE_GONE; }
  if (c.cut)                  { portEXIT_CRITICAL(&g_lock); return TAKE_CUT; }
  const uint32_t avail = g_written - c.pos;
  const size_t n = avail < cap ? avail : cap;
  const size_t at = c.pos % WS_RING_BYTES;
  const size_t first = n < WS_RING_BYTES - at ? n : WS_RING_BYTES - at;
  memcpy(dst, g_ring + at, first);
  if (n > first) memcpy(dst + first, g_ring, n - first);
  size_t take = n;
  if (n == cap) {                       // may end mid-line: stop at the last newline
    size_t j = n;
    while (j > 0 && dst[j - 1] != '\n') j--;
    if (j > 0) take = j;
  }
  c.pos += take;
  portEXIT_CRITICAL(&g_lock);
  len = take;
  return TAKE_OK;
}

// ===========================================================================
// Sender tasks (core 0), one per client slot: the only place WebSocket frames
// are sent from. A send can block up to WS_SEND_TIMEOUT_S on a vanished
// client; that stalls only that client's own task.
// ===========================================================================

static bool sendFrame(int fd, const uint8_t *data, size_t len) {
  httpd_ws_frame_t f = {};
  f.final = true;
  f.type = HTTPD_WS_TYPE_TEXT;
  f.payload = (uint8_t *)data;
  f.len = len;
  return httpd_ws_send_frame_async(g_server, fd, &f) == ESP_OK;
}

static void senderTask(void *arg) {
  const uint8_t i = (uint8_t)(uintptr_t)arg;
  uint8_t *frame = g_frames[i];
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(WS_FLUSH_MS));
    portENTER_CRITICAL(&g_lock);
    WsClient &c = g_clients[i];
    const int fd = c.fd;
    const uint32_t id = c.id;
    const bool hello = fd >= 0 && !c.welcomed;
    if (hello) c.welcomed = true;
    portEXIT_CRITICAL(&g_lock);
    if (fd < 0) continue;

    if (hello) {
      char msg[48];
      const int n = snprintf(msg, sizeof(msg), "# client_id=%lu\n", (unsigned long)id);
      if (!sendFrame(fd, (const uint8_t *)msg, (size_t)n)) {
        dropClient(i, id, fd, g_send_failures);
        continue;
      }
    }
    // Up to 8 frames per tick, so a backlog after a short stall drains fast.
    for (int k = 0; k < 8; k++) {
      size_t len = 0;
      const TakeResult r = clientTake(i, id, frame, WS_FRAME_BYTES, len);
      if (r == TAKE_GONE) break;
      if (r == TAKE_CUT) { dropClient(i, id, fd, g_slow_cut); break; }
      if (!len) break;
      if (!sendFrame(fd, frame, len)) { dropClient(i, id, fd, g_send_failures); break; }
    }
  }
}

// ===========================================================================
// HTTP handlers (HTTP server task)
// ===========================================================================

static void queueLine(uint32_t id, char *s) {
  while (*s == ' ') s++;
  size_t len = strlen(s);
  while (len && s[len - 1] == ' ') s[--len] = '\0';
  if (!len) return;
  if (strcmp(s, "PING") == 0) return;              // liveness only, already noted
  if (strcmp(s, "DUMP") == 0) { g_dump_req = id; return; }   // out of band, never dropped
  WifiCommand c;
  c.client = id;
  strlcpy(c.line, s, sizeof(c.line));
  if (xQueueSend(g_cmdq, &c, 0) != pdTRUE) g_cmd_dropped = g_cmd_dropped + 1;
}

static esp_err_t wsHandler(httpd_req_t *req) {
  const int fd = httpd_req_to_sockfd(req);
  if (req->method == HTTP_GET) {                   // the server has done the handshake
    return addClient(fd) ? ESP_OK : ESP_FAIL;
  }
  httpd_ws_frame_t f = {};
  esp_err_t e = httpd_ws_recv_frame(req, &f, 0);   // length only
  if (e != ESP_OK) return e;
  if (f.len > 255) return ESP_FAIL;                // nobody sends this much: drop the client
  uint8_t buf[256];
  f.payload = buf;
  if (f.len) {
    e = httpd_ws_recv_frame(req, &f, f.len);
    if (e != ESP_OK) return e;
  }
  buf[f.len] = '\0';
  const uint32_t id = touchClient(fd);
  if (f.type == HTTPD_WS_TYPE_CLOSE) {
    httpd_sess_trigger_close(req->handle, fd);
    return ESP_OK;
  }
  // PING/PONG control frames only count as liveness. They are not answered,
  // so the sender task stays the only writer on WebSocket sockets.
  if (f.type != HTTPD_WS_TYPE_TEXT || !id) return ESP_OK;
  char *s = (char *)buf;
  while (s && *s) {
    char *nl = strpbrk(s, "\r\n");
    if (nl) *nl = '\0';
    queueLine(id, s);
    s = nl ? nl + 1 : nullptr;
  }
  return ESP_OK;
}

static esp_err_t pageHandler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  httpd_resp_set_hdr(req, "Connection", "close");
  return httpd_resp_send(req, WEB_INDEX_HTML, sizeof(WEB_INDEX_HTML) - 1);
}

// The files the page loads (logo, fonts), from flash (web_assets.h). Only
// the listed paths; anything else is a 404.
static esp_err_t assetHandler(httpd_req_t *req) {
  const size_t n = strcspn(req->uri, "?#");
  for (size_t i = 0; i < WEB_ASSET_COUNT; i++) {
    const WebAsset &a = WEB_ASSETS[i];
    if (strlen(a.path) != n || strncmp(req->uri, a.path, n) != 0) continue;
    httpd_resp_set_type(req, a.type);
    httpd_resp_set_hdr(req, "Cache-Control", "max-age=86400");   // fonts don't change between flashes
    httpd_resp_set_hdr(req, "Connection", "close");
    return httpd_resp_send(req, (const char *)a.data, a.len);
  }
  return httpd_resp_send_404(req);
}

static esp_err_t noContentHandler(httpd_req_t *req) {
  httpd_resp_set_status(req, "204 No Content");
  return httpd_resp_send(req, nullptr, 0);
}

// Called for every closed session, HTTP or WebSocket. With a custom close
// function the server leaves closing the socket to us.
static void onClose(httpd_handle_t, int fd) {
  portENTER_CRITICAL(&g_lock);
  removeLocked(fd);
  portEXIT_CRITICAL(&g_lock);
  close(fd);
}

// ===========================================================================

bool wifiBegin() {
  for (WsClient &c : g_clients) c.fd = -1;
  g_cmdq = xQueueCreate(WS_CMD_QUEUE, sizeof(WifiCommand));

  WiFi.persistent(false);
  WiFi.mode(WIFI_AP);
  const IPAddress ip(192, 168, 4, 1), mask(255, 255, 255, 0);
  if (!WiFi.softAPConfig(ip, ip, mask) ||
      !WiFi.softAP(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL, 0, WIFI_MAX_STATIONS)) {
    Serial.println(F("E,Wi-Fi access point failed to start"));
    return false;
  }
  WiFi.setTxPower((wifi_power_t)WIFI_TX_POWER_QDBM);

  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.core_id = 0;                       // keep the network off the loop's core
  cfg.stack_size = 6144;
  // The WebSocket clients, plus room for a browser fetching the page's files
  // in parallel (each closes after one file). lwIP allows 13 (16 less 3).
  cfg.max_open_sockets = WS_MAX_CLIENTS + 6;
  cfg.uri_match_fn = httpd_uri_match_wildcard;   // "/fonts/*"; plain URIs still match exactly
  cfg.lru_purge_enable = false;          // would close quiet viewers first
  cfg.send_wait_timeout = WS_SEND_TIMEOUT_S;
  cfg.recv_wait_timeout = 2;
  cfg.close_fn = onClose;
  if (httpd_start(&g_server, &cfg) != ESP_OK) {
    Serial.println(F("E,HTTP server failed to start"));
    return false;
  }

  httpd_uri_t page = {};
  page.uri = "/";
  page.method = HTTP_GET;
  page.handler = pageHandler;
  httpd_register_uri_handler(g_server, &page);

  httpd_uri_t ws = {};
  ws.uri = "/ws";
  ws.method = HTTP_GET;
  ws.handler = wsHandler;
  ws.is_websocket = true;
  ws.handle_ws_control_frames = true;
  httpd_register_uri_handler(g_server, &ws);

  httpd_uri_t icon = {};
  icon.uri = "/favicon.ico";
  icon.method = HTTP_GET;
  icon.handler = noContentHandler;
  httpd_register_uri_handler(g_server, &icon);

  httpd_uri_t fonts = {};
  fonts.uri = "/fonts/*";
  fonts.method = HTTP_GET;
  fonts.handler = assetHandler;
  httpd_register_uri_handler(g_server, &fonts);

  httpd_uri_t logo = {};
  logo.uri = "/logo.webp";
  logo.method = HTTP_GET;
  logo.handler = assetHandler;
  httpd_register_uri_handler(g_server, &logo);

  for (uint8_t i = 0; i < WS_MAX_CLIENTS; i++) {
    char name[12];
    snprintf(name, sizeof(name), "ws_send%u", (unsigned)i);
    xTaskCreatePinnedToCore(senderTask, name, 3072, (void *)(uintptr_t)i, 5, nullptr, 0);
  }
  g_ok = true;
  return true;
}

// Radio off / on without a reboot, for comparing sensor noise with and
// without Wi-Fi (expo/README.md). Off drops every client; on restarts the
// same access point. The server keeps running throughout.
static bool g_radio_on = true;

bool wifiRadio(bool on) {
  if (!g_ok || on == g_radio_on) return g_ok;
  const esp_err_t e = on ? esp_wifi_start() : esp_wifi_stop();
  if (e != ESP_OK) return false;
  if (on) WiFi.setTxPower((wifi_power_t)WIFI_TX_POWER_QDBM);   // reset by a restart
  g_radio_on = on;
  return true;
}

bool     wifiRadioOn()      { return g_ok && g_radio_on; }
bool     wifiOk()           { return g_ok; }
bool     wifiStreaming()    { return g_nclients > 0; }
uint8_t  wifiClientCount()  { return g_nclients; }
uint32_t wifiDroppedBytes() { return g_dropped; }
uint32_t wifiSlowClosures() { return g_slow_cut; }
uint32_t wifiSendFailures() { return g_send_failures; }

bool wifiPollCommand(WifiCommand &cmd) {
  return g_cmdq && xQueueReceive(g_cmdq, &cmd, 0) == pdTRUE;
}

uint32_t wifiTakeDumpRequest() {
  portENTER_CRITICAL(&g_lock);
  const uint32_t id = g_dump_req;
  g_dump_req = 0;
  portEXIT_CRITICAL(&g_lock);
  return id;
}

bool wifiConnected(uint32_t client) {
  bool found = false;
  portENTER_CRITICAL(&g_lock);
  for (WsClient &c : g_clients) if (c.fd >= 0 && c.id == client) { found = true; break; }
  portEXIT_CRITICAL(&g_lock);
  return found;
}

uint32_t wifiLastSeenMs(uint32_t client) {
  uint32_t t = 0;
  portENTER_CRITICAL(&g_lock);
  for (WsClient &c : g_clients) if (c.fd >= 0 && c.id == client) { t = c.last_rx_ms; break; }
  portEXIT_CRITICAL(&g_lock);
  return t;
}

bool wifiClientAddress(uint32_t client, char *buf, size_t n) {
  int fd = -1;
  portENTER_CRITICAL(&g_lock);
  for (WsClient &c : g_clients) if (c.fd >= 0 && c.id == client) { fd = c.fd; break; }
  portEXIT_CRITICAL(&g_lock);
  if (fd < 0 || n < 16) return false;
  struct sockaddr_storage sa = {};
  socklen_t len = sizeof(sa);
  if (getpeername(fd, (struct sockaddr *)&sa, &len) != 0) return false;
  const uint8_t *v4 = nullptr;
  if (sa.ss_family == AF_INET) {
    v4 = (const uint8_t *)&((struct sockaddr_in *)&sa)->sin_addr.s_addr;
  } else if (sa.ss_family == AF_INET6) {                // the server listens on IPv6 too:
    const uint8_t *a = ((struct sockaddr_in6 *)&sa)->sin6_addr.s6_addr;   // ::ffff:a.b.c.d
    static const uint8_t kMapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
    if (memcmp(a, kMapped, sizeof(kMapped)) == 0) v4 = a + 12;
  }
  if (!v4) return false;
  snprintf(buf, n, "%u.%u.%u.%u", v4[0], v4[1], v4[2], v4[3]);
  return true;
}

void wifiPrintInfo(Print &out) {
  out.printf("# wifi=softap wifi_ok=%d wifi_radio=%d wifi_ssid=%s wifi_ip=%s wifi_channel=%u "
             "wifi_tx_qdbm=%d wifi_stations=%u wifi_max_stations=%u\n",
             g_ok ? 1 : 0, wifiRadioOn() ? 1 : 0, WIFI_SSID, WiFi.softAPIP().toString().c_str(), WIFI_CHANNEL,
             WIFI_TX_POWER_QDBM, (unsigned)WiFi.softAPgetStationNum(), WIFI_MAX_STATIONS);
  out.printf("# ws_clients=%u ws_max_clients=%u ws_flush_ms=%lu ws_ring_bytes=%u "
             "ws_dropped_bytes=%lu ws_commands_dropped=%lu ws_send_failures=%lu "
             "ws_slow_closures=%lu\n",
             (unsigned)g_nclients, WS_MAX_CLIENTS, (unsigned long)WS_FLUSH_MS,
             (unsigned)WS_RING_BYTES, (unsigned long)g_dropped,
             (unsigned long)g_cmd_dropped, (unsigned long)g_send_failures,
             (unsigned long)g_slow_cut);
}
