"""Trial info checks, run IDs, cuff sizing and inflation targets."""

from irbtool import trialinfo as ti

GOOD = {
    "subject_id": "s004", "run_id": "S004-R03", "distal_cm": "3", "proximal_cm": "4",
    "height_cm": "175", "weight_kg": "72", "arm_circumference_cm": "30",
    "cuff_size": "adult", "fitzpatrick": "III", "session_systolic": "118",
    "session_diastolic": "76",
}


def test_complete_entry_passes():
    c = ti.check(GOOD, before_start=True)
    assert c.ok, c.errors
    assert c.values["subject_id"] == "S004" and c.values["distal_cm"] == 3.0
    assert not c.warnings


def test_missing_fields_block_start_only():
    raw = dict(GOOD)
    del raw["height_cm"], raw["fitzpatrick"]
    assert not ti.check(raw, before_start=True).ok
    assert ti.check(raw, before_start=False).ok


def test_names_are_refused_as_subject_ids():
    c = ti.check(dict(GOOD, subject_id="Jane Doe"), before_start=True)
    assert any("coded ID" in e for e in c.errors)


def test_run_id_must_match_subject():
    c = ti.check(dict(GOOD, run_id="S005-R01"), before_start=True)
    assert any("does not belong" in e for e in c.errors)


def test_ranges_and_systolic_over_diastolic():
    assert not ti.check(dict(GOOD, weight_kg="900"), before_start=True).ok
    assert not ti.check(dict(GOOD, distal_cm="abc"), before_start=True).ok
    assert not ti.check(dict(GOOD, session_systolic="70", session_diastolic="80"), before_start=True).ok
    after = ti.check({"ref_systolic": "110", "ref_diastolic": "120"}, before_start=False)
    assert not after.ok


def test_map_computed_when_monitor_gives_none():
    c = ti.check({"ref_systolic": "120", "ref_diastolic": "75"}, before_start=False)
    assert c.values["ref_map_computed"] == 90.0


def test_cuff_size_warning():
    assert ti.cuff_size_warning(30, "adult") is None
    w = ti.cuff_size_warning(38, "adult")
    assert w and "large adult" in w
    assert "outside" in ti.cuff_size_warning(60, "adult thigh")
    c = ti.check(dict(GOOD, arm_circumference_cm="40"), before_start=True)
    assert c.ok and c.warnings                     # a warning, not a block


def test_run_ids():
    assert ti.first_run_id("S004") == "S004-R01"
    assert ti.next_run_id("S004-R03") == "S004-R04"
    assert ti.next_run_id("S004-R09") == "S004-R10"
    assert ti.next_run_id("S004-R03-2") == "S004-R04"
    assert ti.unique_run_id("S004-R03", set()) == "S004-R03"
    assert ti.unique_run_id("S004-R03", {"S004-R03"}) == "S004-R03-2"
    assert ti.unique_run_id("S004-R03", {"S004-R03", "S004-R03-2"}) == "S004-R03-3"


def test_inflation_targets():
    t = ti.inflation_targets(118, "session systolic", p_max=280, p_stop=40)
    assert (t.target, t.run_min, t.clamped) == (148, 138, False)
    t = ti.inflation_targets(265, "doppler occlusion", p_max=280, p_stop=40)
    assert t.target == 270 and t.run_min == 270 and t.clamped
    t = ti.inflation_targets(20, "session systolic", p_max=280, p_stop=40)
    assert t.run_min == 50 and t.clamped


def test_header_keeps_existing_key_names():
    c = ti.check(dict(GOOD, ref_systolic="121", ref_diastolic="79", notes="leg moved; p_max=999"),
                 before_start=False)
    lines = ti.header_lines(dict(c.values))
    text = "\n".join(lines)
    for key in ("# run_id=S004-R03", "# cuff_to_sensor_cm=3", "# ref_systolic_mmhg=121",
                "# ref_diastolic_mmhg=79", "# ref_map_mmhg=93.0", "# cuff_size=adult",
                "# fitzpatrick=III"):
        assert key in text, key
    notes = next(l for l in lines if l.startswith("# notes="))
    assert "p_max:999" in notes and "p_max=999" not in notes   # no forged header keys
