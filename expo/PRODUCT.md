# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Passers-by at an exposition (primary).** People walking past the MAPQuest booth, and the visitor
  sitting in the cuff. They glance at a monitor from a few metres away for seconds at a time and
  must understand what is happening without anyone explaining it.
- **Judges (secondary).** They ask for the technical view and read the signal pipeline, the onset
  analysis, the confidence checks and the safety state in depth, usually with a team member beside
  them.
- **The operator (team member).** Runs the cuff from the booth iPad (Start, Deflate, Stop, Zero) and switches the monitor
  between public and technical; never touches the monitor itself.

When the two audiences pull in different directions, the passer-by wins.

## Product Purpose

MAPQuest is a return-to-flow blood pressure prototype. A cuff squeezes the upper arm until blood
stops reaching the forearm, then releases slowly; light sensors on the forearm watch blood volume
and report the cuff pressure at the moment blood flows back. Because it reads blood volume rather
than the pulse, the same moment gives mean arterial pressure (MAP) for LVAD patients who have no
pulse. In a visitor with a pulse it lands near systolic.

The expo monitor dashboard shows the live run and its result to the public, and the signal
pipeline and machine state to judges. Success: a passer-by understands "squeeze, release, blood
comes back, that pressure is the reading" from the screen alone, and wants to try it.

## Positioning

It finds blood pressure with light alone, no pulse needed, which is what LVAD patients require
and what a home cuff (which listens for the pulse) cannot do.

## Operating Context

- A laptop drives a monitor at the booth, full screen, fixed 1600 × 900 design scaled to the
  screen. The laptop is on the board's own Wi-Fi with no internet: no web fonts or CDNs at the
  expo.
- `expo/host/expo_results.py` serves `expo/web/dashboard.html`, relays the board's stream and adds
  each run's analysed result after the cuff vents.
- Modes: public (idle, live run, outcome) and technical. The iPad switches its own screen; a laptop key switches the monitor;
  every new run starts on public.
- A run takes about 80 seconds: inflate to 160 mmHg, 10 s hold, release at 2 mmHg/s, vent at 40.

## Capabilities and Constraints

- The dashboard's content, wording and data are fixed: a redesign changes the visual design and the
  arrangement only, never what is said or shown. Every element, word and number stays.
- Outcomes: number shown, number with a caution, no clear reading, safety stop, run stopped.
- Results are anonymous (run number only).
- The headline number is the refill onset (hinge). Pulse return and the ramp intersect appear in the
  technical view only; steep refill is hidden.
- The monitor can only send the mode switch; it never controls the cuff.

## Brand Commitments

- The MAPQuest name and logo (`expo/web/logo.webp`) stay.
- Crimson stays as a colour somewhere: this is a heart-based project.
- Everything else about the look (ground, other colours, type) is open. The team poster is no longer
  binding beyond these two.

## Evidence on Hand

- Real bench results (DASHBOARD.md §5, DESIGN): refill onset read −6.2 ± 7.9 mmHg against systolic
  over 8 slow manual runs; return to flow detected in 22 of 22 scored real-arm runs; a no-pulse
  simulation within about ±2 mmHg of MAP whenever an onset was found, missing 14–18 % of runs at
  MAP 60.
- Simulator runs (`expo/host/sim_server.py`, stand-in arm with systolic 120) for demonstrating every
  state.
- Absent: any reading in a real pulseless patient. That test has not happened and must not be
  implied.

## Product Principles

1. The passer-by understands the run from across the booth, in seconds.
2. Show the measurement happening, not a claim about it: the live traces are the proof.
3. Safety reads as a feature, calm and deliberate, never alarming on the public screen.
4. Every number on screen is real or clearly a simulation; nothing is invented.
5. The technical view earns a judge's trust by showing its working, including where it can fail.
