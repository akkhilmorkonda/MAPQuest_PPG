# expo/: wireless prototype for the exposition

The ESP32 makes its own password-protected Wi-Fi network. An **iPad** joins it to run the
cuff and show the visitor the run; a **laptop** joins it to analyse each run and drive the
monitor. This folder
shares no code paths with `irb/` or `host/` (it reuses `irb/host/irbtool` read-only, the way
`irb/` reuses `host/mapquest`).

> **2026-11-23: the iPad replaces the phone.** The board serves the dashboard to the iPad
> with an operator panel (Start, Deflate, Stop, Zero, connections) and a Run/Technical
> switch of its own; the technical traces come from the laptop over the board's Wi-Fi.
> Desk-tested on the stand-in, not flashed. The phone page and the USB phone bridge are gone;
> the notes below from 2026-11-18 are kept as the history of the link.
>
> **Status (2026-11-18).** Step 1, the **link simulator**, is built: it runs on a bare
> ESP32 with nothing wired and streams a simulated session at the real system's rates.
> The phone page, laptop receiver, results manager and control rules pass **74 tests**
> against a Python model of the board (`python -m pytest expo/tests`). **Flashed to the WROOM on COM7 on
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
>
> **Results manager and the real phone page (2026-11-18, not flashed):** the laptop process
> `host/expo_results.py` turns each run into a return-of-flow pressure with `host/mapquest`
> and posts it through the board; the phone page now follows DESIGN §5. On the stand-in a
> full run read **121 mmHg against the model's 120** (flag ok), in a browser and in an
> end-to-end socket test. Nothing of this has met a real arm.
>
> **Monitor dashboard (2026-11-18):** the chosen public and technical screens, served on the
> laptop by `expo_results.py` at `http://127.0.0.1:8800/`. Checked in a browser on the
> stand-in through idle, live, result and safety stop; **78 tests** in all.

| Step | State |
|---|---|
| 1 Link simulator on the ESP32 (no wiring) | **flashed; first phone + laptop link test passed.** Dead-client stall fixed in source, not flashed; rows 6, 8 and 13 owed |
| 2 Real firmware over Wi-Fi (`expo_fw`) | **compiles** (IRB `irb_fw` 0.2.1 + Wi-Fi link + control rules); not flashed, needs the wired rig |
| 3 Results manager (`host/expo_results.py`) | **built, tested on the stand-in**; thresholds marked provisional need team dry runs |
| 4 iPad page (the dashboard in board mode) | **built** (operator panel + run and technical screens); desk-tested on the stand-in, not flashed. Replaced the phone page 2026-11-23 |
| 5 Presentation dashboard | **built** (the chosen B3 public + B3c technical screens), served by `expo_results.py`; tested on the stand-in only |

## Network

The board is a WPA2 access point. Name and password live in
`firmware/expo_link_sim/secrets.h`, which **git ignores**. A random password was generated
there on 2026-11-18; open the file to read or change it (8-63 characters). On a fresh
clone, copy `secrets.example.h` to `secrets.h` first, or the sketch refuses to compile.

| | |
|---|---|
| Network | `MAPQuest-Expo` (WPA2) |
| iPad (controls + screens) | `http://192.168.4.1/` |
| Laptop | `expo_results.py` (analysis; monitor at `http://127.0.0.1:8800/`), or `expo_receiver.py` |
| Channel | 6 (`WIFI_CHANNEL` in `config.h`; use 1, 6 or 11, whichever is quietest at the venue) |
| Transmit power | 8.5 dBm (`WIFI_TX_POWER_QDBM`): booth range, smaller current spikes on the 3V3 rail |

The network has no internet. iPads and iPhones show "No Internet Connection" and stay joined; Android
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
| 2 | iPad: join the network, open `http://192.168.4.1/` in landscape | panel shows **Board live** and **Control this iPad**; the sensors; pressure 0 (**Laptop** is green only with `expo_results.py` running) |
| 3 | Laptop: join the network, run `expo_results.py`, open `http://127.0.0.1:8800/` | iPad panel: **Laptop linked**; the monitor shows the same numbers |
| 4 | iPad: **Start** | pressure climbs to 165 on both screens; **Deflate** unlocks at 150 |
| 5 | **Deflate** | Holding 10 s, then the release at 2 mmHg/s; at 40 the cuff vents and the result shows on both screens |
| 6 | Start again, then **Stop** while inflating | vents at once; both screens show "Run stopped" |
| 7 | Start, then lock the iPad or turn its Wi-Fi off | within ~2 s the cuff vents; the monitor shows the safety stop "The operator’s iPad disconnected" |
| 8 | Unlock the iPad | it reconnects and takes control again by itself |
| 9 | **Zero** (blocks the board for 2.5 s, like the real ZERO) | the iPad keeps control; the technical view's event log shows `stream_gap ZERO` |
| 10 | iPad: **Technical**, then **Run** | only the iPad switches; the monitor stays on its view |
| 11 | Laptop: `python expo/host/expo_receiver.py --seconds 120` while the iPad runs a session | `RESULT: PASS` |
| 12 | Walk the iPad to the far side of the booth | technical view, Link and events: missing stays 0 |
| 13 | Turn the **laptop's** Wi-Fi off | iPad: **Laptop offline**, no freeze; event log shows `wifi_client_closed` within ~2 s |

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

Then open `http://127.0.0.1:8765/` (the iPad page), or
`python expo/host/expo_receiver.py --url ws://127.0.0.1:8765/ws`. `--lan` lets an iPad on the
same network reach it. Round trips here are localhost numbers, not radio numbers.

## Step 2: the real firmware (`expo_fw`)

The IRB firmware with the Wi-Fi link added. Sensors, valve, PI deflation control, the
watchdog and the safety layer are the IRB code unchanged (a test checks the safety limits
still match `irb_fw`). What is new:

- **The radio is on.** The IRB firmware turns it off; this one starts the access point at
  boot, before the watchdog, with the valve open. Bluetooth stays off.
- **Data goes to Wi-Fi whenever a device is connected.** Serial gets data lines only after
  `STREAM 1`, as on the bench. Sample counters run from boot and are never reset.
- **Only the distal (forearm) band is required** (`REQUIRE_PROXIMAL_BAND = false` in
  `config.h`, since 2026-11-18). The result reads the distal band only, so START needs
  pressure and the distal PPG; the upper-arm band still shows on the iPad and monitor
  when present, and losing it mid-run sends one warning instead of dumping the cuff. Set
  it to `true` when a model that uses the proximal band is in.
- **The valve is closed except to deflate and to dump** (since 2026-11-20). INFLATE and
  HOLD close it as before; in IDLE it is held closed (energised) once the cuff has read
  below 10 mmHg for 3 s, so it closes again 3 s after every run, dump, zero or valve test.
  It opens in IDLE, by the same path as a dump, whenever the pressure sensor is missing,
  unzeroed, stale or flagged, or the cuff holds more than 20 mmHg outside a run
  (`IDLE_VENT_ABOVE_MMHG`, `IDLE_CLOSE_SETTLE_MS`; `EV,valve` lines). A reset, crash or
  power loss still vents: the pin goes low.
- **Automatic LED level** (since 2026-11-23): `R` (the bench GUI's Refresh)
  and the serial command `LEVEL [D|P]` set each band's IR and red LED current so its
  light sits near half scale (0.8–25.4 mA; it warns when a band stays dim at the top
  current, i.e. is probably not on the skin). Never at Start: a change between the
  pre-roll and the hold would skew the "flow fully stopped" check. So: fit the bands,
  Refresh, then Start.
- **A missing pressure sensor is found again by itself**: in IDLE the board probes for it
  every 2 s and, when it answers, brings it up and zeroes the cuff (the valve has been
  open while it was missing). If the mux was lost too, every sensor is brought up.
- **A band plugged in while the board runs is found by itself** (since 2026-11-23): in IDLE
  each missing PPG, and the IMU of a working band, gets one silent address probe every 3 s;
  one that answers is brought up and LED-levelled (`band_found` event). Not while streaming
  on USB (the bench GUI's Refresh covers that). The iPad's **Refresh** sends `R` at once.
- **IMU at 200 Hz, ±2 g** (was 50 Hz, ±4 g), read from the BMI270's FIFO so no sample
  is missed: 16384 counts = 1 g. Meant for trying to sense the pulse mechanically; the
  "arm moved" check uses the gyro (unchanged, ±500 °/s).
- **The iPad gets the session commands only.** Control rules as in the link simulator,
  plus: the controller may send `START`, `RUN`, `ZERO`, `BLINK`, `R` (re-probe every
  sensor) and `SET` of `rate`, `hold_s`, `p_stop`, `p_run_min`.
  Controller gains, safety limits and the bench commands (`DARK`, `AMP`, `SCAN`, `PPG`,
  `VALVE TEST`, `TEST HANG`, `STREAM`, `RADIO`) are **USB serial only**.
- **Blocking commands no longer need the stream off** (only the serial stream). `ZERO`
  from the iPad blocks the loop ~2.5 s; the board advances the sample counters across
  the pause and sends `stream_gap: ZERO ms=...`, so receivers see the gap as missing
  samples instead of a splice.
- **`RADIO 0|1`** (serial, IDLE) turns the radio off and on without a reboot, for the
  noise comparison below.
- The blue LED (GPIO2) shows the valve: lit = closed; `BLINK` flashes it.

Flash it like the simulator, with `expo_fw` in place of `expo_link_sim` (it needs the
SparkFun BMI270 library, as `irb_fw` does). It shares the simulator's `secrets.h`
settings (copy the file across after changing the password).

### Two boards

`expo_fw` builds for either board; `config.h` picks the pins from the board selected.

| | ESP32 Dev Module (WROOM-32D) | Seeed XIAO ESP32S3 (the rig since 2026-11-18) |
|---|---|---|
| FQBN | `esp32:esp32:esp32` | `esp32:esp32:XIAO_ESP32S3` |
| I2C (mux) | SDA GPIO21, SCL GPIO22 | SDA **D4** (GPIO5), SCL **D5** (GPIO6) |
| Valve MOSFET gate | GPIO25 | **D3** (GPIO4) |
| Pressure (MPRLS) | main bus, beside the mux | **mux channel 7** (SD7/SC7) |
| Bands | mux ch0 distal, ch1 proximal | the same |
| Status LED | GPIO2 | GPIO21 (lit when low) |
| Serial | USB bridge, 921600 baud | the chip's own USB (any baud); writes are dropped, never waited on, when no computer is reading |

Build and upload in **one** command per board. The two builds share a cache folder, so a
plain `upload` after building for the other board sends the wrong image:

```powershell
& "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe" compile --fqbn esp32:esp32:XIAO_ESP32S3 --upload --port COM8 expo/firmware/expo_fw
```

**First run on the XIAO (2026-11-18),** mux plus the distal breakout only: mux at 0x70,
MAX30101 and BMI270 on ch0, `sensors_ok=2/5` (no proximal band, no pressure sensor yet,
so Start stays locked). With `STREAM 1`: S 100.0, M 50.8, C 10.0, ST 2.0 lines/s and no
overflow. One `ppg_overflow` at boot (the FIFO fills during Wi-Fi start-up), before any run.

That first run found a **PPG FIFO bug** in the code inherited from `irb_fw`: a full FIFO
looked nearly empty (its pointers can't show "full"), so it was drained one sample at a
time while samples kept being lost, and the sample counter ran at twice true time. Now an
overflow reads the whole FIFO out (only a read clears the chip's overflow counter), counts
it as one reported gap, and draining resumes. `irb_fw` still has the bug.

`firmware/xiao_bus_test/` is a standalone check of a XIAO's bus: line levels, a scan of the
main bus and every mux channel, the PPG streaming 1 s at 100 Hz and the IMU's chip ID, then
`RESULT: PASS/FAIL`. It drives no valve. Flash it, open the serial monitor, send any line.

**Before visitors, with the rig wired** (serial at 921600, iPad off the network):

| # | Do | Pass when |
|---|---|---|
| F1 | Boot | `sensors_ok=5/5`, `wifi ready`, `reset_reason=power_on` |
| F2 | `RADIO 0`, `DARK D`, `DARK P`, then `RADIO 1`, `DARK D`, `DARK P` | dark offsets agree within their usual spread |
| F3 | `STREAM 1` for 30 s with `RADIO 0`, then 30 s with `RADIO 1`, cuff vented | MPRLS noise (`p_raw` spread) the same both ways; no `ppg_overflow` |
| F4 | `VALVE TEST` with the radio on, the iPad joined and watching | no reset (`INFO` uptime / `reset_reason` not `brownout`) |
| F5 | iPad: rows 2-13 of the simulator checklist, on a real arm | as there; the cuff really vents in rows 6 and 7 |

## Results: the laptop process (`host/expo_results.py`)

Leave it running on the laptop for the whole session, joined to the board's network:

```powershell
python expo/host/expo_results.py
```

It watches like any other device (it never takes control), announces itself with `ANALYSER`
so the iPad's **Laptop** light comes on, and for every run (expo/DESIGN.md §7):

1. **Records it in memory:** from START to the vent, plus 15 s of stream before it (the
   uncuffed pulse).
2. **Numbers it** once it reaches DEFLATE. A run vented before that is not a run.
3. **When it is dumped at `p_stop`,** posts `RESULT <n> - pending` (the iPad shows
   "Analysing…"). When the cuff has vented, runs `host/mapquest`'s `derive()` **unchanged**,
   with the windows taken from the board's own state events instead of guessed from the
   pressure trace (§7.2), and posts `RESULT <n> <mmHg> <flag>`.
4. **Grades it** (§7.4): the headline is the **refill onset (hinge)**; intersect and pulse
   return are computed for the technical view.

| Flag | When | Result |
|---|---|---|
| `ok` | | "Return of flow: 121 mmHg (run 4)" |
| `incomplete` | missing samples, a `stream_gap`/overflow, the link lost, or the light sensor on a rail for > 1 % of the deflation | number + "signal incomplete" |
| `arm_moved` | the validated PPG motion detector (`mapquest/quality.py`) or the IMUs (> 20 °/s) between deflation and 5 s after the onset | number + "arm moved" |
| `pulse_present` | the pulse during the hold was > 50 % of the uncuffed pulse | number + "flow may not have fully stopped" |
| `no_onset` | no hinge onset | "no clear reading, try another run" |
| `stopped` / `safety_stop` | vented by DUMP / by the board before `p_stop` | no number |

5. **Keeps one anonymous row** in `expo/results/expo_results.csv` (gitignored): run, time,
   mmHg, flag, reason. The run's signals are dropped once it is analysed. Run numbers carry
   on across restarts.

**Provisional:** the 20 °/s IMU limit and the 50 % pulse ratio are first guesses, not bench
numbers. Team dry runs on the expo cycle should set them (DESIGN §10), and that is what
`--save-raw DIR` is for: it keeps each run's raw lines. **Team only, never with visitors.**
`--replay FILE` re-analyses a saved run, an `expo_receiver.py --save` capture or a serial
log, and writes no results unless `--results` is given.

On the laptop stand-in (no board), at three times real speed:

```powershell
python expo/host/sim_server.py --speed 3
```

```powershell
python expo/host/expo_results.py --url ws://127.0.0.1:8765/ws --results expo/results/sim_results.csv
```

Then the iPad page at `http://127.0.0.1:8765/` and the monitor at `http://127.0.0.1:8800/`.
The stand-in's arm has a systolic of 120 mmHg, so a run should read about 120.

## The monitor dashboard (`web/dashboard.html`)

`expo_results.py` serves it on the laptop itself: open **`http://127.0.0.1:8800/`** in the
laptop's browser, full screen (F11) on the monitor. The monitor never talks to the board:
the results process relays the board's stream to it and adds each run's number and result,
with the analysis traces. It draws the chosen designs from the dashboard canvas at a fixed
1600 × 900, scaled to the screen:

| Mode | Screens |
|---|---|
| **Public** | idle ("Next up: you"); live run (stepper, pressure, the run as a timeline, "Now / Watch for / time left"); outcome (the number with "Blood is back" marked, a caution tag, "No clear reading", "Safety stop: the cuff released itself", or "Run stopped") |
| **Technical** | pipeline strip (raw light → ΔOD → blood volume live; low-pass, dV/dt and the onset after the vent), the onset close-up against cuff pressure (hinge fit, sloped trend, pulse return, intersect), confidence checks, heartbeat vs its stopped level, arm motion, cuff controller, safety margins, hardware, link and events |

- **Switch modes** with **T**, Page Up/Down or the arrow keys (a presentation clicker works),
  the last press wins and every new run starts on public. (The iPad's own switch changes
  only the iPad.)
  That key press is the **only** thing the dashboard can send, and it can't touch the cuff.
- The outcome stays up for 2 minutes, then the idle screen returns (or at the next Start).
- Opened late, or reloaded, it catches up on the last two minutes at once.
- **The iPad (2026-11-23, desk-tested on the stand-in, not flashed):** the board now serves
  this same page at `http://192.168.4.1/`, marked `data-host="board"` by `tools/embed_web.py`.
  That adds an operator panel under the screen: **Zero**, **Start**, **Deflate** (RUN: hold
  10 s, then the release; unlocks at p_run_min), **Stop** (DUMP, always enabled), the
  connections (board, laptop, pressure, both PPGs, both IMUs, control), one line saying what
  to do next or why Start is locked, and a **Run / Technical** switch that changes the iPad
  only. The iPad takes control when it connects and pings every 250 ms. The analysis and its
  traces come from the laptop (below); the board's own `result` event still shows the number
  if the laptop can't be reached. iPad set-up: join the board's Wi-Fi, open
  `http://192.168.4.1/` in landscape, set Auto-Lock to Never and use Guided Access.
- The laptop side: the server also listens on the board's Wi-Fi
  (`--dash-host`, default `0.0.0.0`; `127.0.0.1` keeps it on the laptop), so the iPad page can
  read the technical traces from it. Those connections are **read-only**: only a browser on the
  laptop itself may switch the mode. `expo_results.py` announces the port with `ANALYSER 8800`
  and prints the address the iPad will use (`iPad technical view reads ws://…:8800/ws`).
  The board pairs that port with the laptop's own address, read from its socket (never one
  it is sent), and repeats it as `analyser_url=ws://…:8800/ws` in INFO and in the
  `analyser … joined` event; it is cleared when the laptop goes quiet. The board also serves
  the page's files (`/logo.webp`, `/fonts/*`) from flash: `tools/embed_web.py` writes them
  into each sketch's `web_assets.h` (the dashboard server's `ASSETS` list, ~140 KB).
  Windows asks once whether Python may accept connections: allow it, including on **Public**
  networks (the board's network shows up as Public), or the iPad can't reach it.
- **Offline:** no web fonts or CDN (the laptop is on the board's network). Its fonts
  (Bricolage Grotesque, Fredoka for the wordmark) ship in `web/fonts/` and
  are served by the results process.
- `--dash-port 0` turns it off.

## The iPad page (`web/dashboard.html`, board mode)

The board serves the dashboard to the iPad at `http://192.168.4.1/`, marked
`data-host="board"`. The visitor's screen is the same as the monitor's; under it is a dark
operator panel (2026-11-23; it replaced the phone page):

- **Buttons:** **Zero** (IDLE), **Start** (locked with one reason until it may run),
  **Deflate** (RUN: hold 10 s, then the release; unlocks at the Run threshold, 150),
  **Stop** (DUMP: always enabled, vents the cuff now).
- **One line of guidance:** why Start is locked; "Recording the resting pulse: keep the arm
  still (n s)" before a run (the "flow fully stopped" check needs 4 s of it); "Pump to 160 ·
  Deflate unlocks at 150"; "Above 190 mmHg: let some air out"; the hold and the release.
- **Connections:** board, laptop, pressure (and zero), forearm and upper-arm light, both
  IMUs, and who has control (**Take** claims it back, never during a run). The upper-arm
  band and the IMUs show grey "not fitted" when absent: they never block a run.
- **Run / Technical:** switches the iPad only. The technical traces come from the laptop.
- Board errors (a refused Start, a lost sensor) show in red for 8 s.

The run profile is the expo one: pump to **160**, Deflate unlocks at **150** (the firmware
default), cap **190**, hold **10 s**. No settings on the iPad: change them over USB serial.

## How the link works

```
 sensor loop (core 1)                      core 0
 ┌──────────────────┐  whole lines   ┌───────────────┐  own read position,   ┌───────────┐
 │ samples, control ├──────────────► │ 16 KB ring    ├──► one sender task ──►│ iPad      │
 │ safety, commands │                │ (shared)      ├──► per client ───────►│ laptop    │
 └────────▲─────────┘                └───────────────┘  one frame / 40 ms    └─────┬─────┘
          │ command queue (DUMP out of band)    HTTP server task ◄── frames ──────┘
          └─────────────────────────────────────────────────────
```

- **Same lines as serial.** Every client gets the IRB firmware's text lines (`S`, `SP`,
  `M`, `C`, `EV`, `E`, `#`) plus `ST`, a 2 Hz status line for the iPad. About 12 kB/s.
- **The loop never waits on Wi-Fi.** It appends whole lines to a shared ring buffer; on the
  other core, one sender task per client batches them from that client's own read position
  into one WebSocket frame every 40 ms.
- **A slow or vanished device only hurts itself.** It falls behind on its own position.
  Once it is a whole ring (~1.3 s) behind, or a send to it blocks for 1 s, the board closes
  it (`ws_slow_closures`, `ws_send_failures`, `wifi_client_closed` event) and everyone else
  carries on without a gap. A closed iPad reconnects by itself, but if it was the
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
| Any device on the network | watch; **DUMP**; `INFO`, `P`, `ECHO <n>`; `CLAIM`, `RELEASE`; the display commands below |
| The controller (one at a time) | `START`, `RUN`, `SET`, `ZERO`, `BLINK`, `R` (`expo_fw`: bench commands stay on serial) |
| USB serial | everything, as on the bench (`STREAM 1` for data on serial) |

**Display commands** never touch the cuff (no valve, settings or session state); the board
keeps the latest values, repeats them in `ST` (`view`, `analyser` columns) and in `INFO`
(`# view=`, `# result `), so a screen that reconnects comes back in step:

| Command | Effect |
|---|---|
| `VIEW PUBLIC` / `VIEW TECHNICAL` | the monitor mode; `EV,view`; reset to public when START is accepted |
| `RESULT <run> <mmHg\|-> <flag>` | the latest result, from the laptop; `EV,result,run=N mmhg=V flag=F` |
| `ANALYSER` | this client is the results process; lost like a controller (2 s of silence) |

- The iPad page claims control when it connects. A second device is refused while the first
  holds it. `CLAIM FORCE` takes it over, but only when no run is active.
- The controller must be heard from every 2 s (`CONTROLLER_TIMEOUT_MS`; the page pings every
  250 ms). If it disconnects or goes quiet **during INFLATE or HOLD of a run it started,
  the cuff is dumped** ("controller lost"). During DEFLATE the run finishes on the board's
  own limits and a warning is logged. A run started over USB serial is not tied to a device.
- **Keep the iPad screen on during a run** (Auto-Lock Never, Guided Access). A locked iPad stops pinging, and that vents
  the cuff (by design). The bulb release screw stays the fallback when nothing else works.

## Layout

```
expo/
  firmware/expo_link_sim/   step 1: the link simulator (compiles; flash this first)
    expo_link_sim.ino       simulated session + sensors, control rules, commands
    wifi_link.h/.cpp        access point, page, WebSocket, ring buffer, per-client senders
    config.h                Wi-Fi, link and simulation settings
    secrets.example.h       copy to secrets.h (gitignored) and set the password
    web_page.h, web_assets.h GENERATED from web/ (the iPad page, logo, fonts)
  firmware/expo_fw/         step 2: real firmware (compiles; not run on hardware)
    expo_fw.ino             irb_fw + Wi-Fi output, control rules, ST line, stream gaps
    wifi_link.h/.cpp        COPY of the simulator's (a test keeps them identical)
    web_page.h              GENERATED, as above
  web/dashboard.html        the screens: the monitor (served by host/expo_results.py) and,
                            in board mode, the iPad's page with the operator panel
  tools/embed_web.py        dashboard.html -> web_page.h, logo + fonts -> web_assets.h (run after editing web/)
  host/usb_bridge.py        SerialLink: the board's USB serial, for host/bench_gui.py
  host/expo_results.py      laptop: the results manager (run -> return-of-flow pressure)
  host/expo_receiver.py     laptop: watch and grade the link
  host/sim_server.py        laptop: stand-in for the board, no hardware (--speed N)
  host/expotool/            ws (WebSocket), linkstats, hub (model of the board's rules),
                            results (recorder, analysis adapter, flags, results file),
                            dashserve (the dashboard's local server and relay)
  results/                  expo_results.csv, anonymous (gitignored)
  tests/                    python -m pytest expo/tests
```

After editing `web/dashboard.html`, run `python expo/tools/embed_web.py` and reflash. A test fails
if the header is out of date.

## Owed before the real system runs over Wi-Fi

- **Radio noise and brownout on the real sensors** (Step 2, rows F1-F5). The IRB firmware
  turns the radio off on purpose; nobody has measured what it does to this rig yet.
- **`expo_fw` on hardware at all.** It compiles; nothing else about it is verified.
