#!/usr/bin/env python3
"""MAPQuest PPG heart-rate monitor.

A standalone live heart-rate display for the MAX30101. Independent of the
return-to-flow tooling: it only needs the board streaming.

    python hr_monitor.py                         # auto-detect the board
    python hr_monitor.py --auto-tune --site finger
    python hr_monitor.py --replay ../data/bench_20261009_160125.csv

Placement matters more than anything here. A **fingertip** gives a pulse
roughly 50x stronger than the wrist, and reads within seconds. The wrist works
-- the 2026-10-09 wrist capture tracks at 79 bpm -- but its pulse is ~0.01% of
DC, so expect "confirming" and "searching" more often, and keep very still.
Use --auto-tune whenever you move the sensor to a new site: a fingertip will
saturate at wrist LED currents.

The window shows:
  * Heart rate, coloured by signal quality. A number appears only when beat
    timing and the spectrum independently agree for 3 s; otherwise the
    reason is shown instead.
  * The band-passed, inverted PPG with each detected beat marked (grey x =
    rejected interval).
  * Heart-rate trend, and the live spectrum with both estimates marked.
  * Perfusion, pulse SNR, HRV (RMSSD), DC level, LED currents, link status.

Buttons: Pause, Auto-tune (re-sets IR LED current for the current site),
Record (raw samples + a per-second heart-rate log into data/).

Signal processing lives in mapquest/heartrate.py; see its docstring and D33.
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

from mapquest.heartrate import QUALITY_COLORS, HeartRateEstimator  # noqa: E402
from mapquest.io import Device, LinkLost, LinkStalled, find_port  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FULL_SCALE = (1 << 18) - 1
DC_TARGET = 131_000
DC_WINDOW = (105_000, 157_000)


# ---------------------------------------------------------------------------
# Sample sources. Each runs in its own thread and posts messages to a queue:
#   ("meta", dict)  ("samples", ndarray rows x columns)  ("status", str)
#   ("reset", None) ("eof", None)
# ---------------------------------------------------------------------------

class SerialSource(threading.Thread):
    """Streams from the board, reconnecting if the link drops."""

    kind = "serial"

    def __init__(self, out: queue.Queue, port: str | None = None,
                 baud: int = 115200, ir_amp: int | None = None,
                 red_amp: int | None = None, auto_tune: bool = False,
                 stall_timeout: float = 3.0) -> None:
        super().__init__(daemon=True)
        self.out = out
        self.port_arg = port
        self.baud = baud
        self.ir_amp = ir_amp
        self.red_amp = red_amp
        self.tune_on_connect = auto_tune
        self.stall_timeout = stall_timeout
        self.cmds: queue.Queue = queue.Queue()
        self._quit = threading.Event()
        self.paused = False

    # -- control, from the GUI thread --------------------------------------

    def command(self, cmd: str) -> None:
        self.cmds.put(cmd)

    def stop(self) -> None:
        self._quit.set()
        self.cmds.put("quit")

    # -- thread ---------------------------------------------------------------

    def _status(self, text: str) -> None:
        self.out.put(("status", text))

    def _poll(self) -> str | None:
        try:
            return self.cmds.get_nowait()
        except queue.Empty:
            return None

    def run(self) -> None:
        while not self._quit.is_set():
            try:
                port = find_port(self.port_arg)
                self._status(f"connecting to {port}…")
                with Device(port, baud=self.baud, settle=1.5) as dev:
                    self._session(dev, port)
            except (LinkLost, LinkStalled) as exc:
                self._status(f"link lost ({str(exc).splitlines()[0][:60]}) — reconnecting")
            except Exception as exc:  # find_port, sensor missing, etc.
                self._status(f"{str(exc).splitlines()[0][:70]} — retrying")
            if not self._quit.is_set():
                self.out.put(("reset", None))
                self._quit.wait(2.0)

    def _send_meta(self, dev: Device, port: str) -> None:
        raw = dict(dev.meta.raw)
        self.out.put(("meta", {
            "fs": dev.meta.fs,
            "columns": list(dev.meta.columns),
            "source": port,
            "ir_amp_ma": raw.get("ir_amp_ma", "?"),
            "red_amp_ma": raw.get("red_amp_ma", "?"),
            "raw": raw,
        }))

    def _session(self, dev: Device, port: str) -> None:
        meta = dev.info()
        if meta.sensor_ok is False:
            raise RuntimeError("sensor not found (sensor_ok=0) — check wiring, send R")
        for ch, val in (("ir", self.ir_amp), ("red", self.red_amp)):
            if val is not None:
                dev.set_amp(ch, val)
        if self.tune_on_connect:
            self._tune(dev)
            self.tune_on_connect = False
        self._send_meta(dev, port)

        while not self._quit.is_set():
            if self.paused:
                cmd = None
                try:
                    cmd = self.cmds.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._handle(cmd, dev, port)
                continue

            self._status(f"streaming from {port}")
            cmd = None
            block: list[list[int]] = []
            last = time.time()
            gen = dev.stream(stall_timeout=self.stall_timeout)
            try:
                for fields, _line in gen:
                    block.append(fields)
                    now = time.time()
                    if now - last >= 0.04:
                        self.out.put(("samples", np.asarray(block, dtype=float)))
                        block, last = [], now
                        cmd = self._poll()
                        if cmd:
                            break
            finally:
                gen.close()
            if block:
                self.out.put(("samples", np.asarray(block, dtype=float)))
            dev.stop()
            self._handle(cmd, dev, port)

    def _handle(self, cmd: str | None, dev: Device, port: str) -> None:
        if cmd == "pause":
            self.paused = True
            self._status("paused")
        elif cmd == "resume":
            self.paused = False
        elif cmd == "tune":
            self._tune(dev)
            self.out.put(("reset", None))
            self._send_meta(dev, port)
        # "quit" falls through; the loop checks the quit flag.

    def _measure_ir(self, dev: Device, seconds: float = 0.6) -> float:
        i_ir = dev.meta.columns.index("ir")
        vals: list[int] = []
        t0 = time.time()
        gen = dev.stream(stall_timeout=self.stall_timeout)
        try:
            for fields, _line in gen:
                vals.append(fields[i_ir])
                if time.time() - t0 >= seconds:
                    break
        finally:
            gen.close()
        dev.stop()
        return float(np.mean(vals[5:] if len(vals) > 10 else vals))

    def _tune(self, dev: Device) -> None:
        """Bring IR DC to mid-scale for the current site.

        Same proportional approach as bench_check.auto_tune: DC above the dark
        floor scales roughly with LED current. Halve on saturation, where the
        proportional step is meaningless.
        """
        amp = int(float(dev.meta.raw.get("ir_amp", 31)))
        dark = dev.meta.dark.get("ir", 0.0)
        for _ in range(6):
            dc = self._measure_ir(dev)
            self._status(f"auto-tune: IR amp {amp} ({amp * 0.2:.1f} mA) → DC {dc:,.0f}")
            if DC_WINDOW[0] <= dc <= DC_WINDOW[1]:
                break
            if dc >= 0.98 * FULL_SCALE:
                new = max(1, amp // 2)
            else:
                new = int(round(amp * (DC_TARGET - dark) / max(dc - dark, 1.0)))
                new = max(1, min(255, new))
            if new == amp:
                break
            amp = new
            dev.set_amp("ir", amp)
            time.sleep(0.2)
        self._status(f"auto-tune done: IR amp {amp} ({amp * 0.2:.1f} mA)")


class ReplaySource(threading.Thread):
    """Plays a saved capture through the monitor at real time × speed."""

    kind = "replay"

    def __init__(self, out: queue.Queue, path: str | Path, speed: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.out = out
        self.path = Path(path)
        self.speed = max(speed, 0.01)
        self._quit = threading.Event()
        self.paused = False
        self.cmds: queue.Queue = queue.Queue()

    def command(self, cmd: str) -> None:
        if cmd == "pause":
            self.paused = True
            self.out.put(("status", "paused"))
        elif cmd == "resume":
            self.paused = False

    def stop(self) -> None:
        self._quit.set()

    @staticmethod
    def load(path: Path) -> tuple[dict, list[str], np.ndarray]:
        meta: dict[str, str] = {}
        cols: list[str] | None = None
        rows: list[list[float]] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
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
            if len(parts) != len(cols):
                continue
            rows.append(parts)
        if not cols or not rows:
            raise ValueError(f"no samples in {path}")
        keep = [c for c in ("ir", "red") if c in cols]
        if "ir" not in keep:
            raise ValueError(f"{path} has no ir column")
        arr = np.array([[float(r[cols.index(c)]) for c in keep] for r in rows])
        return meta, keep, arr

    def run(self) -> None:
        try:
            meta, cols, arr = self.load(self.path)
        except Exception as exc:
            self.out.put(("status", f"replay failed: {exc}"))
            self.out.put(("eof", None))
            return
        fs = float(meta.get("fs", 100.0))
        self.out.put(("meta", {
            "fs": fs, "columns": cols, "source": f"replay: {self.path.name}",
            "ir_amp_ma": meta.get("ir_amp_ma", "?"),
            "red_amp_ma": meta.get("red_amp_ma", "?"), "raw": meta,
        }))
        self.out.put(("status", f"replaying {self.path.name} at {self.speed:g}×"))
        block = max(int(fs * 0.04), 1)
        t0 = time.time()
        paused_total = 0.0
        for i in range(0, len(arr), block):
            if self._quit.is_set():
                return
            if self.paused:
                p0 = time.time()
                while self.paused and not self._quit.is_set():
                    time.sleep(0.05)
                paused_total += time.time() - p0
            due = t0 + paused_total + (i / fs) / self.speed
            delay = due - time.time()
            if delay > 0:
                time.sleep(delay)
            self.out.put(("samples", arr[i:i + block]))
        self.out.put(("status", f"replay finished ({len(arr) / fs:.0f} s)"))
        self.out.put(("eof", None))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class HRMonitorApp:
    def __init__(self, source, channel: str = "ir", site: str = "finger",
                 window_s: float = 8.0, show: bool = True) -> None:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
        from matplotlib.widgets import Button

        self.plt = plt
        self.source = source
        self.q: queue.Queue = source.out
        self.channel = channel
        self.site = site
        self.window_s = window_s
        self.est: HeartRateEstimator | None = None
        self.meta: dict = {}
        self.status = "starting…"
        self.eof = False
        self.last_good: float | None = None
        self.paused = False
        self._ylim: tuple[float, float] | None = None
        self._rec = None
        self._hist_written = 0

        fig = plt.figure(figsize=(13, 8.2))
        self.fig = fig
        try:
            fig.canvas.manager.set_window_title("MAPQuest — PPG heart rate")
        except Exception:
            pass
        gs = fig.add_gridspec(3, 3, height_ratios=[0.8, 1.6, 1.35],
                              hspace=0.45, wspace=0.28,
                              left=0.06, right=0.98, top=0.95, bottom=0.12)

        # -- heart rate --------------------------------------------------
        ax = fig.add_subplot(gs[0, 0])
        ax.axis("off")
        self.t_hr = ax.text(0.02, 0.55, "--", fontsize=64, fontweight="bold",
                            va="center", ha="left", color="#6b7280",
                            transform=ax.transAxes)
        self.t_unit = ax.text(0.62, 0.42, "bpm", fontsize=18, va="center",
                              color="#6b7280", transform=ax.transAxes)
        self.t_quality = ax.text(0.02, 0.05, "", fontsize=11, fontweight="bold",
                                 va="bottom", transform=ax.transAxes)

        # -- details ---------------------------------------------------------
        ax = fig.add_subplot(gs[0, 1:])
        ax.axis("off")
        self.t_info = ax.text(0.0, 0.98, "", fontsize=9.5, family="monospace",
                              va="top", ha="left", transform=ax.transAxes)

        # -- waveform --------------------------------------------------------
        ax = fig.add_subplot(gs[1, :])
        self.ax_wave = ax
        (self.l_wave,) = ax.plot([], [], lw=1.2, color="#1f4e79")
        (self.l_beats,) = ax.plot([], [], "o", ms=6, color="#0a7d2c",
                                  label="beat")
        (self.l_rej,) = ax.plot([], [], "x", ms=7, mew=2, color="#9ca3af",
                                label="rejected interval")
        ax.set_xlim(-window_s, 0)
        ax.set_xlabel("time (s, 0 = now)", fontsize=9)
        ax.set_ylabel("PPG, band-passed 0.5–4 Hz\n[counts, inverted]", fontsize=9)
        ax.axhline(0, color="k", lw=0.5, alpha=0.4)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper left", fontsize=8, ncol=2)
        ax.tick_params(labelsize=8)

        # -- trend -----------------------------------------------------------
        ax = fig.add_subplot(gs[2, 0:2])
        self.ax_trend = ax
        (self.l_trend,) = ax.plot([], [], lw=1.8, color="#0a7d2c",
                                  label="heart rate (shown)")
        (self.l_trend_spec,) = ax.plot([], [], ".", ms=3, color="#d08700",
                                       alpha=0.6, label="spectral estimate")
        ax.set_xlabel("elapsed (s)", fontsize=9)
        ax.set_ylabel("bpm", fontsize=9)
        ax.set_ylim(40, 140)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper left", fontsize=8, ncol=2)
        ax.tick_params(labelsize=8)

        # -- spectrum --------------------------------------------------------
        ax = fig.add_subplot(gs[2, 2])
        self.ax_spec = ax
        (self.l_spec,) = ax.plot([], [], lw=1.2, color="#1f4e79")
        self.v_beat = ax.axvline(np.nan, color="#0a7d2c", lw=1.5, label="beats")
        self.v_spec = ax.axvline(np.nan, color="#d08700", lw=1.2, ls="--",
                                 label="spectral")
        ax.set_xlim(30, 200)
        ax.set_ylim(0, 1.05)
        ax.set_xlabel("heart rate (bpm)", fontsize=9)
        ax.set_ylabel("power, last 12 s\n[normalised]", fontsize=9)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper right", fontsize=8)
        ax.tick_params(labelsize=8)

        # -- buttons ---------------------------------------------------------
        self.b_pause = Button(fig.add_axes([0.06, 0.02, 0.10, 0.045]), "Pause")
        self.b_tune = Button(fig.add_axes([0.17, 0.02, 0.12, 0.045]), "Auto-tune LED")
        self.b_rec = Button(fig.add_axes([0.30, 0.02, 0.10, 0.045]), "Record")
        self.b_pause.on_clicked(self._on_pause)
        self.b_tune.on_clicked(self._on_tune)
        self.b_rec.on_clicked(self._on_record)
        if getattr(source, "kind", "") != "serial":
            self.b_tune.label.set_color("#9ca3af")

        fig.canvas.mpl_connect("close_event", self._on_close)
        self.anim = FuncAnimation(fig, self.tick, interval=50,
                                  cache_frame_data=False) if show else None

    # -- controls ------------------------------------------------------------

    def _on_pause(self, _evt=None) -> None:
        self.paused = not self.paused
        self.source.command("pause" if self.paused else "resume")
        self.b_pause.label.set_text("Resume" if self.paused else "Pause")

    def _on_tune(self, _evt=None) -> None:
        if getattr(self.source, "kind", "") != "serial":
            self.status = "auto-tune needs the board (not available in replay)"
            return
        self.source.command("tune")

    def _on_record(self, _evt=None) -> None:
        if self._rec is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _on_close(self, _evt=None) -> None:
        self.source.stop()
        self._stop_recording()

    # -- recording -------------------------------------------------------

    def _data_columns(self) -> list[str]:
        """Optical channels only. The recorder writes its own continuous idx."""
        return [c for c in self.meta.get("columns", []) if c not in ("idx", "t_us")]

    def _start_recording(self) -> None:
        if not self.meta:
            self.status = "nothing to record yet"
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DATA_DIR / f"hr_{self.site}_{stamp}"
        raw_fh = base.with_suffix(".csv").open("w")
        raw_fh.write("# source=hr_monitor\n")
        raw_fh.write(f"# recorded_at={datetime.now().isoformat(timespec='seconds')}\n")
        raw_fh.write(f"# site={self.site}\n# hr_channel={self.channel}\n")
        for k, v in self.meta.get("raw", {}).items():
            raw_fh.write(f"# {k}={v}\n")
        raw_fh.write(f"# fs={self.meta['fs']:.2f}\n")
        raw_fh.write("idx," + ",".join(self._data_columns()) + "\n")
        hr_fh = Path(str(base) + "_hr.csv").open("w")
        hr_fh.write("t_s,hr_shown,source,quality,hr_beats,hr_spectral,"
                    "spectral_conc,perfusion_pct,pulse_snr,rmssd_ms,reason\n")
        self._rec = {"raw": raw_fh, "hr": hr_fh, "idx": 0, "path": base,
                     "t0": self.est.t_now if self.est else 0.0}
        self._hist_written = len(self.est.history) if self.est else 0
        self.b_rec.label.set_text("Stop rec")
        self.status = f"recording to {base.name}.csv"

    def _stop_recording(self) -> None:
        if self._rec is None:
            return
        for k in ("raw", "hr"):
            try:
                self._rec[k].close()
            except Exception:
                pass
        self.status = f"saved {self._rec['path'].name}.csv and _hr.csv"
        self._rec = None
        try:
            self.b_rec.label.set_text("Record")
        except Exception:
            pass

    def _record_samples(self, block: np.ndarray) -> None:
        if self._rec is None:
            return
        fh = self._rec["raw"]
        i0 = self._rec["idx"]
        for j, row in enumerate(block):
            fh.write(f"{i0 + j}," + ",".join(f"{v:.0f}" for v in row) + "\n")
        self._rec["idx"] = i0 + len(block)

    def _record_history(self) -> None:
        if self._rec is None or self.est is None:
            return
        fh = self._rec["hr"]
        for h in self.est.history[self._hist_written:]:
            def f(v, fmt="{:.2f}"):
                return "" if v is None else fmt.format(v)
            fh.write(",".join([
                f"{h.t:.0f}", f(h.hr_display, "{:.1f}"), h.hr_source or "",
                h.quality, f(h.hr_beats, "{:.1f}"), f(h.hr_spectral, "{:.1f}"),
                f(h.spectral_conc, "{:.2f}"), f"{h.perfusion_pct:.4f}",
                f"{h.pulse_snr:.1f}", f(h.rmssd_ms, "{:.0f}"),
                '"' + h.reason.replace('"', "'") + '"',
            ]) + "\n")
        fh.flush()
        self._hist_written = len(self.est.history)

    # -- update loop ---------------------------------------------------------

    def _drain(self, max_msgs: int = 400) -> None:
        for _ in range(max_msgs):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "meta":
                fs_changed = (not self.meta) or self.meta.get("fs") != payload["fs"]
                self.meta = payload
                if self.est is None or fs_changed:
                    self.est = HeartRateEstimator(payload["fs"])
            elif kind == "samples":
                if self.est is None or not self.meta:
                    continue
                cols = self.meta["columns"]
                ch = self.channel if self.channel in cols else "ir"
                self.est.push(payload[:, cols.index(ch)])
                if self._rec is not None:
                    keep = [cols.index(c) for c in self._data_columns()]
                    self._record_samples(payload[:, keep])
            elif kind == "status":
                self.status = payload
            elif kind == "reset":
                if self.est is not None:
                    self.est.reset()
                self._ylim = None
            elif kind == "eof":
                self.eof = True

    def tick(self, _frame=None):
        self._drain()
        est = self.est
        if est is None:
            self.t_info.set_text(f"Source      {self.status}")
            return ()
        st = est.state()
        self._record_history()

        # Heart rate: shown only when corroborated; during motion the last
        # good value is held, greyed, so the display does not flicker.
        color = QUALITY_COLORS.get(st.quality, "#6b7280")
        if st.hr_display is not None:
            self.t_hr.set_text(f"{st.hr_display:.0f}")
            self.t_hr.set_color(color)
            if st.quality == "GOOD":
                self.last_good = st.hr_display
        elif st.quality == "MOTION" and self.last_good is not None:
            self.t_hr.set_text(f"{self.last_good:.0f}")
            self.t_hr.set_color("#c0c4cc")
        else:
            self.t_hr.set_text("--")
            self.t_hr.set_color("#6b7280")
        self.t_unit.set_color(self.t_hr.get_color())
        label = st.quality.replace("_", " ")
        if st.hr_source == "spectrum":
            label += " · spectral only"
        self.t_quality.set_text(f"{label}\n{st.reason}")
        self.t_quality.set_color(color)

        def fmt(v, spec="{:.1f}", unit=""):
            return "--" if v is None else spec.format(v) + unit

        rec = "off" if self._rec is None else f"ON → {self._rec['path'].name}"
        self.t_info.set_text("\n".join([
            f"Beat timing {fmt(st.hr_beats, unit=' bpm'):<12}  "
            f"Spectrum  {fmt(st.hr_spectral, unit=' bpm')}"
            f"  (concentration {fmt(st.spectral_conc, '{:.2f}')})",
            f"Perfusion   {st.perfusion_pct:.3f} %{'':<6}  "
            f"Pulse SNR {st.pulse_snr:.1f}  (needs ≥ 3)",
            f"HRV RMSSD   {fmt(st.rmssd_ms, '{:.0f}', ' ms'):<12}  "
            f"Beats/10s {st.beats_10s}"
            + (f"   IBI CV {st.ibi_cv:.2f}" if st.ibi_cv is not None else ""),
            f"DC level    {st.dc_frac * 100:.0f} % of full scale"
            + ("   ← low, raise LED" if 0.05 <= st.dc_frac < 0.25 else "")
            + ("   ← near full scale" if st.dc_frac > 0.85 else ""),
            f"LED         IR {self.meta.get('ir_amp_ma', '?')} mA · "
            f"red {self.meta.get('red_amp_ma', '?')} mA   "
            f"channel {self.channel.upper()}   site {self.site}",
            f"Source      {self.meta.get('source', '?')} · "
            f"{self.meta.get('fs', 0):g} Hz · {self.status}",
            f"Recording   {rec}",
        ]))

        # Waveform
        times = est.buffer_times()
        if len(times) > 2:
            filt = np.fromiter(est.filt, dtype=float, count=len(times))
            t_now = est.t_now
            m = times >= t_now - self.window_s
            x, y = times[m] - t_now, filt[m]
            self.l_wave.set_data(x, y)
            if len(y) > 10:
                lo, hi = np.percentile(y, [1, 99])
                pad = (hi - lo) * 0.25 or 1.0
                target = (lo - pad, hi + pad)
                self._ylim = target if self._ylim is None else tuple(
                    0.85 * a + 0.15 * b for a, b in zip(self._ylim, target))
                self.ax_wave.set_ylim(*self._ylim)
            good = [bt for bt, ok in est.beats if ok and bt >= t_now - self.window_s]
            bad = [bt for bt, ok in est.beats if not ok and bt >= t_now - self.window_s]
            for line, ts in ((self.l_beats, good), (self.l_rej, bad)):
                ts_arr = np.array(ts, dtype=float)
                line.set_data(ts_arr - t_now, np.interp(ts_arr, times, filt)
                              if len(ts_arr) else ts_arr)

        # Trend
        hist = est.history
        if hist:
            ts = np.array([h.t for h in hist])
            shown = np.array([h.hr_display if h.hr_display else np.nan for h in hist])
            spec = np.array([h.hr_spectral if h.hr_spectral else np.nan for h in hist])
            self.l_trend.set_data(ts, shown)
            self.l_trend_spec.set_data(ts, spec)
            t_end = max(ts[-1], 60.0)
            self.ax_trend.set_xlim(max(0.0, t_end - 300.0), t_end)
            vis = shown[ts >= t_end - 300.0]
            if np.isfinite(vis).any():
                lo = min(40.0, np.nanmin(vis) - 10)
                hi = max(140.0, np.nanmax(vis) + 10)
                self.ax_trend.set_ylim(lo, hi)

        # Spectrum
        if est.spectrum is not None:
            freqs, pxx = est.spectrum
            bpm = freqs * 60.0
            m = (bpm >= 30) & (bpm <= 200)
            p = pxx[m]
            if p.size and p.max() > 0:
                self.l_spec.set_data(bpm[m], p / p.max())
        self.v_beat.set_xdata([st.hr_beats if st.hr_beats else np.nan] * 2)
        self.v_spec.set_xdata([st.hr_spectral if st.hr_spectral else np.nan] * 2)
        return ()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--site", default="finger",
                    choices=("finger", "wrist", "forearm", "other"),
                    help="where the sensor is; recorded with any capture")
    ap.add_argument("--channel", default="ir", choices=("ir", "red"),
                    help="channel used for heart rate (IR is stronger)")
    ap.add_argument("--ir-amp", type=int, metavar="N",
                    help="IR LED current register, 0-255 (×0.2 mA)")
    ap.add_argument("--red-amp", type=int, metavar="N",
                    help="red LED current register, 0-255")
    ap.add_argument("--auto-tune", action="store_true",
                    help="tune IR LED current for this site on connect")
    ap.add_argument("--window", type=float, default=8.0,
                    help="seconds of waveform shown")
    ap.add_argument("--replay", type=Path, metavar="CSV",
                    help="play a saved capture instead of using the board")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="replay speed multiplier")
    args = ap.parse_args()

    q: queue.Queue = queue.Queue()
    if args.replay:
        source = ReplaySource(q, args.replay, speed=args.speed)
    else:
        source = SerialSource(q, port=args.port, baud=args.baud,
                              ir_amp=args.ir_amp, red_amp=args.red_amp,
                              auto_tune=args.auto_tune)
    app = HRMonitorApp(source, channel=args.channel, site=args.site,
                       window_s=args.window)
    source.start()
    app.plt.show()
    source.stop()
    app._stop_recording()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
