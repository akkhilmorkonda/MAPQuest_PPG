"""The harness: a simulated run goes through cuff_trial.analyse unchanged."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "sim"))

import cuff_trial  # noqa: E402
import run as sim_run  # noqa: E402
from model import Patient, Vessels, simulate  # noqa: E402

MARKERS = ("pulse return", "refill onset (hinge)", "refill onset (intersect)", "steep refill")


def test_csv_round_trip_gives_the_same_markers(tmp_path):
    run = simulate(Patient(map_mmhg=85), seed=3)
    path = sim_run.write_csv(run, tmp_path / "x.csv")
    trial = cuff_trial.load_trial(path)
    assert trial.p_source == "MPRLS sensor"
    assert np.allclose(trial.p_mmhg, run.p_mmhg, atol=0.01)
    assert np.allclose(trial.t, run.t, atol=1e-3)
    a = sim_run.score(run)["markers"]
    b = sim_run.score(run, trial)["markers"]
    for k in MARKERS:
        assert (a[k] is None) == (b[k] is None)
        if a[k] is not None:
            assert b[k] == pytest.approx(a[k], abs=0.2)
    saved = json.loads(path.with_suffix(".truth.json").read_text())
    assert saved["truth"]["map_mmhg"] == pytest.approx(85, abs=0.01)
    assert saved["params"]["patient"]["map_mmhg"] == 85


def test_header_reference_is_the_truth(tmp_path):
    run = simulate(Patient(map_mmhg=90, pulse_pressure=40))
    trial = cuff_trial.load_trial(sim_run.write_csv(run, tmp_path / "y.csv"))
    ref = trial.ref_bp()
    assert ref["systolic"] == pytest.approx(run.truth["sbp_mmhg"], abs=0.05)
    assert ref["map"] == pytest.approx(run.truth["map_mmhg"], abs=0.05)
    assert trial.header["synthetic"] == "true"


def test_same_seed_same_run():
    a, b = simulate(seed=7), simulate(seed=7)
    assert np.array_equal(a.ir, b.ir) and np.array_equal(a.p_mmhg, b.p_mmhg)
    assert not np.array_equal(a.ir, simulate(seed=8).ir)


def test_scores_every_marker():
    sc = sim_run.score(simulate(Patient(map_mmhg=90, pulse_pressure=40)))
    assert set(sc["markers"]) == set(MARKERS)
    tr = sc["truth"]
    # The analysis finds a refill in the right region; how close is the experiment.
    hinge = sc["markers"]["refill onset (hinge)"]
    assert hinge is not None and tr["dbp_mmhg"] < hinge < tr["sbp_mmhg"] + 2


def test_no_inflow_no_onset():
    sc = sim_run.score(simulate(Patient(map_mmhg=85), vessels=Vessels(g_cuff=0.0)))
    assert sc["markers"]["refill onset (hinge)"] is None
    assert sc["markers"]["refill onset (intersect)"] is None


def test_outputs_stay_out_of_data():
    assert sim_run.OUT_DIR.resolve() != (ROOT / "data").resolve()
    assert (ROOT / "data") not in sim_run.OUT_DIR.resolve().parents
