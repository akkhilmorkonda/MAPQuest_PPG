"""Tests for the real-time heart-rate estimator and the monitor GUI (D33)."""

from __future__ import annotations

import queue
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
DATA = ROOT / "data"
sys.path.insert(0, str(HOST))

from mapquest.heartrate import FULL_SCALE, HeartRateEstimator  # noqa: E402

FS = 100.0


def ppg(seconds: float, bpm: float, pi: float = 0.015, seed: int = 0,
        dc: float = 130_000, noise: float = 5.0, drift: float = 0.0,
        jitter: float = 0.02, ibis: list[float] | None = None,
        drop: set[int] | None = None) -> np.ndarray:
    """Reflectance PPG: more blood -> fewer counts.

    Systolic peak plus a smaller diastolic wave, so the beat is non-sinusoidal
    like the real thing. ``ibis`` forces exact intervals; ``drop`` removes
    beats by index to simulate missed detections.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    t = np.arange(n) / FS
    beats, tb, k = [], 0.3, 0
    while tb < seconds + 2:
        beats.append(tb)
        step = ibis[k % len(ibis)] if ibis else 60 / bpm * (1 + rng.normal(0, jitter))
        tb += step
        k += 1
    shape = np.zeros(n)
    for i, b in enumerate(beats):
        if drop and i in drop:
            continue
        shape += (np.exp(-0.5 * ((t - b - 0.12) / 0.06) ** 2)
                  + 0.35 * np.exp(-0.5 * ((t - b - 0.38) / 0.09) ** 2))
    w = np.cumsum(rng.normal(0, 1, n))
    w = w / np.std(w) * drift if drift else 0.0
    return dc * (1 - pi * shape) + w + rng.normal(0, noise, n)


def run(x: np.ndarray, block: int = 5) -> HeartRateEstimator:
    e = HeartRateEstimator(FS)
    for i in range(0, len(x), block):
        e.push(x[i:i + block])
    return e


# ---------------------------------------------------------------------------
# Accuracy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bpm", [48, 60, 72, 95, 130, 170])
def test_rate_is_accurate_across_the_range(bpm):
    s = run(ppg(30, bpm, seed=bpm)).state()
    assert s.quality == "GOOD", s
    assert abs(s.hr_display - bpm) <= 2.0, s.hr_display


def test_spectral_estimate_agrees():
    s = run(ppg(30, 80)).state()
    assert s.hr_spectral is not None and abs(s.hr_spectral - 80) <= 3.0


def test_reacquires_after_a_rate_change():
    """60 -> 100 bpm: the interval history must reset rather than defend 60."""
    x = np.concatenate([ppg(25, 60, seed=1), ppg(25, 100, seed=2)])
    e = run(x)
    late = [h.hr_display for h in e.history if h.t >= 42 and h.hr_display]
    assert late, "never reacquired"
    assert abs(np.median(late) - 100) <= 3.0


def test_a_missed_beat_does_not_move_the_rate():
    x = ppg(40, 72, drop={20, 31})
    e = run(x)
    shown = [h.hr_display for h in e.history if h.t >= 15 and h.hr_display]
    assert shown and max(abs(v - 72) for v in shown) <= 4.0


def test_block_size_does_not_change_the_result():
    """Filter state is carried across blocks, so chunking must be invisible."""
    x = ppg(30, 75)
    results = []
    for block in (1, 7, 50, 333):
        e = run(x, block=block)
        results.append([round(t, 3) for t, ok in e.beats if ok])
    for other in results[1:]:
        assert other == results[0]


def test_rmssd_matches_a_known_rhythm():
    """Alternating 0.80 / 0.86 s intervals -> successive differences of 60 ms."""
    e = run(ppg(70, 72, ibis=[0.80, 0.86], jitter=0.0))
    assert e.rmssd_ms() is not None
    assert abs(e.rmssd_ms() - 60.0) <= 8.0, e.rmssd_ms()


# ---------------------------------------------------------------------------
# Refusing to show a number it cannot back up
# ---------------------------------------------------------------------------

def test_white_noise_never_shows_a_rate():
    e = run(130_000 + np.random.default_rng(4).normal(0, 5, 3000))
    assert all(h.hr_display is None for h in e.history)


def test_drift_without_a_pulse_never_shows_a_rate():
    """Regression: drift raised the SNR and timed as beats.

    Before beat estimates had to be corroborated by the spectrum, and that
    corroboration had to persist, drift alone produced a number in 310 of 390
    seconds of pulse-free recording.
    """
    shown = 0
    for s in range(12):
        e = run(ppg(25, 72, pi=0.0, seed=300 + s, drift=300))
        shown += sum(h.hr_display is not None for h in e.history)
    assert shown == 0


def test_spectrum_is_computed_on_raw_not_filtered_data():
    """Regression: band-passed white noise has a filter-shaped in-band hump.

    On the filtered spectrum, pure noise passed a 6 dB prominence test 100% of
    the time at up to 41 dB. On the raw spectrum, concentration on noise
    stays well below the 0.35 needed to corroborate a beat estimate.
    """
    concs = []
    for s in range(20):
        e = run(130_000 + np.random.default_rng(500 + s).normal(0, 5, 1500))
        concs.append(e._spec[1] or 0.0)
    assert np.percentile(concs, 95) < 0.35, concs


def test_no_contact():
    s = run(2_000 + np.random.default_rng(5).normal(0, 5, 3000)).state()
    assert s.quality == "NO_CONTACT" and s.hr_display is None


def test_saturation():
    x = ppg(20, 72)
    x[-300:] = FULL_SCALE
    s = run(x).state()
    assert s.quality == "SATURATED" and s.hr_display is None


def test_motion_withholds_the_rate():
    x = ppg(30, 72)
    x[-150:] -= 15_000                      # contact shift in the last 1.5 s
    s = run(x).state()
    assert s.quality == "MOTION"
    assert s.hr_display is None


def test_first_reading_waits_for_confirmation():
    e = run(ppg(30, 72))
    first = next(h.t for h in e.history if h.hr_display)
    assert 8.0 <= first <= 16.0, first


# ---------------------------------------------------------------------------
# Anchored on hardware
# ---------------------------------------------------------------------------

def test_real_wrist_capture_tracks():
    """The settled 60 s wrist capture: 78 bpm from its full-minute spectrum."""
    path = DATA / "bench_20261009_160125.csv"
    if not path.exists():
        pytest.skip("hardware capture not present")
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cols = lines[0].split(",")
    a = np.array([ln.split(",") for ln in lines[1:]], float)
    e = run(a[:, cols.index("ir")])
    hs = [h for h in e.history if h.t > 15]
    shown = [h.hr_display for h in hs if h.hr_display]
    assert len(shown) >= 0.9 * len(hs), Counter(h.quality for h in hs)
    assert abs(np.median(shown) - 78) <= 5.0


# ---------------------------------------------------------------------------
# GUI (headless)
# ---------------------------------------------------------------------------

def _write_capture(path: Path, x: np.ndarray, red: np.ndarray | None = None) -> None:
    with path.open("w") as fh:
        fh.write("# fs=100.00\n# ir_amp_ma=8.6\n# red_amp_ma=20.4\n")
        fh.write("idx,t_us,ir,red\n")
        red = x * 1.2 if red is None else red
        for i, (a, b) in enumerate(zip(x, red)):
            fh.write(f"{i},{i * 10000},{a:.0f},{b:.0f}\n")


def _run_app(path: Path, tmp_path: Path, monkeypatch, record: bool = False):
    import hr_monitor as hm
    monkeypatch.setattr(hm, "DATA_DIR", tmp_path)
    q: queue.Queue = queue.Queue()
    src = hm.ReplaySource(q, path, speed=50.0)
    app = hm.HRMonitorApp(src, site="finger", show=False)
    src.start()
    t0 = time.time()
    started = False
    while not app.eof and time.time() - t0 < 30:
        app.tick()
        if record and not started and app.meta:
            app._on_record()
            started = True
        time.sleep(0.01)
    app.tick()
    if record:
        app._stop_recording()
    return app


def test_gui_replay_shows_the_right_rate(tmp_path, monkeypatch):
    path = tmp_path / "finger.csv"
    _write_capture(path, ppg(30, 72))
    app = _run_app(path, tmp_path, monkeypatch)
    assert app.t_hr.get_text() not in ("--", "")
    assert abs(float(app.t_hr.get_text()) - 72) <= 2


def test_gui_recording_writes_consistent_files(tmp_path, monkeypatch):
    path = tmp_path / "finger.csv"
    _write_capture(path, ppg(30, 72))
    _run_app(path, tmp_path, monkeypatch, record=True)
    raws = [p for p in tmp_path.glob("hr_*.csv") if not p.name.endswith("_hr.csv")]
    hrs = list(tmp_path.glob("hr_*_hr.csv"))
    assert raws and hrs
    body = [ln for ln in raws[0].read_text().splitlines() if not ln.startswith("#")]
    header = body[0].split(",")
    assert header == ["idx", "ir", "red"], header
    assert all(len(ln.split(",")) == len(header) for ln in body[1:50])
    hr_lines = hrs[0].read_text().splitlines()
    assert hr_lines[0].startswith("t_s,hr_shown")
    assert len(hr_lines) > 10


def test_gui_survives_a_pulse_free_capture(tmp_path, monkeypatch):
    path = tmp_path / "noise.csv"
    _write_capture(path, ppg(25, 72, pi=0.0, drift=300, seed=77))
    app = _run_app(path, tmp_path, monkeypatch)
    assert app.t_hr.get_text() == "--"
