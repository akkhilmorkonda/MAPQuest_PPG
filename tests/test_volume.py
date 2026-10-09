"""Tests for live blood-volume tracking and the blood-volume tab (D34)."""

from __future__ import annotations

import argparse
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

import synth  # noqa: E402
from mapquest.volume import VolumeTracker  # noqa: E402

FS = 100.0


def synth_run(**over):
    """Counts built from a known dV*L by the same Beer-Lambert relation."""
    base = dict(seconds=90.0, fs=FS, map=90.0, hold_mmhg=150.0, hold_s=10.0,
                deflate_rate=2.0, fill_rate=3.0, noise=5.0, drift=0.0,
                cardiac=True, cardiac_hz=1.2, cardiac_frac=0.004,
                artificial_pulse=False, ap_frac=0.004, no_filling=False,
                constant_volume=False, desaturate=False, desat_to=0.7,
                desat_od660=0.03, p_fs=40.0, p_raw0=1.2e6,
                p_counts_per_mmhg=4200.0, seed=0)
    base.update(over)
    args = argparse.Namespace(**base)
    t, ir, red, _p, _pi, _pr, dv_true = synth.build(args, np.random.default_rng(args.seed))
    return t, ir.astype(float), red.astype(float), dv_true


def track(ir, red, dark=None, block=5, every_s=1.0):
    v = VolumeTracker(FS, dark=dark or {"ir": synth.DARK_IR, "red": synth.DARK_RED})
    log = []
    step = int(every_s * FS)
    for i in range(0, len(ir), block):
        v.push(ir[i:i + block], None if red is None else red[i:i + block])
        if i % step == 0:
            log.append((v.t_now, v.state()))
    return v, log


# ---------------------------------------------------------------------------
# Accuracy against ground truth
# ---------------------------------------------------------------------------

def test_tracks_known_blood_volume():
    t, ir, red, dv_true = synth_run()
    v, _ = track(ir, red)
    a = v.arrays()
    for when in (55.0, 70.0, 85.0):
        j = int(np.argmin(np.abs(a["t"] - when)))
        k = int(np.argmin(np.abs(t - (when - 0.75))))   # causal low-pass lag
        assert abs(a["dv"][j] - dv_true[k]) < 1.5, (when, a["dv"][j], dv_true[k])


def test_rate_matches_the_filling_ramp():
    _t, ir, red, _ = synth_run(fill_rate=3.0)
    v, _ = track(ir, red)
    a = v.arrays()
    late = a["rate"][(a["t"] > 55) & (a["t"] < 85)]
    assert abs(np.nanmedian(late) - 3.0) < 0.2


def test_filling_reported_promptly_and_not_before():
    t, ir, red, dv_true = synth_run()
    _v, log = track(ir, red)
    onset = float(t[np.argmax(dv_true > 0)])
    first = next(tt for tt, s in log if s.trend == "FILLING")
    assert onset <= first <= onset + 5.0, (onset, first)
    # The floor needs 20 s of quiet data after the 10 s auto-zero (D35).
    before = {s.trend for tt, s in log if 34 < tt < onset - 1}
    assert before == {"STABLE"}, before


def test_draining_is_reported():
    """Counts rising steadily = blood leaving.

    The ramp starts at 45 s, after the floor has settled (~33 s). An event
    that begins while the floor is still settling contaminates it -- the
    operational rule is: zero, wait for STABLE, then start.
    """
    rng = np.random.default_rng(1)
    n = int(75 * FS)
    t = np.arange(n) / FS
    ir = 130_000 * (1 + 0.0008 * np.clip(t - 45, 0, None)) + rng.normal(0, 5, n)
    red = 140_000 * (1 + 0.0008 * np.clip(t - 45, 0, None)) + rng.normal(0, 5, n)
    _v, log = track(ir, red, dark={})
    assert any(s.trend == "DRAINING" for tt, s in log if tt > 48)
    assert all(s.dv_um < 0 for tt, s in log if tt > 55 and s.dv_um is not None)


def test_sign_more_blood_is_positive():
    _t, ir, red, _ = synth_run()
    v, _ = track(ir, red)
    assert v.state().dv_um > 50


# ---------------------------------------------------------------------------
# Baseline handling
# ---------------------------------------------------------------------------

def test_auto_zero_after_settling():
    _t, ir, red, _ = synth_run()
    v, log = track(ir, red)
    assert v.events[0][1] == "zero (auto)"
    assert abs(v.events[0][0] - 10.0) < 0.2
    assert log[3][1].trend == "NO BASELINE"


def test_zero_needs_three_seconds_of_data():
    v = VolumeTracker(FS)
    v.push(np.full(150, 130_000.0), np.full(150, 140_000.0))
    assert v.zero() is False
    v.push(np.full(200, 130_000.0), np.full(200, 140_000.0))
    assert v.zero() is True


def test_retune_interrupts_without_losing_history():
    """A re-tune changes counts with no change in blood: break, don't splice."""
    _t, ir, red, _ = synth_run(seconds=60, no_filling=True)
    v = VolumeTracker(FS, dark={})
    v.push(ir[:3000], red[:3000])
    n_before = len(v.t)
    v.interrupt("LED re-tuned")
    # Same tissue, double the LED current: counts double, blood unchanged.
    v.push(ir[3000:] * 2.0, red[3000:] * 2.0)
    a = v.arrays()
    assert len(v.t) > n_before, "history was discarded"
    assert any(lbl == "LED re-tuned" for _t2, lbl in v.events)
    assert sum(lbl == "zero (auto)" for _t2, lbl in v.events) == 2
    gap = np.flatnonzero(~np.isfinite(a["dv"]) & (a["t"] > 29.9))
    assert gap.size, "no gap marked at the re-tune"
    # After re-zeroing, doubled counts must NOT read as a volume change.
    assert abs(v.state().dv_um) < 2.0


def test_repeated_resets_are_deduplicated():
    v = VolumeTracker(FS)
    v.push(np.full(500, 130_000.0), np.full(500, 140_000.0))
    for _ in range(5):
        v.interrupt("link reset")
    assert sum(lbl == "link reset" for _t, lbl in v.events) == 1


def test_block_size_does_not_change_history():
    _t, ir, red, _ = synth_run(seconds=40)
    results = []
    for block in (1, 7, 64):
        v, _ = track(ir, red, block=block)
        a = v.arrays()
        results.append((a["t"].round(4), np.nan_to_num(a["dv"]).round(6)))
    for t_b, dv_b in results[1:]:
        assert np.array_equal(t_b, results[0][0])
        assert np.allclose(dv_b, results[0][1])


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

def test_contact_step_is_flagged_as_motion():
    _t, ir, red, _ = synth_run(seconds=40, no_filling=True)
    ir[2500:] *= 0.93
    red[2500:] *= 0.93
    v, _ = track(ir, red)
    a = v.arrays()
    assert a["motion"][(a["t"] > 24.5) & (a["t"] < 28)].any()


def test_strong_fingertip_pulse_is_not_motion():
    """The low-pass keeps a 2% pulse from tripping the 2 %/s motion floor.

    Its raw slope alone reaches ~8 %/s, which is why the floor is applied to
    the low-passed channels.
    """
    rng = np.random.default_rng(3)
    n = int(40 * FS)
    t = np.arange(n) / FS
    beat = np.sin(2 * np.pi * 1.2 * t)
    ir = 130_000 * (1 - 0.02 * beat) + rng.normal(0, 5, n)
    red = 140_000 * (1 - 0.015 * beat) + rng.normal(0, 5, n)
    v, _ = track(ir, red, dark={})
    assert not v.arrays()["motion"].any()


# ---------------------------------------------------------------------------
# Hardware-anchored
# ---------------------------------------------------------------------------

def _load_bench(path):
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    cols = lines[0].split(",")
    a = np.array([ln.split(",") for ln in lines[1:]], float)
    return a[:, cols.index("ir")], a[:, cols.index("red")]


def test_real_still_capture_is_stable():
    path = DATA / "bench_20261009_160125.csv"
    if not path.exists():
        pytest.skip("hardware capture not present")
    ir, red = _load_bench(path)
    _v, log = track(ir, red, dark={"ir": 8, "red": 12})
    trends = Counter(s.trend for tt, s in log if tt > 34)
    assert trends.get("FILLING", 0) == 0 and trends.get("DRAINING", 0) == 0, trends
    assert trends["STABLE"] >= 0.9 * sum(trends.values())


def test_real_slip_is_flagged_where_it_happened():
    """The 2026-10-09 graded run slipped at 36.4 s (D31)."""
    path = DATA / "provocation_wrist_20261009_170105.csv"
    if not path.exists():
        pytest.skip("hardware capture not present")
    import provocation as pv
    ch, _ph, _proto, meta = pv.load_csv(path)
    v, _ = track(ch["ir"], ch["red"], dark=meta["_dark"])
    a = v.arrays()
    assert a["motion"][(a["t"] > 35) & (a["t"] < 40)].any()


# ---------------------------------------------------------------------------
# The tab and its controls
# ---------------------------------------------------------------------------

def _core(path, tmp_path, monkeypatch):
    import hr_monitor as hm
    monkeypatch.setattr(hm, "DATA_DIR", tmp_path)
    src = hm.ReplaySource(queue.Queue(), path, speed=60.0)
    core = hm.MonitorCore(src, site="wrist", span_s=120)
    src.start()
    return core


def _until_eof(core, during=None):
    t0 = time.time()
    while not core.eof and time.time() - t0 < 30:
        core.tick()
        if during:
            during(core)
        time.sleep(0.01)
    core.tick()


def _write_capture(path, ir, red):
    with path.open("w") as fh:
        fh.write("# fs=100.00\n# dark_ir=95\n# dark_red=88\nidx,t_us,ir,red\n")
        for i, (a, b) in enumerate(zip(ir, red)):
            fh.write(f"{i},{i * 10000},{a:.0f},{b:.0f}\n")


def test_volume_tab_shows_filling(tmp_path, monkeypatch):
    _t, ir, red, _ = synth_run()
    path = tmp_path / "fill.csv"
    _write_capture(path, ir, red)
    core = _core(path, tmp_path, monkeypatch)
    _until_eof(core)
    assert core.vol_view.t_dv.get_text().startswith("+")
    assert float(core.vol_view.t_dv.get_text()) > 50
    assert "FILLING" in core.vol_view.t_trend.get_text()


def test_tune_is_refused_in_replay_and_while_recording(tmp_path, monkeypatch):
    _t, ir, red, _ = synth_run(seconds=20)
    path = tmp_path / "x.csv"
    _write_capture(path, ir, red)
    core = _core(path, tmp_path, monkeypatch)
    assert core.tune() is False and "replay" in core.status

    class FakeSerialSource:
        kind = "serial"
        out = queue.Queue()
        sent: list[str] = []

        def command(self, c):
            self.sent.append(c)

    core.source = FakeSerialSource()
    core.meta = {"fs": 100.0, "columns": ["ir", "red"], "raw": {}}
    core.start_recording()
    assert core.tune() is False and "locked" in core.status
    assert "tune" not in FakeSerialSource.sent
    core.stop_recording()
    assert core.tune() is True and FakeSerialSource.sent == ["tune"]


def test_recording_carries_events_and_volume_log(tmp_path, monkeypatch):
    _t, ir, red, _ = synth_run(seconds=40)
    path = tmp_path / "rec.csv"
    _write_capture(path, ir, red)
    core = _core(path, tmp_path, monkeypatch)
    state = {"rec": False, "marked": False}

    def during(c):
        if c.meta and not state["rec"]:
            c.toggle_record()
            state["rec"] = True
        if c.vol and c.vol.t_now > 20 and not state["marked"]:
            c.mark()
            state["marked"] = True

    _until_eof(core, during)
    core.stop_recording()
    raw = next(p for p in tmp_path.glob("hr_*.csv")
               if not p.name.endswith(("_hr.csv", "_vol.csv")))
    text = raw.read_text()
    assert "# event" in text and "label=mark_1" in text
    assert "label=zero_(auto)" in text
    vol = next(tmp_path.glob("hr_*_vol.csv")).read_text().splitlines()
    assert vol[0] == "t_s,dv_um,dv_ir_um,pct_ir,pct_red,rate_um_s,motion"
    assert len(vol) > 100


# ---------------------------------------------------------------------------
# Noise floor (D35) -- the live 2026-10-09 false FILLING
# ---------------------------------------------------------------------------

def _breathing_run(ramp: bool, seconds: float = 120.0, seed: int = 0):
    """Calm first 30 s, steady drift, then slow breathing -- as seen live.

    Drift -7.35 um/min and a rate floor around +-1.2 um/s match the live run
    that showed a hair-trigger band of -0.33 .. +0.05 um/s and read ordinary
    breathing as FILLING.
    """
    t = np.arange(int(seconds * FS)) / FS
    dv = -7.35 / 60 * t + np.where(t < 30, 0.1, 1.3) * np.sin(2 * np.pi * 0.15 * t)
    if ramp:
        dv = dv + np.clip(t - 60, 0, 40) * 3.0           # 3 um/s, 60-100 s
    rng = np.random.default_rng(seed)
    od_ir = dv / (1555 + 229 * 0.35)
    ir = 130_000 * np.exp(-od_ir) + rng.normal(0, 5, len(t))
    red = 140_000 * np.exp(-0.35 * od_ir) + rng.normal(0, 5, len(t))
    return track(ir, red, dark={})


def test_breathing_alone_is_not_filling():
    """Regression: a frozen 10 s floor read breathing as a trend 30% of the time."""
    v, log = _breathing_run(ramp=False)
    trends = Counter(s.trend for tt, s in log if tt > 35)
    assert set(trends) == {"STABLE"}, trends


def test_band_tracks_the_breathing_envelope():
    v, _ = _breathing_run(ramp=False)
    s = v.state()
    # Breathing swings the rate by about +-1.2 um/s; the band should sit just
    # above that -- not hair-trigger, and not 3 sigma's +-3.5.
    assert 1.0 < s.threshold - s.mu0 < 2.0, (s.lower, s.threshold)


def test_ramp_under_breathing_is_caught_throughout():
    """3 sigma caught this only half the time; flicker then let the floor absorb it."""
    v, log = _breathing_run(ramp=True)
    during = Counter(s.trend for tt, s in log if 64 <= tt <= 100)
    assert during == Counter({"FILLING": sum(during.values())}), during
    before = Counter(s.trend for tt, s in log if 35 < tt < 60)
    assert set(before) == {"STABLE"}, before


def test_a_ramp_does_not_widen_the_band_afterwards():
    """The entry and exit seconds of a ramp must not leak into the floor."""
    quiet, _ = _breathing_run(ramp=False)
    after, _ = _breathing_run(ramp=True)
    w0 = quiet.state().threshold - quiet.state().mu0
    w1 = after.state().threshold - after.state().mu0
    assert w1 < 1.25 * w0, (w0, w1)


def test_band_has_a_minimum_width():
    """A perfectly calm stretch must not produce a hair-trigger band."""
    _t, ir, red, _ = synth_run(seconds=60, no_filling=True, cardiac=False)
    v, _ = track(ir, red)
    s = v.state()
    assert s.threshold - s.mu0 >= 0.5 - 1e-9
