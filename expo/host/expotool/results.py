"""Results manager: from the board's stream to a return-of-flow pressure.

expo/DESIGN.md section 7. Three parts, all usable without a board:

``RunRecorder``  watches the line stream (the same lines the board sends over
                 serial and Wi-Fi) and cuts it into runs: from START (state
                 INFLATE) to the vent (state IDLE), with 15 s of the stream
                 before the run kept as pre-roll (the uncuffed pulse).
``analyse()``    turns one run into a result: windows from the board's own
                 state events (7.2), ``host/mapquest``'s ``derive()`` run
                 unchanged, the hinge onset as the headline marker (7.3),
                 and the confidence flags (7.4).
``ResultsLog``   the anonymous results file (7.5): run number, time, result,
                 flag, reason. Never any signal.

Only a run that reached DEFLATE is a run: it gets a number. Only one that
ended at ``p_stop`` gets a pressure; one vented early gets ``safety_stop``
(or ``stopped`` when someone pressed DUMP) and no number.

Thresholds marked PROVISIONAL are first guesses, not bench-checked numbers;
team dry runs on the expo cycle should set them (DESIGN 10).
"""

from __future__ import annotations

import csv
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from mapquest import pulse_return
from mapquest.mprls import counts_to_mmhg
from mapquest.pipeline import PipelineConfig, derive
from mapquest.quality import detect_motion

FS_DEFAULT = 100.0
PRE_ROLL_S = 15.0
FULL_SCALE = 262143                  # MAX30101, 18 bits
COMPLETED = "reached stop pressure"  # the dump reason of a run that finished

# Confidence thresholds (DESIGN 7.4).
RAIL_HI, RAIL_LO = 0.97, 0.02        # fraction of full scale: saturated / no light
RAIL_MAX_FRACTION = 0.01             # more than 1 % of deflation samples on a rail
PULSE_STOPPED_MAX = 0.5              # PROVISIONAL: occluded pulse / uncuffed pulse
IMU_MOVE_DPS = 20.0                  # PROVISIONAL: gyro magnitude that counts as moving
IMU_LSB_PER_DPS = 65.536             # BMI270 at 500 dps (firmware INFO)
MOTION_AFTER_ONSET_S = 5.0           # motion this long after the onset still matters
MIN_UNCUFFED_S = 4.0                 # pre-roll needed to judge the uncuffed pulse
DUMP_WAIT_S = 20.0                   # give up waiting for IDLE after a dump

# Pressure glitches (DESIGN 7.2): single MPRLS readings that sit far off their
# neighbours. On the rig they read low by 3-20 mmHg, one or two readings at a
# time: the valve's switch-off spike corrupts the conversion it lands in, and
# bus errors add more. A cuff can't move that far in 50 ms and come back.
GLITCH_HALF = 5                      # median over 11 readings (~110 ms at 100 Hz)
GLITCH_MMHG = 3.0                    # off that median by more than this: dropped
GLITCH_MAX_FRACTION = 0.25           # dropping more than this: the trace itself is suspect
# What's left still wobbles ~1.3 mmHg (the valve's flow ripple at the sensor;
# the cuff itself can only lose air while deflating). A centred mean over this
# span halves it and, centred, adds no lag or bias on the 2 mmHg/s ramp.
PRESSURE_SMOOTH_S = 0.5

# Flag order when several apply: the first is sent, all are logged.
FLAG_ORDER = ("incomplete", "arm_moved", "pulse_present")
FLAG_TEXT = {
    "ok": "",
    "no_onset": "no clear return of flow found",
    "incomplete": "signal incomplete",
    "arm_moved": "arm moved",
    "pulse_present": "flow may not have fully stopped",
    "safety_stop": "the cuff vented itself before the end",
    "stopped": "the run was stopped",
}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@dataclass
class Run:
    fs: float
    zero_abs: float | None
    dark: dict
    rows: list = field(default_factory=list)     # S: (idx, t_us, ir, red, p_idx, p_raw, p_status)
    imu: list = field(default_factory=list)      # M: (t_us, band, gx, gy, gz)
    events: list = field(default_factory=list)   # EV: (t_us, name, detail)
    lines: list = field(default_factory=list)    # every line, for --save-raw (team only)
    missing: int = 0                             # S samples missing inside the run
    gaps: list = field(default_factory=list)     # why the stream has holes
    reached_deflate: bool = False
    end_reason: str | None = None
    t_dump_us: float | None = None
    number: int | None = None                    # given by the manager at DEFLATE

    def state_time(self, state: str) -> float | None:
        for t_us, name, detail in self.events:
            if name == "state" and detail == state:
                return t_us
        return None


class RunRecorder:
    """Feed it every line; it says when a run starts deflating, ends, and is
    ready to analyse.

    ``line()`` returns a list of ``(kind, run)``:
      "deflate"   the run reached DEFLATE: it counts, give it a number
      "ended"     the cuff was dumped (any reason) after DEFLATE
      "complete"  the cuff has vented (IDLE): analyse it now
    """

    def __init__(self) -> None:
        self.fs = FS_DEFAULT
        self.zero_abs: float | None = None
        self.dark: dict = {}
        self.run: Run | None = None
        self._pre: deque = deque(maxlen=int(PRE_ROLL_S * FS_DEFAULT))
        self._pre_lines: deque = deque(maxlen=int(PRE_ROLL_S * 330))
        self._last_idx: int | None = None
        self._t_off = 0
        self._t_last: int | None = None

    # -- time: t_us is a 32-bit micros() that wraps every 71.6 min ---------

    def _unwrap(self, t_us: int) -> float:
        if self._t_last is not None:
            if t_us < self._t_last - 2**31:
                self._t_off += 2**32
            elif t_us > self._t_last + 2**31:          # a straggler from before the wrap
                return float(t_us + self._t_off - 2**32)
        self._t_last = t_us
        return float(t_us + self._t_off)

    # -- input -------------------------------------------------------------

    def link_lost(self) -> None:
        if self.run is not None:
            self.run.gaps.append("link lost")
        self._last_idx = None

    def line(self, line: str) -> list:
        line = line.strip()
        if not line:
            return []
        if self.run is not None:
            self.run.lines.append(line)
        else:
            self._pre_lines.append(line)
        if line[0] == "#":
            self._meta(line[1:].strip())
            return []
        f = line.split(",")
        try:
            if f[0] == "S" and len(f) >= 8:
                return self._sample(f)
            if f[0] == "M" and len(f) >= 10 and self.run is not None:
                self.run.imu.append((self._unwrap(int(f[2])), f[3], int(f[7]), int(f[8]), int(f[9])))
            elif f[0] == "EV" and len(f) >= 3:
                return self._event(self._unwrap(int(f[1])), f[2], ",".join(f[3:]))
        except ValueError:
            pass                                       # a garbled line is not a sample
        return []

    def _meta(self, body: str) -> None:
        pairs = dict(tok.split("=", 1) for tok in body.split() if "=" in tok)
        try:
            if "cuff_zero_abs_mmhg" in pairs:
                self.zero_abs = float(pairs["cuff_zero_abs_mmhg"])
            if pairs.get("band") == "D" and "dark_ir" in pairs:
                self.dark = {"ir": float(pairs["dark_ir"]), "red": float(pairs["dark_red"])}
            if "fs" in pairs and body.startswith("mode="):
                self.fs = float(pairs["fs"])
        except ValueError:
            pass

    def _sample(self, f: list) -> list:
        idx = int(f[1])
        row = (idx, self._unwrap(int(f[2])), int(f[3]), int(f[4]), int(f[5]), int(f[6]), int(f[7]))
        if self._last_idx is not None and idx > self._last_idx + 1 and self.run is not None:
            self.run.missing += idx - self._last_idx - 1
        self._last_idx = idx
        run = self.run
        if run is None:
            self._pre.append(row)
            return []
        run.rows.append(row)
        if run.t_dump_us is not None and row[1] - run.t_dump_us > DUMP_WAIT_S * 1e6:
            return self._finish("no IDLE after the dump")       # lost the vent, analyse anyway
        return []

    def _event(self, t_us: float, name: str, detail: str) -> list:
        run = self.run
        if name in ("stream_gap", "ppg_overflow", "wifi_overflow") and run is not None:
            run.gaps.append(f"{name} {detail}".strip())
        if name == "state" and detail == "INFLATE":
            out = self._finish("a new run started") if run is not None else []
            self.run = Run(self.fs, self.zero_abs, dict(self.dark), rows=list(self._pre),
                           lines=list(self._pre_lines))
            self.run.events.append((t_us, name, detail))
            return out
        if run is None:
            return []
        run.events.append((t_us, name, detail))
        if name == "state" and detail == "DEFLATE" and not run.reached_deflate:
            run.reached_deflate = True
            return [("deflate", run)]
        if name == "dump" and run.end_reason is None:
            run.end_reason, run.t_dump_us = detail, t_us
            return [("ended", run)] if run.reached_deflate else []
        if name == "state" and detail == "IDLE" and run.end_reason is not None:
            return self._finish(None)
        return []

    def _finish(self, why: str | None) -> list:
        run, self.run = self.run, None
        self._pre.clear()
        self._pre_lines.clear()
        if run is None:
            return []
        if why:
            run.gaps.append(why)
        if run.end_reason is None:
            run.end_reason = why or "unknown"
        return [("complete", run)] if run.reached_deflate else []


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass
class Result:
    run: int
    mmhg: float | None
    flag: str
    reason: str
    flags: list = field(default_factory=list)
    markers: dict = field(default_factory=dict)   # name -> (t_s, mmHg) or None
    detail: dict = field(default_factory=dict)
    seconds: float = 0.0                          # analysis time

    def command(self) -> str:
        """The RESULT line for the board (expo/DESIGN.md 3.1)."""
        val = "-" if self.mmhg is None else f"{self.mmhg:.1f}"
        return f"RESULT {self.run} {val} {self.flag}"


def _stopped(run: Run, number: int) -> Result:
    flag = "stopped" if run.end_reason == "user" else "safety_stop"
    return Result(number, None, flag, f"cuff vented: {run.end_reason}")


def analyse(run: Run, number: int) -> Result:
    t_start = time.perf_counter()
    if run.end_reason != COMPLETED:
        res = _stopped(run, number)
    else:
        try:
            res = _analyse_complete(run, number)
        except ValueError as e:                    # too little data to run the chain
            res = Result(number, None, "incomplete", f"not enough signal to analyse ({e})")
    res.seconds = time.perf_counter() - t_start
    return res


def _analyse_complete(run: Run, number: int) -> Result:
    if len(run.rows) < 64:
        raise ValueError(f"{len(run.rows)} samples")
    a = np.asarray(run.rows, dtype=float)
    idx, t_us, ir, red, p_idx, p_raw, p_st = a.T
    fs = run.fs
    t = idx / fs                                      # sample time: idx / fs is true time

    def at(t_event_us):                               # board event -> sample time
        return float(np.interp(t_event_us, t_us, t))

    t_infl = at(run.state_time("INFLATE"))
    t_hold_us, t_defl_us = run.state_time("HOLD"), run.state_time("DEFLATE")
    t_defl = at(t_defl_us)
    t_hold = at(t_hold_us) if t_hold_us is not None else t_infl
    t_dump = at(run.t_dump_us)

    # Cuff pressure from the fresh MPRLS readings (powered, not saturated).
    _, first = np.unique(p_idx, return_index=True)
    first = np.sort(first)
    st = p_st[first].astype(int)
    good = first[(st & 0x40).astype(bool) & ~(st & 0x01).astype(bool)]
    if good.size < 10:
        raise ValueError("no good pressure readings")
    absolute = counts_to_mmhg(p_raw[good])
    t_p = t[good]
    glitch = pressure_glitches(absolute)
    absolute, t_p = absolute[~glitch], t_p[~glitch]
    zero = run.zero_abs
    if zero is None:                                  # pre-roll is vented
        early = t_p < t_infl
        zero = float(np.median(absolute[early])) if early.any() else float(absolute[0])
    p_mmhg = smooth_pressure(absolute - zero, t_p)

    def pressure(at_s):
        return float(np.interp(at_s, t_p, p_mmhg))

    # Windows from the board (DESIGN 7.2), not guessed from the pressure trace.
    ref = (max(t_hold, t_defl - 5.0), t_defl)
    hold = (t_hold + 1.0, t_defl)
    channels = {"ir": ir, "red": red}
    d = derive(channels, fs, dark=run.dark or None, ref_window=ref, deflation_start_s=t_defl,
               t=t, config=PipelineConfig(), search_end_s=t_dump)

    onset = d.onset_time_hinge
    markers = {
        "refill onset (hinge)": None if onset is None else (onset, pressure(onset)),
        "refill onset (intersect)": None if d.onset_time_sloped is None
        else (d.onset_time_sloped, pressure(d.onset_time_sloped)),
    }

    flags, reasons, detail = [], [], {
        "windows": {"inflate": t_infl, "hold": t_hold, "deflate": t_defl, "dump": t_dump,
                    "ref": ref},
        "p_deflate_start": pressure(t_defl), "hinge_power": d.hinge_power,
        "pressure_glitches": int(glitch.sum()), "pressure_readings": int(glitch.size),
    }

    # -- signal incomplete: holes in the stream, or light on a rail ----------
    holes = [g for g in run.gaps]
    if run.missing:
        holes.append(f"{run.missing} samples missing")
    if glitch.mean() > GLITCH_MAX_FRACTION:
        holes.append(f"pressure sensor glitched on {glitch.mean() * 100:.0f} % of readings")
    span = (t >= t_defl) & (t <= t_dump)
    railed = 0.0
    if span.any():
        lo, hi = RAIL_LO * FULL_SCALE, RAIL_HI * FULL_SCALE
        on_rail = (ir[span] >= hi) | (red[span] >= hi) | (ir[span] <= lo) | (red[span] <= lo)
        railed = float(on_rail.mean())
        if railed > RAIL_MAX_FRACTION:
            holes.append(f"light sensor at its limit for {railed * 100:.0f} % of the deflation")
    detail["rail_fraction"] = railed
    if holes:
        flags.append("incomplete")
        reasons.append("; ".join(holes))

    # -- arm moved: the validated PPG motion detector, and the IMUs -----------
    m_end = (onset + MOTION_AFTER_ONSET_S) if onset is not None else t_dump
    moved = []
    if span.any():
        mot = detect_motion({"ir": ir[span], "red": red[span]}, fs)
        t0 = float(t[span][0])
        for e0, e1 in mot.events:
            if t0 + e0 <= m_end:
                moved.append(f"light signal jumped at {t0 + e0 - t_defl:.0f} s into deflation")
                break
    if run.imu:
        m = np.asarray([(tu, gx, gy, gz) for tu, _band, gx, gy, gz in run.imu], dtype=float)
        mt = np.interp(m[:, 0], t_us, t)
        sel = (mt >= t_defl) & (mt <= m_end)
        if sel.any():
            peak = float(np.max(np.linalg.norm(m[sel, 1:], axis=1)) / IMU_LSB_PER_DPS)
            detail["imu_peak_dps"] = peak
            if peak > IMU_MOVE_DPS:
                moved.append(f"band rotated at {peak:.0f} deg/s")
    if moved:
        flags.append("arm_moved")
        reasons.append("; ".join(moved))

    # -- pulse never fully stopped: occluded pulse vs the uncuffed pulse -------
    ir_ds = ir - float((run.dark or {}).get("ir", 0.0))
    p_t = np.interp(t, t_p, p_mmhg)
    uncuffed = (p_t < 8.0) & ((t < t_infl) | (t > t_dump + 1.0))
    pr = pulse_return.detect(t, ir_ds, fs, hold, t_defl, uncuffed)
    markers["pulse return"] = None
    if pr is not None:
        if pr.t_return is not None:
            markers["pulse return"] = (pr.t_return, pressure(pr.t_return))
        detail["heart_rate_bpm"] = pr.f0_hz * 60.0
        before = uncuffed & (t < t_infl)
        if before.sum() >= MIN_UNCUFFED_S * fs:
            level = float(np.median(pr.env_hr[before]))
            ratio = pr.floor / level if level > 0 else float("nan")
            detail["occluded_pulse_ratio"] = ratio
            if ratio > PULSE_STOPPED_MAX:
                flags.append("pulse_present")
                reasons.append(f"pulse during the hold was {ratio * 100:.0f} % of the uncuffed pulse")
        else:
            detail["occluded_pulse_ratio"] = None       # joined mid-run: no uncuffed pulse to compare

    detail["traces"] = _traces(run, t, t_us, ir, p_t, d, pr, t_infl, t_dump, fs, onset)

    if onset is None:
        return Result(number, None, "no_onset", "; ".join([FLAG_TEXT["no_onset"]] + reasons),
                      flags, markers, detail)
    flag = next((f for f in FLAG_ORDER if f in flags), "ok")
    mmhg = round(pressure(onset), 1)
    return Result(number, mmhg, flag, "; ".join(reasons), flags, markers, detail)


def pressure_glitches(p: np.ndarray, half: int = GLITCH_HALF, limit: float = GLITCH_MMHG) -> np.ndarray:
    """True for readings more than ``limit`` mmHg off the median of the
    ``2 * half + 1`` readings around them. A median follows ramps and steps
    (inflation, deflation, the dump) exactly, so only isolated readings that
    jump away and straight back are caught; up to ``half`` in a row."""
    p = np.asarray(p, dtype=float)
    if p.size < 2 * half + 1:
        return np.zeros(p.size, dtype=bool)
    windows = np.lib.stride_tricks.sliding_window_view(np.pad(p, half, mode="edge"), 2 * half + 1)
    return np.abs(p - np.median(windows, axis=1)) > limit


def smooth_pressure(p: np.ndarray, t: np.ndarray, span_s: float = PRESSURE_SMOOTH_S) -> np.ndarray:
    """Centred moving mean over ``span_s`` (readings counted from their own
    rate, so dropped glitches don't stretch the window much)."""
    p = np.asarray(p, dtype=float)
    if p.size < 3:
        return p
    rate = (p.size - 1) / max(float(t[-1] - t[0]), 1e-9)
    n = int(round(span_s * rate)) | 1                 # odd, so the window is centred
    if n < 3 or p.size < n:
        return p
    return np.convolve(np.pad(p, n // 2, mode="edge"), np.ones(n) / n, mode="valid")


TRACE_HZ = 10.0


def _list(x, nd=4) -> list:
    return [None if not np.isfinite(v) else round(float(v), nd) for v in np.asarray(x, dtype=float)]


def _traces(run, t, t_us, ir, p_t, d, pr, t_infl, t_dump, fs, onset) -> dict:
    """What the technical view draws, at 10 Hz, times from the start of
    inflation. Shown on the monitor during the visit, never written to the
    results file (DESIGN 7.5)."""
    step = max(1, int(round(fs / TRACE_HZ)))
    sel = np.flatnonzero((t >= t_infl - 5.0) & (t <= t_dump + 3.0))[::step]
    tr = {
        "t": _list(t[sel] - t_infl, 2), "p": _list(p_t[sel], 2),
        "raw_ir": _list(ir[sel], 0), "d_od": _list(d.d_od["ir"][sel], 5),
        "dv": _list(d.dv[sel]), "dv_lp": _list(d.dv_lp[sel]), "ddv": _list(d.ddv[sel]),
        "threshold": None if not np.isfinite(d.threshold) else round(float(d.threshold), 5),
    }
    if d.hinge_fit is not None and onset is not None:
        b0, b1, a, ws = d.hinge_fit
        th = np.linspace(ws, onset + 6.0, 80)
        tr["hinge"] = {"t": _list(th - t_infl, 2),
                       "y": _list(b0 + b1 * (th - ws) + a * np.clip(th - onset, 0, None) ** d.hinge_power),
                       "p": _list(np.interp(th, t, p_t), 2)}
    if d.baseline_fit is not None and d.baseline_span is not None:
        bs, bi = d.baseline_fit
        tb = np.linspace(d.baseline_span[0], (onset or d.baseline_span[1]) + 8.0, 40)
        tr["baseline"] = {"t": _list(tb - t_infl, 2), "y": _list(bs * tb + bi), "p": _list(np.interp(tb, t, p_t), 2)}
    if pr is not None:
        tr["pulse_env"] = _list(pr.env_hr[sel] * np.sqrt(2), 5)
        tr["pulse_thr"] = round(float(pr.threshold * np.sqrt(2)), 5)
    if run.imu:
        m = np.asarray([(tu, gx, gy, gz) for tu, band, gx, gy, gz in run.imu if band == "D"], dtype=float)
        if m.size:
            mt = np.interp(m[:, 0], t_us, t) - t_infl
            g = np.linalg.norm(m[:, 1:], axis=1) / IMU_LSB_PER_DPS
            k = max(1, len(mt) // 400)
            tr["imu"] = {"t": _list(mt[::k], 2), "dps": _list(g[::k], 1), "limit": IMU_MOVE_DPS}
    return tr


def payload(res: Result) -> dict:
    """The result as the dashboard gets it (``A,{"kind":"result",...}``):
    times relative to the start of inflation."""
    w = res.detail.get("windows", {})
    t0 = w.get("inflate", 0.0)
    rel = {k: (round(v - t0, 2) if isinstance(v, float) else v) for k, v in w.items() if k != "ref"}
    marks = {k: (None if v is None else [round(v[0] - t0, 2), round(v[1], 1)]) for k, v in res.markers.items()}
    keep = ("p_deflate_start", "hinge_power", "rail_fraction", "imu_peak_dps", "heart_rate_bpm",
            "occluded_pulse_ratio")
    det = {k: res.detail[k] for k in keep if k in res.detail}
    return _clean({"run": res.run, "mmhg": res.mmhg, "flag": res.flag, "reason": res.reason,
                   "flags": list(res.flags), "markers": marks, "windows": rel, "detail": det,
                   "traces": res.detail.get("traces"), "seconds": round(res.seconds, 3)})


def _clean(x):
    """JSON a browser can parse: no NaN or infinity, no numpy scalars."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return float(x) if np.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    return x


# ---------------------------------------------------------------------------
# The anonymous results file
# ---------------------------------------------------------------------------

class ResultsLog:
    """One row per run (DESIGN 7.5): run, time, result, flag, reason."""

    FIELDS = ("run", "time", "mmhg", "flag", "reason")

    def __init__(self, path: Path | None) -> None:
        """``path=None`` keeps nothing (replays, tests)."""
        self.path = None if path is None else Path(path)
        self.last_run = 0
        if self.path is not None and self.path.exists():
            with self.path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        self.last_run = max(self.last_run, int(row["run"]))
                    except (KeyError, ValueError):
                        pass

    def next_run(self) -> int:
        self.last_run += 1
        return self.last_run

    def append(self, res: Result) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(self.FIELDS)
            w.writerow([res.run, time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "" if res.mmhg is None else f"{res.mmhg:.1f}", res.flag, res.reason])


def describe(res: Result) -> str:
    """One line for the laptop's console."""
    head = (f"run {res.run}: " + ("no number" if res.mmhg is None else f"{res.mmhg:.0f} mmHg")
            + f" [{res.flag}]")
    tail = f" {res.reason}" if res.reason else ""
    marks = ", ".join(f"{k} {v[1]:.0f}" for k, v in res.markers.items() if v)
    return head + tail + (f"  ({marks})" if marks else "") + f"  [{res.seconds:.2f} s]"
