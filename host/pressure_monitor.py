#!/usr/bin/env python3
"""MAPQuest cuff-pressure monitor for the MPRLS sensor test (firmware/mprls_test).

Standalone from the PPG tools: the PPG is unplugged and the MPRLS sits on its
pins. Use it to check the sensor tracks the cuff before it joins the PPG bus.

    python pressure_monitor.py              # auto-detect the board
    python pressure_monitor.py --port COM5
    python pressure_monitor.py --demo       # simulated cuff, no hardware

What it shows:

* **Cuff pressure** in mmHg, relative to the room. The zero is taken
  automatically 2 s after connecting, so connect with the cuff valve OPEN.
  Zero (Z) re-takes it; the absolute reading stays on screen so a bad zero
  is visible.
* **Deflation rate** over the last 2 s, green inside the 2-3 mmHg/s the
  protocol asks for (SPEC 8), amber outside it.
* **Cuff oscillations**: pressure band-passed 0.5-5 Hz over the last 12 s.
  With the cuff between diastolic and systolic, each heartbeat shows as a
  ripple of 1-3 mmHg. Seeing them confirms the sensor and tubing are
  connected and responsive.

Record (R) writes data/pressure_<stamp>.csv: session time, raw counts,
status, absolute and cuff mmHg. Pressures are provisional: the sensor's span
has not been checked against a reference.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.mprls import (  # noqa: E402
    MMHG_PER_PSI, OUTPUT_MAX, OUTPUT_MIN, P_MAX_PSI, PressureTracker, parse_line,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RATE_TARGET = (2.0, 3.0)        # mmHg/s deflation, SPEC 8


# ---------------------------------------------------------------------------
# Sources: put ("line", text) / ("status", text) / ("connected", port) on a queue
# ---------------------------------------------------------------------------

class SerialSource(threading.Thread):
    kind = "serial"

    def __init__(self, out: queue.Queue, port: str | None = None, baud: int = 115200):
        super().__init__(daemon=True)
        self.out, self.port, self.baud = out, port, baud
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import serial
        from mapquest.io import find_port

        while not self._stop.is_set():
            try:
                port = find_port(self.port)
                with serial.Serial(port, self.baud, timeout=0.2) as ser:
                    ser.reset_input_buffer()
                    ser.write(b"I")
                    self.out.put(("connected", port))
                    buf = b""
                    while not self._stop.is_set():
                        chunk = ser.read(ser.in_waiting or 1)
                        if not chunk:
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self.out.put(("line", line.decode("ascii", "replace").strip()))
            except Exception as exc:  # unplugged, wrong port, board resetting
                self.out.put(("status", f"no board: {exc}; retrying"))
                self._stop.wait(2.0)


class DemoSource(threading.Thread):
    """A simulated cuff: rest, inflate to 170, hold, bleed at 2.5 mmHg/s, release.
    Heartbeat ripple appears between 'diastolic' 75 and 'systolic' 120."""

    kind = "demo"

    def __init__(self, out: queue.Queue, fs: float = 100.0, seed: int = 0):
        super().__init__(daemon=True)
        self.out, self.fs = out, fs
        self.rng = np.random.default_rng(seed)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def cuff(t: float) -> float:
        t = t % 90.0
        if t < 5:
            return 0.0
        if t < 12:
            return (t - 5) / 7 * 170.0
        if t < 20:
            return 170.0 - (t - 12) * 0.5
        p = 166.0 - (t - 20) * 2.5
        return max(p, 0.0) if p > 30 else max(0.0, 30 - (t - 20 - 54.4) * 15)

    def run(self) -> None:
        atm_counts = OUTPUT_MIN + (760.0 / MMHG_PER_PSI) / P_MAX_PSI * (OUTPUT_MAX - OUTPUT_MIN)
        per_mmhg = (OUTPUT_MAX - OUTPUT_MIN) / (P_MAX_PSI * MMHG_PER_PSI)
        self.out.put(("connected", "demo"))
        self.out.put(("line", "# firmware=demo"))
        t0, n = time.time(), 0
        while not self._stop.is_set():
            t = n / self.fs
            p = self.cuff(t)
            ripple = 0.0
            if 75 < p < 120:
                ripple = 1.5 * np.sin(2 * np.pi * 1.2 * t) * min(1.0, (120 - p) / 15, (p - 75) / 15)
            counts = int(atm_counts + (p + ripple + self.rng.normal(0, 0.15)) * per_mmhg)
            self.out.put(("line", f"M,{int(t * 1000)},{counts},64"))
            n += 1
            ahead = t0 + n / self.fs - time.time()
            if ahead > 0:
                self._stop.wait(ahead)


# ---------------------------------------------------------------------------
# Core: queue -> tracker, recording, figure. No Tk, so it runs in tests.
# ---------------------------------------------------------------------------

class PressureCore:
    def __init__(self, source, span_s: float = 120.0):
        from matplotlib.figure import Figure

        self.q: queue.Queue = source.out
        self.source = source
        self.tracker = PressureTracker()
        self.span_s = span_s
        self.t_start = time.time()
        self.status = "waiting for the board…"
        self.header: list[str] = []
        self._rec = None
        self.fig = Figure(figsize=(12, 7.5))
        gs = self.fig.add_gridspec(2, 1, height_ratios=[2.2, 1])
        self.ax_p = self.fig.add_subplot(gs[0])
        self.ax_o = self.fig.add_subplot(gs[1])
        self.l_p, = self.ax_p.plot([], [], color="#5b3fa8", lw=1.4)
        self.l_o, = self.ax_o.plot([], [], color="#c43b2f", lw=1.0)
        self.ax_p.set_ylabel("cuff pressure (mmHg)")
        self.ax_p.grid(alpha=0.3)
        self.ax_o.set_ylabel("oscillation (mmHg)\n0.5–5 Hz band")
        self.ax_o.set_xlabel("session time (s)")
        self.ax_o.grid(alpha=0.3)
        self.fig.tight_layout()

    # -- data ---------------------------------------------------------------
    def session_t(self) -> float:
        return time.time() - self.t_start

    def drain(self, max_msgs: int = 2000) -> None:
        for _ in range(max_msgs):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "connected":
                self.tracker.start(self.session_t())
                self.header = []
                self.status = f"connected on {payload}; zeroing in 2 s — keep the cuff valve open"
            elif kind == "status":
                self.status = payload
            elif payload.startswith("#"):
                self.header.append(payload)
                if "sensor missing" in payload or "sensor lost" in payload:
                    self.status = payload.lstrip("# ")
            else:
                s = parse_line(payload)
                if s is None:
                    continue
                self.tracker.push(*s)
                if self._rec is not None and self.tracker.t:
                    st = self.tracker
                    rel = st.abs[-1] - st.zero if st.zero is not None else float("nan")
                    self._rec.write(f"{st.t[-1]:.3f},{s[1]},{s[2]},{st.abs[-1]:.2f},{rel:.2f}\n")

    def zero(self) -> None:
        z = self.tracker.set_zero(1.0)
        if z is not None:
            self.status = f"zeroed at {z:.1f} mmHg absolute"

    # -- recording ----------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self._rec is not None

    def toggle_record(self) -> None:
        if self._rec is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / f"pressure_{datetime.now():%Y%m%d_%H%M%S}.csv"
            self._rec = path.open("w")
            self._rec.write("# source=pressure_monitor (MPRLS, firmware/mprls_test)\n")
            self._rec.write(f"# recorded_at={datetime.now().isoformat(timespec='seconds')}\n")
            for h in self.header:
                self._rec.write(h + "\n")
            z = self.tracker.zero
            self._rec.write(f"# zero_abs_mmhg={'' if z is None else f'{z:.2f}'}\n")
            self._rec.write("# PROVISIONAL: sensor span unverified\n")
            self._rec.write("t_s,counts,status,abs_mmhg,mmhg\n")
            self._rec_path = path
            self.status = f"recording to {path.name}"
        else:
            self.stop_recording()

    def stop_recording(self) -> None:
        if self._rec is not None:
            self._rec.close()
            self.status = f"saved {self._rec_path.name}"
            self._rec = None

    # -- display ------------------------------------------------------------
    def redraw(self):
        st = self.tracker.state()
        if st is None:
            return None
        t, rel, _a = self.tracker.arrays(self.span_s)
        self.l_p.set_data(t, rel)
        self.ax_p.set_xlim(max(t[0], t[-1] - self.span_s), t[-1] + 0.5)
        finite = rel[np.isfinite(rel)]
        top = max(40.0, float(finite.max()) * 1.1) if len(finite) else 40.0
        self.ax_p.set_ylim(-5, top)
        to, osc = self.tracker.oscillations(12.0)
        self.l_o.set_data(to, osc)
        if len(to):
            self.ax_o.set_xlim(to[0], to[-1] + 0.2)
            lim = max(1.0, float(np.abs(osc).max()) * 1.2) if len(osc) else 1.0
            self.ax_o.set_ylim(-lim, lim)
        return st


class TkShell:
    def __init__(self, core: PressureCore):
        import tkinter as tk
        from tkinter import ttk

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.core = core
        self._closing = False
        root = tk.Tk()
        self.root = root
        root.title("MAPQuest — cuff pressure (MPRLS)")
        root.geometry("1200x860")

        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        self.b_rec = ttk.Button(bar, text="Record (R)", command=core.toggle_record)
        self.b_zero = ttk.Button(bar, text="Zero (Z)", command=core.zero)
        self.b_rec.pack(side="left", padx=3)
        self.b_zero.pack(side="left", padx=3)
        self.l_rec = tk.Label(bar, text="", font=("Segoe UI", 16, "bold"), fg="#c43b2f")
        self.l_rec.pack(side="right", padx=(4, 10))
        self.l_clock = tk.Label(bar, text="t = --", font=("Consolas", 22, "bold"))
        self.l_clock.pack(side="right", padx=8)

        top = ttk.Frame(root)
        top.pack(side="top", fill="x", padx=12)
        self.l_mmhg = tk.Label(top, text="--", font=("Segoe UI", 54, "bold"), width=6, anchor="e")
        self.l_mmhg.pack(side="left")
        tk.Label(top, text="mmHg", font=("Segoe UI", 18)).pack(side="left", padx=(4, 30), anchor="s",
                                                               pady=14)
        info = ttk.Frame(top)
        info.pack(side="left", fill="x")
        self.l_rate = tk.Label(info, text="rate --", font=("Segoe UI", 20, "bold"), anchor="w")
        self.l_rate.pack(anchor="w")
        self.l_detail = tk.Label(info, text="", font=("Segoe UI", 12), anchor="w", justify="left")
        self.l_detail.pack(anchor="w")

        self.canvas = FigureCanvasTkAgg(core.fig, master=root)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.l_status = ttk.Label(root, anchor="w", padding=(8, 2))
        self.l_status.pack(side="bottom", fill="x")

        root.bind("<r>", lambda _e: core.toggle_record())
        root.bind("<z>", lambda _e: core.zero())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._callback_error

    def _callback_error(self, exc_type, exc, tb) -> None:
        """A button or key handler raised: keep the window up and say what broke.

        Tk's default prints to stderr, which is None under pythonw (double-click
        launch), and the failure can then take the whole window down.
        """
        import traceback

        where = traceback.extract_tb(tb)[-1] if tb else None
        loc = f" ({Path(where.filename).name}:{where.lineno})" if where else ""
        self.core.status = f"error: {exc_type.__name__}: {exc}{loc}"
        if sys.stderr is not None:
            traceback.print_exception(exc_type, exc, tb)

    def _loop(self) -> None:
        if self._closing:
            return
        try:
            self.core.drain()
            st = self.core.redraw()
            if st is not None:
                self._readouts(st)
                self.canvas.draw_idle()
        except Exception as exc:  # keep the window alive; show what broke
            self.core.status = f"display error: {exc}"
        self.b_rec.config(text="Stop rec (R)" if self.core.recording else "Record (R)")
        self.l_rec.config(text="● REC" if self.core.recording else "")
        self.l_status.config(text=self.core.status)
        self.root.after(50, self._loop)

    def _readouts(self, st) -> None:
        self.l_clock.config(text=f"t = {st.t:7.1f} s")
        if st.zero_mmhg is None:
            self.l_mmhg.config(text="zeroing", fg="#888888")
        else:
            v = round(st.mmhg)
            self.l_mmhg.config(text=f"{v if v != 0 else 0:.0f}", fg="#202020")   # no "-0"
        lo, hi = RATE_TARGET
        if st.rate < -0.5:
            ok = lo <= -st.rate <= hi
            self.l_rate.config(text=f"deflating {-st.rate:.1f} mmHg/s",
                               fg="#1f8a55" if ok else "#c47a00")
        elif st.rate > 0.5:
            self.l_rate.config(text=f"rising {st.rate:.1f} mmHg/s", fg="#5b3fa8")
        else:
            self.l_rate.config(text="steady", fg="#555555")
        z = "--" if st.zero_mmhg is None else f"{st.zero_mmhg:.1f}"
        self.l_detail.config(text=(
            f"absolute {st.abs_mmhg:.1f} mmHg   zero {z}   peak {st.peak:.0f} mmHg\n"
            f"oscillation {st.oscillation:.1f} mmHg p-p (last 3 s)   target deflation "
            f"{lo:g}–{hi:g} mmHg/s" + (f"\n! {st.problem}" if st.problem else "")))

    def run(self) -> None:
        self.root.after(100, self._loop)
        self.root.mainloop()

    def on_close(self) -> None:
        self._closing = True
        self.core.source.stop()
        self.core.stop_recording()
        self.root.quit()
        self.root.destroy()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--demo", action="store_true", help="simulated cuff, no hardware needed")
    ap.add_argument("--span", type=float, default=120.0, help="seconds of pressure shown")
    args = ap.parse_args(argv)
    q: queue.Queue = queue.Queue()
    src = DemoSource(q) if args.demo else SerialSource(q, args.port)
    core = PressureCore(src, span_s=args.span)
    src.start()
    TkShell(core).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
