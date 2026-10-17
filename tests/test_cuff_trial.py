"""Cuff trial tool (D43, host/cuff_trial.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

import cuff_trial  # noqa: E402

TEST6 = ROOT / "data" / "hr_wrist_20261017_145914_test6.csv"


def fake_trial(name="x.csv"):
    """Room pressure, inflate to 170 by 30 s, settle, deflate 2 mmHg/s, release."""
    t_p = np.arange(0, 120, 0.01)
    p = np.interp(t_p, [0, 20, 30, 34, 40, 100, 104, 120],
                  [0, 0, 170, 160, 158, 38, 0, 0])
    t = np.arange(0, 120, 0.01)
    return cuff_trial.Trial(Path(name), {}, 100.0, t, {}, {}, t_p, p, "test")


def test_auto_windows_from_pressure():
    w = cuff_trial.auto_windows(fake_trial())
    assert w.inflation == pytest.approx(20.5, abs=0.2)
    assert w.peak == pytest.approx(30.0, abs=0.3)
    # 20 mmHg below the (median-smoothed, so slightly blunted) peak: ~44.5 s,
    # later than peak + 8 s.
    assert w.deflation == pytest.approx(44.5, abs=0.5)
    assert w.hold == (pytest.approx(33.0, abs=0.3), w.deflation)
    assert w.ref == (pytest.approx(w.deflation - 5), w.deflation)
    assert w.release == pytest.approx(102.0, abs=0.6)
    assert w.source == "auto"


def test_stored_windows_override_auto(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"x.csv": {"deflation": 60, "hold": [40, 55],
                                        "motion": [[70, 72]], "x": [10, 110]}}))
    tr = fake_trial()
    w = cuff_trial.stored_windows(tr, cuff_trial.auto_windows(tr), f)
    assert w.deflation == 60 and w.hold == (40, 55) and w.ref == (55, 60)
    assert w.motion == ((70, 72),) and w.x == (10, 110)
    assert w.source.startswith("hand-checked")
    other = fake_trial("y.csv")
    w2 = cuff_trial.stored_windows(other, cuff_trial.auto_windows(other), f)
    assert w2.source == "auto"


@pytest.mark.skipif(not TEST6.exists(), reason="recording not present")
def test_end_to_end_on_a_recorded_trial(tmp_path, capsys):
    out = tmp_path / "fig.png"
    assert cuff_trial.main([str(TEST6), "--out", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 20_000
    text = capsys.readouterr().out
    assert "hand-checked" in text
    assert "pulse return" in text and "refill onset (hinge)" in text
    trial = cuff_trial.load_trial(TEST6)
    assert trial.p_source == "MPRLS sensor"
    assert trial.ref_bp()["systolic"] == 111
    w = cuff_trial.stored_windows(trial, cuff_trial.auto_windows(trial))
    res = cuff_trial.analyse(trial, w)
    for name, tt in res["markers"]:
        assert tt is not None, name
        # All three markers sit between the reference diastolic and systolic
        # on this trial; a wild value means something upstream broke.
        assert 75 < trial.pressure(tt) < 111, (name, trial.pressure(tt))
