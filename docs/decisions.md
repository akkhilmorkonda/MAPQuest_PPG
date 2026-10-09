# MAPQuest — Decision Log

Every non-obvious choice, why it was made, and what it costs. Entries are append-only:
when a decision changes, the old entry is marked **superseded** rather than edited, so the
reasoning that led there stays visible.

**Add an entry whenever:** a choice deviates from [`SPEC.md`](SPEC.md), a default is picked
that someone could reasonably pick differently, a bug reveals a wrong assumption, or an
experiment settles something.

| # | Decision | Status |
|---|---|---|
| [D1](#d1) | Red + IR, not green | accepted |
| [D2](#d2) | ~~Green + IR for best waveform quality~~ | **superseded by D1** |
| [D3](#d3) | SpO₂ mode rather than Multi-LED | accepted |
| [D4](#d4) | 400 sps internal + 4× averaging → 100 Hz | accepted |
| [D5](#d5) | Inline MAX30101 driver, no SparkFun library | accepted |
| [D6](#d6) | ASCII CSV over USB CDC, not binary | accepted |
| [D7](#d7) | `idx / fs` is the master clock, not `t_us` | accepted |
| [D8](#d8) | Zero-phase filtering (`filtfilt`) | accepted |
| [D9](#d9) | Savitzky–Golay derivatives, not `np.diff` | accepted |
| [D10](#d10) | Onset by ramp intersection, not threshold crossing | accepted — **deviates from SPEC §5.1** |
| [D11](#d11) | IR-only fallback converted to micrometres | accepted — bug fix |
| [D12](#d12) | `R` retry command added | accepted — **extends SPEC §3.4** |
| [D13](#d13) | `sensor_ok` reported in `INFO` | accepted — **extends SPEC §3.4** |
| [D14](#d14) | Pulse detection by spectral prominence | accepted — bug fix |
| [D15](#d15) | Arm-raise provocation is the viability gate | accepted — **not in SPEC** |
| [D16](#d16) | ABA design in the provocation protocol | accepted |
| [D17](#d17) | Typed link errors and partial-capture salvage | accepted |
| [D18](#d18) | **Measurement site: wrist, not ventral forearm** | accepted — **deviates from SPEC §2.1/§8** |
| [D19](#d19) | Pressure path compiled out behind a flag | accepted |
| [D20](#d20) | Explicit LED currents + die-temperature bracketing | accepted |
| [D21](#d21) | A wrong-signed manoeuvre vetoes a 'responds' verdict | accepted — bug fix |
| [D22](#d22) | Pulse detection needs >= 30 s and an adaptive Welch window | accepted — bug fix |
| [D23](#d23) | Non-blocking serial reads + watchdog | accepted — bug fix |
| [D24](#d24) | ~~Harmonic series as pulse evidence~~ | **superseded by D30** |
| [D25](#d25) | Green evaluated as a third channel, declined | accepted |
| [D26](#d26) | Scope: relative & monotonic accuracy, 2–5 min, detect-and-flag motion | accepted — **user's call** |
| [D27](#d27) | Motion flagged by speed: sigma test plus an absolute floor | accepted |
| [D28](#d28) | Graded dose-response as the validation, with a magnitude gate | accepted |
| [D29](#d29) | Hardware captures are tracked in git | accepted — after data loss |
| [D30](#d30) | Harmonics are informational only; a pulse needs ≥6 dB on its own | accepted — reverses D24 |
| [D31](#d31) | Coupling slips detected inside holds; affected manoeuvres excluded | accepted |
| [D32](#d32) | Run inputs in the CSV header; console log written through | accepted — bug fix |
| [O1](#o1) | Red channel drift — cause and mitigation | **resolved** |
| [O2](#o2) | `synth.py` noise defaults vs hardware | **resolved** |
| [O3](#o3) | Is the onset noise floor low enough to work? | **likely yes** |
| [O4](#o4) | Red is the contact-sensitive channel — two-wavelength vs IR-only | **open** |

---

<a id="d1"></a>
## D1 — Red (660 nm) + IR (880 nm), not green

**Status:** accepted · 2026-10-05

**Context.** The MAX30101 carries green, red and IR. Only two channels are needed.

**Decision.** Red + IR. Green is left unused.

**Why.** SPEC §5.1 step 6 converts optical density into blood volume with
`ΔV·L = 229·ΔOD₆₆₀ + 1555·ΔOD₈₈₀`. Those coefficients are tied to haemoglobin extinction at
660 and 880 nm; there is no green coefficient to substitute. More importantly the Hb−HbO₂
extinction difference is **+2906 cm⁻¹/M at 660 nm and −488 cm⁻¹/M at 880 nm** — the sign flip
is what lets the pair cancel desaturation of trapped blood. Green sits where oxy and deoxy
absorb almost equally, so it carries nearly no saturation information and cannot play either
role. Green's 0.3–0.8 mm penetration would also largely miss the bulk filling being measured.

**Consequences.** Locked into SpO₂ mode (see D3). Green remains available for *diagnostics*
if coupling ever needs a more sensitive probe — that would not affect the measurement path.

Full reasoning and sources: [`led-selection-report.md`](led-selection-report.md).

---

<a id="d2"></a>
## D2 — ~~Green + IR, for best reflectance waveform quality~~

**Status:** **superseded by D1** · 2026-10-05

**What was decided.** Green + IR in Multi-LED mode, on the general reflectance-PPG literature:
green has the best AC/DC ratio, highest SNR, and sharpest dicrotic notch.

**Why it was wrong.** It optimised for *pulsatile waveform quality at a fingertip*, which was
the stated goal before `SPEC.md` arrived. The actual measurement is return-to-flow MAP built
on a two-wavelength Beer–Lambert solve, where green is not merely worse but unusable. The
research was sound; it answered the wrong question.

**Lesson.** Confirm the measurement model before optimising the front end. The literature's
"best" depends entirely on what is being measured.

---

<a id="d3"></a>
## D3 — SpO₂ mode rather than Multi-LED

**Status:** accepted · 2026-10-05

**Decision.** `MODE[2:0] = 0b011` (SpO₂), which drives exactly Red + IR.

**Why.** Follows from D1. Multi-LED would work but needs explicit slot configuration for no
benefit. One trap recorded for posterity: on the MAX30101, **"Heart Rate mode" is Red-only,
not green-only** — people arriving from the MAX30102 (which has no green) routinely assume
otherwise. Green is reachable *only* through Multi-LED.

**Consequences.** FIFO layout is fixed at Red-then-IR, 3 bytes each. Verified against the
datasheet (p.15: "3 bytes for RED, 3 bytes for IR") and SparkFun's reference implementation.

---

<a id="d4"></a>
## D4 — 400 sps internal with `SMP_AVE = 4`, giving 100 Hz output

**Status:** accepted · 2026-10-05

**Context.** SPEC §3.1 asks for 100 Hz. Datasheet Table 11 caps two-channel operation at
**400 sps** when the pulse width is 411 µs (the setting that gives full 18-bit resolution).

**Decision.** Run the ADC at its 400 sps ceiling and average 4 samples in hardware.

**Why.** Output rate and internal rate are separable. Running the ADC at 100 sps would meet
the spec while discarding the headroom; spending it on averaging buys roughly a 2× noise
reduction for free. The measurement is a slow DC trend whose detection threshold is set
directly by the noise floor, so this is the right place to spend it.

**Consequences.** Changing the output rate means changing `SAMPLE_AVERAGING`, not
`SAMPLE_RATE_SPS`. Both live in `config.h` and are echoed into every recording header.

---

<a id="d5"></a>
## D5 — Inline MAX30101 driver, not the SparkFun library

**Status:** accepted · 2026-10-05 *(rationale recovered 2026-10-06 — see note)*

**Decision.** The sketch talks to the sensor directly over `Wire`. No external libraries.

**Why.**
1. **No install step.** Open the `.ino` in Arduino IDE and upload.
2. **The SparkFun library names its FIFO getters by slot position, not by LED.**
   `getFIFORed()` returns whatever sits in slot 1. Put green in slot 1 and `getFIFORed()`
   returns green. For a project whose correctness depends on knowing which wavelength
   produced which number — the two-wavelength solve weights them 229 vs 1555 — that is a
   serious footgun.
3. Full control over the slot and averaging configuration D3/D4 depend on.

**Consequences.** ~200 lines of register code to maintain, and the datasheet is the only
reference. Compile-verified on every change via the `arduino-cli` bundled inside Arduino IDE.

> **Note.** This rationale was lost when `docs/design.md` was deleted during the restructure
> to match SPEC's layout, and recovered from git history (`782fbd5`) on 2026-10-06. It is the
> reason this decision log exists.

---

<a id="d6"></a>
## D6 — ASCII CSV over USB CDC, not a binary protocol

**Status:** accepted · 2026-10-05

**Decision.** `S,<idx>,<t_us>,<ir>,<red>` as text.

**Why.** At 100 Hz × 2 channels this is roughly 3 kB/s — nowhere near a USB CDC bottleneck.
Text is debuggable from any serial monitor, which matters a great deal during bring-up. The
bandwidth saved by a binary format buys nothing here.

**Revisit if** the sample rate rises substantially or more channels are added.

---

<a id="d7"></a>
## D7 — `idx / fs` is the master clock; `t_us` is diagnostics only

**Status:** accepted · 2026-10-05 (follows SPEC §3.3)

**Why.** `t_us` is `micros()` at the moment the host drained a FIFO *burst*, so every sample
in that burst shares a timestamp. It is not the conversion time. Using it as a timebase would
quantise time into bursts. The sample index divided by the configured rate is uniform and
correct.

**Consequences.** `t_us` is still recorded, and is what SPEC §3.5 uses to verify output rate
and detect jitter. Measured on hardware at **99.37 Hz vs 100 configured (−0.63 %)** — within
spec, and benign, because pressure is attached per-PPG-sample so the small scale error
cancels when reading pressure at the onset.

---

<a id="d8"></a>
## D8 — Zero-phase filtering (`sosfiltfilt`)

**Status:** accepted · 2026-10-05

**Why.** A causal filter imposes a frequency-dependent group delay, which would shift the
onset in time and therefore bias the reported pressure. Offline we can afford to be
non-causal and avoid the question entirely.

**Consequences.** The pipeline needs the whole buffer, so it is not real-time. SPEC §5.3's
real-time variant must instead subtract a known delay (≈1.75 s ≈ 3.5 mmHg at 2 mmHg/s).
**Zero-phase is not zero-smearing** — see D10, which exists because of exactly that
confusion.

---

<a id="d9"></a>
## D9 — Savitzky–Golay derivatives, not finite differencing

**Status:** accepted · 2026-10-05

**Why.** Finite differencing multiplies high-frequency noise by ω per derivative. SG fits a
local polynomial and differentiates it analytically, smoothing and differentiating in one
well-conditioned step. The 2.0 s window is one HeartMate-3 artificial-pulse period, so it
cancels that component and its harmonics — keeping the pipeline valid on LVAD data without
re-tuning (SPEC §5.1).

---

<a id="d10"></a>
## D10 — Onset by ramp intersection, not threshold crossing

**Status:** accepted · 2026-10-05 · **deviates from SPEC §5.1 step 10**

**Context.** The spec defines onset as the first sample of a sustained excursion above
`μ₀ + 3σ₀`, and SPEC §6 requires accuracy within **±1 mmHg**.

**Problem.** The spec's own method cannot meet the spec's own criterion. Measured at
**+2.6 mmHg** on synthetic ground truth. The cause is structural, not noise: the 0.3 Hz
zero-phase low-pass and the 2 s SG window are both *symmetric* kernels, so they smear the
ramp's corner backwards in time as well as forwards. The derivative starts climbing before
filling actually begins. Confirmed by sweeping the window: 0.5 s → +2.16, 2 s → +2.56,
4 s → +3.70 mmHg.

**Decision.** Default to `--onset-method intersect`: fit the established ramp and extrapolate
back to where it meets the occlusion baseline. Because it uses the ramp's slope and offset
rather than the location of its corner, smearing cancels. Measured error **−0.15 mmHg**.

**Consequences.** Both estimators are always computed and both printed, so the difference
stays visible. `--onset-method threshold` gives literal spec behaviour.
**This is a candidate spec amendment** — flagged for the user, not yet resolved.

---

<a id="d11"></a>
## D11 — IR-only fallback converted to micrometres

**Status:** accepted · 2026-10-05 · bug fix

**Problem.** SPEC §5.1 step 6 states the fallback as `ΔOD₈₈₀ / 6.13 cm⁻¹`, which yields
**centimetres**, while the two-wavelength branch yields **micrometres**. Implemented
literally, the two paths sat 10⁴ apart, making any shared threshold, comparison or plot
meaningless.

**Decision.** Convert the fallback to µm (`×1e4`).

**Sanity check.** `1e4 / 6.13 = 1631 µm/OD` lands close to the two-wavelength `COEFF_IR_880`
of 1555 — the fallback is the same formula with the red term dropped, which is the
confirmation the conversion is the right way round.

---

<a id="d12"></a>
## D12 — `R` retry command

**Status:** accepted · 2026-10-06 · **extends SPEC §3.4**

**Decision.** `R` (or `RETRY`) re-probes and reconfigures the sensor at runtime.

**Why.** Bring-up involves wiring mistakes. Without this, every fix costs a reflash. Cheap to
add, materially faster to iterate.

---

<a id="d13"></a>
## D13 — `sensor_ok` reported in `INFO`

**Status:** accepted · 2026-10-06 · **extends SPEC §3.4** · bug fix

**Problem.** The firmware printed `part_id=0x%02X` using `EXPECTED_PART_ID`, the compile-time
constant — so with **no sensor attached at all**, `INFO` still reported `part_id=0x15` and the
bench check passed "MAX30101 found at 0x57".

**Decision.** Report the id actually read, plus an explicit `sensor_ok` flag. `START` is
refused when it is false, rather than streaming a plausible-looking flat line.

**Host note.** A missing `sensor_ok` field parses as `None`, not `False` — older firmware
must not be mistaken for a failure.

---

<a id="d14"></a>
## D14 — Pulse detection by spectral prominence

**Status:** accepted · 2026-10-06 · bug fix

**Problem.** The detector took the largest in-band FFT bin and compared it to a distant
5–20 Hz noise floor. On a drift-dominated recording the spectrum falls monotonically, so the
argmax lands on the band's **lower edge** and the comparison reports a large SNR for a pulse
that does not exist. It confidently reported "45 bpm, 27 dB" on a recording with no pulse.

**Decision.** Require a genuine local peak with prominence, measured against its own
neighbourhood. A 1/f spectrum has no local maxima, so it correctly yields nothing.

**Verified.** Synthetic 1/f drift → 2.5 dB (rejected); real 0.15 % pulse → 18.4 dB (accepted).

**Consequence.** Conservative: a weak real pulse buried in drift is reported as absent. That
is the right failure direction — see D15, where the pulse is advisory anyway.

---

<a id="d15"></a>
## D15 — The arm-raise provocation test is the viability gate

**Status:** accepted · 2026-10-06 · **not in SPEC**

**Context.** Every SPEC §3.5 acceptance criterion passes with the photodiode staring at a
wall. The first wrist attempt met them all while showing a perfusion index of 0.024 % — no
usable signal.

**Decision.** Gate on whether DC responds to a deliberate blood-volume change: raise the arm
(venous drainage), lower it (pooling), with rests between.

**Why not the cardiac pulse.** The measurement detects slow venous filling, which is roughly
an order of magnitude larger than the cardiac AC. A site can be perfectly viable while
showing no resolvable heartbeat, so gating on a pulse would reject working setups. Borne out
on hardware: the pulse was undetectable, while the provocation response was **z = 37.8**.

**Sign convention.** More blood → more absorption → **fewer** counts. Arm up raises counts.
A response with the wrong sign is flagged as its own failure mode, not a pass.

---

<a id="d16"></a>
## D16 — ABA design in the provocation protocol

**Status:** accepted · 2026-10-06

**Decision.** Rest before *and* after each manoeuvre; compare each manoeuvre to the mean of
the two surrounding rests.

**Why.** Baseline drift at these timescales is large — ~1,400 counts over 60 s was measured.
Comparing a manoeuvre only to the rest preceding it would read that drift as a response.
Verified: 1,500 counts of pure linear drift with no real response classifies as **flat**
(z = 0.4).

**Also:** only the last 50 % of each phase is used, because venous volume settles over several
seconds and averaging the transient would understate the effect.

**Known limitation.** If the baseline does not recover after a manoeuvre, the correction
underestimates it. Observed on 2026-10-06: `rest3` never returned to baseline, so the
arm-down effect was reported as −1,769 counts where against `rest2` alone it is −3,528.

---

<a id="d17"></a>
## D17 — Typed link errors and partial-capture salvage

**Status:** accepted · 2026-10-06

**Problem.** Once streaming began, an empty read just continued the loop — a silent board
spun forever with no diagnosis. A disconnect raised a bare pyserial traceback and discarded
the whole capture.

**Decision.** `LinkLost` (port gone) and `LinkStalled` (port open, firmware silent) carry the
sample count and port diagnostics. Captures that die partway are kept and analysed, with the
failure reported alongside.

**Why distinguish them.** They point at different causes: `LinkLost` means cable or
re-enumeration; `LinkStalled` means the OS still has the device, so suspect the sketch.

---

<a id="d18"></a>
## D18 — Measurement site: wrist, not ventral forearm

**Status:** accepted · 2026-10-06 · **deviates from SPEC §2.1 and §8**

**Context.** SPEC §2.1 specifies "PPG on ventral forearm, 5–10 cm below cuff edge" and §8
repeats it in the trial procedure. The forearm attempt gave a perfusion index of 0.024 % and
no detectable response.

**Decision.** Use the wrist for the time being. **User's call**, made 2026-10-06.

**Concern raised before the decision.** The wrist is the site where Red/IR is weakest —
commercial wrist heart-rate sensors all use green, because green's AC/DC ratio and shallow
penetration suit the wrist's superficial capillary bed. Red/IR there is the hardest
combination.

**Outcome — the concern did not materialise.** The provocation test passed decisively:
arm-up **+3.31 % of DC, z = 37.8**; arm-down **−1.72 %, z = 19.2**; both correct sign. The
site responds to blood volume far more strongly than the forearm attempt did.

**Consequences and open risks.**
- Return-to-flow remains valid: the wrist is still distal to an upper-arm cuff, so it fills.
- Filling **volume and time course will differ** from the forearm — more tendon and bone, less
  muscle. Any filling-rate figure borrowed from forearm venous-occlusion literature does not
  transfer.
- SPEC §8's trial procedure needs updating if wrist becomes permanent.
- The red channel's drift (O1) was measured at the wrist and may be site-specific.

**Revisit if** the forearm is retried with better mechanical coupling, or if O1 traces the red
drift to something about this site.

---

<a id="d19"></a>
## D19 — Pressure path compiled out behind `PRESSURE_ENABLED`

**Status:** accepted · 2026-10-05

**Decision.** HX710B support is written but `#if`-d out, defaulting off. With it off the
firmware omits the pressure fields from `S` lines and the host records PPG only.

**Why.** The sensor is not owned yet. Writing it now while the datasheet details are fresh —
the 60 µs SCK ceiling, the 27-pulse mode select — is cheaper than returning later. Compiling
both paths on every change keeps the disabled branch from rotting.

**Caveat.** Compiled, never executed. The timing-critical parts are exactly the ones a
compiler cannot check.

---

<a id="d20"></a>
## D20 — Explicit LED current flags and die-temperature bracketing

**Status:** accepted · 2026-10-06

**Context.** O1 needs a controlled experiment: vary red LED current, hold everything else
fixed, compare drift. Auto-tune picks currents on its own, so it cannot answer the question.

**Decision.**
- `--ir-amp` / `--red-amp` on `bench_check.py` set currents explicitly and exclude that
  channel from `--auto-tune`, so one channel can be pinned while the other is tuned.
- Every capture is bracketed by a `TEMP` reading, and the rise is reported and written into
  the header.

**Why the temperature.** The self-heating hypothesis for O1 was inferred, not measured.
Datasheet Table 13 gives up to 8 °C rise at high LED current and duty cycle, and red output
and wavelength both move with it. Reading the die directly turns a hypothesis into a
measurement, and costs one serial command either side of the run.

**Also fixed here.** The plain-capture CSV writer still emitted only three header lines —
the same gap already fixed for provocation captures. Both now write the full firmware
header, so any two recordings are comparable.

**And: results that only existed on screen are now saved.** `--save` also writes a `.log.txt`
of the full console report, and the auto-tune trajectory (the amp → DC pairs) goes into the
CSV header as `autotune_<channel>`. That trajectory is a DC-versus-LED-current curve — real
data bearing directly on O1 — and it was previously printed and discarded. Warnings, verdicts
and tuning steps live nowhere else, so without this a run could not be reviewed after the
fact or shared.

---

<a id="d21"></a>
## D21 — A strongly wrong-signed manoeuvre vetoes "responds"

**Status:** accepted · 2026-10-09 · bug fix

**Problem.** The verdict returned `responds` if *any* manoeuvre had the right sign and
z ≥ 3, without checking whether another had the wrong sign. On a run from 2026-10-06 both
manoeuvres drove counts **down** — arm-up by −6.5 %, arm-down by −10.4 % — because the strap
was settling monotonically through the capture. Arm-down happened to agree with that drift,
so the run was reported as a working sensor.

**Decision.** A new `contradictory` verdict. `responds` now requires at least one strong
correct effect **and no strong wrong-signed one**.

**Why it matters.** This is the exact failure the provocation test exists to catch. A
monotonic baseline shift makes whichever manoeuvre points the same way look correct, so the
test was vulnerable to the artifact it was designed to detect.

**How to recognise it in the raw trace:** both manoeuvres moving counts the same direction.
Re-seat the sensor, let it settle, re-run.

---

<a id="d22"></a>
## D22 — Pulse detection requires ≥30 s, with an adaptive Welch window

**Status:** accepted · 2026-10-09 · bug fix

**Problem.** `nperseg` was fixed at 8 s. On a 10 s capture that averages **one** segment —
a raw periodogram, whose variance equals its mean — so "prominence" was measured on noise.
The same 10 s record from 2026-10-09 reported **195, 255 or 30 bpm** depending only on the
segment length chosen. Across channels it gave 195 bpm on IR and 172 on red, which a real
pulse cannot do.

**Decision.**
- `nperseg = clip(len/4, 4 s, 10 s)` — target ~8 averaged segments while keeping resolution
  fine enough to separate a rate from its harmonics.
- Skip the pulse verdict entirely below **30 s of data**, and say why.
- Gate on **data duration, not wall-clock**: a stalled link can burn 60 s and deliver 10 s
  of samples, and it is the sample count that limits the spectrum.

**Result.** The same 10 s capture now reports **75 bpm on both channels** — plausible, and
agreeing across wavelengths, which is the signature of a real pulse rather than noise.

**Consequence.** The 10 s auto-tune run no longer claims anything about pulse. Any capture
meant to answer "is there a pulse" must be ≥30 s.

---

<a id="d23"></a>
## D23 — Non-blocking serial reads, with a watchdog behind them

**Status:** accepted · 2026-10-09 · bug fix

**Problem.** A 60 s capture froze at 24 s with no error and no traceback — the program just
stopped. D17 had already added a 3 s stall detector, and it never fired.

**Cause.** The stall detector sits *after* `readline()` returns. pyserial's blocking
`readline()` on Windows does not reliably honour its timeout once a USB CDC device vanishes
mid-transfer; the call hangs indefinitely, so the detector never gets a turn. The timeout
being respected was an assumption, not a guarantee.

**Decision.**
1. **Poll `in_waiting` and assemble lines ourselves** instead of calling `readline()`. The
   read never blocks, so the stall timer always runs — and `in_waiting` raises promptly on a
   dead handle, which is the signal we want anyway.
2. **A watchdog thread** as the last line of defence: if nothing arrives for
   `max(4 × stall_timeout, 10 s)` it calls `cancel_read()` and closes the handle from
   outside, turning any remaining hang into an exception. It reports itself distinctly,
   since firing means something wedged *below* our timeout layer.

**Consequence.** `Device.readline()` no longer blocks; it returns `""` when nothing is ready
and sleeps 2 ms to avoid spinning. Callers already loop against their own deadlines, so this
is transparent to them.

**Tested** with a mock whose reads wedge partway through: raises `LinkStalled` in well under
10 s and keeps the samples received before the hang.

---

<a id="d24"></a>
## D24 — ~~A harmonic series is the strongest evidence of a real pulse~~

**Status:** **superseded by D30** · 2026-10-09

**Context.** Peak prominence alone kept being ambiguous: 4.4 dB on one capture, with no way
to tell a weak real pulse from a lucky noise bump.

**Decision.** After finding the best in-band peak, check for peaks near 2× and 3× it.

**Why.** A PPG beat is sharply peaked, not sinusoidal, so a real pulse necessarily puts
energy at its harmonics. Noise and 1/f drift do not produce a harmonic series. A peak with
harmonics now passes at ≥3 dB where a lone peak still needs ≥6 dB.

**Confirmed on hardware.** The clean 60 s wrist capture gave 78 bpm with peaks at 162 and
234 bpm — the 2nd and 3rd harmonics — turning "is 78 bpm real?" from a judgement call into
an automated check. Red on the same capture showed 126 bpm at 4.3 dB with no coherent
series, correctly staying unconvincing.

---

<a id="d25"></a>
## D25 — Green evaluated as a third channel, and declined

**Status:** accepted · 2026-10-09

**Question.** Could the unused green LED improve accuracy? This revisits D1, which ruled green
out of the *measurement*; the question here was whether it earns a place as an auxiliary
channel.

**What it would have bought.** Not ΔV·L accuracy — the two-wavelength solve is fixed at
660/880 nm and green has no coefficient. Two real but indirect benefits: motion
discrimination (green at 0.3–0.8 mm vs IR at 1.5–2.5 mm, so a coupling change hits both
while deep filling hits mostly IR), and an independent heart-rate channel to validate the
optical front end against a reference.

**The cost, from datasheet Table 14 (p.26).** Channel-to-channel timing at 411 µs is
**1107 µs**. Three channels need 3,321 µs per sample, which does not fit the 2,500 µs period
at 400 sps; two channels (2,214 µs) do, which is exactly why SpO₂ caps there. Adding green
forces either 200 sps with 2× averaging, or 215 µs at 17-bit — **about 1.4× more noise**
either way, moving the onset threshold from 0.627 to ~0.88 µm/s.

**Decision.** Decline. Protect the measurement channels.

**Why.** The benefits are diagnostic, not measurement accuracy, and both have adequate
substitutes: motion can be flagged from transient speed (D27), and IR already shows a clean
78 bpm with 2nd and 3rd harmonics for the heart-rate cross-check. Paying a permanent 1.4×
noise penalty on the measurement for diagnostic convenience is the wrong trade.

**Revisit if** motion artifacts prove unflaggable from IR/red alone, or if the noise budget
later turns out to have slack we are not using.

---

<a id="d26"></a>
## D26 — Scope: what "accurately measure blood volume change" means here

**Status:** accepted · 2026-10-09 · **user's call**

Four scoping decisions, taken together because each constrains the others.

| Question | Decision |
|---|---|
| Accuracy target | **Relative and monotonic.** The signal must be proportional to true volume change and repeatable; absolute scale stays arbitrary. |
| Validation | **Graded dose-response** (D28), plus a **Whoop** as an independent heart-rate reference for the optical front end. |
| Stability window | **2–5 minutes** — one full return-to-flow protocol with margin. |
| Motion | **Detect and flag** (D27). Flagged spans are excluded, never repaired. |

**Why relative is enough.** Return-to-flow reads MAP off the *timing* of filling onset; a
constant scale error on ΔV·L cancels. Absolute µm would need the optical path length
(differential pathlength factor), which varies with tissue, wavelength and geometry, and a
reference standard to calibrate against — a known-hard problem that nothing downstream
currently needs.

**Consequence.** ΔV·L remains volume × an unknown path length. Its units are honest only as
*relative* micrometres. Any future claim of absolute volume reopens this decision.

---

<a id="d27"></a>
## D27 — Motion is flagged by speed, with a sigma test and an absolute floor

**Status:** accepted · 2026-10-09

**Context.** With green declined (D25), motion has to be separated from blood volume without
a depth reference.

**Decision.** Flag a sample when its fractional rate of change is both **> 8 robust sigmas**
*and* **> 2 % of DC per second**. Dilate each span by 0.75 s, merge spans closer than
0.25 s. Exclude flagged samples from analysis; never interpolate across them — an onset
sitting on invented data is worse than a gap.

**Why speed works.** Motion and filling live on different timescales. On real captures a
settled still run never exceeds **1.3 %/s**; genuine arm movement reaches **17 %/s** at the
95th percentile and **187 %/s** at peak. Venous filling is a ramp over seconds.

**Why both tests.** The first version used the sigma test alone and flagged a clean still
capture: on a very quiet signal the robust sigma collapses and ordinary noise clears
8 sigma. The absolute floor sits in the 1.3–17 %/s gap with room on both sides.

**Verified on hardware.** Clean 60 s capture: 0 events. Provocation run: all 4 arm
movements caught. A synthetic −0.1 %/s filling ramp passes through untouched.

---

<a id="d28"></a>
## D28 — Graded dose-response is the validation, with a magnitude gate

**Status:** accepted · 2026-10-09

**Protocol.** Arm straight, rotated at the shoulder: +90°, +45°, −45°, −90°, with
heart-level rests between (`--graded`, 170 s). Wrist height ≈ arm length × sin(angle), and
hydrostatic pressure is ρgh — predictable from geometry alone. So blood volume should fall
monotonically as the wrist rises. That is the strongest validation available with no
reference instrument, and it tests exactly the D26 target.

`rest1` is 30 s so the cardiac pulse can be resolved there (D22) and compared against the
Whoop via `--whoop-bpm`.

**Judged by rank, not by line.** Spearman correlation asks only whether higher wrist means
less blood. Linearity (R²) is reported but not required — venous compliance is itself
non-linear, so a curve is physiology, not error.

**The magnitude gate — found by the tests.** Four noise values land in perfect descending
order **1 time in 24**. A synthetic flat run (seed 9) produced ΔV·L of
[0.04, 0.03, 0.01, 0.00] µm — Spearman −1.00, "strictly monotonic" — on a recording whose
verdict was *flat*. Rank order alone would certify a dose-response ~4 % of the time with no
response at all. So a dose-response is only **supported** when the extreme positions
(±90°, the largest expected effect) are individually significant, z ≥ 3.

**Expected sign comes from the angle, not the phase name**, so any protocol with angles in
`PHASE_ANGLE_DEG` gets correct sign checks.

---

<a id="d29"></a>
## D29 — Hardware captures are tracked in git

**Status:** accepted · 2026-10-09 · after data loss

**What happened.** `data/*.csv` was gitignored, and pre-commit cleanups ran
`rm -f data/*.csv data/*.png`. That permanently deleted
`provocation_wrist_20261006_133005` — the run that established the wrist as viable
(z = 37.8). Its numbers survive in D18 and the conversation record, but the raw data cannot
be re-analysed with D27's motion detection or D21's corrected verdict.

**Decision.** Hardware captures are committed. Only regenerable artifacts stay ignored:
`sim_*`, `synth_*`, scratch `_*`, and `results.csv`. A capture of a real arm at a real moment
cannot be regenerated, and at ~180 kB per minute there is no cost argument against keeping
it.

**Rule going forward.** Never bulk-delete `data/`. Remove specific known-scratch files by
name only.

---

<a id="d30"></a>
## D30 — Harmonics are informational only

**Status:** accepted · 2026-10-09 · **reverses D24**

**What D24 claimed.** That noise and drift do not produce a harmonic series, so a 3–6 dB peak
with peaks near 2× and 3× it could pass as a pulse.

**Why it was wrong.** The implementation matched *any* peak of ≥2 dB prominence within
1.5 bins of 2× or 3×, and a noisy spectrum has dozens of those. Measured directly — the
fraction of arbitrary in-band frequencies that "have harmonics":

| harmonic prominence bar | noisy rest (graded run) | clean 60 s capture |
|---|---|---|
| 2 dB (as implemented) | **62 %** | 69 % |
| 4 dB | 14 % | 34 % |
| 6 dB | 0 % | 21 % |

So the rule mostly certified chance. It passed a 129 bpm "pulse" at 3.5 dB on the graded run
while red, in the same window, put the rate at 104 bpm.

**Decision.** A pulse needs **≥6 dB prominence on its own**. Harmonics are still reported,
with no effect on the verdict.

**What survives.** The clean capture's 78 bpm stands on its own 14.3 dB, and its harmonics at
162 bpm (8.4 dB) and 234 bpm (5.9 dB) are genuinely prominent — consistent with a real
pulse, but no longer counted as proof of one.

---

<a id="d31"></a>
## D31 — Coupling slips are detected inside holds, and the comparisons they break are excluded

**Status:** accepted · 2026-10-09

**What happened.** The first graded run (2026-10-09 17:01) slipped **6.5 s into the +90°
hold**: both channels stepped down ~7 % in under a second and never returned. Venous
drainage moves counts *up*, smoothly. The run was reported CONTRADICTORY and NOT MONOTONIC —
which reads as the signal being wrong, when the sensor had moved.

**Why the motion detector missed it.** The arm-raise and the slip were one continuous
flagged span, and spans were classified by where they *started* — at the transition.

**Decision.** After a 5 s allowance for each transition, compare medians of consecutive
1 s windows on every channel. A jump over **1 % of DC** is a slip. Real settling moves
~0.1 %/s; a slip moves several percent in under a second. Medians ignore the spikes that
accompany the slip itself.

A slip excludes exactly the comparisons that straddle it — the manoeuvre it occurred in, or
one whose bracketing rest it shifted. Phases entirely after it share the new coupling and
remain valid. If an extreme position is excluded, the dose-response is reported
**COMPROMISED**: neither supported nor refuted.

**Verified.** Finds exactly one slip on the graded run — up90 at 36.4 s, −7.1 % — and none
on the clean capture. Ignores a 3 % exponential settle and steps at transitions.

---

<a id="d32"></a>
## D32 — Run inputs live in the CSV header; the console log writes through

**Status:** accepted · 2026-10-09 · bug fix

The graded run has no `.log.txt`. The log was buffered and written at exit, so a process
killed while the plot window was open lost it — and with it the Whoop reading and arm
length, which existed nowhere else.

The log now writes through to disk line by line, and `protocol`, `arm_cm` and `whoop_bpm` are
written into the CSV header, so the capture is self-contained either way.

---

<a id="o1"></a>
## O1 — Red channel drift: cause and mitigation

**Status:** **RESOLVED 2026-10-09** · raised 2026-10-06

**Observation.** With the arm still, red drifts ~90× more than IR:

| Phase | IR | Red |
|---|---|---|
| rest1 | −3.2 ct/s | −290 ct/s |
| rest2 | +178 ct/s | −1,064 ct/s (−0.73 %/s) |
| rest3 | −34 ct/s | −1,167 ct/s (−0.95 %/s) |

**Why it matters.** It enters ΔV·L through the `229·ΔOD₆₆₀` term and inflates the onset noise
floor. On `rest3`, dropping red halves the threshold (16.8 vs 34.3 µm/s) — but on `rest1`
two-wavelength is slightly *better* (7.65 vs 9.05), so there is no clean case for removing it.

**Evidence against the leading hypothesis (2026-10-09).** Two measurements weaken it:
a 10 s capture at red_amp=**105** (21 mA, the highest tested) drifted only +7.9 ct/s versus
IR's +3.9 — **2×**, not 90×. And die temperature on the 2026-10-06 run started at 34.7 °C,
roughly 10 °C above ambient, rising only **+0.81 °C** across 85 s. So the part does run hot,
but reaches steady state: self-heating looks like a startup transient, not ongoing drift.

The original 90× figure came from `rest1` of the 2026-10-06 run — the first 15 s after
strapping, which is exactly when contact pressure is settling. `rest2` and `rest3` followed
arm manoeuvres, so their "drift" may be genuine physiological recovery rather than
instrumental. **The confound is time-since-strapping, not LED current.**

**Original hypothesis (now doubtful).** Red LED self-heating. Auto-tune set red to 19.8 mA against IR's
8.4 mA, and datasheet Table 13 gives up to 8 °C rise at high current and duty cycle.

**Update 2026-10-09 — re-seating fixed it.** After re-strapping, a 10 s capture at
red_amp=102 drifted **+15.8 ct/s** against IR's −8.1 — around 2×, and both negligible.
Against the original `rest1` figure of −290 ct/s, red is roughly **18× more stable**. Taken
with the die-temperature evidence above, O1 looks resolved: **the drift was contact settling
in the first seconds after strapping, not LED current.** The mitigation is a settling period
before recording (`--settle`), not a current change. Confirm with one 60 s run before
closing.

**RESOLUTION.** A settled 60 s capture at red_amp=102 (20.4 mA, the high current) drifted
**+5.70 ct/s** against IR's +2.84 — a ratio of **2.0×**, with total spans of 478 and 377
counts on ~168k and ~142k DC. Against the original 90× figure, the cause is settled:

> **The drift was contact settling in the first seconds after strapping. Not LED current,
> not self-heating.** The mitigation is `--settle`, not a current change.

Red at 20.4 mA is fine. No reason to reduce it, and the A/B current test is unnecessary.

**Original next test (no longer needed).** Two back-to-back still captures at the same placement, red
current high then low, IR pinned identically in both — see D20 for the flags. Measured with
the arm **still**: drift is a resting property, and the provocation's movement transients
would only obscure it. Die temperature is now logged either side of each run, so the
heating hypothesis is tested directly rather than inferred.

---

<a id="o2"></a>
## O2 — `synth.py` noise defaults vs hardware

**Status:** **RESOLVED 2026-10-09** · raised 2026-10-06

**Problem.** `synth.py` defaults to a 3 µm/s filling ramp. The measured onset threshold in the
cleanest rest period is **7.65 µm/s** — the simulator's signal is below the real hardware's
noise floor. The SPEC §6 tests have been passing against conditions easier than reality.

**Impact.** False confidence. A green test suite currently does not imply the measurement
will work.

**RESOLUTION.** The premise was a measurement taken before the settling problem (O1) was
understood. On a clean, settled 60 s capture the threshold is **0.627 µm/s**, so the
simulator's 3 µm/s ramp sits **4.8× above** it rather than below. The synthetic tests are
conservative relative to real hardware, not optimistic. No re-anchoring needed.

**Keep in view:** this holds only for *settled* captures. A run without `--settle` had a
threshold of 7.65 µm/s, above the simulator's ramp. The settling discipline is what makes
the tests representative.

---

<a id="o3"></a>
## O3 — Is the onset noise floor low enough?

**Status:** **open** · raised 2026-10-06

**Measured 2026-10-09, settled.** Threshold `μ₀ + 3σ₀` = **+0.627 µm/s** over a clean
60 s wrist capture — **12× lower** than the best unsettled figure of 7.65 µm/s. IR-only
gives essentially the same (0.616), so red no longer costs anything.

Against the arm-raise response (~14 µm/s of dV·L), that is roughly **22× headroom**.

**Earlier, unsettled:** +7.65 µm/s in the cleanest rest phase; +34 and +113 in the two
contaminated ones.

**Unknown.** The real venous filling rate under cuff occlusion at the wrist. The arm-raise
produced ~70 µm over ~5 s settling (≈14 µm/s), which clears the threshold roughly 2×. If cuff
filling is comparable it will work; if it is slower it will not.

**Still unknown:** the real venous filling rate under cuff occlusion at the wrist. The
arm-raise is a proxy, not the measurement. But with 22× headroom against that proxy, the
noise floor has stopped being the thing most likely to sink the project.

**Resolves fully when** the HX710B arrives and a real deflation can be recorded.

---

<a id="o4"></a>
## O4 — Red is the contact-sensitive channel: two-wavelength or IR-only?

**Status:** **open** · raised 2026-10-09

**Observation.** On the graded run, between rests — arm at heart level every time, so any
change is coupling, not physiology — red's baseline moved **2–3× more** than IR's:

| | rest1→2 | rest2→3 | rest3→4 | rest4→5 |
|---|---|---|---|---|
| IR | −2.77 % | −1.11 % | +1.24 % | +1.23 % |
| red | −3.29 % | −3.21 % | +3.35 % | +1.45 % |

Red also stepped +2.7 % at the start of the −45° hold while IR barely moved. This fits O1,
where red's early drift was also contact settling: shorter wavelength, shallower
penetration, more weight on the surface where contact changes happen — and the red LED sits
at a different position in the package, so a tilt changes its coupling differently.

**Why it matters.** With the slip excluded, the three remaining graded positions are
correctly ordered in **IR-only** ΔV·L (−60 cm: +14.0, −42 cm: +3.6, +42 cm: −1.1 µm) but
**not** in two-wavelength ΔV·L, because red's coupling wander enters through the 229·ΔOD₆₆₀
term. (Three values order correctly by chance 1 time in 6, so this is suggestive only.)

**The trade-off.** Two-wavelength exists to reject desaturation of trapped blood under
occlusion (D1) — and that rejection is real, tested in SPEC §6. IR-only avoids red's contact
sensitivity but loses it. In arm-position tests no blood is trapped, so desaturation is
absent and IR-only is legitimate there; during a real cuff occlusion it is not.

**Resolves with** a clean graded run (no slips) analysed both ways, and eventually a cuff
occlusion where desaturation actually occurs. Not decided here.
