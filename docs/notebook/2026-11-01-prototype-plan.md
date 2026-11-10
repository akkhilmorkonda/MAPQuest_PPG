# Notebook entry: where the prototype stands and what comes next

**Date:** 1 November 2026
**Author:** [name]
**Covers:** cuff testing from 9 to 19 October 2026, and the prototyping plan from here

---

## Summary

**The goal.** Read blood pressure from the moment blood returns to the forearm as an
upper-arm cuff deflates. The end use is mean arterial pressure (MAP) in patients with a
heart pump (LVAD), who have little or no pulse.

**The prototype as it stands.**
- **Optical sensor:** a red and infrared light sensor (MAX30101) on the inner forearm,
  3–7 cm below the cuff, measuring how much blood is in the tissue.
- **Pressure sensor:** a cuff pressure sensor (MPRLS) on the same data stream, so light and
  pressure share one clock.
- **Electronics and software:** an Arduino Nano ESP32, and a laptop monitor that records
  every run.
- **Reference:** a home BP monitor reading. Since 18 October, an automated BP monitor drives
  the cuff and gives its reading from the same deflation.

**What we tested.** 30 cuff runs over four phases, 22 of them scored against a reference
BP:

| Phase | Dates | Runs | What changed |
|---|---|---|---|
| 1. Getting a signal | 9–11 Oct | 7 | Cuff pressure first read from a phone video, then a sensor |
| 2. Manual with reference | 16–17 Oct | 8 | Reference BP before each run; sensor at 7 cm, then 3 cm |
| 3. Automated cuff | 18 Oct | 6 | Monitor runs the cycle and reads the same deflation |
| 4. Mixed | 19 Oct | 9 | 5 slow manual runs, 4 automated runs |

**Key results.**
- **Return of flow is detected on every run.** In a person with a pulse it marks systolic.
  - **Automated runs:** −3 ± 5 mmHg from the monitor's systolic, with 7 of 10 within
    5 mmHg.
  - **Manual runs:** −6 ± 7 mmHg, but their reference was taken minutes before the run.
- **The MAP marker is not reproducible yet.** A "surge" in the refill landed within
  5 mmHg of MAP on 5 of 6 slow runs on 16–17 October and on none of the 5 slow runs on
  19 October.
- **How the cuff is run matters as much as the analysis.** The deflation speed, the hold
  at the top and the inflation speed all change what the signal looks like.

---

## What went well

- **One clock for both sensors.** Putting cuff pressure on the same stream as the light
  sensor removed every timing question. Reading the gauge off a phone video worked as a
  stopgap, but this is far better.
- **Return to flow is robust.** It was found on all 22 scored runs, at two sensor
  distances, with manual and automated cuffs, and across several days.
- **The automated monitor improved the reference.** Taking the reference from the same
  deflation cut the apparent error roughly in half. It also made runs repeatable: the
  cycle is identical every time.
- **Same-day turnaround.** Every run was analysed within minutes and fed into the next
  session's changes. That's how we caught the fast-deflation problem, the effect of the
  hold, and the blood pooling caused by slow inflation.
- **We understand the signal now.** Each phase of a run has a physical explanation:
  pooling while the cuff inflates, the drain while it's closed, the plateau, the refill,
  the release. That makes odd runs explainable instead of mysterious.
- **Decisions are written down.** Every change and its reason is logged, so results can
  be traced back.

---

## What we need to do

These are prototyping and testing tasks, not software.

### 1. Control the cuff ourselves

- **Slow the deflation.** We need a steady, adjustable deflation of about 2 mmHg/s. Both
  the MAP marker and an accurate systolic depend on it, and the automated monitor runs at
  about 4 mmHg/s. Options:
  - a needle valve or bleed restrictor in the monitor's line;
  - our own pump and valve, with the monitor used only for its reading.
- **Hold at the top.** Hold the cuff for 20–30 s at 150–160 mmHg before deflating, so the
  forearm's blood volume settles. On every run where it settled, the onset was
  unambiguous.
- **Inflate quickly.** A slow inflation pools blood in the forearm and shrinks the refill.
  Inflate in under about 3 s.
- **Done when:** the cuff can repeat a set profile (fast inflation, hold, steady 2 mmHg/s
  release) and the same-deflation reference reading is still available.

### 2. Make sensor placement repeatable

- **Build a holder.** We need a strap or bracket that fixes the sensor's position, its
  distance from the cuff edge and its contact pressure, and shields it from room light.
  Sensor slipping and contact changes were early problems, and runs on different days
  looked very different.
- **Mark the spot.** Use a skin marker, and measure the cuff-to-sensor distance every
  time.
- **Add strain relief to the cable,** so arm movement doesn't tug the sensor.
- **Done when:** the sensor can be taken off and put back on the same spot, giving a
  similar signal size across sessions.

### 3. Write a standard test procedure

- **Subject preparation:** seated, rested 5 minutes, arm supported at heart level, elbow
  straight, palm up.
- **Between runs:** 1–2 minutes of rest, so the forearm returns to normal.
- **Reference reading:** take the reference from the same deflation where possible.
  Otherwise repeat it, and discard the first reading of the day; it read 7 mmHg high on
  19 October.
- **Run log:** record the sensor spot, distance, room conditions and anything unusual.
- **Done when:** a written procedure exists and every run follows it.

### 4. Experiments with the improved rig

1. **Placement study (top priority).** In one session, compare marked spots: over a
   visible vein and away from one, at 3 cm and at 7–10 cm from the cuff. This tests why
   the MAP surge appeared one day and not the next, and how much of the signal is the
   cuff squeezing nearby tissue.
2. **Repeatability.** The same spot and protocol on different days, 5–10 runs each.
3. **More people.** At least 2–3 more volunteers, to show the results aren't specific to
   one arm, and to widen the range of blood pressures tested (currently 108–124 systolic).

### 5. A bench model of the target patient

- **Why:** healthy volunteers have a pulse, so in them return to flow measures systolic.
  The claim that matters for heart-pump patients, that return to flow equals MAP when
  there is no pulse, cannot be tested on a volunteer.
- **What to build:** a flow phantom.
  - a steady pressure source (for example a raised fluid reservoir) that sets a known
    "MAP";
  - soft tubing under the cuff;
  - a tissue-like chamber downstream for the optical sensor;
  - later, a small pump to add a pulse of adjustable size, to test weakly pulsatile
    patients.
- **Done when:** the cuff, sensor and analysis run on the phantom, and return to flow can
  be compared against the set pressure, with and without a pulse.

### Suggested order

| Step | Work | Why first |
|---|---|---|
| A | Cuff control (1) + sensor holder (2) + procedure (3) | Everything else depends on repeatable runs |
| B | Placement study and repeatability (4.1, 4.2) | Settles the MAP-surge question on volunteers |
| C | Bench phantom (5) | Tests the real target: MAP with no pulse |
| D | More volunteers (4.3) | Strengthens the systolic result once the rig is stable |

---

## Reflection

- **Small samples misled us once.** After four slow runs on 17 October, the MAP surge
  looked like a strong result: within 3 mmHg every time. Five runs two days later didn't
  reproduce it. From now on, a result needs to repeat across sessions before we treat it
  as a finding. We should also decide what counts as success *before* running.
- **The protocol was the biggest variable.** Deflation speed, hold time, inflation speed
  and sensor position changed the signal more than any analysis choice. Early on we
  focused on the analysis. The next round should focus on making the hardware and
  procedure repeatable.
- **Our accuracy is limited by our reference.** A home monitor's reading taken minutes
  earlier carries its own error of several mmHg, enough to hide or invent a 10 mmHg gap.
  The same-deflation reading was the single biggest improvement. Any further accuracy
  claims need a reference at least that good.
- **We have been testing the easier problem.** Volunteers let us show return to flow is
  detectable and timed well, and that's real progress. But the device's purpose, MAP in
  patients without a pulse, hasn't been tested at all. The phantom is how we get there
  without patients.
- **Record keeping matters.** For a week every recording was labelled "wrist" when the
  sensor was at the inner elbow. It didn't change any results, but it could have. Accurate
  metadata (site, distance, protocol) should be part of every run.
- **Fast iteration worked.** Recording and analysing in the same session let us change
  course daily. That should stay, alongside the more disciplined testing above.
