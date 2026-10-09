#!/usr/bin/env python3
"""MAPQuest PPG monitor — live heart rate and blood volume, in two tabs.

Standalone from the return-to-flow tooling: it only needs the board streaming.

    python hr_monitor.py                          # auto-detect the board
    python hr_monitor.py --auto-tune --site finger
    python hr_monitor.py --replay ../data/bench_20261009_160125.csv

**Heart rate tab** (D33). Heart rate coloured by signal quality, shown only when
beat timing and the spectrum independently agree for 3 s; the band-passed PPG
with each beat marked; HR trend; live spectrum; perfusion, pulse SNR, RMSSD.

**Blood volume tab** (D34). Two-wavelength and IR-only blood volume over the
last few minutes (relative µm, + = more blood), each channel's percent change,
the rate of change against its noise floor, and a FILLING / DRAINING / STABLE
readout using the same mu0 + 3 sigma0, held-for-2-s rule as the offline
onset detector. Motion is shown as a red strip under the trace; zeros, marks
and re-tunes as labelled lines.

**One LED tuning serves both tabs**: both want DC mid-scale -- heart rate for
SNR, blood volume for headroom as counts move either way. Auto-tune sets IR and
red. Re-tuning changes counts with no change in blood, so it breaks the volume
trace (marked) and re-zeroes; it is refused while recording.

Placement: a fingertip pulse is ~50x the wrist's. The wrist works but expect
heart-rate "confirming" more often. Run --auto-tune whenever you move sites.

Buttons / keys: Pause (space) · Auto-tune LEDs (T) · Record (R) · Zero volume
(Z) · Mark event (M). Record writes raw samples with events inline, a
per-second heart-rate log, and a 10 Hz blood-volume log into data/.
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
from mapquest.volume import VolumeTracker  # noqa: E402
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
                self.out.put(("reset", "link reset"))
                self._quit.wait(2.0)

    def _send_meta(self, dev: Device, port: str) -> None:
        raw = dict(dev.meta.raw)
        self.out.put(("meta", {
            "fs": dev.meta.fs,
            "columns": list(dev.meta.columns),
            "source": port,
            "ir_amp_ma": raw.get("ir_amp_ma", "?"),
            "red_amp_ma": raw.get("red_amp_ma", "?"),
            "dark": dict(dev.meta.dark),
            "raw": raw,
        }))

    def _session(self, dev: Device, port: str) -> None:
        meta = dev.info()
        if meta.sensor_ok is False:
            raise RuntimeError("sensor not found (sensor_ok=0) — check wiring, send R")
        for ch, val in (("ir", self.ir_amp), ("red", self.red_amp)):
            if val is not None:
                dev.set_amp(ch, val)
        # Dark offsets for delta-OD (SPEC 5.1 step 3). Heart rate does not
        # need them; blood volume does. LEDs are off for ~2 s.
        self._status("measuring dark offsets (LEDs off, ~2 s)…")
        dev.measure_dark()
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
            self.out.put(("reset", "LED re-tuned"))
            self._send_meta(dev, port)
        # "quit" falls through; the loop checks the quit flag.

    def _measure(self, dev: Device, channel: str, seconds: float = 0.6) -> float:
        i_ch = dev.meta.columns.index(channel)
        vals: list[int] = []
        t0 = time.time()
        gen = dev.stream(stall_timeout=self.stall_timeout)
        try:
            for fields, _line in gen:
                vals.append(fields[i_ch])
                if time.time() - t0 >= seconds:
                    break
        finally:
            gen.close()
        dev.stop()
        return float(np.mean(vals[5:] if len(vals) > 10 else vals))

    def _tune(self, dev: Device) -> None:
        """Bring IR *and* red DC to mid-scale for the current site.

        One tuning serves both tabs: heart rate wants DC mid-scale for SNR,
        blood volume wants it for headroom, since filling lowers counts and
        draining raises them. Heart rate only reads IR, but the two-wavelength
        volume needs red tuned too.

        Same proportional approach as bench_check.auto_tune: DC above the dark
        floor scales roughly with LED current. Halve on saturation, where the
        proportional step is meaningless.
        """
        parts = []
        for channel in ("ir", "red"):
            if channel not in dev.meta.columns:
                continue
            amp = int(float(dev.meta.raw.get(f"{channel}_amp", 31)))
            dark = dev.meta.dark.get(channel, 0.0)
            for _ in range(6):
                dc = self._measure(dev, channel)
                self._status(f"auto-tune {channel.upper()}: amp {amp} "
                             f"({amp * 0.2:.1f} mA) → DC {dc:,.0f}")
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
                dev.set_amp(channel, amp)
                time.sleep(0.2)
            parts.append(f"{channel.upper()} {amp * 0.2:.1f} mA")
        self._status("auto-tune done: " + ", ".join(parts))


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
            "dark": {c: float(meta[f"dark_{c}"]) for c in ("ir", "red")
                     if f"dark_{c}" in meta},
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
# Views. Each draws into a matplotlib Figure and knows nothing about Tk, so the
# same code renders in the window and headless in the tests.
# ---------------------------------------------------------------------------

def _fmt(v, spec="{:.1f}", unit=""):
    return "--" if v is None else spec.format(v) + unit


def _smooth_ylim(old, lo, hi, pad_frac=0.15, min_span=1e-9):
    span = max(hi - lo, min_span)
    target = (lo - span * pad_frac, hi + span * pad_frac)
    if old is None:
        return target
    return tuple(0.85 * a + 0.15 * b for a, b in zip(old, target))


class HRView:
    """Heart-rate tab."""

    def __init__(self, fig, window_s: float = 8.0) -> None:
        self.fig = fig
        self.window_s = window_s
        self._ylim = None
        gs = fig.add_gridspec(3, 3, height_ratios=[0.8, 1.6, 1.35],
                              hspace=0.45, wspace=0.28,
                              left=0.06, right=0.98, top=0.96, bottom=0.07)

        ax = fig.add_subplot(gs[0, 0])
        ax.axis("off")
        self.t_hr = ax.text(0.02, 0.58, "--", fontsize=64, fontweight="bold",
                            va="center", ha="left", color="#6b7280",
                            transform=ax.transAxes)
        self.t_unit = ax.text(0.62, 0.45, "bpm", fontsize=18, va="center",
                              color="#6b7280", transform=ax.transAxes)
        self.t_quality = ax.text(0.02, 0.0, "", fontsize=11, fontweight="bold",
                                 va="bottom", transform=ax.transAxes)

        ax = fig.add_subplot(gs[0, 1:])
        ax.axis("off")
        self.t_info = ax.text(0.0, 0.98, "", fontsize=9.5, family="monospace",
                              va="top", ha="left", transform=ax.transAxes)

        ax = fig.add_subplot(gs[1, :])
        self.ax_wave = ax
        (self.l_wave,) = ax.plot([], [], lw=1.2, color="#1f4e79")
        (self.l_beats,) = ax.plot([], [], "o", ms=6, color="#0a7d2c", label="beat")
        (self.l_rej,) = ax.plot([], [], "x", ms=7, mew=2, color="#9ca3af",
                                label="rejected interval")
        ax.set_xlim(-window_s, 0)
        ax.set_xlabel("time (s, 0 = now)", fontsize=9)
        ax.set_ylabel("PPG, band-passed 0.5–4 Hz\n[counts, inverted]", fontsize=9)
        ax.axhline(0, color="k", lw=0.5, alpha=0.4)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper left", fontsize=8, ncol=2)
        ax.tick_params(labelsize=8)

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

    def waiting(self, status: str) -> None:
        self.t_info.set_text(f"Source      {status}")

    def update(self, core: "MonitorCore", st) -> None:
        est = core.est
        color = QUALITY_COLORS.get(st.quality, "#6b7280")
        # Shown only when corroborated; during motion the last good value is
        # held, greyed, so the display does not flicker.
        if st.hr_display is not None:
            self.t_hr.set_text(f"{st.hr_display:.0f}")
            self.t_hr.set_color(color)
        elif st.quality == "MOTION" and core.last_good is not None:
            self.t_hr.set_text(f"{core.last_good:.0f}")
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

        meta = core.meta
        self.t_info.set_text("\n".join([
            f"Beat timing {_fmt(st.hr_beats, unit=' bpm'):<12}  "
            f"Spectrum  {_fmt(st.hr_spectral, unit=' bpm')}"
            f"  (concentration {_fmt(st.spectral_conc, '{:.2f}')})",
            f"Perfusion   {st.perfusion_pct:.3f} %{'':<6}  "
            f"Pulse SNR {st.pulse_snr:.1f}  (needs ≥ 3)",
            f"HRV RMSSD   {_fmt(st.rmssd_ms, '{:.0f}', ' ms'):<12}  "
            f"Beats/10s {st.beats_10s}"
            + (f"   IBI CV {st.ibi_cv:.2f}" if st.ibi_cv is not None else ""),
            f"DC level    {st.dc_frac * 100:.0f} % of full scale"
            + ("   ← low, raise LED" if 0.05 <= st.dc_frac < 0.25 else "")
            + ("   ← near full scale" if st.dc_frac > 0.85 else ""),
            f"LED         IR {meta.get('ir_amp_ma', '?')} mA · "
            f"red {meta.get('red_amp_ma', '?')} mA   "
            f"channel {core.channel.upper()}   site {core.site}",
            f"Source      {meta.get('source', '?')} · {meta.get('fs', 0):g} Hz",
            f"Recording   {core.recording_label()}",
        ]))

        times = est.buffer_times()
        if len(times) > 2:
            filt = np.fromiter(est.filt, dtype=float, count=len(times))
            t_now = est.t_now
            m = times >= t_now - self.window_s
            x, y = times[m] - t_now, filt[m]
            self.l_wave.set_data(x, y)
            if len(y) > 10:
                lo, hi = np.percentile(y, [1, 99])
                self._ylim = _smooth_ylim(self._ylim, lo, hi, 0.25, 1.0)
                self.ax_wave.set_ylim(*self._ylim)
            good = [bt for bt, ok in est.beats if ok and bt >= t_now - self.window_s]
            bad = [bt for bt, ok in est.beats if not ok and bt >= t_now - self.window_s]
            for line, ts in ((self.l_beats, good), (self.l_rej, bad)):
                ts_arr = np.array(ts, dtype=float)
                line.set_data(ts_arr - t_now, np.interp(ts_arr, times, filt)
                              if len(ts_arr) else ts_arr)

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
                self.ax_trend.set_ylim(min(40.0, np.nanmin(vis) - 10),
                                       max(140.0, np.nanmax(vis) + 10))

        if est.spectrum is not None:
            freqs, pxx = est.spectrum
            bpm = freqs * 60.0
            m = (bpm >= 30) & (bpm <= 200)
            p = pxx[m]
            if p.size and p.max() > 0:
                self.l_spec.set_data(bpm[m], p / p.max())
        self.v_beat.set_xdata([st.hr_beats if st.hr_beats else np.nan] * 2)
        self.v_spec.set_xdata([st.hr_spectral if st.hr_spectral else np.nan] * 2)


class VolumeView:
    """Blood-volume tab (D34)."""

    TREND_COLORS = {
        "FILLING": "#1f6feb", "DRAINING": "#d08700", "STABLE": "#0a7d2c",
        "MOTION": "#6b7280", "SETTLING": "#6b7280", "NO BASELINE": "#6b7280",
    }
    EVENT_COLORS = {"zero": "#0a7d2c", "mark": "#7c3aed"}

    def __init__(self, fig, span_s: float = 300.0) -> None:
        self.fig = fig
        self.span_s = span_s
        self._ylims: dict[str, tuple | None] = {"dv": None, "pct": None, "rate": None}
        self._n_events = 0
        gs = fig.add_gridspec(3, 2, height_ratios=[0.8, 1.7, 1.35],
                              hspace=0.45, wspace=0.22,
                              left=0.07, right=0.98, top=0.96, bottom=0.07)

        ax = fig.add_subplot(gs[0, 0])
        ax.axis("off")
        self.t_dv = ax.text(0.02, 0.58, "--", fontsize=46, fontweight="bold",
                            va="center", ha="left", color="#6b7280",
                            transform=ax.transAxes)
        self.t_dv_unit = ax.text(0.66, 0.48, "µm", fontsize=16, va="center",
                                 color="#6b7280", transform=ax.transAxes)
        self.t_trend = ax.text(0.02, 0.0, "", fontsize=11, fontweight="bold",
                               va="bottom", transform=ax.transAxes)

        ax = fig.add_subplot(gs[0, 1])
        ax.axis("off")
        self.t_info = ax.text(0.0, 0.98, "", fontsize=9, family="monospace",
                              va="top", ha="left", transform=ax.transAxes)

        ax = fig.add_subplot(gs[1, :])
        self.ax_dv = ax
        (self.l_dv,) = ax.plot([], [], lw=1.8, color="#0b5d3b",
                               label="two-wavelength (660 + 880 nm)")
        (self.l_dv_ir,) = ax.plot([], [], lw=1.0, color="#d08700", alpha=0.85,
                                  label="IR-only")
        (self.l_motion,) = ax.plot([], [], lw=6, color="#c43b2f", alpha=0.55,
                                   solid_capstyle="butt", label="motion",
                                   transform=ax.get_xaxis_transform())
        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_ylabel("blood volume ΔV·L\n[relative µm, + = more blood]", fontsize=9)
        ax.set_xlabel("elapsed (s)", fontsize=9)
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), fontsize=8,
                  ncol=3, frameon=False)
        ax.tick_params(labelsize=8)

        ax = fig.add_subplot(gs[2, 0])
        self.ax_pct = ax
        (self.l_pct_ir,) = ax.plot([], [], lw=1.1, color="#1f4e79", label="IR")
        (self.l_pct_red,) = ax.plot([], [], lw=1.1, color="#c43b2f", label="red")
        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_ylabel("change from baseline [%]\n(fewer counts = more blood)",
                      fontsize=9)
        ax.set_xlabel("elapsed (s)", fontsize=9)
        ax.set_title("red wandering more than IR = contact sensitivity (O4)",
                     fontsize=8, loc="left", color="#555")
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper left", fontsize=8, ncol=2)
        ax.tick_params(labelsize=8)

        ax = fig.add_subplot(gs[2, 1])
        self.ax_rate = ax
        (self.l_rate,) = ax.plot([], [], lw=1.1, color="#0b5d3b")
        self.h_hi = ax.axhline(np.nan, color="#1f6feb", lw=1.0, ls="--",
                               label="μ0 ± 3σ0")
        self.h_lo = ax.axhline(np.nan, color="#1f6feb", lw=1.0, ls="--")
        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_ylabel("rate d(ΔV·L)/dt [µm/s]", fontsize=9)
        ax.set_xlabel("elapsed (s)", fontsize=9)
        ax.set_title("sustained beyond the band for 2 s = filling / draining",
                     fontsize=8, loc="left", color="#555")
        ax.grid(alpha=0.25, lw=0.5)
        ax.legend(loc="upper left", fontsize=8)
        ax.tick_params(labelsize=8)

    def waiting(self, status: str) -> None:
        self.t_info.set_text(f"Source  {status}")

    def _draw_new_events(self, vol) -> None:
        for t, label in vol.events[self._n_events:]:
            kind = ("zero" if label.startswith("zero") else
                    "mark" if label.startswith("mark") else "interrupt")
            color = self.EVENT_COLORS.get(kind, "#c43b2f")
            for ax in (self.ax_dv, self.ax_pct, self.ax_rate):
                ax.axvline(t, color=color, lw=1.0, alpha=0.7,
                           ls="-" if kind != "zero" else ":")
            self.ax_dv.text(t, 0.97, f" {label}", rotation=90, fontsize=7,
                            color=color, va="top", ha="right",
                            transform=self.ax_dv.get_xaxis_transform())
        self._n_events = len(vol.events)

    def update(self, core: "MonitorCore") -> None:
        vol = core.vol
        if vol is None:
            return
        s = vol.state()
        color = self.TREND_COLORS.get(s.trend, "#6b7280")
        self.t_dv.set_text("--" if s.dv_um is None else f"{s.dv_um:+.1f}")
        self.t_dv.set_color(color if s.dv_um is not None else "#6b7280")
        self.t_dv_unit.set_color(self.t_dv.get_color())
        self.t_trend.set_text(f"{s.trend}\n{s.reason}")
        self.t_trend.set_color(color)

        thr = s.threshold
        lo_thr = None if s.mu0 is None else s.mu0 - 3 * s.sigma0
        meta = core.meta
        base = ("not set" if s.baseline_age_s is None else
                f"zeroed {s.baseline_age_s:.0f} s ago "
                f"({'auto' if s.baseline_auto else 'manual'})")
        self.t_info.set_text("\n".join([
            f"Two-wavelength {_fmt(s.dv_um, '{:+.1f}', ' µm'):<11} "
            f"IR-only {_fmt(s.dv_ir_um, '{:+.1f}', ' µm')}",
            f"Rate           {_fmt(s.rate_um_s, '{:+.2f}', ' µm/s'):<11} "
            f"band {_fmt(lo_thr, '{:+.2f}')} … {_fmt(thr, '{:+.2f}')} µm/s",
            f"Per channel    IR {_fmt(s.pct_ir, '{:+.2f}', ' %'):<9} "
            f"red {_fmt(s.pct_red, '{:+.2f}', ' %')}"
            + (f"   red wanders {s.red_ir_ratio:.1f}× IR" if s.red_ir_ratio else ""),
            f"Baseline       {base}",
            f"Drift          {_fmt(s.drift_um_min, '{:+.2f}', ' µm/min')} "
            "over the last 60 s",
            f"LED            IR {meta.get('ir_amp_ma', '?')} mA · red "
            f"{meta.get('red_amp_ma', '?')} mA"
            + ("   (locked: recording)" if core.recording else ""),
            f"Recording      {core.recording_label()}",
            "Keys           Z zero · M mark · space pause · R record",
        ]))

        self._draw_new_events(vol)
        a = vol.arrays()
        if a["t"].size == 0:
            return
        t_end = max(vol.t_now, 30.0)
        t0 = max(0.0, t_end - self.span_s)
        vis = a["t"] >= t0
        self.l_dv.set_data(a["t"], a["dv"])
        self.l_dv_ir.set_data(a["t"], a["dv_ir"])
        self.l_motion.set_data(a["t"], np.where(a["motion"], 0.03, np.nan))
        self.l_pct_ir.set_data(a["t"], a["pct_ir"])
        self.l_pct_red.set_data(a["t"], a["pct_red"])
        self.l_rate.set_data(a["t"], a["rate"])
        self.h_hi.set_ydata([thr if thr is not None else np.nan] * 2)
        self.h_lo.set_ydata([lo_thr if lo_thr is not None else np.nan] * 2)

        for ax in (self.ax_dv, self.ax_pct, self.ax_rate):
            ax.set_xlim(t0, t_end)
        for key, ax, series, extra in (
            ("dv", self.ax_dv, (a["dv"], a["dv_ir"]), ()),
            ("pct", self.ax_pct, (a["pct_ir"], a["pct_red"]), ()),
            ("rate", self.ax_rate, (a["rate"],),
             tuple(v for v in (thr, lo_thr) if v is not None)),
        ):
            # Scale to the trustworthy data: motion spans are flagged rather
            # than allowed to set the axis, and the rate uses robust limits so
            # one slip spike cannot flatten the noise band into invisibility.
            keep = vis & ~a["motion"]
            vals = np.concatenate([x[keep] for x in series])
            vals = vals[np.isfinite(vals)]
            if vals.size:
                floor = {"dv": 2.0, "pct": 0.2, "rate": 0.5}[key]
                if key == "rate":
                    lo, hi = (float(v) for v in np.percentile(vals, [2, 98]))
                else:
                    lo, hi = float(np.min(vals)), float(np.max(vals))
                if extra:
                    lo, hi = min(lo, *extra), max(hi, *extra)
                mid = (lo + hi) / 2
                if hi - lo < floor:
                    lo, hi = mid - floor / 2, mid + floor / 2
                self._ylims[key] = _smooth_ylim(self._ylims[key], lo, hi)
                ax.set_ylim(*self._ylims[key])


# ---------------------------------------------------------------------------
# Core: everything except the window. Drains the source queue into both
# estimators, holds the controls, writes recordings, drives the views.
# ---------------------------------------------------------------------------

class MonitorCore:
    def __init__(self, source, channel: str = "ir", site: str = "finger",
                 window_s: float = 8.0, span_s: float = 300.0,
                 hr_fig=None, vol_fig=None) -> None:
        from matplotlib.figure import Figure

        self.source = source
        self.q: queue.Queue = source.out
        self.channel = channel
        self.site = site
        self.hr_fig = hr_fig or Figure(figsize=(13, 8.2))
        self.vol_fig = vol_fig or Figure(figsize=(13, 8.2))
        self.hr_view = HRView(self.hr_fig, window_s)
        self.vol_view = VolumeView(self.vol_fig, span_s)
        self.est: HeartRateEstimator | None = None
        self.vol: VolumeTracker | None = None
        self.meta: dict = {}
        self.status = "starting…"
        self.eof = False
        self.last_good: float | None = None
        self.paused = False
        self._rec: dict | None = None

    # Convenience for tests and callers that only care about the readout.
    @property
    def t_hr(self):
        return self.hr_view.t_hr

    @property
    def recording(self) -> bool:
        return self._rec is not None

    def recording_label(self) -> str:
        return "off" if self._rec is None else f"ON → {self._rec['path'].name}"

    # -- controls ----------------------------------------------------------

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.source.command("pause" if self.paused else "resume")

    def tune(self) -> bool:
        if getattr(self.source, "kind", "") != "serial":
            self.status = "auto-tune needs the board (not available in replay)"
            return False
        if self.recording:
            # Re-tuning mid-run changes counts with no change in blood, so
            # delta-OD would jump (SPEC 9). Stop the recording first.
            self.status = "LED locked while recording — stop recording to re-tune"
            return False
        self.source.command("tune")
        self.status = "auto-tuning IR and red…"
        return True

    def zero(self) -> None:
        if self.vol is None:
            return
        self.status = ("blood-volume baseline zeroed" if self.vol.zero()
                       else "need 3 s of data since the last reset to zero")

    def mark(self) -> None:
        if self.vol is not None:
            self.status = f"{self.vol.mark()} at {self.vol.t_now:.1f} s"

    def toggle_record(self) -> None:
        if self._rec is None:
            self.start_recording()
        else:
            self.stop_recording()

    # -- recording -------------------------------------------------------

    def _data_columns(self) -> list[str]:
        """Optical channels only. The recorder writes its own continuous idx."""
        return [c for c in self.meta.get("columns", []) if c not in ("idx", "t_us")]

    def start_recording(self) -> None:
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
        for k, v in self.meta.get("dark", {}).items():
            raw_fh.write(f"# dark_{k}={v:.0f}\n")
        raw_fh.write(f"# fs={self.meta['fs']:.2f}\n")
        raw_fh.write("idx," + ",".join(self._data_columns()) + "\n")
        hr_fh = Path(str(base) + "_hr.csv").open("w")
        hr_fh.write("t_s,hr_shown,source,quality,hr_beats,hr_spectral,"
                    "spectral_conc,perfusion_pct,pulse_snr,rmssd_ms,reason\n")
        vol_fh = Path(str(base) + "_vol.csv").open("w")
        vol_fh.write("t_s,dv_um,dv_ir_um,pct_ir,pct_red,rate_um_s,motion\n")
        self._rec = {
            "raw": raw_fh, "hr": hr_fh, "vol": vol_fh, "idx": 0, "path": base,
            "hist": len(self.est.history) if self.est else 0,
            "vol_t": self.vol.t_now if self.vol else 0.0,
            "events": len(self.vol.events) if self.vol else 0,
        }
        self.status = f"recording to {base.name}.csv (+ _hr, _vol)"

    def stop_recording(self) -> None:
        if self._rec is None:
            return
        for k in ("raw", "hr", "vol"):
            try:
                self._rec[k].close()
            except Exception:
                pass
        self.status = f"saved {self._rec['path'].name}.csv, _hr.csv, _vol.csv"
        self._rec = None

    def _record_samples(self, block: np.ndarray) -> None:
        fh = self._rec["raw"]
        i0 = self._rec["idx"]
        for j, row in enumerate(block):
            fh.write(f"{i0 + j}," + ",".join(f"{v:.0f}" for v in row) + "\n")
        self._rec["idx"] = i0 + len(block)

    def _record_logs(self) -> None:
        rec = self._rec
        if rec is None:
            return

        def f(v, fmt="{:.2f}"):
            return "" if v is None or (isinstance(v, float) and not np.isfinite(v)) \
                else fmt.format(v)

        if self.est is not None:
            for h in self.est.history[rec["hist"]:]:
                rec["hr"].write(",".join([
                    f"{h.t:.0f}", f(h.hr_display, "{:.1f}"), h.hr_source or "",
                    h.quality, f(h.hr_beats, "{:.1f}"), f(h.hr_spectral, "{:.1f}"),
                    f(h.spectral_conc), f"{h.perfusion_pct:.4f}",
                    f"{h.pulse_snr:.1f}", f(h.rmssd_ms, "{:.0f}"),
                    '"' + h.reason.replace('"', "'") + '"',
                ]) + "\n")
            rec["hist"] = len(self.est.history)
            rec["hr"].flush()
        if self.vol is not None:
            a = self.vol.arrays()
            new = a["t"] > rec["vol_t"]
            for i in np.flatnonzero(new):
                rec["vol"].write(",".join([
                    f"{a['t'][i]:.2f}", f(a["dv"][i]), f(a["dv_ir"][i]),
                    f(a["pct_ir"][i], "{:.4f}"), f(a["pct_red"][i], "{:.4f}"),
                    f(a["rate"][i], "{:.3f}"), str(int(a["motion"][i])),
                ]) + "\n")
            if new.any():
                rec["vol_t"] = float(a["t"][new][-1])
            rec["vol"].flush()
            # Events go inline in the raw capture as comments, so a replay or
            # re-analysis sees exactly where zeros, marks and re-tunes fell.
            for t, label in self.vol.events[rec["events"]:]:
                rec["raw"].write(f"# event t_s={t:.2f} label={label.replace(' ', '_')}\n")
            rec["events"] = len(self.vol.events)

    # -- update loop -------------------------------------------------------

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
                if self.vol is None or fs_changed:
                    self.vol = VolumeTracker(payload["fs"], dark=payload.get("dark"))
                else:
                    self.vol.dark = dict(payload.get("dark") or self.vol.dark)
            elif kind == "samples":
                if self.est is None or not self.meta:
                    continue
                cols = self.meta["columns"]
                ch = self.channel if self.channel in cols else "ir"
                self.est.push(payload[:, cols.index(ch)])
                red = payload[:, cols.index("red")] if "red" in cols else None
                self.vol.push(payload[:, cols.index("ir")], red)
                if self._rec is not None:
                    self._record_samples(payload[:, [cols.index(c)
                                                     for c in self._data_columns()]])
            elif kind == "status":
                self.status = payload
            elif kind == "reset":
                if self.est is not None:
                    self.est.reset()
                if self.vol is not None:
                    self.vol.interrupt(payload or "reset")
            elif kind == "eof":
                self.eof = True

    def tick(self, views=("hr", "vol")) -> None:
        self._drain()
        if self.est is None:
            self.hr_view.waiting(self.status)
            self.vol_view.waiting(self.status)
            return
        st = self.est.state()
        if st.quality == "GOOD" and st.hr_display is not None:
            self.last_good = st.hr_display
        if "hr" in views:
            self.hr_view.update(self, st)
        if "vol" in views:
            self.vol_view.update(self)
        self._record_logs()


class TkShell:
    """The window: a toolbar, two tabs, a status line."""

    def __init__(self, core: MonitorCore) -> None:
        import tkinter as tk
        from tkinter import ttk

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.core = core
        self._closing = False
        root = tk.Tk()
        self.root = root
        root.title("MAPQuest — PPG monitor")
        root.geometry("1320x900")

        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        self.b_pause = ttk.Button(bar, text="Pause (space)", command=self.on_pause)
        self.b_tune = ttk.Button(bar, text="Auto-tune LEDs (T)", command=core.tune)
        self.b_rec = ttk.Button(bar, text="Record (R)", command=core.toggle_record)
        self.b_zero = ttk.Button(bar, text="Zero volume (Z)", command=core.zero)
        self.b_mark = ttk.Button(bar, text="Mark event (M)", command=core.mark)
        for b in (self.b_pause, self.b_tune, self.b_rec, self.b_zero, self.b_mark):
            b.pack(side="left", padx=3)

        self.nb = ttk.Notebook(root)
        self.nb.pack(side="top", fill="both", expand=True)
        f_hr, f_vol = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.nb.add(f_hr, text="  Heart rate  ")
        self.nb.add(f_vol, text="  Blood volume  ")
        self.c_hr = FigureCanvasTkAgg(core.hr_fig, master=f_hr)
        self.c_hr.get_tk_widget().pack(fill="both", expand=True)
        self.c_vol = FigureCanvasTkAgg(core.vol_fig, master=f_vol)
        self.c_vol.get_tk_widget().pack(fill="both", expand=True)

        self.l_status = ttk.Label(root, anchor="w", padding=(8, 2))
        self.l_status.pack(side="bottom", fill="x")

        root.bind("<space>", lambda _e: self.on_pause())
        root.bind("<t>", lambda _e: core.tune())
        root.bind("<r>", lambda _e: core.toggle_record())
        root.bind("<z>", lambda _e: core.zero())
        root.bind("<m>", lambda _e: core.mark())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_pause(self) -> None:
        self.core.toggle_pause()

    def active_view(self) -> str:
        return "hr" if self.nb.index(self.nb.select()) == 0 else "vol"

    def _loop(self) -> None:
        if self._closing:
            return
        view = self.active_view()
        try:
            # Both estimators always ingest data; only the visible tab redraws.
            self.core.tick(views=(view,))
            (self.c_hr if view == "hr" else self.c_vol).draw_idle()
        except Exception as exc:  # keep the window alive; show what broke
            self.core.status = f"display error: {exc}"
        self.b_pause.config(text="Resume (space)" if self.core.paused else "Pause (space)")
        self.b_rec.config(text="Stop rec (R)" if self.core.recording else "Record (R)")
        can_tune = (getattr(self.core.source, "kind", "") == "serial"
                    and not self.core.recording)
        self.b_tune.state(["!disabled"] if can_tune else ["disabled"])
        self.l_status.config(text=self.core.status)
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
                    help="tune IR and red LED current for this site on connect")
    ap.add_argument("--window", type=float, default=8.0,
                    help="seconds of heart-rate waveform shown")
    ap.add_argument("--span", type=float, default=300.0,
                    help="seconds of blood-volume history shown")
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
    core = MonitorCore(source, channel=args.channel, site=args.site,
                       window_s=args.window, span_s=args.span)
    source.start()
    TkShell(core).run()
    source.stop()
    core.stop_recording()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
