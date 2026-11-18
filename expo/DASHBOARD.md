# Expo dashboard: visual design

**Status:** design settled 2026-11-18 · not built. This is the look and layout of the monitor
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

The dashboard matches the team poster: a pale lavender ground, crimson for what matters now,
navy for type and finished things, white rounded cards. It is the poster's palette in a calmer,
clinical arrangement (white cards, crimson used sparingly).

### 2.1 Colour tokens

| Token | Hex | Used for |
|---|---|---|
| ground | `#F4F6FB` | page background |
| card | `#FFFFFF` | cards, chart backgrounds |
| card border | `#DDE3F1` | 1.5 px card outline |
| divider | `#E4E8F3` | lines inside cards, empty bar tracks |
| navy | `#1A1F3D` | text, pressure line, finished steps, dark cards |
| navy, safety | `#1A2A5C` | safety-stop state (never red on the public screens) |
| text, secondary | `#4A5070` | labels, captions |
| text, muted | `#8E97B8` | waiting states, "after the vent" |
| crimson | `#C1203B` | the wordmark, the current step, the blood line, the result, the onset |
| crimson, dark | `#9E1830` | crimson text on pale pink |
| crimson, light text | `#FCE4E7` | secondary text on crimson cards |
| pink band | `#FCF4F5` / `#FCEEF0` | "blood blocked" region, deflate band |
| lavender band | `#EAEFF9` | hold band |
| periwinkle | `#9DAFDB` / `#B9C3DE` | target ramp, sloped trend, connectors |
| light text on navy | `#C8D6F4` / `#DCE5F8` | labels and secondary text on navy cards |
| pulse blue | `#3B6FD8` | heartbeat trace, pulse-return marker, light sensor in the arm drawing |
| caution | `#FBE7B5` on `#6B4A00` | the low-confidence tag only |
| threshold amber | `#D99A2B` | dashed noise-floor and motion lines |
| ok green | `#1F6B3E` | check marks, sensor dots, "● Live" |

**Colour rules.**
- **Crimson means "now" or "the answer":** the active step, the result, the onset line. Never
  for errors on the public screens.
- **A safety stop is navy (`#1A2A5C`),** framed as the device doing its job (DESIGN §8.2).
- **Amber only for the caution tag** and dashed thresholds.
- **Finished steps are navy with a ✓; future steps are white with a grey outline.**

### 2.2 Type

| Role | Face | Weight · size |
|---|---|---|
| Wordmark "MAPQuest" | Fredoka | 700 · 42 px public, 32 px technical, crimson |
| Tagline "Blood pressure without a pulse" | Inter | 600 · 13 px |
| Big numbers (pressure, result) | Inter | 800 · 120–200 px, tabular figures, tight letter-spacing |
| Card titles | Inter | 800 · 22–38 px |
| Labels | Inter | 700 · 13–15 px, secondary colour |
| Body | Inter | 400–600 · 16–18 px |
| Event log | JetBrains Mono | 500 · 11.5 px |

### 2.3 Shapes and spacing

- **Frame:** 1600 × 900 (16:9 monitor), padding 28 × 36 px public, 22 × 32 px technical.
- **Radii:** 24–26 px hero and bottom cards, 20–22 px technical cards, 16 px strip chips,
  999 px pills, 8–12 px tags and tiles.
- **Gaps:** 20 px between public cards, 12 px between technical cards.
- **No shadows or gradients:** flat fills and 1.5 px outlines only.

---

## 3. Shared frame

Every screen has the same header row:

```
MAPQuest            ① Idle ─ ② Inflate ─ ③ Hold ─ ④ Deflate ─ ⑤ Result      Visitor   ● Live
Blood pressure                                                                Run 23   Public view
without a pulse
```

- **Stepper:** Idle → Inflate → Hold → Deflate → Result (DESIGN §6.1). Finished steps navy with ✓,
  the current step crimson (navy `#1A2A5C` in a safety stop), future steps white with an outline.
- **Run number:** anonymous, the only visitor identifier on screen.
- **Status chip:** "● Live" in green; becomes "Reconnecting" or "Analyser offline" (DESIGN §8.4).
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

**Hero card.**
- **Left column (280 px):** cuff pressure at 120 px, "mmHg" in crimson, then time in cuff and
  release speed.
- **Chart:** the run from 0 to about 80 s with Inflate, Hold and Deflate shaded (ground, lavender,
  pale pink). Cuff pressure is navy, blood in the forearm crimson. A crimson cursor and a navy
  dot mark "now"; everything after it is covered by a translucent white panel.

**Bottom row: the Narrator** (columns 1.3 : 1.2 : 0.8, 200 px tall).

| Phase | Now | Watch for | Time ring |
|---|---|---|---|
| idle | **Next up: you** · Rest your arm on the table and we'll fit the cuff. | **The cuff going on** · Then the black line climbs as it squeezes. | 80 s · in total |
| inflate | **Squeezing the cuff** · Up to 160 mmHg, so blood can't reach the forearm. | **The black line climbing** · Cuff pressure rising to 160 mmHg. | 73 s left |
| hold | **Blood flow stopped** · Holding 10 seconds so the forearm settles. | **The red line dipping** · Blood drains from the forearm while no new blood gets in. | 64 s left |
| deflate | **Releasing slowly** · Our valve lets out 2 mmHg every second. | **The red line turning upward** · That's blood squeezing back past the cuff into the forearm. | 36 s left |

- **Now** is crimson with a "Now" label; **Watch for** is white with a small rising-line icon;
  the **time ring** is navy, its pink arc proportional to the time left out of 80 s.
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
5. **"Reconnecting" and "Analyser offline"** notices are specified (§3) but not drawn in the
   mockups.
6. **Crossfade timing** for the mode switch (DESIGN §6.2) is not specified.
