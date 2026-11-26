"""Log the expo board's serial output with timestamps, without resetting it.

Opening the XIAO's USB port the usual way restarts the board, which wipes the
reason for the last reset. This opens it with DTR and RTS held off, so the
board keeps running, and logs every line that is not a data line (#, EV, E,
and the chip's own boot messages) with the laptop's time.

A board restart is printed as RESTART with its reason: from the boot line
("booting, reset_reason=..."), or, when the USB port vanished during the
restart and the boot line was missed, from INFO, which is asked for each
time the port (re)opens. The first INFO also names the reason for the last
reset before logging started.

    python expo/tools/board_log.py --port COM8
    python expo/tools/board_log.py --port COM8 --out expo/results/board_log.txt

Ctrl+C stops it and prints a summary: restarts by reason, Wi-Fi client
closures, and how long each boot lasted.
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import serial

DATA = ("S,", "SP,", "M,", "C,", "ST,")
BOOT = re.compile(r"booting, reset_reason=(\w+)")
REASON = re.compile(r"^# reset_reason=(\w+)")
INFO_QUIET = ("# columns", "# set ", "# limits", "# mode=", "# imu=", "# pressure=", "# i2c_hz",
              "# board=", "# firmware=", "# watchdog", "# controller=", "# view=", "# mux_ok",
              "# band=", "# cuff_zero", "# valve_pin", "# wifi=", "# ws_")


def open_quietly(port: str, baud: int) -> serial.Serial:
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, baud, 0.2
    s.dtr = False                    # set before open: no reset pulse on USB-CDC
    s.rts = False
    s.open()
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--out", type=Path, help="also append the log to this file")
    ap.add_argument("--all-info", action="store_true", help="print every INFO line, not just the useful ones")
    args = ap.parse_args(argv)
    out = args.out.open("a", encoding="utf-8") if args.out else None
    reasons: Counter = Counter()
    closures = 0
    boot_at: float | None = None
    lifetimes: list[float] = []
    first_reason_seen = False

    def say(text: str) -> None:
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + text
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    def restart(reason: str, how: str) -> None:
        nonlocal boot_at
        now = time.monotonic()
        up = ""
        if boot_at is not None:
            lifetimes.append(now - boot_at)
            up = f", previous boot lasted {lifetimes[-1]:.0f} s"
        boot_at = now
        reasons[reason] += 1
        say(f"RESTART reason={reason}{up}{how}")

    say(f"logging {args.port} (no reset); Ctrl+C to stop")
    s, buf = None, b""
    lost = False                     # the port went away: the board may have restarted
    ask_at = 0.0                     # when to ask for INFO after (re)opening
    try:
        while True:
            if s is None:
                try:
                    s = open_quietly(args.port, args.baud)
                    say("port open")
                    ask_at = time.monotonic() + 0.5
                except serial.SerialException:
                    time.sleep(0.5)
                    continue
            try:
                buf += s.read(4096)
                if ask_at and time.monotonic() >= ask_at:
                    s.write(b"INFO\n")
                    ask_at = 0.0
            except serial.SerialException:
                say("PORT LOST (board restarted, USB reset or unplugged)")
                try:
                    s.close()
                except Exception:
                    pass
                s, buf, lost = None, b"", True
                continue
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                ln = raw.decode("ascii", "replace").strip()
                if not ln or ln.startswith(DATA):
                    continue
                m = BOOT.search(ln)
                if m:
                    restart(m.group(1), "")
                    lost, first_reason_seen = False, True
                r = REASON.match(ln)
                if r and lost:                    # the boot line was missed while the port was away
                    restart(r.group(1), " (read from INFO after the port came back)")
                    lost = False
                elif r and not first_reason_seen:
                    say(f"last reset before logging started: reason={r.group(1)}")
                    first_reason_seen = True
                if "wifi_client_closed" in ln:
                    closures += 1
                if not args.all_info and ln.startswith(INFO_QUIET):
                    continue
                say(ln)
    except KeyboardInterrupt:
        pass
    say(f"summary: restarts {dict(reasons) or 'none'}; wifi_client_closed events {closures}; "
        f"boot lifetimes (s) {[round(x) for x in lifetimes] or 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
