#!/usr/bin/env python3
"""MAPQuest flow bench — intralipid phantom: flow vs no flow in the tube.

Standalone from the cuff and heart-rate tooling: no pressure, no heart rate, no
blood-volume model. It answers one question per transition: when the tube
under the sensor goes from flowing to no flow (drained to air), or back, how
big is the optical change against the noise?

    python flow_bench.py --auto-tune --run-id F-01 --note "1% intralipid, 40 cm head"
    python flow_bench.py --replay ../benchtop_data/flow_20261102_101500_F-01.csv
    python flow_bench.py --report ../benchtop_data/flow_20261102_101500_F-01.csv

**Recording.** Starts by itself as soon as the board streams. Stop / Start
recording (R) closes the file and opens a new one. Every recording goes to
benchtop_data/ beside data/: the raw IR/red with every marker inline, and on
stop a _steps.csv with the step table. Closing the window stops and saves.

**Protocol.** Intralipid flowing through the clear tube at a constant pressure,
sensor on the tube. One marker button (space) toggles: the first press marks
NO FLOW (press it as you open the dump), the next marks FLOW (flow back), and
so on. The run is assumed to start flowing.

**LED tuning.** Tune with intralipid *flowing* (the baseline state), then leave
it. Do one practice no-flow first: if either channel leaves the 5–95 % band,
re-tune in the brighter state. A re-tune changes counts with nothing physical
changing: it is allowed mid-recording but logged, the OD trace re-zeroes, and
no step window reaches across it. The ADC range is fixed in firmware
(config.h); only change it if auto-tune cannot reach mid-scale at all.

**Display.** Top: IR and red as % of full scale, with the 5 % and 95 % lines.
Middle: change in optical density since the last zero (+ = less light back).
Bottom: one row per marker: the median over BEFORE s ending 1 s ahead of the
press against the median over AFTER s starting SETTLE s after it, as counts %,
dOD and step ÷ noise, the noise taken from whichever window is flowing (robust
detrended SD), since the no-flow side is full of dips.

Buttons / keys: Mark NO FLOW / FLOW (space) · Stop / Start recording (R) ·
Auto-tune LEDs (T).
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hr_monitor import (  # noqa: E402
    FULL_SCALE, ReplaySource, SerialSource, clean_run_id, next_run_id,
)

# Benchtop experiments live beside data/, not in it: marker_report.py scores
# every data/hr_*.csv, and bench recordings must never land among the cuff trials.
DATA_DIR = Path(__file__).resolve().parent.parent / "benchtop_data"

RAIL_LO, RAIL_HI = 0.05, 0.95       # fraction of full scale: warn outside
CLIP_HI = 0.98                      # fraction of full scale: treat as saturated
ZERO_S = 2.0                        # seconds averaged for the OD zero
AUTO_ZERO_S = 3.0                   # zero this long after start or a reset
MIN_WINDOW_S = 2.0                  # shortest usable before/after window
# BEFORE ends this long ahead of the key press. The tube can start draining a
# moment before the key is hit; a second of the change inside the baseline
# window inflated its noise tenfold in a replayed run.
GUARD_S = 1.0
TRANSITIONS = ("no_flow", "flow")


def is_boundary(label: str) -> bool:
    """Markers, re-tunes and link resets: no step window reaches across one."""
    return not label.startswith("zero")


# ---------------------------------------------------------------------------
# Steps: the measurement this tool exists for
# ---------------------------------------------------------------------------

@dataclass
class Step:
    n: int
    kind: str                # "no_flow" or "flow"
    t: float                 # key press, session s
    status: str              # "ok", "waiting", "too short"
    flags: str = ""          # "clipped" (window shortened), "rail" (saturated)
    ir: dict | None = None   # before, after (medians), d_counts, d_pct, d_od, noise, snr
    red: dict | None = None


def flowing_noise(x: np.ndarray, tx: np.ndarray) -> float:
    """Robust SD of x about a straight-line trend.

    The trend takes out slow drift; 1.4826 x MAD is the SD for Gaussian
    noise but ignores an occasional bubble.
    """
    if len(x) < 3:
        return float("nan")
    resid = x - np.polyval(np.polyfit(tx, x, 1), tx)
    return float(1.4826 * np.median(np.abs(resid - np.median(resid))))


def _channel_step(before: np.ndarray, after: np.ndarray, t_before: np.ndarray,
                  t_after: np.ndarray, dark: float, noise_from: str) -> dict:
    # Medians, not means: with no flow, residual liquid and air pockets put
    # 5-9 % dips into the trace (2026-11-02 run) that would drag a mean.
    mb, ma = float(np.median(before)), float(np.median(after))
    # Noise from the flowing side of the step: before a no_flow marker,
    # after a flow marker. The no-flow side is full of dips; measuring the
    # noise there scored the obvious return to flow at SNR 0-2.
    noise = (flowing_noise(before, t_before) if noise_from == "before"
             else flowing_noise(after, t_after))
    d = ma - mb
    span_b, span_a = mb - dark, ma - dark
    d_od = (float(-np.log(span_a / span_b))
            if span_a > 0 and span_b > 0 else float("nan"))
    return {
        "before": mb, "after": ma, "d_counts": d,
        "d_pct": 100.0 * d / span_b if span_b > 0 else float("nan"),
        "d_od": d_od, "noise": noise, "noise_from": noise_from,
        "snr": abs(d) / noise if noise and noise > 0 else float("inf"),
    }


def compute_steps(t: np.ndarray, ir: np.ndarray, red: np.ndarray | None,
                  events: list[tuple[float, str]], dark: dict,
                  before_s: float = 10.0, settle_s: float = 5.0,
                  after_s: float = 10.0) -> list[Step]:
    """One Step per no_flow / flow marker.

    BEFORE: the before_s ending GUARD_S ahead of the key press, but never
    reaching back past the previous boundary (marker, re-tune, link reset)
    plus settle_s. AFTER: after_s starting settle_s after the press, cut
    short by the next boundary. A window
    under MIN_WINDOW_S makes the step "too short"; an AFTER window still
    filling makes it "waiting".

    Levels are window medians. Noise is the robust detrended SD of the
    flowing window: BEFORE for a no_flow marker, AFTER for a flow marker.
    """
    marks = sorted((te, lab) for te, lab in events if lab in TRANSITIONS)
    bounds = sorted(te for te, lab in events if is_boundary(lab))
    t_end = float(t[-1]) if len(t) else 0.0
    steps: list[Step] = []
    for k, (te, kind) in enumerate(marks):
        prev_t = max((b for b in bounds if b < te), default=None)
        next_t = min((b for b in bounds if b > te), default=None)
        flags = []
        b1 = te - GUARD_S
        b0 = b1 - before_s
        if prev_t is not None and b0 < prev_t + settle_s:
            b0 = prev_t + settle_s
            flags.append("clipped")
        a0, a1 = te + settle_s, te + settle_s + after_s
        if next_t is not None and next_t < a1:
            a1 = next_t
            flags.append("clipped")
        step = Step(k + 1, kind, te, "ok")
        if b1 - b0 < MIN_WINDOW_S or a1 - a0 < MIN_WINDOW_S:
            step.status = "too short"
        elif next_t is None and t_end < a1:
            step.status = "waiting"
        if step.status != "ok":
            step.flags = ",".join(dict.fromkeys(flags))
            steps.append(step)
            continue
        sel_b = (t >= b0) & (t < b1)
        sel_a = (t >= a0) & (t < a1)
        noise_from = "before" if kind == "no_flow" else "after"
        for name, x in (("ir", ir), ("red", red)):
            if x is None:
                continue
            xb, xa = x[sel_b], x[sel_a]
            if len(xb) < 3 or len(xa) < 3:
                step.status = "too short"
                break
            if max(xb.max(), xa.max()) >= CLIP_HI * FULL_SCALE:
                flags.append("rail")
            setattr(step, name, _channel_step(xb, xa, t[sel_b], t[sel_a],
                                              dark.get(name, 0.0), noise_from))
        step.flags = ",".join(dict.fromkeys(flags))
        steps.append(step)
    return steps


STEP_COLUMNS = ["n", "kind", "t_s", "status", "flags",
                "ir_before", "ir_after", "ir_d_counts", "ir_d_pct", "ir_d_od",
                "ir_noise", "ir_snr",
                "red_before", "red_after", "red_d_counts", "red_d_pct", "red_d_od",
                "red_noise", "red_snr"]


def steps_csv(steps: list[Step]) -> str:
    def f(v):
        return "" if v is None or not np.isfinite(v) else f"{v:.6g}"
    lines = [",".join(STEP_COLUMNS)]
    for s in steps:
        row = [str(s.n), s.kind, f"{s.t:.2f}", s.status, s.flags]
        for ch in (s.ir, s.red):
            row += [f(ch[k]) if ch else "" for k in
                    ("before", "after", "d_counts", "d_pct", "d_od", "noise", "snr")]
        lines.append(",".join(row))
    return "\n".join(lines) + "\n"


def step_table(steps: list[Step], last: int | None = None) -> str:
    """Fixed-width ASCII text, for the window and any console encoding."""
    head = (f"{'#':>2} {'kind':<8}{'t (s)':>8}  "
            f"{'IR d%':>7}{'dOD':>8}{'SNR':>7}   "
            f"{'red d%':>7}{'dOD':>8}{'SNR':>7}  note")
    rows = [head]
    for s in (steps[-last:] if last else steps):
        cells = f"{s.n:>2} {s.kind:<8}{s.t:8.1f}  "
        if s.status != "ok":
            rows.append(cells + f"{'':>45}{s.status}")
            continue
        for ch in (s.ir, s.red):
            if ch is None:
                cells += f"{'-':>7}{'':>8}{'':>7}   "
            else:
                snr = "inf" if not np.isfinite(ch["snr"]) else f"{ch['snr']:.0f}"
                cells += f"{ch['d_pct']:+7.2f}{ch['d_od']:+8.4f}{snr:>7}   "
        rows.append(cells.rstrip() + ("  " + s.flags if s.flags else ""))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------

def load_recording(path: Path) -> dict:
    """Header, events (session s), IR/red and their session times."""
    meta: dict[str, str] = {}
    events: list[tuple[float, str]] = []
    cols: list[str] | None = None
    rows: list[list[float]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# event"):
            kv = dict(tok.partition("=")[::2] for tok in line[7:].split() if "=" in tok)
            try:
                events.append((float(kv["t_s"]), kv.get("label", "mark")))
            except (KeyError, ValueError):
                pass
            continue
        if line.startswith("#"):
            for tok in line.lstrip("# ").split():
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    meta[k] = v
            continue
        if cols is None:
            cols = [c.strip() for c in line.split(",")]
            continue
        parts = line.split(",")
        if len(parts) == len(cols):
            rows.append([float(p) for p in parts])
    if not cols or not rows or "ir" not in cols:
        raise ValueError(f"no IR samples in {path}")
    arr = np.asarray(rows)
    fs = float(meta.get("fs", 100.0))
    t0 = float(meta.get("session_t0", 0.0))
    return {
        "meta": meta, "events": events, "fs": fs,
        "t": t0 + np.arange(len(arr)) / fs,
        "ir": arr[:, cols.index("ir")],
        "red": arr[:, cols.index("red")] if "red" in cols else None,
        "dark": {c: float(meta[f"dark_{c}"]) for c in ("ir", "red") if f"dark_{c}" in meta},
    }


class FlowReplaySource(ReplaySource):
    """hr_monitor's replay, plus the recording's events at their times."""

    def run(self) -> None:
        try:
            rec = load_recording(self.path)
            t0 = float(rec["meta"].get("session_t0", 0.0))
            self.out.put(("replay_events", [(t - t0, lab) for t, lab in rec["events"]]))
        except Exception:
            pass                      # the base class reports a bad file
        super().run()


class _Grow:
    """Append-only float buffer."""

    def __init__(self) -> None:
        self.a = np.empty(4096)
        self.n = 0

    def extend(self, x: np.ndarray) -> None:
        need = self.n + len(x)
        if need > len(self.a):
            self.a = np.resize(self.a, max(need, 2 * len(self.a)))
        self.a[self.n:need] = x
        self.n = need

    def view(self) -> np.ndarray:
        return self.a[:self.n]


# ---------------------------------------------------------------------------
# Core: state and recording, no Tk
# ---------------------------------------------------------------------------

class FlowCore:
    def __init__(self, source, span_s: float = 180.0, before_s: float = 10.0,
                 settle_s: float = 5.0, after_s: float = 10.0,
                 auto_record: bool = False, run_id: str = "", note: str = "") -> None:
        from matplotlib.figure import Figure

        self.source = source
        self.q = source.out
        self.span_s = span_s
        self.before_s, self.settle_s, self.after_s = before_s, settle_s, after_s
        self.meta: dict = {}
        self.fs = 100.0
        self.ir, self.red = _Grow(), _Grow()
        self.events: list[tuple[float, str]] = []
        self.zero_ref: dict | None = None
        self._auto_zero_at: float | None = AUTO_ZERO_S
        self._replay_events: list[tuple[float, str]] = []
        self.flowing = True                 # runs start with intralipid flowing
        self.steps: list[Step] = []
        self.status = "waiting for the board…"
        self.eof = False
        self.set_run_info(run_id, note)
        self.auto_record = auto_record      # start once, when the board first streams
        self._rec: dict | None = None
        self.last_saved: Path | None = None
        self.fig = Figure(figsize=(13, 8.5))
        self.view = FlowView(self.fig)

    # -- clock and data ------------------------------------------------------

    @property
    def t_now(self) -> float:
        return self.ir.n / self.fs

    def times(self) -> np.ndarray:
        return np.arange(self.ir.n) / self.fs

    @property
    def dark(self) -> dict:
        return dict(self.meta.get("dark") or {})

    @property
    def has_red(self) -> bool:
        return "red" in self.meta.get("columns", [])

    def levels(self) -> dict:
        """Mean of the last 0.5 s per channel, as a fraction of full scale."""
        n = max(int(0.5 * self.fs), 1)
        out = {"ir": float(self.ir.view()[-n:].mean()) / FULL_SCALE if self.ir.n else None}
        if self.has_red and self.red.n:
            out["red"] = float(self.red.view()[-n:].mean()) / FULL_SCALE
        return out

    def range_warning(self) -> str | None:
        bad = [f"{ch.upper()} {v:.0%}" for ch, v in self.levels().items()
               if v is not None and not RAIL_LO <= v <= RAIL_HI]
        return ("outside 5–95 % of full scale: " + ", ".join(bad)) if bad else None

    # -- controls --------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._rec is not None

    def tune(self) -> bool:
        if getattr(self.source, "kind", "") != "serial":
            self.status = "auto-tune needs the board (not available in replay)"
            return False
        self.source.command("tune")
        self.status = "auto-tuning IR and red — tune with intralipid flowing"
        if self.recording:
            self.status += " (logged in the recording; steps will not span it)"
        return True

    def add_event(self, label: str, t: float | None = None) -> None:
        t = self.t_now if t is None else t
        self.events.append((t, label))
        if self._rec is not None:
            self._rec["raw"].write(f"# event t_s={t:.2f} label={label.replace(' ', '_')}\n")
            self._rec["raw"].flush()

    def toggle_flow(self) -> None:
        """The one marker button: NO FLOW, then FLOW, then NO FLOW..."""
        if not self.ir.n:
            self.status = "no data yet"
            return
        label = "no_flow" if self.flowing else "flow"
        self.flowing = not self.flowing
        self.add_event(label)
        self.status = (f"{label.replace('_', ' ').upper()} at {self.t_now:.1f} s — "
                       f"step ready in {self.settle_s + self.after_s:g} s")

    def zero(self, auto: bool = False) -> bool:
        n = int(ZERO_S * self.fs)
        if self.ir.n < n:
            self.status = f"need {ZERO_S:g} s of data to zero"
            return False
        self.zero_ref = {"ir": float(self.ir.view()[-n:].mean())}
        if self.has_red:
            self.zero_ref["red"] = float(self.red.view()[-n:].mean())
        self.add_event("zero_(auto)" if auto else "zero")
        if not auto:
            self.status = "OD zeroed"
        return True

    def od(self, ch: str, x: np.ndarray) -> np.ndarray:
        """ΔOD since the zero: + = less light back."""
        if not self.zero_ref or ch not in self.zero_ref:
            return np.full(len(x), np.nan)
        d = self.dark.get(ch, 0.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return -np.log((x - d) / (self.zero_ref[ch] - d))

    # -- recording ---------------------------------------------------------------

    def set_run_info(self, run_id: str, note: str) -> None:
        self.run_id = clean_run_id(run_id)
        # ASCII only: hr_monitor's replay reads files in the platform encoding.
        self.note = " ".join(note.encode("ascii", "replace").decode().split())

    def toggle_record(self) -> None:
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if not self.meta:
            self.status = "nothing to record yet"
            return
        warn = self.range_warning()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DATA_DIR / (f"flow_{stamp}" + (f"_{self.run_id}" if self.run_id else ""))
        fh = Path(str(base) + ".csv").open("w", encoding="utf-8")
        fh.write("# source=flow_bench\n")
        fh.write(f"# recorded_at={datetime.now().isoformat(timespec='seconds')}\n")
        if self.run_id:
            fh.write(f"# run_id={self.run_id}\n")
        if self.note:
            fh.write(f"# note={self.note.replace(' ', '_')}\n")
        fh.write(f"# session_t0={self.t_now:.3f}\n")
        for k, v in self.meta.get("raw", {}).items():
            fh.write(f"# {k}={v}\n")
        for k, v in self.dark.items():
            if f"dark_{k}" not in self.meta.get("raw", {}):
                fh.write(f"# dark_{k}={v:.0f}\n")
        fh.write(f"# fs={self.fs:.2f}\n")
        fh.write(f"# step_before_s={self.before_s:g} step_settle_s={self.settle_s:g} "
                 f"step_after_s={self.after_s:g} step_guard_s={GUARD_S:g}\n")
        cols = ["ir"] + (["red"] if self.has_red else [])
        fh.write("idx," + ",".join(cols) + "\n")
        self._rec = {"raw": fh, "idx": 0, "path": base, "t0": self.t_now, "cols": cols}
        self.status = f"recording to {base.name}.csv"
        if warn:
            self.status += f" — ! {warn}"

    def stop_recording(self) -> None:
        rec = self._rec
        if rec is None:
            return
        self._rec = None
        try:
            rec["raw"].close()
        except Exception:
            pass
        t0 = rec["t0"]
        mine = [(t, lab) for t, lab in self.events if t >= t0]
        steps = self._steps_for(mine)
        Path(str(rec["path"]) + "_steps.csv").write_text(steps_csv(steps), encoding="utf-8")
        n_ok = sum(s.status == "ok" for s in steps)
        self.last_saved = Path(str(rec["path"]) + ".csv")
        self.status = (f"saved {n_ok} steps to {DATA_DIR.name}/{rec['path'].name}.csv "
                       "and _steps.csv")
        if self.run_id and next_run_id(self.run_id) != self.run_id:
            self.run_id = next_run_id(self.run_id)

    def _record(self, ir: np.ndarray, red: np.ndarray | None) -> None:
        rec = self._rec
        fh, i0 = rec["raw"], rec["idx"]
        for j in range(len(ir)):
            row = f"{ir[j]:.0f}" + (f",{red[j]:.0f}" if red is not None and "red" in rec["cols"] else "")
            fh.write(f"{i0 + j},{row}\n")
        rec["idx"] = i0 + len(ir)

    # -- update loop ---------------------------------------------------------------

    def _steps_for(self, events) -> list[Step]:
        if not self.ir.n:
            return []
        return compute_steps(self.times(), self.ir.view(),
                             self.red.view() if self.has_red else None,
                             events, self.dark, self.before_s, self.settle_s, self.after_s)

    def _drain(self, max_msgs: int = 400) -> None:
        for _ in range(max_msgs):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "meta":
                self.meta = payload
                self.fs = float(payload["fs"])
            elif kind == "samples":
                if not self.meta:
                    continue
                cols = self.meta["columns"]
                ir = payload[:, cols.index("ir")]
                red = payload[:, cols.index("red")] if "red" in cols else None
                if self.auto_record:
                    self.auto_record = False
                    self.start_recording()
                self.ir.extend(ir)
                if red is not None:
                    self.red.extend(red)
                if self._rec is not None:
                    self._record(ir, red)
            elif kind == "status":
                self.status = payload
            elif kind == "reset":
                # A re-tune or link reset moves the counts with nothing physical
                # changing: mark it, and re-zero the OD trace once settled.
                self.add_event((payload or "reset").replace(" ", "_"))
                self.zero_ref = None
                self._auto_zero_at = self.t_now + AUTO_ZERO_S
            elif kind == "replay_events":
                self._replay_events = sorted(payload)
            elif kind == "eof":
                self.eof = True

    def tick(self) -> None:
        self._drain()
        while self._replay_events and self._replay_events[0][0] <= self.t_now:
            t, lab = self._replay_events.pop(0)
            if is_boundary(lab):
                self.add_event(lab, t)
                if lab in TRANSITIONS:
                    self.flowing = lab == "flow"
        if self._auto_zero_at is not None and self.t_now >= self._auto_zero_at:
            if self.zero(auto=True):
                self._auto_zero_at = None
        if not self.ir.n:
            self.view.waiting(self.status)
            return
        if self._rec is not None:
            self._rec["raw"].flush()
        self.steps = self._steps_for(self.events)
        self.view.update(self)


# ---------------------------------------------------------------------------
# View: draws into a matplotlib Figure, knows nothing about Tk
# ---------------------------------------------------------------------------

EVENT_COLORS = {"no_flow": "#c43b2f", "flow": "#2a6fb0"}
IR_COLOR, RED_COLOR = "#7a3db8", "#d0542c"


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) < n:
        return x
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.empty_like(x)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    out[:n - 1] = c[1:n] / np.arange(1, n)
    return out


def _decimate(t: np.ndarray, *ys: np.ndarray, max_pts: int = 3000):
    step = max(len(t) // max_pts, 1)
    return (t[::step],) + tuple(y[::step] for y in ys)


class FlowView:
    def __init__(self, fig) -> None:
        self.fig = fig
        gs = fig.add_gridspec(3, 1, height_ratios=[1.0, 1.0, 0.75], hspace=0.35)
        self.ax_pct = fig.add_subplot(gs[0])
        self.ax_od = fig.add_subplot(gs[1], sharex=self.ax_pct)
        self.ax_tab = fig.add_subplot(gs[2])
        self.ax_tab.axis("off")
        fig.subplots_adjust(left=0.07, right=0.98, top=0.95, bottom=0.03)

        ax = self.ax_pct
        (self.l_ir,) = ax.plot([], [], color=IR_COLOR, lw=1.0, label="IR")
        (self.l_red,) = ax.plot([], [], color=RED_COLOR, lw=1.0, label="red")
        for y in (RAIL_LO, RAIL_HI):
            ax.axhline(100 * y, color="#888", ls="--", lw=0.8)
        ax.set_ylabel("% of full scale")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.3)
        self.t_level = ax.set_title("", loc="right", fontsize=10)

        ax = self.ax_od
        (self.o_ir,) = ax.plot([], [], color=IR_COLOR, lw=1.0, label="IR")
        (self.o_red,) = ax.plot([], [], color=RED_COLOR, lw=1.0, label="red")
        ax.axhline(0, color="#888", lw=0.6)
        ax.set_ylabel("ΔOD since zero (+ = less light)")
        ax.set_xlabel("session time, s")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8)

        self.t_table = self.ax_tab.text(0.0, 1.0, "", va="top", ha="left",
                                        family="monospace", fontsize=9,
                                        transform=self.ax_tab.transAxes)
        self._n_events = 0
        self._vlines: list = []

    def waiting(self, status: str) -> None:
        self.t_table.set_text(status)

    def _draw_new_events(self, events) -> None:
        for t, label in events[self._n_events:]:
            if label.startswith("zero"):
                continue
            color = EVENT_COLORS.get(label, "#666")
            for ax in (self.ax_pct, self.ax_od):
                self._vlines.append(ax.axvline(t, color=color, lw=1.2,
                                               ls="-" if label in TRANSITIONS else ":"))
            self._vlines.append(self.ax_pct.text(
                t, 1.0, f" {label}", color=color, fontsize=8, va="top",
                transform=self.ax_pct.get_xaxis_transform()))
        self._n_events = len(events)

    def update(self, core: FlowCore) -> None:
        t_all = core.times()
        lo = max(0.0, core.t_now - core.span_s)
        sel = t_all >= lo
        t = t_all[sel]
        n_s = max(int(0.5 * core.fs), 1)
        ir = core.ir.view()[sel]
        red = core.red.view()[sel] if core.has_red else None

        td, ird = _decimate(t, ir)
        self.l_ir.set_data(td, 100 * ird / FULL_SCALE)
        oi = _decimate(t, _smooth(core.od("ir", ir), n_s))[1]
        self.o_ir.set_data(td, oi)
        if red is not None:
            self.l_red.set_data(td, 100 * _decimate(t, red)[1] / FULL_SCALE)
            self.o_red.set_data(td, _decimate(t, _smooth(core.od("red", red), n_s))[1])

        hi = max(core.t_now, lo + 10)
        self.ax_pct.set_xlim(lo, hi)
        pct = [100 * ird / FULL_SCALE]
        if red is not None:
            pct.append(100 * _decimate(t, red)[1] / FULL_SCALE)
        pmin = min(float(np.nanmin(p)) for p in pct)
        pmax = max(float(np.nanmax(p)) for p in pct)
        pad = max(2.0, 0.15 * (pmax - pmin))
        self.ax_pct.set_ylim(max(0.0, pmin - pad), min(100.0, pmax + pad))
        ods = [oi] + ([self.o_red.get_ydata()] if red is not None else [])
        finite = np.concatenate([np.asarray(o, float)[np.isfinite(o)] for o in ods])
        if finite.size:
            omin, omax = float(finite.min()), float(finite.max())
            opad = max(0.002, 0.15 * (omax - omin))
            self.ax_od.set_ylim(omin - opad, omax + opad)

        self._draw_new_events(core.events)
        levels = core.levels()
        self.t_level.set_text("   ".join(f"{ch.upper()} {v:.0%}" for ch, v in levels.items()
                                         if v is not None))
        self.t_level.set_color("#c43b2f" if core.range_warning() else "black")
        table = step_table(core.steps, last=8) if core.steps else (
            "No steps yet. Press space as you open the dump (NO FLOW), and again "
            "when flow is back (FLOW).\n"
            f"Each step: median over {core.before_s:g} s before the press vs median over "
            f"{core.after_s:g} s starting {core.settle_s:g} s after it.")
        self.t_table.set_text(table)


# ---------------------------------------------------------------------------
# Offline report
# ---------------------------------------------------------------------------

def report(path: Path, before_s: float, settle_s: float, after_s: float) -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rec = load_recording(path)
    meta = rec["meta"]
    before_s = float(meta.get("step_before_s", before_s))
    settle_s = float(meta.get("step_settle_s", settle_s))
    after_s = float(meta.get("step_after_s", after_s))
    steps = compute_steps(rec["t"], rec["ir"], rec["red"], rec["events"], rec["dark"],
                          before_s, settle_s, after_s)
    print(f"{path.name}: {len(rec['t']) / rec['fs']:.0f} s, "
          f"{sum(s.kind == 'no_flow' for s in steps)} no-flow, "
          f"{sum(s.kind == 'flow' for s in steps)} flow markers")
    if meta.get("note"):
        print(f"note: {meta['note'].replace('_', ' ')}")
    print(step_table(steps) if steps else "no flow / no-flow markers in this recording")

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 6.5), sharex=True)
    t = rec["t"]
    for x, color, name in ((rec["ir"], IR_COLOR, "IR"), (rec["red"], RED_COLOR, "red")):
        if x is None:
            continue
        a1.plot(t, 100 * x / FULL_SCALE, color=color, lw=0.8, label=name)
        d = rec["dark"].get(name.lower(), 0.0)
        n0 = min(int(ZERO_S * rec["fs"]), len(x))
        ref = float(x[:n0].mean())
        with np.errstate(divide="ignore", invalid="ignore"):
            a2.plot(t, _smooth(-np.log((x - d) / (ref - d)), max(int(0.5 * rec["fs"]), 1)),
                    color=color, lw=0.8, label=name)
    for te, lab in rec["events"]:
        if lab.startswith("zero"):
            continue
        for ax in (a1, a2):
            ax.axvline(te, color=EVENT_COLORS.get(lab, "#666"), lw=1.0,
                       ls="-" if lab in TRANSITIONS else ":")
    a1.set_ylabel("% of full scale")
    a2.set_ylabel("ΔOD from start")
    a2.set_xlabel("session time, s")
    for ax in (a1, a2):
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8)
    a1.set_title(f"{path.name}  (red line: no flow, blue line: flow)", fontsize=10)
    fig.tight_layout()
    out = path.with_name(path.stem + "_report.png")
    fig.savefig(out, dpi=120)
    print(f"figure: {out}")
    return 0


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class TkShell:
    def __init__(self, core: FlowCore) -> None:
        import tkinter as tk
        from tkinter import ttk

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.core = core
        self._closing = False
        root = tk.Tk()
        self.root = root
        root.title("MAPQuest — flow bench")
        root.geometry("1320x900")

        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        self.b_flow = tk.Button(bar, text="", width=24, font=("Segoe UI", 12, "bold"),
                                command=core.toggle_flow)
        self.b_rec = ttk.Button(bar, text="", width=24, command=self.on_record)
        self.b_tune = ttk.Button(bar, text="Auto-tune LEDs (T)", command=core.tune)
        for b in (self.b_flow, self.b_rec, self.b_tune):
            b.pack(side="left", padx=4)
        self.l_rec = tk.Label(bar, text="", font=("Segoe UI", 16, "bold"), fg="#c43b2f")
        self.l_rec.pack(side="right", padx=(4, 10))
        self.l_clock = tk.Label(bar, text="t = --", font=("Consolas", 26, "bold"))
        self.l_clock.pack(side="right", padx=8)
        self.l_state = tk.Label(bar, text="", font=("Segoe UI", 16, "bold"))
        self.l_state.pack(side="right", padx=12)

        info = ttk.Frame(root)
        info.pack(side="top", fill="x", padx=8, pady=(0, 4))
        ttk.Label(info, text="Next recording —  run ID:").pack(side="left")
        self.v_rid = tk.StringVar(value=core.run_id)
        self.e_rid = ttk.Entry(info, textvariable=self.v_rid, width=14)
        self.e_rid.pack(side="left", padx=(6, 18))
        ttk.Label(info, text="note (intralipid %, head height, tube…):").pack(side="left")
        self.v_note = tk.StringVar(value=core.note)
        self.e_note = ttk.Entry(info, textvariable=self.v_note, width=60)
        self.e_note.pack(side="left", padx=6)

        self.canvas = FigureCanvasTkAgg(core.fig, master=root)
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
        self.l_status = ttk.Label(root, anchor="w", padding=(8, 2))
        self.l_status.pack(side="bottom", fill="x")

        def key(fn):
            return lambda e: None if e.widget.winfo_class() in ("TEntry", "Entry") else fn()

        root.bind("<space>", key(core.toggle_flow))
        root.bind("<r>", key(self.on_record))
        root.bind("<t>", key(core.tune))
        root.bind("<Return>", lambda _e: root.focus_set())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_record(self) -> None:
        """Stop saves the file; Start opens a new one with the fields as typed."""
        core = self.core
        if not core.recording:
            core.set_run_info(self.v_rid.get(), self.v_note.get())
        core.toggle_record()
        self.v_rid.set(core.run_id)       # stop steps the ID on: F-01 -> F-02

    def _loop(self) -> None:
        if self._closing:
            return
        core = self.core
        try:
            core.tick()
            self.canvas.draw_idle()
        except Exception as exc:  # keep the window alive; show what broke
            core.status = f"display error: {exc}"
        if core.flowing:
            self.b_flow.config(text="Mark NO FLOW (space)", bg="#f6d9d6")
            self.l_state.config(text="FLOWING", fg="#2a6fb0")
        else:
            self.b_flow.config(text="Mark FLOW (space)", bg="#d8e6f4")
            self.l_state.config(text="NO FLOW", fg="#c43b2f")
        self.b_rec.config(text="Stop recording (R)" if core.recording
                          else "Start recording (R)")
        is_serial = getattr(core.source, "kind", "") == "serial"
        self.b_tune.state(["!disabled"] if is_serial else ["disabled"])
        warn = core.range_warning()
        self.l_status.config(text=core.status + (f"   ·   ! {warn}" if warn else ""))
        self.l_clock.config(text=f"t = {core.t_now:7.1f} s" if core.ir.n else "t = --")
        self.l_rec.config(text="● REC" if core.recording else "not recording")
        self.root.after(50, self._loop)

    def run(self) -> None:
        self.root.after(100, self._loop)
        self.root.mainloop()

    def on_close(self) -> None:
        self._closing = True
        self.core.source.stop()
        self.core.stop_recording()
        self.root.quit()
        self.root.destroy()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ir-amp", type=int, metavar="N",
                    help="IR LED current register, 0-255 (×0.2 mA)")
    ap.add_argument("--red-amp", type=int, metavar="N",
                    help="red LED current register, 0-255")
    ap.add_argument("--auto-tune", action="store_true",
                    help="tune IR and red on connect (have intralipid flowing)")
    ap.add_argument("--run-id", default="",
                    help="ID for the first recording, e.g. F-01; steps on with each new one")
    ap.add_argument("--note", default="",
                    help="free text for the recording header (intralipid %%, head height...)")
    ap.add_argument("--span", type=float, default=180.0, help="seconds shown")
    ap.add_argument("--before", type=float, default=10.0,
                    help="seconds of baseline before each key press")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="seconds skipped after each key press while the tube drains or fills")
    ap.add_argument("--after", type=float, default=10.0,
                    help="seconds averaged after the settle")
    ap.add_argument("--replay", type=Path, metavar="CSV",
                    help="play a saved recording instead of using the board "
                         "(not recorded unless you press Start recording)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--report", type=Path, metavar="CSV",
                    help="print the step table for a saved recording and save a figure")
    args = ap.parse_args()

    if args.report:
        return report(args.report, args.before, args.settle, args.after)

    q: queue.Queue = queue.Queue()
    if args.replay:
        source = FlowReplaySource(q, args.replay, speed=args.speed)
    else:
        source = SerialSource(q, port=args.port, baud=args.baud,
                              ir_amp=args.ir_amp, red_amp=args.red_amp,
                              auto_tune=args.auto_tune)
    core = FlowCore(source, span_s=args.span, before_s=args.before,
                    settle_s=args.settle, after_s=args.after,
                    auto_record=not args.replay, run_id=args.run_id, note=args.note)
    source.start()
    TkShell(core).run()
    source.stop()
    core.stop_recording()
    return 0


if __name__ == "__main__":
    sys.exit(main())
