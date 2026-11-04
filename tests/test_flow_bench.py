"""Tests for the flow bench (intralipid phantom: flow vs no flow in the tube)."""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

import flow_bench as fb  # noqa: E402

FS = 100.0


def bench_signal(seconds=125.0, cycles=((30.0, 60.0), (80.0, 105.0)), seed=0,
                 ir_flow=130_000.0, ir_air=100_000.0, red_flow=140_000.0,
                 red_air=120_000.0, noise=30.0, ramp_s=2.0):
    """Counts for a tube that drains at each no-flow and refills at each flow."""
    t = np.arange(int(seconds * FS)) / FS
    air = np.zeros_like(t)
    for d, f in cycles:
        air += np.clip((t - d) / ramp_s, 0, 1) - np.clip((t - f) / ramp_s, 0, 1)
    rng = np.random.default_rng(seed)
    ir = ir_flow + (ir_air - ir_flow) * air + rng.normal(0, noise, len(t))
    red = red_flow + (red_air - red_flow) * air + rng.normal(0, noise, len(t))
    events = [(e, lab) for d, f in cycles for e, lab in ((d, "no_flow"), (f, "flow"))]
    return t, ir, red, events


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def test_no_flow_and_flow_steps_are_measured():
    t, ir, red, events = bench_signal()
    steps = fb.compute_steps(t, ir, red, events, {"ir": 10.0, "red": 12.0})
    assert [s.kind for s in steps] == ["no_flow", "flow", "no_flow", "flow"]
    assert all(s.status == "ok" for s in steps)
    d = steps[0]
    assert d.ir["d_counts"] == pytest.approx(-30_000, rel=0.01)
    assert d.ir["d_pct"] == pytest.approx(-23.1, abs=0.2)
    assert d.ir["d_od"] == pytest.approx(-np.log((100_000 - 10) / (130_000 - 10)), abs=1e-3)
    assert d.ir["noise"] == pytest.approx(30, rel=0.15)
    assert d.ir["snr"] > 500
    assert steps[1].ir["d_counts"] == pytest.approx(+30_000, rel=0.01)
    assert steps[0].red["d_counts"] == pytest.approx(-20_000, rel=0.01)


def test_a_late_key_press_does_not_inflate_the_noise():
    # The tube starts draining at 30 s; the marker is hit 0.8 s later.
    t, ir, red, _ = bench_signal(cycles=((30.0, 200.0),), seconds=60)
    s = fb.compute_steps(t, ir, red, [(30.8, "no_flow")], {})[0]
    assert s.ir["noise"] == pytest.approx(30, rel=0.15)
    assert s.ir["snr"] > 500


def test_dips_during_no_flow_do_not_spoil_either_step():
    """2026-11-02: residual liquid and air gave 5-9 % dips all through no flow.

    With means and BEFORE-window noise the return to flow scored SNR 0-2.
    Levels are medians and the noise comes from the flowing side.
    """
    t, ir, red, events = bench_signal(cycles=((30.0, 70.0),), seconds=100,
                                      ir_air=101_500.0, red_air=136_000.0)
    rng = np.random.default_rng(1)
    for start in rng.uniform(31, 69, 20):           # 0.1-0.4 s dips, 7 % deep
        s = (t >= start) & (t < start + rng.uniform(0.1, 0.4))
        ir[s] *= 0.95
        red[s] *= 0.93
    no_flow, flow = fb.compute_steps(t, ir, red, events, {})
    assert no_flow.ir["noise_from"] == "before" and flow.ir["noise_from"] == "after"
    for s, sign in ((no_flow, -1), (flow, +1)):
        assert s.status == "ok"
        assert s.red["noise"] == pytest.approx(30, rel=0.2)    # flowing noise only
        assert s.red["d_counts"] == pytest.approx(sign * 4_000, rel=0.1)
        assert s.red["snr"] > 100


def test_baseline_drift_is_not_counted_as_noise():
    t, ir, red, events = bench_signal(noise=5.0)
    ir = ir + 20.0 * t                      # 20 counts/s drift
    s = fb.compute_steps(t, ir, red, events, {})[0]
    assert s.ir["noise"] == pytest.approx(5.0, rel=0.2)


def test_windows_stop_at_neighbouring_markers():
    # Flow back 8 s after no-flow: AFTER is cut to 3 s, BEFORE of the
    # refill starts 5 s after the no-flow -- both still usable, both flagged.
    t, ir, red, events = bench_signal(cycles=((30.0, 38.0),), seconds=70)
    steps = fb.compute_steps(t, ir, red, events, {})
    assert [s.status for s in steps] == ["ok", "ok"]
    assert "clipped" in steps[0].flags and "clipped" in steps[1].flags
    assert steps[0].ir["after"] == pytest.approx(100_000, rel=0.002)


def test_no_window_reaches_across_a_retune():
    # A re-tune at 25 s jumps the counts with no change in the tube.
    t, ir, red, events = bench_signal(cycles=((30.0, 200.0),), seconds=60)
    ir = np.where(t >= 25.0, ir + 40_000, ir)
    s = fb.compute_steps(t, ir, red, events[:1] + [(25.0, "LED_re-tuned")], {})[0]
    assert s.status == "too short"                 # BEFORE would be 24-29 s: under 2 s
    s = fb.compute_steps(t, ir, red, [(20.0, "LED_re-tuned"), (30.0, "no_flow")], {})[0]
    assert s.status == "ok" and "clipped" in s.flags
    assert s.ir["before"] == pytest.approx(170_000, rel=0.002)   # after the re-tune only


def test_too_short_and_waiting():
    # Flow back 6 s after no-flow: 1 s left on each side of the pair.
    t, ir, red, events = bench_signal(cycles=((30.0, 36.0),), seconds=60)
    steps = fb.compute_steps(t, ir, red, events, {})
    assert [s.status for s in steps] == ["too short", "too short"]
    # A marker 5 s before the end of the data: its AFTER window is still filling.
    t, ir, red, _ = bench_signal(cycles=(), seconds=40)
    steps = fb.compute_steps(t, ir, red, [(35.0, "no_flow")], {})
    assert steps[0].status == "waiting"


def test_saturation_is_flagged():
    t, ir, red, events = bench_signal(ir_air=fb.FULL_SCALE + 5_000.0)
    ir = np.minimum(ir, fb.FULL_SCALE)
    s = fb.compute_steps(t, ir, red, events, {})[0]
    assert "rail" in s.flags


def test_step_table_and_csv():
    t, ir, red, events = bench_signal()
    steps = fb.compute_steps(t, ir, red, events, {})
    table = fb.step_table(steps)
    assert "no_flow" in table and "flow" in table and "-23." in table
    rows = fb.steps_csv(steps).splitlines()
    assert rows[0].split(",") == fb.STEP_COLUMNS
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# Core, recording and replay
# ---------------------------------------------------------------------------

def _write_capture(path, ir, red):
    with path.open("w") as fh:
        fh.write("# fs=100.00\n# dark_ir=10\n# dark_red=12\nidx,t_us,ir,red\n")
        for i, (a, b) in enumerate(zip(ir, red)):
            fh.write(f"{i},{i * 10000},{a:.0f},{b:.0f}\n")


def _core(path, tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(fb, "DATA_DIR", tmp_path / "benchtop_data")
    src = fb.FlowReplaySource(queue.Queue(), path, speed=60.0)
    core = fb.FlowCore(src, **kw)
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


def test_auto_record_one_button_then_replay_and_report(tmp_path, monkeypatch):
    _t, ir, red, _ = bench_signal(seconds=90, cycles=((30.0, 60.0),))
    cap = tmp_path / "cap.csv"
    _write_capture(cap, ir, red)
    core = _core(cap, tmp_path, monkeypatch, auto_record=True,
                 run_id="F-01", note="1% intralipid, 40 cm head")
    pressed: set[float] = set()

    def during(c):
        assert c.recording or not c.ir.n          # recording from the first sample
        for when in (30.0, 60.0):
            if c.t_now >= when and when not in pressed:
                c.toggle_flow()
                pressed.add(when)

    _until_eof(core, during)
    assert core.zero_ref is not None              # auto-zero after 3 s
    assert core.flowing                           # no flow, then flow again
    core.stop_recording()
    assert core.run_id == "F-02" and not core.recording

    folder = tmp_path / "benchtop_data"
    rec_path = next(folder.glob("flow_*_F-01.csv"))
    assert core.last_saved == rec_path
    text = rec_path.read_text()
    assert "# session_t0=0.000" in text           # nothing missed before recording
    assert "label=no_flow" in text and "label=flow" in text
    assert "# note=1%_intralipid,_40_cm_head" in text
    steps = (folder / (rec_path.stem + "_steps.csv")).read_text().splitlines()
    assert len(steps) == 3 and all(",ok," in r for r in steps[1:])
    assert steps[1].split(",")[1] == "no_flow"

    # The saved file gives the same steps offline, and replays with its markers.
    rec = fb.load_recording(rec_path)
    off = fb.compute_steps(rec["t"], rec["ir"], rec["red"], rec["events"], rec["dark"])
    assert off[0].ir["d_pct"] == pytest.approx(-23.1, abs=0.5)
    assert fb.report(rec_path, 10, 5, 10) == 0
    assert rec_path.with_name(rec_path.stem + "_report.png").exists()

    replay = _core(rec_path, tmp_path, monkeypatch)
    _until_eof(replay)
    assert not replay.recording                   # a replay is not re-recorded
    assert [lab for _t, lab in replay.events if lab in fb.TRANSITIONS] == ["no_flow", "flow"]
    assert [s.status for s in replay.steps] == ["ok", "ok"]
    assert len(list(folder.glob("flow_*.csv"))) == 2      # recording + _steps only


def test_stop_then_start_makes_a_new_file(tmp_path, monkeypatch):
    _t, ir, red, _ = bench_signal(seconds=20, cycles=())
    cap = tmp_path / "x.csv"
    _write_capture(cap, ir, red)
    core = _core(cap, tmp_path, monkeypatch, auto_record=True, run_id="F-01")
    state = {"stopped": False}

    def during(c):
        if c.t_now > 10 and not state["stopped"]:
            c.toggle_record()                     # stop: F-01 saved
            c.toggle_record()                     # start: F-02
            state["stopped"] = True

    _until_eof(core, during)
    core.stop_recording()
    names = sorted(p.name for p in (tmp_path / "benchtop_data").glob("flow_*.csv"))
    assert any(n.endswith("_F-01.csv") for n in names)
    assert any(n.endswith("_F-02.csv") for n in names)
    assert sum(n.endswith("_steps.csv") for n in names) == 2


def test_tune_allowed_while_recording_and_logged(tmp_path, monkeypatch):
    _t, ir, red, _ = bench_signal(seconds=10, cycles=())
    cap = tmp_path / "x.csv"
    _write_capture(cap, ir, red)
    core = _core(cap, tmp_path, monkeypatch)
    assert core.tune() is False and "replay" in core.status

    class FakeSerial:
        kind = "serial"
        out = queue.Queue()
        sent: list[str] = []

        def command(self, c):
            self.sent.append(c)

    _until_eof(core)
    core.source = FakeSerial()
    core.start_recording()
    assert core.tune() is True and FakeSerial.sent == ["tune"]
    assert "logged" in core.status
    core.q.put(("reset", "LED re-tuned"))
    core.tick()
    assert core.zero_ref is None and core.events[-1][1] == "LED_re-tuned"
    core.stop_recording()
    text = next((tmp_path / "benchtop_data").glob("flow_*[0-9].csv")).read_text()
    assert "label=LED_re-tuned" in text


def test_range_warning():
    core = fb.FlowCore(type("S", (), {"out": queue.Queue()})())
    core.meta = {"fs": 100.0, "columns": ["ir", "red"], "raw": {}}
    core.ir.extend(np.full(100, 0.5 * fb.FULL_SCALE))
    core.red.extend(np.full(100, 0.97 * fb.FULL_SCALE))
    assert "RED" in core.range_warning() and "IR" not in core.range_warning()


def test_recordings_go_to_benchtop_data_beside_data():
    # marker_report.py scores data/hr_*.csv; bench files must never land there.
    assert fb.DATA_DIR == ROOT / "benchtop_data"
