"""The session controller: everything the GUI does, without the GUI.

``irb_session.py`` draws this; the tests drive it against the mock board.
One ``tick()`` drains the link, feeds the model, the recorder and the live
traces, and runs the timed parts (the recording tail after a dump, LED
preparation). Every operator action is a method returning ``(ok, message)``.
"""

from __future__ import annotations

import json
import math
import queue
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from . import trialinfo
from .link import Link, LinkItem, SerialTransport
from .mock import MockBoard
from .model import DeviceModel
from .protocol import Control, Distal, Error, Event, Imu, Meta, Proximal, TimeUnwrapper
from .recorder import DATA_ROOT, RunRecorder, SessionLog

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host"))
from mapquest.volume import VolumeTracker  # noqa: E402  (read-only reuse, D34)

TAIL_S = 10.0                    # keep recording this long after a dump (DESIGN 2)
START_CONFIRM_S = 2.0            # START with no INFLATE by then: treat as refused
HISTORY_S = 300.0                # live traces kept
FULL_SCALE = 262143
TUNE_LO, TUNE_HI, TUNE_MID = 0.40, 0.60, 0.50   # IR / red DC as a fraction of full scale (SPEC 3.1)
TUNE_STEPS = 8
LSB_PER_G = 8192.0

# Before-start fields kept between runs (the after-run reference is not).
PERSISTED = ("subject_id", "run_id", "distal_cm", "proximal_cm", "height_cm", "weight_kg",
             "arm_circumference_cm", "cuff_size", "bladder_width_cm", "fitzpatrick",
             "session_systolic", "session_diastolic")


class SessionCore:
    def __init__(self, data_root: Path = DATA_ROOT, clock=time.monotonic) -> None:
        self.data_root = Path(data_root)
        self.clock = clock
        self.t_start = clock()
        self.q: queue.Queue[LinkItem] = queue.Queue()
        self.link: Link | None = None
        self.board: MockBoard | None = None      # set in mock mode
        self.threaded = True
        self.model = DeviceModel()
        self.status = "not connected"
        self.trial_raw: dict = self._load_trial()
        self.after_raw: dict = {}
        self.recorder: RunRecorder | None = None
        self.log: SessionLog | None = None
        self._log_buffer: list[tuple[str, str, datetime]] = []
        self.targets: trialinfo.Targets | None = None
        self.p_occ: float | None = None
        self.p_return: float | None = None
        self._tail_until: float | None = None
        self._start_sent_at: float | None = None
        self._prep = None                         # LED preparation coroutine
        self.vol: VolumeTracker | None = None
        self._vol_t0: float | None = None         # board seconds of the tracker's first sample
        self._vol_buf: list[tuple[int, int]] = []
        self._unwrap = TimeUnwrapper()
        self.p_hist: deque = deque(maxlen=int(HISTORY_S * 10))       # (t_s, p, pref)
        self.imu_hist = {b: deque(maxlen=int(HISTORY_S * 50)) for b in "DP"}  # (t_s, |a|-1g)
        self.dc = {b: deque(maxlen=50) for b in "DP"}                  # recent (ir, red)

    # ------------------------------------------------------------------ time

    def session_s(self, host_s: float | None = None) -> float:
        return (self.clock() if host_s is None else host_s) - self.t_start

    def board_s(self, t_us: int) -> float:
        return self._unwrap(t_us) / 1e6

    # ------------------------------------------------------------ connection

    def connect(self, port: str) -> tuple[bool, str]:
        try:
            transport = SerialTransport(port)
        except Exception as exc:                  # noqa: BLE001
            return False, f"could not open {port}: {exc}"
        self._attach(transport, threaded=True)
        self._logline("link", f"connected to {port}")
        return True, f"connected to {port}"

    def connect_mock(self, *, realtime: bool = True, threaded: bool = True, **kw) -> MockBoard:
        self.board = MockBoard(realtime=realtime, **kw)
        self._attach(self.board, threaded=threaded)
        self._logline("link", "connected to MOCK board (simulation, not hardware)")
        return self.board

    def _attach(self, transport, threaded: bool) -> None:
        self.disconnect()
        self.model = DeviceModel()
        self.threaded = threaded
        self.link = Link(transport, self.q, clock=self.clock)
        if threaded:
            self.link.start()
        self.send("INFO")
        self.send("STREAM 1")
        self.status = "connected: streaming"

    def disconnect(self) -> None:
        if self.link is not None:
            if self.model.active:
                self.send("DUMP")
            self.link.stop()
            self.link = None
        if self.recorder is not None and self.recorder.status == "recording":
            self.recorder.stop()

    @property
    def connected(self) -> bool:
        return self.link is not None and self.link.alive

    def send(self, cmd: str) -> bool:
        ok = self.link is not None and self.link.send(cmd)
        if not ok:
            self.status = f"not sent (link down): {cmd}"
        return ok

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        if self.link is not None and not self.threaded:
            self.link.poll_once()
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self._handle(item)
        self._push_volume()
        now = self.clock()
        if self._tail_until is not None and now >= self._tail_until:
            self._end_recording()
        if (self._start_sent_at is not None and now - self._start_sent_at > START_CONFIRM_S
                and self.model.state == "IDLE"):
            self._start_failed("the board did not enter INFLATE")
        if self._prep is not None:
            try:
                next(self._prep)
            except StopIteration:
                self._prep = None

    def _handle(self, item: LinkItem) -> None:
        if item.kind == "sent":
            self._logline("sent", item.text)
            return
        if item.kind == "down":
            self._logline("link", f"down: {item.text}")
            if self.recorder is not None and self.recorder.status == "recording":
                self.recorder.mark("link_down", item.text, self.session_s(item.host_s))
                self._end_recording()
            # Last, so nothing overwrites it. The board ends a run on its own
            # limits, but the operator must know the screen is no longer live.
            self.status = (f"LINK DOWN ({item.text}): the board finishes any run by itself. "
                           "If in doubt, open the bulb release screw.")
            return

        rec = item.record
        self.model.apply(rec, item.host_s)
        if self.recorder is not None and self.recorder.status == "recording":
            self.recorder.write(rec, item.text, self.session_s(item.host_s))

        if isinstance(rec, Distal):
            self._vol_buf.append((rec.ir, rec.red))
            self.dc["D"].append((rec.ir, rec.red))
            if self._vol_t0 is None:
                self._vol_t0 = self.board_s(rec.t_us)
        elif isinstance(rec, Proximal):
            self.dc["P"].append((rec.ir, rec.red))
        elif isinstance(rec, Imu):
            g = math.sqrt(rec.ax ** 2 + rec.ay ** 2 + rec.az ** 2) / LSB_PER_G
            self.imu_hist[rec.band].append((self.board_s(rec.t_us), g - 1.0))
        elif isinstance(rec, Control):
            self.p_hist.append((self.board_s(rec.t_us), rec.p_mmhg, rec.pref_mmhg))
        elif isinstance(rec, Event):
            self._logline("event", f"{rec.name},{rec.detail}")
            self._on_event(rec, item.host_s)
        elif isinstance(rec, Error):
            self._logline("error", rec.message)
            self.status = f"board: {rec.message}"
        elif isinstance(rec, Meta):
            if rec.pairs.get("streaming") == "1":
                self._reset_traces()
            if any(k in rec.pairs for k in ("dark_ir", "cuff_zero_abs_mmhg", "set", "streaming")) \
                    or rec.text.startswith(("set ", "ready", "NOT ready")):
                self._logline("meta", rec.text)

    def _on_event(self, ev: Event, host_s: float) -> None:
        if ev.name == "state" and ev.detail == "INFLATE":
            self._start_sent_at = None
            self.status = "INFLATE: pump the bulb; press O when the Doppler goes silent"
        elif ev.name == "state" and ev.detail == "HOLD":
            self.status = "HOLD"
        elif ev.name == "state" and ev.detail == "DEFLATE":
            self.status = "DEFLATING: press D when the Doppler flow returns"
        elif ev.name == "start_refused":
            self._start_failed(ev.detail)
        elif ev.name == "dump":
            self.status = f"DUMPED: {ev.detail}"
            if self.recorder is not None and self.recorder.status == "recording":
                self._tail_until = host_s + TAIL_S
        elif ev.name == "amp" and self.vol is not None:
            self.vol.interrupt("LED change")

    def _reset_traces(self) -> None:
        self.vol = None
        self._vol_t0 = None
        self._vol_buf.clear()

    def _push_volume(self) -> None:
        if not self._vol_buf:
            return
        if self.vol is None:
            dark = self.model.info.bands["D"]
            try:
                d = {"ir": float(dark["dark_ir"]), "red": float(dark["dark_red"])}
            except (KeyError, ValueError):
                d = {}
            self.vol = VolumeTracker(self.model.info.fs, dark=d, history_s=HISTORY_S)
        arr = np.asarray(self._vol_buf, dtype=float)
        self._vol_buf.clear()
        self.vol.push(arr[:, 0], arr[:, 1])

    # ------------------------------------------------------------- the log

    def _logline(self, kind: str, text: str) -> None:
        if self.log is None:
            self._log_buffer.append((kind, text, datetime.now()))
            del self._log_buffer[:-2000]
        else:
            self.log.write(kind, text)

    def _ensure_log(self, subject: str) -> None:
        if self.log is not None and self.log.path.parent.name == subject:
            return
        if self.log is not None:
            self.log.close()
        self.log = SessionLog(self.data_root, subject)
        for kind, text, when in self._log_buffer:
            self.log.write(kind, text, when)
        self._log_buffer.clear()

    # ------------------------------------------------------------- actions

    def check_trial(self) -> trialinfo.Checked:
        return trialinfo.check(self.trial_raw, before_start=True)

    def start(self) -> tuple[bool, str]:
        if self.recorder is not None:
            return False, "save or discard the last run first"
        if self._prep is not None:
            return False, "LED preparation still running"
        c = self.check_trial()
        if not c.ok:
            return False, "; ".join(c.errors)
        ok, why = self.model.can_start(self.clock())
        if not ok:
            return False, why
        v = c.values
        p_max = self.model.info.setting("p_max", 280.0)
        p_stop = self.model.info.setting("p_stop", 40.0)
        self.targets = trialinfo.inflation_targets(v["session_systolic"], "session systolic",
                                                   p_max, p_stop)
        self.p_occ = self.p_return = None
        self._ensure_log(v["subject_id"])
        self.send(f"SET p_run_min {self.targets.run_min:g}")
        # The header snapshot is taken now, before the board's "# set" reply
        # arrives; record the value just sent (a refusal shows as an E line).
        self.model.info.settings["p_run_min"] = f"{self.targets.run_min:.4f}"
        stream_t0 = (self.model.stream_started_s - self.t_start
                     if self.model.stream_started_s is not None else 0.0)
        self.recorder = RunRecorder(self.data_root, v, self.model.info, session_t0=stream_t0,
                                    t_us_now=self.model.t_us, unwrap_from=self.model._unwrap)
        self.recorder.mark("targets", f"target={self.targets.target:g} run_min={self.targets.run_min:g} "
                           f"basis={self.targets.basis}", self.session_s())
        self._save_trial()
        self.send("START")
        self._start_sent_at = self.clock()
        msg = f"recording {self.recorder.run_id}"
        if self.recorder.run_id != v["run_id"]:
            msg += f" (run ID {v['run_id']} was taken)"
        for w in c.warnings:
            self._logline("warning", w)
        self.status = msg
        return True, msg

    def _start_failed(self, why: str) -> None:
        self._start_sent_at = None
        if self.recorder is not None and self.model.state == "IDLE":
            self.recorder.discard()
            self.recorder = None
        self.status = f"START refused: {why}"

    def mark_occlusion(self) -> tuple[bool, str]:
        """O: the Doppler went silent. Moves the target and the Run gate."""
        if self.model.state != "INFLATE":
            return False, "O is for INFLATE: mark when the Doppler goes silent"
        p = self.model.p_mmhg
        if math.isnan(p):
            return False, "no pressure reading"
        self.p_occ = p
        self.targets = trialinfo.inflation_targets(
            p, "doppler occlusion", self.model.info.setting("p_max", 280.0),
            self.model.info.setting("p_stop", 40.0))
        self.send(f"SET p_run_min {self.targets.run_min:g}")
        if self.recorder is not None:
            self.recorder.mark("doppler_occluded", f"p={p:.1f} target={self.targets.target:g} "
                               f"run_min={self.targets.run_min:g}", self.session_s())
        self._logline("operator", f"doppler occluded at {p:.1f}")
        return True, f"Doppler silent at {p:.0f}: inflate to {self.targets.target:.0f}"

    def mark_return(self) -> tuple[bool, str]:
        """D: the Doppler flow came back (live backup to the video)."""
        if self.recorder is None or self.recorder.status != "recording":
            return False, "not recording"
        p = self.model.p_mmhg
        self.p_return = p
        self.recorder.mark("doppler_flow_return", f"p={p:.1f}", self.session_s())
        self._logline("operator", f"doppler flow return at {p:.1f}")
        return True, f"Doppler flow back at {p:.0f} mmHg"

    def run(self) -> tuple[bool, str]:
        run_min = self.targets.run_min if self.targets else self.model.info.setting("p_run_min")
        ok, why = self.model.can_run(run_min)
        if not ok:
            return False, why
        self.send("RUN")
        return True, "RUN sent"

    def dump(self) -> tuple[bool, str]:
        ok = self.send("DUMP")
        self._logline("operator", "DUMP pressed")
        return ok, "DUMP sent" if ok else "LINK DOWN: open the bulb release screw"

    def zero_volume(self) -> None:
        if self.vol is not None:
            self.vol.zero()

    def _end_recording(self) -> None:
        self._tail_until = None
        if self.recorder is None or self.recorder.status != "recording":
            return
        self.recorder.stop()
        self.status = (f"run {self.recorder.run_id} ended "
                       f"({self.recorder.dumps[-1] if self.recorder.dumps else 'stopped'}): "
                       "enter the standard-cuff reading, then Save run")

    def save_run(self) -> tuple[bool, str]:
        if self.recorder is None or self.recorder.status == "recording":
            return False, "no finished run to save"
        c = trialinfo.check(self.after_raw, before_start=False)
        if not c.ok:
            return False, "; ".join(c.errors)
        after = {k: v for k, v in c.values.items() if k.startswith("ref_") or k == "notes"}
        if self.p_occ is not None:
            after["doppler_occlusion_mark_mmhg"] = round(self.p_occ, 1)
        if self.p_return is not None:
            after["doppler_return_mark_mmhg"] = round(self.p_return, 1)
        self.recorder.save(after)
        rid = self.recorder.run_id
        self._logline("run", f"saved {rid} in {self.recorder.dir.name}")
        self.trial_raw["run_id"] = trialinfo.next_run_id(rid)
        self.after_raw = {}
        self.recorder = None
        self._save_trial()
        self.status = f"saved {rid}; next run {self.trial_raw['run_id']}"
        return True, self.status

    def discard_run(self) -> tuple[bool, str]:
        if self.recorder is None or self.recorder.status == "recording":
            return False, "no finished run to discard"
        rid = self.recorder.run_id
        self.recorder.discard()
        self.recorder = None
        self._logline("run", f"discarded {rid}")
        self.status = f"discarded {rid}"
        return True, self.status

    # --------------------------------------------- LED dark + auto-tune

    def prepare_leds(self) -> tuple[bool, str]:
        if self.model.state != "IDLE" or self.recorder is not None:
            return False, "only in IDLE with no run open"
        if self._prep is not None:
            return False, "already running"
        self._prep = self._prepare()
        return True, "measuring dark offsets, then tuning LEDs"

    def _wait(self, seconds: float):
        end = self.clock() + seconds
        while self.clock() < end:
            yield

    def _prepare(self):
        self.send("STREAM 0")
        yield from self._wait(0.3)
        for tag in "DP":
            self.model.info.bands[tag].pop("dark_ir", None)
            self.send(f"DARK {tag}")
            end = self.clock() + 6.0
            while "dark_ir" not in self.model.info.bands[tag] and self.clock() < end:
                yield
            if "dark_ir" not in self.model.info.bands[tag]:
                self.status = f"DARK {tag} got no reply"
        self.send("STREAM 1")
        yield from self._wait(1.0)
        for tag in "DP":
            yield from self._tune(tag)
        self.send("INFO")
        self.status = "LEDs ready: " + ", ".join(
            f"{t} IR {self._dc_pct(t, 0):.0f}% red {self._dc_pct(t, 1):.0f}%" for t in "DP")

    def _dc_pct(self, tag: str, ch: int) -> float:
        d = self.dc[tag]
        return 100.0 * float(np.mean([s[ch] for s in d])) / FULL_SCALE if d else float("nan")

    def _tune(self, tag: str):
        band = self.model.info.bands[tag]
        for _ in range(TUNE_STEPS):
            self.dc[tag].clear()
            yield from self._wait(0.6)
            if len(self.dc[tag]) < 20:
                self.status = f"band {tag}: no PPG data to tune on"
                return
            changed = False
            for ch, (name, key) in enumerate((("IR", "ir_amp"), ("RED", "red_amp"))):
                frac = self._dc_pct(tag, ch) / 100.0
                if TUNE_LO <= frac <= TUNE_HI:
                    continue
                amp = int(band.get(key, 31))
                new = int(round(amp * TUNE_MID / max(frac, 0.01)))
                new = min(max(new, 1), 255)
                if new != amp:
                    self.send(f"AMP {tag} {name} {new}")
                    band[key] = str(new)
                    changed = True
            if not changed:
                return
        self.status = f"band {tag}: LEDs did not settle in 40-60 % (check contact)"

    # ------------------------------------------------------- persistence

    def _trial_file(self) -> Path:
        return self.data_root / ".last_trial.json"

    def _load_trial(self) -> dict:
        try:
            return json.loads(self._trial_file().read_text())
        except (OSError, ValueError):
            return {}

    def _save_trial(self) -> None:
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            keep = {k: self.trial_raw[k] for k in PERSISTED if self.trial_raw.get(k) not in (None, "")}
            self._trial_file().write_text(json.dumps(keep, indent=1))
        except OSError:
            pass

    def close(self) -> None:
        self.disconnect()
        if self.log is not None:
            self.log.close()
