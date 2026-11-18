"""Link statistics for the expo stream: rates, missing samples, round trip.

Feed every received WebSocket message to ``LinkStats.message``. It counts
lines per stream, checks each stream's own sample counter for gaps, keeps
the board's ST status and the ECHO round trips, and grades the link against
the rates the firmware is built for.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from irbtool.protocol import Control, Distal, Error, Event, Imu, Meta, Proximal, parse_line

# Lines per second the board sends (IRB firmware rates; ST is expo-only).
EXPECTED_HZ = {"S": 100.0, "SP": 100.0, "MD": 50.0, "MP": 50.0, "C": 10.0, "ST": 2.0}
RATE_TOLERANCE = 0.03       # +/- 3 % over the whole capture
RTT_LIMIT_MS = 250.0        # median ECHO round trip, including up to 40 ms of batching


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class LinkStats:
    started: float | None = None
    last: float | None = None
    bytes: int = 0
    messages: int = 0
    lines: dict = field(default_factory=dict)          # stream -> count
    missing: dict = field(default_factory=dict)        # stream -> samples missing
    unknown: int = 0
    errors: list = field(default_factory=list)
    events: list = field(default_factory=list)         # (name, detail)
    rtts_ms: list = field(default_factory=list)
    status: dict = field(default_factory=dict)         # latest ST fields
    state: str = "?"
    p_mmhg: float = float("nan")
    client_id: int = 0
    _last_idx: dict = field(default_factory=dict)

    def message(self, text: str, now: float) -> None:
        if self.started is None:
            self.started = now
        self.last = now
        self.bytes += len(text)
        self.messages += 1
        for line in text.split("\n"):
            line = line.strip()
            if line:
                self.line(line, now)

    def _count(self, key: str, idx: int | None = None) -> None:
        self.lines[key] = self.lines.get(key, 0) + 1
        if idx is None:
            return
        prev = self._last_idx.get(key)
        if prev is not None and idx != prev + 1:
            self.missing[key] = self.missing.get(key, 0) + max(0, idx - prev - 1)
        self._last_idx[key] = idx

    def line(self, line: str, now: float) -> None:
        if line.startswith("ST,"):
            self._status(line)
            return
        rec = parse_line(line)
        if isinstance(rec, Distal):
            self._count("S", rec.idx)
        elif isinstance(rec, Proximal):
            self._count("SP", rec.idx)
        elif isinstance(rec, Imu):
            self._count("M" + rec.band, rec.m_idx)
        elif isinstance(rec, Control):
            self._count("C", rec.c_idx)
            self.state, self.p_mmhg = rec.state, rec.p_mmhg
        elif isinstance(rec, Event):
            self._count("EV")
            self.events.append((rec.name, rec.detail))
            if rec.name == "state":
                self.state = rec.detail
        elif isinstance(rec, Error):
            self._count("E")
            self.errors.append(rec.message)
        elif isinstance(rec, Meta):
            self._count("meta")
            if "client_id" in rec.pairs:
                self.client_id = int(rec.pairs["client_id"])
        else:
            self.unknown += 1

    def _status(self, line: str) -> None:
        from .hub import ST_COLUMNS
        f = line.split(",")[1:]
        if len(f) != len(ST_COLUMNS):
            self.unknown += 1
            return
        self._count("ST")
        self.status = dict(zip(ST_COLUMNS, f))
        self.state = self.status["state"]

    def echo(self, rtt_ms: float) -> None:
        self.rtts_ms.append(rtt_ms)

    # -- reporting ---------------------------------------------------------

    @property
    def elapsed(self) -> float:
        if self.started is None or self.last is None:
            return 0.0
        return self.last - self.started

    def rate(self, key: str) -> float:
        return self.lines.get(key, 0) / self.elapsed if self.elapsed > 0 else 0.0

    def checks(self) -> list[Check]:
        out = []
        t = self.elapsed
        for key, hz in EXPECTED_HZ.items():
            n = self.lines.get(key, 0)
            got = self.rate(key)
            ok = t > 0 and abs(got - hz) <= hz * RATE_TOLERANCE
            out.append(Check(f"{key} rate", ok, f"{got:.2f} Hz (expect {hz:g}, {n} lines in {t:.1f} s)"))
        lost = sum(self.missing.values())
        out.append(Check("missing samples", lost == 0,
                         ", ".join(f"{k} {v}" for k, v in self.missing.items()) or "none"))
        dropped = int(self.status.get("ws_dropped_bytes", "0") or 0)
        out.append(Check("board buffer overflow", dropped == 0, f"{dropped} bytes dropped on the board"))
        if self.rtts_ms:
            med = statistics.median(self.rtts_ms)
            out.append(Check("round trip", med <= RTT_LIMIT_MS,
                             f"median {med:.0f} ms, worst {max(self.rtts_ms):.0f} ms over {len(self.rtts_ms)}"))
        else:
            out.append(Check("round trip", False, "no ECHO replies"))
        out.append(Check("unparsed lines", self.unknown == 0, str(self.unknown)))
        return out

    def summary_line(self) -> str:
        r = self.rate
        rtt = f"{statistics.median(self.rtts_ms[-10:]):.0f} ms" if self.rtts_ms else "--"
        kbs = self.bytes / self.elapsed / 1000 if self.elapsed > 0 else 0.0
        lost = sum(self.missing.values())
        return (f"S {r('S'):5.1f}  SP {r('SP'):5.1f}  M {r('MD'):4.1f}/{r('MP'):4.1f}  C {r('C'):4.1f}  "
                f"ST {r('ST'):3.1f} Hz | {kbs:5.1f} kB/s | rtt {rtt} | missing {lost} | "
                f"{self.state} {self.p_mmhg:6.1f} mmHg")
