"""Motion artifact detection.

Without a green reference channel (declined in D25), motion is separated from
blood volume by **speed** rather than by depth. That works because the two live
on different timescales:

* A motion or contact transient is fast -- the step change happens within a
  few hundred milliseconds, and we have measured them at 10,000+ counts.
* Venous filling is slow. The whole point of the measurement is a ramp
  developing over seconds, and the pipeline low-passes at 0.3 Hz to isolate it.

So a sample whose rate of change far exceeds what resting noise can produce is
an artifact, and the gap between the two regimes is wide enough that the
threshold is not delicate.

The flagged mask is used to *exclude* spans, never to repair them. Interpolating
across an artifact invents blood volume that was never measured, and a
return-to-flow onset sitting on invented data is worse than a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class MotionReport:
    mask: np.ndarray
    """True where the sample is inside a flagged artifact span."""

    fs: float
    n_events: int = 0
    flagged_pct: float = 0.0
    longest_event_s: float = 0.0
    threshold: float = float("nan")
    events: list[tuple[float, float]] = field(default_factory=list)
    """(start_s, end_s) for each flagged span."""

    @property
    def clean(self) -> np.ndarray:
        return ~self.mask

    def describe(self) -> str:
        if self.n_events == 0:
            return "no motion artifacts detected"
        return (f"{self.n_events} motion event(s), {self.flagged_pct:.1f}% of "
                f"samples flagged, longest {self.longest_event_s:.2f} s")


def _robust_sigma(x: np.ndarray) -> float:
    """MAD-based scale, which a few large transients cannot inflate.

    A plain standard deviation is dragged upward by the very artifacts we are
    trying to find, which would raise the threshold until they stop being
    detected.
    """
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    return float(mad * 1.4826)


def detect_motion(
    channels: dict[str, np.ndarray],
    fs: float,
    k: float = 8.0,
    dilate_s: float = 0.75,
    min_gap_s: float = 0.25,
    min_rate_pct_per_s: float = 2.0,
) -> MotionReport:
    """Flag spans moving faster than resting noise can account for.

    Parameters
    ----------
    k : threshold in robust sigmas of the per-sample fractional change.
    dilate_s : grow each flagged span by this much either side. A transient's
        edges matter as much as its peak, and the recovery tail is just as
        unusable as the spike.
    min_gap_s : merge flagged spans closer together than this, so one physical
        event is reported once rather than as a burst.
    min_rate_pct_per_s : absolute floor, as a percentage of DC per second. A
        span must be both statistically unusual *and* physically fast.

        The floor exists because the sigma test alone is relative: on a very
        quiet recording the robust sigma becomes tiny and ordinary noise
        clears 8 sigma, flagging a still arm. Measured on real captures, a
        clean 60 s still run never exceeds 1.3 %/s, while genuine arm movement
        reaches 17 %/s at the 95th percentile and 187 %/s at peak. A 2 %/s
        floor sits in that gap with room on both sides.
    """
    if not channels:
        raise ValueError("need at least one channel")

    n = len(next(iter(channels.values())))
    if n < 16:
        return MotionReport(mask=np.zeros(n, dtype=bool), fs=fs)

    # Fractional rate of change, so channels at different DC levels contribute
    # comparably and a bright channel does not dominate.
    combined = np.zeros(n)        # in robust sigmas
    rate_pct = np.zeros(n)        # in % of DC per second
    for v in channels.values():
        v = np.asarray(v, dtype=float)
        dc = float(np.median(v))
        if dc <= 0:
            continue
        d = np.abs(np.gradient(v, 1.0 / fs)) / dc
        rate_pct = np.maximum(rate_pct, d * 100.0)
        sigma = _robust_sigma(d)
        if sigma <= 0:
            continue
        combined = np.maximum(combined, d / sigma)

    # Both tests must agree: statistically unusual AND physically fast.
    mask = (combined > k) & (rate_pct > min_rate_pct_per_s)

    # Dilate, then merge nearby spans.
    if mask.any():
        w = max(int(round(dilate_s * fs)), 1)
        kernel = np.ones(2 * w + 1, dtype=int)
        mask = np.convolve(mask.astype(int), kernel, mode="same") > 0

        gap = max(int(round(min_gap_s * fs)), 1)
        idx = np.flatnonzero(mask)
        if idx.size:
            for a, b in zip(idx[:-1], idx[1:]):
                if 1 < b - a <= gap:
                    mask[a:b] = True

    # Summarise the spans.
    events: list[tuple[float, float]] = []
    if mask.any():
        edges = np.diff(mask.astype(int))
        starts = list(np.flatnonzero(edges == 1) + 1)
        ends = list(np.flatnonzero(edges == -1) + 1)
        if mask[0]:
            starts.insert(0, 0)
        if mask[-1]:
            ends.append(n)
        events = [(a / fs, b / fs) for a, b in zip(starts, ends)]

    return MotionReport(
        mask=mask,
        fs=fs,
        n_events=len(events),
        flagged_pct=float(mask.mean() * 100.0),
        longest_event_s=max((b - a for a, b in events), default=0.0),
        threshold=float(k),
        events=events,
    )


def clean_mean(x: np.ndarray, mask: np.ndarray) -> float:
    """Mean over unflagged samples, falling back to all if none survive."""
    keep = ~mask
    if not keep.any():
        return float(np.mean(x))
    return float(np.mean(np.asarray(x)[keep]))
