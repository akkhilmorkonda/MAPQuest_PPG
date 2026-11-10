"""Phase 3 plumbing (sim/experiments.py): how runs are judged, not what they show."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "sim"))

import experiments as E  # noqa: E402
from model import Cuff, Patient, Vessels  # noqa: E402


def _row(m, flow=80.0, map_=80.0):
    return {"map": map_, "sbp": map_, "flow": flow, E.HINGE: m,
            E.HINGE + " false": m is None or abs(m - flow) > E.FALSE_MMHG}


def test_summary_excludes_false_onsets_and_counts_them():
    rows = [_row(81.0), _row(79.0), _row(80.0), _row(120.0), _row(None)]
    s = E.summarise(rows, E.HINGE)
    assert s["n"] == 5 and s["detected"] == 4
    assert s["false_rate"] == pytest.approx(0.4)
    assert s["bias"] == pytest.approx(0.0)
    assert s["sd"] == pytest.approx(1.0)


def test_false_onset_is_judged_against_true_flow_not_map():
    # A pulse: flow returns at 95 although MAP is 80. 93 is a true onset.
    assert not _row(93.0, flow=95.0)[E.HINGE + " false"]
    assert _row(80.0, flow=95.0)[E.HINGE + " false"]


def test_one_run_borrows_the_draw():
    d = E.Draw("x", gain=500.0, drain=0.0, white=8.0, lf=0.0)
    r = E.one(Vessels(), d, Patient(map_mmhg=80), Cuff(hold_mmhg=130), seed=2)
    assert r["source"] == "x" and r["map"] == pytest.approx(80.0, abs=0.01)
    assert set(E.MARKERS) <= set(r)


def test_cohort_gives_each_draw_its_own_seed():
    pop = [E.Draw(str(i), 500.0, 0.0, 8.0, 0.0) for i in range(3)]
    rows = E.cohort(Vessels(), pop, Patient(map_mmhg=80), Cuff(hold_mmhg=130), n_seeds=2)
    assert [r["source"] for r in rows] == ["0", "1", "2"] * 2
    assert len({r["seed"] for r in rows}) == 6
