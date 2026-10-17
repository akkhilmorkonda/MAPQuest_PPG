"""Pulse return during cuff deflation: when the heartbeat reappears (D43).

The refill onset in pipeline.py reads the slow rise in blood volume. This
reads the other signature of return to flow: the pulse itself coming back.

Under full occlusion the PPG is not silent. A faint ripple at heart rate
survives -- pulsation transmitted through the tissue under and beyond the
cuff -- so "the first beat-sized bump" fires early, while the cuff is still
above systolic (2026-10-16 test4 showed rhythm at a cuff of 120-124 with no
flow). What changes at return is the SIZE of that ripple relative to itself.
So the floor is measured on the occluded signal, early in the hold, and the
return is the first sustained rise well above it:

1. Pulse signal: -log(IR) band-passed 0.5-4 Hz, % of DC, systole positive.
2. Heart rate f0 from Welch over the uncuffed data (before inflation, after
   release) AND the deflation, length-weighted. The deflation is where the
   heart band has to sit, and it is usually the longest stretch. A short
   pre-inflation window alone can be wrong: test6 gave 42 bpm, and test27
   gave 90 bpm from 7 s before inflation while the deflation pulse was 75,
   which put the band beside the pulse and missed its return entirely.
3. Heart-band envelope: the pulse band-passed to f0 +/-15 %, as a moving RMS
   over about two beats. The off-band envelope is the rest of the 0.5-4 Hz
   pulse, the same way; motion is broadband and lifts both.
4. Floor: median heart-band envelope over the early hold.
5. Return: the first time after deflation starts that the heart-band
   envelope is above k x floor AND above the off-band envelope, held for
   hold_s seconds.

Timing: the narrow heart-band filter and the centred RMS both run forwards
and backwards, so a sudden return registers about a second EARLY; a pulse
that grows back slowly crosses k x floor LATE. On synthetic returns that
grow to full size over 0.5-8 s the error spanned -1.3 to +0.7 s with a
quiet floor (+3 s for the slowest growth over a ripple floor 3x larger):
at 2 mmHg/s, roughly -3 to +1.5 mmHg. No correction is applied for either.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass
class PulseReturn:
    t_return: float | None
    f0_hz: float
    floor: float
    """Median heart-band envelope over the early hold (% of DC)."""
    threshold: float
    env_hr: np.ndarray
    env_off: np.ndarray
    pulse: np.ndarray
    """The 0.5-4 Hz pulse signal (% of DC, systole positive)."""


def pulse_signal(ir: np.ndarray, fs: float, band=(0.5, 4.0)) -> np.ndarray:
    """-log(IR) band-passed, in % of DC. More blood absorbs more: systole up."""
    x = np.log(np.maximum(np.asarray(ir, dtype=float), 1e-9))
    sos = signal.butter(2, [band[0] / (fs / 2), band[1] / (fs / 2)], "band", output="sos")
    return -signal.sosfiltfilt(sos, x) * 100.0


def heart_rate(pulse: np.ndarray, fs: float, mask: np.ndarray,
               lo_hz: float = 0.7, hi_hz: float = 3.0) -> float | None:
    """Dominant frequency of the pulse over the masked (uncuffed) samples.

    Each contiguous run of the mask gets its own Welch estimate; the spectra
    are averaged, so a gap between runs is never read as signal."""
    # Every run is zero-padded to the same FFT length so the spectra share a
    # frequency grid; runs under 4 s (a few beats) are too short to use.
    nfft = int(16 * fs)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(int), [0]))))
    psd_sum = None
    for a, b in zip(edges[::2], edges[1::2]):
        if b - a < int(4 * fs):
            continue
        nper = min(int(8 * fs), b - a)
        f, pxx = signal.welch(pulse[a:b], fs=fs, nperseg=nper, nfft=nfft)
        psd_sum = pxx * (b - a) if psd_sum is None else psd_sum + pxx * (b - a)
    if psd_sum is None:
        return None
    band = (f >= lo_hz) & (f <= hi_hz)
    return float(f[band][np.argmax(psd_sum[band])])


def _moving_rms(x: np.ndarray, n: int) -> np.ndarray:
    return np.sqrt(np.convolve(x * x, np.ones(n) / n, mode="same"))


def detect(
    t: np.ndarray,
    ir: np.ndarray,
    fs: float,
    hold: tuple[float, float],
    search_from: float,
    uncuffed: np.ndarray,
    k: float = 3.0,
    hold_s: float = 3.0,
    rel_bw: float = 0.15,
    f0_hz: float | None = None,
) -> PulseReturn | None:
    """Find pulse return.

    t, ir : session time and dark-subtracted IR counts
    hold : (start_s, end_s) of the early, fully occluded hold -- the floor
    search_from : deflation start; return is searched for after this
    uncuffed : boolean mask of samples with the cuff off, for the heart rate
    f0_hz : skip the estimate and use this heart rate
    """
    pulse = pulse_signal(ir, fs)
    f0 = f0_hz or heart_rate(pulse, fs, uncuffed | (t >= search_from))
    if f0 is None:
        return None
    nyq = fs / 2.0
    sos = signal.butter(2, [f0 * (1 - rel_bw) / nyq, f0 * (1 + rel_bw) / nyq],
                        "band", output="sos")
    hr = signal.sosfiltfilt(sos, pulse)
    n = max(int(round(2.0 / f0 * fs)), 3)
    env_hr = _moving_rms(hr, n)
    env_off = _moving_rms(pulse - hr, n)

    h = (t >= hold[0]) & (t <= hold[1])
    if h.sum() < n:
        return None
    floor = float(np.median(env_hr[h]))
    thr = k * floor
    ok = (env_hr > thr) & (env_hr > env_off)
    i0 = int(np.searchsorted(t, search_from))
    m = max(int(round(hold_s * fs)), 1)
    run = np.convolve(ok[i0:].astype(int), np.ones(m, int), mode="valid")
    j = np.flatnonzero(run == m)
    t_ret = None
    if j.size:
        t_ret = float(t[i0 + j[0]])
    return PulseReturn(t_return=t_ret, f0_hz=f0, floor=floor, threshold=thr,
                       env_hr=env_hr, env_off=env_off, pulse=pulse)
