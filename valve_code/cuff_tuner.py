"""
cuff_tuner.py - live tuning GUI for cuff_deflation_test.ino (build 4+)

Connects to the ESP32 over serial, plots each deflation run live
(pressure vs. target ramp, deflation rate, valve duty), lets you change
the PID gains with sliders (optionally while a run is in progress), and
keeps a table of runs so you can compare tunings side by side.

Requires : Python 3.9+   and   pip install pyserial matplotlib
Run      : python cuff_tuner.py
Safety   : Esc or the red STOP button dumps the cuff at any time.
           Closing the window also sends STOP.
"""

import csv
import datetime
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
import serial
import serial.tools.list_ports

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

BAUD = 115200
CSV_HEADER = "t_s,P_mmHg,Pref_mmHg,rate_mmHg_s,duty_pct"
STATUS_RE = re.compile(r"# P =\s*([-\d.]+) mmHg\s+rate =\s*([-+\d.]+)")
SUMMARY_RE = re.compile(r"mean rate ([-\d.]+).*RMS error ([-\w.]+)")


# --------------------------------------------------------------------------- serial
class SerialLink:
    """Background reader that turns the serial stream into lines on a queue."""

    def __init__(self):
        self.ser = None
        self.q = queue.Queue()
        self.alive = False

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open and self.alive

    def open(self, port):
        s = serial.Serial()
        s.port = port
        s.baudrate = BAUD
        s.timeout = 0.1
        s.dtr = False  # keep the ESP32 auto-reset lines released,
        s.rts = False  # so connecting does not reboot the board
        s.open()
        self.ser = s
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        while self.alive:
            try:
                data = self.ser.read(512)
            except (serial.SerialException, OSError, TypeError) as e:
                if self.alive:
                    self.q.put(("error", str(e)))
                self.alive = False
                break
            if data:
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.q.put(("line", line.decode(errors="replace").strip()))

    def send(self, cmd):
        if self.connected:
            self.ser.write((cmd + "\n").encode())

    def reset_board(self):
        """Pulse EN via RTS (normal boot, not download mode)."""
        if self.connected:
            self.ser.dtr = False
            self.ser.rts = True
            time.sleep(0.1)
            self.ser.rts = False

    def close(self):
        self.alive = False
        time.sleep(0.15)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None


# --------------------------------------------------------------------------- data
class Run:
    def __init__(self, idx, kind, params):
        self.idx = idx
        self.kind = kind
        self.params = dict(params)
        self.started = datetime.datetime.now()
        self.t, self.p, self.pref, self.rate, self.duty = [], [], [], [], []
        self.summary = None
        self.mean_rate = None
        self.rms = None
        self.status = "running"
        self.gains_changed = False

    def label(self):
        if self.kind == "PID":
            g = self.params
            s = f"Run {self.idx} · PID  Kp={g['kp']:.3f}  Ki={g['ki']:.3f}  Kd={g['kd']:.3f}"
            if self.gains_changed:
                s += " (changed mid-run)"
        else:
            s = f"Run {self.idx} · {self.kind}"
        if self.mean_rate is not None:
            s += f"  ·  mean {self.mean_rate:.2f} mmHg/s"
        if self.rms is not None:
            s += f"  ·  RMS {self.rms:.2f}"
        return s


# --------------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cuff Deflation Tuner")
        self.geometry("1360x860")
        self.minsize(1100, 700)

        self.link = SerialLink()
        self.runs = []
        self.current = None
        self.pending_kind = "PID"
        self.dirty = False
        self._gain_job = None
        self.port_map = {}

        self.vars = {
            "kp": tk.DoubleVar(value=0.08),
            "ki": tk.DoubleVar(value=0.04),
            "kd": tk.DoubleVar(value=0.0),
            "rate": tk.DoubleVar(value=2.0),
            "u0": tk.DoubleVar(value=4.0),
            "stop": tk.DoubleVar(value=40.0),
            "start": tk.DoubleVar(value=140.0),
            "duty": tk.DoubleVar(value=5.0),
        }
        self.live_gains = tk.BooleanVar(value=True)

        self._build_ui()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<Escape>", lambda e: self.cmd_stop())
        self.after(50, self.poll)
        self.after(200, self.redraw)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        left = ttk.Frame(self, padding=8)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self, padding=(0, 8, 8, 8))
        right.pack(side="right", fill="both", expand=True)

        # Connection
        con = ttk.LabelFrame(left, text="Connection", padding=6)
        con.pack(fill="x", pady=(0, 6))
        self.port_box = ttk.Combobox(con, width=30, state="readonly")
        self.port_box.grid(row=0, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Button(con, text="Refresh", command=self.refresh_ports).grid(row=1, column=0, sticky="ew")
        self.connect_btn = ttk.Button(con, text="Connect", command=self.toggle_connect)
        self.connect_btn.grid(row=1, column=1, sticky="ew")
        ttk.Button(con, text="Reset board", command=self.reset_board).grid(row=1, column=2, sticky="ew")
        for c in range(3):
            con.columnconfigure(c, weight=1)

        # Live readout
        st = ttk.LabelFrame(left, text="Live", padding=6)
        st.pack(fill="x", pady=(0, 6))
        self.mode_lbl = ttk.Label(st, text="Mode: —", font=("Segoe UI", 10, "bold"))
        self.mode_lbl.pack(anchor="w")
        self.p_lbl = ttk.Label(st, text="— mmHg", font=("Segoe UI", 22, "bold"))
        self.p_lbl.pack(anchor="w")
        self.rate_lbl = ttk.Label(st, text="rate — mmHg/s")
        self.rate_lbl.pack(anchor="w")

        # Actions
        act = ttk.LabelFrame(left, text="Actions", padding=6)
        act.pack(fill="x", pady=(0, 6))
        btns = [
            ("Zero (vented)", lambda: self.send("z")),
            ("Valve click test", lambda: self.send("v")),
            ("Hold (inflate)", lambda: self.send("h")),
            ("Arm (auto-start)", self.cmd_arm),
            ("Run now", self.cmd_run),
        ]
        for i, (txt, fn) in enumerate(btns):
            ttk.Button(act, text=txt, command=fn).grid(row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)
        stop = tk.Button(act, text="STOP / DUMP  (Esc)", command=self.cmd_stop, bg="#c62828", fg="white",
                         activebackground="#8e0000", activeforeground="white", font=("Segoe UI", 11, "bold"))
        stop.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        act.columnconfigure(0, weight=1)
        act.columnconfigure(1, weight=1)

        # Open loop
        ol = ttk.LabelFrame(left, text="Open loop (valve characterization)", padding=6)
        ol.pack(fill="x", pady=(0, 6))
        self._slider(ol, 0, "Duty %", "duty", 0, 50, 0.5)
        ttk.Button(ol, text="Open-loop run", command=self.cmd_open).grid(row=1, column=0, columnspan=3,
                                                                       sticky="ew", pady=(4, 0))

        # Controller
        pid = ttk.LabelFrame(left, text="Controller", padding=6)
        pid.pack(fill="x", pady=(0, 6))
        self._slider(pid, 0, "Kp", "kp", 0, 0.30, 0.002, self.schedule_gains)
        self._slider(pid, 1, "Ki", "ki", 0, 0.20, 0.002, self.schedule_gains)
        self._slider(pid, 2, "Kd", "kd", 0, 0.10, 0.001, self.schedule_gains)
        ttk.Checkbutton(pid, text="Send gains live (works mid-run)", variable=self.live_gains).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(2, 6))
        self._slider(pid, 4, "Target mmHg/s", "rate", 0.5, 5.0, 0.1)
        self._slider(pid, 5, "Start duty %", "u0", 0, 30, 0.5)
        self._slider(pid, 6, "Stop mmHg", "stop", 20, 100, 1)
        self._slider(pid, 7, "Arm above mmHg", "start", 60, 250, 1)
        ttk.Button(pid, text="Apply all to board", command=self.apply_all).grid(row=8, column=0, columnspan=3,
                                                                               sticky="ew", pady=(4, 0))
        self.device_lbl = ttk.Label(pid, text="Board: —", wraplength=300, foreground="#555")
        self.device_lbl.grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Runs table
        rf = ttk.LabelFrame(left, text="Runs (select one to overlay in grey)", padding=6)
        rf.pack(fill="both", expand=True)
        cols = ("run", "type", "kp", "ki", "kd", "mean", "rms")
        self.tree = ttk.Treeview(rf, columns=cols, show="headings", height=6, selectmode="browse")
        widths = (34, 70, 48, 48, 48, 48, 48)
        heads = ("#", "Type", "Kp", "Ki", "Kd", "Mean", "RMS")
        for c, w, h in zip(cols, widths, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.mark_dirty())
        bf = ttk.Frame(rf)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Button(bf, text="Save run CSV", command=self.save_run).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="Clear overlay", command=self.clear_selection).pack(side="left", expand=True, fill="x")

        # Plots
        self.fig = Figure(figsize=(8, 6), dpi=100)
        gs = self.fig.add_gridspec(3, 1, height_ratios=[3, 2, 1.3], hspace=0.12)
        self.ax_p = self.fig.add_subplot(gs[0])
        self.ax_r = self.fig.add_subplot(gs[1], sharex=self.ax_p)
        self.ax_d = self.fig.add_subplot(gs[2], sharex=self.ax_p)

        self.ghost_p, = self.ax_p.plot([], [], color="0.65", lw=1.4, label="overlay run")
        self.pref_line, = self.ax_p.plot([], [], "k--", lw=1.0, label="target ramp")
        self.p_line, = self.ax_p.plot([], [], color="#1565c0", lw=1.8, label="pressure")
        self.ax_p.set_ylabel("Pressure (mmHg)")
        self.ax_p.legend(loc="upper right", fontsize=8)

        self.ghost_r, = self.ax_r.plot([], [], color="0.65", lw=1.2)
        self.target_line, = self.ax_r.plot([], [], "k--", lw=1.0, label="target")
        self.r_line, = self.ax_r.plot([], [], color="#ef6c00", lw=1.6, label="measured")
        self.ax_r.set_ylabel("Deflation\n(mmHg/s)")
        self.ax_r.legend(loc="upper right", fontsize=8)

        self.ghost_d, = self.ax_d.plot([], [], color="0.65", lw=1.0, drawstyle="steps-post")
        self.d_line, = self.ax_d.plot([], [], color="#2e7d32", lw=1.4, drawstyle="steps-post")
        self.ax_d.set_ylabel("Duty (%)")
        self.ax_d.set_xlabel("Time since run start (s)")

        for ax in (self.ax_p, self.ax_r, self.ax_d):
            ax.grid(True, alpha=0.3)
        for ax in (self.ax_p, self.ax_r):
            ax.tick_params(labelbottom=False)
        self.title_txt = self.fig.suptitle("No run yet", fontsize=10)
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.94, bottom=0.08)

        plot_frame = ttk.Frame(right)
        plot_frame.pack(fill="both", expand=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        toolbar = NavigationToolbar2Tk(self.canvas, plot_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        # Console
        cf = ttk.Frame(right)
        cf.pack(fill="x", pady=(6, 0))
        self.console = tk.Text(cf, height=8, state="disabled", font=("Consolas", 9), wrap="none")
        sb = ttk.Scrollbar(cf, command=self.console.yview)
        self.console.configure(yscrollcommand=sb.set)
        self.console.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        ef = ttk.Frame(right)
        ef.pack(fill="x", pady=(4, 0))
        ttk.Label(ef, text="Command:").pack(side="left")
        self.cmd_entry = ttk.Entry(ef)
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.cmd_entry.bind("<Return>", self._send_entry)
        ttk.Button(ef, text="Send", command=self._send_entry).pack(side="left")

    def _slider(self, parent, row, label, key, lo, hi, res, on_change=None):
        var = self.vars[key]

        def moved(v):
            var.set(round(round(float(v) / res) * res, 6))
            if on_change:
                on_change()

        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Scale(parent, from_=lo, to=hi, variable=var, orient="horizontal", length=150,
                  command=moved).grid(row=row, column=1, sticky="ew", padx=4)
        e = ttk.Entry(parent, textvariable=var, width=7)
        e.grid(row=row, column=2)
        if on_change:
            e.bind("<Return>", lambda ev: on_change())
        parent.columnconfigure(1, weight=1)

    def get(self, key):
        try:
            return float(self.vars[key].get())
        except (tk.TclError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ connection
    def refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        self.port_map = {f"{p.device} — {p.description}": p.device for p in ports}
        names = list(self.port_map)
        self.port_box["values"] = names
        if names and not self.port_box.get():
            self.port_box.current(0)

    def toggle_connect(self):
        if self.link.connected:
            self.set_disconnected()
            return
        name = self.port_box.get()
        if not name:
            messagebox.showwarning("No port", "Pick a serial port first.")
            return
        try:
            self.link.open(self.port_map[name])
        except Exception as e:
            messagebox.showerror("Could not open port",
                                 f"{e}\n\nClose the Arduino Serial Monitor first - only one program can use the port.")
            return
        self.connect_btn.config(text="Disconnect")
        self.log(f"Connected to {self.port_map[name]}. The board was not reset; press 'Reset board' to restart it.")
        self.send("p")

    def set_disconnected(self):
        self.link.close()
        self.connect_btn.config(text="Connect")
        self.log("Disconnected.")

    def reset_board(self):
        if not self.link.connected:
            self.log("Not connected.")
            return
        self.log("Resetting board - it will re-zero, so keep the cuff vented.")
        self.link.reset_board()

    # ------------------------------------------------------------------ commands
    def send(self, cmd):
        if not self.link.connected:
            self.log(f"(not connected) {cmd}")
            return
        self.link.send(cmd)
        self.log(f"> {cmd}")

    def _send_entry(self, event=None):
        cmd = self.cmd_entry.get().strip()
        if cmd:
            self.send(cmd)
            self.cmd_entry.delete(0, "end")

    def schedule_gains(self):
        if not self.live_gains.get() or not self.link.connected:
            return
        if self._gain_job:
            self.after_cancel(self._gain_job)
        self._gain_job = self.after(250, self.send_gains)

    def send_gains(self):
        self._gain_job = None
        self.send(f"k{self.get('kp'):.4f},{self.get('ki'):.4f},{self.get('kd'):.4f}")
        if self.current and self.current.status == "running" and self.current.kind == "PID":
            self.current.gains_changed = True

    def apply_all(self):
        self.send(f"t{self.get('rate'):.2f}")
        self.send(f"k{self.get('kp'):.4f},{self.get('ki'):.4f},{self.get('kd'):.4f}")
        self.send(f"u{self.get('u0'):.1f}")
        self.send(f"e{self.get('stop'):.0f}")
        self.send(f"g{self.get('start'):.0f}")

    def cmd_arm(self):
        self.apply_all()
        self.pending_kind = "PID"
        self.send("a")

    def cmd_run(self):
        self.apply_all()
        self.pending_kind = "PID"
        self.send("r")

    def cmd_open(self):
        self.apply_all()
        duty = self.get("duty")
        self.pending_kind = f"open loop {duty:g}%"
        self.send(f"d{duty:g}")

    def cmd_stop(self):
        self.send("s")

    def snapshot(self):
        return {k: self.get(k) for k in ("kp", "ki", "kd", "rate", "u0", "stop")}

    # ------------------------------------------------------------------ incoming data
    def poll(self):
        try:
            while True:
                kind, payload = self.link.q.get_nowait()
                if kind == "line":
                    self.handle_line(payload)
                else:
                    self.log(f"! serial error: {payload}")
                    self.set_disconnected()
        except queue.Empty:
            pass
        self.after(50, self.poll)

    def handle_line(self, line):
        if not line:
            return
        if line == CSV_HEADER:
            self.start_run()
            return
        if line.startswith("#"):
            self.log(line)
            self.handle_status(line)
            return
        try:
            vals = [float(x) for x in line.split(",")]
        except ValueError:
            self.log(line)
            return
        if len(vals) == 5 and self.current is not None and self.current.status == "running":
            t, p, pref, rate, duty = vals
            r = self.current
            r.t.append(t)
            r.p.append(p)
            r.pref.append(pref)
            r.rate.append(-rate)  # plot deflation as a positive rate
            r.duty.append(duty)
            self.p_lbl.config(text=f"{p:.1f} mmHg")
            self.rate_lbl.config(text=f"rate {-rate:+.2f} mmHg/s deflating   duty {duty:.1f}%")
            self.mark_dirty()
        elif len(vals) == 2:  # raw stream (x command): millis,mmHg
            self.p_lbl.config(text=f"{vals[1]:.1f} mmHg")
        else:
            self.log(line)

    def handle_status(self, line):
        m = STATUS_RE.match(line)
        if m:
            self.p_lbl.config(text=f"{float(m.group(1)):.1f} mmHg")
            self.rate_lbl.config(text=f"rate {float(m.group(2)):+.2f} mmHg/s")
        if line.startswith("# mode ->"):
            self.mode_lbl.config(text="Mode: " + line[len("# mode ->"):].strip())
        if line.startswith("# rate="):
            self.device_lbl.config(text="Board: " + " ".join(line[2:].split()[:7]))
        if line.startswith("# summary:") and self.current is not None:
            r = self.current
            r.summary = line[2:]
            m = SUMMARY_RE.search(line)
            if m:
                r.mean_rate = float(m.group(1))
                try:
                    r.rms = float(m.group(2))
                except ValueError:
                    r.rms = None
            r.status = "done"
            self.update_row(r)
            self.mark_dirty()
        if line.startswith("# DUMP:") and self.current is not None and self.current.status == "running":
            reason = line[len("# DUMP:"):].strip()
            if reason != "reached stop pressure":  # that case is followed by a summary line
                self.current.status = "stopped"
                self.update_row(self.current)
                self.mark_dirty()

    def start_run(self):
        r = Run(len(self.runs) + 1, self.pending_kind, self.snapshot())
        self.pending_kind = "PID"
        self.runs.append(r)
        self.current = r
        self.tree.insert("", "end", iid=str(r.idx), values=self.row_values(r))
        self.tree.see(str(r.idx))
        self.mark_dirty()

    def row_values(self, r):
        pid = r.kind == "PID"
        star = "*" if r.gains_changed else ""
        if r.mean_rate is not None:
            mean = f"{r.mean_rate:.2f}"
        else:
            mean = r.status if r.status != "running" else "…"
        return (r.idx, "PID" + star if pid else r.kind.replace("open loop ", "OL "),
                f"{r.params['kp']:.3f}" if pid else "", f"{r.params['ki']:.3f}" if pid else "",
                f"{r.params['kd']:.3f}" if pid else "", mean,
                f"{r.rms:.2f}" if r.rms is not None else "")

    def update_row(self, r):
        if self.tree.exists(str(r.idx)):
            self.tree.item(str(r.idx), values=self.row_values(r))

    # ------------------------------------------------------------------ plotting
    def mark_dirty(self):
        self.dirty = True

    def overlay_run(self):
        sel = self.tree.selection()
        if sel:
            r = self.runs[int(sel[0]) - 1]
            return None if r is self.current else r
        if len(self.runs) >= 2:
            return self.runs[-2]
        return None

    def redraw(self):
        if self.dirty:
            self.dirty = False
            r = self.current
            g = self.overlay_run()
            if r:
                self.p_line.set_data(r.t, r.p)
                self.pref_line.set_data(r.t, r.pref)
                self.r_line.set_data(r.t, r.rate)
                self.d_line.set_data(r.t, r.duty)
                tmax = r.t[-1] if r.t else 1.0
                rate = r.params["rate"]
                self.target_line.set_data([0, max(tmax, 1.0)], [rate, rate])
                self.title_txt.set_text(r.label() + ("" if r.status != "stopped" else "  ·  stopped"))
            for line, attr in ((self.ghost_p, "p"), (self.ghost_r, "rate"), (self.ghost_d, "duty")):
                if g:
                    line.set_data(g.t, getattr(g, attr))
                else:
                    line.set_data([], [])
            self.ghost_p.set_label(f"overlay: run {g.idx}" if g else "overlay run")
            self.ax_p.legend(loc="upper right", fontsize=8)
            for ax in (self.ax_p, self.ax_r, self.ax_d):
                ax.relim()
                ax.autoscale_view()
            self.ax_d.set_ylim(bottom=0)
            self.canvas.draw_idle()
        self.after(200, self.redraw)

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())
        self.mark_dirty()

    # ------------------------------------------------------------------ files / misc
    def save_run(self):
        sel = self.tree.selection()
        r = self.runs[int(sel[0]) - 1] if sel else self.current
        if r is None or not r.t:
            messagebox.showinfo("Nothing to save", "No run data yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"cuff_run{r.idx}_{r.started:%Y%m%d_%H%M%S}.csv")
        if not path:
            return
        with open(path, "w", newline="") as f:
            f.write(f"# run {r.idx}, {r.kind}, started {r.started.isoformat(timespec='seconds')}\n")
            f.write("# params at start: " + ", ".join(f"{k}={v:g}" for k, v in r.params.items()) + "\n")
            if r.gains_changed:
                f.write("# note: gains were changed during this run\n")
            if r.summary:
                f.write(f"# {r.summary}\n")
            w = csv.writer(f)
            w.writerow(["t_s", "P_mmHg", "Pref_mmHg", "deflation_rate_mmHg_s", "duty_pct"])
            w.writerows(zip(r.t, r.p, r.pref, r.rate, r.duty))
        self.log(f"Saved run {r.idx} to {path}")

    def log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n")
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 2000:
            self.console.delete("1.0", f"{lines - 2000}.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def on_close(self):
        if self.link.connected:
            self.link.send("s")  # dump the cuff on exit
            time.sleep(0.1)
            self.link.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
