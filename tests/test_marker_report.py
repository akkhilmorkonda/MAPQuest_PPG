"""Marker fit report (D47, host/marker_report.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

import marker_report as mr  # noqa: E402

TEST25 = ROOT / "data" / "hr_wrist_20261017_174322_test25.csv"


def test_summarise_counts_and_skips_missing():
    s = mr.summarise([-2.0, 4.0, None, -12.0, float("nan")])
    assert s["n"] == 3
    assert s["bias"] == pytest.approx(-10 / 3)
    assert s["sd"] == pytest.approx(np.std([-2, 4, -12], ddof=1))
    assert s["within5"] == 2 and s["within10"] == 2
    assert mr.summarise([None])["n"] == 0


def test_error_against_each_target():
    row = {"markers": {"steep refill": 88.0, "pulse return": None},
           "systolic": 111.0, "map": 87.0}
    assert mr.error(row, "steep refill", "map") == pytest.approx(1.0)
    assert mr.error(row, "steep refill", "systolic") == pytest.approx(-23.0)
    assert mr.error(row, "pulse return", "map") is None


def test_exclusions_read_from_windows_file(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"_about": "x", "a.csv": {"exclude": "too fast"},
                             "b.csv": {"deflation": 60}}))
    assert mr.exclusions(f) == {"a.csv": "too fast"}


def test_only_recordings_with_reference_bp(tmp_path):
    (tmp_path / "hr_wrist_1.csv").write_text("# fs=100\n# ref_systolic_mmhg=120\nidx,ir\n")
    (tmp_path / "hr_wrist_2.csv").write_text("# fs=100\n# trial_info=none\nidx,ir\n")
    (tmp_path / "hr_wrist_1_vol.csv").write_text("# ref_systolic_mmhg=120\n")
    assert [p.name for p in mr.trial_files(tmp_path)] == ["hr_wrist_1.csv"]


@pytest.mark.skipif(not TEST25.exists(), reason="recording not present")
def test_scores_a_recorded_run():
    r = mr.score_run(TEST25)
    assert r["run"] == "test25" and r["group"] == "slow manual"
    assert 1.5 < r["rate"] < 2.2
    assert r["markers"]["steep refill"] == pytest.approx(87.8, abs=0.5)
    assert mr.error(r, "steep refill", "map") == pytest.approx(0.8, abs=0.5)
