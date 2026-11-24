"""Laptop receiver for the expo board: watch the wireless stream and grade it.

Join the board's Wi-Fi network first (expo/README.md), then:

    python expo/host/expo_receiver.py                      # live, Ctrl-C for the report
    python expo/host/expo_receiver.py --seconds 120        # fixed-length link check
    python expo/host/expo_receiver.py --save link.txt      # keep every line received
    python expo/host/expo_receiver.py --url ws://127.0.0.1:8765/ws    # the local sim server

It only watches: it never takes control, so it can run beside the iPad.
One status line per second; at the end, a pass/fail report against the rates
the firmware is built for, with missing samples, the board's own buffer
overflow count and the ECHO round trip. Exit code 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expotool.linkstats import LinkStats  # noqa: E402
from expotool.ws import WSClient  # noqa: E402

DEFAULT_URL = "ws://192.168.4.1/ws"


def run(url: str, seconds: float | None, save: Path | None, quiet: bool, out=print) -> LinkStats:
    out(f"connecting to {url} ...")
    ws = WSClient(url)
    out("connected; watching (Ctrl-C to stop)")
    stats = LinkStats()
    fh = None
    if save:
        save.parent.mkdir(parents=True, exist_ok=True)
        fh = open(save, "w", encoding="utf-8", newline="\n")
        fh.write(f"# saved_by=expo_receiver url={url} started={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    echo_sent: dict[int, float] = {}
    n_echo = 0
    t0 = time.monotonic()
    next_ping = next_echo = next_print = t0
    try:
        ws.send_text("INFO")
        while seconds is None or time.monotonic() - t0 < seconds:
            now = time.monotonic()
            if now >= next_ping:
                ws.send_text("PING")
                next_ping = now + 1.0
            if now >= next_echo and stats.client_id:
                n_echo += 1
                echo_sent[n_echo] = now
                ws.send_text(f"ECHO {n_echo}")
                next_echo = now + 2.0
            if now >= next_print and not quiet and stats.elapsed > 0:
                out(stats.summary_line())
                next_print = now + 1.0
            msg = ws.recv(timeout=0.1)
            if msg is None:
                continue
            arrived = time.monotonic()
            stats.message(msg, arrived)
            if fh:
                fh.write(msg if msg.endswith("\n") else msg + "\n")
            for line in msg.split("\n"):
                if line.startswith("# echo=") and f"client={stats.client_id}" in line:
                    n = int(line.split("=", 1)[1].split()[0])
                    if n in echo_sent:
                        stats.echo((arrived - echo_sent.pop(n)) * 1000.0)
    except KeyboardInterrupt:
        pass
    except ConnectionError as e:
        out(f"LINK LOST: {e}")
    finally:
        ws.close()
        if fh:
            fh.close()
    return stats


def report(stats: LinkStats, out=print) -> bool:
    out("")
    out(f"link report: {stats.elapsed:.1f} s, {stats.messages} frames, {stats.bytes / 1000:.1f} kB")
    ok_all = True
    for c in stats.checks():
        ok_all &= c.ok
        out(f"  {'PASS' if c.ok else 'FAIL'}  {c.name:22s} {c.detail}")
    interesting = [e for e in stats.events if e[0] not in ("state",)]
    if interesting:
        out("  events: " + "; ".join(f"{n} {d}".strip() for n, d in interesting[-8:]))
    if stats.errors:
        out("  errors: " + "; ".join(stats.errors[-5:]))
    out("RESULT: " + ("PASS" if ok_all else "FAIL"))
    return ok_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Watch and grade the expo board's wireless stream.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"WebSocket URL (default {DEFAULT_URL})")
    ap.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl-C)")
    ap.add_argument("--save", type=Path, help="write every received line to this file")
    ap.add_argument("--quiet", action="store_true", help="no per-second status lines")
    args = ap.parse_args(argv)
    try:
        stats = run(args.url, args.seconds, args.save, args.quiet)
    except OSError as e:
        print(f"could not connect to {args.url}: {e}")
        print("Is this computer on the board's Wi-Fi network (expo/README.md)?")
        return 2
    return 0 if report(stats) else 1


if __name__ == "__main__":
    sys.exit(main())
