"""The results manager (expo/DESIGN.md section 7) and the display commands
(3.1): runs simulated on the stand-in's board model, in virtual time, then
the whole loop over real sockets."""

import csv
import re
import threading
import time
from pathlib import Path

import pytest

from expotool import hub as hubmod
from expotool.hub import ArmBoard, LinkSimHub
from expotool.results import FLAG_TEXT, ResultsLog, RunRecorder, analyse
from expotool.ws import WSClient
from irbtool.mock import MockBoard

EXPO = Path(__file__).resolve().parent.parent
SBP = 120.0                                   # the board model's systolic: where flow returns


class Session:
    """A phone driving the hub in virtual time, every line fed to a recorder."""

    def __init__(self, board=None, drop=None):
        self.hub = LinkSimHub(board)
        self.rec = RunRecorder()
        self.cid = self.hub.connect(0.0)
        self.t = 0.0
        self.drop = drop
        self.signals = []
        self.lines = []
        self.say("CLAIM")
        self.say("INFO")

    def say(self, text):
        self.hub.receive(self.cid, text, self.t)

    def step(self, dt=0.04):
        self.t = round(self.t + dt, 6)
        self.say("PING")
        lines, direct = self.hub.tick(self.t)
        for ln in direct.get(self.cid, []) + lines:
            if self.drop and self.drop[0] <= self.t <= self.drop[1] and ln.startswith("S,"):
                continue
            self.lines.append(ln)
            self.signals += self.rec.line(ln)

    def until(self, cond, limit=300.0):
        while not cond() and self.t < limit:
            self.step()
        assert cond(), f"timed out at t={self.t}"

    def run(self, dump_below=None, pre_roll=16.0):
        self.until(lambda: self.t >= pre_roll)
        self.say("START")
        self.until(lambda: self.hub.board.p >= hubmod.P_RUN_MIN)
        self.say("RUN")
        if dump_below is not None:
            self.until(lambda: self.hub.board.state == "DEFLATE" and self.hub.board.p < dump_below)
            self.say("DUMP")
        self.until(lambda: self.completed())
        return self.completed()[0]

    def completed(self):
        return [r for k, r in self.signals if k == "complete"]


def result_of(session, **kw):
    return analyse(session.run(**kw), 1)


# ---- the analysis -------------------------------------------------------------

def test_a_normal_run_reads_near_systolic():
    res = result_of(Session())
    assert res.flag == "ok", res.reason
    assert abs(res.mmhg - SBP) < 5.0, res.mmhg
    assert res.command() == f"RESULT 1 {res.mmhg:.1f} ok"
    assert res.markers["pulse return"] is not None     # technical-mode markers are kept too
    assert res.seconds < 2.0


def test_the_windows_come_from_the_board_events():
    s = Session()
    res = analyse(s.run(), 1)
    w = res.detail["windows"]
    assert w["inflate"] < w["hold"] < w["deflate"] < w["dump"]
    assert res.detail["p_deflate_start"] > hubmod.P_RUN_MIN
    assert w["ref"][1] == w["deflate"]


def test_a_pulse_that_never_stops_is_flagged():
    res = result_of(Session(MockBoard(realtime=False)))      # MockBoard's pulse ignores the cuff
    assert res.flag == "pulse_present"
    assert res.mmhg is not None                               # still a number, with the caution


def test_a_hole_in_the_stream_is_flagged_incomplete():
    res = result_of(Session(drop=(40.0, 40.5)))
    assert res.flag == "incomplete"
    assert "samples missing" in res.reason


def test_no_refill_means_no_number():
    res = result_of(Session(ArmBoard(realtime=False, sbp=30.0)))   # flow returns below p_stop
    assert res.flag == "no_onset" and res.mmhg is None
    assert res.command() == "RESULT 1 - no_onset"


def test_a_stopped_run_gets_no_number():
    res = result_of(Session(), dump_below=100.0)
    assert (res.flag, res.mmhg) == ("stopped", None)


def test_a_run_dumped_before_deflation_is_not_a_run():
    s = Session()
    s.until(lambda: s.t >= 5.0)
    s.say("START")
    s.until(lambda: s.hub.board.p >= 100)
    s.say("DUMP")
    s.until(lambda: s.hub.board.state == "IDLE" and s.t > 12.0)
    assert s.signals == []


def test_every_flag_has_phone_text():
    page = (EXPO / "web" / "index.html").read_text(encoding="utf-8")
    for flag in ("arm_moved", "pulse_present", "incomplete"):
        assert flag in page
    for flag in ("no_onset", "safety_stop", "stopped", "pending"):
        assert f"'{flag}'" in page
    assert set(FLAG_TEXT) | {"pending"} == set(hubmod.RESULT_FLAGS)


# ---- the manager: numbering, pending, the anonymous file, replay ---------------------

def _manager(tmp_path, **kw):
    import expo_results
    return expo_results.Manager(ResultsLog(tmp_path / "results.csv"), out=lambda s: None,
                                threaded=False, **kw)


def test_manager_posts_pending_then_the_result_and_keeps_only_a_row(tmp_path):
    s = Session()
    s.run()
    mgr = _manager(tmp_path)
    sent = []
    for ln in s.lines:
        sent += mgr.line(ln)
    sent += mgr.poll()
    assert sent[0] == "RESULT 1 - pending"
    assert re.fullmatch(r"RESULT 1 \d+\.\d ok", sent[1]), sent
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert list(rows[0]) == ["run", "time", "mmhg", "flag", "reason"]   # no signal, no person
    assert rows[0]["run"] == "1" and rows[0]["flag"] == "ok"
    # Numbering carries on from the file across a restart.
    assert ResultsLog(tmp_path / "results.csv").next_run() == 2


def test_raw_lines_are_kept_only_when_asked(tmp_path):
    s = Session()
    s.run()
    mgr = _manager(tmp_path, save_raw=tmp_path / "raw")
    for ln in s.lines:
        mgr.line(ln)
    mgr.poll()
    saved = tmp_path / "raw" / "run_0001.txt"
    assert saved.exists()
    # ...and a saved run replays to the same result.
    import expo_results
    again = expo_results.Manager(ResultsLog(None), out=lambda s: None, threaded=False)
    expo_results.replay(saved, again)
    assert again.results[0].mmhg == mgr.results[0].mmhg
    assert not (tmp_path / "raw2").exists()


# ---- display commands on the board model ---------------------------------------

def test_view_is_open_to_watchers_and_start_returns_to_public():
    h = LinkSimHub()
    phone, laptop = h.connect(0), h.connect(0)
    h.receive(phone, "CLAIM", 0)
    h.receive(laptop, "VIEW TECHNICAL", 0)          # the laptop is not the controller
    assert h.view_technical
    assert h.status_line().split(",")[13] == "TECHNICAL"      # field 0 is the "ST" tag
    h.receive(phone, "START", 0.1)
    assert not h.view_technical
    out, _ = h.tick(0.2)
    assert any(",view,PUBLIC start" in ln for ln in out)


def test_result_is_validated_stored_and_repeated_in_info():
    h = LinkSimHub()
    c = h.connect(0)
    for bad in ("RESULT", "RESULT 0 120 ok", "RESULT 3 abc ok", "RESULT 3 400 ok", "RESULT 3 120 great"):
        h.receive(c, bad, 0)
    out, _ = h.tick(0.05)
    assert sum(ln.startswith("E,RESULT") for ln in out) == 5
    assert h.result == ""
    h.receive(c, "RESULT 3 118.4 arm_moved", 0.1)
    h.receive(c, "INFO", 0.1)
    out, _ = h.tick(0.2)
    assert any(ln.endswith(",result,run=3 mmhg=118.4 flag=arm_moved") for ln in out)
    assert "# result run=3 mmhg=118.4 flag=arm_moved" in out


def test_the_analyser_light_follows_the_laptop_process():
    h = LinkSimHub()
    a = h.connect(0)
    h.receive(a, "ANALYSER", 0)
    assert h.status_line().split(",")[14] == str(a)
    h.tick(1.0)
    assert h.analyser == a
    h.tick(1.0 + hubmod.CONTROLLER_TIMEOUT_S + 0.5)          # went quiet
    assert h.analyser == 0


# ---- the whole loop over real sockets ------------------------------------------------

def test_phone_board_and_results_process_end_to_end(tmp_path):
    import expo_results
    import sim_server
    speed = 8.0                                     # an 80 s run in ~10 s
    _srv, port, stop_srv = sim_server.start_in_thread(speed=speed)
    url = f"ws://127.0.0.1:{port}/ws"
    mgr = expo_results.Manager(ResultsLog(tmp_path / "r.csv"), out=lambda s: None)
    stop = threading.Event()
    worker = threading.Thread(target=expo_results.run_live,
                              kwargs=dict(url=url, mgr=mgr, ping_s=0.05, out=lambda s: None, stop=stop),
                              daemon=True)
    worker.start()
    phone = WSClient(url)
    results, state, p = [], "?", 0.0
    try:
        phone.send_text("CLAIM")
        t0, last_ping, phase = time.monotonic(), 0.0, "pre"
        while time.monotonic() - t0 < 40:
            if time.monotonic() - last_ping > 0.05:      # under 2 s of simulated time
                phone.send_text("PING")
                last_ping = time.monotonic()
            msg = phone.recv(timeout=0.05) or ""
            for ln in msg.split("\n"):
                f = ln.split(",")
                if f[0] == "C" and len(f) > 4:
                    state, p = f[3], float(f[4])
                elif f[0] == "EV" and len(f) > 3 and f[2] == "result":
                    results.append(",".join(f[3:]))
            if phase == "pre" and time.monotonic() - t0 > 2.5:     # 20 s of pre-roll
                phone.send_text("START")
                phase = "start"
            elif phase == "start" and state == "INFLATE" and p >= hubmod.P_RUN_MIN:
                phone.send_text("RUN")
                phase = "run"
            if any("flag=pending" not in r for r in results):
                break
    finally:
        phone.close()
        stop.set()
        worker.join(3)
        stop_srv()
    assert results and results[0].endswith("flag=pending"), results
    final = re.fullmatch(r"run=1 mmhg=([\d.]+) flag=ok", results[-1])
    assert final, results
    assert abs(float(final.group(1)) - SBP) < 5.0


# ---- firmware contract ---------------------------------------------------------------

def _ino(name):
    return (EXPO / "firmware" / name / f"{name}.ino").read_text(encoding="utf-8")


def test_both_sketches_carry_the_same_display_block():
    blocks = []
    for name in ("expo_link_sim", "expo_fw"):
        src = _ino(name)
        a = src.index("// Display state (expo/DESIGN.md 3.1).")
        b = src.index("}\n", src.index("static void printDisplayInfo() {")) + 2
        blocks.append(src[a:b])
    assert blocks[0] == blocks[1], "copy the display block from expo_link_sim.ino into expo_fw.ino"


@pytest.mark.parametrize("name", ["expo_link_sim", "expo_fw"])
def test_result_flags_match_the_firmware(name):
    m = re.search(r"kResultFlags\[\] = \{([^}]*)\}", _ino(name))
    assert tuple(re.findall(r'"([a-z_]+)"', m.group(1))) == hubmod.RESULT_FLAGS


@pytest.mark.parametrize("name", ["expo_link_sim", "expo_fw"])
def test_run_gate_default_is_the_expo_profile(name):
    cfg = (EXPO / "firmware" / name / "config.h").read_text(encoding="utf-8")
    assert float(re.search(r"DEFAULT_P_RUN_MIN\s*=\s*([\d.]+)f", cfg).group(1)) == hubmod.P_RUN_MIN


def test_results_folder_is_not_tracked():
    assert "expo/results/" in (EXPO.parent / ".gitignore").read_text(encoding="utf-8")
