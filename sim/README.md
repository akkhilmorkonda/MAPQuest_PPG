# sim/: does return-to-flow read MAP without a pulse?

A simulation study, kept apart from the dev tools. It imports `host/` read-only, changes
nothing there, and writes only to `sim/out/` (gitignored), never `data/`. `marker_report.py`
scores every `data/hr_*.csv` that has a reference BP, so simulated runs must not land there.

## Why not `host/synth.py`

`synth.py` starts the fill at the moment the cuff crosses `--map`. Running it without a pulse
can only confirm that assumption. In `model.py` the onset pressure is an **output**: blood
crosses the cuff whenever arterial pressure is above cuff pressure, and the arterial
waveform decides when that first happens. The same equations give flow returning at
systolic with a pulse and at MAP without one. The runs with a pulse can be checked
against the real trials before the runs without one are trusted.

## Model (`model.py`)

```
P_art(t) --[artery under cuff]--> distal artery --[arterioles]--> veins --[vein under cuff]--> CVP
```

- **Arterial pressure:** MAP, plus optionally a native pulse (`pulse_pressure`, `hr_bpm`),
  intermittent ejection (`pulse_every`), the HeartMate 3 artificial pulse (`hm3_pp_mmhg`;
  every 2 s, −2000 rpm for 0.15 s then +2000 rpm for 0.2 s) and respiration.
- **Cuff:** off, a fast inflation, a hold, a linear bleed, then a dump at 30 mmHg.
- **Vessels under the cuff:** Starling resistors. They stay closed until the pressure inside
  exceeds the cuff, are flow-limited by the cuff, and open fully over `open_span` mmHg.
- **Optics:** tissue volume (`c_art·P_d + c_ven·P_v`, in dV·L µm) goes to counts through
  synth's Beer–Lambert split, plus white count noise and slow band-limited noise
  (`lf_noise_um`). Pressure is MPRLS counts.
- **Truth:** each run records MAP, SBP and DBP, and when and at what cuff pressure blood
  first crossed the cuff.

The `Vessels` defaults are the phase 1 placeholders. The fitted settings are in
`calibration.json` (variants A and B, below). How softly the cuffed artery opens
(`open_span`, `open_power`) is what a no-pulse result depends on most.

## Use

```bash
python sim/run.py --map 85                    # LVAD, no pulse
python sim/run.py --map 85 --hm3 8            # HeartMate 3 artificial pulse
python sim/run.py --map 90 --pp 45            # healthy subject
python sim/run.py --map 85 --plot             # CSV, .truth.json and cuff_trial figure in sim/out/
python -m pytest sim/tests -q                 # model physics, harness, calibration and experiment plumbing (28 tests)

python sim/calibrate.py fit                   # phase 2: shared settings from the training runs
python sim/calibrate.py scan                  # opening margin vs onset bias, optical pulse held -> variant B
python sim/calibrate.py check --variant B     # held-out runs: refill shape, pulse size, markers
python sim/calibrate.py sweep --variant B     # pulse pressure 0-50 at MAP 85

python sim/experiments.py soft                # phase 3: fit the soft-opening bracket
python sim/experiments.py all                 # E1-E4 (about 20 min); results/phase3.json
```

Each run is scored by `cuff_trial.analyse` unchanged: the same windows, pipeline and
four markers as the real trials. The saved CSVs are in the recording format, so
`python host/cuff_trial.py sim/out/<run>.csv` works on them too. Run the dev suite with
`pytest tests/` as before. A bare `pytest` from the repo root also collects `sim/tests`.
`run.py` still uses the phase 1 default settings; the calibrated variants are used
through `calibrate.py` until phase 3 wires them in.

## Phase 2 results: calibration against the real runs

The 22 scored runs (the ones `marker_report.py` uses) are split by alternating recording
order: 11 for training, 11 held out. Each run gets a **twin**: the model driven by that run's
own cuff-pressure trace, with an arterial pulse built from its reference systolic and
diastolic and its heart rate. Three things are free per run: optical gain, offset and a linear
drain. Everything else is shared, so the shape of the refill has to come from the model. The
sensor noise is measured on each run's occluded hold. Numbers are in `calibration.json`;
figures are in `figures/`.

**What the fit found.** The cuffed artery opens fully as soon as arterial pressure clears the
cuff (`open_span` went to its 0.5 mmHg limit), and the cuff limits the inflow. With a pulse,
the refill grows with how much of each beat clears the cuff: a trickle at systolic, rising to
full flow below diastolic. Without a pulse, full inflow starts at MAP.

**Two variants, kept because the data cannot separate them:**

| | A: shape fit only | B: optical pulse held nearer its real size |
|---|---|---|
| Optical pulse, twin ÷ real (held out) | 0.18× | 0.51× |
| Refill shape, median R² (held out) | 0.961 | 0.955 |
| Simple hinge at reference SBP, for comparison | 0.939 (p = 2) | 0.939 |
| **Hinge onset vs reference SBP, held out: twin** | −0.9 ± 4.4 | **−5.6 ± 4.6** |
| Hinge onset vs reference SBP, held out: real | −6.9 ± 6.2 | −6.9 ± 6.2 |
| Twin − real, same run | +5.9 ± 7.2 | **+1.2 ± 5.6** |

B reproduces where the real runs put the onset; A reads 6 mmHg too early. In B's twins, blood
first crosses the cuff within 3 mmHg of systolic, yet the hinge reads 5.6 below it: the
first trickle is too small to see. So the model explains the real "onset reads below
systolic" as a **detection effect, not an opening margin**. The scan agrees: adding an
opening margin (0–9 mmHg) only moves the twins further from the real runs. The fit
quality barely changes (1−R² 0.048 → 0.057).

**What the calibrated model predicts, MAP 85, median of 5 seeds** (`figures/phase2-pulse-sweep-B.png`):

| Native pulse pressure | 0 | 5 | 10 | 20 | 40 |
|---|---|---|---|---|---|
| Hinge − MAP, 2 mmHg/s | **+0.1** | +0.7 | +6.2 | +12.3 | +26.9 |
| Hinge − MAP, 4 mmHg/s | **+1.6** | +1.7 | +3.3 | +7.6 | +28.3 |
| Intersect − MAP, 2 mmHg/s | +0.8 | +1.1 | +1.8 | +8.0 | +21.3 |
| SBP − MAP | 0 | 3.7 | 7.4 | 14.7 | 29.4 |

- **No pulse: the onset reads MAP.** Within 2 mmHg in both variants, at both deflation rates.
  One seed in five gave a false early onset (+44 mmHg) when a slow noise swing tripped the
  threshold. That failure mode is worth sizing in E1.
- **A small pulse moves it toward systolic.** At 2 mmHg/s, a 10 mmHg pulse pressure put the
  hinge 84% of the way from MAP to systolic. *Superseded by E2 below: on the full
  22-run population it is 6–39%. The phase 2 figure came from 5 seeds of one median run.*
- **Pulse return and steep refill stay quiet without a pulse.** Pulse return finds nothing.
  Steep refill appears only on the noisy seed.

**What does not match, and limits on the above:**
- **Pulse return reads 10 mmHg too early** in the twins (−2.6 vs −12.8 on held-out runs).
  The optical pulse is still only half its real size, and the fit keeps shrinking it
  (an arterial compartment adds a jump at opening that the real refills do not show).
  Pulse-return results from the model are not trustworthy yet.
- **Steep refill** (D44) is not reproduced either: −12.8 vs −22.0, with few detections.
- **The sharp opening sits at the parameter limit.** With a pulse, the beat-to-beat duty cycle
  and a gradually opening artery both soften the refill, and these runs cannot tell
  them apart. A soft opening (the phase 1 default, 20 mmHg) read −3.3 vs MAP with no
  pulse. Phase 3 should bracket with both.
- The beat shape is fixed, the reference BP carries several mmHg of its own error, the
  sweep uses 5 seeds, and the HeartMate 3 pulse is not in it yet.

The LVAD Doppler comparison from the plan is only qualitative here. Onset moves from MAP
toward systolic as the pulse grows, as the clinical finding says. Our detection gets there
at a smaller pulse than Doppler is reported to.

## Plan

| Phase | Work | State |
|---|---|---|
| 1 | Forward model, physics tests, harness through `cuff_trial.analyse` | **done** |
| 2 | Fit to real pulsatile runs; held-out check; opening-margin scan; pulse-pressure sweep | **done**: variant B matches the real onset; pulse return does not |
| 3 | E1–E5 on the 22-run population, sharp and soft opening | **done**: accurate when found; E1 fails on missed onsets at low MAP |
| 4 | Notebook write-up | next |

## Phase 3 pass criterion (set 2026-11-01, before any phase 3 run)

**E1 passes** if, with no pulse at **2 mmHg/s** across **MAP 60–100**, the **hinge onset**
meets all three on the realistic population (below), in **both** opening variants:

1. **Bias:** mean (onset − true MAP) within ±2 mmHg, false onsets excluded;
2. **Spread:** SD ≤ 3 mmHg, false onsets excluded;
3. **False onsets:** ≤ 5% of runs. A false onset is an onset more than 10 mmHg from where
   blood truly first crossed the cuff, or no onset at all.

Each MAP level is judged on its own; one level failing fails E1. Other rates and markers
are reported, not judged. The population: every simulated run takes its optical gain,
drain and sensor noise from one of the 22 real runs' twins, in turn. The settings come
from `calibration.json` variant B (sharp opening) and a soft-opening variant fitted the
same way with `open_span` held at 20 mmHg. The cuff holds at MAP + 50 mmHg.

## Phase 3 results

`python sim/experiments.py soft` then `all`. Results are in `results/phase3.json`;
figures are in `figures/phase3-*`. There are 22 runs per cell, one for each real-run draw.

**The soft bracket is as plausible as B.** Held out, its refill R² is 0.963 against
B's 0.955. Its hinge reads −7.4 ± 4.1 vs reference SBP, against −6.9 ± 6.2 for the real
runs (same run: −0.6 ± 5.8). The real runs cannot choose between the two, so every
result below is given for both.

### E1, no pulse: **FAIL** on the pre-registered criterion, in both variants

| Hinge at 2 mmHg/s | MAP 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|
| sharp: bias ± SD | +0.6 ± 0.5 | +0.0 ± 1.4 | +0.1 ± 0.9 | −0.1 ± 0.7 | −0.0 ± 0.7 |
| sharp: false onsets | **18%** | 5% | 5% | 0% | 0% |
| soft: bias ± SD | −1.2 ± 0.9 | −1.5 ± 1.0 | −1.7 ± 0.8 | **−2.04** ± 0.8 | −1.9 ± 1.9 |
| soft: false onsets | **14%** | 5% | 5% | 0% | 0% |

- **When an onset is found, it reads MAP.** Bias is within ±2 mmHg and SD within 2 mmHg
  at every MAP and every rate from 1 to 4 mmHg/s (`figures/phase3-e1-no-pulse.png`). The
  one bias miss, soft at MAP 90, is −2.04 against a limit of ±2. Faster deflation reads
  higher (sharp: −0.5 at 1 mmHg/s to +1.5 at 4) and the soft opening reads about 1.7 lower
  throughout. The bracket of possible bias is about −2 to +2 mmHg.
- **What fails is finding the onset at all, at low MAP.** Of 67 false onsets in all 880
  E1 runs, 49 are no onset at all. They come from the draws that borrow the noisiest
  or most strongly draining real runs: test53, test54, 10.16.26 (slow noise 0.7–1.5 µm)
  and test58 (drain −2.4 µm/s). The pipeline only calls an onset once the volume is net
  *rising* (threshold floored at zero, D39). At MAP 60 the inflow is weaker and can fail to
  overcome the drain before the cuff is dumped. At 1 mmHg/s the rate is 9–27% at every MAP.

### E2, residual native pulse (MAP 80): reading moves toward systolic gradually

| Native pulse pressure | 0 | 5 | 10 | 15 | 20 | 40 |
|---|---|---|---|---|---|---|
| hinge − MAP, sharp, 2 mmHg/s | +0.1 | +0.5 | +2.9 | +7.0 | +11.1 | +25.6 |
| hinge − MAP, soft, 2 mmHg/s | −1.7 | −1.2 | +0.5 | +4.9 | +8.8 | +21.9 |
| SBP − MAP | 0 | 3.7 | 7.4 | 11.0 | 14.7 | 29.4 |

Up to a 10 mmHg pulse pressure the hinge stays within 3 mmHg of MAP. By 15–20 mmHg it reads
5–11 mmHg high, 45–75% of the way to systolic. At 4 mmHg/s it reads slightly closer to MAP
for pulses of 10–40 mmHg. The intersect behaves much the same (`figures/phase3-e2-pulse-pressure.png`).

### E3, LVAD waveforms (MAP 80, 2 mmHg/s)

| Case | hinge − MAP, sharp | soft | SBP − MAP |
|---|---|---|---|
| no pulse | +0.1 | −1.6 | 0 |
| HeartMate 3 pulse, 4 / 8 / 12 mmHg | +0.4 / +0.7 / +1.1 | −1.4 / −1.2 / −0.9 | 1.8 / 3.6 / 5.4 |
| PP 20 every beat | +11.1 | +8.8 | 14.7 |
| PP 20 every 2nd / 3rd / 5th beat | +13.0 / +13.4 / +11.5 | +12.5 / +13.0 / +11.1 | 17–19 |
| HM3 8 + PP 10 every 3rd beat | +1.8 | +0.1 | 10.2 |

- **The HeartMate 3 artificial pulse barely matters.** It adds about 1 mmHg at 12 mmHg
  peak-to-peak. The 2 s derivative window does its job.
- **Intermittent aortic-valve opening matters as much as a regular pulse.** One ejecting
  beat in five reads as high as every beat. Flow first crosses at the peak of the
  ejecting beats, whatever their rate. False onsets rise to 14–27%, and to 32–64% for the
  intersect.

### E4, stress cases (no pulse, MAP 80, 2 mmHg/s; baseline false-onset rate 5%)

| Case | hinge bias, sharp / soft | false onsets, sharp / soft |
|---|---|---|
| slow noise ×2 / ×4 | +0.2, −0.2 / −1.6, −1.9 | 27%, 36% / 23%, 45% |
| refill ×0.25 / ×0.1 | +0.4 / −0.4 (×0.25) | 64%, 95% / 73%, 100% |
| drain −2 µm/s throughout | +0.1 / −1.7 | 55% / 59% |
| slow inflation (15 s) | +0.4 / −1.2 | 5% / 0% |
| short hold (8 s) | +0.4 / −1.6 | 18% / 14% |
| 5 µm motion bump at MAP+15 / MAP+5 | +0.2, −1.3 / −1.6, −3.2 | 32%, 27% / 27%, 32% |
| −5 µm motion bump at MAP−5 | −2.4 / −3.9 | 0% / 0% |

The same pattern as E1 holds: noise, a weak refill, a drain and motion mostly cost
**detection**, not accuracy. Motion just before MAP is the exception: it biases the
reading by 1–4 mmHg. The model's drain is constant; real drains slow down (O5). So the
drain row is a worst case.

### E5, pulse return and steep refill without a pulse

- **Pulse return:** never fires without a native pulse, including with the HeartMate 3
  pulse up to 12 mmHg. It also never fires with intermittent ejection, so it cannot be the
  fallback for those patients.
- **Steep refill (D44):** never fires without a pulse in the sharp variant, and in 1 run
  of 22 (−3 mmHg) in the soft one. It does not produce a false "MAP" marker.

### What phase 3 says, and what it does not

- **The method holds where it can measure.** With little or no pulse, the hinge onset
  reads MAP to within about ±2 mmHg, with an SD under 2, across MAP 60–100 and 1–4 mmHg/s.
  That covers a HeartMate 3 pulse and a residual pulse pressure up to about 10 mmHg.
- **It fails the pre-registered criterion on reliability.** Too many runs find no onset
  at low MAP, with strong drains and with noisy sites. The fix is a detection question
  (the net-filling threshold, the noise floor, hold length, sensor fixation), not a
  question about the physics.
- **Patients whose valve opens on some beats read toward systolic.** Neither the hinge
  nor pulse return gives MAP for them.
- **Limits:** the model is fitted to pulsatile arms of healthy volunteers, not LVAD
  physiology (vascular tone, flow distribution). The drain is constant. The beat shape is
  fixed. There is no oxygen desaturation in the optics. The sample is 22 runs per cell.
