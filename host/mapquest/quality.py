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


@dataclass
class SlipReport:
    """Per-phase coupling slips found by ``detect_slips``."""

    slipped: dict[int, float]
    """phase index -> largest window-to-window level jump, % of DC."""

    times: dict[int, float]
    """phase index -> time of that jump, s."""

    threshold_pct: float

    def describe(self, keys: list[str]) -> str:
        if not self.slipped:
            return "no coupling slips during holds"
        parts = [f"{keys[i]} at {self.times[i]:.1f} s ({v:+.1f}%)"
                 for i, v in sorted(self.slipped.items())]
        return "coupling slipped mid-hold: " + ", ".join(parts)


def detect_slips(
    channels: dict[str, np.ndarray],
    phase_of: np.ndarray,
    fs: float,
    allow_s: float = 5.0,
    window_s: float = 1.0,
    threshold_pct: float = 1.0,
) -> SlipReport:
    """Find sudden level changes inside a hold, after the transition settles.

    The motion detector cannot catch this on its own: on 2026-10-09 an
    arm-raise and a slip 6.5 s later were one continuous flagged span, so the
    slip was filed as part of the transition. A slip is better defined by what
    it does to the level than by when the flagged span started.

    Within a hold, once ``allow_s`` has passed, real blood-volume change is
    gradual: an exponential settle with a time constant of seconds moves the
    level ~0.1 %/s. The sensor shifting on the skin moves it several percent in
    under a second. Comparing medians of consecutive ``window_s`` windows
    separates the two with a wide margin, and medians shrug off the spikes
    that accompany the slip itself.

    All channels are checked, because a tilt can change red and IR coupling
    differently -- their LEDs sit at different positions in the package.
    """
    slipped: dict[int, float] = {}
    times: dict[int, float] = {}
    w = max(int(round(window_s * fs)), 2)
    step = max(w // 4, 1)

    for i in np.unique(phase_of):
        idx = np.flatnonzero(phase_of == i)
        lo = idx[0] + int(round(allow_s * fs))
        hi = idx[-1] - int(round(0.5 * fs))
        if hi - lo < 2 * w:
            continue
        worst, worst_t = 0.0, float("nan")
        for v in channels.values():
            v = np.asarray(v, dtype=float)
            dc = float(np.median(v[idx]))
            if dc <= 0:
                continue
            for s in range(lo, hi - 2 * w + 1, step):
                a = float(np.median(v[s:s + w]))
                b = float(np.median(v[s + w:s + 2 * w]))
                jump = (b - a) / dc * 100.0
                if abs(jump) > abs(worst):
                    worst, worst_t = jump, (s + w) / fs
        if abs(worst) > threshold_pct:
            slipped[int(i)] = worst
            times[int(i)] = worst_t

    return SlipReport(slipped=slipped, times=times, threshold_pct=threshold_pct)
