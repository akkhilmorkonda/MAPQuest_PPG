"""Results manager: the laptop process that turns each run into a result.

Join the board's Wi-Fi network (expo/README.md), then leave this running for
the whole session:

    python expo/host/expo_results.py
    python expo/host/expo_results.py --url ws://127.0.0.1:8765/ws      # the local stand-in
    python expo/host/expo_results.py --replay capture.txt              # analyse a saved stream

What it does (expo/DESIGN.md section 7):

1. Connects to the board as a watcher and announces itself (ANALYSER), so the
   iPad's Laptop light comes on. It never takes control of the cuff.
2. Records every run in memory: START to the vent, plus 15 s before it.
3. When a run that reached DEFLATE is dumped, gives it the next run number
   and posts ``RESULT <n> - pending`` (the iPad shows "Analysing...").
4. When the cuff has vented, runs host/mapquest unchanged on the run, grades
   it (no onset / incomplete / arm moved / pulse never stopped) and posts
   ``RESULT <n> <mmHg> <flag>`` through the board to every screen.
5. Appends one anonymous row to the results file. The run's signals are
   dropped once it is analysed (DESIGN 7.5).
6. Serves the monitor dashboard on this laptop (http://127.0.0.1:8800/,
   web/dashboard.html): it relays the board's stream to it, adds run numbers
   and results with their traces, and passes back only its mode switch.
   expo (iPad): the same server listens on the board's Wi-Fi too, read-only,
   and ``ANALYSER <port>`` tells the board where it is, so the iPad page can
   fetch the technical view's traces from it. ``--dash-host 127.0.0.1`` keeps
   it on this laptop.

``--save-raw DIR`` keeps each run's lines for TEAM dry runs only (to check a
missed onset afterwards). Never use it with visitors: it is the raw signal.

The link to the board is retried every 2 s; a run that loses the link is
graded "signal incomplete".
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expotool.dashserve import DashServer  # noqa: E402
from expotool.results import Result, ResultsLog, Run, RunRecorder, analyse, describe, payload  # noqa: E402
from expotool.ws import WSClient  # noqa: E402

DEFAULT_URL = "ws://192.168.4.1/ws"
DASH_PORT = 8800
DASH_HOST = "0.0.0.0"              # the board's Wi-Fi too (read-only there), for the iPad
DEFAULT_RESULTS = Path(__file__).resolve().parents[1] / "results" / "expo_results.csv"
PING_S = 0.5                       # the board drops a silent analyser after 2 s
RETRY_S = 2.0


def say(text: str) -> None:
    print(text, flush=True)        # visible at once even when the console is a pipe or a log


class Manager:
    """Everything between the stream and the RESULT lines, transport-free.

    ``line()`` takes each received line and returns the commands to send.
    Analysis runs on a worker thread so the link keeps being serviced;
    ``poll()`` collects finished results and returns their RESULT lines.
    """

    def __init__(self, log: ResultsLog, save_raw: Path | None = None, out=say,
                 threaded: bool = True, dash=None) -> None:
        self.log = log
        self.save_raw = save_raw
        self.out = out
        self.dash = dash                             # DashServer, or None
        self.recorder = RunRecorder()
        self.results: list = []
        self._done: queue.Queue = queue.Queue()
        self._threaded = threaded
        self._tell("hello", next_run=self.log.last_run + 1)

    def _tell(self, kind: str, **payload) -> None:
        if self.dash is not None:
            self.dash.message(kind, **payload)

    def line(self, line: str) -> list[str]:
        send = []
        for kind, run in self.recorder.line(line):
            if kind == "deflate":
                run.number = self.log.next_run()
                self.out(f"run {run.number}: deflating")
                self._tell("run", run=run.number)
            elif kind == "ended":
                if run.end_reason == "reached stop pressure":
                    send.append(f"RESULT {run.number} - pending")
                    self.out(f"run {run.number}: analysing")
                    self._tell("pending", run=run.number)
            elif kind == "complete":
                self._submit(run)
        return send

    def link_lost(self) -> None:
        self.recorder.link_lost()

    def _submit(self, run: Run) -> None:
        if self.save_raw:
            self.save_raw.mkdir(parents=True, exist_ok=True)
            (self.save_raw / f"run_{run.number:04d}.txt").write_text(
                "\n".join(run.lines) + "\n", encoding="utf-8")
        if self._threaded:
            threading.Thread(target=self._work, args=(run,), daemon=True).start()
        else:
            self._work(run)

    def _work(self, run: Run) -> None:
        try:
            res = analyse(run, run.number)
        except Exception as e:                       # never take the process down mid-session
            res = Result(run.number, None, "incomplete", f"analysis failed: {type(e).__name__}: {e}")
        # No raw visitor signals are kept once the run is analysed (DESIGN 7.5).
        run.rows.clear()
        run.lines.clear()
        run.imu.clear()
        self._done.put(res)

    def poll(self) -> list[str]:
        send = []
        while True:
            try:
                res = self._done.get_nowait()
            except queue.Empty:
                return send
            self.log.append(res)
            self.results.append(res)
            self.out(describe(res))
            send.append(res.command())
            self._tell("result", at=round(time.time(), 1), **payload(res))
            self._tell("hello", next_run=self.log.last_run + 1)


def analyser_command(dash) -> str:
    """``ANALYSER <dashboard port>``: the board pairs the port with this
    laptop's address so the iPad can reach the dashboard server. Without a
    dashboard, plain ``ANALYSER``. Older firmware ignores the port."""
    port = getattr(dash, "port", 0) if dash is not None else 0
    return f"ANALYSER {port}" if port else "ANALYSER"


def run_live(url: str, mgr: Manager, seconds: float | None = None, ping_s: float = PING_S,
             out=say, stop: threading.Event | None = None, dash=None,
             dash_cmds: queue.Queue | None = None) -> None:
    """Stay connected to the board until stopped. ``dash`` gets the stream
    relayed; ``dash_cmds`` holds the dashboard's mode switches to forward."""
    t_end = None if seconds is None else time.monotonic() + seconds
    stop = stop or threading.Event()
    while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
        try:
            ws = WSClient(url)
        except OSError as e:
            out(f"cannot reach the board at {url} ({e}); retrying in {RETRY_S:.0f} s")
            stop.wait(RETRY_S)
            continue
        out(f"connected to {url}")
        if dash is not None:
            dash.message("link", up=True)
            if dash.port:
                try:
                    here = ws.sock.getsockname()[0]       # this laptop on the board's network
                    out(f"iPad technical view reads ws://{here}:{dash.port}/ws")
                except OSError:
                    pass
        try:
            ws.send_text(analyser_command(dash))
            ws.send_text("INFO")                    # cuff zero, dark offsets, sample rate
            next_ping = next_echo = 0.0
            me, n_echo, echo_sent, rtts = 0, 0, {}, []
            while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
                now = time.monotonic()
                if now >= next_ping:
                    ws.send_text("PING")
                    next_ping = now + ping_s
                if dash is not None and me and now >= next_echo:      # round trip, for the monitor
                    n_echo += 1
                    echo_sent[n_echo] = now
                    ws.send_text(f"ECHO {n_echo}")
                    next_echo = now + 2.0
                for cmd in mgr.poll():
                    ws.send_text(cmd)
                while dash_cmds is not None and not dash_cmds.empty():
                    ws.send_text(dash_cmds.get_nowait())     # VIEW only (DashServer filters)
                msg = ws.recv(timeout=0.05)
                if msg:
                    arrived = time.monotonic()
                    if dash is not None:
                        dash.relay(msg)
                    for ln in msg.split("\n"):
                        if ln.startswith("# client_id="):
                            me = int(ln.split("=", 1)[1])
                        elif ln.startswith("# echo=") and f"client={me}" in ln and dash is not None:
                            sent = echo_sent.pop(int(ln.split("=", 1)[1].split()[0]), None)
                            if sent is not None:
                                rtts = (rtts + [(arrived - sent) * 1000.0])[-10:]
                                dash.message("rtt", ms=round(sorted(rtts)[len(rtts) // 2], 1))
                        for cmd in mgr.line(ln):
                            ws.send_text(cmd)
        except ConnectionError as e:
            out(f"LINK LOST: {e}; reconnecting")
            mgr.link_lost()
            if dash is not None:
                dash.message("link", up=False)
        finally:
            ws.close()
    mgr.poll()                                     # a result finishing now is still logged


def replay(path: Path, mgr: Manager) -> None:
    """Analyse a saved stream (expo_receiver --save, --save-raw, a serial log)."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            mgr.line(ln)
    mgr.poll()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turn each expo run into a return-of-flow result.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"board WebSocket (default {DEFAULT_URL})")
    ap.add_argument("--results", type=Path,
                    help=f"anonymous results file, appended (default {DEFAULT_RESULTS}; "
                         "--replay writes none unless given)")
    ap.add_argument("--save-raw", type=Path, metavar="DIR",
                    help="TEAM DRY RUNS ONLY: keep each run's raw lines here")
    ap.add_argument("--replay", type=Path, metavar="FILE", help="analyse a saved stream and exit")
    ap.add_argument("--dash-port", type=int, default=DASH_PORT,
                    help=f"serve the monitor dashboard on this laptop at this port (default {DASH_PORT}; 0 = off)")
    ap.add_argument("--dash-host", default=DASH_HOST,
                    help=f"address the dashboard server listens on (default {DASH_HOST}: this laptop and, "
                         "read-only, the board's Wi-Fi for the iPad; 127.0.0.1 = this laptop only)")
    args = ap.parse_args(argv)
    results = args.results or (None if args.replay else DEFAULT_RESULTS)
    if args.replay:
        mgr = Manager(ResultsLog(results), args.save_raw, threaded=False)
        replay(args.replay, mgr)
        if not mgr.results:
            say("no run in that file reached DEFLATE")
        return 0
    dash = dash_cmds = None
    if args.dash_port:
        dash_cmds = queue.Queue()
        dash = DashServer(on_command=dash_cmds.put)
        port = dash.start(args.dash_host, args.dash_port)
        say(f"dashboard -> http://127.0.0.1:{port}/  (open it full screen on the monitor)")
    mgr = Manager(ResultsLog(results), args.save_raw, dash=dash)
    if args.save_raw:
        say(f"SAVING RAW RUNS to {args.save_raw} (team dry runs only, never visitors)")
    say(f"results -> {results}")
    try:
        run_live(args.url, mgr, dash=dash, dash_cmds=dash_cmds)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
