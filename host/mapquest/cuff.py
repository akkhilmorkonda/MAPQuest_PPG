"""Trial windows from the cuff pressure trace (D43).

Shared by cuff_trial.py and analyze.py, so both start their onset searches
from the same place.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import median_filter

DEFLATION_DROP_MMHG = 20.0
HOLD_SKIP_S = 3.0
MIN_HOLD_S = 8.0
REF_S = 5.0
DUMP_RATE_MMHG_S = 10.0


@dataclass
class Windows:
    inflation: float
    """Last time below 10 mmHg before the peak."""
    peak: float
    hold: tuple[float, float]
    """Occluded stretch for the pulse-return floor: 3 s after the peak (clear
    of the inflation transient) until deflation."""
    deflation: float
    """First time 20 mmHg below the peak, at least 8 s after it. The onset
    searches start here."""
    ref: tuple[float, float]
    """The 5 s before deflation: the refill-onset noise floor."""
    release: float
    """First time below 20 mmHg after the peak."""
    source: str = "auto"
    x: tuple[float, float] | None = None
    motion: tuple = ()
    dump: float | None = None
    """Start of the final rapid release: the unbroken stretch, ending at the
    release, where the median-filtered pressure falls faster than 10 mmHg/s.
    Searches after the onset stop here. An automated monitor
    dumps the cuff the moment it has its reading, often near diastolic, and
    the dump itself is the steepest volume change in the record (D45)."""


def windows_from_pressure(t_p: np.ndarray, p_mmhg: np.ndarray) -> Windows:
    """Find the trial's phases on a 10 Hz copy of the trace, median-filtered
    over 1 s so a single noisy reading cannot move them.

    A bare "first falling sample" picks the first noise tick of the MPRLS at
    the start of the recording; a drop from the peak cannot be faked that way.
    """
    tg = np.arange(t_p[0], t_p[-1], 0.1)
    pg = median_filter(np.interp(tg, t_p, p_mmhg), 11)
    ip = int(np.argmax(pg))
    peak = float(tg[ip])
    below = np.flatnonzero(pg[:ip] < 10.0)
    inflation = float(tg[below[-1]]) if below.size else float(tg[0])
    rel = np.flatnonzero(pg[ip:] < 20.0)
    release = float(tg[ip + rel[0]]) if rel.size else float(tg[-1])
    drop = np.flatnonzero(pg[ip:] < pg[ip] - DEFLATION_DROP_MMHG)
    deflation = float(tg[ip + drop[0]]) if drop.size else release
    deflation = min(max(deflation, peak + MIN_HOLD_S), release)
    # The dump is the unbroken fast fall that ends at the release, traced back
    # from there; a quick valve step mid-deflation is not part of it.
    i0 = int(np.searchsorted(tg, deflation))
    falling = np.gradient(pg, tg) < -DUMP_RATE_MMHG_S
    j = int(np.searchsorted(tg, release))
    while j > i0 and falling[j - 1]:
        j -= 1
    dump = float(tg[j])
    return Windows(inflation, peak, (peak + HOLD_SKIP_S, deflation), deflation,
                   (deflation - REF_S, deflation), release, dump=min(dump, release))
