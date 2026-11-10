#!/usr/bin/env python3
"""Phase 3: does return-to-flow read MAP without a pulse, and what breaks it?

    python sim/experiments.py soft    # fit the soft-opening bracket (open_span held at 20)
    python sim/experiments.py e1      # no pulse: MAP 60-100 x 1-4 mmHg/s  (judged, README)
    python sim/experiments.py e2      # native pulse pressure 0-40
    python sim/experiments.py e3      # HeartMate 3 pulse, intermittent ejection
    python sim/experiments.py e4      # stress cases at no pulse
    python sim/experiments.py all     # e1-e4; e5 is read out of e1 and e3

Every simulated run borrows its optical gain, drain and sensor noise from one
of the 22 real runs' twins, in turn (the "population"), so refill size and
noise vary as much as they did on real arms. Two opening variants bracket the
one thing the real runs cannot pin down: how sharply the cuffed artery opens.

Results go to sim/results/phase3.json; figures to sim/figures/.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "host"))
sys.path.insert(0, str(HERE))

import calibrate as cal_mod  # noqa: E402
import marker_report  # noqa: E402
from model import Cuff, Patient, Sensor, Vessels, simulate  # noqa: E402
from run import score  # noqa: E402

RESULTS = HERE / "results" / "phase3.json"
FIG_DIR = HERE / "figures"
MARKERS = marker_report.MARKERS
HINGE = "refill onset (hinge)"
FALSE_MMHG = 10.0
"""An onset more than this far from where blood truly first crossed the cuff is a false onset."""
SOFT_SPAN = 20.0
VARIANTS = ("sharp", "soft")


@dataclass
class Draw:
    """What one real run contributes to a simulated one."""
    source: str
    gain: float
    drain: float
    white: float
    lf: float


def _variant_vessels(cal: dict, name: str) -> Vessels:
    key = {"sharp": "B", "soft": "B-soft"}[name]
    if key not in cal.get("variants", {}):
        raise SystemExit(f"No variant {key} in sim/calibration.json: run `experiments.py soft` first.")
    return Vessels(**cal["variants"][key]["vessels"])


def population(cal: dict, name: str) -> list[Draw]:
    """One draw per real run, from its twin under this variant (cached in the calibration)."""
    pops = cal.setdefault("populations", {})
    if name not in pops:
        v = _variant_vessels(cal, name)
        rows = cal_mod._run_table(cal_mod.load_runs(), v)
        pops[name] = [asdict(Draw(r["run"], r["gain"], r["drain_um_s"], r["white_counts"],
                                  r["slow_um"] / cal["slow_noise_gain"])) for r in rows]
        cal_mod.CAL_FILE.write_text(json.dumps(cal, indent=2, default=float))
    return [Draw(**d) for d in pops[name]]


def one(v: Vessels, d: Draw, patient: Patient, cuff: Cuff, seed: int,
        gain_x: float = 1.0, lf_x: float = 1.0, drain: float | None = None,
        bump: tuple[float, float] | None = None) -> dict:
    """Simulate and score one run. ``bump`` = (cuff mmHg, um): a 2 s movement
    artefact in the optics when the cuff passes that pressure."""
    vv = replace(cal_mod.scaled(v, d.gain * gain_x),
                 drain_rate=d.drain if drain is None else drain, drain_tau=0.0)
    s = Sensor(noise_counts=d.white, lf_noise_um=d.lf * lf_x, lf_band=cal_mod.LF_BAND)
    run = simulate(patient, cuff, vv, s, seed=seed)
    if bump is not None:
        top = int(np.argmax(run.p_true))  # search the deflation only: pressure falls from here
        t_b = float(np.interp(-bump[0], -run.p_true[top:], run.t[top:]))
        shape = np.exp(-0.5 * ((run.t - t_b) / 0.6) ** 2)
        od = bump[1] * shape / (1555 + 229 * 0.35)
        run.ir = np.rint(run.ir * np.exp(-od))
        run.red = np.rint(run.red * np.exp(-0.35 * od))
    sc = score(run)
    tr = sc["truth"]
    flow = tr["flow_onset_mmhg"]
    out = {"source": d.source, "seed": seed, "map": tr["map_mmhg"], "sbp": tr["sbp_mmhg"],
           "flow": flow, "steep_note": sc["steep_note"]}
    for n in MARKERS:
        m = sc["markers"][n]
        out[n] = m
        out[n + " false"] = m is None or flow is None or abs(m - flow) > FALSE_MMHG
    return out


def cohort(v, pop, patient, cuff, n_seeds: int = 1, **kw) -> list[dict]:
    return [one(v, d, patient, cuff, seed=1000 * k + i, **kw)
            for k in range(n_seeds) for i, d in enumerate(pop)]


def summarise(rows: list[dict], marker: str, ref: str = "map") -> dict:
    """Bias and SD against ``ref`` with false onsets excluded, and the false-onset rate."""
    good = [r[marker] - r[ref] for r in rows if not r[marker + " false"]]
    n = len(rows)
    det = sum(r[marker] is not None for r in rows)
    return {"n": n, "detected": det,
            "false_rate": 1.0 - len(good) / n if n else float("nan"),
            "bias": float(np.mean(good)) if good else float("nan"),
            "sd": float(np.std(good, ddof=1)) if len(good) > 1 else float("nan"),
            "median": float(np.median(good)) if good else float("nan")}


def _fmt(s: dict) -> str:
    return (f"{s['bias']:+5.1f} ± {s['sd']:3.1f}  false {100 * s['false_rate']:4.1f}%"
            f"  (n {s['n']})")


def _load_results() -> dict:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {}


def _save(key: str, value) -> None:
    res = _load_results()
    res[key] = value
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(res, indent=2, default=float))


# -- soft-opening bracket ----------------------------------------------------

def cmd_soft(args) -> int:
    """Fit B-soft: B's pulse tie, open_span held at 20 mmHg, the rest refit to
    the training runs. Then the same held-out marker check as B."""
    cal = cal_mod.load_cal()
    b = Vessels(**cal["variants"]["B"]["vessels"])
    tie = b.c_art * b.r_art
    runs = {r.name: r for r in cal_mod.load_runs()}
    train = [runs[n] for n in cal["train"]]
    test = [runs[n] for n in cal["test"]]
    start = cal_mod._tied(replace(b, open_span=SOFT_SPAN, open_power=1.0, open_offset=0.0,
                                  g_cuff=1.0, r_art=100.0), tie)
    v, f = cal_mod.fit(train, start, args.maxiter, fields=("g_cuff", "r_art"), pulse_tie=tie)
    print(f"B-soft: open_span {v.open_span:g}, g_cuff {v.g_cuff:.3g}, r_art {v.r_art:.3g}, "
          f"training 1-R2 {f:.4f} (B: {cal['scan'][0]['one_minus_r2']:.4f})")
    rows = {x["run"]: x for x in cal_mod._run_table(test, v)}
    det = [cal_mod.twin_markers(r, v, rows[r.name], cal["slow_noise_gain"], range(3)) for r in test]
    r2 = float(np.median([x["r2"] for x in rows.values()]))
    tw = [d["twin_minus_sbp"][HINGE] for d in det]
    re_ = [d["real_minus_sbp"][HINGE] for d in det]
    diff = [a - b_ for a, b_ in zip(tw, re_) if a is not None and b_ is not None]
    print(f"held out: refill R2 median {r2:.3f}; hinge vs SBP twin {cal_mod._stats(tw)}, "
          f"real {cal_mod._stats(re_)}, twin - real {cal_mod._stats(diff)}; "
          f"pulse twin/real {cal_mod._pulse_ratio(det):.2f}")
    cal["variants"]["B-soft"] = {
        "about": f"as B, but the cuffed artery opens over {SOFT_SPAN:g} mmHg (held, not "
                 "fitted): the soft end of what the real runs allow",
        "vessels": asdict(v), "training_1_minus_r2": f,
        "held_out": {"r2_median": r2, "hinge_twin_minus_sbp": tw, "hinge_real_minus_sbp": re_}}
    cal.get("populations", {}).pop("soft", None)
    cal_mod.CAL_FILE.write_text(json.dumps(cal, indent=2, default=float))
    return 0


# -- E1: no pulse ------------------------------------------------------------

E1_MAPS = (60.0, 70.0, 80.0, 90.0, 100.0)
E1_RATES = (1.0, 2.0, 3.0, 4.0)


def cmd_e1(args) -> int:
    cal = cal_mod.load_cal()
    out = {"criterion": "hinge, 2 mmHg/s, every MAP: |bias| <= 2, SD <= 3, false <= 5%",
           "rows": []}
    verdict = {}
    for name in VARIANTS:
        v = _variant_vessels(cal, name)
        pop = population(cal, name)
        print(f"\nE1, {name} opening: no pulse, onset minus true MAP, false onsets excluded")
        print(f"  {'MAP':>4s} {'rate':>4s}  {'hinge':>34s}  {'intersect':>34s}")
        ok = True
        for rate in E1_RATES:
            for m in E1_MAPS:
                rows = cohort(v, pop, Patient(map_mmhg=m),
                              Cuff(hold_mmhg=m + 50.0, deflate_rate=rate), args.seeds)
                h, i = summarise(rows, HINGE), summarise(rows, "refill onset (intersect)")
                out["rows"].append({"variant": name, "map": m, "rate": rate, "hinge": h,
                                    "intersect": i, "runs": rows})
                judged = rate == 2.0
                passed = (abs(h["bias"]) <= 2 and h["sd"] <= 3 and h["false_rate"] <= 0.05)
                if judged:
                    ok &= passed
                flag = ("  PASS" if passed else "  FAIL") if judged else ""
                print(f"  {m:4.0f} {rate:4.0f}  {_fmt(h):>34s}  {_fmt(i):>34s}{flag}")
        verdict[name] = ok
        print(f"  E1 ({name} opening): {'PASS' if ok else 'FAIL'}")
    out["verdict"] = verdict
    _save("e1", out)
    figure_e1(out)
    return 0


def figure_e1(out: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(12, 7.4), sharex=True, sharey="row",
                             gridspec_kw={"height_ratios": [3, 2]})
    colors = dict(zip(E1_RATES, ("#2B5F9E", "#0a7d2c", "#D0761C", "#c43b2f")))
    for col, name in enumerate(VARIANTS):
        ax, axf = axes[0, col], axes[1, col]
        for rate in E1_RATES:
            rr = [r for r in out["rows"] if r["variant"] == name and r["rate"] == rate]
            x = np.array([r["map"] for r in rr]) + (rate - 2.5) * 0.8
            y = [r["hinge"]["bias"] for r in rr]
            e = [r["hinge"]["sd"] for r in rr]
            ax.errorbar(x, y, yerr=e, fmt="o-", color=colors[rate], ms=4, lw=1.2, capsize=2,
                        label=f"{rate:g} mmHg/s")
            axf.plot(x, [100 * r["hinge"]["false_rate"] for r in rr], "o-", color=colors[rate],
                     ms=4, lw=1.2)
        ax.axhspan(-2, 2, color="#0a7d2c", alpha=0.08, label="±2 mmHg (criterion)")
        ax.axhline(0, color="#8A8F99", lw=0.8)
        ax.set_title(f"{name} opening: verdict {'PASS' if out['verdict'][name] else 'FAIL'}")
        ax.grid(alpha=0.3)
        axf.axhspan(0, 5, color="#0a7d2c", alpha=0.08)
        axf.set_xlabel("true MAP, mmHg")
        axf.grid(alpha=0.3)
    axes[0, 0].set_ylabel("hinge onset − true MAP, mmHg\n(mean ± SD, false onsets excluded)")
    axes[1, 0].set_ylabel("false onsets, % of runs\n(criterion ≤ 5% at 2 mmHg/s)")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle("E1: no pulse. Does the hinge onset read MAP?")
    fig.tight_layout()
    FIG_DIR.mkdir(exist_ok=True)
    fig.savefig(FIG_DIR / "phase3-e1-no-pulse.png", dpi=110)
    plt.close(fig)


# -- E2: native pulse pressure ----------------------------------------------

E2_PPS = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 40.0)


def cmd_e2(args) -> int:
    cal = cal_mod.load_cal()
    out = {"map": 80.0, "rows": []}
    for name in VARIANTS:
        v = _variant_vessels(cal, name)
        pop = population(cal, name)
        for rate in (2.0, 4.0):
            print(f"\nE2, {name} opening, {rate:g} mmHg/s, MAP 80: marker - MAP "
                  "(fraction of the way to SBP), false rate vs where flow returned")
            print(f"  {'PP':>4s} {'SBP-MAP':>7s}  " + "  ".join(f"{n[:22]:>28s}" for n in MARKERS))
            for pp in E2_PPS:
                rows = cohort(v, pop, Patient(map_mmhg=80.0, pulse_pressure=pp),
                              Cuff(hold_mmhg=80.0 + 50.0 + pp, deflate_rate=rate), args.seeds)
                span = float(np.median([r["sbp"] - r["map"] for r in rows]))
                s = {n: summarise(rows, n) for n in MARKERS}
                out["rows"].append({"variant": name, "rate": rate, "pp": pp,
                                    "sbp_minus_map": span, "markers": s, "runs": rows})
                cells = []
                for n in MARKERS:
                    b = s[n]["bias"]
                    frac = f" ({b / span:+.2f})" if span > 1 and np.isfinite(b) else ""
                    cells.append(f"{'--' if not np.isfinite(b) else f'{b:+.1f}'}{frac} "
                                 f"f{100 * s[n]['false_rate']:.0f}%")
                print(f"  {pp:4g} {span:7.1f}  " + "  ".join(f"{c:>28s}" for c in cells))
    _save("e2", out)
    figure_e2(out)
    return 0


def figure_e2(out: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    styles = {("sharp", 2.0): ("#0a7d2c", "-"), ("sharp", 4.0): ("#0a7d2c", "--"),
              ("soft", 2.0): ("#D0761C", "-"), ("soft", 4.0): ("#D0761C", "--")}
    for ax, n in zip(axes, (HINGE, "refill onset (intersect)")):
        for (name, rate), (c, ls) in styles.items():
            rr = [r for r in out["rows"] if r["variant"] == name and r["rate"] == rate]
            ax.plot([r["pp"] for r in rr], [r["markers"][n]["bias"] for r in rr], "o",
                    ls=ls, color=c, ms=4, lw=1.3, label=f"{name}, {rate:g} mmHg/s")
        rr = [r for r in out["rows"] if r["variant"] == "sharp" and r["rate"] == 2.0]
        ax.plot([r["pp"] for r in rr], [r["sbp_minus_map"] for r in rr], ":", color="#c43b2f",
                label="systolic")
        ax.axhline(0, color="#8A8F99", lw=0.8, ls=":")
        ax.set_title(n)
        ax.set_xlabel("native pulse pressure, mmHg")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("marker − true MAP, mmHg (mean, false onsets excluded)")
    axes[0].legend(fontsize=8)
    fig.suptitle("E2: MAP 80. How fast does a residual pulse pull the reading to systolic?")
    fig.tight_layout()
    FIG_DIR.mkdir(exist_ok=True)
    fig.savefig(FIG_DIR / "phase3-e2-pulse-pressure.png", dpi=110)
    plt.close(fig)


# -- E3: LVAD waveforms ------------------------------------------------------

E3_CASES = (
    ("no pulse", {}),
    ("HM3 4 mmHg", {"hm3_pp_mmhg": 4.0}),
    ("HM3 8 mmHg", {"hm3_pp_mmhg": 8.0}),
    ("HM3 12 mmHg", {"hm3_pp_mmhg": 12.0}),
    ("PP 20, every beat", {"pulse_pressure": 20.0}),
    ("PP 20, every 2nd beat", {"pulse_pressure": 20.0, "pulse_every": 2}),
    ("PP 20, every 3rd beat", {"pulse_pressure": 20.0, "pulse_every": 3}),
    ("PP 20, every 5th beat", {"pulse_pressure": 20.0, "pulse_every": 5}),
    ("HM3 8 + PP 10 every 3rd", {"hm3_pp_mmhg": 8.0, "pulse_pressure": 10.0, "pulse_every": 3}),
)


def cmd_e3(args) -> int:
    cal = cal_mod.load_cal()
    out = {"map": 80.0, "rate": 2.0, "rows": []}
    for name in VARIANTS:
        v = _variant_vessels(cal, name)
        pop = population(cal, name)
        print(f"\nE3, {name} opening, MAP 80, 2 mmHg/s: marker - MAP, false rate")
        print(f"  {'case':26s} {'SBP-MAP':>7s}  " + "  ".join(f"{n[:22]:>22s}" for n in MARKERS))
        for label, kw in E3_CASES:
            rows = cohort(v, pop, Patient(map_mmhg=80.0, **kw),
                          Cuff(hold_mmhg=150.0, deflate_rate=2.0), args.seeds)
            span = float(np.median([r["sbp"] - r["map"] for r in rows]))
            s = {n: summarise(rows, n) for n in MARKERS}
            out["rows"].append({"variant": name, "case": label, "patient": kw,
                                "sbp_minus_map": span, "markers": s, "runs": rows})
            cells = [f"{'--' if not np.isfinite(s[n]['bias']) else format(s[n]['bias'], '+.1f')}"
                     f" f{100 * s[n]['false_rate']:.0f}%" for n in MARKERS]
            print(f"  {label:26s} {span:7.1f}  " + "  ".join(f"{c:>22s}" for c in cells))
    _save("e3", out)
    return 0


# -- E4: stress cases at no pulse ---------------------------------------------

E4_CASES = (
    ("baseline", {}, {}),
    ("slow noise x2", {"lf_x": 2.0}, {}),
    ("slow noise x4", {"lf_x": 4.0}, {}),
    ("weak refill x0.25", {"gain_x": 0.25}, {}),
    ("weak refill x0.1", {"gain_x": 0.1}, {}),
    ("strong drain -2 um/s", {"drain": -2.0}, {}),
    ("slow inflation 15 s", {}, {"inflate_s": 15.0}),
    ("short hold 8 s", {}, {"hold_s": 8.0}),
    ("motion +5 um at MAP+15", {"bump": (15.0, 5.0)}, {}),
    ("motion +5 um at MAP+5", {"bump": (5.0, 5.0)}, {}),
    ("motion -5 um at MAP-5", {"bump": (-5.0, -5.0)}, {}),
)


def cmd_e4(args) -> int:
    cal = cal_mod.load_cal()
    out = {"map": 80.0, "rate": 2.0, "rows": []}
    for name in VARIANTS:
        v = _variant_vessels(cal, name)
        pop = population(cal, name)
        print(f"\nE4, {name} opening, no pulse, MAP 80, 2 mmHg/s: hinge and intersect - MAP")
        print(f"  {'case':26s} {'hinge':>34s}  {'intersect':>34s}")
        for label, kw, ckw in E4_CASES:
            kw = dict(kw)
            if "bump" in kw:
                kw["bump"] = (80.0 + kw["bump"][0], kw["bump"][1])
            rows = cohort(v, pop, Patient(map_mmhg=80.0),
                          Cuff(hold_mmhg=130.0, deflate_rate=2.0, **ckw), args.seeds, **kw)
            h, i = summarise(rows, HINGE), summarise(rows, "refill onset (intersect)")
            out["rows"].append({"variant": name, "case": label, "hinge": h, "intersect": i,
                                "runs": rows})
            print(f"  {label:26s} {_fmt(h):>34s}  {_fmt(i):>34s}")
    _save("e4", out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("soft")
    p.add_argument("--maxiter", type=int, default=100)
    for c in ("e1", "e2", "e3", "e4", "all"):
        p = sub.add_parser(c)
        p.add_argument("--seeds", type=int, default=1,
                       help="noise realisations per real-run draw (22 draws each)")
    args = ap.parse_args(argv)
    cmds = {"soft": cmd_soft, "e1": cmd_e1, "e2": cmd_e2, "e3": cmd_e3, "e4": cmd_e4}
    if args.cmd == "all":
        for c in ("e1", "e2", "e3", "e4"):
            cmds[c](args)
        return 0
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
