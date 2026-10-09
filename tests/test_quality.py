"""Tests for motion artifact detection (host/mapquest/quality.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

from mapquest.quality import detect_motion  # noqa: E402

FS = 100.0
DATA = ROOT / "data"


def still(n=6000, seed=0, dc=142_000.0, noise=40.0):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    # Slow settled drift plus noise: what a good still capture looks like.
    return dc + 2.8 * t + rng.normal(0, noise, n)


def test_still_signal_is_not_flagged():
    r = detect_motion({"ir": still(), "red": still(seed=1, dc=168_000)}, FS)
    assert r.n_events == 0, r.describe()


def test_step_artifact_is_flagged():
    ir = still()
    ir[3000:] += 8000            # contact shift: a fast 5.6% step
    r = detect_motion({"ir": ir}, FS)
    assert r.n_events >= 1
    a, b = r.events[0]
    assert a <= 30.0 <= b, f"event {a:.2f}-{b:.2f}s does not cover the step at 30 s"


def test_slow_filling_ramp_is_not_flagged():
    """The signal we want to measure must never be mistaken for motion.

    A venous filling ramp of a few percent over tens of seconds is far below
    the 2 %/s floor, so it must pass through untouched.
    """
    ir = still()
    t = np.arange(len(ir)) / FS
    ir -= np.clip(t - 20.0, 0, None) * 142_000 * 0.001   # -0.1 %/s from 20 s
    r = detect_motion({"ir": ir}, FS)
    assert r.n_events == 0, r.describe()


def test_quiet_signal_noise_does_not_trip_the_sigma_test():
    """Regression: on a very quiet signal the robust sigma collapses.

    Before the absolute floor, ordinary noise on a clean 60 s capture cleared
    8 sigma and flagged a still arm.
    """
    ir = still(noise=3.0)
    r = detect_motion({"ir": ir}, FS)
    assert r.n_events == 0, r.describe()


def test_dilation_covers_the_recovery_tail():
    ir = still()
    ir[3000:3020] += 9000        # 200 ms spike
    r = detect_motion({"ir": ir}, FS, dilate_s=0.75)
    a, b = r.events[0]
    assert b - a >= 1.5 - 0.05


def test_nearby_spikes_merge_into_one_event():
    ir = still()
    ir[3000:3010] += 9000
    ir[3030:3040] += 9000        # 200 ms later
    r = detect_motion({"ir": ir}, FS)
    assert r.n_events == 1


def test_real_clean_capture_has_no_events():
    """Anchored on hardware: the settled 60 s wrist run from 2026-10-09."""
    path = DATA / "bench_20261009_160125.csv"
    if not path.exists():
        import pytest
        pytest.skip("hardware capture not present")
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cols = lines[0].split(",")
    a = np.array([ln.split(",") for ln in lines[1:]], float)
    r = detect_motion({c: a[:, cols.index(c)] for c in ("ir", "red")}, FS)
    assert r.n_events == 0, r.describe()


def test_real_movement_capture_catches_every_manoeuvre():
    """Anchored on hardware: every arm movement must land in a flagged span."""
    path = DATA / "provocation_wrist_20261006_161139.csv"
    if not path.exists():
        import pytest
        pytest.skip("hardware capture not present")
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cols = lines[0].split(",")
    rows = [ln.split(",") for ln in lines[1:]]
    ph = [r[cols.index("phase")] for r in rows]
    ch = {c: np.array([float(r[cols.index(c)]) for r in rows]) for c in ("ir", "red")}
    r = detect_motion(ch, FS)
    bounds = [i / FS for i in range(1, len(ph)) if ph[i] != ph[i - 1]]
    for b in bounds:
        assert any(a0 - 2 <= b <= b0 + 2 for a0, b0 in r.events), f"missed {b:.1f}s"


# ---------------------------------------------------------------------------
# Coupling slips inside holds (D31)
# ---------------------------------------------------------------------------

from mapquest.quality import detect_slips  # noqa: E402


def phases(n_each=(1500, 2000, 1500)):
    return np.concatenate([np.full(n, i) for i, n in enumerate(n_each)])


def test_mid_hold_step_is_a_slip():
    ph = phases()
    ir = still(n=len(ph))
    ir[1500 + 650:] -= 0.07 * 142_000        # -7% step 6.5 s into phase 1
    s = detect_slips({"ir": ir}, ph, FS)
    assert 1 in s.slipped
    assert abs(s.times[1] - 21.5) < 1.0
    assert s.slipped[1] < -5.0


def test_gradual_settling_is_not_a_slip():
    """Venous volume settles exponentially; that is physiology, not a slip."""
    ph = phases()
    ir = still(n=len(ph))
    t = np.arange(2000) / FS
    ir[1500:3500] += 0.03 * 142_000 * (1 - np.exp(-t / 4.0))   # +3%, tau 4 s
    s = detect_slips({"ir": ir}, ph, FS)
    assert 1 not in s.slipped, s.slipped


def test_step_at_the_transition_is_not_a_slip():
    """Moving the arm is expected to move the level; only holds are policed."""
    ph = phases()
    ir = still(n=len(ph))
    ir[1500 + 100:] += 0.05 * 142_000        # 1 s after the transition
    s = detect_slips({"ir": ir}, ph, FS)
    assert 1 not in s.slipped


def test_red_only_slip_is_caught():
    """A tilt can move one LED's coupling and not the other's."""
    ph = phases()
    ir = still(n=len(ph))
    red = still(n=len(ph), seed=4, dc=170_000)
    red[1500 + 900:] += 0.04 * 170_000
    s = detect_slips({"ir": ir, "red": red}, ph, FS)
    assert 1 in s.slipped


def test_real_graded_capture_has_exactly_one_slip():
    """Anchored on hardware: the 2026-10-09 graded run slipped in up90 only."""
    path = DATA / "provocation_wrist_20261009_170105.csv"
    if not path.exists():
        import pytest
        pytest.skip("hardware capture not present")
    import provocation as pv
    ch, ph, proto, meta = pv.load_csv(path)
    s = detect_slips(ch, ph, meta["_fs"])
    keys = [p[0] for p in proto]
    assert [keys[i] for i in s.slipped] == ["up90"], s.describe(keys)
    assert 35.0 < s.times[keys.index("up90")] < 38.0


def test_real_clean_capture_has_no_slips():
    path = DATA / "bench_20261009_160125.csv"
    if not path.exists():
        import pytest
        pytest.skip("hardware capture not present")
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cols = lines[0].split(",")
    a = np.array([ln.split(",") for ln in lines[1:]], float)
    s = detect_slips({c: a[:, cols.index(c)] for c in ("ir", "red")},
                     np.zeros(len(a), dtype=int), FS)
    assert not s.slipped
