"""Return-to-flow analysis pipeline (docs/SPEC.md section 5).

The measurement: an upper-arm cuff is inflated above arterial pressure, then
bled down at ~2 mmHg/s. SPEC assumes that while the cuff is above systolic the
forearm's blood volume is frozen; on a real arm it drains slowly instead (O5),
which is why onset uses a sloped baseline (D39). Once the cuff falls below
arterial pressure but stays above venous pressure, blood flows in and cannot
drain, so the forearm fills, reflected light falls, and delta-OD rises. MAP is read off as the cuff pressure at the
onset of sustained filling.

The signal of interest is therefore the SLOW DC trend, not the pulsatile
waveform. Every stage is retained so the chain can be inspected end to end:

    raw counts
      -> dark-subtracted
      -> delta-OD        ln(ref / counts), per wavelength
      -> delta V*L       two-wavelength Beer-Lambert solve, in micrometres
      -> low-pass        0.3 Hz Butterworth, zero-phase
      -> dV/dt           Savitzky-Golay, 2.0 s window
      -> onset seed      first sustained excursion above max(mu0, 0) + k*sigma0
      -> onset           filling ramp intersected with the sloped occlusion trend

Two choices worth knowing about:

* **Zero-phase filtering** (``filtfilt``). A causal filter group-delays the
  signal, which would bias the onset time and therefore the reported pressure.
  Offline we can afford to be non-causal and avoid that entirely. The real-time
  variant (SPEC 5.3) has to subtract a known delay instead.

* **A 2.0 s derivative window.** The HeartMate 3 artificial pulse repeats every
  2 s, so a window of exactly one period cancels it and its harmonics. This
  keeps the same pipeline valid on LVAD data without re-tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

# Two-wavelength Beer-Lambert inversion, SPEC 5.1 step 6. Coefficients convert
# per-wavelength optical density change into blood volume per unit area,
# expressed as a path-length-scaled volume dV*L in micrometres.
COEFF_RED_660 = 229.0
COEFF_IR_880 = 1555.0

# IR-only fallback: dV*L = delta-OD_880 / 6.13 cm^-1 (SPEC 5.1 step 6).
#
# That expression yields CENTIMETRES, while the two-wavelength solve above
# yields MICROMETRES. Mixing them unconverted would put the two paths 1e4
# apart, so the fallback is converted to um to keep one unit across the
# pipeline. Note 1e4/6.13 = 1631 um/OD lands close to the two-wavelength
# COEFF_IR_880 of 1555, which is the consistency check that the conversion is
# the right way round: the fallback is the two-wavelength formula with the red
# term dropped, not a different quantity.
MU_A_IR_PER_CM = 6.13
UM_PER_CM = 1.0e4

# (key, label, unit) for each retained stage, in pipeline order.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("raw", "0. Raw counts", "counts"),
    ("dark_sub", "1. Dark-subtracted", "counts"),
    ("d_od", "2. delta-OD", "OD"),
    ("dv", "3. delta V*L", "um"),
    ("dv_lp", "4. Low-pass 0.3 Hz", "um"),
    ("ddv", "5. d(V*L)/dt", "um/s"),
)


@dataclass
class PipelineConfig:
    """Tunable parameters. Defaults follow SPEC 5.1."""

    lowpass_hz: float = 0.3
    """Butterworth corner for the volume signal."""

    filter_order: int = 2

    sg_window_s: float = 2.0
    """Savitzky-Golay window. One HeartMate 3 artificial-pulse period."""

    sg_polyorder: int = 2

    k_sigma: float = 3.0
    """Onset threshold in noise-floor standard deviations."""

    hold_s: float = 2.0
    """Excursion must be sustained this long to count as onset."""

    onset_method: str = "hinge"
    """``"threshold"`` is SPEC 5.1 step 10 verbatim: the first sample of a
    sustained excursion above mu0 + k*sigma0.

    That method is biased EARLY, and the bias is structural rather than noise.
    Both the 0.3 Hz zero-phase low-pass and the 2 s Savitzky-Golay window are
    symmetric kernels, so they smear the ramp's corner backwards in time as
    well as forwards. The derivative therefore starts climbing roughly half a
    kernel before the true onset, and at 2 mmHg/s that reads as a few mmHg too
    high -- measured at +2.6 mmHg with the SPEC defaults, against a SPEC 6
    acceptance criterion of +/-1 mmHg.

    ``"intersect"`` removes that bias: it fits the established filling ramp and
    extrapolates back to where it meets the occlusion baseline. Because it uses
    the ramp's slope and offset rather than the location of its corner, corner
    smearing cancels. This is the default because it is the one that meets the
    acceptance criterion; pass ``--onset-method threshold`` for the literal
    SPEC behaviour.

    ``"hinge"`` (the default since D43) fits "trend, then rising" in one go
    and takes the corner. On a sharp ramp it agrees with the intersect; when
    filling starts softly -- slow deflation past a weak pulse, as in the cuff
    trials -- a straight ramp fitted to the curve meets the baseline late, so
    the intersect reads low (by 4-5 mmHg on synthetic curved fills, against
    +/-0.5 for the hinge). The intersect is still computed and reported."""

    fit_s: float = 5.0
    """Length of the established-ramp segment fitted by the intersect method."""

    baseline: str = "sloped"
    """What the intersect method extrapolates the ramp back to (D39, O5).

    ``"flat"`` is D10 as first built: the mean level of the reference window.
    That assumes blood volume is frozen under occlusion. On both real cuff
    trials it was not -- it fell steadily -- and a rising ramp meets a level
    left behind by a falling baseline LATE, so the pressure reads LOW: 11 mmHg
    on the 2026-10-09 20:35 trial.

    ``"sloped"`` fits a line to the occlusion trend just before the threshold
    crossing and intersects line with line. On a flat baseline the fitted
    slope is ~0 and it reduces to ``"flat"``; on a sloped one it finds the
    corner wherever the reference window sits."""

    baseline_s: float = 10.0
    """Length of the pre-onset trend fitted by the sloped baseline."""

    baseline_guard_s: float = 2.0
    """Gap between the end of the trend fit and the threshold crossing. The
    zero-phase low-pass smears the ramp's corner backwards in time; fitting
    into that smear would tilt the trend up and pull the onset late."""

    floor_mu0: bool = True
    """Onset requires NET filling: threshold = max(mu0, 0) + k*sigma0.

    Under occlusion nothing can make volume rise, but a drain that is slowing
    down makes dV/dt rise towards zero by itself. With mu0 taken where the
    drain is steepest (the SPEC default window, right after inflation), the
    literal mu0 + k*sigma0 is crossed by that deceleration alone -- a false
    onset. Flooring mu0 at zero changes nothing when mu0 >= 0."""

    ir_only: bool = False
    """Use the single-wavelength fallback instead of the two-wavelength solve."""

    pulsatility_band: tuple[float, float] = (0.5, 5.0)
    """Band for the informational pulsatility check (SPEC 5.1 step 12)."""


@dataclass
class Derivation:
    """All retained stages plus the derived result."""

    fs: float
    t: np.ndarray
    raw: dict[str, np.ndarray]
    dark_sub: dict[str, np.ndarray]
    d_od: dict[str, np.ndarray]
    dv: np.ndarray
    dv_lp: np.ndarray
    ddv: np.ndarray
    ref_slice: slice
    mu0: float
    sigma0: float
    threshold: float
    onset_index: int | None
    config: PipelineConfig = field(default_factory=PipelineConfig)

    # Both estimators are always computed so the two can be compared on any
    # recording. `onset_index`/`onset_time` follow config.onset_method.
    threshold_index: int | None = None
    onset_time_intersect: float | None = None
    ramp_slope: float = float("nan")
    ramp_fit: tuple[float, float] | None = None
    """(slope, intercept) of the fitted ramp in dv_lp, for plotting."""
    baseline_fit: tuple[float, float] | None = None
    """(slope, intercept) of the baseline the ramp was intersected with. Slope
    0 and the reference-window mean for ``baseline="flat"``."""
    baseline_span: tuple[float, float] | None = None
    """(start_s, end_s) of the data the sloped baseline was fitted to."""
    onset_time_flat: float | None = None
    onset_time_sloped: float | None = None
    """Both baselines are always computed, so the difference stays visible.
    `onset_time_intersect` is whichever config.baseline selects."""
    onset_time_hinge: float | None = None
    hinge_power: float | None = None
    """1 = sharp corner, 2 = soft (parabolic) start; chosen by fit (D43)."""
    hinge_fit: tuple | None = None
    """(b0, b1, a, ws): y = b0 + b1*(t-ws) + a*max(0, t-t0)**p, for plotting."""

    @property
    def onset_time(self) -> float | None:
        if self.config.onset_method == "intersect":
            return self.onset_time_intersect
        if self.config.onset_method == "hinge":
            return self.onset_time_hinge
        return None if self.threshold_index is None else float(self.t[self.threshold_index])

    @property
    def threshold_time(self) -> float | None:
        return None if self.threshold_index is None else float(self.t[self.threshold_index])

    def stage(self, key: str):
        return getattr(self, key)


def _zero_phase_lowpass(x: np.ndarray, fs: float, cutoff: float, order: int) -> np.ndarray:
    sos = signal.butter(order, cutoff / (fs / 2.0), btype="low", output="sos")
    default_pad = 3 * (2 * len(sos) + 1)
    padlen = min(default_pad, max(len(x) // 3 - 1, 0))
    return signal.sosfiltfilt(sos, x, padlen=padlen)


def _sg_window_samples(cfg: PipelineConfig, fs: float, n: int) -> int:
    w = int(round(cfg.sg_window_s * fs))
    if w % 2 == 0:
        w += 1
    w = max(w, cfg.sg_polyorder + 2 + (cfg.sg_polyorder % 2))
    if w > n:
        w = n if n % 2 else n - 1
    return w


def delta_od(counts: np.ndarray, reference: float) -> np.ndarray:
    """delta-OD = ln(reference / counts). Rises as the tissue darkens.

    Counts are clamped away from zero first: a dark-subtracted count can land
    at or below zero on a noisy sample, and log of that would poison the whole
    downstream chain with inf/nan rather than failing visibly.
    """
    safe = np.maximum(counts, 1e-9)
    return np.log(max(reference, 1e-9) / safe)


def blood_volume(d_od: dict[str, np.ndarray], ir_only: bool) -> np.ndarray:
    """Convert per-wavelength delta-OD into dV*L in micrometres (SPEC 5.1 step 6)."""
    if ir_only or "red" not in d_od:
        return d_od["ir"] / MU_A_IR_PER_CM * UM_PER_CM
    return COEFF_RED_660 * d_od["red"] + COEFF_IR_880 * d_od["ir"]


def find_onset(
    ddv: np.ndarray,
    fs: float,
    search_from: int,
    mu0: float,
    sigma0: float,
    k: float,
    hold_s: float,
) -> tuple[int | None, float]:
    """First sample after ``search_from`` where dV/dt stays above threshold.

    Returns ``(index_of_run_start, threshold)``. The index is the FIRST sample
    of the qualifying run, not the end of it -- the cuff pressure there is the
    reported result (SPEC 5.1 step 11).
    """
    threshold = mu0 + k * sigma0
    hold_n = max(int(round(hold_s * fs)), 1)

    above = ddv > threshold
    above[:search_from] = False
    if not above.any():
        return None, threshold

    # Find a run of `hold_n` consecutive True values via a cumulative-sum
    # window, which is O(n) rather than scanning each candidate start.
    c = np.concatenate(([0], np.cumsum(above.astype(np.int32))))
    if len(above) < hold_n:
        return None, threshold
    run_sums = c[hold_n:] - c[:-hold_n]
    starts = np.flatnonzero(run_sums == hold_n)
    if starts.size == 0:
        return None, threshold
    return int(starts[0]), threshold


def fit_baseline(
    dv_lp: np.ndarray,
    t: np.ndarray,
    fs: float,
    ref_slice: slice,
    crossing_index: int,
    baseline_s: float,
    guard_s: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Line through the occlusion trend just before the threshold crossing.

    The window ends ``guard_s`` before the crossing, to stay clear of the
    corner the low-pass smears backwards, and runs ``baseline_s`` back from
    there -- but never earlier than the reference window, the earliest data
    known to be under occlusion. If that leaves under 3 s, the reference window
    itself is fitted instead.

    Returns ``((slope, intercept), (start_s, end_s))``.
    """
    end = crossing_index - int(round(guard_s * fs))
    start = max(ref_slice.start, end - int(round(baseline_s * fs)))
    if end - start < int(3.0 * fs):
        start, end = ref_slice.start, ref_slice.stop
    slope, intercept = np.polyfit(t[start:end], dv_lp[start:end], 1)
    return (float(slope), float(intercept)), (float(t[start]), float(t[end - 1]))


def ramp_intersection(
    dv_lp: np.ndarray,
    t: np.ndarray,
    fs: float,
    ref_slice: slice,
    crossing_index: int,
    hold_s: float,
    fit_s: float,
    baseline: tuple[float, float] | None = None,
) -> tuple[float | None, float, tuple[float, float] | None]:
    """Extrapolate the established filling ramp back to the baseline.

    The threshold crossing tells us roughly where filling began; this refines
    it. We fit a straight line to the part of the ramp that is unambiguously
    established -- starting one hold period after the crossing, so the fit
    never includes the smeared corner -- and intersect that line with the
    baseline: ``(slope, intercept)`` of the occlusion trend, or by default the
    flat mean level of the occlusion reference window (D10).

    Corner smearing shifts WHERE the signal starts to bend, but not the slope
    or offset of the straight parts either side of it, so the intersection is
    unbiased by the filtering.

    Returns ``(onset_time, slope, (slope, intercept))``.
    """
    if baseline is None:
        baseline = (0.0, float(np.mean(dv_lp[ref_slice])))
    b_slope, b_intercept = baseline

    i0 = crossing_index + int(round(hold_s * fs))
    i1 = min(i0 + int(round(fit_s * fs)), len(dv_lp))
    if i1 - i0 < 8:
        # Too little ramp left to fit; fall back to whatever remains.
        i0 = min(crossing_index, len(dv_lp) - 8)
        i1 = len(dv_lp)
        if i1 - i0 < 8:
            return None, float("nan"), None

    slope, intercept = np.polyfit(t[i0:i1], dv_lp[i0:i1], 1)
    if slope <= max(b_slope, 0.0):
        return None, float(slope), (float(slope), float(intercept))

    onset_t = (b_intercept - intercept) / (slope - b_slope)

    # The extrapolation must land inside the record and at or after the
    # reference window; otherwise the ramp fit was not describing real filling.
    lo, hi = float(t[ref_slice.stop - 1]), float(t[-1])
    if not (lo - 1.0 <= onset_t <= hi):
        return None, float(slope), (float(slope), float(intercept))

    return float(onset_t), float(slope), (float(slope), float(intercept))


def hinge_onset(
    dv_lp: np.ndarray,
    t: np.ndarray,
    ref_slice: slice,
    crossing_index: int,
    before_s: float = 12.0,
    after_s: float = 6.0,
    powers: tuple[float, ...] = (1.0, 2.0),
    step_s: float = 0.05,
    decimate: int = 5,
):
    """Onset as the corner of a "trend, then rising" fit (D43).

    Model: y = b0 + b1*(t - ws) + a*max(0, t - t0)**p, with a > 0. The trend
    term is the occlusion baseline (sloped, as D39); the hinge term is the
    filling. p = 1 is a sharp corner -- the same geometry as the ramp
    intersection. p = 2 is the soft start physics predicts when inflow grows
    with how far the cuff has fallen below the opening pressure; a straight
    ramp fitted to that curve meets the baseline late, reading low (16.5 mmHg
    low on 2026-10-17 test6 by the reference). t0 and p are chosen by least
    squares over the window around the threshold crossing.

    The 0.3 Hz zero-phase low-pass smooths the corner symmetrically, which
    leaves the least-squares corner of a sharp hinge where it was.

    Only the two ends, p = 1 and p = 2, are offered. An intermediate p = 1.5
    trades corner position against curvature: on a sharp synthetic ramp it
    won the fit about a third of the time and read up to 2 mmHg high.

    Returns (t0, p, (b0, b1, a, ws)) or None.
    """
    seed = float(t[crossing_index])
    ws = max(float(t[ref_slice.start]), seed - before_s)
    we = min(float(t[-1]), seed + after_s)
    w = (t >= ws) & (t <= we)
    tw = t[w][::decimate]
    yw = dv_lp[w][::decimate]
    if tw.size < 20:
        return None
    best = None
    for p in powers:
        for t0 in np.arange(ws + 2.0, seed + 1.0, step_s):
            h = np.clip(tw - t0, 0.0, None) ** p
            X = np.column_stack([np.ones_like(tw), tw - ws, h])
            coef, *_ = np.linalg.lstsq(X, yw, rcond=None)
            if coef[2] <= 0:
                continue
            sse = float(np.sum((X @ coef - yw) ** 2))
            if best is None or sse < best[0]:
                best = (sse, float(t0), float(p), tuple(float(c) for c in coef) + (ws,))
    if best is None:
        return None
    return best[1], best[2], best[3]


def steep_refill(ddv: np.ndarray, t: np.ndarray, start: float, end: float) -> float | None:
    """Where the refill accelerates most: the peak of d2(dV*L)/dt2 between
    ``start`` (the refill onset) and ``end`` (D44).

    In a pulsatile arm blood starts to trickle past the cuff below systolic,
    but inflow only surges once the cuff is below the pressure the artery
    sits at for most of the beat. On the six cuff trials with reference BP
    this point tracked MAP: within 3 mmHg on all four slow deflations, high
    by 12-17 mmHg on two faster ones with the sensor 7 cm from the cuff.
    """
    i = (t >= start) & (t <= end)
    if i.sum() < 5:
        return None
    acc = np.gradient(ddv[i], t[i])
    return float(t[i][int(np.argmax(acc))])


SURGE_MIN_GAP_S = 3.0
SURGE_MIN_RATIO = 1.2
SURGE_EDGE_S = 2.0
SURGE_SLOPE_S = 4.0


def surge_check(dv_lp: np.ndarray, t: np.ndarray, onset: float, steep: float,
                end: float) -> tuple[bool, str, float]:
    """Is the steep-refill point a real surge? (D46)

    ``steep_refill`` always returns its sharpest bend, surge or not. On a
    single straight ramp that bend is the onset corner itself (test4, test47,
    test48), and with no corner at all it is noise (test49, 1 s before the
    search ended). A surge must:

    - come at least 3 s after the onset, or it is the onset's corner;
    - leave the refill steeper than before: slope over the 4 s after it at
      least 1.2x the slope over the 4 s before;
    - sit at least 2 s inside the search window, or it was cut off.

    Returns ``(ok, reason, slope_ratio)``.
    """
    def slope(a: float, b: float) -> float:
        i = (t >= a) & (t <= b)
        return float(np.polyfit(t[i], dv_lp[i], 1)[0]) if i.sum() >= 10 else float("nan")

    before = slope(steep - SURGE_SLOPE_S, steep)
    after = slope(steep, steep + SURGE_SLOPE_S)
    ratio = after / max(before, 0.05) if np.isfinite(before) and np.isfinite(after) else float("nan")
    if steep - onset < SURGE_MIN_GAP_S:
        return False, f"onset corner ({steep - onset:.1f} s after onset)", ratio
    if end - steep < SURGE_EDGE_S:
        return False, f"at the end of the search ({end - steep:.1f} s from it)", ratio
    if not ratio >= SURGE_MIN_RATIO:
        return False, f"refill not steeper after it (slope ratio {ratio:.2f})", ratio
    return True, f"slope ratio {ratio:.1f}", ratio


def pulsatility_rms(x: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """RMS of the cardiac band during the reference window (SPEC 5.1 step 12).

    Informational: a healthy pulsatile subject will show a clear value here,
    which is the flag that onset is reading near systolic rather than MAP.
    """
    nyq = fs / 2.0
    lo, hi = band[0] / nyq, min(band[1], nyq * 0.95) / nyq
    if not (0 < lo < hi < 1) or len(x) < 32:
        return float("nan")
    sos = signal.butter(2, [lo, hi], btype="band", output="sos")
    padlen = min(3 * (2 * len(sos) + 1), max(len(x) // 3 - 1, 0))
    return float(np.sqrt(np.mean(signal.sosfiltfilt(sos, x, padlen=padlen) ** 2)))


def derive(
    channels: dict[str, np.ndarray],
    fs: float,
    dark: dict[str, float] | None = None,
    ref_window: tuple[float, float] | None = None,
    deflation_start_s: float | None = None,
    t: np.ndarray | None = None,
    config: PipelineConfig | None = None,
) -> Derivation:
    """Run the full chain.

    Parameters
    ----------
    channels : raw counts keyed by wavelength name ("ir", "red")
    fs : output sample rate, from the recording header
    dark : per-channel dark offsets from the DARK command
    ref_window : (start_s, end_s) occlusion reference window. Defaults to the
        last 3 s before ``deflation_start_s``, or the first 3 s of the record.
    deflation_start_s : onset search starts here; defaults to the reference
        window's end.
    """
    cfg = config or PipelineConfig()
    if "ir" not in channels:
        raise ValueError("an 'ir' channel is required")

    n = len(next(iter(channels.values())))
    if n < 64:
        raise ValueError(f"need at least 64 samples, got {n}")
    if t is None:
        t = np.arange(n) / fs

    # -- 1. Dark subtraction (SPEC 5.1 step 3) -----------------------------
    dark = dark or {}
    raw = {k: np.asarray(v, dtype=float) for k, v in channels.items()}
    dark_sub = {k: v - float(dark.get(k, 0.0)) for k, v in raw.items()}

    # -- 2. Reference window (SPEC 5.1 step 4) -----------------------------
    if ref_window is not None:
        i0 = int(np.searchsorted(t, ref_window[0]))
        i1 = int(np.searchsorted(t, ref_window[1]))
    elif deflation_start_s is not None:
        i1 = int(np.searchsorted(t, deflation_start_s))
        i0 = int(np.searchsorted(t, max(t[0], deflation_start_s - 3.0)))
    else:
        i0 = 0
        i1 = int(np.searchsorted(t, t[0] + 3.0))
    i0 = max(0, min(i0, n - 2))
    i1 = max(i0 + 2, min(i1, n))
    ref = slice(i0, i1)

    # -- 3. delta-OD (SPEC 5.1 step 5) -------------------------------------
    d_od = {k: delta_od(v, float(np.mean(v[ref]))) for k, v in dark_sub.items()}

    # -- 4. Blood volume (SPEC 5.1 step 6) ---------------------------------
    dv = blood_volume(d_od, cfg.ir_only)

    # -- 5. Low-pass (SPEC 5.1 step 7) -------------------------------------
    dv_lp = _zero_phase_lowpass(dv, fs, cfg.lowpass_hz, cfg.filter_order)

    # -- 6. Derivative (SPEC 5.1 step 8) -----------------------------------
    win = _sg_window_samples(cfg, fs, n)
    ddv = signal.savgol_filter(dv_lp, win, cfg.sg_polyorder, deriv=1, delta=1.0 / fs)

    # -- 7. Noise floor over the reference window (SPEC 5.1 step 9) --------
    mu0 = float(np.mean(ddv[ref]))
    sigma0 = float(np.std(ddv[ref]))

    # -- 8. Onset (SPEC 5.1 steps 10-11) -----------------------------------
    search_from = int(np.searchsorted(t, deflation_start_s)) if deflation_start_s is not None else i1
    mu_eff = max(mu0, 0.0) if cfg.floor_mu0 else mu0
    threshold_index, threshold = find_onset(
        ddv, fs, search_from, mu_eff, sigma0, cfg.k_sigma, cfg.hold_s
    )

    # -- 9. Bias-free refinement (see PipelineConfig.onset_method) ---------
    # Both baselines are computed; config.baseline picks the reported one.
    onset_t_intersect: float | None = None
    onset_flat = onset_sloped = None
    slope = float("nan")
    ramp_fit = base_fit = base_span = None
    if threshold_index is not None:
        flat = (0.0, float(np.mean(dv_lp[ref])))
        sloped, base_span = fit_baseline(
            dv_lp, t, fs, ref, threshold_index, cfg.baseline_s, cfg.baseline_guard_s
        )
        onset_flat, slope, ramp_fit = ramp_intersection(
            dv_lp, t, fs, ref, threshold_index, cfg.hold_s, cfg.fit_s, flat
        )
        onset_sloped, _, _ = ramp_intersection(
            dv_lp, t, fs, ref, threshold_index, cfg.hold_s, cfg.fit_s, sloped
        )
        if cfg.baseline == "flat":
            onset_t_intersect, base_fit = onset_flat, flat
        elif cfg.baseline == "sloped":
            onset_t_intersect, base_fit = onset_sloped, sloped
        else:
            raise ValueError(f"unknown baseline {cfg.baseline!r}")

    # -- 10. Corner of a trend-then-rising fit (D43) -----------------------
    onset_hinge = hinge_power = hinge_fit = None
    if threshold_index is not None:
        hg = hinge_onset(dv_lp, t, ref, threshold_index)
        if hg is not None:
            onset_hinge, hinge_power, hinge_fit = hg

    return Derivation(
        fs=fs, t=t, raw=raw, dark_sub=dark_sub, d_od=d_od,
        dv=dv, dv_lp=dv_lp, ddv=ddv,
        ref_slice=ref, mu0=mu0, sigma0=sigma0, threshold=threshold,
        onset_index=threshold_index, config=cfg,
        threshold_index=threshold_index,
        onset_time_intersect=onset_t_intersect,
        ramp_slope=slope, ramp_fit=ramp_fit,
        baseline_fit=base_fit, baseline_span=base_span,
        onset_time_flat=onset_flat, onset_time_sloped=onset_sloped,
        onset_time_hinge=onset_hinge, hinge_power=hinge_power, hinge_fit=hinge_fit,
    )


def calibrate_pressure(p_raw: np.ndarray, raw0: float, raw_ref: float,
                       p_ref: float) -> np.ndarray:
    """Two-point linear calibration (SPEC 4.1)."""
    span = raw_ref - raw0
    if abs(span) < 1e-9:
        raise ValueError("calibration span is zero — RAW0 and RAW_REF are identical")
    return (np.asarray(p_raw, dtype=float) - raw0) * p_ref / span


def resample_pressure(p_idx: np.ndarray, p_raw: np.ndarray,
                      n_samples: int) -> np.ndarray:
    """Put pressure on the PPG timebase (SPEC 5.1 step 2).

    The HX710B runs at its own ~40 Hz, and the firmware attaches the most
    recent value to each PPG sample. Interpolating on the HX710B's own sample
    counter recovers the real transitions instead of the stair-steps that
    holding the last value produces.
    """
    p_idx = np.asarray(p_idx, dtype=float)
    p_raw = np.asarray(p_raw, dtype=float)
    sample_pos = np.arange(n_samples, dtype=float)

    # Keep the first PPG sample at which each pressure reading appeared.
    _, first = np.unique(p_idx, return_index=True)
    first = np.sort(first)
    if first.size < 2:
        return np.full(n_samples, p_raw[0] if p_raw.size else np.nan)
    return np.interp(sample_pos, sample_pos[first], p_raw[first])
