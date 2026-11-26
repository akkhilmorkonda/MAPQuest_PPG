# Expo dashboard: visual design

**Status:** design settled 2026-11-18 · **built the same day** as `web/dashboard.html`, served
by `host/expo_results.py` at `http://127.0.0.1:8800/` (README, "The monitor dashboard");
tested on the laptop stand-in only. Where the build differs, §8 says so. This is the look and layout of the monitor
dashboard described in [`DESIGN.md`](DESIGN.md) §6: what is on each screen, how it changes with
the run, and the theme it shares with the team poster.
**Mockups:** the design canvas *MAPQuest expo dashboard*
(<https://claude.ai/artifact/CuLHfKPHFe8KGML7zBp5DY>, private until shared from its Share menu).
The three chosen screens are on its **Chosen** page; every other option is kept for reference.
All values in the mockups (118 mmHg, run 23, 2.02 mmHg/s…) are illustrative, except the bench
and simulation figures in §5.4 and the limits in §5.5, which come from the decision log,
the notebook and `expo_fw/config.h`.

---

## 1. Chosen screens

| Mode | Canvas board | What it is |
|---|---|---|
| Public, during the run | `B3-Live` | the run as a timeline, with the pressure beside it and three narrator cards underneath |
| Public, after the run | `B3-Result` | the whole run with the return of flow marked, the outcome beside it, and three narrator cards |
| Technical, any state | `B3c-Technical` | the onset close-up as the hero, the pipeline strip above, checks on the right, the machine below |

Each board has a switch in the canvas's Tweaks panel that walks it through every state it must
handle: `phase` on the live board, `outcome` on the result board, `state` on the technical board.
The real dashboard is one page that moves through these states as the board reports them.

---

## 2. Theme

Restyled 2026-11-23 (layout and wording unchanged): graphite ink on a cool clinical grey,
crimson for what matters now (the one colour kept from the poster, because this is a heart
project), near-square corners and hairline outlines. The earlier poster palette (lavender,
navy, periwinkle, pill shapes, 24 px radii) read as generic and was retired; the MAPQuest
logo and Fredoka wordmark stay.

### 2.1 Colour tokens

| Token | Hex | Used for |
|---|---|---|
| ground | `#ECEEED` | page background |
| card | `#FBFBFA` | cards, chart backgrounds |
| card border | `#D3D8D6` | 1 px card outline |
| divider | `#E1E5E3` | lines inside cards, empty bar tracks |
| graphite (`--navy`) | `#17191C` | text, pressure line, finished steps, dark cards |
| slate, safety | `#2F3B45` | safety-stop state (never red on the public screens) |
| text, secondary | `#4D5357` | labels, captions |
| text, muted | `#6B7175` | waiting states, "after the vent" |
| crimson | `#C1203B` | the wordmark, the current step, the blood line, the result, the onset |
| crimson, dark | `#9A1530` | crimson text on pale pink |
| crimson, light text | `#FBD9DF` | secondary text on crimson cards |
| pink band | `#F7EBED` | "blood blocked" region, deflate band |
| grey bands | `#F1F3F2` / `#E4E8E6` | inflate band, hold band |
| trend grey | `#9BA4A1` / `#A9B0AD` | target ramp, sloped trend, connectors |
| light text on graphite | `#D6DADC` | secondary text on dark cards |
| pulse blue | `#2B5FB8` | heartbeat trace, pulse-return marker |
| caution | `#F4E4BC` on `#5E4200` | the low-confidence tag only |
| threshold amber | `#D99A2B` | dashed noise-floor and motion lines |
| ok green | `#1E6A43` | check marks, sensor dots, "● Live" |

**Colour rules.**
- **Crimson means "now" or "the answer":** the active step, the result, the onset line. Never
  for errors on the public screens.
- **A safety stop is slate (`#2F3B45`),** framed as the device doing its job (DESIGN §8.2).
- **Amber only for the caution tag** and dashed thresholds.
- **Finished steps are graphite with a ✓; future steps are grey** (§3).
- **Crimson on the public screens is text and lines, never a filled panel**, except the result's
  "Blood is back" tag and the dashed onset line.

### 2.2 Type

All fonts ship in `web/fonts/` (WOFF2, served by `dashserve.py`), because the booth laptop is
offline. Bricolage Grotesque (chosen 2026-11-23 over Atkinson Hyperlegible, whose slashed zero
read "1Ø6") carries all text and numbers; its zero is plain.

| Role | Face | Weight · size |
|---|---|---|
| Wordmark "MAPQuest" | Fredoka | 700 · 42 px public, 32 px technical, crimson |
| Logo (`web/logo.webp`) | image | left of the wordmark: 64 px tall public, 42 px technical; also the tab icon |
| Big numbers (pressure, result) | Bricolage Grotesque | 800 · 176 px live, 200 px result, tabular figures |
| Public captions | Bricolage Grotesque | 800 · 46 px "Now" (36 px for the longer outcome lines), 30 px the other two |
| Values | Bricolage Grotesque | 800 · 26–30 px (time in cuff, release speed, run number) |
| Labels | Bricolage Grotesque | 400 · 15 px secondary colour; 700 for "Now", "Watch for", "What it means" |
| Body | Bricolage Grotesque | 400 · 19 px public; 12–17 px technical |
| Event log | Atkinson Hyperlegible Mono | 11.5 px |

Public type scale: 15 / 19 / 30 / 46 / 176 px.

### 2.3 Shapes and spacing

- **Frame:** 1600 × 900 (16:9 monitor), padding 28 × 36 px public, 22 × 32 px technical.
- **Public screens have no cards:** the number, the chart and the three messages sit on the
  page ground, separated by space and 1 px rules. The technical screen keeps its 6 px cards.
- **Radii:** 6 px technical cards, 4 px strip chips, 2–3 px tags and chips; square numbered
  badges. No pills.
- **No shadows or gradients:** flat fills and 1 px lines only.

---

## 3. Shared frame

Every screen has the same header row:

```
MAPQuest            ① Idle ─ ② Inflate ─ ③ Hold ─ ④ Deflate ─ ⑤ Result      Visitor   ● Live
Blood pressure                                                                Run 23   Public view
without a pulse
```

- **Stepper:** Idle → Inflate → Hold → Deflate → Result (DESIGN §6.1). On the public screens it
  is plain text: finished steps graphite with ✓, the current step crimson and underlined (slate in
  a safety stop), future steps grey. The technical screen keeps small filled chips.
- **Run number:** anonymous, the only visitor identifier on screen ("Visitor Run 8" on one line).
- **Status:** "● Live" in green beside "Public view"; becomes "Reconnecting" or "Analyser
  offline" (DESIGN §8.4). During a run the public numbers then grey out and "Paused: last
  reading N s ago" appears under the pressure, so a frozen number never looks current.
- **Mode label:** "Public view" or "Technical view", small, so a presenter knows which is up.

---

## 4. Public mode

### 4.1 During the run (`B3-Live`)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ header + stepper                                                             │
├──────────────┬───────────────────────────────────────────────────────────────┤
│ Cuff pressure│  Inflate │ Hold │ Deflate · 2 mmHg/s                          │
│     112      │  ─── cuff pressure (navy) ───                                 │
│     mmHg     │  ─── blood in the forearm (crimson) ───        │ now          │
│              │                                         (rest greyed out)     │
│ Time in cuff │                                                               │
│ Release speed│  0 s                          about 80 s · vents at 40 mmHg   │
├──────────────┴──────────┬──────────────────────────┬─────────────────────────┤
│ NOW (crimson)           │ WATCH FOR (white)        │ (ring) 36 s left (navy) │
│ Releasing slowly        │ The red line turning     │ then the cuff empties   │
│ Our valve lets out 2…   │ upward                   │ by itself               │
└─────────────────────────┴──────────────────────────┴─────────────────────────┘
```

**Reading and chart** (restyled 2026-11-23, no card).
- **Left column (330 px):** cuff pressure at 176 px, "mmHg" in grey, then time in cuff and
  release speed as a two-column list directly beneath.
- **Chart:** the run from 0 to about 80 s. Phase names (Inflate, Hold, Deflate · 2 mmHg/s) sit in
  a row above the plot; Hold is shaded grey, Deflate pale pink only as far as it has happened, the
  rest of the phase a thin pink strip. A pressure scale (0–160, headed "mmHg") belongs to the
  cuff line; blood in the forearm is relative. Cuff pressure is graphite, blood crimson, each
  labelled at its end (no legend). A dashed graphite line and two dots mark "now".

**Bottom row: the Narrator** (columns 1.25 : 1.15 : 0.8, 204 px tall, one open band under a 1 px
rule with dividers, no cards).

| Phase | Now | Watch for | Time ring |
|---|---|---|---|
| idle | **Next up: you** · Rest your arm on the table and we'll fit the cuff. | **The cuff going on** · Then the black line climbs as it squeezes. | 80 s · in total |
| inflate | **Squeezing the cuff** · Up to 160 mmHg, so blood can't reach the forearm. | **The black line climbing** · Cuff pressure rising to 160 mmHg. | 73 s left |
| hold | **Blood flow stopped** · Holding 10 seconds so the forearm settles. | **The red line dipping** · Blood drains from the forearm while no new blood gets in. | 64 s left |
| deflate | **Releasing slowly** · Our valve lets out 2 mmHg every second. | **The red line turning upward** · That's blood squeezing back past the cuff into the forearm. | 36 s left |

- **Now** has its caption in crimson text (the only crimson block on the public screen);
  **Watch for** is graphite text; **time left** is a number with a thin crimson bar proportional
  to the time left (it replaced the ring).
- **Time left** during deflation is (pressure − 40) ÷ 2 s. During inflation it is an estimate,
  because pumping is by hand.

### 4.2 After the run (`B3-Result`)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ header + stepper (Result lit)                                                │
├──────────────────────────────────────────────────────┬───────────────────────┤
│ The whole run                                        │ Return-of-flow        │
│  Inflate │ Hold │ Deflate                            │ pressure              │
│  ─── pressure ───     ┊ ● Blood is back              │   118                 │
│  ─── blood ───────────┊────────╱                     │   mmHg                │
│                      onset                           │ (caution tag)         │
├─────────────────────────┬────────────────────────────┼───────────────────────┤
│ WHAT WE FOUND (crimson) │ WHAT IT MEANS (white)      │ Want to try it? (navy)│
└─────────────────────────┴────────────────────────────┴───────────────────────┘
```

**Hero card.**
- **Chart:** the whole run, the same bands and lines as the live screen. For a result, a crimson
  dashed line at the onset with dots on both curves and a "Blood is back" tag. For a safety stop,
  the run ends at the stop with a navy "Cuff released itself" line.
- **Outcome panel (470 px, right):**

| Outcome | Panel |
|---|---|
| result | "Return-of-flow pressure" · **118** at 200 px · mmHg · "The cuff pressure when blood flowed back into the forearm." |
| caution | as result, plus the amber tag "Low confidence: arm moved during the release" |
| no-reading | **No clear reading** · "The blood came back too faintly to pin down a pressure. Want to try again?" |
| safety-stop | navy shield icon · **Safety stop: the cuff released itself** · "It was up long enough, so it let go for comfort. No reading this run." |
| analysing | three crimson-to-pink dots · **Analysing…** · "Finding the moment blood came back." |

**Bottom row: the Narrator** (the W1 board exactly; 1.3 : 1.2 : 0.8, 200 px).

| Card | Content |
|---|---|
| **What we found** (crimson) | follows the outcome, below |
| **What it means** (white) | **For you, this lands near your systolic.** · For an LVAD patient with no pulse, the same moment is their MAP, the number clinicians keep at 70–90. |
| **Want to try it?** (navy) | About 80 seconds. Ask anyone on the team. |

| Outcome | What we found |
|---|---|
| result | **Blood came back at 118 mmHg** · Found with light alone. No pulse needed. |
| caution | **Blood came back at 118 mmHg** · Your arm moved during the release, so treat this as a rough reading. |
| no-reading | **No clear reading this time** · Blood came back too faintly to pin down. Another run usually works. |
| safety-stop | **The cuff released itself early** · A built-in safety limit ended the run. No reading this time. |
| analysing | **Working it out…** · Finding the moment blood came back. |

The number appears twice on the result screen (panel and "What we found") on purpose: the
panel is for people across the booth, the card for people reading the story.

---

## 5. Technical mode (`B3c-Technical`)

Aimed at a **judge's deep dive**: it explains how the reading is made while still showing every
status a teammate needs.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ MAPQuest  Finding the moment blood came back   stepper   Run 23 · …  ● Live  │
├────────┬────────┬────────────┬───────────┬──────────────┬────────────────────┤
│1 Raw   │2 ΔOD   │3 Blood vol.│4 Low-pass │5 dV/dt vs    │6 Hinge onset       │
│ light  │        │            │  0.3 Hz   │  μ₀+3σ₀      │  118 mmHg          │
├────────┴────────┴────────────┴───────────┴──────────┬───┴────────────────────┤
│ Close-up: blood volume, 140 → 90 mmHg               │ How the corner is found│
│  ① trend (dashed) ─────                             │  ① ② ③                 │
│                    ② corner ┊ ─── hinge fit ③       ├────────────────────────┤
│  Hinge onset · 118.2   pulse return · 115.4         │ Confidence checks      │
│                        intersect · 112.6            │  ✓✓✓✓ + 2 traces       │
│                                                     ├────────────────────────┤
│                                                     │ How well does it work? │
├──────────────┬──────────────┬───────────────────────┴──────┬─────────────────┤
│ Cuff         │ Safety       │ Hardware                     │ Link and events │
│ controller   │ margins      │ bands ─ mux ─ ESP32 ─ MPRLS  │ rates, log      │
└──────────────┴──────────────┴──────────────────────────────┴─────────────────┘
```

### 5.1 Pipeline strip

Six chips joined left to right: **Raw light · ΔOD · Blood volume · Low-pass 0.3 Hz ·
dV/dt vs μ₀ + 3σ₀ · Hinge onset**, each with a sparkline.
- **During the run** chips 1–3 draw live up to "now". Chips 4–5 are dashed with "after the
  vent", because the validated filters are zero-phase and need the whole run (SPEC §5.1).
- **After the run** all five draw in full. Chip 6 is crimson with "118 mmHg" for a result,
  dashed with "none found" for no-reading, "fitting…" while analysing, "—" otherwise.

### 5.2 Hero: onset close-up

Low-passed blood volume against cuff pressure, **140 → 90 mmHg**, the window around the corner.

| State | Hero |
|---|---|
| idle · inflate · hold | empty axes with one muted line: "Waiting for a visitor…", "Inflating. The close-up starts when the release reaches 140 mmHg.", "Holding at 160. The last 3 s of the hold become the reference window." |
| deflate | the raw volume drawn live up to a navy "now · 112 mmHg" cursor; "The corner is fitted after the vent: the filters need the whole run." |
| analysing | the full curve with a navy "Fitting the hinge…" tag |
| result · caution | pink "blood blocked" band up to the onset; dashed sloped occlusion trend; crimson curve; dotted navy hinge fit; crimson onset line and dot with "Hinge onset · 118.2 mmHg"; dashed blue **pulse return** and grey **intersect** marks; **badges ① ② ③** on the trend, the corner and the curved refill |
| no-reading | a curve that never leaves the trend, with "No sustained rise above the noise floor before the vent" |
| safety-stop | "Stopped by the hold limit before the release began. The cuff vented." |

The header note above the chart says what is being shown: "live, raw volume · fit pending",
"windows from board events · refill shape: soft start (p = 2)", "motion flagged at 104 mmHg"…

Steep refill is not shown anywhere (DESIGN §2).

### 5.3 Right column

1. **How the corner is found** (replaces a big result card: the chart already labels the
   reading). Three numbered steps matching the hero's badges:
   1. **Fit the drain.** With the artery shut, blood slowly leaves the forearm. That slope is the baseline.
   2. **Find the corner.** Where the curve leaves the baseline for good. Its cuff pressure is the reading.
   3. **Allow a soft start.** Slow releases refill gradually, so the fit may bend (p = 2) instead of snapping.

   A note beside the title follows the state: "runs after the vent", "running now", "done for
   run 23", "done · motion flagged", "no corner this run", "skipped: no release".
2. **Confidence checks:** Onset found · Arm still · Flow fully stopped · Signal complete
   (DESIGN §7.4). Each shows – (not yet), … (checking), ✓, ! (caution, amber) or ✗ (failed, crimson)
   with a one-line reason. Under them, two small traces drawn up to "now":
   - heartbeat in the forearm against a dashed line at 3× its stopped level;
   - arm motion from the forearm IMU against a dashed threshold (it spikes in the caution state).
3. **How well does this work?** "Bench: refill onset read −6.2 ± 7.9 mmHg against systolic
   over 8 slow manual runs." (D47) Always labelled as bench data, not expo data.

### 5.4 Bottom row

| Card | Content |
|---|---|
| **Cuff controller** | the five states as small chips (done navy, current crimson, "Dump · hold limit" navy in a safety stop); target ramp (thick periwinkle) against actual pressure (thin navy) drawn up to now; release rate and valve state ("closed", "6.8 % open", "7.1 % mean") |
| **Safety margins** | over-pressure (of 280 mmHg), hold timeout (of 120 s), run timeout (of 135 s) as bars of how much is used; a tripped limit turns its bar and text crimson, reads "tripped", and gives the card a crimson outline. Footer: "All limits armed · watchdog 1 s" during the run, "No safety stop · vented at 40 mmHg" after, "Tripped: hold timeout → valve opened" on a stop |
| **Hardware** | forearm band and upper-arm band (100 Hz) → mux → ESP32 → MPRLS (100 Hz) and valve, each with a status dot; the valve label follows the run (closed, pulsing, open) |
| **Link and events** | stream rates, round trip and missing samples on one line; the last three board events in monospace (e.g. "0:31 view → technical", "1:20 dump · stop pressure", "1:21 result 118 → phone, monitor") |

### 5.5 What each element reads

| Element | Source |
|---|---|
| pressure, rate, valve duty, controller state | the board's `C` lines (10 Hz) |
| stepper, phase bands, windows, events | `EV,state` and other `EV` lines |
| raw light, ΔOD, live blood volume | `S` lines (100 Hz), computed in the laptop process |
| low-pass, dV/dt, hinge, markers | `host/mapquest` after the vent (DESIGN §7) |
| confidence checks | the laptop process's checks (DESIGN §7.4) |
| heartbeat trace during the run | needs the live pulse detector (DESIGN §9.3) |
| arm motion | `M` lines (IMU) |
| safety limits | `INFO` (limits) plus timers from the `C` and `EV` lines |
| link stats | the laptop process's link counters (`expotool/linkstats.py`) |
| time left (public) | (pressure − 40) ÷ 2 during deflation |

---

## 6. Options kept on the canvas

Kept for reference, not chosen:

| Page | Boards | Idea |
|---|---|---|
| Explorations | A (3 boards) | poster-faithful: pink and periwinkle pills, numbered badges, bubbly wordmark |
| Explorations | B, B2 | clinical first passes; B2 has a fixed left rail with a vertical stepper |
| Explorations | B3 technical, B3a, B3b, B3c public | full-bleed timeline (B3a), release plotted against pressure like the poster (B3b), phases as growing columns (B3c) |
| Chosen | W1–W10 | bottom-widget sets for the public screens; **W1 Narrator chosen**. Others: inside the arm, pulse vs blood, for the person in the cuff, why it matters, home cuff vs MAPQuest, today at the booth, engineering proof, what's next, one bar |
| Chosen | T1–T6 | technical panel sets: explain every stage, instrument rack, signal quality, hardware live, safety story, evidence. Not used; the original B3c panels were kept with one refinement (§5.3) |

Ideas worth reusing later: T6's evidence cards (D47 table, 22 of 22 runs, the no-pulse
simulation) for a judges' Q&A slide, and T5's fail-safe cards for the IRB safety story.

---

## 7. Open

1. **Idle screen content** beyond "Next up: you" (what MAPQuest is, the LVAD story).
2. **The caution tag on the technical screen.** It used to sit on the removed result card; now
   the flag shows only as "!" in the checks, the motion spike and the chart note. Add a chip to
   the chart header if judges need it plainer.
3. **Live heartbeat trace** needs the live pulse detector (DESIGN §9.3); until then it can only
   be drawn after the vent.
4. **Inflation countdown** is an estimate while pumping by hand.
5. ~~"Reconnecting" and "Analyser offline" notices~~: built as the status chip's text (§3).
6. ~~Crossfade timing~~: built as a 0.35 s fade.

---

## 8. As built (2026-11-18)

- **Fonts:** the booth laptop is offline, so the page loads no web fonts; since 2026-11-23 the
  three faces (§2.2) ship in `web/fonts/` and `dashserve.py` serves them.
- **Fixed 1600 × 900,** scaled to fit the window (letterboxed on other aspect ratios).
- **Public timeline axis** is the run's own expected length (to the vent at `p_stop`), not a
  fixed 80 s, so a slow pump or a long hold still fits.
- **Live close-up** shows the release so far (at least 60 mmHg of it, growing as the pressure
  falls), since the corner is unknown until the vent; after the vent it zooms to the corner
  (onset + 22 down to 50 mmHg below).
- **Heartbeat trace** draws after the vent only (open item 3).
- **The outcome** stays up 2 minutes, then the idle screen (no "Next visitor" yet).
- **Link line** shows rates, the results process's own `ECHO` round trip and missing samples;
  the event log shows the run's events (device connect/disconnect churn is left out).
- **Safety-stop text** follows the dump reason with DESIGN §8.2's public lines, instead of
  the mockup's single hold-timeout sentence.

## 9. UI-review fixes (2026-11-26)

From the `ui-review` of 2026-11-26 (findings 1–6; restore point: tag `dashboard-review-baseline`).
Wording is unchanged; these supersede §4 and §5 where they differ.

- **Two strips on the public chart.** Cuff pressure on top with its mmHg scale; blood in the
  forearm below, relative, with no scale, since it is not a pressure. On a result, "Blood is
  back" marks the pressure line at the onset, where the reading actually is.
- **Labels never sit on a line.** Line labels, the result tag, the technical "corner is fitted
  after the vent" note and the "sloped occlusion trend" label are placed in the first clear spot
  from a short list of candidates (`placeText` in `web/dashboard.html`).
- **Live cuff pressure rides on the chart,** beside the head of its line like an instrument
  readout; the left number column is gone and time in cuff and release speed sit under the chart.
- **Technical view in three ruled zones** (pipeline row, close-up with its notes, the machine
  row), divided by rules instead of 13 outlined boxes. A tripped safety limit tints its zone.
- **No middle-dot strings:** values sit beside their labels, or follow a comma or colon.
- **Labels beside, not above:** "Now", "Watch for", "What we found" and "What it means" hang to
  the left of their text; "Return-of-flow pressure" sits beside the result number; "The whole
  run" names the time axis.

## 10. UI-review fixes 7–9 (2026-11-26)

Wording unchanged; restore points: tags `dashboard-review-baseline` (before any fixes) and
`dashboard-review-fixes-1-6`.

- **One type scale.** Visitor screens: 40 px (the lead caption), 24 px (secondary headlines,
  "Watch for", time left), 17 px (body, line labels), 14 px (labels, axes). Technical: 24, 17,
  14 and 12 px. Text uses two weights, 400 and 700; the heavy 800 is kept for the numbers (the
  live readout, the result, the onset).
- **"Watch for" is written on the chart,** beside the line it describes (the black line while
  inflating, the red line from the hold on; where the black line will climb while idle), with a
  hairline leader when it has to sit apart, never across another label. During a run the bottom
  row is "Now" (leading) and the time left; the outcome keeps three columns sized by their text.
- **Drawn status marks** (`icon()` in `web/dashboard.html`: tick, cross, alert, pending ring,
  working dots, dot) replace the ✓ ● ✗ – ! … characters in the steppers, confidence checks,
  the Live badge and the hardware panel.
