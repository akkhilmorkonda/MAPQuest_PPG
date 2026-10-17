"""Pulse-return detector (D43, host/mapquest/pulse_return.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))

from mapquest import pulse_return as prt  # noqa: E402

FS = 100.0
F0 = 1.15          # 69 bpm


def ppg(amp_of_t, seconds=110.0, seed=0, noise=0.0005, bumps=()):
    """IR counts whose pulse amplitude (fraction of DC) follows amp_of_t(t).

    bumps: (t0, t1, amp) extra stretches of rhythmic ripple during occlusion,
    the transmitted pulsation that made "first beat-sized bump" fire early."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 1 / FS)
    amp = amp_of_t(t)
    for t0, t1, a in bumps:
        amp = np.where((t >= t0) & (t < t1), np.maximum(amp, a), amp)
    phase = 2 * np.pi * np.cumsum(F0 * (1 + 0.03 * np.sin(2 * np.pi * t / 17))) / FS
    pulse = amp * (np.sin(phase) + 0.35 * np.sin(2 * phase + 0.6))
    ir = 200_000.0 * (1 - pulse) * (1 + rng.normal(0, noise, t.size))
    return t, ir


def profile(t_return, occluded=0.0002, full=0.004, rise_s=4.0):
    """Cuff off until 20 s, occluded 20-t_return, pulse growing back after."""
    def amp(t):
        a = np.where(t < 20, full, occluded)
        grow = np.clip((t - t_return) / rise_s, 0, 1)
        return np.where(t >= t_return, occluded + (full - occluded) * grow, a)
    return amp


def run(t, ir, search_from=40.0, **kw):
    uncuffed = t < 18
    return prt.detect(t, ir, FS, hold=(24.0, 38.0), search_from=search_from,
                      uncuffed=uncuffed, **kw)


def test_heart_rate_from_uncuffed_data():
    t, ir = ppg(profile(70.0))
    pulse = prt.pulse_signal(ir, FS)
    f0 = prt.heart_rate(pulse, FS, t < 18)
    assert abs(f0 - F0) < 0.08, f"heart rate {f0 * 60:.0f} bpm vs {F0 * 60:.0f}"


def test_pulse_return_found_near_true_time():
    t, ir = ppg(profile(70.0))
    r = run(t, ir)
    assert r is not None and r.t_return is not None
    assert 69.0 <= r.t_return <= 73.0, f"return at {r.t_return:.2f} s, true 70 s"


def test_occlusion_ripple_sets_the_floor_not_the_return():
    """A rhythmic ripple through the whole hold, with occasional bigger
    stretches, must not be read as the pulse coming back."""
    t, ir = ppg(profile(70.0, occluded=0.0006), bumps=[(48, 51, 0.0012), (58, 60, 0.0011)])
    r = run(t, ir)
    assert r.t_return is not None and r.t_return > 66.0, f"early return at {r.t_return}"


def test_no_return_when_pulse_stays_occluded():
    t, ir = ppg(profile(1e9))
    r = run(t, ir)
    assert r.t_return is None


def test_motion_is_not_a_pulse():
    """Broadband junk lifts the off-band envelope as much as the heart band."""
    t, ir = ppg(profile(1e9))
    rng = np.random.default_rng(3)
    m = (t > 50) & (t < 58)
    ir = ir * (1 + np.where(m, rng.normal(0, 0.01, t.size), 0))
    r = run(t, ir)
    assert r.t_return is None, f"motion read as pulse return at {r.t_return}"


def test_heart_rate_falls_back_to_deflation():
    t, ir = ppg(profile(60.0))
    r = prt.detect(t, ir, FS, hold=(24.0, 38.0), search_from=40.0,
                   uncuffed=np.zeros(t.size, bool))
    assert r is not None and abs(r.f0_hz - F0) < 0.08
    assert r.t_return is not None and 59.0 <= r.t_return <= 63.0
