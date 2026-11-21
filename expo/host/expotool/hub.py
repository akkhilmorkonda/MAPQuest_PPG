"""Python model of the expo board's wireless side.

``LinkSimHub`` follows the rules both expo sketches give Wi-Fi clients:
client numbering, the ``# client_id`` welcome, one controller at a time
(CLAIM, CLAIM FORCE, RELEASE), commands open to everyone, the controller-lost
dump, ECHO, BLINK, the ST status line, the stream gap after ZERO, and the
display commands (VIEW, RESULT, ANALYSER; expo/DESIGN.md 3.1). The board
underneath is ``irbtool.mock.MockBoard`` (read-only reuse), which already
follows the IRB firmware's session rules and emits its line formats and rates.

It is what ``sim_server.py`` serves, so the phone page, the laptop receiver
and the results process can be exercised without the ESP32. Passing tests
here prove the host side and the protocol contract, not the firmware.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from irbtool.mock import PPG_DROPOUT_S, ROOM_ABS_MMHG, VENTED_BELOW, MockBoard, _abs_to_counts

FIRMWARE = "mapquest-expo-linksim-pysim"
CONTROLLER_TIMEOUT_S = 2.0          # config.h CONTROLLER_TIMEOUT_MS (also the analyser's)
STATUS_PERIOD_S = 0.5               # STATUS_PERIOD_MS
PUMP_TARGET = 160.0                 # SIM_PUMP_TARGET
P_RUN_MIN = 150.0                   # DEFAULT_P_RUN_MIN, the expo profile (DESIGN 4.2)
OPEN_TO_ALL = ("INFO", "P", "ECHO", "CLAIM", "RELEASE", "DUMP", "VIEW", "RESULT", "ANALYSER")
ST_COLUMNS = ("t_us", "state", "sensors", "zeroed", "controller", "clients", "p_run_min",
              "hold_s", "rate", "p_max", "ws_dropped_bytes", "loop_max_us", "view", "analyser")
SENSOR_ORDER = ("pressure", "ppg_D", "ppg_P", "imu_D", "imu_P")
RESULT_FLAGS = ("pending", "ok", "no_onset", "arm_moved", "pulse_present", "incomplete",
                "safety_stop", "stopped")   # kResultFlags in both sketches
REQUIRE_PROXIMAL_BAND = False       # expo_fw config.h: the result reads the distal band only
IDLE_VENT_ABOVE = 20.0              # expo_fw config.h IDLE_VENT_ABOVE_MMHG
IDLE_CLOSE_SETTLE_S = 3.0           # IDLE_CLOSE_SETTLE_MS


class ArmBoard(MockBoard):
    """The IRB mock board with a pulse that the cuff can stop.

    MockBoard's heartbeat never changes with cuff pressure, so every run on it
    would read "pulse never fully stopped". Here the pulse shrinks to a faint
    ripple (5 %) above systolic and grows back as the cuff drops below it,
    as on a real arm. Everything else is MockBoard unchanged."""

    RIPPLE = 0.05
    _prox_at_start = True
    _prox_warned = False

    # -- expo_fw's band rule (REQUIRE_PROXIMAL_BAND): only the distal band is
    # needed. A missing proximal band doesn't refuse START, and losing it
    # mid-run warns once instead of dumping the cuff. MockBoard checks both.

    def _start(self) -> None:
        if REQUIRE_PROXIMAL_BAND or "ppg_P" not in self.failed:
            self._prox_at_start, self._prox_warned = True, False
            return super()._start()
        self.failed.discard("ppg_P")
        try:
            super()._start()
        finally:
            self.failed.add("ppg_P")
        self._prox_at_start, self._prox_warned = False, False

    def _safety(self) -> None:
        if REQUIRE_PROXIMAL_BAND:
            return super()._safety()
        active = self.state in ("INFLATE", "HOLD", "DEFLATE")
        stale = self.t - self.last["ppg_P"] > PPG_DROPOUT_S
        if active and stale and self._prox_at_start and not self._prox_warned:
            self._prox_warned = True
            self._event("warning", "proximal PPG dropout: the run continues (not used by the result)")
        saved = self.last["ppg_P"]
        self.last["ppg_P"] = self.t                # the base check must not dump for it
        try:
            super()._safety()
        finally:
            self.last["ppg_P"] = saved

    # -- expo_fw's IDLE valve (idleValve()): held closed once the cuff has read
    # empty for IDLE_CLOSE_SETTLE_S; opened when the pressure sensor is gone or
    # the cuff holds pressure outside a run. Any dump restarts the wait.

    _idle_empty_t: float | None = None

    def _dump(self, reason: str) -> None:
        super()._dump(reason)
        self._idle_empty_t = None

    def _window(self) -> None:
        was = self.state
        super()._window()
        if self.state != "IDLE":
            return
        if was != "IDLE":
            self._idle_empty_t = None
        why = ("no pressure reading" if "pressure" in self.failed
               else "cuff pressurised outside a run" if self.p > IDLE_VENT_ABOVE else None)
        if why:
            self._idle_empty_t = None
            if self.valve_closed:
                self.valve_closed = False
                self._event("valve", f"open in IDLE: {why}")
            return
        if self.p >= VENTED_BELOW:
            self._idle_empty_t = None
            return
        if self._idle_empty_t is None:
            self._idle_empty_t = self.t
        if not self.valve_closed and self.t - self._idle_empty_t >= IDLE_CLOSE_SETTLE_S:
            self.valve_closed = True
            self._event("valve", "closed in IDLE")

    def _pulse_scale(self) -> float:
        if self.p >= self.sbp:
            return self.RIPPLE
        return self.RIPPLE + (1 - self.RIPPLE) * min(1.0, (self.sbp - self.p) / 30.0)

    def _ppg(self) -> None:
        # MockBoard._ppg with the pulse amplitude scaled; kept line for line
        # so the stream is the same apart from that.
        for tag in ("D", "P"):
            if f"ppg_{tag}" in self.failed:
                continue
            self.last[f"ppg_{tag}"] = self.t
            pulse = 0.0015 * math.sin(2 * math.pi * 1.2 * self.t) * (self._pulse_scale() if tag == "D" else 1.0)
            vol = self.volume if tag == "D" else 0.0
            ir_amp, red_amp = self.amps[tag]
            ir = 9 + 4200 * ir_amp * (1 - 0.0005 * vol + pulse) + self.rng.gauss(0, 20)
            red = 13 + 3500 * red_amp * (1 - 0.0008 * vol + pulse) + self.rng.gauss(0, 20)
            ir, red = (int(min(max(v, 0), 262143)) for v in (ir, red))
            if self.streaming:
                if tag == "D":
                    raw = _abs_to_counts(ROOM_ABS_MMHG + self.p)
                    self._emit(f"S,{self.idx['D']},{self._us()},{ir},{red},{self.p_idx},{raw},64")
                else:
                    self._emit(f"SP,{self.idx['P']},{self._us()},{ir},{red}")
            self.idx[tag] += 1


@dataclass
class _Client:
    last_rx: float
    direct: list[str] = field(default_factory=list)


class LinkSimHub:
    def __init__(self, board: MockBoard | None = None, now: float = 0.0) -> None:
        self.board = board or ArmBoard(realtime=False)
        self.board.command("STREAM 1")       # Wi-Fi clients always get the stream
        self.board.command(f"SET p_run_min {P_RUN_MIN:g}")
        self.board.out.clear()
        self.clients: dict[int, _Client] = {}
        self.next_id = 1
        self.controller = 0
        self.run_by_wifi = False
        self.blinks = 0
        self.view_technical = False
        self.analyser = 0
        self.result = ""
        self._now = now
        self._next_status = now
        self._out: list[str] = []

    # -- connections -------------------------------------------------------

    def connect(self, now: float) -> int:
        cid = self.next_id
        self.next_id += 1
        self.clients[cid] = _Client(now, [f"# client_id={cid}"])
        return cid

    def disconnect(self, cid: int) -> None:
        self.clients.pop(cid, None)

    def receive(self, cid: int, text: str, now: float) -> None:
        client = self.clients.get(cid)
        if client is None:
            return
        client.last_rx = now
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line == "PING":
                continue
            if line == "DUMP":                          # out of band in the firmware
                self._event("dump_request", f"client {cid}")
                self.board.command("DUMP")
                continue
            verb = line.split(" ", 1)[0]
            if verb in OPEN_TO_ALL or cid == self.controller:
                self._command(line, cid)
            else:
                self._emit(f"E,{verb} refused: client {cid} does not have control (send CLAIM)")

    # -- output ------------------------------------------------------------

    def _emit(self, line: str) -> None:
        self._flush_board()          # keep the board's lines and ours in the order they happened
        self._out.append(line)

    def _event(self, name: str, detail: str = "") -> None:
        self._emit(f"EV,{self.board._us()},{name},{detail}")

    def _set_controller(self, cid: int, why: str) -> None:
        self.controller = cid
        self._event("controller", f"{cid if cid else 'none'} {why}")

    def _set_view(self, technical: bool, why: str) -> None:
        self.view_technical = technical
        self._event("view", f"{'TECHNICAL' if technical else 'PUBLIC'} {why}")

    # -- commands ----------------------------------------------------------

    def _command(self, line: str, cid: int) -> None:
        verb, _, arg = line.partition(" ")
        arg = arg.strip()
        b = self.board
        active = b.state in ("INFLATE", "HOLD", "DEFLATE")
        if verb == "CLAIM":
            if self.controller in (0, cid):
                self._set_controller(cid, "claimed")
            elif arg == "FORCE" and not active:
                self._set_controller(cid, f"taken from {self.controller}")
            else:
                tail = " and a run is active" if arg == "FORCE" else \
                    " (CLAIM FORCE takes it when no run is active)"
                self._emit(f"E,CLAIM refused: client {self.controller} has control{tail}")
        elif verb == "RELEASE":
            if self.controller != cid:
                self._emit("E,RELEASE refused: this client does not have control")
            elif active and self.run_by_wifi:
                self._emit("E,RELEASE refused while its run is active")
            else:
                self._set_controller(0, "released")
        elif verb == "VIEW":
            if arg.upper() not in ("PUBLIC", "TECHNICAL"):
                self._emit("E,VIEW needs PUBLIC or TECHNICAL")
            else:
                self._set_view(arg.upper() == "TECHNICAL", f"client {cid}")
        elif verb == "RESULT":
            self._result(arg)
        elif verb == "ANALYSER":
            self.analyser = cid
            self._event("analyser", f"{cid} joined")
        elif verb == "ECHO":
            n = int(arg) if arg.isdigit() else 0
            self._emit(f"# echo={n} client={cid}")
        elif verb == "BLINK":
            self.blinks += 1
            self._emit("# blink")
        elif verb == "STREAM":
            self._emit("E,STREAM is serial-only: Wi-Fi clients always receive the stream")
        elif verb in ("ZERO", "R"):                  # blocking: Wi-Fi gets a stream gap instead
            if b.state != "IDLE":
                self._emit(f"E,{verb} refused: state is {b.state}")
                return
            b.streaming = False                      # MockBoard refuses these while streaming
            b.command(verb)
            b.streaming = True
            self._flush_board()
            self._event("stream_gap", verb)
        elif verb == "START":
            b.command("START")
            if b.state == "INFLATE":
                self.run_by_wifi = True
                b.pump_to(max(PUMP_TARGET, b.settings["p_run_min"] + 15.0))
                if self.view_technical:
                    self._set_view(False, "start")
        elif verb == "INFO":
            b.command("INFO")
            self._flush_board()
            self._info()
        else:                                        # RUN, SET, P, DUMP, anything else
            b.command(line)
            if verb == "SET" and arg.startswith("p_run_min") and b.state == "INFLATE" \
                    and b.pump_target is not None:
                b.pump_to(max(b.pump_target, b.settings["p_run_min"] + 15.0))

    def _result(self, arg: str) -> None:
        """RESULT <run> <mmhg|-> <flag>, validated as the firmware does."""
        m = re.fullmatch(r"(\d+) (\S+) (\S+)", arg)
        if not m or int(m.group(1)) == 0:
            self._emit("E,RESULT needs <run> <mmhg|-> <flag>")
            return
        run, val, flag = m.groups()
        if flag not in RESULT_FLAGS:
            self._emit(f"E,RESULT unknown flag: {flag}")
            return
        if val != "-":
            try:
                ok = 0.0 <= float(val) <= 300.0
            except ValueError:
                ok = False
            if not ok:
                self._emit(f"E,RESULT mmHg must be 0-300 or -: {val}")
                return
        self.result = f"run={int(run)} mmhg={val} flag={flag}"
        self._event("result", self.result)

    def _info(self) -> None:
        self._emit(f"# firmware={FIRMWARE}")
        self._emit("# board=pc mode=link_simulator sensors=simulated")
        self._emit(f"# wifi=simulated ws_clients={len(self.clients)}")
        self._emit(f"# controller={self.controller} "
                   f"controller_timeout_ms={int(CONTROLLER_TIMEOUT_S * 1000)} "
                   f"run_by_wifi={int(self.run_by_wifi)}")
        self._emit(f"# view={'TECHNICAL' if self.view_technical else 'PUBLIC'} analyser={self.analyser}")
        if self.result:
            self._emit(f"# result {self.result}")
        self._emit("# columns_ST=" + ",".join(ST_COLUMNS))

    def status_line(self) -> str:
        b, s = self.board, self.board.settings
        sensors = "".join("0" if k in b.failed else "1" for k in SENSOR_ORDER)
        return (f"ST,{b._us()},{b.state},{sensors},{int(b.zeroed)},{self.controller},"
                f"{len(self.clients)},{s['p_run_min']:.1f},{s['hold_s']:.1f},{s['rate']:.2f},"
                f"{s['p_max']:.1f},0,0,{'TECHNICAL' if self.view_technical else 'PUBLIC'},"
                f"{self.analyser}")

    # -- time --------------------------------------------------------------

    def _flush_board(self) -> None:
        data = self.board.out.decode()
        self.board.out.clear()
        self._out.extend(ln for ln in data.split("\n") if ln)

    def tick(self, now: float) -> tuple[list[str], dict[int, list[str]]]:
        """Advance to ``now`` (seconds). Returns the lines for every client and
        the lines for individual clients (welcomes)."""
        dt = max(0.0, now - self._now)
        self._now = now
        # Advance in 10 ms steps so the controller check runs between samples.
        while dt > 1e-9:
            step = min(dt, 0.01)
            self.board.advance(step)
            dt -= step
            self._flush_board()
        self._controller_check(now)
        self._analyser_check(now)
        if now >= self._next_status:
            self._next_status = now + STATUS_PERIOD_S
            self._emit(self.status_line())
        direct = {}
        for cid, c in self.clients.items():
            if c.direct:
                direct[cid], c.direct = c.direct, []
        out, self._out = self._out, []
        return (out if self.clients else []), direct

    def _gone(self, cid: int, now: float) -> bool:
        c = self.clients.get(cid)
        return c is None or now - c.last_rx > CONTROLLER_TIMEOUT_S

    def _analyser_check(self, now: float) -> None:
        if self.analyser and self._gone(self.analyser, now):
            self.analyser = 0
            self._event("analyser", "none lost")

    def _controller_check(self, now: float) -> None:
        if not self.controller or not self._gone(self.controller, now):
            return
        self._set_controller(0, "lost")
        b = self.board
        if not self.run_by_wifi or b.state not in ("INFLATE", "HOLD", "DEFLATE"):
            return
        if b.state in ("INFLATE", "HOLD"):
            b._dump("controller lost")
            self._flush_board()
        else:
            self._event("warning", "controller lost during DEFLATE: the run finishes on the board's limits")
