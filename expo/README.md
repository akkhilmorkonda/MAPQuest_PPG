# expo/: wireless prototype for the exposition

The ESP32 makes its own password-protected Wi-Fi network. A **phone** joins it to run the
cuff and watch the data; a **laptop** joins it as the presentation display. This folder
shares no code paths with `irb/` or `host/` (it reuses `irb/host/irbtool` read-only, the way
`irb/` reuses `host/mapquest`).

> **Status (2026-11-18).** Step 1, the **link simulator**, is built: it runs on a bare
> ESP32 with nothing wired and streams a simulated session at the real system's rates.
> The phone page, laptop receiver and control rules pass **54 tests** against a Python
> model of the board (`python -m pytest expo/tests`). **Flashed to the WROOM on COM7 on
> 2026-11-18:** it boots (`wifi_ok=1`, slowest loop pass 0.18 ms), and a laptop scan sees
> `MAPQuest-Expo` as WPA2-Personal on channel 6 at 95 % signal.
>
> **First link test, 2026-11-18 12:23** (phone controlling, laptop browser + receiver
> watching; `expo/link_test_1*.txt`): phone joined, **Blink LED** works, laptop view works.
> Receiver over 70.9 s: every stream on rate (S/SP 100.03, M 50.01 per band, C 10.00, ST
> 2.00 Hz), **0 missing samples, 0 bytes dropped**, round trip median 76 ms / worst 136 ms,
> `RESULT: PASS`. The phone's Start, Run (10 s hold), deflation and a manual DUMP at ~101
> mmHg all arrived; a controller loss in IDLE correctly did not vent. The capture ended
> when the laptop left the network (board uptime confirmed no reboot).
> Not covered yet: a run all the way to 40 mmHg with the laptop watching, and the
> controller-lost vent during INFLATE (row 8).
>
> **Found by that test, fixed in source (not yet flashed):** when the laptop left, the board
> dropped 46.7 kB and logged 2 send failures, because one sender task served every client
> in turn and waited up to 1 s on a dead one. Now each client has its own sender task and
> read position, so a vanished device only falls behind and is closed by itself
> (`wifi_client_closed` event); the others lose nothing. Compiles; needs a reflash and a
> re-test (row 13).
>
> `firmware/expo_fw/` (the real firmware: IRB sensors + valve + this Wi-Fi link + the
> control rules) **compiles, never run on hardware**. It needs the rig wired; see "Step 2".

| Step | State |
|---|---|
| 1 Link simulator on the ESP32 (no wiring) | **flashed; first phone + laptop link test passed.** Dead-client stall fixed in source, not flashed; rows 6, 8 and 13 owed |
| 2 Real firmware over Wi-Fi (`expo_fw`) | **compiles** (IRB `irb_fw` 0.2.1 + Wi-Fi link + control rules); not flashed, needs the wired rig |
| 3 Presentation display | the page's `?view` layout for now; your design to come |

## Network

The board is a WPA2 access point. Name and password live in
`firmware/expo_link_sim/secrets.h`, which **git ignores**. A random password was generated
there on 2026-11-18; open the file to read or change it (8-63 characters). On a fresh
clone, copy `secrets.example.h` to `secrets.h` first, or the sketch refuses to compile.

| | |
|---|---|
| Network | `MAPQuest-Expo` (WPA2) |
| Phone (controls) | `http://192.168.4.1/` |
| Laptop (watch only) | `http://192.168.4.1/?view`, or `expo_receiver.py` |
| Channel | 6 (`WIFI_CHANNEL` in `config.h`; use 1, 6 or 11, whichever is quietest at the venue) |
| Transmit power | 8.5 dBm (`WIFI_TX_POWER_QDBM`): booth range, smaller current spikes on the 3V3 rail |

The network has no internet. iPhones show "No Internet Connection" and stay joined; Android
asks whether to stay connected (say yes). The laptop loses internet over Wi-Fi while it is
joined, so the presentation pages must work offline, and they do.

## Step 1: test the link (link simulator)

### Flash

Arduino IDE 2.x, board **ESP32 Dev Module**, open
`expo/firmware/expo_link_sim/expo_link_sim.ino`, upload. **No libraries to install**: the
WebSocket server is ESP-IDF's, built into the esp32 core. Use upload speed 230400 if
921600 gives an esptool error (it did once on this board, see `irb/README.md`). This
replaces the IRB firmware on the board until you flash that again.

Without the IDE:

```powershell
& "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" compile --fqbn esp32:esp32:esp32 expo/firmware/expo_link_sim
```

```powershell
& "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" upload --fqbn esp32:esp32:esp32 --port COM7 --board-options UploadSpeed=230400 expo/firmware/expo_link_sim
```

The simulator drives **nothing** but the LED on GPIO2 (lit = simulated valve closed). It
holds GPIO25, the real valve pin, low (vented) the whole time.

### Checklist

Paste the receiver's report and anything odd into the session log.

| # | Do | Pass when |
|---|---|---|
| 1 | Reset the board, Serial Monitor at 921600 | `# ready: join Wi-Fi "MAPQuest-Expo" and open http://192.168.4.1/` |
| 2 | Phone: join the network, open `http://192.168.4.1/` | header shows **Live** and **You control**; five green lights; pressure 0 |
| 3 | **Blink LED** | the board's LED flashes for ~1.5 s |
| 4 | Laptop: join the network, open `http://192.168.4.1/?view` | same numbers as the phone; header says **Watching**; Link panel shows 2 clients |
| 5 | Phone: **Start** | LED on; pressure climbs to 160 on both screens; readout turns green and **Run** unlocks at 140 |
| 6 | **Run** | Holding 10 s, then Deflating at −2.0 mmHg/s; at 40 the cuff vents, LED off, back to Ready. No red banner |
| 7 | Start again, then **DUMP** while inflating | LED off at once; Venting, then Ready |
| 8 | Start, then lock the phone or turn its Wi-Fi off | within ~2 s: LED off, laptop shows red **Cuff vented automatically: controller lost** |
| 9 | Unlock the phone | it reconnects and takes control again by itself |
| 10 | **Zero cuff** (blocks the board for 2.5 s, like the real ZERO) | phone keeps control; event log shows `stream_gap: ZERO` |
| 11 | Laptop: `python expo/host/expo_receiver.py --seconds 120` while the phone runs a session | `RESULT: PASS` |
| 12 | Walk the phone to the far side of the booth | Link panel: missing stays 0, round trip stays well under 250 ms |
| 13 | Laptop and phone both watching, then turn the **laptop's** Wi-Fi off | phone's Link panel: missing stays 0, no freeze; event log shows `wifi_client_closed` within ~2 s |

`expo_receiver.py` grades the link: every stream within ±3 % of its rate (S, SP 100 Hz,
M 50 Hz per band, C 10 Hz, ST 2 Hz), **zero missing samples**, zero bytes dropped on the
board, median round trip ≤ 250 ms (this includes up to 40 ms of batching). `--save
link.txt` keeps every line.

### No hardware: the local stand-in

`expo/host/sim_server.py` serves the same page and WebSocket from the laptop, using a Python
model of the simulator's wireless rules over the IRB mock board:

```powershell
python expo/host/sim_server.py
```

Then open `http://127.0.0.1:8765/` and `http://127.0.0.1:8765/?view`, or
`python expo/host/expo_receiver.py --url ws://127.0.0.1:8765/ws`. `--lan` lets a phone on the
same network reach it. Round trips here are localhost numbers, not radio numbers.

## Step 2: the real firmware (`expo_fw`)

The IRB firmware with the Wi-Fi link added. Sensors, valve, PI deflation control, the
watchdog and the safety layer are the IRB code unchanged (a test checks the safety limits
still match `irb_fw`). What is new:

- **The radio is on.** The IRB firmware turns it off; this one starts the access point at
  boot, before the watchdog, with the valve open. Bluetooth stays off.
- **Data goes to Wi-Fi whenever a device is connected.** Serial gets data lines only after
  `STREAM 1`, as on the bench. Sample counters run from boot and are never reset.
- **The phone gets the session commands only.** Control rules as in the link simulator,
  plus: the controller may send `START`, `RUN`, `ZERO`, `BLINK` and `SET` of `rate`,
  `hold_s`, `p_stop`, `p_run_min`. Controller gains, safety limits and the bench commands
  (`DARK`, `AMP`, `R`, `SCAN`, `PPG`, `VALVE TEST`, `TEST HANG`, `STREAM`, `RADIO`) are
  **USB serial only**.
- **Blocking commands no longer need the stream off** (only the serial stream). `ZERO`
  from the phone blocks the loop ~2.5 s; the board advances the sample counters across
  the pause and sends `stream_gap: ZERO ms=...`, so receivers see the gap as missing
  samples instead of a splice.
- **`RADIO 0|1`** (serial, IDLE) turns the radio off and on without a reboot, for the
  noise comparison below.
- The blue LED (GPIO2) shows the valve: lit = closed; `BLINK` flashes it.

Flash it like the simulator, with `expo_fw` in place of `expo_link_sim` (it needs the
SparkFun BMI270 library, as `irb_fw` does). It shares the simulator's `secrets.h`
settings (copy the file across after changing the password).

**Before visitors, with the rig wired** (serial at 921600, phone off the network):

| # | Do | Pass when |
|---|---|---|
| F1 | Boot | `sensors_ok=5/5`, `wifi ready`, `reset_reason=power_on` |
| F2 | `RADIO 0`, `DARK D`, `DARK P`, then `RADIO 1`, `DARK D`, `DARK P` | dark offsets agree within their usual spread |
| F3 | `STREAM 1` for 30 s with `RADIO 0`, then 30 s with `RADIO 1`, cuff vented | MPRLS noise (`p_raw` spread) the same both ways; no `ppg_overflow` |
| F4 | `VALVE TEST` with the radio on, a phone joined and watching | no reset (`INFO` uptime / `reset_reason` not `brownout`) |
| F5 | Phone: rows 2-13 of the simulator checklist, on a real arm | as there; the cuff really vents in rows 7 and 8 |

## How the link works

```
 sensor loop (core 1)                      core 0
 ┌──────────────────┐  whole lines   ┌───────────────┐  own read position,   ┌───────────┐
 │ samples, control ├──────────────► │ 16 KB ring    ├──► one sender task ──►│ phone     │
 │ safety, commands │                │ (shared)      ├──► per client ───────►│ laptop    │
 └────────▲─────────┘                └───────────────┘  one frame / 40 ms    └─────┬─────┘
          │ command queue (DUMP out of band)    HTTP server task ◄── frames ──────┘
          └─────────────────────────────────────────────────────
```

- **Same lines as serial.** Every client gets the IRB firmware's text lines (`S`, `SP`,
  `M`, `C`, `EV`, `E`, `#`) plus `ST`, a 2 Hz status line for the phone. About 12 kB/s.
- **The loop never waits on Wi-Fi.** It appends whole lines to a shared ring buffer; on the
  other core, one sender task per client batches them from that client's own read position
  into one WebSocket frame every 40 ms.
- **A slow or vanished device only hurts itself.** It falls behind on its own position.
  Once it is a whole ring (~1.3 s) behind, or a send to it blocks for 1 s, the board closes
  it (`ws_slow_closures`, `ws_send_failures`, `wifi_client_closed` event) and everyone else
  carries on without a gap. A closed phone reconnects by itself, but if it was the
  controller during INFLATE or HOLD, the cuff has already been dumped (controller lost).
- **Loss is never hidden.** Each stream keeps its own sample counter, so a receiver counts
  missing samples. Blocking commands report a `stream_gap` event.
- **DUMP skips the queue.** The server task flags it directly, so a full command queue
  cannot delay it.
- **No extra library.** ESP-IDF's HTTP server with WebSocket support is in the esp32 core
  (`CONFIG_HTTPD_WS_SUPPORT=y`); the laptop side is stdlib Python (`expotool/ws.py`).

## Control rules

| Who | May |
|---|---|
| Any device on the network | watch; **DUMP**; `INFO`, `P`, `ECHO <n>`; `CLAIM` |
| The controller (one at a time) | everything else: `START`, `RUN`, `SET`, `ZERO`, `BLINK`, `RELEASE` |
| USB serial | everything, as on the bench (`STREAM 1` for data on serial) |

- The phone page claims control when it connects. A second phone is refused while the first
  holds it. `CLAIM FORCE` takes it over, but only when no run is active.
- The controller must be heard from every 2 s (`CONTROLLER_TIMEOUT_MS`; the page pings every
  250 ms). If it disconnects or goes quiet **during INFLATE or HOLD of a run it started,
  the cuff is dumped** ("controller lost"). During DEFLATE the run finishes on the board's
  own limits and a warning is logged. A run started over USB serial is not tied to a phone.
- **Keep the phone screen on during a run.** A locked phone stops pinging, and that vents
  the cuff (by design). The bulb release screw stays the fallback when nothing else works.

## Layout

```
expo/
  firmware/expo_link_sim/   step 1: the link simulator (compiles; flash this first)
    expo_link_sim.ino       simulated session + sensors, control rules, commands
    wifi_link.h/.cpp        access point, page, WebSocket, ring buffer, per-client senders
    config.h                Wi-Fi, link and simulation settings
    secrets.example.h       copy to secrets.h (gitignored) and set the password
    web_page.h              GENERATED from web/index.html
  firmware/expo_fw/         step 2: real firmware (compiles; not run on hardware)
    expo_fw.ino             irb_fw + Wi-Fi output, control rules, ST line, stream gaps
    wifi_link.h/.cpp        COPY of the simulator's (a test keeps them identical)
    web_page.h              GENERATED, as above
  web/index.html            phone controls + display; ?view = laptop, watch only
  tools/embed_web.py        index.html -> web_page.h (run after editing the page)
  host/expo_receiver.py     laptop: watch and grade the link
  host/sim_server.py        laptop: stand-in for the board, no hardware
  host/expotool/            ws (WebSocket), linkstats, hub (model of the board's rules)
  tests/                    python -m pytest expo/tests
```

After editing `web/index.html`, run `python expo/tools/embed_web.py` and reflash. A test fails
if the header is out of date.

## Owed before the real system runs over Wi-Fi

- **Radio noise and brownout on the real sensors** (Step 2, rows F1-F5). The IRB firmware
  turns the radio off on purpose; nobody has measured what it does to this rig yet.
- **`expo_fw` on hardware at all.** It compiles; nothing else about it is verified.
