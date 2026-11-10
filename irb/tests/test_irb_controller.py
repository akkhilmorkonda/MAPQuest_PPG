"""The session controller (what the GUI does) against the mock board, on a
virtual clock so every run is deterministic."""

import json

import pytest

from irbtool.session import TAIL_S, SessionCore

TRIAL = {"subject_id": "S004", "run_id": "S004-R01", "distal_cm": "3", "proximal_cm": "4",
         "height_cm": "175", "weight_kg": "72", "arm_circumference_cm": "30",
         "cuff_size": "adult", "fitzpatrick": "III", "session_systolic": "118",
         "session_diastolic": "76"}


class VClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, tmp_path):
        self.clock = VClock()
        self.core = SessionCore(data_root=tmp_path, clock=self.clock)
        self.board = self.core.connect_mock(realtime=False, threaded=False)
        self.core.trial_raw = dict(TRIAL)
        self.run(1.0)

    def run(self, seconds, step=0.05):
        for _ in range(int(round(seconds / step))):
            self.board.advance(step)
            self.clock.t += step
            self.core.tick()

    def states(self):
        return [l.split("\t")[2] for l in self.log_lines() if "\tevent\tstate," in l]

    def log_lines(self):
        return self.core.log.path.read_text().splitlines() if self.core.log else []


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.core.close()


def full_run(rig, occlusion_at=None, pump_to=150):
    ok, msg = rig.core.start()
    assert ok, msg
    rig.run(0.3)
    assert rig.core.model.state == "INFLATE"
    rig.board.pump_to(pump_to)
    if occlusion_at is not None:
        rig.run(occlusion_at / 40.0)
        ok, msg = rig.core.mark_occlusion()
        assert ok, msg
    rig.run(pump_to / 40.0 + 0.5)
    ok, msg = rig.core.run()
    assert ok, msg
    rig.run(10.5)                                    # hold
    rig.run((pump_to - 40) / 2.0 + 1.0)              # deflate to 40
    return rig.core.recorder


def test_connect_streams_and_builds_volume(rig):
    assert rig.core.model.streaming and rig.core.model.info.firmware.startswith("mapquest-irb-fw")
    assert rig.core.vol is not None and rig.core.vol.n_total > 50
    assert len(rig.core.p_hist) >= 9 and len(rig.core.imu_hist["D"]) >= 45


def test_start_refused_on_incomplete_trial_info(rig):
    rig.core.trial_raw.pop("fitzpatrick")
    ok, msg = rig.core.start()
    assert not ok and "fitzpatrick" in msg and rig.core.recorder is None


def test_full_session_records_saves_and_steps_run_id(rig, tmp_path):
    rec = full_run(rig)
    assert rec.status == "recording"                 # still in the 10 s tail
    rig.run(TAIL_S + 0.5)
    assert rec.status == "unsaved"
    assert rig.core.model.state == "IDLE"
    ok, msg = rig.core.start()
    assert not ok and "save or discard" in msg       # no new run until this one is settled

    rig.core.after_raw = {"ref_systolic": "121", "ref_diastolic": "79"}
    ok, msg = rig.core.save_run()
    assert ok, msg
    assert rig.core.trial_raw["run_id"] == "S004-R02"
    distal = (rec.dir / "distal.csv").read_text()
    assert "# ref_systolic_mmhg=121" in distal and "# trial_info=complete" in distal
    doc = json.loads((rec.dir / "run.json").read_text())
    assert doc["dumps"] == ["reached stop pressure"] and doc["status"] == "saved"
    events = (rec.dir / "events.csv").read_text()
    assert ",targets,target=148 run_min=138 basis=session systolic,operator," in events
    states = rig.states()
    assert states[-5:] == ["state,INFLATE", "state,HOLD", "state,DEFLATE", "state,DUMP", "state,IDLE"]
    # the trial fields are remembered for the next session, without the reference
    saved = json.loads((tmp_path / ".last_trial.json").read_text())
    assert saved["subject_id"] == "S004" and "ref_systolic" not in saved


def test_doppler_occlusion_mark_moves_target_and_run_gate(rig):
    ok, _ = rig.core.start()
    rig.run(0.3)
    rig.board.pump_to(200)
    rig.run(110 / 40.0)                               # ~110 mmHg
    ok, msg = rig.core.mark_occlusion()
    assert ok, msg
    p = rig.core.p_occ
    assert abs(rig.core.targets.target - (p + 30)) < 0.2
    assert abs(rig.core.targets.run_min - (p + 20)) < 0.2
    rig.run(0.3)
    # the board accepted the new Run gate during INFLATE
    assert abs(rig.board.settings["p_run_min"] - rig.core.targets.run_min) < 0.01
    assert "doppler_occluded" in (rig.core.recorder.dir / "events.csv").read_text()


def test_run_is_refused_below_the_gate(rig):
    rig.core.start()
    rig.run(0.3)
    rig.board.pump_to(100)
    rig.run(3.0)
    ok, msg = rig.core.run()
    assert not ok and "below" in msg


def test_fault_dump_shows_and_still_ends_recording(rig):
    rig.core.start()
    rig.run(0.3)
    rig.board.pump_to(150)
    rig.run(4.0)
    rig.board.fail("ppg_P")
    rig.run(1.0)
    assert rig.core.model.fault == "ppg dropout P"
    assert "DUMPED" in rig.core.status
    rig.run(TAIL_S + 0.5)
    assert rig.core.recorder.status == "unsaved"
    ok, _ = rig.core.discard_run()
    assert ok and rig.core.recorder is None


def test_doppler_return_mark_goes_into_header(rig):
    rig.core.start()
    rig.run(0.3)
    rig.board.pump_to(150)
    rig.run(4.5)
    rig.core.run()
    rig.run(10.5 + 15.0)                              # 30 mmHg into the deflation
    ok, msg = rig.core.mark_return()
    assert ok, msg
    rig.run(50.0 + TAIL_S)
    rig.core.after_raw = {"ref_systolic": "120", "ref_diastolic": "80"}
    rec = rig.core.recorder
    assert rig.core.save_run()[0]
    assert "# doppler_return_mark_mmhg=" in (rec.dir / "distal.csv").read_text()


def test_start_refused_by_board_discards_the_empty_run(rig):
    rig.board.p = 30.0                                # cuff not vented on the board
    rig.board.valve_closed = True
    rig.core.model.p_mmhg = 0.0                       # host thinks it is vented
    ok, _ = rig.core.start()
    assert ok
    rig.run(0.5)
    assert rig.core.recorder is None and "START refused" in rig.core.status


def test_prepare_leds_measures_dark_and_tunes(rig):
    rig.board.amps["D"] = [5, 5]                      # far too dim
    rig.board.amps["P"] = [80, 80]                    # far too bright
    ok, msg = rig.core.prepare_leds()
    assert ok, msg
    rig.run(20.0)
    assert rig.core._prep is None
    for tag in "DP":
        assert "dark_ir" in rig.core.model.info.bands[tag]
        assert 40 <= rig.core._dc_pct(tag, 0) <= 60, (tag, rig.core._dc_pct(tag, 0))
        assert 40 <= rig.core._dc_pct(tag, 1) <= 60, (tag, rig.core._dc_pct(tag, 1))
    assert rig.core.model.streaming


def test_link_loss_mid_run_ends_recording(rig):
    rig.core.start()
    rig.run(0.3)
    rig.board.pump_to(150)
    rig.run(2.0)
    rig.board.close()
    rig.run(0.2)
    assert "LINK DOWN" in rig.core.status
    assert rig.core.recorder.status == "unsaved"
    assert "link_down" in (rig.core.recorder.dir / "events.csv").read_text()
