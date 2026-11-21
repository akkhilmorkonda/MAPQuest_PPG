"""Bench GUI for the expo board over USB: three graphs and the controls.

For benchtop testing of the electronics and the software logic, not visitors.
The board stays plugged into this laptop; no Wi-Fi, no phone, no browser.

    python expo/host/bench_gui.py                 # the only serial port
    python expo/host/bench_gui.py --port COM8
    python expo/host/bench_gui.py --mock          # the simulated board: no hardware

Three graphs, stacked on one time axis (the last N seconds):
  1. cuff pressure, mmHg (pressure glitches dropped and smoothed, as the
     results analysis does);
  2. relative blood volume, dV*L in um = 229 dOD_red + 1555 dOD_IR, distal
     (solid; the band the result uses) and proximal (dashed, from the SP
     lines; shown only), each against its own reference.
     dOD = ln(reference / light): host/mapquest's pipeline steps 2 and 3,
     dark offsets included. The reference is the light over the 2 s before
     deflation, as the analysis uses; "Zero volume" sets both to now;
  3. accelerometer, raw counts: X, Y, Z of each band's IMU (distal solid,
     proximal dashed), to see what the IMU picks up around the return of
     flow. At the firmware's +-4 g range, 8192 counts = 1 g.

Left: the connected parts, START / RUN / DUMP (Esc always dumps), Zero cuff,
Refresh sensors (R), VALVE TEST, and the result of each run from the results
manager (expotool/results.py).

Recording: "Start recording" streams every line from the board (pressure
gauge, both PPGs, both IMUs, the valve controller, status, events, errors) and
every button press to expo/results/bench/run_<NNNN>/raw.txt as it arrives.
Runs are numbered on from the highest in expo_results.csv, results/raw and the
bench folders; on stop the folder is renamed for the runs it holds (run_0008,
run_0008-0009, or no_run_<time> if none reached DEFLATE);
"Stop recording" closes it and writes one CSV per stream beside it
(pressure, ppg_distal, ppg_proximal, imu, valve_control, status, events,
buttons). raw.txt also replays with ``expo_results.py --replay``, and
``bench_gui.py --export <raw.txt>`` redoes the CSVs. "Save everything so far"
writes what's in memory (the last ~2 million lines) when you forgot to record. Closing the window
during a run sends DUMP.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expotool.results import (FLAG_TEXT, GLITCH_HALF, GLITCH_MMHG, IMU_LSB_PER_DPS,  # noqa: E402
                              PRESSURE_SMOOTH_S, ResultsLog, RunRecorder, analyse)
from mapquest.pipeline import COEFF_IR_880, COEFF_RED_660  # noqa: E402
from usb_bridge import SerialLink  # noqa: E402

EXPO = Path(__file__).resolve().parent.parent
SAVE_DIR = EXPO / "results" / "bench"
RUN_DIR = re.compile(r"run_(\d{4})(?:-(\d{4}))?")   # run_0008, or run_0008-0009 for two runs


def highest_run(save_dir: Path = SAVE_DIR, results: Path = EXPO / "results") -> int:
    """The highest run number used anywhere: the results file, results/raw,
    and the bench recordings (run_NNNN folders). New runs count on from it,
    so the bench and the expo results share one numbering."""
    hi = ResultsLog(results / "expo_results.csv").last_run
    for p in (results / "raw").glob("run_*.txt"):
        m = re.fullmatch(r"run_(\d{4})\.txt", p.name)
        if m:
            hi = max(hi, int(m.group(1)))
    if save_dir.exists():
        for p in save_dir.iterdir():
            m = RUN_DIR.fullmatch(p.name)
            if m and p.is_dir():
                hi = max(hi, int(m.group(2) or m.group(1)))
    return hi


def run_folder_name(first: int, runs: list[int]) -> str:
    """Where a finished recording ends up: by the runs it holds."""
    if not runs:
        return f"no_run_{time.strftime('%Y%m%d_%H%M%S')}"
    lo, hi = min(runs), max(runs)
    return f"run_{lo:04d}" if lo == hi else f"run_{lo:04d}-{hi:04d}"
OUT_MIN, OUT_MAX, PSI_MAX, MMHG_PER_PSI = 1677722, 15099494, 25.0, 51.71493
ACTIVE = ("INFLATE", "HOLD", "DEFLATE")
PAUSE_STREAM = ("ZERO", "R", "VALVE")       # refused while streaming on serial
KEEP_S = 600.0
STATE_COLOURS = {"IDLE": "#5f6b7a", "INFLATE": "#b26b00", "HOLD": "#8a5a00",
                 "DEFLATE": "#1f5fa8", "DUMP": "#c62828"}
PARTS = ("gauge", "valve", "mux", "ppg_D", "imu_D", "ppg_P", "imu_P")
PART_NAMES = {"gauge": "Pressure gauge", "valve": "Valve", "mux": "Mux",
              "ppg_D": "Distal PPG", "imu_D": "Distal IMU", "ppg_P": "Prox PPG", "imu_P": "Prox IMU"}
ST_BITS = {"gauge": 0, "ppg_D": 1, "ppg_P": 2, "imu_D": 3, "imu_P": 4}   # the ST sensor bits
GOOD, BAD, NOTE, UNKNOWN = "#2e7d32", "#c62828", "#1f5fa8", "#8a8a8a"


class MockLink:
    """The simulated board (expotool.hub over the IRB mock), with SerialLink's interface."""

    pause_stream = False             # the simulator pauses its own stream for ZERO

    def __init__(self, speed: float = 1.0) -> None:
        from expotool.hub import LinkSimHub
        self.hub = LinkSimHub()
        self.speed = speed
        self.t0 = time.monotonic()
        self.cid = self.hub.connect(0.0)
        self.hub.receive(self.cid, "CLAIM", 0.0)
        self.lock = threading.Lock()
        self.first = True

    def start(self) -> None:
        pass

    def now(self) -> float:
        return (time.monotonic() - self.t0) * self.speed

    def take(self):
        with self.lock:
            self.hub.receive(self.cid, "PING", self.now())
            lines, _ = self.hub.tick(self.now())
        ev, self.first = (["up mock"] if self.first else []), False
        return lines, ev

    def write(self, lines) -> None:
        with self.lock:
            for ln in lines:
                if not ln.startswith("STREAM"):
                    self.hub.receive(self.cid, ln, self.now())

    def close(self) -> None:
        pass


class Signals:
    """The three series, from the stream (seconds on the board's clock)."""

    def __init__(self) -> None:
        self.zero_abs = None
        self.dark = {"D": {"ir": 0.0, "red": 0.0}, "P": {"ir": 0.0, "red": 0.0}}
        self.clear()

    def clear(self) -> None:
        self.wrap = 0
        self.last_us = None
        self.t_first = None
        self.pressure = deque()       # (t, mmHg): glitches dropped, smoothed
        self.light = {"D": deque(), "P": deque()}   # (t, ir, red) 10 Hz means per band, dark-subtracted
        self.accel = {"D": deque(), "P": deque()}  # (t, ax, ay, az) raw counts per band, 50 Hz
        self.states = deque()         # (t, state)
        self.last_p_idx = None
        self._med = deque(maxlen=2 * GLITCH_HALF + 1)
        self._avg = deque(maxlen=max(3, int(PRESSURE_SMOOTH_S * 100) | 1))
        self._acc = {"D": [], "P": []}
        self.ref = {"D": None, "P": None}   # (ir, red) light reference per band
        self.onset = None             # (t, mmHg) from the analysis

    def t(self, us_text: str) -> float:
        us = int(us_text)
        if self.last_us is not None and us < self.last_us - (1 << 31):
            self.wrap += 1 << 32
        self.last_us = us
        t = (us + self.wrap) / 1e6
        if self.t_first is None:
            self.t_first = t
        return t - self.t_first

    def latest_t(self) -> float:
        for q in (self.light["D"], self.light["P"], self.pressure):
            if q:
                return q[-1][0]
        return 0.0

    def line(self, ln: str) -> None:
        f = ln.split(",")
        try:
            if f[0] == "S" and len(f) >= 8:
                t = self.t(f[2])
                ir, red, p_idx, p_raw, st = (int(x) for x in f[3:8])
                self._light("D", t, ir, red)
                if p_idx != self.last_p_idx and self.zero_abs is not None and st & 0x40 and not st & 0x01:
                    self.last_p_idx = p_idx
                    self._pressure(t, (p_raw - OUT_MIN) * PSI_MAX / (OUT_MAX - OUT_MIN) * MMHG_PER_PSI
                                   - self.zero_abs)
            elif f[0] == "SP" and len(f) >= 5:          # the proximal band: SP,idx,t_us,ir,red
                self._light("P", self.t(f[2]), int(f[3]), int(f[4]))
            elif f[0] == "M" and len(f) >= 10 and f[3] in self.accel:
                self.accel[f[3]].append((self.t(f[2]), int(f[4]), int(f[5]), int(f[6])))
            elif f[0] == "C" and len(f) >= 8 and self.zero_abs is None:
                p = float(f[4])                     # no zero yet: the board's own 10 Hz pressure
                if not math.isnan(p):
                    self.pressure.append((self.t(f[2]), p))
            elif f[0] == "EV" and len(f) >= 4 and f[2] == "state":
                t = self.t(f[1])
                self.states.append((t, f[3]))
                if f[3] == "INFLATE":
                    self.onset, self.ref = None, {"D": None, "P": None}
                elif f[3] == "DEFLATE":
                    self.set_ref(t)                 # the analysis's reference: the end of the hold
            elif ln.startswith("# "):
                pairs = dict(tok.split("=", 1) for tok in ln[2:].split() if "=" in tok)
                if "cuff_zero_abs_mmhg" in pairs:
                    self.zero_abs = float(pairs["cuff_zero_abs_mmhg"])
                if pairs.get("band") in self.dark and "dark_ir" in pairs:
                    self.dark[pairs["band"]] = {"ir": float(pairs["dark_ir"]), "red": float(pairs["dark_red"])}
        except (ValueError, IndexError):
            pass
        cut = self.latest_t() - KEEP_S
        for q in (self.pressure, self.light["D"], self.light["P"], self.accel["D"], self.accel["P"]):
            while q and q[0][0] < cut:
                q.popleft()

    def _pressure(self, t: float, p: float) -> None:
        self._med.append(p)
        if len(self._med) == self._med.maxlen and abs(p - float(np.median(self._med))) > GLITCH_MMHG:
            return                                  # a glitch, as results.pressure_glitches
        self._avg.append(p)
        self.pressure.append((t, sum(self._avg) / len(self._avg)))

    def _light(self, band: str, t: float, ir: int, red: int) -> None:
        d = self.dark[band]
        acc = self._acc[band]
        acc.append((ir - d["ir"], red - d["red"]))
        if len(acc) >= 10:
            a = np.mean(acc, axis=0)
            self.light[band].append((t, float(a[0]), float(a[1])))
            acc.clear()

    def set_ref(self, t_end: float | None = None) -> None:
        """Light reference per band: the mean over the 2 s before ``t_end`` (default: now)."""
        for band, light in self.light.items():
            if not light:
                continue
            end = light[-1][0] if t_end is None else t_end
            sel = [(ir, red) for t, ir, red in light if end - 2.0 <= t <= end]
            if sel:
                a = np.mean(sel, axis=0)
                self.ref[band] = (float(a[0]), float(a[1]))


class BenchApp:
    def __init__(self, link, title: str) -> None:
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        self.link = link
        self.sig = Signals()
        self.rec = RunRecorder()
        self.lines_kept: list[str] = []
        self.state, self.sensors, self.zeroed = "?", "?????", False
        self.mux_ok, self.gauge_status, self.gauge_integrity, self.duty = None, None, "?", None
        self.paused = False
        self.auto_refreshed = False
        self.rec_file, self.rec_dir, self.rec_n, self.rec_t0 = None, None, 0, 0.0
        self.last_run = highest_run()        # one numbering with the expo results
        self.rec_runs: list[int] = []        # runs that reached DEFLATE in this recording

        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("1400x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_all("<Escape>", lambda e: self.send("DUMP", source="Esc key"))
        left = ttk.Frame(self.root, padding=8)
        left.pack(side="left", fill="y")

        self.v_link = tk.StringVar(value="link: starting")
        ttk.Label(left, textvariable=self.v_link).pack(anchor="w")
        self.l_state = tk.Label(left, text="?", font=("Segoe UI", 18, "bold"), fg="white", bg="#5f6b7a")
        self.l_state.pack(fill="x", pady=(4, 0))
        self.v_p = tk.StringVar(value="-- mmHg")
        tk.Label(left, textvariable=self.v_p, font=("Segoe UI", 26, "bold")).pack(fill="x")

        box = ttk.LabelFrame(left, text="Connected parts", padding=3)
        box.pack(fill="x", pady=4)
        self.l_part = {}
        for i, key in enumerate(PARTS):
            lb = tk.Label(box, text=f"{PART_NAMES[key]}: ?", bg=UNKNOWN, fg="white", anchor="w",
                          font=("Segoe UI", 9), padx=4)
            lb.grid(row=i, column=0, sticky="ew", pady=1)
            self.l_part[key] = lb
        box.columnconfigure(0, weight=1)

        box = ttk.Frame(left)
        box.pack(fill="x", pady=4)
        buttons = (("START", "START"), ("RUN", "RUN"), ("Zero cuff", "ZERO"), ("Refresh sensors", "R"),
                   ("Valve test", "VALVE TEST"))
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(box, text=text, command=lambda c=cmd: self.send(c)).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)
        ttk.Button(box, text="Zero volume", command=self.zero_volume).grid(row=2, column=1, sticky="ew", padx=1)
        tk.Button(box, text="DUMP  (Esc)", bg="#c62828", fg="white", font=("Segoe UI", 13, "bold"),
                  command=lambda: self.send("DUMP")).grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        self.b_pause = ttk.Button(box, text="Pause graphs", command=self.toggle_pause)
        self.b_pause.grid(row=4, column=0, sticky="ew", padx=1)
        ttk.Button(box, text="Save everything so far", command=self.save).grid(row=4, column=1, sticky="ew", padx=1)
        self.b_rec_start = tk.Button(box, text="●  Start recording", fg="#c62828", font=("Segoe UI", 10, "bold"),
                                     command=self.start_recording)
        self.b_rec_start.grid(row=5, column=0, sticky="ew", padx=1, pady=(6, 1))
        self.b_rec_stop = tk.Button(box, text="■  Stop recording", font=("Segoe UI", 10, "bold"),
                                    command=self.stop_recording, state="disabled")
        self.b_rec_stop.grid(row=5, column=1, sticky="ew", padx=1, pady=(6, 1))
        self.v_rec = tk.StringVar(value="Not recording")
        tk.Label(box, textvariable=self.v_rec, wraplength=250, justify="left", font=("Segoe UI", 8)).grid(
            row=6, column=0, columnspan=2, sticky="w")
        box.columnconfigure((0, 1), weight=1)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="Show last").pack(side="left")
        self.v_win = tk.StringVar(value="120")
        ttk.Combobox(row, textvariable=self.v_win, values=("30", "60", "120", "300", "600"), width=5,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(row, text="s").pack(side="left")
        self.v_detrend = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Accelerometer: remove offsets (each channel minus its\n"
                                   "mean over the window, to see small changes)",
                        variable=self.v_detrend).pack(anchor="w", pady=(4, 0))

        self.v_result = tk.StringVar(value="No run analysed yet")
        tk.Label(left, textvariable=self.v_result, wraplength=250, justify="left",
                 font=("Segoe UI", 10, "bold"), fg="#1f5fa8").pack(anchor="w", pady=6)
        self.log = tk.Text(left, width=36, height=10, font=("Consolas", 8), wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#c62828")

        self.fig = Figure(figsize=(10, 9), dpi=90, constrained_layout=True)
        self.ax_p, self.ax_v, self.ax_m = self.fig.subplots(3, 1, sharex=True)
        self.h_p, = self.ax_p.plot([], [], color="#1a1f3d", lw=1.4)
        self.ax_p.set_ylabel("cuff pressure, mmHg")
        self.h_v = {"D": self.ax_v.plot([], [], color="#c1203b", lw=1.4, label="distal (forearm)")[0],
                    "P": self.ax_v.plot([], [], color="#6a3d9a", lw=1.2, ls="--", label="proximal (upper arm)")[0]}
        self.ax_v.set_ylabel("relative blood volume\ndV*L, um")
        self.ax_v.legend(loc="upper left", fontsize=7)
        self.h_acc = {}                    # (band, axis) -> line
        for band, style, name in (("D", "-", "distal"), ("P", "--", "prox")):
            for k, (axis, colour) in enumerate((("X", "#c62828"), ("Y", "#2e7d32"), ("Z", "#1f5fa8"))):
                self.h_acc[(band, k)], = self.ax_m.plot([], [], ls=style, color=colour, lw=0.8,
                                                        label=f"{name} {axis}")
        self.ax_m.set_ylabel("accelerometer, raw counts\n(8192 = 1 g)")
        self.ax_m.set_xlabel("time, s")
        self.ax_m.legend(loc="upper left", fontsize=7, ncol=6)
        for ax in (self.ax_p, self.ax_v, self.ax_m):
            ax.grid(alpha=0.3)
        self.marks = []
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side="right", fill="both", expand=True)

        self.root.after(50, self.poll)
        self.root.after(300, self.redraw)

    # -- commands ------------------------------------------------------------

    def link_up(self) -> bool:
        return getattr(self.link, "ser", True) is not None     # SerialLink: None while down

    def record(self, what: str) -> None:
        """A button press, into the saved lines among the board's own, so a
        saved file shows what was pressed, when, and what the board did next.
        ``# gui`` lines are comments to the results manager and --replay."""
        wall = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        board = f"{self.sig.latest_t():.2f}s" if self.sig.t_first is not None else "?"
        self.keep(f"# gui {wall} board_t {board} {what}")

    def keep(self, line: str) -> None:
        """Every line, from the board or a button: into memory, and into the
        recording when one is running."""
        self.lines_kept.append(line)
        if self.rec_file is not None:
            self.rec_file.write(line + "\n")
            self.rec_n += 1

    # -- recording -----------------------------------------------------------

    def start_recording(self) -> None:
        if self.rec_file is not None:
            return
        # Named for the run it will hold; renamed at the stop if it held none or two.
        self.last_run = max(self.last_run, highest_run())
        self.rec_runs = []
        self.rec_dir = SAVE_DIR / f"run_{self.last_run + 1:04d}"
        if self.rec_dir.exists():                      # never write into an old run
            self.rec_dir = SAVE_DIR / f"run_{self.last_run + 1:04d}_{time.strftime('%H%M%S')}"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.rec_file = open(self.rec_dir / "raw.txt", "w", encoding="utf-8", newline="\n")
        self.rec_n, self.rec_t0 = 0, time.monotonic()
        # What the lines need to be read on their own: the cuff zero and the darks
        # known so far, then INFO from the board (columns, settings, sensors).
        self.keep(f"# recording started {time.strftime('%Y-%m-%d %H:%M:%S')} link {self.v_link.get()}")
        if self.sig.zero_abs is not None:
            self.keep(f"# cuff_zero_abs_mmhg={self.sig.zero_abs:.3f} (known at start)")
        for band, d in self.sig.dark.items():
            self.keep(f"# band={band} dark_ir={d['ir']:.1f} dark_red={d['red']:.1f} (known at start)")
        self.record("button Start recording")
        self.send("INFO", source="recording start")
        self.b_rec_start.configure(state="disabled")
        self.b_rec_stop.configure(state="normal")
        self.logline(f"recording to {self.rec_dir.name}")

    def stop_recording(self) -> None:
        if self.rec_file is None:
            return
        self.record("button Stop recording")
        self.keep(f"# recording stopped {time.strftime('%Y-%m-%d %H:%M:%S')}")
        f, self.rec_file = self.rec_file, None
        f.close()
        self._name_recording()
        folder, n, secs = self.rec_dir, self.rec_n, time.monotonic() - self.rec_t0
        self.b_rec_start.configure(state="normal")
        self.b_rec_stop.configure(state="disabled")
        self.v_rec.set(f"Saved {n} lines, {secs:.0f} s: {folder.name} (writing CSVs…)")

        def work():
            try:
                counts = export_csv(folder / "raw.txt")
                text = f"Saved {secs:.0f} s to {folder.name}: " + ", ".join(f"{k} {v}" for k, v in counts.items())
            except Exception as e:
                text = f"Saved {folder.name}/raw.txt; CSV export failed: {type(e).__name__}: {e}"
            self.root.after(0, lambda: (self.v_rec.set(text), self.logline(text[:80])))

        threading.Thread(target=work, daemon=True).start()

    def _name_recording(self) -> None:
        """Rename the closed recording by its runs: run_0008, run_0008-0009,
        or no_run_<time> when no run reached DEFLATE (its number is reused)."""
        name = run_folder_name(self.last_run, self.rec_runs)
        if name == self.rec_dir.name:
            return
        target = SAVE_DIR / name
        if target.exists():
            target = SAVE_DIR / f"{name}_{time.strftime('%H%M%S')}"
        try:
            self.rec_dir = self.rec_dir.rename(target)
        except OSError as e:
            self.logline(f"could not rename {self.rec_dir.name} to {target.name}: {e}", "err")

    def send(self, cmd: str, source: str = "button") -> None:
        if not self.link_up():
            self.record(f"{source} {cmd}: NOT SENT (USB link down)")
            self.logline(f"> {cmd}: NOT SENT, USB link down", "err")
            return
        if cmd.split(" ")[0] in PAUSE_STREAM and getattr(self.link, "pause_stream", True):
            lines = ["STREAM 0", cmd, "STREAM 1"]
        else:
            lines = [cmd]
        self.link.write(lines)
        self.record(f"{source} {cmd}: sent {' | '.join(lines)}")
        self.logline(f"> {cmd}")

    def zero_volume(self) -> None:
        self.sig.set_ref()
        self.record("button Zero volume (local: blood volume reference set to now)")
        self.logline("> Zero volume")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.b_pause.configure(text="Resume graphs" if self.paused else "Pause graphs")
        self.record(f"button {'Pause' if self.paused else 'Resume'} graphs (local)")

    def save(self) -> None:
        self.record("button Save lines (local)")
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        path = SAVE_DIR / time.strftime("bench_%Y%m%d_%H%M%S.txt")
        path.write_text("\n".join(self.lines_kept) + "\n", encoding="utf-8")
        self.logline(f"saved {len(self.lines_kept)} lines: {path.name}")

    def logline(self, text: str, tag: str = "") -> None:
        self.log.insert("end", text + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")
        self.log.see("end")

    # -- stream --------------------------------------------------------------

    def poll(self) -> None:
        lines, events = self.link.take()
        for ev in events:
            self.v_link.set(f"link: {ev}")
            self.logline(f"link {ev}", "" if ev.startswith("up") else "err")
            if ev.startswith("up"):
                self.link.write(["INFO"])
                if not self.auto_refreshed:
                    # The board looks for its sensors once at boot and never again by
                    # itself: one missed then stays missing until R. Ask once.
                    self.auto_refreshed = True
                    self.send("R", source="auto (on connect)")
        for ln in lines:
            ln = ln.rstrip("\r")
            if not ln:
                continue
            self.keep(ln)
            self.sig.line(ln)
            for kind, run in self.rec.line(ln):
                if kind == "deflate":                  # a run gets its number here, as in expo_results
                    self.last_run = max(self.last_run, highest_run()) + 1
                    run.number = self.last_run
                    if self.rec_file is not None:
                        self.rec_runs.append(run.number)
                elif kind == "complete":
                    self._analyse(run)
            self._status(ln)
        if self.rec_file is not None:
            self.rec_file.flush()                      # on disk within ~50 ms, even if the app dies
        if len(self.lines_kept) > 2_000_000:
            del self.lines_kept[:500_000]
        self.root.after(50, self.poll)

    def _bit(self, part: str, ok: bool) -> None:
        s = list(self.sensors.ljust(5, "?"))
        s[ST_BITS[part]] = "1" if ok else "0"
        self.sensors = "".join(s)

    def _status(self, ln: str) -> None:
        f = ln.split(",")
        if f[0] == "S" and len(f) >= 8 and f[7].isdigit():
            self.gauge_status = int(f[7])
        elif f[0] == "C" and len(f) >= 8:
            self.state = f[3]
            try:
                self.duty = float(f[7])
            except ValueError:
                pass
        elif f[0] == "ST" and len(f) >= 5:
            self.state, self.sensors, self.zeroed = f[2], f[3], f[4] == "1"
        elif f[0] == "EV" and len(f) >= 3:
            if f[2] != "slow_loop":
                self.logline(",".join(f[2:])[:80])
            if f[2] == "state" and len(f) >= 4:
                self.state = f[3]
        elif f[0] == "E":
            self.logline(ln[2:][:80], "err")
        elif ln.startswith("# "):
            pairs = dict(tok.split("=", 1) for tok in ln[2:].split() if "=" in tok)
            if "mux_ok" in pairs:
                self.mux_ok = pairs["mux_ok"] == "1"
            if "pressure_ok" in pairs:
                self._bit("gauge", pairs["pressure_ok"] == "1")
                self.gauge_integrity = pairs.get("pressure_integrity", "?")
                if pairs.get("pressure_status", "").startswith("0x"):
                    self.gauge_status = int(pairs["pressure_status"], 16)
            if pairs.get("band") in ("D", "P"):
                self._bit(f"ppg_{pairs['band']}", pairs.get("ppg_ok") == "1")
                self._bit(f"imu_{pairs['band']}", pairs.get("imu_ok") == "1")
            if "cuff_zero_abs_mmhg" in pairs:
                self.zeroed = True
            if ln.startswith(("# ready", "# NOT", "# mapquest")):
                self.logline(ln[2:][:80])

    def _analyse(self, run) -> None:
        n = getattr(run, "number", None) or self.last_run
        self.v_result.set(f"Run {n}: analysing…")

        def work():
            try:
                res = analyse(run, n)
            except Exception as e:
                msg = f"Run {n}: analysis failed ({type(e).__name__}: {e})"
                self.root.after(0, lambda: self.v_result.set(msg))
                return
            self.root.after(0, lambda: self._show_result(n, res))

        threading.Thread(target=work, daemon=True).start()

    def _show_result(self, n: int, res) -> None:
        mm = "no reading" if res.mmhg is None else f"{res.mmhg:.1f} mmHg"
        flag = FLAG_TEXT.get(res.flag) or res.flag
        self.v_result.set(f"Run {n}: {mm} ({flag})" + (f"\n{res.reason}" if res.reason else ""))
        self.logline(f"result run {n}: {mm} {res.flag}")
        on = res.markers.get("refill onset (hinge)")
        if on and self.sig.states:
            # analysis time is from its own start; place it by the deflate event
            t_defl = next((t for t, s in reversed(self.sig.states) if s == "DEFLATE"), None)
            w = res.detail.get("windows", {})
            if t_defl is not None and "deflate" in w:
                self.sig.onset = (t_defl + on[0] - w["deflate"], on[1])

    # -- drawing -------------------------------------------------------------

    def _parts(self) -> None:
        bits = self.sensors.ljust(5, "?")

        def show(key, colour, text):
            self.l_part[key].configure(bg=colour, text=f"{PART_NAMES[key]}: {text}")

        g, st = bits[0], self.gauge_status
        if g == "?":
            show("gauge", UNKNOWN, "?")
        elif g == "0":
            show("gauge", BAD, "NOT FOUND (check wiring, Refresh)")
        elif self.gauge_integrity == "FAIL" or (st is not None and st & 0x04):
            show("gauge", BAD, "integrity FAIL: replace")
        elif not self.zeroed:
            show("gauge", "#d08700", "not zeroed (vent, Zero cuff)")
        else:
            show("gauge", GOOD, "OK")
        d = self.duty
        if d is None or math.isnan(d):
            show("valve", UNKNOWN, "?")
        elif d <= 0:
            show("valve", NOTE, "closed (commanded)")
        elif d >= 100:
            show("valve", "#5f6b7a", "open (commanded)")
        else:
            show("valve", "#e08a00", f"{d:.0f}% open (commanded)")
        show("mux", UNKNOWN if self.mux_ok is None else GOOD if self.mux_ok else BAD,
             "?" if self.mux_ok is None else "OK" if self.mux_ok else "NOT FOUND")
        for key in ("ppg_D", "imu_D", "ppg_P", "imu_P"):
            b = bits[ST_BITS[key]]
            show(key, UNKNOWN if b == "?" else GOOD if b == "1" else BAD,
                 "?" if b == "?" else "OK" if b == "1" else "not found")

    def redraw(self) -> None:
        try:
            self._draw()
        finally:
            self.root.after(400, self.redraw)

    def _draw(self) -> None:
        s = self.sig
        self.l_state.configure(text=self.state, bg=STATE_COLOURS.get(self.state, "#5f6b7a"))
        self.v_p.set(f"{s.pressure[-1][1]:6.1f} mmHg" if s.pressure else "-- mmHg")
        if self.rec_file is not None:
            secs = time.monotonic() - self.rec_t0
            self.v_rec.set(f"● RECORDING {int(secs // 60)}:{int(secs % 60):02d} · {self.rec_n:,} lines\n"
                           f"→ {self.rec_dir.name}")
        self._parts()
        if self.paused:
            return
        t_end = s.latest_t()
        try:
            t0 = t_end - float(self.v_win.get())
        except ValueError:
            t0 = t_end - 120.0

        def window(q):
            a = np.asarray([r for r in q if r[0] >= t0], dtype=float)
            if len(a) > 3000:
                a = a[:: len(a) // 3000]
            return a

        p = window(s.pressure)
        self.h_p.set_data(p[:, 0] if len(p) else [], p[:, 1] if len(p) else [])
        for band, line in self.h_v.items():       # each band against its own reference
            lt = window(s.light[band])
            if len(lt) and s.ref[band] is None:
                s.ref[band] = (float(lt[0, 1]), float(lt[0, 2]))
            ref = s.ref[band]
            if len(lt) and ref:
                od_ir = np.log(max(ref[0], 1e-9) / np.maximum(lt[:, 1], 1e-9))
                od_red = np.log(max(ref[1], 1e-9) / np.maximum(lt[:, 2], 1e-9))
                dv = COEFF_RED_660 * od_red + COEFF_IR_880 * od_ir
                if len(dv) >= 10:
                    dv = np.convolve(dv, np.ones(10) / 10, mode="same")   # 1 s smoothing for display
                line.set_data(lt[:, 0], dv)
            else:
                line.set_data([], [])
        detrend = self.v_detrend.get()
        self.ax_m.set_ylabel("accelerometer, counts minus mean\n(8192 = 1 g)" if detrend
                             else "accelerometer, raw counts\n(8192 = 1 g)")
        for band in ("D", "P"):
            a = window(s.accel[band])
            for k in range(3):
                y = a[:, k + 1] if len(a) else np.array([])
                if detrend and len(y):
                    y = y - y.mean()
                self.h_acc[(band, k)].set_data(a[:, 0] if len(a) else [], y)

        for m in self.marks:
            m.remove()
        self.marks = []
        for t, st in s.states:
            if t >= t0:
                for ax in (self.ax_p, self.ax_v, self.ax_m):
                    self.marks.append(ax.axvline(t, color=STATE_COLOURS.get(st, "#888"), lw=0.8, alpha=0.5))
                self.marks.append(self.ax_p.text(t, 1.0, f" {st}", transform=self.ax_p.get_xaxis_transform(),
                                                 fontsize=8, color=STATE_COLOURS.get(st, "#888"), va="top"))
        if s.onset and s.onset[0] >= t0:
            for ax in (self.ax_p, self.ax_v, self.ax_m):
                self.marks.append(ax.axvline(s.onset[0], color="#c1203b", ls="--", lw=1.2))
            self.marks.append(self.ax_p.text(s.onset[0], s.onset[1], f"  onset {s.onset[1]:.1f}",
                                             color="#c1203b", fontsize=9, fontweight="bold"))
        self.ax_m.set_xlim(t0, max(t_end, t0 + 1))
        for ax in (self.ax_p, self.ax_v, self.ax_m):
            ax.relim()
            ax.autoscale_view(scalex=False)
        self.canvas.draw_idle()

    # -- lifecycle -----------------------------------------------------------

    def on_close(self) -> None:
        if self.state in ACTIVE:
            self.link.write(["DUMP"])                 # never leave a closed cuff behind
            self.record("window closed during a run: DUMP sent")
            time.sleep(0.2)
        if self.rec_file is not None:                 # finish the recording before exiting
            self.keep(f"# recording stopped {time.strftime('%Y-%m-%d %H:%M:%S')} (window closed)")
            self.rec_file.close()
            self.rec_file = None
            self._name_recording()
            try:
                export_csv(self.rec_dir / "raw.txt")
            except Exception:
                pass                                  # raw.txt is complete; --export can redo the CSVs
        self.link.close()
        self.root.destroy()

    def run(self) -> None:
        self.link.start()
        self.root.mainloop()


# ---------------------------------------------------------------------------
# A recording, split into one CSV per data stream
# ---------------------------------------------------------------------------

# file name: (line kind, header). The columns are the firmware's (INFO columns_*).
CSV_STREAMS = {
    "ppg_distal.csv": ("S", ["idx", "t_us", "ir", "red"]),
    "pressure.csv": ("S", ["t_us", "p_idx", "p_raw", "p_status", "p_abs_mmhg", "p_cuff_mmhg"]),
    "ppg_proximal.csv": ("SP", ["idx", "t_us", "ir", "red"]),
    "imu.csv": ("M", ["m_idx", "t_us", "band", "ax", "ay", "az", "gx", "gy", "gz", "gyro_dps"]),
    "valve_control.csv": ("C", ["c_idx", "t_us", "state", "p_mmhg", "pref_mmhg", "rate_mmhg_s", "valve_open_pct"]),
    "status.csv": ("ST", ["t_us", "state", "sensors", "zeroed", "controller", "clients", "p_run_min", "hold_s",
                          "rate", "p_max", "ws_dropped_bytes", "loop_max_us", "view", "analyser"]),
    "events.csv": ("EV", ["t_us", "name", "detail"]),
    "buttons.csv": ("# gui", ["wall_time", "board_t", "what"]),
}


def export_csv(raw: Path) -> dict[str, int]:
    """Write one CSV per stream next to ``raw`` (a recording's raw.txt).
    Returns rows per file. Pressure rows are the fresh MPRLS readings only
    (each S line repeats the latest), in mmHg once the cuff zero is known;
    errors (E lines) go into events.csv as name "error"."""
    import csv
    rows = {name: [] for name in CSV_STREAMS}
    zero = None
    last_p_idx = None
    for ln in raw.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.startswith("# gui "):
            parts = ln[6:].split(" ", 3)               # wall, "board_t", value, what
            if len(parts) == 4:
                rows["buttons.csv"].append([parts[0], parts[2], parts[3]])
            continue
        if ln.startswith("#"):
            if "cuff_zero_abs_mmhg=" in ln:
                try:
                    zero = float(ln.split("cuff_zero_abs_mmhg=")[1].split()[0])
                except ValueError:
                    pass
            continue
        f = ln.split(",")
        kind = f[0]
        try:
            if kind == "S" and len(f) >= 8:
                rows["ppg_distal.csv"].append(f[1:5])
                if f[5] != last_p_idx:
                    last_p_idx = f[5]
                    absm = (int(f[6]) - OUT_MIN) * PSI_MAX / (OUT_MAX - OUT_MIN) * MMHG_PER_PSI
                    good = int(f[7]) & 0x40 and not int(f[7]) & 0x01
                    rows["pressure.csv"].append([f[2], f[5], f[6], f[7], f"{absm:.3f}" if good else "",
                                                 f"{absm - zero:.3f}" if good and zero is not None else ""])
            elif kind == "SP" and len(f) >= 5:
                rows["ppg_proximal.csv"].append(f[1:5])
            elif kind == "M" and len(f) >= 10:
                g = math.sqrt(sum(int(x) ** 2 for x in f[7:10])) / IMU_LSB_PER_DPS
                rows["imu.csv"].append(f[1:10] + [f"{g:.2f}"])
            elif kind == "C" and len(f) >= 8:
                rows["valve_control.csv"].append(f[1:8])
            elif kind == "ST" and len(f) >= 15:
                rows["status.csv"].append(f[1:15])
            elif kind == "EV" and len(f) >= 3:
                rows["events.csv"].append([f[1], f[2], ",".join(f[3:])])
            elif kind == "E":
                rows["events.csv"].append(["", "error", ln[2:]])
        except (ValueError, IndexError):
            continue                                   # a garbled line (USB noise) is skipped
    counts = {}
    for name, (_kind, header) in CSV_STREAMS.items():
        with open(raw.parent / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows[name])
        counts[name.removesuffix(".csv")] = len(rows[name])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bench GUI for the expo board over USB.")
    ap.add_argument("--port", help="serial port, e.g. COM8 (default: the only one connected)")
    ap.add_argument("--mock", action="store_true", help="the simulated board, no hardware")
    ap.add_argument("--speed", type=float, default=1.0, help="--mock only: run the simulation faster")
    ap.add_argument("--export", type=Path, metavar="RAW",
                    help="split a recording's raw.txt (or any saved lines) into CSVs, then exit")
    args = ap.parse_args(argv)
    if args.export:
        for name, n in export_csv(args.export).items():
            print(f"{name}.csv: {n} rows")
        return 0
    if args.mock:
        link, title = MockLink(args.speed), "MAPQuest bench — simulated board"
    else:
        link, title = SerialLink(args.port), f"MAPQuest bench — {args.port or 'USB'}"
    BenchApp(link, title).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
