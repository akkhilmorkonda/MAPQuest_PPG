"""The expo board over USB, with the Wi-Fi client rules done on the laptop.

``usb_bridge.py`` serves the phone page and the dashboard's WebSocket on this
laptop, fed by the board's USB serial stream instead of its Wi-Fi. The board
already does the session itself (START, RUN, the safety limits, VIEW, RESULT);
over USB it treats the laptop as the trusted bench link, so it does not apply
the rules it gives Wi-Fi clients. ``UsbHub`` applies those here, as
expo_fw's handleWifiCommand does:

- client numbers and the ``# client_id`` welcome;
- one controller at a time (CLAIM, CLAIM FORCE, RELEASE), lost after
  CONTROLLER_TIMEOUT_S of silence; losing it during INFLATE or HOLD of a run
  it started sends DUMP, during DEFLATE it warns;
- the analyser (ANALYSER), lost the same way;
- ECHO answered here (the round trip is browser to laptop);
- the controller-only verbs (START, RUN, SET of the session keys, ZERO,
  BLINK, R); bench commands are refused, as over Wi-Fi;
- ZERO and R, which the board refuses while its USB stream is on: sent as
  STREAM 0, the command, STREAM 1, with a stream_gap event for the clients.

The board's own controller and analyser fields (its Wi-Fi side) are replaced
by the laptop's in ST lines and INFO, and its controller / analyser events are
not passed on. Everything else goes through unchanged.

No I/O here: ``usb_bridge.py`` moves bytes, tests drive this directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONTROLLER_TIMEOUT_S = 2.0           # expo_fw config.h CONTROLLER_TIMEOUT_MS
OPEN_TO_ALL = ("INFO", "P", "VIEW", "RESULT")          # forwarded for anyone
LOCAL = ("PING", "CLAIM", "RELEASE", "ECHO", "ANALYSER")
CONTROLLER_VERBS = ("START", "RUN", "SET", "ZERO", "BLINK", "R")
SET_KEYS = ("rate", "hold_s", "p_stop", "p_run_min")   # what a Wi-Fi controller may SET
NEEDS_STREAM_OFF = ("ZERO", "R")
ACTIVE = ("INFLATE", "HOLD", "DEFLATE")
STREAM_STALE_S = 3.0                 # no data this long: ask for the stream again

_CONTROLLER_RE = re.compile(r"(?<![\w])controller=\d+")
_ANALYSER_RE = re.compile(r"(?<![\w])analyser=\d+")


@dataclass
class _Client:
    last_rx: float
    direct: list[str] = field(default_factory=list)


class UsbHub:
    def __init__(self, now: float = 0.0) -> None:
        self.clients: dict[int, _Client] = {}
        self.next_id = 1
        self.controller = 0
        self.analyser = 0
        self.run_by_bridge = False
        self.state = "?"
        self.t_us = 0                     # the board's clock, last seen
        self.last_data = now              # last S/ST line from the board
        self.last_stream_ask = -1e9
        self.to_board: list[str] = []     # command lines for the serial port
        self._out: list[str] = []

    # -- browser side ------------------------------------------------------

    def connect(self, now: float) -> int:
        cid = self.next_id
        self.next_id += 1
        self.clients[cid] = _Client(now, [f"# client_id={cid}", "# link=usb_bridge"])
        return cid

    def disconnect(self, cid: int) -> None:
        self.clients.pop(cid, None)       # the timeout check notices a lost controller

    def receive(self, cid: int, text: str, now: float) -> None:
        client = self.clients.get(cid)
        if client is None:
            return
        client.last_rx = now
        for raw in text.splitlines():
            line = raw.strip()
            if line:
                self._command(cid, line)

    def _command(self, cid: int, line: str) -> None:
        verb, _, arg = line.partition(" ")
        arg = arg.strip()
        if verb == "PING":
            return
        if line == "DUMP":                                   # always, from anyone
            self._event("dump_request", f"client {cid}")
            self._board("DUMP")
            return
        if verb == "CLAIM":
            self._claim(cid, arg)
        elif verb == "RELEASE":
            if self.controller != cid:
                self._emit("E,RELEASE refused: this client does not have control")
            elif self.state in ACTIVE and self.run_by_bridge:
                self._emit("E,RELEASE refused while its run is active")
            else:
                self._set_controller(0, "released")
        elif verb == "ECHO":
            n = int(arg) if arg.isdigit() else 0
            self.clients[cid].direct.append(f"# echo={n} client={cid}")
        elif verb == "ANALYSER":
            self.analyser = cid
            self._event("analyser", f"{cid} joined")
        elif verb in OPEN_TO_ALL:
            self._board(line)
        elif verb in CONTROLLER_VERBS:
            if cid != self.controller:
                self._emit(f"E,{verb} refused: client {cid} does not have control (send CLAIM)")
            elif verb == "SET" and arg.split(" ")[0] not in SET_KEYS:
                self._emit(f"E,SET {arg.split(' ')[0]} refused: only {'|'.join(SET_KEYS)} from a client")
            elif verb in NEEDS_STREAM_OFF:
                self._board("STREAM 0", line, "STREAM 1")
                self._event("stream_gap", verb)
            else:
                if verb == "START":
                    self.run_by_bridge = True
                self._board(line)
        elif verb == "STREAM":
            self._emit("E,STREAM is handled by the bridge: clients always receive the stream")
        else:
            self._emit(f"E,{verb} refused: bench commands are not sent from the page")

    def _claim(self, cid: int, arg: str) -> None:
        if self.controller in (0, cid):
            self._set_controller(cid, "claimed")
        elif arg == "FORCE" and self.state not in ACTIVE:
            self._set_controller(cid, f"taken from {self.controller}")
        else:
            tail = " and a run is active" if arg == "FORCE" else " (CLAIM FORCE takes it when no run is active)"
            self._emit(f"E,CLAIM refused: client {self.controller} has control{tail}")

    # -- board side --------------------------------------------------------

    def board_lines(self, lines: list[str], now: float) -> None:
        """Lines read from the serial port, newest last."""
        for ln in lines:
            ln = ln.rstrip("\r\n")
            if not ln:
                continue
            f = ln.split(",")
            kind = f[0]
            if kind in ("S", "SP", "M", "C", "ST", "EV") and len(f) > 2:
                stamp = f[1] if kind == "EV" else f[2] if kind in ("S", "SP", "M", "C") else f[1]
                if stamp.isdigit():
                    self.t_us = int(stamp)
            if kind in ("S", "ST", "C"):
                self.last_data = now
            if kind == "ST" and len(f) >= 7:
                self.state = f[2]
                f[5], f[6] = str(self.controller), str(len(self.clients))
                if len(f) >= 15:
                    f[14] = str(self.analyser)
                ln = ",".join(f)
            elif kind == "EV" and len(f) >= 3:
                if f[2] in ("controller", "analyser", "dump_request"):
                    continue                                 # the board's Wi-Fi side, not ours
                if f[2] == "state" and len(f) >= 4:
                    self.state = f[3]
                    if self.state == "IDLE":
                        self.run_by_bridge = False
            elif ln.startswith("#"):
                ln = _CONTROLLER_RE.sub(f"controller={self.controller}", ln)
                ln = _ANALYSER_RE.sub(f"analyser={self.analyser}", ln)
            self._out.append(ln)

    def link_lost(self) -> None:
        self._emit("E,USB link to the board lost: reconnecting")
        self.state = "?"

    def link_up(self) -> None:
        self._emit("# usb link up: asking the board for its stream")
        self.last_stream_ask = -1e9

    # -- time --------------------------------------------------------------

    def tick(self, now: float) -> tuple[list[str], dict[int, list[str]]]:
        """Lines for every client, lines for single clients. Commands for the
        board accumulate in ``to_board`` (the caller writes and clears it)."""
        self._lost_check(now)
        if now - self.last_data > STREAM_STALE_S and now - self.last_stream_ask > STREAM_STALE_S:
            self.last_stream_ask = now
            self._board("STREAM 1")
        direct = {}
        for cid, c in self.clients.items():
            if c.direct:
                direct[cid], c.direct = c.direct, []
        out, self._out = self._out, []
        return (out if self.clients else []), direct

    def _gone(self, cid: int, now: float) -> bool:
        c = self.clients.get(cid)
        return c is None or now - c.last_rx > CONTROLLER_TIMEOUT_S

    def _lost_check(self, now: float) -> None:
        if self.analyser and self._gone(self.analyser, now):
            self.analyser = 0
            self._event("analyser", "none lost")
        if not self.controller or not self._gone(self.controller, now):
            return
        self._set_controller(0, "lost")
        if not self.run_by_bridge or self.state not in ACTIVE:
            return
        if self.state in ("INFLATE", "HOLD"):
            self._board("DUMP")
            self._event("dump_request", "controller lost")
        else:
            self._event("warning", "controller lost during DEFLATE: the run finishes on the board's limits")

    # -- helpers -----------------------------------------------------------

    def _board(self, *lines: str) -> None:
        self.to_board.extend(lines)

    def _emit(self, line: str) -> None:
        self._out.append(line)

    def _event(self, name: str, detail: str = "") -> None:
        self._out.append(f"EV,{self.t_us},{name},{detail}")

    def _set_controller(self, cid: int, why: str) -> None:
        self.controller = cid
        self._event("controller", f"{cid if cid else 'none'} {why}")
