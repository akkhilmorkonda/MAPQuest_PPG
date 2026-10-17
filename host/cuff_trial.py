#!/usr/bin/env python3
"""Cuff trial: pulse return, refill onset and cuff pressure in one figure (D43, D44).

    python cuff_trial.py ../data/hr_wrist_20261017_145914_test6.csv
    python cuff_trial.py <csv> --deflation 64 --hold 50,64      # override windows
    python cuff_trial.py <csv> --motion 80,84 --x 40,120         # shade, zoom

Reads an hr_monitor recording with cuff pressure -- the MPRLS columns on the
PPG stream (D41), or a ``<name>_gauge.csv`` read from video (D38), which wins
when present.

Windows: command-line options first, then the recording's entry in
data/cuff_windows.json (hand-checked windows for past trials), then these
automatic ones from the pressure trace:

    inflation   last time below 10 mmHg before the peak
    hold        3 s after the peak (clear of the inflation transient) until
                deflation: the occluded floor for pulse return
    deflation   first time 20 mmHg below the peak, at least 8 s after it;
                the onset searches start here
    reference   the 5 s before deflation (refill-onset noise floor)
    release     first time below 20 mmHg after the peak

The automatic windows are a starting point, not a verdict: an operator who
lets the cuff creep, or a movement during the hold, can put them in the wrong
place. They are printed with the result; check them on the figure.

Markers, each with the cuff pressure at that moment:

    pulse return        heart-band pulse back above 3x its occluded floor
                        (mapquest/pulse_return.py)
    refill onset        corner of the trend-then-rising fit (hinge, default)
    refill, intersect   straight ramp extrapolated to the sloped baseline (D39)
    steep refill        where the refill accelerates most, within 20 s of the
                        onset; tracked MAP on the slow trials so far (D44)

With reference BP in the header (D42) each is also reported against systolic
and MAP. Saves ``<name>_cuff.png`` next to the recording.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.cuff import REF_S, Windows, windows_from_pressure  # noqa: E402
from mapquest.io import load_csv  # noqa: E402
from mapquest.mprls import counts_to_mmhg  # noqa: E402
from mapquest.pipeline import PipelineConfig, derive, steep_refill  # noqa: E402
from mapquest import pulse_return  # noqa: E402

UNCUFFED_MMHG = 8.0
STEEP_SEARCH_S = 20.0
WINDOWS_FILE = Path(__file__).resolve().parent.parent / "data" / "cuff_windows.json"


@dataclass
class Trial:
    path: Path
    header: dict
    fs: float
    t: np.ndarray
    """Session clock, s: the same clock the GUI shows and the gauge video read."""
    channels: dict
    dark: dict
    t_p: np.ndarray
    p_mmhg: np.ndarray
    p_source: str

    def pressure(self, at):
        return np.interp(at, self.t_p, self.p_mmhg)

    def ref_bp(self) -> dict:
        out = {}
        for k in ("systolic", "diastolic", "map"):
            v = self.header.get(f"ref_{k}_mmhg")
            if v:
                out[k] = float(v.split()[0])
        if "systolic" in out and "diastolic" in out and "map" not in out:
            out["map"] = out["diastolic"] + (out["systolic"] - out["diastolic"]) / 3
        return out


def _read_table(path: Path) -> dict[str, np.ndarray]:
    rows = [ln.strip() for ln in path.open() if ln.strip() and not ln.startswith("#")]
    names = rows[0].split(",")
    a = np.array([[float(x) for x in r.split(",")] for r in rows[1:]])
    return {n: a[:, i] for i, n in enumerate(names)}


def load_trial(path: str | Path) -> Trial:
    path = Path(path)
    meta, _t, channels, extra = load_csv(path)
    hdr = dict(meta.raw)
    fs = meta.fs
    t = float(hdr.get("session_t0", 0.0)) + extra["idx"] / fs
    dark = meta.dark or {}

    gauge = path.with_name(path.stem + "_gauge.csv")
    if gauge.exists():
        g = _read_table(gauge)
        t_p, p, src = g["t_s"], g["mmhg"], "gauge video"
    elif hdr.get("pressure") == "mprls" and "p_raw" in extra:
        # Fresh readings only: the firmware holds the latest one on every sample.
        _, first = np.unique(extra["p_idx"], return_index=True)
        first = np.sort(first)
        absolute = counts_to_mmhg(extra["p_raw"][first].astype(float))
        t_p = t[first]
        zero = hdr.get("cuff_zero_abs_mmhg")
        zero = float(zero) if zero else float(np.median(absolute[t_p <= t_p[0] + 2.0]))
        p, src = absolute - zero, "MPRLS sensor"
    else:
        raise SystemExit(f"{path.name}: no cuff pressure (no MPRLS columns, no _gauge.csv)")
    return Trial(path, hdr, fs, t, channels, dark, t_p, p, src)


def auto_windows(trial: Trial) -> Windows:
    return windows_from_pressure(trial.t_p, trial.p_mmhg)


def stored_windows(trial: Trial, w: Windows, path: Path = WINDOWS_FILE) -> Windows:
    """Apply the recording's hand-checked entry from data/cuff_windows.json."""
    if not path.exists():
        return w
    entry = json.loads(path.read_text()).get(trial.path.name)
    if not entry:
        return w
    if "deflation" in entry:
        w.deflation = float(entry["deflation"])
        w.hold = (w.hold[0], w.deflation)
        w.ref = (w.deflation - REF_S, w.deflation)
    if "hold" in entry:
        w.hold = tuple(entry["hold"])
    if "ref" in entry:
        w.ref = tuple(entry["ref"])
    if "x" in entry:
        w.x = tuple(entry["x"])
    w.motion = tuple(tuple(m) for m in entry.get("motion", ()))
    w.source = f"hand-checked ({path.name})"
    return w


def analyse(trial: Trial, w: Windows) -> dict:
    t = trial.t
    d = derive(trial.channels, trial.fs, dark=trial.dark, ref_window=w.ref,
               deflation_start_s=w.deflation, t=t, config=PipelineConfig())
    p_t = trial.pressure(t)
    inside = (t >= trial.t_p[0]) & (t <= trial.t_p[-1])
    uncuffed = inside & (p_t < UNCUFFED_MMHG) & ((t < w.inflation) | (t > w.release))
    ir = trial.channels["ir"] - float(trial.dark.get("ir", 0.0))
    pr = pulse_return.detect(t, ir, trial.fs, w.hold, w.deflation, uncuffed)
    onset = next((x for x in (d.onset_time_hinge, d.onset_time_sloped, d.threshold_time)
                  if x is not None), None)
    steep = (steep_refill(d.ddv, t, onset, min(onset + STEEP_SEARCH_S, w.release))
             if onset is not None else None)
    return {"derivation": d, "pulse": pr,
            "markers": [("pulse return", pr.t_return if pr else None),
                        ("refill onset (hinge)", d.onset_time_hinge),
                        ("refill onset (intersect)", d.onset_time_sloped),
                        ("steep refill", steep)]}


def report(trial: Trial, w: Windows, res: dict) -> None:
    d, pr = res["derivation"], res["pulse"]
    P = trial.pressure
    print(f"File       : {trial.path.name}")
    if trial.header.get("run_id"):
        print(f"Run ID     : {trial.header['run_id']}")
    print(f"Pressure   : {trial.p_source}")
    print(f"Windows    : {w.source}: hold {w.hold[0]:.1f}-{w.hold[1]:.1f} s, "
          f"reference {w.ref[0]:.1f}-{w.ref[1]:.1f} s, deflation from {w.deflation:.1f} s "
          f"({P(w.deflation):.0f} mmHg)")
    if pr:
        print(f"Heart rate : {pr.f0_hz * 60:.0f} bpm; occluded pulse floor {pr.floor:.4f} %")
    ref = trial.ref_bp()
    if ref:
        print(f"Reference  : {ref.get('systolic', 0):.0f}/{ref.get('diastolic', 0):.0f} mmHg, "
              f"MAP {ref.get('map', 0):.0f}")
    print()
    for name, tt in res["markers"]:
        if tt is None:
            print(f"  {name:26s}: none")
            continue
        line = f"  {name:26s}: t = {tt:6.2f} s  {P(tt):6.1f} mmHg"
        if "systolic" in ref:
            line += f"  (vs systolic {P(tt) - ref['systolic']:+5.1f}, vs MAP {P(tt) - ref['map']:+5.1f})"
        print(line)
    if d.hinge_power is not None:
        print(f"\n  refill shape: p = {d.hinge_power:g} "
              f"({'sharp corner' if d.hinge_power == 1 else 'soft start'})")


def figure(trial: Trial, w: Windows, res: dict, out: Path, x=None, motion=(),
           title=None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d, pr = res["derivation"], res["pulse"]
    t, P = trial.t, trial.pressure
    x0, x1 = x or w.x or (max(t[0], w.inflation - 5.0), min(t[-1], w.release + 5.0))
    sel = (t >= x0) & (t <= x1)
    colors = {"pulse return": "tab:purple", "refill onset (hinge)": "tab:green",
              "refill onset (intersect)": "tab:gray", "steep refill": "#d6197a"}
    ev = sorted([(n, tt, colors[n]) for n, tt in res["markers"] if tt is not None],
                key=lambda e: e[1])

    fig, ax = plt.subplots(4, 1, figsize=(13, 12), sharex=True,
                           gridspec_kw=dict(height_ratios=[1.2, 1, 1.3, 0.9]))
    gs = (trial.t_p >= x0) & (trial.t_p <= x1)
    a = ax[0]
    a.plot(trial.t_p[gs], trial.p_mmhg[gs], "k.", ms=2)
    a.set_ylabel(f"cuff pressure (mmHg)\n{trial.p_source}")
    rid = trial.header.get("run_id")
    a.set_title(title or f"{trial.path.stem}" + (f"  (run {rid})" if rid else ""), fontsize=11)
    pmax = float(np.max(trial.p_mmhg[gs])) if gs.any() else 150.0
    # Labels in a row above the trace, spread around the events, each with an
    # arrow down to its point: no overlap with each other or the title.
    span = (x1 - x0) * 0.15
    mid = float(np.mean([e[1] for e in ev])) if ev else 0.0
    lxs = [mid + (i - (len(ev) - 1) / 2) * span for i in range(len(ev))]
    hi, lo = x1 - span * 0.55, x0 + span * 0.6
    if lxs and lxs[-1] > hi:
        lxs = [v - (lxs[-1] - hi) for v in lxs]
    if lxs and lxs[0] < lo:
        lxs = [v + (lo - lxs[0]) for v in lxs]
    for (lab, tt, c), lx in zip(ev, lxs):
        for aa in ax:
            aa.axvline(tt, color=c, ls="--", lw=1)
        a.annotate(f"{lab.replace(' (', chr(10) + '(')}\n{P(tt):.0f} mmHg", (tt, P(tt)),
                   xytext=(lx, pmax * 1.22), fontsize=8.5, color=c, ha="center",
                   va="bottom", arrowprops=dict(arrowstyle="->", color=c, lw=0.8))
    for key, col in (("systolic", "#c43b2f"), ("map", "#d08700"), ("diastolic", "#1f6feb")):
        v = trial.ref_bp().get(key)
        if v:
            a.axhline(v, color=col, ls=":", lw=1.1)
            a.text(x0 + (x1 - x0) * 0.005, v + 1.5,
                   f"ref {'MAP' if key == 'map' else key} {v:.0f}",
                   color=col, fontsize=8, va="bottom")
    a.grid(alpha=0.3)
    a.set_ylim(-10, pmax * 1.55)

    a = ax[1]
    if pr:
        a.plot(t[sel], pr.pulse[sel], lw=0.5, color="tab:red", alpha=0.7,
               label="IR pulse (0.5-4 Hz, % of DC)")
        a.plot(t[sel], pr.env_hr[sel] * np.sqrt(2), color="k", lw=1.3,
               label=f"heart-band amplitude ({pr.f0_hz * 60:.0f} bpm +/-15%)")
        a.plot(t[sel], pr.env_off[sel] * np.sqrt(2), color="tab:cyan", lw=1,
               label="off-band amplitude")
        a.axhline(pr.threshold * np.sqrt(2), color="tab:purple", ls=":", lw=1,
                  label="3x occluded floor")
        # Scale to the cuffed stretch: uncuffed beats are ten times the size
        # of the returning ones and would flatten everything that matters.
        cuffed = sel & (t >= w.hold[0]) & (t <= w.release)
        if not cuffed.any():
            cuffed = sel
        lim = max(np.percentile(np.abs(pr.pulse[cuffed]), 99.5) * 1.3,
                  pr.threshold * np.sqrt(2) * 2)
        a.set_ylim(-lim, lim)
    a.axvspan(*w.hold, color="0.9", zorder=0, label="occluded hold (floor)")
    a.set_ylabel("pulse (%)")
    a.legend(loc="upper right", fontsize=8)
    a.grid(alpha=0.3)

    a = ax[2]
    a.plot(t[sel], d.dv_lp[sel], color="tab:blue", lw=1.5, label="blood volume (660+880)")
    if d.hinge_fit is not None and d.onset_time_hinge is not None:
        b0, b1, k, ws = d.hinge_fit
        th = np.linspace(ws, d.onset_time_hinge + 6.0, 200)
        a.plot(th, b0 + b1 * (th - ws) + k * np.clip(th - d.onset_time_hinge, 0, None)
               ** d.hinge_power, color="tab:green", lw=1.4,
               label=f"hinge fit (p = {d.hinge_power:g})")
    if d.baseline_fit and d.ramp_fit and d.onset_time_sloped:
        bs, bi = d.baseline_fit
        tt = np.linspace(d.baseline_span[0], d.onset_time_sloped + 3, 50)
        a.plot(tt, bs * tt + bi, ":", color="0.4", lw=1.2, label=f"occlusion trend {bs:+.2f} um/s")
        rs, ri = d.ramp_fit
        tt = np.linspace(d.onset_time_sloped - 2, d.onset_time_sloped + 9, 50)
        a.plot(tt, rs * tt + ri, "--", color="0.4", lw=1.2, label=f"straight ramp {rs:+.2f} um/s")
    a.axvspan(*w.ref, color="0.85", zorder=0, label="reference window")
    a.set_ylabel("blood volume dV*L (um)\n+ = more blood")
    a.legend(loc="upper right", fontsize=8, ncol=2)
    a.grid(alpha=0.3)

    a = ax[3]
    a.plot(t[sel], d.ddv[sel], color="tab:blue", lw=1)
    a.axhline(d.threshold, color="tab:purple", ls=":", lw=1, label="threshold max(mu0,0)+3 sigma0")
    a.axhline(0, color="k", lw=0.5)
    a.set_ylabel("d(dV*L)/dt (um/s)")
    a.set_xlabel("session time (s), same clock as the on-screen display")
    a.legend(loc="upper left", fontsize=8)
    a.grid(alpha=0.3)
    yl = np.percentile(np.abs(d.ddv[sel]), 99) * 1.4
    a.set_ylim(-yl, yl)

    for m0, m1 in tuple(motion) + tuple(w.motion):
        for aa in ax:
            aa.axvspan(m0, m1, color="#c43b2f", alpha=0.12, lw=0, zorder=0)
        ax[1].text((m0 + m1) / 2, 0.92, "movement", transform=ax[1].get_xaxis_transform(),
                   ha="center", va="top", fontsize=8, color="#c43b2f")
    ax[0].set_xlim(x0, x1)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"\nSaved figure to {out}")


def _pair(s: str) -> tuple[float, float]:
    a, b = (float(v) for v in s.split(","))
    return a, b


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path)
    ap.add_argument("--deflation", type=float, help="deflation start, s (onset search from here)")
    ap.add_argument("--hold", type=_pair, help="occluded hold for the pulse floor, 'a,b' s")
    ap.add_argument("--ref", type=_pair, help="refill-onset reference window, 'a,b' s")
    ap.add_argument("--x", type=_pair, help="figure time range, 'a,b' s")
    ap.add_argument("--motion", action="append", type=_pair, default=[],
                    help="shade a movement span 'a,b' s (repeatable)")
    ap.add_argument("--title")
    ap.add_argument("--out", type=Path, help="figure path (default <name>_cuff.png)")
    args = ap.parse_args(argv)

    trial = load_trial(args.csv)
    w = stored_windows(trial, auto_windows(trial))
    if args.deflation is not None or args.hold or args.ref:
        w.source = "command line"
    if args.deflation is not None:
        w.deflation = args.deflation
        w.hold = (w.hold[0], args.deflation)
        w.ref = (args.deflation - REF_S, args.deflation)
    if args.hold:
        w.hold = args.hold
    if args.ref:
        w.ref = args.ref
    res = analyse(trial, w)
    report(trial, w, res)
    out = args.out or args.csv.with_name(args.csv.stem + "_cuff.png")
    figure(trial, w, res, out, x=args.x, motion=args.motion, title=args.title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
