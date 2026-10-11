"""Cuff pressure from the MPRLS sensor (Adafruit 3965, Honeywell MPRLS0025PA00001A).

The firmware (firmware/mprls_test) streams raw 24-bit counts. The sensor reads
ABSOLUTE pressure, 0-25 psi; the cuff is measured relative to the room, so the
reading taken with the cuff open is subtracted (the zero). That also removes
most of the sensor's offset error, which is specified as a fraction of its
25 psi span and would otherwise be several mmHg.

Span accuracy is still unverified against a reference (D38's water column).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import signal

# Transfer function A: 10 % to 90 % of 2^24 counts spans Pmin..Pmax.
OUTPUT_MIN = 0x19999A          # 1 677 722
OUTPUT_MAX = 0xE66666          # 15 099 494
P_MIN_PSI = 0.0
P_MAX_PSI = 25.0
MMHG_PER_PSI = 51.71493

# Status byte, datasheet: bit 6 powered, bit 5 busy, bit 2 integrity, bit 0 saturation.
ST_POWERED = 0x40
ST_BUSY = 0x20
ST_INTEGRITY = 0x04
ST_SATURATED = 0x01


def counts_to_mmhg(counts):
    """Absolute pressure in mmHg from raw counts."""
    c = np.asarray(counts, dtype=float)
    psi = (c - OUTPUT_MIN) * (P_MAX_PSI - P_MIN_PSI) / (OUTPUT_MAX - OUTPUT_MIN) + P_MIN_PSI
    return psi * MMHG_PER_PSI


def status_problem(status: int) -> str | None:
    """Why a sample must be discarded, or None."""
    if not status & ST_POWERED:
        return "sensor reports no power"
    if status & ST_SATURATED:
        return "reading saturated"
    return None


def status_warning(status: int) -> str | None:
    """A flag worth showing that does not, on the evidence, spoil the reading.

    The memory-integrity bit (a checksum the sensor runs at power-up) was set
    on every sample of the 2026-10-11 runs with the MPRLS sharing the bus with
    the PPG, yet the readings tracked the cuff cleanly (0 -> 129 -> 7 mmHg,
    ~1 mmHg noise). Discarding them blanked the display. Readings are kept
    and the flag is shown; power-cycling the board is the first thing to try.
    """
    if status & ST_INTEGRITY:
        return "sensor integrity flag set: readings kept; unplug USB to power-cycle"
    return None


def parse_line(line: str) -> tuple[int, int, int] | None:
    """(ms, counts, status) from an 'M,...' line, else None."""
    if not line.startswith("M,"):
        return None
    try:
        _, ms, counts, status = line.strip().split(",")
        return int(ms), int(counts), int(status)
    except ValueError:
        return None


@dataclass
class PressureState:
    t: float               # session seconds of the latest sample
    mmhg: float            # cuff pressure relative to the zero
    abs_mmhg: float        # absolute pressure
    zero_mmhg: float | None
    rate: float            # mmHg/s over the last RATE_WINDOW_S; negative = deflating
    peak: float            # highest cuff pressure since the last zero
    oscillation: float     # peak-to-peak of the 0.5-5 Hz band, 3 s ending 0.5 s ago, mmHg
    problem: str | None
    warning: str | None = None   # a flag shown but not disqualifying


class PressureTracker:
    """Rolling buffer of cuff pressure with zeroing, rate and oscillation."""

    RATE_WINDOW_S = 2.0
    AUTO_ZERO_S = 2.0          # zero on the first 2 s after connect (cuff open)
    OSC_BAND = (0.5, 5.0)

    def __init__(self, fs: float = 100.0, history_s: float = 900.0):
        self.fs = fs
        n = int(history_s * fs)
        self.t = deque(maxlen=n)
        self.abs = deque(maxlen=n)
        self.zero: float | None = None
        self.peak = 0.0
        self.problem: str | None = None
        self.warning: str | None = None
        self._ms0: int | None = None
        self._t0 = 0.0
        self._sos = signal.butter(2, [self.OSC_BAND[0] / (fs / 2), self.OSC_BAND[1] / (fs / 2)],
                                  btype="band", output="sos")

    def start(self, session_t: float) -> None:
        """A new link: device milliseconds restart, the zero is re-taken."""
        self._ms0 = None
        self._t0 = session_t
        self.zero = None
        self.peak = 0.0

    def push(self, ms: int, counts: int, status: int) -> None:
        """A reading stamped with the board's millis() (firmware/mprls_test)."""
        if status_problem(status) is None and self._ms0 is None:
            self._ms0 = ms
        t = self._t0 + (ms - (self._ms0 if self._ms0 is not None else ms)) / 1000.0
        self.push_at(t, counts, status)

    def push_at(self, t: float, counts: int, status: int) -> None:
        """A reading at session time t (the combined PPG + pressure stream)."""
        self.problem = status_problem(status)
        self.warning = status_warning(status)
        if self.problem:
            return
        self.t.append(t)
        self.abs.append(float(counts_to_mmhg(counts)))
        if self.zero is None and t - self._t0 >= self.AUTO_ZERO_S:
            self.set_zero(self.AUTO_ZERO_S)
        if self.zero is not None:
            self.peak = max(self.peak, self.abs[-1] - self.zero)

    def set_zero(self, over_s: float = 1.0) -> float | None:
        """Take the mean of the last over_s seconds as atmospheric."""
        if not self.t:
            return None
        t = np.asarray(self.t)
        a = np.asarray(self.abs)
        sel = t >= t[-1] - over_s
        self.zero = float(a[sel].mean())
        self.peak = 0.0
        return self.zero

    def arrays(self, span_s: float | None = None):
        t = np.asarray(self.t)
        a = np.asarray(self.abs)
        if span_s is not None and len(t):
            sel = t >= t[-1] - span_s
            t, a = t[sel], a[sel]
        rel = a - self.zero if self.zero is not None else np.full_like(a, np.nan)
        return t, rel, a

    def oscillations(self, span_s: float = 12.0):
        """0.5-5 Hz band of cuff pressure: the heartbeat seen through the cuff."""
        t, _rel, a = self.arrays(span_s)
        if len(a) < 3 * 13:
            return t, np.zeros_like(a)
        return t, signal.sosfiltfilt(self._sos, a - a.mean())

    def state(self) -> PressureState | None:
        if not self.t:
            return None
        t, rel, a = self.arrays(max(self.RATE_WINDOW_S, 3.0))
        recent = t >= t[-1] - self.RATE_WINDOW_S
        rate = float(np.polyfit(t[recent], a[recent], 1)[0]) if recent.sum() >= 5 else 0.0
        # Filter 12 s and measure 3 s ending 0.5 s back, clear of the filter's
        # edge effects at both ends.
        to, osc = self.oscillations(12.0)
        win = (to >= to[-1] - 3.5) & (to <= to[-1] - 0.5) if len(to) else np.zeros(0, bool)
        return PressureState(
            t=float(t[-1]), mmhg=float(rel[-1]), abs_mmhg=float(a[-1]), zero_mmhg=self.zero,
            rate=rate, peak=self.peak, oscillation=float(np.ptp(osc[win])) if win.sum() else 0.0,
            problem=self.problem, warning=self.warning,
        )
