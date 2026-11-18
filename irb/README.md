# irb/: IRB session tool

Firmware and laptop tool for IRB human-subject sessions. The design is in
[`DESIGN.md`](DESIGN.md). This folder shares no code paths with `firmware/mapquest_ppg/`
or `host/hr_monitor.py`.

> **Hardware status (2026-11-10):** fw 0.2.1 is flashed to a bare WROOM-32D (ESP32-D0WD-V3,
> 4 MB, CP210x on COM7). With **nothing wired** it boots, reports `sensors_ok=0/5`, keeps the
> valve pin open, and `TEST HANG` resets through the watchdog (row 9 below passes). Every
> other row, and all phase 2 checks, need the mux, bands, MPRLS and valve wired and have
> **not** been run.
>
> Upload with `--board-options UploadSpeed=230400`: esptool hit one serial-noise error at the
> default 921600 upload speed. The ESP-IDF I2C driver also prints its own `E (…) i2c.master:`
> lines when a device NACKs; the host treats them as unknown lines and ignores them.

| Phase | State |
|---|---|
| 1 Firmware bring-up | **flashed (bare board): boot + watchdog (row 9) pass. Rows 1–8, 10 need wiring** |
| 2 Streaming, control, safety | **flashed (fw 0.2.1); no sensors or valve wired, checks not run** |
| 3 Host link + recorder | **done against a mock board: 37 tests pass** (`python -m pytest irb/tests`). Not yet run against the real board |
| 4 GUI | **done on the mock board**: full session walked through in the window; controller tests pass. On the real (bare) board it connects, streams, shows all five sensors red and refuses Start |
| 5 Bench verification | not started |

## Phase 1: hardware bring-up

### Flash

Arduino IDE 2.x, board **ESP32 Dev Module** (core `esp32:esp32`), open
`irb/firmware/irb_fw/irb_fw.ino`, upload. One library: **SparkFun BMI270 Arduino Library**
(Library Manager; 1.0.3 is what it was compiled against). It loads the BMI270's startup
config. The Serial Monitor runs at **921600** baud, with line ending set to Newline.

Compile check without the IDE:

```bash
"/c/Program Files/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe" compile --fqbn esp32:esp32:esp32 irb/firmware/irb_fw
```

### Wiring

Full diagram: [`docs/wiring.svg`](docs/wiring.svg) (regenerate with `python irb/docs/make_wiring.py`).

| ESP32 | Goes to |
|---|---|
| 3V3, GND | TCA9548A VIN/GND, MPRLS VIN/GND, and power for both bands from the same rail (the mux switches only SDA/SCL) |
| GPIO21 (SDA), GPIO22 (SCL) | TCA9548A SDA/SCL **and** MPRLS SDA/SCL (main bus) |
| TCA9548A SD0/SC0 | distal band: MAX30101 + BMI270 |
| TCA9548A SD1/SC1 | proximal band: MAX30101 + BMI270 |
| GPIO25 | 220 Ω → MOSFET gate, 10 kΩ gate → GND; valve between supply and drain |

Before powering the valve, check DESIGN §3.3: a flyback diode across the coil, and how
the coil is powered.

### Checklist

Do these in order with the cuff **vented**. Paste the output of each into the session
log.

| # | Send | Pass when |
|---|---|---|
| 1 | (reset the board) | boot prints `reset_reason=power_on` or `external_pin`, then `# ready: all 5 sensors found` |
| 2 | `SCAN` | main bus shows `0x18`. Channels 0 and 1 each show `0x57` and `0x68` (plus `0x18`, which is visible on every channel) |
| 3 | `INFO` | `mux_ok=1`, both bands `ppg_ok=1 part_id=0x15 imu_ok=1 imu_chip_id=0x24 imu_configured=1`, `pressure_ok=1 pressure_integrity=ok`, `watchdog_ok=1`, `sensors_ok=5/5` |
| 4 | `PPG D`, then `PPG P`, each with a finger on its sensor | IR and red well above dark (tens of thousands of counts), and they change when the finger lifts |
| 5 | `ZERO`, then `P` | `p_mmhg` within ±1 of 0 with the cuff vented |
| 6 | Pump the bulb in steps (~50, 100, 150 mmHg), sending `P` at each and reading the cuff gauge | `p_mmhg` matches the gauge within a few mmHg at each step |
| 7 | `VALVE TEST` (cuff vented, valve connected) | five audible clicks; the board does **not** reset (a `brownout` reset means the valve supply is sagging the rail) |
| 8 | `VALVE TEST` with the cuff pumped to ~100 mmHg | pressure holds while closed and falls while open, five times |
| 9 | `TEST HANG` | the board resets within ~1 s and boots with `reset_reason=task_watchdog` (or `panic`); the valve is open |
| 10 | Unplug one band, `R` | `INFO` names the missing sensor, `sensors_ok=3/5`. Plug back in, `R`, 5/5 |

Phase 1 is done when every row passes. Row 6 is not the span calibration in DESIGN §3.3;
that needs a reference gauge at 50–200 mmHg.

## Phase 2: streaming, session control, safety

### Commands

| Command | Allowed | Does |
|---|---|---|
| `INFO` | any time | settings, limits, sensor status, column lists |
| `STREAM 1` / `STREAM 0` | any time | data lines on / off. `STREAM 1` resets counters and prints `INFO` first |
| `START` | IDLE | closes the valve → INFLATE. Refused unless the cuff is zeroed and vented, the pressure sensor is fresh with no integrity flag, and both PPGs are OK |
| `RUN` | INFLATE | → HOLD for `hold_s`, then DEFLATE. Refused below `p_run_min` |
| `DUMP` | any time | opens the valve, reason `user` |
| `SET <key> <value>` | IDLE | `rate kp ki kd u0 hold_s p_stop p_run_min p_max hold_timeout_s`. `p_max` and `hold_timeout_s` can only go down from the compiled limits |
| `AMP <D\|P> <IR\|RED> <0-255>` | IDLE | LED current; logged as an `amp` event because counts jump |
| `ZERO`, `DARK <D\|P>`, `R`, `SCAN`, `PPG <D\|P>`, `VALVE TEST`, `TEST HANG` | IDLE and not streaming | blocking bench commands |
| `P` | any time | latest pressure |

### Lines from the board

| Line | Fields | Rate |
|---|---|---|
| `S` | `idx,t_us,ir,red,p_idx,p_raw,p_status`: distal PPG with the latest pressure; same columns as `data/hr_*.csv` | 100 Hz |
| `SP` | `idx,t_us,ir,red`: proximal PPG, its own counter | 100 Hz |
| `M` | `m_idx,t_us,band,ax,ay,az,gx,gy,gz`: raw BMI270 counts (8192 LSB/g, 65.536 LSB/dps) | 50 Hz per band |
| `C` | `c_idx,t_us,state,p_mmhg,pref_mmhg,rate_mmhg_s,duty_pct`: controller window | 10 Hz |
| `EV` | `t_us,name,detail`: state changes, dumps with reason, run summary, warnings. Sent even when not streaming | on event |
| `E` | message | on error |

`t_us` is the ESP32's `micros()`, shared by every line. It wraps after 71.6 min, so the host
unwraps it. A FIFO overflow advances `idx` past the lost samples (and says so), so
`idx / fs` stays true time.

### How the safety layer is built

- **Valve writes go through one spinlock.** The PWM close comes from an `esp_timer`
  callback that can run on the other core, so a close is only honoured while closing is
  allowed. A dump clears that permission and opens the valve in the same critical
  section, so a callback already in flight can't re-close it. Every loop pass also
  re-asserts "open" outside INFLATE, HOLD and DEFLATE.
- **Automatic dumps (every loop pass, during INFLATE / HOLD / DEFLATE):**
  - pressure: no good reading for 250 ms, the sensor lost, or its integrity flag set;
  - over-pressure: above `p_max`;
  - PPG: either band lost (3 I2C failures in a row) or silent for 500 ms;
  - hold timeout: valve closed and above 20 mmHg for longer than `hold_timeout_s`;
  - run timeout: 2 × the expected deflation time + 15 s.
- **IMU dropout** only sends a warning event.
- **A window with no good pressure reading in DEFLATE** opens the valve for that window.
  If it persists, the dropout dump follows.

## Phase 3: host link and recorder (`irb/host/irbtool/`)

| Module | Does |
|---|---|
| `protocol.py` | one typed record per line; malformed lines become `Unknown`, never an exception; gathers the INFO block; unwraps `t_us` |
| `trialinfo.py` | checks every trial field (coded subject IDs only, ranges, systolic > diastolic), AHA cuff-size warning, run IDs, inflation targets (DESIGN 2.1) |
| `recorder.py` | one folder per run (DESIGN 6), auto-suffixed run IDs, trial info written into every header on save, discard, `run.json`, the session log |
| `link.py` | serial reader thread at 921600; opening the port does not reset the board (DTR/RTS held released) |
| `model.py` | the live picture of the board for the GUI: state, pressure, sensor lights, unacknowledged faults, whether Start / Run are allowed |
| `mock.py` | a simulated board with the firmware's commands, state machine and dump reasons, plus `pump_to()` and `fail()` hooks |

What the tests cover: every line type, malformed lines, the `t_us` wrap, the INFO block, every
trial-info rule, cuff sizing and targets, run-ID suffixing, the folder layout, save / re-save /
discard, `distal.csv` loading with `host/mapquest/io.load_csv`, a full run on the mock
(INFLATE → HOLD → DEFLATE → DUMP → IDLE), the Run gate, Start refused on an un-vented cuff,
and every dump reason: user, pressure dropout, PPG dropout on each band, over-pressure and
hold timeout. They also check that IMU dropout only warns, that limits can only be
tightened, and that a lost link is reported.

The mock follows the firmware's rules but not its PI loop, so passing tests prove the host
side and the protocol contract, not the firmware.

## Phase 4: the session window (`irb/host/irb_session.py`)

```bash
python irb/host/irb_session.py --mock          # simulated board, no hardware
python irb/host/irb_session.py --port COM7     # the real board
```

- **Layout:** large cuff pressure (readable on the Doppler video), session clock, state
  banner, one light per sensor, plots of cuff pressure with the target ramp, distal relative
  blood volume (`host/mapquest/volume.py`, read-only) and |a| − 1 g for both IMUs. The
  proximal PPG is recorded, not plotted.
- **Buttons and keys:** Start · Run · DUMP (**Esc**, always, even while typing) ·
  **O** Doppler silent (INFLATE: target = p + 30, Run gate = p + 20, sent to the board) ·
  **D** Doppler flow back (live backup to the video) · Prepare LEDs (DARK on both bands,
  then auto-tune IR and red into 40–60 % of full scale) · Zero volume (**Z**).
- **Gates:** Start needs complete trial info, streaming, pressure and both PPGs OK and a
  vented cuff. Run needs INFLATE and pressure at or above the Run gate. Disabled buttons
  turn grey.
- **After a run:** recording continues 10 s past the dump, then the run waits UNSAVED for
  the after-run cuff reading. Save writes it into every file and steps the run ID; Discard
  asks first. A new run can't start until the last one is saved or discarded.
- **Faults:** an automatic dump raises a red banner that stays until acknowledged. A lost
  link says so and tells the operator the board finishes the run itself, with the bulb
  release screw as the fallback.
- **Closing the window** sends DUMP if a run is active, and asks before leaving a run unsaved.
- Before-start trial fields are remembered in `irb_data/.last_trial.json` (gitignored).

`irbtool/session.py` holds all of this logic without the GUI; `tests/test_irb_controller.py`
drives it on a virtual clock: full session and save, O/D marks, the Run gate, fault dump,
board-side Start refusal, LED preparation and link loss.

### Checks owed on hardware (after the phase 1 checklist)

| # | Do | Pass when |
|---|---|---|
| 1 | `STREAM 1` for 5 min, log it | no `E,` lines; `S` and `SP` idx each continuous at ~100 Hz; `M` ~50 Hz per band; `C` 10 Hz |
| 2 | Cuff on a rigid cylinder: `START`, pump to 160, `RUN` | HOLD 10 s, DEFLATE at 2.0 ± 0.2 mmHg/s (`run_summary`), dump at 40, back to IDLE |
| 3 | DESIGN §7 fault-injection table | each dump fires with its stated reason |

