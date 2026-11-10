#!/usr/bin/env python3
"""Run a simulated patient through the real cuff-trial analysis.

The simulated run goes through ``cuff_trial.analyse`` unchanged -- the same
windows, pipeline and four markers the real trials are scored with -- and the
markers are compared with the model's ground truth.

    python sim/run.py --map 85                         # LVAD, no pulse
    python sim/run.py --map 85 --hm3 8                 # HeartMate 3 artificial pulse
    python sim/run.py --map 90 --pp 45                 # healthy subject
    python sim/run.py --map 85 --save --plot           # CSV + truth + cuff_trial figure

Output goes to ``sim/out/`` (gitignored), never ``data/``: marker_report.py
scores every ``data/hr_*.csv`` with a reference BP, and simulated runs must
not mix into the real-trial statistics.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cuff_trial  # noqa: E402
from mapquest.mprls import MMHG_PER_PSI, OUTPUT_MAX, OUTPUT_MIN, P_MAX_PSI, P_MIN_PSI  # noqa: E402
from model import Cuff, Patient, Sensor, SimRun, Vessels, simulate  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"
ROOM_ABS_MMHG = 760.0


def to_trial(run: SimRun, path: Path | None = None) -> cuff_trial.Trial:
    """The run as cuff_trial loads a recording, without touching the disk."""
    tr = run.truth
    header = {
        "source": "mapquest-sim", "synthetic": "true",
        "ref_systolic_mmhg": f"{tr['sbp_mmhg']:.1f}",
        "ref_diastolic_mmhg": f"{tr['dbp_mmhg']:.1f}",
        "ref_map_mmhg": f"{tr['map_mmhg']:.1f}",
    }
    s = run.sensor
    return cuff_trial.Trial(path or Path("sim.csv"), header, s.fs, run.t,
                            {"ir": run.ir.astype(float), "red": run.red.astype(float)},
                            {"ir": s.dark_ir, "red": s.dark_red},
                            run.t, run.p_mmhg, "simulated")


def score(run: SimRun, trial: cuff_trial.Trial | None = None) -> dict:
    """Markers as cuff pressure, beside the truth."""
    trial = trial or to_trial(run)
    w = cuff_trial.auto_windows(trial)
    res = cuff_trial.analyse(trial, w)
    markers = {name: (None if tt is None else float(trial.pressure(tt)))
               for name, tt in res["markers"]}
    tr = run.truth
    return {
        "truth": tr,
        "markers": markers,
        "error_vs_map": {k: None if v is None else v - tr["map_mmhg"] for k, v in markers.items()},
        "error_vs_sbp": {k: None if v is None else v - tr["sbp_mmhg"] for k, v in markers.items()},
        "steep_note": res.get("steep_note"),
        "windows": w,
        "result": res,
        "trial": trial,
    }


def _p_raw(abs_mmhg: np.ndarray) -> np.ndarray:
    """Inverse of mprls.counts_to_mmhg."""
    psi = abs_mmhg / MMHG_PER_PSI
    return np.rint((psi - P_MIN_PSI) * (OUTPUT_MAX - OUTPUT_MIN) / (P_MAX_PSI - P_MIN_PSI)
                   + OUTPUT_MIN).astype(np.int64)


def write_csv(run: SimRun, path: Path, run_id: str = "sim") -> Path:
    """A recording in hr_monitor's format, so cuff_trial.py and analyze.py
    read it like any other. The truth goes in a ``.truth.json`` beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    s, tr = run.sensor, run.truth
    p_raw = _p_raw(ROOM_ABS_MMHG + run.p_mmhg)
    with path.open("w", newline="\n") as fh:
        fh.write("# source=mapquest-sim\n# synthetic=true\n")
        fh.write(f"# run_id={run_id}\n")
        fh.write(f"# ref_systolic_mmhg={tr['sbp_mmhg']:.1f}\n")
        fh.write(f"# ref_diastolic_mmhg={tr['dbp_mmhg']:.1f}\n")
        fh.write(f"# ref_map_mmhg={tr['map_mmhg']:.1f}  (true time-mean)\n")
        fh.write("# session_t0=0.000\n# mode=spo2\n# channels=ir,red\n")
        fh.write(f"# fs={s.fs:.2f}\n# resolution_bits=18\n# pressure=mprls\n")
        fh.write("# columns=idx,ir,red,p_idx,p_raw,p_status\n")
        fh.write(f"# dark_ir={s.dark_ir:g}\n# dark_red={s.dark_red:g}\n")
        fh.write(f"# cuff_zero_abs_mmhg={ROOM_ABS_MMHG:.3f}\n")
        fh.write("idx,ir,red,p_idx,p_raw,p_status\n")
        ir, red = run.ir.astype(np.int64), run.red.astype(np.int64)
        for i in range(len(run.t)):
            fh.write(f"{i},{ir[i]},{red[i]},{i},{p_raw[i]},64\n")
    params = {"patient": asdict(run.patient), "cuff": asdict(run.cuff),
              "vessels": asdict(run.vessels), "sensor": asdict(run.sensor)}
    path.with_suffix(".truth.json").write_text(
        json.dumps({"truth": tr, "params": params}, indent=2))
    return path


def _fmt(x):
    return "   --" if x is None else f"{x:6.1f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", type=float, default=85.0, help="true MAP, mmHg")
    ap.add_argument("--pp", type=float, default=0.0, help="native pulse pressure, mmHg")
    ap.add_argument("--hr", type=float, default=75.0, help="heart rate, bpm")
    ap.add_argument("--pulse-every", type=int, default=1,
                    help="only every n-th beat ejects (intermittent aortic valve opening)")
    ap.add_argument("--hm3", type=float, default=0.0,
                    help="HeartMate 3 artificial pulse, peak-to-peak mmHg")
    ap.add_argument("--resp", type=float, default=0.0, help="respiratory swing, mmHg")
    ap.add_argument("--deflate-rate", type=float, default=2.0, help="mmHg/s")
    ap.add_argument("--hold-mmhg", type=float, default=160.0)
    ap.add_argument("--hold-s", type=float, default=25.0)
    ap.add_argument("--inflate-s", type=float, default=2.0)
    ap.add_argument("--noise", type=float, default=40.0, help="optical noise, counts")
    ap.add_argument("--drain-rate", type=float, default=0.0, help="O5 drain, um/s (negative)")
    ap.add_argument("--drain-tau", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--save", action="store_true", help="write CSV + truth to sim/out/")
    ap.add_argument("--plot", action="store_true", help="cuff_trial figure (implies --save)")
    ap.add_argument("--name", help="file stem (default from the settings)")
    args = ap.parse_args(argv)

    run = simulate(
        Patient(map_mmhg=args.map, pulse_pressure=args.pp, hr_bpm=args.hr,
                pulse_every=args.pulse_every, hm3_pp_mmhg=args.hm3, resp_mmhg=args.resp),
        Cuff(deflate_rate=args.deflate_rate, hold_mmhg=args.hold_mmhg,
             hold_s=args.hold_s, inflate_s=args.inflate_s),
        Vessels(drain_rate=args.drain_rate, drain_tau=args.drain_tau),
        Sensor(noise_counts=args.noise),
        seed=args.seed)
    trial = None
    if args.save or args.plot:
        name = args.name or (f"sim_map{args.map:g}_pp{args.pp:g}_hm3{args.hm3:g}"
                             f"_r{args.deflate_rate:g}_s{args.seed}")
        path = write_csv(run, OUT_DIR / f"{name}.csv", run_id=name)
        trial = cuff_trial.load_trial(path)
        print(f"Wrote {path}")
    sc = score(run, trial)
    tr = sc["truth"]
    print(f"Truth: MAP {tr['map_mmhg']:.1f}  SBP {tr['sbp_mmhg']:.1f}  DBP {tr['dbp_mmhg']:.1f}"
          f"  flow first crossed the cuff at {_fmt(tr['flow_onset_mmhg']).strip()} mmHg")
    print(f"  {'marker':26s} {'mmHg':>6s} {'vs MAP':>7s} {'vs SBP':>7s}")
    for k, v in sc["markers"].items():
        print(f"  {k:26s} {_fmt(v)} {_fmt(sc['error_vs_map'][k])} {_fmt(sc['error_vs_sbp'][k])}")
    if sc["steep_note"]:
        print(f"  steep refill: {sc['steep_note']}")
    if args.plot:
        out = trial.path.with_name(trial.path.stem + "_cuff.png")
        cuff_trial.figure(trial, sc["windows"], sc["result"], out,
                          title=f"Simulated: MAP {tr['map_mmhg']:.0f}, pulse {args.pp:g} mmHg,"
                                f" HM3 {args.hm3:g} mmHg")
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
