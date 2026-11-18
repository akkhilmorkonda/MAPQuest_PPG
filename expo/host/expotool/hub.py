"""Python model of the expo link simulator's wireless side.

``LinkSimHub`` follows ``expo/firmware/expo_link_sim``'s rules for Wi-Fi
clients: client numbering, the ``# client_id`` welcome, one controller at a
time (CLAIM, CLAIM FORCE, RELEASE), commands open to everyone, the
controller-lost dump, ECHO, BLINK, the ST status line and the stream gap
after ZERO. The board underneath is ``irbtool.mock.MockBoard`` (read-only
reuse), which already follows the IRB firmware's session rules and emits
its line formats and rates.

It is what ``sim_server.py`` serves, so the phone page and the laptop
receiver can be exercised without the ESP32. Passing tests here prove the
host side and the protocol contract, not the firmware.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from irbtool.mock import MockBoard

FIRMWARE = "mapquest-expo-linksim-pysim"
CONTROLLER_TIMEOUT_S = 2.0          # expo_link_sim/config.h CONTROLLER_TIMEOUT_MS
STATUS_PERIOD_S = 0.5               # STATUS_PERIOD_MS
PUMP_TARGET = 160.0                 # SIM_PUMP_TARGET
OPEN_TO_ALL = ("INFO", "P", "ECHO", "CLAIM", "RELEASE", "DUMP")
ST_COLUMNS = ("t_us", "state", "sensors", "zeroed", "controller", "clients", "p_run_min",
              "hold_s", "rate", "p_max", "ws_dropped_bytes", "loop_max_us")
SENSOR_ORDER = ("pressure", "ppg_D", "ppg_P", "imu_D", "imu_P")


@dataclass
class _Client:
    last_rx: float
    direct: list[str] = field(default_factory=list)


class LinkSimHub:
    def __init__(self, board: MockBoard | None = None, now: float = 0.0) -> None:
        self.board = board or MockBoard(realtime=False)
        self.board.command("STREAM 1")       # Wi-Fi clients always get the stream
        self.board.out.clear()
        self.clients: dict[int, _Client] = {}
        self.next_id = 1
        self.controller = 0
        self.run_by_wifi = False
        self.blinks = 0
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
        elif verb == "ECHO":
            n = int(arg) if arg.isdigit() else 0
            self._emit(f"# echo={n} client={cid}")
        elif verb == "BLINK":
            self.blinks += 1
            self._emit("# blink")
        elif verb == "STREAM":
            self._emit("E,STREAM is serial-only: Wi-Fi clients always receive the stream")
        elif verb == "ZERO":
            if b.state != "IDLE":
                self._emit(f"E,ZERO refused: state is {b.state}")
                return
            b.streaming = False                      # MockBoard refuses ZERO while streaming
            b.command("ZERO")
            b.streaming = True
            self._flush_board()
            self._event("stream_gap", "ZERO")
        elif verb == "START":
            b.command("START")
            if b.state == "INFLATE":
                self.run_by_wifi = True
                b.pump_to(max(PUMP_TARGET, b.settings["p_run_min"] + 15.0))
        elif verb == "INFO":
            b.command("INFO")
            self._flush_board()
            self._info()
        else:                                        # RUN, SET, P, DUMP, anything else
            b.command(line)
            if verb == "SET" and arg.startswith("p_run_min") and b.state == "INFLATE" \
                    and b.pump_target is not None:
                b.pump_to(max(b.pump_target, b.settings["p_run_min"] + 15.0))

    def _info(self) -> None:
        self._emit(f"# firmware={FIRMWARE}")
        self._emit("# board=pc mode=link_simulator sensors=simulated")
        self._emit(f"# wifi=simulated ws_clients={len(self.clients)}")
        self._emit(f"# controller={self.controller} "
                   f"controller_timeout_ms={int(CONTROLLER_TIMEOUT_S * 1000)} "
                   f"run_by_wifi={int(self.run_by_wifi)}")
        self._emit("# columns_ST=" + ",".join(ST_COLUMNS))

    def status_line(self) -> str:
        b, s = self.board, self.board.settings
        sensors = "".join("0" if k in b.failed else "1" for k in SENSOR_ORDER)
        return (f"ST,{b._us()},{b.state},{sensors},{int(b.zeroed)},{self.controller},"
                f"{len(self.clients)},{s['p_run_min']:.1f},{s['hold_s']:.1f},{s['rate']:.2f},"
                f"{s['p_max']:.1f},0,0")

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
        if now >= self._next_status:
            self._next_status = now + STATUS_PERIOD_S
            self._emit(self.status_line())
        direct = {}
        for cid, c in self.clients.items():
            if c.direct:
                direct[cid], c.direct = c.direct, []
        out, self._out = self._out, []
        return (out if self.clients else []), direct

    def _controller_check(self, now: float) -> None:
        if not self.controller:
            return
        c = self.clients.get(self.controller)
        if c is not None and now - c.last_rx <= CONTROLLER_TIMEOUT_S:
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
