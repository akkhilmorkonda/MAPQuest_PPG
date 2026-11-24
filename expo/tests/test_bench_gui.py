"""The bench GUI names its recordings by run number, sharing the expo results' numbering."""
import bench_gui as g


def _tree(tmp_path, results_last=5, raw=(5,), bench=()):
    (tmp_path / "raw").mkdir(parents=True)
    (tmp_path / "bench").mkdir()
    (tmp_path / "expo_results.csv").write_text(
        "run,time,mmhg,flag,reason\n" + (f"{results_last},t,1,ok,\n" if results_last else ""), encoding="utf-8")
    for n in raw:
        (tmp_path / "raw" / f"run_{n:04d}.txt").write_text("", encoding="utf-8")
    for name in bench:
        (tmp_path / "bench" / name).mkdir()
    return tmp_path / "bench", tmp_path


def test_runs_count_on_from_the_results_file_and_the_bench_folders(tmp_path):
    assert g.highest_run(*_tree(tmp_path / "a")) == 5                       # results file and raw
    assert g.highest_run(*_tree(tmp_path / "b", bench=("run_0006", "run_0007"))) == 7
    assert g.highest_run(*_tree(tmp_path / "c", bench=("run_0008-0009",))) == 9   # two runs in one folder
    ignored = ("no_run_20261121_140000", "rec_20261121_132235", "run_0012_extra", "cleared_runs_1-4")
    assert g.highest_run(*_tree(tmp_path / "d", bench=ignored)) == 5
    assert g.highest_run(*_tree(tmp_path / "e", results_last=0, raw=(3, 11))) == 11


def test_reference_fields_are_checked():
    assert g.parse_reference("118", "76") == ((118.0, 76.0, None), "")
    assert g.parse_reference("118", "76", "64")[0] == (118.0, 76.0, 64.0)
    for sbp, dbp, hr, why in (("", "76", "", "numbers"), ("76", "118", "", "below systolic"),
                              ("300", "80", "", "systolic"), ("118", "20", "", "diastolic"),
                              ("118", "76", "x", "heart rate"), ("118", "76", "300", "heart rate")):
        vals, msg = g.parse_reference(sbp, dbp, hr)
        assert vals is None and why in msg, (sbp, dbp, hr, msg)


def test_reference_summary_matches_readings_to_the_cuff(tmp_path):
    res, ref = tmp_path / "bench_results.csv", tmp_path / "references.csv"
    for row in ({"run": 9, "mmhg": "96.9", "flag": "ok"}, {"run": 10, "mmhg": "", "flag": "no_onset"},
                {"run": 11, "mmhg": "95.0", "flag": "ok"}, {"run": 11, "mmhg": "95.7", "flag": "ok"}):
        g.append_row(res, g.RESULT_FIELDS, row)
    for run, s, d in ((9, 120, 78), (9, 116, 74), (10, 118, 76), (11, 110, 71), (12, 112, 70)):
        g.append_row(ref, g.REF_FIELDS, {"run": run, "when": "after run", "ref_sbp": s, "ref_dbp": d})
    summ = g.reference_summary(res, ref)
    # run 10 has no number, run 12 no reading: only 9 and 11 compare; 11 uses its latest reading
    assert [r["run"] for r in summ["runs"]] == [9, 11]
    r9, r11 = summ["runs"]
    assert r9["sbp"] == 118 and r9["dbp"] == 76 and r9["n_refs"] == 2          # two references averaged
    assert abs(r9["err_sbp"] - (96.9 - 118)) < 1e-9
    assert abs(r9["map"] - (76 + (118 - 76) / 3)) < 1e-9
    assert abs(r11["err_sbp"] - (95.7 - 110)) < 1e-9
    assert abs(summ["mean_sbp"] - ((96.9 - 118) + (95.7 - 110)) / 2) < 1e-9
    assert g.reference_summary(tmp_path / "none.csv", ref)["n"] == 0


def test_a_finished_recording_is_named_by_its_runs():
    assert g.run_folder_name(8, [8]) == "run_0008"
    assert g.run_folder_name(8, [8, 9]) == "run_0008-0009"
    assert g.run_folder_name(8, []).startswith("no_run_")                    # its number is reused
