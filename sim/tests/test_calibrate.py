"""Calibration plumbing (sim/calibrate.py). The fit itself is an experiment, not a test."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "sim"))

import calibrate as cal  # noqa: E402
from model import Cuff, Patient, Vessels, simulate  # noqa: E402


def test_encode_decode_round_trip():
    v = Vessels(open_span=12.0, open_power=1.7, open_offset=3.0, g_cuff=0.4, c_art=0.8, r_art=20.0)
    back = cal._decode(cal._encode(v))
    for k in cal.FIELDS:
        assert getattr(back, k) == pytest.approx(getattr(v, k))


def test_scaled_vessels_scale_volume_only():
    v = Vessels()
    a = simulate(Patient(map_mmhg=85), Cuff(), v, seed=1)
    b = simulate(Patient(map_mmhg=85), Cuff(), cal.scaled(v, 2.5), seed=1)
    assert np.allclose(b.dv, 2.5 * a.dv, atol=1e-6)
    assert b.truth["flow_onset_s"] == a.truth["flow_onset_s"]


def test_nuisance_solve_recovers_gain_offset_drain():
    t = np.linspace(0, 30, 3001)
    m = np.maximum(t - 12, 0) ** 2
    y = 1.7 * m + 4.0 - 0.3 * t
    coef, yhat = cal.solve_nuisance(t, y, m, 0.0)
    assert coef == pytest.approx([1.7, 4.0, -0.3], abs=1e-9)
    assert np.allclose(yhat, y)


def test_nuisance_gain_never_negative():
    t = np.linspace(0, 30, 301)
    coef, _ = cal.solve_nuisance(t, -t ** 2, t ** 2, 0.0)
    assert coef[0] == 0.0


def test_twin_split_is_disjoint_and_alternates():
    class R:
        def __init__(self, f):
            self.file = f
    runs = [R(f"hr_x_202610{d:02d}_120000_t.csv") for d in range(10, 20)]
    a, b = cal.split(runs)
    assert not set(map(id, a)) & set(map(id, b))
    assert len(a) == 5 and len(b) == 5
