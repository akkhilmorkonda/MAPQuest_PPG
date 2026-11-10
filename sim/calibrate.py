#!/usr/bin/env python3
"""Phase 2: fit the model to the real pulsatile runs, then check it on runs it never saw.

Every scored run in data/ (the ones marker_report.py uses) gets a model
TWIN: the same cuff pressure trace, an arterial pulse built from that run's
reference systolic/diastolic and heart rate, and the shared vessel settings.
The twin's blood volume is compared with the real one around the refill.

    python sim/calibrate.py fit      # shared settings from the training runs
    python sim/calibrate.py check    # held-out runs: refill shape, pulse size, markers
    python sim/calibrate.py sweep    # pulse pressure 0-50: which pressure does onset read?

Per run, three things are free and solved by least squares: optical gain
(path length and coupling differ run to run), offset, and a linear drain (the
same sloped baseline the pipeline assumes, O5). Everything else is shared,
so the shape of the refill below systolic has to come from the vessel model.

Reads data/ only. Writes sim/calibration.json and sim/figures/.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from scipy import optimize, signal
from scipy.ndimage import median_filter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(HERE))

import cuff_trial  # noqa: E402
import marker_report  # noqa: E402
from mapquest.pipeline import PipelineConfig, _zero_phase_lowpass  # noqa: E402
from model import (SIM_FS, Patient, Sensor, Vessels, _decimate, _finish,  # noqa: E402
                   beat_shape, coloured_noise, respond)

CAL_FILE = HERE / "calibration.json"
FIG_DIR = HERE / "figures"
FIT_BELOW_SBP = 35.0
"""The fit window runs from the reference window to the cuff 35 mmHg below
the reference systolic (about diastolic), or the dump if that comes first."""
LF_BAND = (0.03, 0.3)
WILD_MMHG = 10.0
CFG = PipelineConfig()
SHAPE_FRAC = float(beat_shape(np.arange(0, 1, 1e-4)).mean())
"""Where MAP sits in the model's beat, as a fraction of pulse pressure above
diastolic. The twin's waveform runs between the reference SBP and DBP."""

# Shared settings that can be fitted: field -> (to the optimiser's unbounded
# coordinate, back, bounds). c_ven stays fixed: the per-run optical gain
# absorbs the overall volume scale, so one compliance has to be pinned.
_LOG = (np.log, np.exp)
_LIN = (lambda x: x, lambda x: x)
TRANSFORMS = {
    "open_span": (*_LOG, 0.5, 200.0),
    "open_power": (*_LOG, 0.3, 4.0),
    "open_offset": (*_LIN, -20.0, 30.0),
    "g_cuff": (*_LOG, 1e-3, 100.0),
    "c_art": (*_LOG, 1e-3, 20.0),
    "r_art": (*_LOG, 0.1, 1.0e5),
}
FIELDS = ("open_span", "open_power", "open_offset", "g_cuff", "c_art", "r_art")


def _encode(v: Vessels, fields=FIELDS) -> np.ndarray:
    return np.array([TRANSFORMS[f][0](getattr(v, f)) for f in fields])


def _decode(x: np.ndarray, base: Vessels | None = None, fields=FIELDS) -> Vessels:
    out = {}
    for f, xi in zip(fields, np.asarray(x, float)):
        _, back, lo, hi = TRANSFORMS[f]
        out[f] = float(np.clip(back(xi), lo, hi))
    return replace(base or Vessels(), **out)


@dataclass
class RealRun:
    name: str
    file: str
    group: str
    rate: float
    trial: cuff_trial.Trial
    w: cuff_trial.Windows
    res: dict
    sbp: float
    dbp: float
    hr_bpm: float
    fit: tuple[float, float]
    markers: dict

    @property
    def patient(self) -> Patient:
        pp = self.sbp - self.dbp
        return Patient(map_mmhg=self.dbp + SHAPE_FRAC * pp, pulse_pressure=pp,
                       hr_bpm=self.hr_bpm)

    def cuff_at(self):
        """The real cuff trace, median-filtered over 0.5 s (sensor noise is
        not something the arteries see)."""
        tp, p = self.trial.t_p, self.trial.p_mmhg
        k = max(int(round(0.5 / max(np.median(np.diff(tp)), 1e-3))) | 1, 3)
        ps = median_filter(p, k)
        return lambda t: np.interp(t, tp, ps)


def load_runs() -> list[RealRun]:
    skip = marker_report.exclusions()
    runs = []
    for path in marker_report.trial_files():
        if path.name in skip:
            continue
        row = marker_report.score_run(path)
        trial = cuff_trial.load_trial(path)
        auto = cuff_trial.auto_windows(trial)
        w = cuff_trial.stored_windows(trial, auto)
        w.dump = auto.dump
        res = cuff_trial.analyse(trial, w)
        pr = res["pulse"]
        ref = trial.ref_bp()
        a = w.ref[0]
        t, p = trial.t, trial.pressure(trial.t)
        below = np.flatnonzero((t > w.deflation) & (p < ref["systolic"] - FIT_BELOW_SBP))
        b = float(t[below[0]]) if below.size else w.release
        b = min(b, w.dump or w.release, w.release)
        runs.append(RealRun(
            name=row["run"], file=path.name, group=row["group"], rate=row["rate"],
            trial=trial, w=w, res=res, sbp=ref["systolic"], dbp=ref["diastolic"],
            hr_bpm=60.0 * pr.f0_hz if pr and pr.f0_hz else 75.0, fit=(a, b),
            markers=row["markers"]))
    return runs


def split(runs: list[RealRun]) -> tuple[list[RealRun], list[RealRun]]:
    """Alternate in recording order, so both halves span every session and group."""
    order = sorted(runs, key=lambda r: r.file.split("_", 2)[-1])
    return order[0::2], order[1::2]


def twin_dv(run: RealRun, v: Vessels, t0: float, t1: float):
    """Noise-free model dV*L at the recording rate on [t0, t1], low-passed as the pipeline does."""
    r = respond(t0, t1, run.cuff_at(), run.patient, v)
    k = int(round(SIM_FS / run.trial.fs))
    t = _decimate(r["t"], k)
    dv = _decimate(r["dv"], k)
    return t, dv, _zero_phase_lowpass(dv, run.trial.fs, CFG.lowpass_hz, CFG.filter_order), r


def solve_nuisance(t, y, m, t_ref):
    """y ~ gain*m + offset + drain*(t - t_ref), gain >= 0. Returns (coef, fitted)."""
    A = np.column_stack([m, np.ones_like(t), t - t_ref])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    if coef[0] < 0:
        c2, *_ = np.linalg.lstsq(A[:, 1:], y, rcond=None)
        coef = np.array([0.0, *c2])
    return coef, A @ coef


def run_fit(run: RealRun, v: Vessels) -> dict:
    a, b = run.fit
    t0 = max(run.trial.t[0], run.w.inflation - 10.0)
    tm, _dv, dv_lp, r = twin_dv(run, v, t0, b + 1.0)
    d = run.res["derivation"]
    i = (d.t >= a) & (d.t <= b)
    t, y = d.t[i], d.dv_lp[i]
    m = np.interp(t, tm, dv_lp)
    coef, yhat = solve_nuisance(t, y, m, a)
    sst = float(np.sum((y - y.mean()) ** 2)) or 1.0
    return {"t": t, "y": y, "yhat": yhat, "m": m, "coef": coef,
            "r2": 1.0 - float(np.sum((y - yhat) ** 2)) / sst, "t0": t0, "resp": r}


def _tied(v: Vessels, pulse_tie: float | None) -> Vessels:
    """Hold c_art * r_art fixed: the optical pulse relative to the refill rate.
    Pinning c_art alone does not hold the pulse, because the per-run gain
    rescales everything when r_art changes."""
    return v if pulse_tie is None else replace(v, c_art=pulse_tie / v.r_art)


def objective(x, runs, base, fields=FIELDS, pulse_tie=None):
    v = _tied(_decode(x, base, fields), pulse_tie)
    return float(np.mean([1.0 - run_fit(r, v)["r2"] for r in runs]))


def fit(runs: list[RealRun], start: Vessels, maxiter: int = 300,
        fields=FIELDS, pulse_tie: float | None = None) -> tuple[Vessels, float]:
    res = optimize.minimize(objective, _encode(start, fields),
                            args=(runs, start, fields, pulse_tie), method="Nelder-Mead",
                            options={"maxiter": maxiter, "xatol": 1e-2, "fatol": 1e-4,
                                     "disp": False})
    return _tied(_decode(res.x, start, fields), pulse_tie), float(res.fun)


# -- Noise -------------------------------------------------------------------

def noise_of(run: RealRun) -> dict:
    """What the sensor adds, measured in the occluded hold, where no blood crosses the cuff.

    white: IR counts above 8 Hz (well above the heart band), as a count std.
    slow:  the low-passed dV*L in the hold after removing a quadratic drain, um std.
    """
    d = run.res["derivation"]
    h0, h1 = run.w.hold
    i = (d.t >= h0) & (d.t <= h1)
    ir = d.dark_sub["ir"]
    sos = signal.butter(4, 8.0, "high", fs=d.fs, output="sos")
    white = float(np.std(signal.sosfiltfilt(sos, ir)[i]))
    t = d.t[i]
    resid = d.dv_lp[i] - np.polyval(np.polyfit(t - t[0], d.dv_lp[i], 2), t - t[0])
    return {"white_counts": white, "slow_um": float(np.std(resid)), "hold_s": float(h1 - h0)}


def slow_noise_gain(fs: float = 100.0, seconds: float = 20.0, n: int = 50) -> float:
    """std of the measured statistic per unit lf_noise_um: band noise through
    the pipeline low-pass and a quadratic detrend over a hold-sized window."""
    rng = np.random.default_rng(0)
    m = int(seconds * fs)
    out = []
    for _ in range(n):
        x = coloured_noise(m + 2000, fs, 1.0, LF_BAND, rng)
        y = _zero_phase_lowpass(x, fs, CFG.lowpass_hz, CFG.filter_order)[1000:-1000]
        tt = np.arange(m) / fs
        out.append(np.std(y - np.polyval(np.polyfit(tt, y, 2), tt)))
    return float(np.mean(out))


# -- Commands ----------------------------------------------------------------

def _run_table(runs: list[RealRun], v: Vessels) -> list[dict]:
    out = []
    for r in runs:
        f = run_fit(r, v)
        out.append({"run": r.name, "file": r.file, "group": r.group, "rate": round(r.rate, 2),
                    "sbp": r.sbp, "dbp": r.dbp, "hr_bpm": round(r.hr_bpm, 1),
                    "fit_window_s": [round(x, 1) for x in r.fit],
                    "r2": round(f["r2"], 3), "gain": round(float(f["coef"][0]), 3),
                    "drain_um_s": round(float(f["coef"][2]), 3), **noise_of(r)})
    return out


def cmd_fit(args) -> int:
    runs = load_runs()
    train, test = split(runs)
    start = Vessels()
    print(f"{len(runs)} runs: {len(train)} training, {len(test)} held out")
    print(f"default settings: training 1-R2 = {objective(_encode(start), train, start):.4f}")
    best, best_f = None, np.inf
    # Two starts, so a soft-opening and a sharp-opening artery both get a chance.
    for s in (start, replace(start, open_span=3.0, g_cuff=5.0)):
        v, f = fit(train, s, args.maxiter)
        print(f"  start span {s.open_span:g}, g {s.g_cuff:g}: 1-R2 = {f:.4f}  "
              + ", ".join(f"{k} {getattr(v, k):.3g}" for k in FIELDS))
        if f < best_f:
            best, best_f = v, f
    gain = slow_noise_gain()
    cal = {
        "about": "sim/calibrate.py fit: shared vessel settings fitted to the training runs "
                 "(per-run optical gain, offset and linear drain free).",
        "vessels": asdict(best),
        "training_1_minus_r2": best_f,
        "train": [r.name for r in train], "test": [r.name for r in test],
        "slow_noise_gain": gain,
        "lf_band": list(LF_BAND),
        "runs": _run_table(runs, best),
    }
    CAL_FILE.write_text(json.dumps(cal, indent=2))
    print(f"Wrote {CAL_FILE}")
    return 0


def load_cal() -> dict:
    if not CAL_FILE.exists():
        raise SystemExit("No sim/calibration.json: run `python sim/calibrate.py fit` first.")
    return json.loads(CAL_FILE.read_text())


def null_fit(run: RealRun, power: float) -> float:
    """R2 of the simplest alternative: a hinge at the moment the cuff crosses
    the reference systolic, (t - t_sbp)^power, with the same free gain,
    offset and drain. The vessel model has to beat this to be worth having."""
    a, b = run.fit
    d = run.res["derivation"]
    i = (d.t >= a) & (d.t <= b)
    t, y = d.t[i], d.dv_lp[i]
    p = run.trial.pressure(t)
    cross = np.flatnonzero((t > run.w.deflation) & (p < run.sbp))
    t_s = t[cross[0]] if cross.size else t[-1]
    m = np.maximum(t - t_s, 0.0) ** power
    _, yhat = solve_nuisance(t, y, m, a)
    sst = float(np.sum((y - y.mean()) ** 2)) or 1.0
    return 1.0 - float(np.sum((y - yhat) ** 2)) / sst


def _pulse_rms(t, frac, fs, a, b) -> float:
    """Heart-band RMS of a fractional optical signal on [a, b], % of DC."""
    sos = signal.butter(2, [0.5, 4.0], "band", fs=fs, output="sos")
    x = signal.sosfiltfilt(sos, frac)
    i = (t >= a) & (t <= b)
    return float(100 * np.sqrt(np.mean(x[i] ** 2))) if i.sum() > fs else float("nan")


def twin_trial(run: RealRun, v: Vessels, row: dict, slow_gain: float, seed: int):
    """A full twin recording of a real run: the run's own cuff trace and
    windows, the model's blood volume at that run's gain and drain, and that
    run's measured sensor noise. Returns (trial, twin truth dict)."""
    tr, w, fs = run.trial, run.w, run.trial.fs
    t0 = max(tr.t[0], w.inflation - 20.0)
    t1 = min(tr.t[-1], w.release + 15.0)
    r = respond(t0, t1, run.cuff_at(), run.patient, v)
    k = int(round(SIM_FS / fs))
    t = _decimate(r["t"], k)
    a = run.fit[0]
    dv = (row["gain"] * _decimate(r["dv"], k)
          + row["drain_um_s"] * (np.clip(t, w.inflation, w.release) - a))
    pre = (tr.t >= t0) & (tr.t < w.inflation - 1.0)
    dark = tr.dark
    s = Sensor(fs=fs,
               base_ir=float(np.median(tr.channels["ir"][pre])) - float(dark.get("ir", 0)),
               base_red=float(np.median(tr.channels["red"][pre])) - float(dark.get("red", 0)),
               dark_ir=float(dark.get("ir", 0)), dark_red=float(dark.get("red", 0)),
               noise_counts=row["white_counts"], lf_noise_um=row["slow_um"] / slow_gain,
               lf_band=LF_BAND)
    ir, red = _finish(t, dv, np.zeros_like(t), 0.0, s, np.random.default_rng(seed),
                      lambda ir, red, _p, _d: (ir, red))
    twin = cuff_trial.Trial(Path(f"twin_{run.name}.csv"), dict(tr.header), fs, t,
                            {"ir": ir, "red": red}, dict(dark), tr.t_p, tr.p_mmhg, tr.p_source)
    flow = (r["t"] > w.deflation) & (r["q"] > 0)
    onset = float(r["t"][np.argmax(flow)]) if flow.any() else None
    return twin, {"flow_onset_mmhg": None if onset is None else float(tr.pressure(onset)),
                  "pulse_pct": _pulse_rms(t, (dv - dv[0]) / (1555 + 229 * 0.35), fs,
                                          t0 + 2, w.inflation - 1.0)}


def _stats(x):
    x = np.array([v for v in x if v is not None and np.isfinite(v)])
    if x.size == 0:
        return "   none detected"
    sd = f"{x.std(ddof=1):4.1f}" if x.size > 1 else "   -"
    return f"{x.mean():+6.1f} ± {sd}  (n {x.size})"


def twin_markers(r: RealRun, v: Vessels, row: dict, slow_gain: float, seeds) -> dict:
    """Twin markers for one real run, mean over seeds, as mmHg minus the
    reference systolic (None unless most seeds find it); plus pulse sizes."""
    names = marker_report.MARKERS
    twins = [twin_trial(r, v, row, slow_gain, seed) for seed in seeds]
    mk = {n: [] for n in names}
    for twin, _truth in twins:
        res = cuff_trial.analyse(twin, replace(r.w))
        for n, tt in res["markers"]:
            mk[n].append(None if tt is None else float(r.trial.pressure(tt)) - r.sbp)
    truth = twins[0][1]
    out = {"run": r.name, "twin_minus_sbp": {}, "real_minus_sbp": {}}
    for n in names:
        got = [x for x in mk[n] if x is not None]
        out["twin_minus_sbp"][n] = float(np.mean(got)) if len(got) * 2 > len(mk[n]) else None
        real = r.markers.get(n)
        out["real_minus_sbp"][n] = None if real is None else real - r.sbp
    fo = truth["flow_onset_mmhg"]
    out["flow_onset_minus_sbp"] = None if fo is None else fo - r.sbp
    out["pulse_pct_twin"] = truth["pulse_pct"]
    out["pulse_pct_real"] = _pulse_rms(
        r.trial.t, r.trial.channels["ir"] / np.median(r.trial.channels["ir"]), r.trial.fs,
        max(r.trial.t[0], r.w.inflation - 18.0), r.w.inflation - 1.0)
    return out


def _bias(details: list[dict], n: str, which: str) -> float:
    x = [d[which][n] for d in details if d[which][n] is not None]
    return float(np.mean(x)) if x else float("nan")


def _pulse_ratio(details: list[dict]) -> float:
    r = [d["pulse_pct_twin"] / d["pulse_pct_real"] for d in details
         if np.isfinite(d["pulse_pct_twin"]) and np.isfinite(d["pulse_pct_real"])]
    return float(np.median(r))


def variant(cal: dict, name: str) -> Vessels:
    found = cal.get("variants", {}).get(name)
    if found is None:
        if name != "A":
            raise SystemExit(f"No variant {name!r} in sim/calibration.json: run `scan` first.")
        return Vessels(**cal["vessels"])
    return Vessels(**found["vessels"])


def cmd_check(args) -> int:
    cal = load_cal()
    v = variant(cal, args.variant)
    runs = {r.name: r for r in load_runs()}
    test = [runs[n] for n in cal["test"]]
    train = [runs[n] for n in cal["train"]]
    rows = {x["run"]: x for x in _run_table(test, v)}
    seeds = range(args.seeds)
    names = marker_report.MARKERS
    print(f"Variant {args.variant}: " + ", ".join(f"{k} {getattr(v, k):.3g}" for k in FIELDS))

    print("\nRefill shape, R2 over the fit window (gain, offset and drain free per run)")
    print(f"  {'':10s} {'this variant':>13s} {'default':>8s} {'hinge p=1':>10s} {'hinge p=2':>10s}"
          "   (hinges at the reference systolic)")
    shape = {}
    for label, group in (("training", train), ("held out", test)):
        vals = np.array([[run_fit(r, v)["r2"], run_fit(r, Vessels())["r2"],
                          null_fit(r, 1.0), null_fit(r, 2.0)] for r in group])
        shape[label] = vals
        med = np.median(vals, axis=0)
        print(f"  {label:10s} {med[0]:13.3f} {med[1]:8.3f} {med[2]:10.3f} {med[3]:10.3f}   (median)")

    print("\nHeld-out runs: twin vs real, markers as mmHg minus reference systolic")
    print(f"  {'run':9s} {'group':12s} {'flow':>6s}  " + "  ".join(f"{n[:14]:>14s}" for n in names))
    detail = []
    for r in test:
        d = twin_markers(r, v, rows[r.name], cal["slow_noise_gain"], seeds)
        detail.append(d)
        cells = []
        for n in names:
            tw, re_ = d["twin_minus_sbp"][n], d["real_minus_sbp"][n]
            cells.append(f"{'--' if tw is None else f'{tw:+.1f}':>6s}/{'--' if re_ is None else f'{re_:+.1f}':<7s}")
        fo = d["flow_onset_minus_sbp"]
        print(f"  {r.name:9s} {r.group:12s} {'--' if fo is None else f'{fo:+.1f}':>6s}  "
              + "  ".join(cells))
    print("  (twin/real; 'flow' = where blood first crossed the cuff in the twin)")

    print("\nSummary over held-out runs, mmHg minus reference systolic")
    print(f"  {'marker':26s} {'twin':>24s} {'real':>24s}  twin - real, same run")
    for n in names:
        tw = [d["twin_minus_sbp"][n] for d in detail]
        re_ = [d["real_minus_sbp"][n] for d in detail]
        diff = [a - b for a, b in zip(tw, re_) if a is not None and b is not None]
        print(f"  {n:26s} {_stats(tw):>24s} {_stats(re_):>24s}  {_stats(diff)}")
    pt = np.array([d["pulse_pct_twin"] for d in detail])
    pr = np.array([d["pulse_pct_real"] for d in detail])
    ok = np.isfinite(pt) & np.isfinite(pr)
    print(f"\nResting optical pulse, heart-band RMS % of DC: twin median {np.median(pt[ok]):.3f}, "
          f"real median {np.median(pr[ok]):.3f}, twin/real per run median {_pulse_ratio(detail):.2f}")

    cal.setdefault("checks", {})[args.variant] = {
        "shape_r2_median": {k: dict(zip(("variant", "default", "hinge_p1", "hinge_p2"),
                                        np.median(x, axis=0).round(3).tolist()))
                            for k, x in shape.items()},
        "held_out": detail,
        "seeds": len(seeds),
    }
    cal.pop("check", None)
    CAL_FILE.write_text(json.dumps(cal, indent=2, default=float))
    figure_check(test, v, args.variant)
    print(f"Updated {CAL_FILE}; figure in {FIG_DIR}")
    return 0


def cmd_scan(args) -> int:
    """Variant B: how much of the real 'onset below the reference systolic'
    is an opening margin in the artery? Pin c_art to the real optical pulse,
    then for each opening offset refit the opening and resistance to the
    refill shape, and see where the twins' onset bias matches the real one
    on the TRAINING runs. The held-out runs stay untouched for `check`."""
    cal = load_cal()
    base = Vessels(**cal["vessels"])
    runs = {r.name: r for r in load_runs()}
    train = [runs[n] for n in cal["train"]]
    seeds = range(args.seeds)
    gain = cal["slow_noise_gain"]
    hinge = "refill onset (hinge)"

    rows = {x["run"]: x for x in _run_table(train, base)}
    ratio = _pulse_ratio([twin_markers(r, base, rows[r.name], gain, range(1)) for r in train])
    tie = base.c_art * base.r_art / ratio
    print(f"Variant A optical pulse is {ratio:.2f}x the real one; holding c_art * r_art at "
          f"{tie:.3g}", flush=True)

    scan = []
    print(f"\n  {'offset':>6s} {'span':>6s} {'g_cuff':>7s} {'r_art':>7s} {'1-R2':>7s} "
          f"{'hinge twin':>10s} {'real':>6s} {'pulse ret twin':>14s} {'real':>6s} {'pulse x':>7s}",
          flush=True)
    for off in args.offsets:
        start = _tied(replace(base, open_offset=off, open_span=5.0), tie)
        v, f = fit(train, start, args.maxiter, fields=("open_span", "g_cuff", "r_art"),
                   pulse_tie=tie)
        rows = {x["run"]: x for x in _run_table(train, v)}
        det = [twin_markers(r, v, rows[r.name], gain, seeds) for r in train]
        row = {"open_offset": off, "vessels": asdict(v), "one_minus_r2": f,
               "hinge_twin": _bias(det, hinge, "twin_minus_sbp"),
               "hinge_real": _bias(det, hinge, "real_minus_sbp"),
               "pulse_twin": _bias(det, "pulse return", "twin_minus_sbp"),
               "pulse_real": _bias(det, "pulse return", "real_minus_sbp"),
               "pulse_ratio": _pulse_ratio(det)}
        scan.append(row)
        print(f"  {off:6.1f} {v.open_span:6.2f} {v.g_cuff:7.3g} {v.r_art:7.3g} {f:7.4f} "
              f"{row['hinge_twin']:+10.1f} {row['hinge_real']:+6.1f} {row['pulse_twin']:+14.1f} "
              f"{row['pulse_real']:+6.1f} {row['pulse_ratio']:7.2f}", flush=True)

    # B: the offset whose twins reproduce the real hinge bias on the training runs.
    gap = np.array([r["hinge_twin"] - r["hinge_real"] for r in scan])
    pick = scan[int(np.argmin(np.abs(gap)))]
    zero = [r for r in scan if r["open_offset"] == 0]
    variants = {
        "A": {"about": "shape fit only: flow returns exactly where arterial pressure "
                       "clears the cuff, the reference systolic taken as true",
              "vessels": cal["vessels"]},
        "B": {"about": f"optical pulse held at its real size; opening offset "
                       f"{pick['open_offset']:g} mmHg, chosen so the training twins' hinge "
                       f"bias matches the real one (onset below SBP is physiology)",
              "vessels": pick["vessels"]},
    }
    if zero:
        variants["A2"] = {"about": "as A, with the optical pulse held at its real size "
                                   "and no opening offset",
                          "vessels": zero[0]["vessels"]}
    cal["scan"] = scan
    cal["variants"] = variants
    CAL_FILE.write_text(json.dumps(cal, indent=2, default=float))
    print(f"\nVariant B: opening offset {pick['open_offset']:g} mmHg. Updated {CAL_FILE}")
    return 0


def figure_check(test: list[RealRun], v: Vessels, name: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = len(test)
    cols = 4
    fig, axes = plt.subplots((n + cols - 1) // cols, cols, figsize=(14, 3.0 * ((n + cols - 1) // cols)),
                             squeeze=False)
    for ax, r in zip(axes.flat, test):
        f = run_fit(r, v)
        p = r.trial.pressure(f["t"])
        ax.plot(p, f["y"], color="#1f4e79", lw=1.6, label="real")
        ax.plot(p, f["yhat"], color="#D0761C", lw=1.4, ls="--", label=f"twin (R² {f['r2']:.2f})")
        ax.axvline(r.sbp, color="#c43b2f", lw=0.8, ls=":")
        ax.axvline(r.dbp + (r.sbp - r.dbp) / 3, color="#8A8F99", lw=0.8, ls=":")
        ax.invert_xaxis()
        ax.set_title(f"{r.name} ({r.group}, {r.rate:.1f} mmHg/s)", fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.tick_params(labelsize=7)
    for ax in list(axes.flat)[n:]:
        ax.axis("off")
    fig.supxlabel("cuff pressure, mmHg (falling →); dotted: reference systolic (red), MAP (grey)")
    fig.supylabel("blood volume dV·L, µm (low-passed)")
    fig.suptitle(f"Held-out runs: real refill vs model twin, variant {name} (shared settings from the training runs)")
    fig.tight_layout()
    FIG_DIR.mkdir(exist_ok=True)
    fig.savefig(FIG_DIR / f"phase2-heldout-refill-{name}.png", dpi=110)
    plt.close(fig)


def scaled(v: Vessels, k: float) -> Vessels:
    """The same vessels seen with optical gain k: volumes and flows scale,
    time constants and pressures do not."""
    return replace(v, c_art=v.c_art * k, c_ven=v.c_ven * k, g_cuff=v.g_cuff * k,
                   r_art=v.r_art / k, g_ven=v.g_ven * k)


def cmd_sweep(args) -> int:
    from model import Cuff, simulate
    import run as sim_run
    cal = load_cal()
    v0 = variant(cal, args.variant)
    runs = load_runs()
    rows = _run_table(runs, v0)
    gain = float(np.median([r["gain"] for r in rows]))
    sensor = Sensor(noise_counts=float(np.median([r["white_counts"] for r in rows])),
                    lf_noise_um=float(np.median([r["slow_um"] for r in rows])) / cal["slow_noise_gain"],
                    lf_band=LF_BAND)
    v = scaled(v0, gain)
    pps = (0, 5, 10, 15, 20, 30, 40, 50)
    names = marker_report.MARKERS
    out = []
    print(f"Variant {args.variant}; MAP 85, median gain {gain:.2f}, median noise; "
          f"{args.seeds} seeds; no drain")
    print("Marker minus MAP, mmHg: median over seeds (fraction of the way from MAP to SBP); "
          f"Nw = N seeds more than {WILD_MMHG:g} mmHg from where flow really returned")
    for rate in (2.0, 4.0):
        print(f"\n  deflation {rate:g} mmHg/s")
        print(f"  {'PP':>4s} {'SBP-MAP':>8s} {'flow':>6s}  " + "  ".join(f"{n[:18]:>18s}" for n in names))
        for pp in pps:
            res = {n: [] for n in names}
            flows = []
            for seed in range(args.seeds):
                runr = simulate(Patient(map_mmhg=85, pulse_pressure=pp), Cuff(deflate_rate=rate),
                                v, sensor, seed=seed)
                sc = sim_run.score(runr)
                flows.append(sc["truth"]["flow_onset_mmhg"] - 85)
                for n in names:
                    res[n].append(sc["error_vs_map"][n])
            span = sc["truth"]["sbp_mmhg"] - 85
            flow = float(np.median(flows))
            row = {"rate": rate, "pp": pp, "sbp_minus_map": span, "flow_minus_map": flow}
            cells = []
            for n in names:
                got = [x for x in res[n] if x is not None]
                # Median, plus how many seeds landed > 10 mmHg from where flow
                # really returned: a slow noise swing can trip the onset early.
                m = float(np.median(got)) if got else None
                wild = sum(abs(x - flow) > WILD_MMHG for x in got)
                row[n] = m
                row[n + " all"] = res[n]
                row[n + " wild"] = wild
                frac = "" if m is None or span < 1 else f" ({m / span:+.2f})"
                tag = f" {wild}w" if wild else ""
                cells.append(f"{'--' if m is None else f'{m:+.1f}' + frac + tag:>18s}")
            out.append(row)
            print(f"  {pp:4g} {span:8.1f} {row['flow_minus_map']:+6.1f}  " + "  ".join(cells))
    cal.setdefault("sweeps", {})[args.variant] = {"map": 85, "gain": gain, "seeds": args.seeds,
                                                   "rows": out}
    CAL_FILE.write_text(json.dumps(cal, indent=2, default=float))
    figure_sweep(out, args.variant)
    print(f"\nUpdated {CAL_FILE}; figure in {FIG_DIR}")
    return 0


def figure_sweep(rows: list[dict], name: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    colors = {"pulse return": "tab:purple", "refill onset (hinge)": "tab:green",
              "refill onset (intersect)": "tab:gray", "steep refill": "#d1197c"}
    for ax, rate in zip(axes, (2.0, 4.0)):
        rr = [r for r in rows if r["rate"] == rate]
        pp = [r["pp"] for r in rr]
        ax.plot(pp, [r["sbp_minus_map"] for r in rr], color="#c43b2f", lw=1, ls=":", label="systolic")
        ax.axhline(0, color="#8A8F99", lw=1, ls=":", label="MAP")
        for n, c in colors.items():
            # Only where most seeds found the marker: one detection in five is noise.
            ys = [np.nan if r[n] is None or 2 * sum(x is not None for x in r[n + " all"])
                  <= len(r[n + " all"]) else r[n] for r in rr]
            ax.plot(pp, ys, "o-", color=c, lw=1.4, ms=4, label=n)
        ax.set_title(f"deflation {rate:g} mmHg/s")
        ax.set_xlabel("native pulse pressure, mmHg")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("marker minus true MAP, mmHg")
    axes[0].legend(fontsize=8)
    fig.suptitle(f"Calibrated model (variant {name}), MAP 85: which pressure does each marker read as the pulse grows?")
    fig.tight_layout()
    FIG_DIR.mkdir(exist_ok=True)
    fig.savefig(FIG_DIR / f"phase2-pulse-sweep-{name}.png", dpi=110)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fit")
    p.add_argument("--maxiter", type=int, default=300)
    p = sub.add_parser("check")
    p.add_argument("--seeds", type=int, default=3)
    p.add_argument("--variant", default="A")
    p = sub.add_parser("scan")
    p.add_argument("--offsets", type=float, nargs="+", default=[0, 1.5, 3, 4.5, 6, 9])
    p.add_argument("--seeds", type=int, default=2)
    p.add_argument("--maxiter", type=int, default=120)
    p = sub.add_parser("sweep")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--variant", default="A")
    args = ap.parse_args(argv)
    return {"fit": cmd_fit, "check": cmd_check, "scan": cmd_scan, "sweep": cmd_sweep}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
