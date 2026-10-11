"""Tests for the MPRLS cuff-pressure path: host/mapquest/mprls.py and pressure_monitor.py."""

from __future__ import annotations

import queue
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

from mapquest import mprls  # noqa: E402
from mapquest.mprls import (  # noqa: E402
    MMHG_PER_PSI, OUTPUT_MAX, OUTPUT_MIN, PressureTracker, counts_to_mmhg, parse_line,
    status_problem,
)

PER_MMHG = (OUTPUT_MAX - OUTPUT_MIN) / (25.0 * MMHG_PER_PSI)
ATM = 760.0


def counts_for(abs_mmhg: float) -> int:
    return int(round(OUTPUT_MIN + abs_mmhg * PER_MMHG))


def test_transfer_function_end_points():
    assert counts_to_mmhg(OUTPUT_MIN) == pytest.approx(0.0)
    assert counts_to_mmhg(OUTPUT_MAX) == pytest.approx(25 * MMHG_PER_PSI)
    assert counts_to_mmhg(counts_for(ATM)) == pytest.approx(ATM, abs=0.01)


def test_one_count_is_well_below_a_mmhg():
    """24-bit counts: resolution is not what limits accuracy."""
    assert 1.0 / PER_MMHG < 0.001


def test_parse_line():
    assert parse_line("M,1234,8000000,64") == (1234, 8000000, 64)
    assert parse_line("# firmware=x") is None
    assert parse_line("M,12,garbage,64") is None


def test_status_problem():
    assert status_problem(0x40) is None
    assert "power" in status_problem(0x00)
    assert "integrity" in status_problem(0x44)
    assert "saturated" in status_problem(0x41)


def feed(tracker, pressures, fs=100.0, start_ms=5000):
    for i, p in enumerate(pressures):
        tracker.push(start_ms + int(i * 1000 / fs), counts_for(ATM + p), 0x40)


def test_auto_zero_then_cuff_pressure():
    tr = PressureTracker()
    tr.start(10.0)
    feed(tr, [0.0] * 250 + [120.0] * 100)
    st = tr.state()
    assert st.zero_mmhg == pytest.approx(ATM, abs=0.05)
    assert st.mmhg == pytest.approx(120.0, abs=0.05)
    assert st.peak == pytest.approx(120.0, abs=0.05)
    assert st.t == pytest.approx(10.0 + 3.49, abs=0.02)


def test_no_pressure_until_zeroed():
    tr = PressureTracker()
    tr.start(0.0)
    feed(tr, [0.0] * 100)                     # 1 s: before the 2 s auto-zero
    st = tr.state()
    assert st.zero_mmhg is None and np.isnan(st.mmhg)


def test_deflation_rate():
    tr = PressureTracker()
    tr.start(0.0)
    t = np.arange(0, 8, 0.01)
    feed(tr, np.where(t < 3, 0.0, 150.0 - 2.5 * (t - 3)))
    assert tr.state().rate == pytest.approx(-2.5, abs=0.05)


def test_oscillation_amplitude():
    """A 1.5 mmHg heartbeat ripple reads as ~3 mmHg peak-to-peak."""
    tr = PressureTracker()
    tr.start(0.0)
    t = np.arange(0, 10, 0.01)
    feed(tr, np.where(t < 3, 0.0, 100.0 + 1.5 * np.sin(2 * np.pi * 1.2 * t)))
    assert tr.state().oscillation == pytest.approx(3.0, abs=0.4)


def test_bad_status_samples_are_skipped():
    tr = PressureTracker()
    tr.start(0.0)
    tr.push(0, counts_for(ATM), 0x41)
    assert tr.state() is None and tr.problem == "reading saturated"


def test_manual_zero_resets_peak():
    tr = PressureTracker()
    tr.start(0.0)
    feed(tr, [0.0] * 250 + [50.0] * 200)
    tr.set_zero(1.0)                          # zeroed with 50 mmHg in the cuff
    st = tr.state()
    assert st.mmhg == pytest.approx(0.0, abs=0.05) and st.peak == pytest.approx(0.0, abs=0.05)


# ---------------------------------------------------------------------------
# Monitor core, headless
# ---------------------------------------------------------------------------

class _Source:
    kind = "test"

    def __init__(self):
        self.out = queue.Queue()

    def stop(self):
        pass


def test_core_drains_records_and_draws(tmp_path, monkeypatch):
    import pressure_monitor as pm
    monkeypatch.setattr(pm, "DATA_DIR", tmp_path)
    src = _Source()
    core = pm.PressureCore(src)
    src.out.put(("connected", "TEST"))
    src.out.put(("line", "# firmware=mprls-test-0.1"))
    for i in range(300):
        p = 0.0 if i < 250 else 80.0
        src.out.put(("line", f"M,{1000 + i * 10},{counts_for(ATM + p)},64"))
    core.drain()
    core.toggle_record()
    for i in range(300, 400):
        src.out.put(("line", f"M,{1000 + i * 10},{counts_for(ATM + 80.0)},64"))
    core.drain()
    core.toggle_record()
    st = core.redraw()
    assert st.mmhg == pytest.approx(80.0, abs=0.05)
    rec = next(tmp_path.glob("pressure_*.csv")).read_text().splitlines()
    assert "# firmware=mprls-test-0.1" in rec
    rows = [r for r in rec if r[:1].isdigit()]
    assert len(rows) == 100
    assert float(rows[-1].split(",")[4]) == pytest.approx(80.0, abs=0.05)


def test_demo_cuff_profile():
    import pressure_monitor as pm
    c = pm.DemoSource.cuff
    assert c(2) == 0 and c(12) == pytest.approx(170)
    assert c(40) == pytest.approx(116.0)           # deflating at 2.5 mmHg/s
    assert c(88) == 0
