#!/usr/bin/env python3
"""MAPQuest IRB session tool (irb/DESIGN.md section 5).

    python irb/host/irb_session.py                 # pick the port in the window
    python irb/host/irb_session.py --port COM7
    python irb/host/irb_session.py --mock          # simulated board: no hardware

Keys: Esc = DUMP (always, even while typing) · O = Doppler went silent
(during inflation) · D = Doppler flow back (during deflation) · Z = zero the
blood-volume trace.

Closing the window sends DUMP if a run is active.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from irbtool import trialinfo  # noqa: E402
from irbtool.link import list_ports  # noqa: E402
from irbtool.model import BAD, OFF, OK, WARN  # noqa: E402
from irbtool.session import SessionCore  # noqa: E402

PLOT_SPAN_S = 120.0

STATE_COLORS = {"IDLE": "#5f6b7a", "INFLATE": "#b26b00", "HOLD": "#8a5a00",
                "DEFLATE": "#1f5fa8", "DUMP": "#455a64"}
STATE_TEXT = {"DUMP": "DUMP: venting"}
DISABLED_BG = "#c9ccd1"
LIGHT_COLORS = {OK: "#2e7d32", WARN: "#d08700", BAD: "#c62828", OFF: "#8a8a8a"}
LIGHT_NAMES = {"pressure": "Pressure", "ppg_D": "Distal PPG", "ppg_P": "Proximal PPG",
               "imu_D": "Distal IMU", "imu_P": "Proximal IMU"}

BEFORE_FIELDS = [   # (key, label, width or choices)
    ("subject_id", "Subject", 7), ("run_id", "Run ID", 11),
    ("distal_cm", "Distal cm", 5), ("proximal_cm", "Proximal cm", 5),
    ("height_cm", "Height cm", 5), ("weight_kg", "Weight kg", 5),
    ("arm_circumference_cm", "Arm circ. cm", 5),
    ("cuff_size", "Cuff", list(trialinfo.CUFF_SIZES)),
    ("bladder_width_cm", "Bladder cm", 5),
    ("fitzpatrick", "Fitzpatrick", list(trialinfo.FITZPATRICK)),
    ("session_systolic", "Session SBP", 5), ("session_diastolic", "DBP", 5),
]
AFTER_FIELDS = [("ref_systolic", "After-run cuff SBP", 5), ("ref_diastolic", "DBP", 5),
                ("ref_map", "MAP (if shown)", 5), ("notes", "Notes", 40)]


class App:
    def __init__(self, core: SessionCore, mock: bool) -> None:
        import tkinter as tk
        from tkinter import ttk

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        self.tk, self.ttk = tk, ttk
        self.core = core
        self.mock = mock
        self._closing = False
        self._last_draw = 0.0

        root = tk.Tk()
        self.root = root
        root.title("MAPQuest IRB session" + ("  [MOCK BOARD - SIMULATION]" if mock else ""))
        root.geometry("1560x980")
        root.configure(bg="white")

        # -- top bar: connection, run, clock --------------------------------
        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=8, pady=(6, 2))
        ttk.Label(bar, text="Port").pack(side="left")
        self.v_port = tk.StringVar()
        self.cb_port = ttk.Combobox(bar, textvariable=self.v_port, width=34, state="readonly")
        self.cb_port.pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self.refresh_ports).pack(side="left", padx=2)
        self.b_connect = ttk.Button(bar, text="Connect", command=self.on_connect)
        self.b_connect.pack(side="left", padx=2)
        ttk.Button(bar, text="Mock board", command=self.on_mock).pack(side="left", padx=(2, 16))
        self.l_run = tk.Label(bar, text="", font=("Segoe UI", 14, "bold"), bg="white")
        self.l_run.pack(side="left", padx=8)
        self.l_clock = tk.Label(bar, text="", font=("Consolas", 30, "bold"), bg="white")
        self.l_clock.pack(side="right", padx=8)
        self.l_rec = tk.Label(bar, text="", font=("Segoe UI", 18, "bold"), fg="#c62828", bg="white")
        self.l_rec.pack(side="right", padx=8)

        # -- sensor lights --------------------------------------------------
        lights = ttk.Frame(root)
        lights.pack(side="top", fill="x", padx=8, pady=2)
        self.lights = {}
        for key, name in LIGHT_NAMES.items():
            lab = tk.Label(lights, text=f"● {name}", font=("Segoe UI", 12, "bold"),
                           fg="white", bg=LIGHT_COLORS[OFF], padx=10, pady=3)
            lab.pack(side="left", padx=3)
            self.lights[key] = lab

        # -- fault banner (hidden until a fault dump) -------------------------
        self.f_fault = tk.Frame(root, bg="#c62828")
        self.l_fault = tk.Label(self.f_fault, text="", font=("Segoe UI", 18, "bold"),
                                fg="white", bg="#c62828", padx=12, pady=6)
        self.l_fault.pack(side="left")
        tk.Button(self.f_fault, text="Acknowledge", font=("Segoe UI", 12),
                  command=lambda: self.core.model.acknowledge_fault()).pack(side="right", padx=10)

        # -- middle: controls left, plots right -------------------------------
        mid = ttk.Frame(root)
        mid.pack(side="top", fill="both", expand=True, padx=8, pady=4)
        self.mid = mid
        left = tk.Frame(mid, bg="white", width=470)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.l_p = tk.Label(left, text="---", font=("Consolas", 96, "bold"), bg="white")
        self.l_p.pack(anchor="center", pady=(4, 0))
        tk.Label(left, text="mmHg cuff pressure", font=("Segoe UI", 14), bg="white").pack()
        self.l_state = tk.Label(left, text="NOT CONNECTED", font=("Segoe UI", 22, "bold"),
                                fg="white", bg=STATE_COLORS["IDLE"], pady=4)
        self.l_state.pack(fill="x", pady=6)
        self.l_target = tk.Label(left, text="", font=("Segoe UI", 16), bg="white", justify="left")
        self.l_target.pack(anchor="w", padx=6)
        self.l_rate = tk.Label(left, text="", font=("Segoe UI", 14), bg="white", justify="left")
        self.l_rate.pack(anchor="w", padx=6, pady=(0, 8))

        btns = tk.Frame(left, bg="white")
        btns.pack(fill="x", pady=4)
        self.b_start = tk.Button(btns, text="START\n(close valve, record)", font=("Segoe UI", 15, "bold"),
                                 bg="#2e7d32", fg="white", height=2, command=self.on_start)
        self.b_start.pack(side="left", fill="x", expand=True, padx=3)
        self.b_run = tk.Button(btns, text="RUN\n(hold, then deflate)", font=("Segoe UI", 15, "bold"),
                               bg="#1f5fa8", fg="white", height=2, command=self.on_run)
        self.b_run.pack(side="left", fill="x", expand=True, padx=3)
        tk.Button(left, text="DUMP  (Esc)", font=("Segoe UI", 26, "bold"), bg="#c62828",
                  fg="white", activebackground="#8e0000", height=1, pady=10,
                  command=self.on_dump).pack(fill="x", padx=3, pady=6)

        marks = tk.Frame(left, bg="white")
        marks.pack(fill="x")
        self.b_occ = ttk.Button(marks, text="O  Doppler silent", command=self.on_occlusion)
        self.b_occ.pack(side="left", fill="x", expand=True, padx=2)
        self.b_ret = ttk.Button(marks, text="D  Doppler flow back", command=self.on_return)
        self.b_ret.pack(side="left", fill="x", expand=True, padx=2)
        tools = tk.Frame(left, bg="white")
        tools.pack(fill="x", pady=4)
        self.b_prep = ttk.Button(tools, text="Prepare LEDs (dark + tune)", command=self.on_prepare)
        self.b_prep.pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(tools, text="Zero volume (Z)", command=core.zero_volume).pack(
            side="left", fill="x", expand=True, padx=2)

        if mock:
            mk = ttk.LabelFrame(left, text="Mock board (simulation only)")
            mk.pack(fill="x", pady=8, padx=2)
            self.v_pump = tk.StringVar(value="160")
            ttk.Entry(mk, textvariable=self.v_pump, width=6).pack(side="left", padx=2)
            ttk.Button(mk, text="Pump bulb", command=self.on_mock_pump).pack(side="left", padx=2)
            self.v_fail = tk.StringVar(value="ppg_P")
            ttk.Combobox(mk, textvariable=self.v_fail, width=9, state="readonly",
                         values=list(LIGHT_NAMES)).pack(side="left", padx=(10, 2))
            ttk.Button(mk, text="Fail", command=lambda: self._mock("fail")).pack(side="left")
            ttk.Button(mk, text="Heal", command=lambda: self._mock("heal")).pack(side="left")

        self.fig = Figure(figsize=(9, 6), dpi=100)
        gs = self.fig.add_gridspec(3, 1, height_ratios=[3, 2, 1.4], hspace=0.12)
        self.ax_p = self.fig.add_subplot(gs[0])
        self.ax_v = self.fig.add_subplot(gs[1], sharex=self.ax_p)
        self.ax_a = self.fig.add_subplot(gs[2], sharex=self.ax_p)
        self.ln_pref, = self.ax_p.plot([], [], "k--", lw=1, label="target ramp")
        self.ln_p, = self.ax_p.plot([], [], color="#5b3fa8", lw=1.8, label="cuff")
        self.ax_p.set_ylabel("Cuff (mmHg)")
        self.ax_p.legend(loc="upper right", fontsize=8)
        self.ln_v, = self.ax_v.plot([], [], color="#c43b2f", lw=1.5)
        self.ax_v.set_ylabel("Distal blood\nvolume (µm)")
        self.ln_aD, = self.ax_a.plot([], [], color="#1f5fa8", lw=1, label="distal")
        self.ln_aP, = self.ax_a.plot([], [], color="#888888", lw=1, label="proximal")
        self.ax_a.set_ylabel("|a| − 1 g")
        self.ax_a.set_xlabel("board time (s)")
        self.ax_a.legend(loc="upper right", fontsize=8)
        for ax in (self.ax_p, self.ax_v, self.ax_a):
            ax.grid(True, alpha=0.3)
        for ax in (self.ax_p, self.ax_v):
            ax.tick_params(labelbottom=False)
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.97, bottom=0.07)
        self.canvas = FigureCanvasTkAgg(self.fig, master=mid)
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)

        # -- trial info -------------------------------------------------------
        form = ttk.LabelFrame(root, text="Trial info (coded IDs only; written into every file of the run)")
        form.pack(side="top", fill="x", padx=8, pady=4)
        self.vars, self.widgets = {}, {}
        self._build_row(form, BEFORE_FIELDS, 0, core.trial_raw)
        self._build_row(form, AFTER_FIELDS, 2, core.after_raw)
        self.b_save = ttk.Button(form, text="Save run", command=self.on_save)
        self.b_save.grid(row=3, column=8, padx=4, pady=2, sticky="ew")
        self.b_discard = ttk.Button(form, text="Discard run", command=self.on_discard)
        self.b_discard.grid(row=3, column=9, padx=4, pady=2, sticky="ew")
        self.l_warn = ttk.Label(form, text="", foreground="#b26b00")
        self.l_warn.grid(row=4, column=0, columnspan=24, sticky="w", padx=4)

        self.l_status = ttk.Label(root, anchor="w", padding=(8, 3), font=("Segoe UI", 11))
        self.l_status.pack(side="bottom", fill="x")

        # -- keys ---------------------------------------------------------------
        def key(fn):
            return lambda e: None if e.widget.winfo_class() in ("TEntry", "Entry", "TCombobox") else fn()

        root.bind_all("<Escape>", lambda _e: self.on_dump())          # always
        root.bind("<o>", key(self.on_occlusion))
        root.bind("<d>", key(self.on_return))
        root.bind("<z>", key(core.zero_volume))
        root.bind("<Return>", lambda _e: root.focus_set())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_ports()

    # ------------------------------------------------------------------ form

    def _build_row(self, parent, fields, row, source) -> None:
        tk, ttk = self.tk, self.ttk
        col = 0
        for key, label, spec in fields:
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(6, 2))
            var = tk.StringVar(value=str(source.get(key, "")))
            if isinstance(spec, list):
                w = ttk.Combobox(parent, textvariable=var, values=spec, width=12, state="readonly")
            else:
                w = ttk.Entry(parent, textvariable=var, width=spec)
            w.grid(row=row + 1, column=col, sticky="w", padx=(6, 2), pady=(0, 4))
            self.vars[key], self.widgets[key] = var, w
            col += 1

    def _read_form(self) -> None:
        for key, _l, _s in BEFORE_FIELDS:
            self.core.trial_raw[key] = self.vars[key].get()
        for key, _l, _s in AFTER_FIELDS:
            self.core.after_raw[key] = self.vars[key].get()
        sid = self.core.trial_raw.get("subject_id", "").strip().upper()
        if trialinfo.SUBJECT_RE.match(sid) and not self.vars["run_id"].get().strip():
            self.vars["run_id"].set(trialinfo.first_run_id(sid))

    def _lock_form(self, locked: bool) -> None:
        for key, _l, spec in BEFORE_FIELDS:
            w = self.widgets[key]
            if locked:
                w.state(["disabled"])
            else:
                w.state(["!disabled", "readonly"] if isinstance(spec, list) else ["!disabled"])

    # --------------------------------------------------------------- actions

    def _show(self, result) -> None:
        ok, msg = result
        self.core.status = msg if ok else f"⚠ {msg}"

    def refresh_ports(self) -> None:
        ports = list_ports()
        self.port_map = {f"{d} — {desc}": d for d, desc in ports}
        self.cb_port["values"] = list(self.port_map)
        if ports and not self.v_port.get():
            self.cb_port.current(0)

    def on_connect(self) -> None:
        name = self.v_port.get()
        if not name:
            self.core.status = "pick a port first (Refresh), or use Mock board"
            return
        self._show(self.core.connect(self.port_map[name]))

    def on_mock(self) -> None:
        self.core.connect_mock(realtime=True, threaded=True)
        self.mock = True
        self.root.title("MAPQuest IRB session  [MOCK BOARD - SIMULATION]")

    def on_start(self) -> None:
        self._read_form()
        self._show(self.core.start())

    def on_run(self) -> None:
        self._show(self.core.run())

    def on_dump(self) -> None:
        self._show(self.core.dump())

    def on_occlusion(self) -> None:
        self._show(self.core.mark_occlusion())

    def on_return(self) -> None:
        self._show(self.core.mark_return())

    def on_prepare(self) -> None:
        self._show(self.core.prepare_leds())

    def on_save(self) -> None:
        self._read_form()
        ok, msg = self.core.save_run()
        self._show((ok, msg))
        if ok:
            self.vars["run_id"].set(self.core.trial_raw["run_id"])
            for key, _l, _s in AFTER_FIELDS:
                self.vars[key].set("")

    def on_discard(self) -> None:
        from tkinter import messagebox
        rec = self.core.recorder
        if rec is None:
            return
        if messagebox.askyesno("Discard run", f"Delete run {rec.run_id} and all its files?\n"
                                              "This cannot be undone."):
            self._show(self.core.discard_run())

    def on_mock_pump(self) -> None:
        if self.core.board is not None:
            try:
                self.core.board.pump_to(float(self.v_pump.get()))
            except ValueError:
                pass

    def _mock(self, what: str) -> None:
        if self.core.board is not None:
            getattr(self.core.board, what)(self.v_fail.get())

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        if self._closing:
            return
        try:
            self._read_form()
            self.core.tick()
            self._update()
            now = time.monotonic()
            if now - self._last_draw > 0.25:
                self._last_draw = now
                self._draw()
        except Exception as exc:                    # keep the window alive; show what broke
            self.core.status = f"display error: {exc}"
        self.root.after(50, self._loop)

    def _update(self) -> None:
        c, m = self.core, self.core.model
        s = c.session_s()
        self.l_clock.config(text=f"t = {s:8.1f} s")
        connected = c.connected
        p = m.p_mmhg
        self.l_p.config(text="---" if (not connected or math.isnan(p)) else f"{p:5.0f}")
        state = m.state if connected else "NOT CONNECTED"
        self.l_state.config(text=STATE_TEXT.get(state, state), bg=STATE_COLORS.get(m.state, "#5f6b7a"))

        t = c.targets
        if m.state == "INFLATE" and t:
            ready = not math.isnan(p) and p >= t.run_min
            self.l_target.config(text=f"Inflate to ~{t.target:.0f}  ·  RUN at ≥ {t.run_min:.0f}\n"
                                      f"(from {t.basis}{', clamped' if t.clamped else ''})",
                                 fg="#2e7d32" if ready else "black")
            self.l_p.config(fg="#2e7d32" if ready else "black")
        else:
            self.l_target.config(text=f"Hold {m.info.setting('hold_s'):.0f} s · stop at "
                                      f"{m.info.setting('p_stop'):.0f} mmHg" if connected else "",
                                 fg="black")
            self.l_p.config(fg="black")
        rate = -m.rate_mmhg_s if not math.isnan(m.rate_mmhg_s) else float("nan")
        self.l_rate.config(text=(f"Deflating {rate:4.1f} mmHg/s (target {m.info.setting('rate'):.1f})"
                                 if m.state == "DEFLATE" else ""))

        for key, (level, text) in m.lights(time.monotonic()).items():
            lab = self.lights[key]
            lab.config(bg=LIGHT_COLORS[level] if connected else LIGHT_COLORS[OFF],
                       text=f"● {LIGHT_NAMES[key]}" + ("" if level == OK else f": {text}"))

        if m.fault:
            self.l_fault.config(text=f"AUTOMATIC DUMP: {m.fault}")
            if not self.f_fault.winfo_ismapped():
                self.f_fault.pack(side="top", fill="x", padx=8, before=self.mid)
        elif self.f_fault.winfo_ismapped():
            self.f_fault.pack_forget()

        rec = c.recorder
        recording = rec is not None and rec.status == "recording"
        unsaved = rec is not None and rec.status == "unsaved"
        self.l_rec.config(text="● REC" if recording else "UNSAVED" if unsaved else "")
        self.l_run.config(text=f"{rec.run_id}" if rec else c.trial_raw.get("run_id", ""))
        self._lock_form(rec is not None)

        can_start, why = m.can_start(time.monotonic())
        start_ok = connected and rec is None and can_start
        run_ok = m.state == "INFLATE"
        # Disabled buttons go grey: a saturated colour reads as "press me".
        self.b_start.config(state="normal" if start_ok else "disabled",
                            bg="#2e7d32" if start_ok else DISABLED_BG)
        self.b_run.config(state="normal" if run_ok else "disabled",
                          bg="#1f5fa8" if run_ok else DISABLED_BG)
        self.b_occ.state(["!disabled"] if m.state == "INFLATE" else ["disabled"])
        self.b_ret.state(["!disabled"] if recording else ["disabled"])
        self.b_prep.state(["!disabled"] if (connected and m.state == "IDLE" and rec is None)
                          else ["disabled"])
        self.b_save.state(["!disabled"] if unsaved else ["disabled"])
        self.b_discard.state(["!disabled"] if unsaved else ["disabled"])

        chk = c.check_trial()
        notes = chk.warnings + ([] if chk.ok else chk.errors)
        if connected and rec is None and not can_start and m.state == "IDLE":
            notes.append(f"Start blocked: {why}")
        self.l_warn.config(text="  ·  ".join(notes))
        self.l_status.config(text=c.status)

    def _draw(self) -> None:
        c = self.core
        if c.p_hist:
            t, p, pref = zip(*c.p_hist)
            self.ln_p.set_data(t, p)
            self.ln_pref.set_data(t, pref)
            t1 = t[-1]
            self.ax_p.set_xlim(t1 - PLOT_SPAN_S, t1 + 2)
            finite = [v for v in p if not math.isnan(v)]
            if finite:
                self.ax_p.set_ylim(-5, max(60.0, max(finite) * 1.1))
        if c.vol is not None and c._vol_t0 is not None:
            a = c.vol.arrays()
            if len(a["t"]):
                tv = a["t"] + c._vol_t0
                self.ln_v.set_data(tv, a["dv"])
                fin = a["dv"][~(a["dv"] != a["dv"])]
                if len(fin):
                    lo, hi = float(fin.min()), float(fin.max())
                    pad = max((hi - lo) * 0.1, 2.0)
                    self.ax_v.set_ylim(lo - pad, hi + pad)
        for band, ln in (("D", self.ln_aD), ("P", self.ln_aP)):
            h = c.imu_hist[band]
            if h:
                ta, av = zip(*h)
                ln.set_data(ta, av)
        self.ax_a.set_ylim(-0.3, 0.3)
        self.canvas.draw_idle()

    def run(self) -> None:
        self.root.after(100, self._loop)
        self.root.mainloop()

    def on_close(self) -> None:
        from tkinter import messagebox
        rec = self.core.recorder
        if rec is not None and rec.status == "unsaved":
            if not messagebox.askyesno("Unsaved run", f"Run {rec.run_id} is not saved. Its files "
                                       "stay on disk with trial_info=pending. Close anyway?"):
                return
        self._closing = True
        if self.core.model.active:
            self.core.dump()
        self.core.close()
        self.root.quit()
        self.root.destroy()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. COM7")
    ap.add_argument("--mock", action="store_true", help="use the simulated board (no hardware)")
    ap.add_argument("--data", type=Path, help="where runs go (default: irb_data/ at the repo root)")
    args = ap.parse_args()

    core = SessionCore(**({"data_root": args.data} if args.data else {}))
    app = App(core, mock=args.mock)
    if args.mock:
        core.connect_mock(realtime=True, threaded=True)
    elif args.port:
        app._show(core.connect(args.port))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
