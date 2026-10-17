# MAPQuest

Return-to-flow blood pressure prototype. An upper-arm cuff is bled down while a MAX30101 watches
the forearm; when cuff pressure drops below arterial pressure, blood flows in and cannot drain, the
forearm fills, and reflected light falls. **MAP is the cuff pressure at the onset of sustained
filling.**

Full requirements: [`docs/SPEC.md`](docs/SPEC.md). Channel choice: [`docs/led-selection-report.md`](docs/led-selection-report.md).

![Example output](docs/example-output.png)

*Synthetic recording, ground-truth MAP 90 mmHg, recovered at 89.9 mmHg.*

## Status

| Milestone | State |
|---|---|
| M0 Toolchain | firmware compiles for `arduino:esp32:nano_nora` |
| M1 PPG streaming | code complete; **run `bench_check.py` to close it out** |
| M2 Pressure | **MPRLS on the shared I2C bus** (D41); firmware 0.3 streams it with the PPG |
| M3 Logger | complete; pressure path untested |
| M4 Analysis | complete, validated against synthetic ground truth |
| M5 Tests | **37/37 passing** — SPEC §6 analysis criteria + protocol/mock-firmware |
| M6 Bench trials | not started |

Nothing here has touched real hardware yet. The analysis numbers come from synthetic data, and the
serial protocol is validated against a mock firmware (`tests/test_protocol.py`) rather than the
board. `bench_check.py` is the bridge: it runs the SPEC §3.5 acceptance criteria on real hardware.

## Hardware

| | |
|---|---|
| MCU | Arduino Nano ESP32 (ESP32-S3) |
| PPG | SparkFun SEN-16474 — MAX30101 Qwiic breakout |
| Pressure | Adafruit 3965 MPRLS ported breakout, 0–25 psi absolute, I2C 0x18 (D41) |

### Wiring (SPEC 2.2)

Both sensors share one I2C bus, wired in parallel on a breadboard:

| Nano ESP32 | MAX30101 | MPRLS |
|---|---|---|
| `3V3` | `3V3` | `VIN` |
| `GND` | `GND` | `GND` |
| `A4` | `SDA` | `SDA` |
| `A5` | `SCL` | `SCL` |

MAX30101 `INT` and MPRLS `3Vo`, `RST`, `EOC` stay unconnected. The MPRLS port tees into the
cuff hose. Without the MPRLS the firmware streams PPG only.

## Quick start

### 1. Flash

Open `firmware/mapquest_ppg/mapquest_ppg.ino` in Arduino IDE 2.x, select **Arduino Nano ESP32**,
upload. **No libraries to install** — the MAX30101 driver is inline.

At 115200 baud you should see a `#` metadata block then:

```
# ready — send START to stream, INFO for settings
```

### 2. Python

```bash
pip install -r host/requirements.txt
```

### 3. Verify the analysis without hardware

```bash
cd host
python synth.py --map 90 --out ../data/s90.csv
python analyze.py ../data/s90.csv
```

Should report onset within ±1 mmHg of 90. Then run the acceptance suite:

```bash
python -m pytest tests/ -v
```

### 4. Check the hardware

With the Nano and MAX30101 wired and flashed — no cuff or pressure sensor needed:

```bash
cd host
python bench_check.py --auto-tune --plot
```

This runs the SPEC §3.5 acceptance criteria as pass/fail: sensor found at 0x57, DARK offsets
≤128 counts, IR DC inside 105k–157k, output rate within 1% of 100 Hz, and zero dropped samples.
`--auto-tune` sets the LED currents for you; `--full` runs the 5-minute drop test that is the real
M1 gate. `--save` keeps the samples as CSV **and** the console report as `.log.txt`, with the
auto-tune DC-versus-current trajectory in the CSV header.

### 4b. Confirm the sensor sees blood

SPEC 3.5 can pass with the photodiode staring at a wall, so there is a separate
test for whether light actually reaches perfused tissue:

```bash
cd host && python bench_check.py --provocation --site wrist --plot --save
```

It cues you through raise / rest / lower cycles and reports whether IR DC moves
reversibly with arm position. Raising the arm drains venous blood, so counts should
**rise**; lowering it should drop them.

This is a better gate than hunting for a heartbeat: the measurement detects slow venous
filling, which is roughly 10x larger than the cardiac AC, so a site can be perfectly
viable while showing no resolvable pulse. The protocol rests before and after each
manoeuvre, so baseline drift cancels rather than faking a response.

### 5. Record

```bash
cd host
python logger.py --calibrate                      # HX710B only; the MPRLS needs no calibration run
python logger.py --subject T01 --trial 1 --note "left arm, seated"
python analyze.py ../data/<date>_T01_1.csv
```

The logger sends `INFO`, then `DARK`, then `START`, and writes a self-describing CSV. Ctrl-C sends
`STOP` and closes cleanly.

## PPG monitor — heart rate and blood volume

A standalone live display with two tabs, separate from the return-to-flow tools:

```bash
cd host && python hr_monitor.py --auto-tune --site finger
```

Shows heart rate coloured by signal quality, the band-passed waveform with every beat
marked, the HR trend, the live spectrum, perfusion, pulse SNR and HRV. A number appears
only when beat timing and the spectrum agree for 3 s — otherwise it shows why not. A
fingertip reads in ~11 s; the wrist works but is ~50× weaker. `--replay CAPTURE.csv`
plays any saved recording through it without the board. Details in D33.

The **Blood volume** tab plots two-wavelength and IR-only ΔV·L over time (relative µm,
+ = more blood), each channel's % change, and the rate of change against its noise floor,
with a FILLING / DRAINING / STABLE readout. **Z** zeroes the baseline, **M** drops a labelled
marker. One LED tuning serves both tabs; re-tuning breaks and re-zeroes the volume trace and
is refused while recording. Details in D34.

**Trial info.** Before each recorded cuff run, fill in the row under the toolbar: a run ID
(labels the files, e.g. `hr_wrist_20261015_150212_A-03.csv`, and steps to A-04 when you
stop), your reference blood pressure (systolic / diastolic, from a BP monitor) and the distance from the end
of the cuff to the sensor in cm. Record checks the values (bad ones block the recording with a
message, blank ones are allowed with a warning) and writes them into the recording header;
`analyze.py` then reports the onset against them. The fields lock while recording and are
remembered for the next run. Details in D42.

The toolbar shows a large **session clock** (and ● REC while recording). Until the pressure
sensor is fitted, film the cuff gauge and this screen together on a phone: every frame then
pairs a gauge reading with session time, so cuff pressure can be read back against the
recording with no clock sync. Put videos in `data/video/` (not tracked by git). Details in D38.

`gauge_video.py` reads the pressure off such a video. The first run writes a contact sheet of
the on-screen clock; read a few values off it and run again with them:

```
cd host
python gauge_video.py ../data/video/trial.mov --recording ../data/hr_wrist_X.csv
python gauge_video.py ../data/video/trial.mov --recording ../data/hr_wrist_X.csv \
    --clock "0.5=81.9,24.5=105.7,48.5=129.8,78.5=159.5"
```

It writes `<recording>_gauge.csv` (session time, mmHg) and `<recording>_gauge_check.png`, which
shows the needle as read on six frames, the tick calibration, and the pressure trace. Look at it
before trusting the numbers. A hand-held camera is fine; propped is better. Pressures are
provisional until the gauge's span is checked against a reference.

### Cuff trial figure

```
cd host
python cuff_trial.py ../data/hr_wrist_20261017_145914_test6.csv
python cuff_trial.py <csv> --deflation 64 --hold 50,64 --motion 80,84   # override windows
```

Writes `<recording>_cuff.png` and prints three return-of-flow markers with the cuff pressure
at each, against the reference BP if it was entered: **pulse return** (the heartbeat back
above three times its occluded ripple), **refill onset (hinge)** and **refill onset
(intersect)**. Windows come from the pressure trace, or from `data/cuff_windows.json` for
trials already checked by hand. Check them on the figure. Details in D43.

## Cuff pressure sensor test (MPRLS)

A standalone check that the Adafruit MPRLS (product 3965) tracks the cuff, before it joins
the PPG on the I2C bus. Unplug the PPG and wire the sensor to the same four Nano pins:
VIN to 3V3, GND to GND, SDA to A4, SCL to A5 (3Vo, RST and EOC unconnected). Tee its port
into the cuff hose.

Flash `firmware/mprls_test/mprls_test.ino`, then:

```
cd host
python pressure_monitor.py            # or --port COM5; --demo needs no hardware
```

Connect with the cuff valve **open**: the zero is taken 2 s after connecting (Z re-takes
it). The window shows cuff pressure, the deflation rate against the 2–3 mmHg/s target, and
the cuff's heartbeat oscillations, which confirm the tubing and sensor respond. R records
to `data/pressure_<stamp>.csv`. Details in D40.

## Layout

```
firmware/mapquest_ppg/
  mapquest_ppg.ino     self-contained MAX30101 driver + SPEC 3.4 protocol
  config.h             all tunables; echoed into every recording header
firmware/mprls_test/
  mprls_test.ino       standalone MPRLS cuff-pressure test (D40)
host/
  mapquest/
    io.py              serial protocol, metadata parsing, CSV load, calibration
    pipeline.py        raw -> dOD -> dV*L -> low-pass -> dV/dt -> onset
    pulse_return.py    heartbeat back above its occluded floor (D43)
    cuff.py            trial windows from the cuff pressure trace (D43)
  bench_check.py       SPEC 3.5 hardware acceptance checks, LED auto-tune
  provocation.py       arm-raise blood-volume response test
  hr_monitor.py        live PPG monitor: heart-rate + blood-volume tabs
  logger.py            recording + two-point pressure calibration
  analyze.py           offline analysis, figure, results.csv row
  cuff_trial.py        cuff trial figure: pulse return, refill onset, pressure (D43)
  synth.py             synthetic recordings with known ground truth
  gauge_video.py       cuff pressure read from a phone video of the gauge (D38)
  pressure_monitor.py  live cuff pressure from the MPRLS test firmware (D40)
tests/
  test_pipeline.py     SPEC section 6 analysis acceptance tests
  test_protocol.py     serial protocol against a mock firmware
  test_provocation.py  arm-raise response analysis
  test_pulse_return.py pulse-return detector on synthetic pulses
  test_cuff_trial.py   cuff trial windows and an end-to-end recorded trial
data/                  recordings (tracked, D29); data/video/ phone videos (ignored)
docs/
  SPEC.md              the requirements
  decisions.md         decision log — every deviation and why
  architecture.md      system + pipeline diagrams, living status
  led-selection-report.md  why Red + IR, with sources
```

## Configuration

Red + IR in SpO2 mode, 411 µs pulse width (18-bit), 400 sps internal with 4× hardware averaging →
**100 Hz output per channel**. Everything tunable is in `firmware/mapquest_ppg/config.h`.

Green is deliberately unused — the two-wavelength Beer–Lambert solve needs the 660/880 nm pair, and
green's 0.3–0.8 mm penetration would miss the forearm filling anyway. See the LED report.

## Decisions

Every non-obvious choice is recorded in [`docs/decisions.md`](docs/decisions.md), including the
four places this build deviates from `SPEC.md` (onset method, measurement site, and two
protocol additions) and the three open questions that gate whether the measurement
works at all.

## Two things to know before trusting a number

**Onset method.** `analyze.py` defaults to `--onset-method hinge` (D43): a fit of "occlusion
trend, then rising" whose corner is the onset. It agrees with the intersect described below on
a sharp ramp, and also handles a refill that starts softly, which a straight ramp fit reads
4–5 mmHg low. `--onset-method intersect` is the previous default, which is *not* SPEC §5.1
step 10 verbatim either. The spec's threshold-crossing method reads **~2.6 mmHg high**, because the 0.3 Hz
zero-phase low-pass and the 2 s Savitzky–Golay window are both symmetric kernels and smear the
ramp's corner backwards in time — so the derivative starts climbing before filling actually began.
That misses the spec's own ±1 mmHg acceptance criterion. The `intersect` method fits the
established ramp and extrapolates back to the occlusion baseline, which is unaffected by corner
smearing; it lands within ±0.2 mmHg on synthetic data. Both are always computed and both are
printed. Pass `--onset-method threshold` for literal spec behaviour. The ramp is extrapolated back to a
line fitted to the occlusion trend (`--baseline sloped`, default), because on a real arm blood
volume keeps draining while the cuff is up. Extrapolating to a flat level instead read 11 mmHg
low on the first trial with pressure, and 53 mmHg low with an earlier reference window (D39).

**Healthy subjects read near systolic, not MAP.** Teammates have a pulse, so filling begins when
the cuff drops below *systolic*. Bench trials validate the signal chain and detection, not MAP
accuracy — that needs a non-pulsatile phantom or an LVAD patient. The analysis prints a
pulsatility RMS and flags this automatically.

## Serial protocol (SPEC 3.4)

| Command | Action |
|---|---|
| `START` | begin streaming `S` lines, reset sample index |
| `STOP` | stop |
| `INFO` | print all settings as `#` metadata |
| `DARK` | LEDs off, average 200 samples, report dark offsets |
| `IRAMP <0-255>` | set IR LED amplitude (refused while streaming) |
| `REDAMP <0-255>` | set red LED amplitude |
| `TEMP` | one-shot die temperature |
| `R` | re-probe and reconfigure the sensor (not in SPEC §3.4; added so a wiring fix needs no reflash) |

Data lines: `S,<idx>,<t_us>,<ir>,<red>[,<p_idx>,<p_raw>]`. Errors: `E,<message>`.

`INFO` reports `sensor_ok=0|1` alongside the part id it **actually read**. An earlier version
echoed the expected `0x15` constant, so a disconnected sensor reported as present; `START` is now
refused when `sensor_ok=0` rather than streaming a plausible-looking flat line.

Use `idx / fs` as the timebase, not `t_us` — `t_us` is the host read time of a FIFO burst, so
several samples in one burst share it. It is there for jitter diagnostics only.

## If a capture freezes or drops

The ESP32-S3's native USB CDC can re-enumerate under load. Captures that die partway are
kept and analysed, with the failure reported. If the board vanishes entirely, wait a few
seconds before retrying — it is probably still re-enumerating, and `find_port` will say so.

Most common cause by a wide margin is the cable: charge-only and thin cables enumerate fine
and then drop under sustained traffic. Then USB hubs, then another program holding the port
(Arduino IDE's Serial Monitor and Serial Plotter both do).

## Safety

Bench experiment, **not a medical device**. Occlusion ≤ 2 min per trial, 2–3 min rest between.
Stop immediately for pain, numbness or tingling. Exclude anyone on anticoagulants or with vascular
disease or fragile skin. Full protocol in SPEC §8.
