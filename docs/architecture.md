# MAPQuest — Architecture & Signal Pipeline

Living document. Updated as the system changes; see the status table at the bottom for what
is verified on hardware versus only on synthetic data. The reasoning behind each choice
lives in [`decisions.md`](decisions.md).

Last updated: 2026-10-09 — first real cuff occlusion (O3, O5); earlier: rolling noise band for blood volume (D35) and sliding-window heart rate (D36), after the first live run of the monitor. Earlier the same day, after the first graded run — added coupling-slip detection (D31),
demoted harmonics to informational (D30), and opened O4 (red is contact-sensitive).

---

## 1. End-to-end system

```mermaid
flowchart TD
    subgraph SUBJECT["Subject"]
        CUFF["Upper-arm cuff<br/>manual bulb, bled at ~2 mmHg/s"]
        TISSUE["Distal tissue — WRIST<br/>(SPEC says forearm; see D18)<br/>fills when cuff drops below<br/>arterial pressure"]
        CUFF -->|occludes| TISSUE
    end

    subgraph SENSORS["Sensors"]
        MAX["MAX30101 — SparkFun SEN-16474<br/>Red 660 nm + IR 880 nm<br/>green unused (D25)<br/>reflectance, I2C 0x57, 18-bit"]
        HX["MPS20N0040D + HX710B<br/>0-300 mmHg, 24-bit<br/>NOT YET FITTED"]
        WHOOP["Whoop<br/>independent heart-rate<br/>reference (D26)"]
    end

    TISSUE -->|"reflected light<br/>(more blood = fewer counts)"| MAX
    CUFF -.->|air pressure| HX
    TISSUE -.->|"heart rate,<br/>read by operator"| WHOOP

    subgraph FW["Firmware — Arduino Nano ESP32 (ESP32-S3)"]
        CFG["config.h<br/>SpO2 mode, 411 us PW<br/>400 sps / 4x avg = 100 Hz"]
        DRV["Inline MAX30101 driver<br/>no external libraries (D5)"]
        FIFO["FIFO drain<br/>32-deep, 6 B/sample<br/>overflow reported as E lines"]
        CMD["Command handler<br/>START STOP INFO DARK<br/>IRAMP REDAMP TEMP R"]
        CFG --> DRV --> FIFO
    end

    MAX -->|I2C 400 kHz| DRV
    HX -.->|"bit-bang, 27 pulses"| FIFO

    FIFO -->|"S,idx,t_us,ir,red"| LINK
    CMD <-->|commands| LINK
    LINK["USB CDC serial<br/>non-blocking reads + watchdog (D23)"]

    subgraph HOST["Host — Python"]
        IO["mapquest/io.py<br/>protocol, metadata, link health"]
        QUAL["mapquest/quality.py<br/>motion flagging (D27)"]
        BENCH["bench_check.py<br/>acceptance, --settle,<br/>--graded, --whoop-bpm"]
        PROV["provocation.py<br/>arm-position protocols,<br/>dose-response fit"]
        LOG["logger.py<br/>recording + pressure calibration"]
        PIPE["mapquest/pipeline.py<br/>return-to-flow analysis"]
        ANA["analyze.py<br/>onset + figure + results row"]
        HRE["mapquest/heartrate.py<br/>streaming HR estimator (D33)"]
        HRM["hr_monitor.py<br/>live PPG monitor:<br/>heart-rate + blood-volume tabs"]
        VOLT["mapquest/volume.py<br/>streaming blood volume (D34)"]
        VOLT --> HRM
        IO --> BENCH
        IO --> PROV
        IO --> LOG
        IO --> HRM
        HRE --> HRM
        QUAL --> PROV
        LOG --> PIPE
        PIPE --> ANA
    end

    LINK --> IO
    WHOOP -.->|"--whoop-bpm"| BENCH

    subgraph OUT["Outputs — hardware captures tracked in git (D29)"]
        CSV["data/*.csv<br/>full firmware header,<br/>LED amps, die temp, auto-tune curve"]
        LOGS["data/*.log.txt<br/>full console report"]
        PNG["Figures"]
    end

    BENCH --> CSV
    BENCH --> LOGS
    PROV --> CSV
    PROV --> PNG
    LOG --> CSV
    ANA --> PNG

    classDef missing fill:#ffe6e6,stroke:#c43b2f,stroke-dasharray:4 3
    classDef external fill:#eef3fb,stroke:#1f4e79
    class HX missing
    class WHOOP external
```

---

## 2. Blood-volume measurement — the core of the system

The current focus (D26). Everything else in MAPQuest is built on this signal being right.
Accuracy target is **relative and monotonic**: proportional to true volume change and
repeatable, with an arbitrary absolute scale.

```mermaid
flowchart TD
    STRAP(["Sensor strapped to wrist"]) --> SETTLE["<b>Settle</b><br/>--settle 45<br/>contact pressure stabilises<br/><i>12x lower noise floor (O1/O3)</i>"]
    SETTLE --> RAW["<b>Raw counts</b><br/>IR + red, 18-bit, 100 Hz"]
    RAW --> MOT{"<b>Motion flag</b> (D27)<br/>rate > 8 sigma<br/>AND > 2 % of DC per second"}
    MOT -->|flagged| EXCL["Excluded from analysis<br/><i>never interpolated —<br/>invented data is worse than a gap</i>"]
    MOT -->|clean| SLIP{"<b>Coupling slip?</b> (D31)<br/>level jump > 1% between<br/>1 s windows, mid-hold"}
    SLIP -->|yes| EXCL2["Comparisons straddling<br/>the slip excluded<br/><i>sensor moved, not blood</i>"]
    SLIP -->|no| DARK["Dark subtraction"]
    DARK --> OD["<b>delta-OD</b><br/>ln(ref / counts) per wavelength"]
    OD --> DV["<b>Blood volume, relative</b><br/>dV·L = 229·dOD660 + 1555·dOD880<br/><i>L unknown, so relative µm (D26)</i><br/><i>red is contact-sensitive — O4</i>"]

    DV --> USE1["Return-to-flow onset<br/>(section 3)"]
    DV --> USE2["Graded dose-response<br/>validation (section 4)"]

    RAW -.-> PULSE["<b>Cardiac pulse</b> — quality only<br/>adaptive Welch, >= 30 s (D22)<br/>needs >= 6 dB prominence;<br/>harmonics informational (D30)"]
    PULSE -.->|"cross-check"| WH["Whoop heart rate"]

    classDef gate fill:#fff4e0,stroke:#e07b00
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    class MOT,SETTLE,SLIP gate
    class EXCL,EXCL2 bad
```

**Measured on hardware (2026-10-09, settled 60 s wrist capture):** onset threshold
**0.627 µm/s**; red drift 2.0× IR (was 90× before settling); motion flagger 0 false events.

---

## 3. Return-to-flow signal pipeline

The measurement detects **slow venous filling**, not the cardiac pulse. The pulse is roughly
an order of magnitude smaller and is treated only as a quality flag.

```mermaid
flowchart TD
    RAW["<b>0. Raw counts</b><br/>ir, red — 18-bit, 100 Hz"]
    DARK["<b>1. Dark subtraction</b><br/>counts - dark<br/>from the DARK command"]
    REF["Reference window<br/>last 3 s of occlusion<br/>before deflation starts"]
    OD["<b>2. delta-OD</b><br/>ln(ref / counts)<br/>per wavelength"]
    DV["<b>3. Blood volume</b><br/>dV·L = 229·dOD660 + 1555·dOD880<br/>relative micrometres"]
    LP["<b>4. Low-pass</b><br/>Butterworth 0.3 Hz, 2nd order<br/>zero-phase (filtfilt)"]
    D1["<b>5. Derivative</b><br/>Savitzky-Golay, 2.0 s window<br/>one HeartMate-3 pulse period"]
    NOISE["<b>6. Noise floor</b><br/>mu0, sigma0 over the<br/>reference window"]
    ONSET["<b>7. Onset</b><br/>sustained excursion above<br/>mu0 + 3·sigma0"]
    REFINE["<b>7b. Ramp intersection</b><br/>fit the established ramp,<br/>extrapolate to baseline"]
    MAP["<b>8. Result</b><br/>cuff pressure at onset"]

    RAW --> DARK --> OD --> DV --> LP --> D1
    REF -.->|"counts_ref"| OD
    D1 --> NOISE --> ONSET --> REFINE --> MAP

    MOTION["Motion mask (D27)<br/><b>not yet wired into onset</b>"]
    MOTION -.-> ONSET

    PRAW["Pressure raw<br/>HX710B, ~40 Hz"]
    PCAL["Two-point calibration<br/>(raw - RAW0)·P_REF / (RAW_REF - RAW0)"]
    PRES["Resample to PPG timebase<br/>interpolate on HX710B counter"]
    PRAW --> PCAL --> PRES --> MAP

    IRONLY["IR-only fallback<br/>dOD880 / 6.13 cm^-1 × 1e4<br/>vulnerable to desaturation"]
    OD -.->|"--ir-only"| IRONLY
    IRONLY -.-> LP

    PULS["Pulsatility RMS<br/>0.5-5 Hz during reference<br/><i>flag only: pulsatile subject<br/>reads near systolic, not MAP</i>"]
    OD -.-> PULS

    classDef deviation fill:#fff4e0,stroke:#e07b00
    classDef missing fill:#ffe6e6,stroke:#c43b2f,stroke-dasharray:4 3
    class REFINE deviation
    class PRAW,PCAL,PRES,MOTION missing
```

**Orange** = deliberate deviation from SPEC §5.1 (D10). The spec's threshold-crossing onset
reads ~2.6 mmHg high, because the 0.3 Hz zero-phase filter and the 2 s Savitzky–Golay window
are both symmetric kernels and smear the ramp corner backwards in time. `--onset-method
intersect` fits the established ramp and extrapolates back, landing within ±0.2 mmHg.

**Red dashed** = not yet built or not yet fitted. The motion mask exists and is used by the
provocation analysis, but onset detection does not consult it yet — so a motion transient
during a real deflation could still be read as filling.

---

## 4. Validation — how we know the volume signal is right

There is no reference instrument for blood volume, so validation leans on physics that is
predictable from geometry alone (D28).

```mermaid
flowchart TD
    G0(["bench_check.py --graded --whoop-bpm N"]) --> R1["rest1, 30 s<br/>heart level"]
    R1 --> HR{"IR pulse rate<br/>matches Whoop<br/>within ~8 bpm?"}
    R1 --> U90["+90 deg — wrist ~+60 cm"]
    U90 --> U45["+45 deg — wrist ~+42 cm"]
    U45 --> D45["-45 deg — wrist ~-42 cm"]
    D45 --> D90["-90 deg — wrist ~-60 cm"]
    D90 --> SL{"<b>Slip check</b> (D31)<br/>did an extreme position's<br/>coupling move mid-hold?"}
    SL -->|yes| COMP["<b>COMPROMISED</b><br/>neither supported nor refuted —<br/>strain-relieve cable, re-run"]
    SL -->|no| FIT["<b>Dose-response fit</b><br/>dV·L vs wrist height<br/>(rests between each, ABA)"]

    FIT --> GATE{"<b>Magnitude gate</b><br/>are ±90 deg individually<br/>significant, z >= 3?"}
    GATE -->|no| NOISE2["NO MEASURABLE RESPONSE<br/><i>4 noise values sort correctly<br/>1 time in 24</i>"]
    GATE -->|yes| MONO{"Strictly monotonic?<br/>higher wrist = less blood"}
    MONO -->|yes| PASS(["<b>Validated:</b> signal tracks<br/>blood volume in rank order"])
    MONO -->|"one step out"| CHECK["Check that phase for<br/>flagged motion"]
    MONO -->|no| FAIL["Coupling changed mid-run,<br/>or drift-dominated"]

    HR -->|yes| FE(["Optical front end<br/>independently confirmed"])

    classDef good fill:#e6f5ea,stroke:#0a7d2c
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    classDef gate fill:#fff4e0,stroke:#e07b00
    class PASS,FE good
    class NOISE2,FAIL bad
    class GATE,SL,COMP gate
```

**First hardware run (2026-10-09 17:01): COMPROMISED.** The +90° hold slipped at 36.4 s
(−7.1 %). The three remaining positions order correctly in IR-only ΔV·L but not in
two-wavelength — see O4.

Rank order is judged by Spearman correlation; linearity (R²) is reported but not required,
because venous compliance is itself non-linear.

---

## 5. PPG monitor (standalone) — two tabs, one acquisition path

`hr_monitor.py`, separate from the return-to-flow tooling. One source, one LED tuning, two
estimators fed from the same samples; only the visible tab redraws.

```mermaid
flowchart LR
    SRC(["Board<br/>(DARK on connect)<br/>or --replay"]) --> CORE["MonitorCore<br/>drains samples,<br/>controls, recording"]
    TUNE["Auto-tune: IR + red<br/>to DC mid-scale"] -.-> SRC
    CORE -->|IR| HRE["HeartRateEstimator<br/>(D33)"]
    CORE -->|"IR + red"| VOL["VolumeTracker<br/>(D34)"]
    HRE --> T1["<b>Heart rate tab</b>"]
    VOL --> T2["<b>Blood volume tab</b>"]
    TUNE -.->|"re-tune breaks trace,<br/>re-zeroes; refused<br/>while recording"| VOL
    CORE --> REC["Record: raw + events,<br/>_hr.csv, _vol.csv"]

    classDef gate fill:#fff4e0,stroke:#e07b00
    class TUNE gate
```

### Blood volume tab

```mermaid
flowchart TD
    RAW["IR + red counts"] --> DK["Dark subtraction"]
    DK --> LP["<b>Low-pass 0.3 Hz</b>, causal<br/><i>~0.75 s lag; offline uses zero-phase</i>"]
    LP --> REF{"Reference set?<br/>Zero button, or auto<br/>10 s after connect / re-tune"}
    REF -->|no| WAIT["NO BASELINE"]
    REF -->|yes| OD["delta-OD = ln(ref / counts)"]
    OD --> DV["<b>ΔV·L</b>, relative µm<br/>two-wavelength and IR-only<br/>side by side (O4)"]
    DV --> RATE["Rate: slope over 2 s"]
    LP --> MOT{"Either channel moving<br/>> 2 %/s (low-passed)?"}
    MOT -->|yes| MS["MOTION — red strip"]
    RATE --> FL["<b>Noise band</b> (D35)<br/>rolling 60 s of quiet samples:<br/>μ0 = median, half-width =<br/>1.2 × P95 of |rate − μ0|, ≥ 0.5"]
    FL --> TR{"Beyond the band for 2 s?<br/><i>hysteresis: stay while<br/>2 s mean beyond half</i>"}
    TR -->|above| FILL(["FILLING"])
    TR -->|below| DRAIN(["DRAINING"])
    TR -->|no| STAB(["STABLE"])

    classDef gate fill:#fff4e0,stroke:#e07b00
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    class REF,MOT,TR gate
    class MS,WAIT bad
```

The band tracks the real fluctuation envelope — breathing included (on the graded run 46 %
of rest-period power sat at 0.15–0.4 Hz). Samples in or near a trend are excluded, so a
real ramp cannot be adapted away. Zero, wait for STABLE (~30 s), then start the event.

### Heart rate tab

The guiding rule (D33): **show a number only when two independent estimates corroborate
it** — a blank with a reason beats a confident wrong value.

```mermaid
flowchart TD
    SRC(["Board, or --replay CSV"]) --> RAW["Raw IR counts<br/>pushed in ~40 ms blocks"]
    RAW --> BP["<b>Band-pass 0.5–4 Hz</b><br/>4th-order Butterworth, causal,<br/>state carried across blocks"]
    BP --> INV["Invert<br/><i>reflectance: systole = fewer counts</i>"]
    RAW --> SNR{"<b>Pulse SNR</b> >= 3?<br/>in-band vs noise floor above 10 Hz"}
    SNR -->|no| NOB["No beats timed<br/><i>relative threshold would find<br/>'beats' in noise</i>"]
    INV --> PK["<b>Peaks</b><br/>0.3 s refractory, adaptive prominence,<br/>confirmed after 0.6 s, sub-sample refined"]
    SNR -->|yes| PK
    PK --> IBI["<b>Interval gating</b><br/>0.3–2.0 s, within 30% of median;<br/>4 rejections reset history"]
    IBI --> HRB["Beat HR (D36)<br/>60 / mean interval over<br/>a sliding window, default 10 s"]
    RAW --> SPEC["<b>Spectral HR</b>, last 12 s<br/>Welch on RAW detrended data<br/>confidence = concentration"]
    HRB --> AGREE{"Agree within 8 bpm<br/>and concentration >= 0.35?"}
    SPEC --> AGREE
    AGREE -->|"yes, for 3 s"| SHOW(["<b>Show heart rate</b>"])
    AGREE -->|no| BLANK["Show -- and the reason"]
    RAW --> Q{"No contact? saturated?<br/>baseline moving?"}
    Q -->|yes| BLANK

    classDef gate fill:#fff4e0,stroke:#e07b00
    classDef good fill:#e6f5ea,stroke:#0a7d2c
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    class SNR,AGREE,Q gate
    class SHOW good
    class NOB,BLANK bad
```

Each safeguard was added because a test caught the monitor showing a confident wrong number:
on pulse-free drift, the first version showed a heart rate in 310 of 390 seconds; the final
one in 0. On the real 60 s wrist capture it reads in 44 of 44 seconds, median 79 bpm.

---

## 6. Diagnostic path — what we run before trusting a recording

```mermaid
flowchart TD
    START(["Board + sensor wired"]) --> ID{"INFO reports<br/>sensor_ok=1 and<br/>part_id 0x15?"}
    ID -->|no| FIXW["Check SDA=A4, SCL=A5, 3V3, GND<br/>send R to retry, no reflash"]
    FIXW --> ID
    ID -->|yes| DARKC{"DARK offsets<br/>under ~128 counts?"}
    DARKC -->|no| SHADE["Shade from ambient light"]
    SHADE --> DARKC
    DARKC -->|yes| TUNE["Auto-tune LED current<br/>IR DC into 105k-157k"]
    TUNE --> SET["Settle ~45 s<br/>before capturing"]
    SET --> LINKC{"Link stable?<br/>rate within 1%,<br/>zero dropped samples"}
    LINKC -->|"stall / lost"| CABLE["Partial capture kept.<br/>Port still present = firmware;<br/>port gone = cable or hub"]
    CABLE --> LINKC
    LINKC -->|yes| PROVC{"<b>Provocation test</b><br/>does arm raise/lower<br/>move the DC?"}

    PROVC -->|"FLAT"| PIPE2["Light piping across the surface.<br/><b>Raising LED current makes it worse.</b><br/>Press flat, strap it."]
    PROVC -->|"INVERTED"| SIGN["Wrong sign — strap squeezing<br/>during movement"]
    PROVC -->|"CONTRADICTORY"| CONTRA["Both manoeuvres moved counts<br/>the same way: baseline drifting.<br/>Re-seat, settle, re-run (D21)"]
    PROVC -->|"RESPONDS"| READY(["Site viable —<br/>proceed to graded validation"])
    PIPE2 --> PROVC
    SIGN --> PROVC
    CONTRA --> PROVC

    classDef good fill:#e6f5ea,stroke:#0a7d2c
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    class READY good
    class PIPE2,SIGN,CONTRA bad
```

---

## 7. Test layer

```mermaid
flowchart LR
    subgraph SYN["Synthetic ground truth"]
        SYNTH["synth.py<br/>known MAP, cardiac +<br/>artificial pulse, desaturation"]
        PBUILD["build() / build_graded()<br/>known response, drift,<br/>noise-only controls"]
    end
    subgraph MOCK["Mock firmware"]
        FAKE["FakeSerial<br/>full SPEC 3.4 protocol;<br/>disconnect, stall, reset,<br/>wedged read, real-time pacing"]
    end
    subgraph HW["Real hardware captures"]
        CAP["data/*.csv in git<br/>anchor tests to real signals"]
    end
    subgraph TESTS["172 tests"]
        T1["test_pipeline.py<br/>SPEC section 6 criteria"]
        T2["test_protocol.py<br/>link, bench_check, pulse"]
        T3["test_provocation.py<br/>verdicts, dose-response"]
        T4["test_quality.py<br/>motion + slips"]
        T5["test_heartrate.py<br/>HR estimator + GUI"]
        T6["test_volume.py<br/>blood volume + tab"]
    end
    SYNTH --> T1
    FAKE --> T2
    PBUILD --> T3
    CAP --> T4
    CAP --> T5
    CAP --> T6
    SYNTH --> T6
    PBUILD --> T5
    CAP --> T3
```

---

## 8. Status

| Layer | State | Evidence |
|---|---|---|
| Firmware — PPG | **working on hardware** | 100 Hz, zero dropped samples over 60 s, part id 0x15 |
| Firmware — pressure | written, never run | compiles under `PRESSURE_ENABLED=1` only |
| Serial link | **working, with known stall** | stalls caught and partial data kept (D23); root cause parked |
| Blood-volume signal | **responds on hardware** | wrist provocation z=37.8 / 19.2 (raw data lost, D29) |
| Noise floor | **good on hardware** | 0.627 µm/s settled, ~22× headroom vs arm-raise response |
| Motion flagging | **working on hardware** | 0 false events on still capture; 4/4 movements caught |
| Graded dose-response | **run once: compromised** | +90° slipped mid-hold; needs a clean re-run |
| Slip detection | **working on hardware** | finds the one real slip; none on the clean capture |
| Whoop HR cross-check | built, **reading not captured** | run's log lost before D32; Whoop value unknown |
| Return-to-flow pipeline | synthetic only | ±0.2 mmHg on synthetic ground truth |
| **Return of flow, real cuff** | **detected, with mmHg (provisional)** | 20:35 trial: refill onset 110 mmHg (sloped baseline), pulse back 106 mmHg; flat-baseline D10 gives 99 (O5) |
| Pressure calibration | never run | no HX710B |
| Cuff pressure from video (D38) | **works** | 3,804 frames read; zero held to 1.4 mmHg across the trial; gauge span unverified |
| Heart-rate tab | **works on real wrist replay** | 44/44 s shown, median 79 bpm; not yet run live or against the Whoop |
| Blood-volume tab | **works on real replay** | still capture STABLE throughout; graded-run slip flagged live; not yet run live |

![Cuff trial 2026-10-09 20:35: cuff pressure from the gauge video, IR pulse, blood volume and its derivative, with return-of-flow markers](figures/cuff-trial-2-20261009-2035.png)
*Cuff trial 2026-10-09 20:35 (`hr_wrist_20261009_203550`). Top to bottom: cuff pressure read from the gauge video (provisional, zero offset corrected), IR pulse with its amplitude, two-wavelength and IR-only ΔV·L with the occlusion trend and ramp fit, and d(ΔV·L)/dt. Dashed lines: dV/dt leaves its noise band (120 mmHg), refill onset with sloped baseline (110), regular beats resume (106), refill onset with flat baseline (99).*

### Open issues

| Issue | Impact |
|---|---|
| Graded dose-response compromised by a coupling slip | the D26 accuracy claim is still unvalidated |
| Sensor slips on skin during arm movement | mechanical fixation, likely cable strain — blocks every validation |
| Red ~2–3× more contact-sensitive than IR (O4) | contaminates two-wavelength ΔV·L; trade-off against desaturation rejection |
| Motion mask not wired into return-to-flow onset | a transient during deflation could be read as filling |
| Serial stall with port still present — parked by user | costs retries; would ruin a real deflation mid-measurement |
| ΔV·L is relative (unknown path length) | fine for D26; reopens if absolute volume is ever needed |
| No HX710B | onset time is measured live; mmHg comes from a video of the gauge (D38), ±2–5 mmHg |

### Resolved since last update

| Was | Resolution |
|---|---|
| Red drifts ~90× more than IR | contact settling, not LEDs — 2.0× after `--settle` (O1) |
| Onset noise floor ~7.7 µm/s | 0.627 µm/s settled, 12× better (O3) |
| `synth.py` ramp below real noise floor | it's 4.8× above the settled floor (O2) |
| Cardiac rate unstable between settings | Welch averaged ~1 segment; adaptive window + 30 s minimum (D22) |
