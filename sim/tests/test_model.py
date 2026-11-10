"""Physics of the forward model (sim/model.py).

These check that the model behaves as the physics says it must. They do NOT
check how well the pipeline recovers MAP -- that is the experiment, and
asserting it here would build the answer into the test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "sim"))

from model import (SIM_FS, Cuff, Patient, Vessels, arterial_pressure,  # noqa: E402
                   hm3_pulse, simulate)

T = np.arange(0, 60, 1 / SIM_FS)


def test_native_pulse_keeps_map_and_pulse_pressure():
    p = arterial_pressure(T, Patient(map_mmhg=90, pulse_pressure=40, hr_bpm=72))
    assert p.mean() == pytest.approx(90, abs=0.05)
    assert p.max() - p.min() == pytest.approx(40, abs=0.5)
    # A plausible arterial waveform: MAP sits in the lower half of the pulse.
    assert 0.25 < (p.mean() - p.min()) / (p.max() - p.min()) < 0.5


def test_no_pulse_is_flat():
    p = arterial_pressure(T, Patient(map_mmhg=75))
    assert np.ptp(p) == pytest.approx(0, abs=1e-9)
    assert p.mean() == pytest.approx(75)


def test_hm3_pulse_period_and_size():
    h = hm3_pulse(T)
    assert h.mean() == pytest.approx(0, abs=1e-9)
    assert np.ptp(h) == pytest.approx(1.0)
    one = int(2.0 * SIM_FS)
    assert np.allclose(h[2 * one:3 * one], h[3 * one:4 * one], atol=1e-6)
    p = arterial_pressure(T, Patient(map_mmhg=80, hm3_pp_mmhg=8))
    assert np.ptp(p) == pytest.approx(8, abs=0.05)


def test_intermittent_ejection_leaves_skipped_beats_flat():
    pt = Patient(map_mmhg=80, pulse_pressure=20, hr_bpm=60, pulse_every=3)
    p = arterial_pressure(T, pt)
    beat = int(SIM_FS)  # 60 bpm: one beat per second
    assert np.ptp(p[0:beat]) > 15        # beat 0 ejects
    assert np.ptp(p[beat:3 * beat]) < 1e-9  # beats 1 and 2 do not
    assert p.mean() == pytest.approx(80, abs=0.05)


@pytest.mark.parametrize("map_mmhg", [65.0, 85.0, 100.0])
def test_without_a_pulse_flow_returns_at_map(map_mmhg):
    tr = simulate(Patient(map_mmhg=map_mmhg)).truth
    assert tr["flow_onset_mmhg"] == pytest.approx(map_mmhg, abs=0.1)


def test_with_a_pulse_flow_returns_at_systolic():
    pt = Patient(map_mmhg=90, pulse_pressure=40, hr_bpm=75)
    tr = simulate(pt, Cuff(deflate_rate=2.0)).truth
    # The first beat whose peak clears the cuff: at most one beat period
    # (0.8 s = 1.6 mmHg of deflation) after the cuff passes systolic.
    assert tr["sbp_mmhg"] - 1.7 <= tr["flow_onset_mmhg"] <= tr["sbp_mmhg"] + 0.05
    assert tr["flow_onset_mmhg"] > tr["map_mmhg"] + 20


def test_occlusion_stops_inflow_and_freezes_volume():
    run = simulate(Patient(map_mmhg=85, pulse_pressure=40))
    tr = run.truth
    hold = (run.t > tr["hold_start_s"] + 3) & (run.t < tr["deflation_s"])
    assert np.all(run.q_cuff[hold] == 0)
    assert np.ptp(run.dv[hold]) < 0.5


def test_occlusion_drain_follows_its_rate():
    run = simulate(Patient(map_mmhg=85), vessels=Vessels(drain_rate=-0.5))
    tr = run.truth
    hold = (run.t > tr["hold_start_s"] + 3) & (run.t < tr["deflation_s"])
    slope = np.polyfit(run.t[hold], run.dv[hold], 1)[0]
    assert slope == pytest.approx(-0.5, abs=0.02)


def _cardiac_rms(run, a, b):
    sos = signal.butter(2, [0.5, 4.0], "band", fs=run.sensor.fs, output="sos")
    ac = signal.sosfiltfilt(sos, run.dv)
    i = (run.t > a) & (run.t < b)
    return float(np.sqrt(np.mean(ac[i] ** 2)))


def test_optical_pulse_vanishes_under_occlusion_and_returns():
    run = simulate(Patient(map_mmhg=90, pulse_pressure=40))
    tr = run.truth
    rest = _cardiac_rms(run, 2, tr["inflation_s"] - 1)
    held = _cardiac_rms(run, tr["hold_start_s"] + 3, tr["deflation_s"])
    back = _cardiac_rms(run, tr["release_s"] + 5, tr["end_s"] - 1)
    assert rest > 0.5
    assert held < 0.05 * rest
    assert back > 0.5 * rest


def test_slow_inflation_pools_blood():
    def pooled(inflate_s):
        run = simulate(Patient(map_mmhg=85), Cuff(inflate_s=inflate_s))
        tr = run.truth
        before = run.dv[run.t < tr["inflation_s"]].mean()
        held = run.dv[(run.t > tr["hold_start_s"] + 3) & (run.t < tr["deflation_s"])].mean()
        return held - before
    fast, slow = pooled(2.0), pooled(15.0)
    assert slow > fast + 10


def test_refill_fills_the_veins():
    run = simulate(Patient(map_mmhg=85))
    tr = run.truth
    at_dump = float(np.interp(tr["dump_s"], run.t, run.dv))
    at_onset = float(np.interp(tr["flow_onset_s"], run.t, run.dv))
    assert at_dump - at_onset > 50
