#!/usr/bin/env python3
"""How well each return-of-flow marker fits systolic and MAP (D47).

    python marker_report.py                 # every trial with reference BP
    python marker_report.py --no-figure

In a subject with a pulse, flow first gets past the cuff at SYSTOLIC, so a
return-to-flow marker is judged against systolic; the end use, patients with
little or no pulse, needs MAP. This scores every marker against both, on
every recording in data/ whose header carries a reference BP (D42), using
the same analysis as cuff_trial.py (hand-checked windows where stored).

A recording is left out when its data/cuff_windows.json entry has
"exclude": "<reason>"; the reason is printed.

Runs are grouped by how the reference was taken and how fast the cuff fell:

    slow manual     hand deflation at 3 mmHg/s or slower; reference taken
                    before the run
    fast manual     hand deflation faster than 3 mmHg/s
    automated       the monitor drove the cuff and read the same deflation
                    (trial info entered after the run, D45)

Writes data/marker_report.csv (one row per run and marker) and
docs/figures/marker-fit.png.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cuff_trial  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MARKERS = ("pulse return", "refill onset (hinge)", "refill onset (intersect)", "steep refill")
TARGETS = ("systolic", "map")
FAST_MMHG_S = 3.0
GROUPS = ("slow manual", "fast manual", "automated")


def trial_files(data: Path = DATA) -> list[Path]:
    out = []
    for p in sorted(data.glob("hr_*.csv")):
        if p.stem.endswith(("_hr", "_vol", "_gauge")):
            continue
        with p.open() as fh:
            head = "".join(line for line, _ in zip(fh, range(80)))
        if "# ref_systolic_mmhg=" in head:
            out.append(p)
    return out


def exclusions(path: Path = cuff_trial.WINDOWS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    entries = json.loads(path.read_text())
    return {k: v["exclude"] for k, v in entries.items()
            if isinstance(v, dict) and v.get("exclude")}


def deflation_rate(trial, w) -> float:
    """Mean fall from the deflation start to the dump, mmHg/s."""
    end = (w.dump or w.release) - 0.5
    return float((trial.pressure(w.deflation) - trial.pressure(end)) / max(end - w.deflation, 1e-6))


def score_run(path: Path) -> dict:
    trial = cuff_trial.load_trial(path)
    auto = cuff_trial.auto_windows(trial)
    w = cuff_trial.stored_windows(trial, cuff_trial.auto_windows(trial))
    w.dump = auto.dump
    res = cuff_trial.analyse(trial, w)
    ref = trial.ref_bp()
    rate = deflation_rate(trial, w)
    if trial.header.get("trial_info_entered") == "after_run":
        group = "automated"
    else:
        group = "fast manual" if rate > FAST_MMHG_S else "slow manual"
    markers = {name: (None if tt is None else float(trial.pressure(tt)))
               for name, tt in res["markers"]}
    return {
        "run": trial.header.get("run_id") or path.stem,
        "file": path.name,
        "group": group,
        "rate": rate,
        "distance_cm": float(trial.header.get("cuff_to_sensor_cm") or "nan"),
        "systolic": ref.get("systolic"),
        "diastolic": ref.get("diastolic"),
        "map": ref.get("map"),
        "markers": markers,
        "steep_note": res.get("steep_note"),
    }


def summarise(errors: list[float]) -> dict:
    """Bias, spread and hit rates of marker-minus-reference errors."""
    e = np.asarray([x for x in errors if x is not None and np.isfinite(x)], dtype=float)
    if e.size == 0:
        return {"n": 0}
    return {
        "n": int(e.size),
        "bias": float(e.mean()),
        "sd": float(e.std(ddof=1)) if e.size > 1 else float("nan"),
        "mae": float(np.abs(e).mean()),
        "within5": int(np.sum(np.abs(e) <= 5)),
        "within10": int(np.sum(np.abs(e) <= 10)),
    }


def error(row: dict, marker: str, target: str) -> float | None:
    v = row["markers"].get(marker)
    return None if v is None or row[target] is None else v - row[target]


def table(rows: list[dict], groups=GROUPS) -> list[dict]:
    out = []
    for target in TARGETS:
        for marker in MARKERS:
            for group in ("all",) + tuple(groups):
                sel = [r for r in rows if group == "all" or r["group"] == group]
                if not sel:
                    continue
                s = summarise([error(r, marker, target) for r in sel])
                out.append({"target": target, "marker": marker, "group": group,
                            "runs": len(sel), **s})
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "file", "group", "deflation_mmhg_s", "cuff_to_sensor_cm",
                    "ref_systolic", "ref_diastolic", "ref_map", "marker", "marker_mmhg",
                    "minus_systolic", "minus_map", "note"])
        for r in rows:
            for m in MARKERS:
                v = r["markers"].get(m)
                es, em = error(r, m, "systolic"), error(r, m, "map")
                w.writerow([r["run"], r["file"], r["group"], f"{r['rate']:.2f}",
                            f"{r['distance_cm']:g}", r["systolic"], r["diastolic"],
                            f"{r['map']:.1f}",
                            m, "" if v is None else f"{v:.1f}",
                            "" if es is None else f"{es:+.1f}",
                            "" if em is None else f"{em:+.1f}",
                            (r["steep_note"] or "") if m == "steep refill" and v is None else ""])


def print_report(rows: list[dict], skipped: dict[str, str]) -> None:
    print(f"{len(rows)} runs with reference BP")
    for name, why in skipped.items():
        print(f"  left out: {name} ({why})")
    print()
    print(f"  {'run':10s} {'group':12s} {'mmHg/s':>6s} {'cm':>3s} {'ref':>7s}   "
          + "  ".join(f"{m[:16]:>16s}" for m in MARKERS))
    for r in rows:
        cells = []
        for m in MARKERS:
            v = r["markers"].get(m)
            cells.append(f"{'none':>16s}" if v is None
                         else f"{v:6.1f} ({v - r['systolic']:+5.1f})".rjust(16))
        print(f"  {r['run']:10s} {r['group']:12s} {r['rate']:6.2f} {r['distance_cm']:3g} "
              f"{r['systolic']:3.0f}/{r['diastolic']:<3.0f}   " + "  ".join(cells))
    print("  (marker mmHg, and minus reference systolic)\n")
    for target in TARGETS:
        print(f"Against {'SYSTOLIC' if target == 'systolic' else 'MAP'}"
              "  (bias ± SD mmHg; within 5 / within 10 of the detected runs)")
        for row in table(rows):
            if row["target"] != target:
                continue
            if row["n"] == 0:
                txt = "none detected"
            else:
                sd = f"{row['sd']:4.1f}" if np.isfinite(row.get("sd", np.nan)) else "  - "
                txt = (f"{row['bias']:+6.1f} ± {sd}   n {row['n']:2d}/{row['runs']:<2d}  "
                       f"±5: {row['within5']:2d}  ±10: {row['within10']:2d}")
            print(f"  {row['marker']:26s} {row['group']:12s} {txt}")
        print()


COLORS = {"slow manual": "#2B5F9E", "fast manual": "#8A8F99", "automated": "#D0761C"}


def figure(rows: list[dict], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(15, 6.8), sharey=True)
    labels = ["pulse return", "refill onset\n(curved-start fit)",
              "refill onset\n(straight line)", "steep refill"]
    for ax, target in zip(axes, TARGETS):
        ax.axvspan(-5, 5, color="#E9EEF5", zorder=0)
        ax.axvline(0, color="#17223B", lw=1.5)
        for j, m in enumerate(MARKERS):
            for gi, g in enumerate(GROUPS):
                sel = [r for r in rows if r["group"] == g]
                ev = [error(r, m, target) for r in sel]
                ev = [e for e in ev if e is not None]
                y = j + (gi - 1) * 0.22
                ax.plot(ev, [y] * len(ev), "o", ms=8, color=COLORS[g], mec="#17223B",
                        mew=0.5, alpha=0.9, label=g if (j == 0 and target == "systolic") else None)
                if len(ev) >= 2:
                    ax.plot([np.mean(ev)] * 2, [y - 0.09, y + 0.09], color="#17223B", lw=2.5)
                missing = len(sel) - len(ev)
                if sel and missing:
                    ax.text(45, y, f"{g}: {missing} none", va="center", ha="right",
                            fontsize=8, color=COLORS[g])
        for j in range(len(MARKERS) - 1):
            ax.axhline(j + 0.5, color="#D5D9E0", lw=0.8)
        ax.set_yticks(range(len(MARKERS)))
        ax.set_yticklabels(labels, fontsize=10)
        ax.set_xlim(-32, 46)
        ax.grid(axis="x", alpha=0.3)
        ax.set_xlabel(f"marker minus reference {'systolic' if target == 'systolic' else 'MAP'} (mmHg)"
                      "\nshaded: within 5 mmHg   |   bar: group mean", fontsize=10)
        ax.set_title("Against systolic: return-to-flow in a pulsatile arm" if target == "systolic"
                     else "Against MAP: the target in patients", fontsize=12)
    # The panels share one y axis: set its direction once (top row first).
    axes[0].set_ylim(len(MARKERS) - 0.5, -0.5)
    axes[0].legend(loc="lower left", fontsize=9, frameon=True, framealpha=0.9)
    n = len(rows)
    fig.suptitle(f"How well each marker fits systolic and MAP: {n} cuff runs with reference BP",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"Saved figure to {out}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-figure", action="store_true")
    ap.add_argument("--csv", type=Path, default=DATA / "marker_report.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "figures" / "marker-fit.png")
    args = ap.parse_args(argv)

    skip = exclusions()
    rows, skipped = [], {}
    for p in trial_files():
        if p.name in skip:
            skipped[p.name] = skip[p.name]
            continue
        rows.append(score_run(p))
    print_report(rows, skipped)
    write_csv(rows, args.csv)
    print(f"Wrote {args.csv}")
    if not args.no_figure:
        figure(rows, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
