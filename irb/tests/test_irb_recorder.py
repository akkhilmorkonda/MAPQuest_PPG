"""Run folders: layout, headers, run-ID suffixing, save / discard, and that
the distal file still loads with the existing analysis code."""

import json
from datetime import datetime

import pytest

from irbtool.mock import MockBoard
from irbtool.protocol import DeviceInfo, Meta, parse_line
from irbtool.recorder import PENDING_LINE, RunRecorder, SessionLog, taken_run_ids

TRIAL = {"subject_id": "S004", "run_id": "S004-R01", "distal_cm": 3.0, "proximal_cm": 4.0,
         "height_cm": 175.0, "weight_kg": 72.0, "arm_circumference_cm": 30.0,
         "cuff_size": "adult", "fitzpatrick": "III", "session_systolic": 118.0,
         "session_diastolic": 76.0}


def record_mock_run(root, trial=TRIAL, seconds=3.0, started=None):
    board = MockBoard()
    board.command("STREAM 1")
    info = DeviceInfo()
    lines = board.read_available().decode().splitlines()
    for line in lines:
        rec = parse_line(line)
        if isinstance(rec, Meta):
            info.apply(rec)
    rr = RunRecorder(root, trial, info, session_t0=12.5, started=started)
    board.advance(seconds)
    board.command("START")
    board.advance(0.2)
    for i, line in enumerate(board.read_available().decode().splitlines()):
        rr.write(parse_line(line), line, host_s=i * 0.001)
    rr.mark("doppler_silent", "p=131.0", host_s=9.9)
    return rr


def test_layout_and_counts(tmp_path):
    rr = record_mock_run(tmp_path)
    rr.stop()
    names = sorted(p.name for p in rr.dir.iterdir())
    assert names == ["control.csv", "distal.csv", "events.csv", "imu.csv", "proximal.csv",
                     "raw_serial.txt", "run.json"]
    assert rr.dir.parent.name == "S004" and rr.dir.name.startswith("S004-R01_")
    # 3.2 s of streaming: 100 Hz per PPG, 2 x 50 Hz IMU, 10 Hz controller (+-1 at the ends)
    assert abs(rr.counts["distal"] - 320) <= 1 and rr.counts["proximal"] == rr.counts["distal"]
    assert abs(rr.counts["imu"] - 320) <= 2 and abs(rr.counts["control"] - 32) <= 1
    doc = json.loads((rr.dir / "run.json").read_text())
    assert doc["status"] == "unsaved" and doc["trial"]["cuff_size"] == "adult"


def test_events_include_board_and_operator(tmp_path):
    rr = record_mock_run(tmp_path)
    rr.stop()
    text = (rr.dir / "events.csv").read_text()
    assert ",state,INFLATE,board," in text
    assert ",doppler_silent,p=131.0,operator," in text


def test_run_id_is_suffixed_not_overwritten(tmp_path):
    a = record_mock_run(tmp_path, started=datetime(2026, 11, 12, 14, 0, 0))
    a.stop()
    b = record_mock_run(tmp_path, started=datetime(2026, 11, 12, 14, 5, 0))
    b.stop()
    assert a.run_id == "S004-R01" and b.run_id == "S004-R01-2"
    assert json.loads((b.dir / "run.json").read_text())["run_id_suffixed"] is True
    assert taken_run_ids(tmp_path / "S004") == {"S004-R01", "S004-R01-2"}


def test_save_writes_trial_info_into_every_header(tmp_path):
    rr = record_mock_run(tmp_path)
    rr.save({"ref_systolic": 121.0, "ref_diastolic": 79.0, "ref_map_computed": 93.0})
    for name in ("distal.csv", "proximal.csv", "imu.csv", "control.csv", "events.csv"):
        text = (rr.dir / name).read_text()
        assert PENDING_LINE not in text
        assert "# ref_systolic_mmhg=121" in text and "# fitzpatrick=III" in text
        assert "# trial_info=complete" in text
    # a corrected reference replaces the block rather than appending a second one
    rr.save({"ref_systolic": 122.0})
    text = (rr.dir / "distal.csv").read_text()
    assert text.count("# trial_info=complete") == 1 and "# ref_systolic_mmhg=122" in text
    assert json.loads((rr.dir / "run.json").read_text())["status"] == "saved"


def test_discard_removes_the_folder(tmp_path):
    rr = record_mock_run(tmp_path)
    rr.discard()
    assert not rr.dir.exists()
    with pytest.raises(RuntimeError):
        rr.save({})


def test_distal_csv_loads_with_existing_analysis_code(tmp_path):
    from mapquest.io import load_csv
    rr = record_mock_run(tmp_path)
    rr.save({"ref_systolic": 121.0, "ref_diastolic": 79.0})
    meta, t, channels, extra = load_csv(rr.dir / "distal.csv")
    assert meta.fs == 100.0 and meta.has_pressure
    assert meta.raw["cuff_to_sensor_cm"] == "3" and meta.raw["ref_systolic_mmhg"] == "121"
    assert meta.raw["band"] == "D" and meta.raw["cuff_zero_abs_mmhg"] == "760.000"
    assert len(t) == rr.counts["distal"] and set(channels) == {"ir", "red"} and "p_raw" in extra


def test_session_log(tmp_path):
    log = SessionLog(tmp_path, "S004", stamp="20261112_140000")
    log.write("sent", "START")
    log.write("event", "dump,user")
    log.close()
    lines = log.path.read_text().splitlines()
    assert log.path.name == "session_20261112_140000.log"
    assert lines[0].endswith("\tsent\tSTART") and len(lines) == 2
