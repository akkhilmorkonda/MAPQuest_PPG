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
import numpy as np

from expotool.results import FLAG_TEXT, ResultsLog, RunRecorder, analyse, pressure_glitches
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


def test_glitch_filter_keeps_ramps_and_steps_and_drops_lone_readings():
    t = np.arange(0, 20, 0.01)
    p = np.where(t < 5, 0.0, np.where(t < 8, (t - 5) * 60.0, np.maximum(180.0 - (t - 8) * 2.0, 40.0)))
    p[t > 18] = 5.0                                          # the dump: a step down
    assert not pressure_glitches(p).any()                    # inflation, deflation, the dump: all kept
    bad = p.copy()
    hits = [300, 900, 901, 1500]                             # lone readings, one pair
    bad[hits] -= 12.0
    bad[1200] += 6.0
    assert np.flatnonzero(pressure_glitches(bad)).tolist() == sorted(hits + [1200])


class GlitchySession(Session):
    """Every ``every``-th pressure reading comes back ``drop`` mmHg low, the way
    the rig's MPRLS does on a valve switch-off or a bus error."""

    def __init__(self, every, drop=12.0, **kw):
        self.every, self.drop_counts = every, int(drop * (15099494 - 1677722) / (25 * 51.71493))
        super().__init__(**kw)
        feed = self.rec.line

        def mangled(ln):
            f = ln.split(",")
            if f[0] == "S" and len(f) >= 8 and int(f[5]) % self.every == 0:
                f[6] = str(int(f[6]) - self.drop_counts)
                ln = ",".join(f)
            return feed(ln)
        self.rec.line = mangled


def test_pressure_glitches_are_dropped_before_the_reading():
    clean = result_of(Session())
    res = result_of(GlitchySession(every=7))                 # 7: never in step with the 10 Hz trace
    assert res.flag == "ok", res.reason
    assert abs(res.mmhg - clean.mmhg) < 1.0, (res.mmhg, clean.mmhg)
    assert res.detail["pressure_glitches"] > 0.12 * res.detail["pressure_readings"]
    tr, w = res.detail["traces"], res.detail["windows"]      # the close-up's x axis is clean too
    t = np.array(tr["t"])
    p = np.array([np.nan if v is None else v for v in tr["p"]])
    defl = (t > w["deflate"] - w["inflate"]) & (t < w["dump"] - w["inflate"])
    assert defl.sum() > 100
    assert np.nanmax(np.abs(np.diff(p[defl]))) < 2.0         # 2 mmHg/s at 10 Hz is 0.2 a step


def test_a_mostly_glitching_pressure_sensor_is_flagged_incomplete():
    res = result_of(GlitchySession(every=3))
    assert res.flag == "incomplete"
    assert "pressure sensor glitched" in res.reason


# ---- the valve between runs (expo_fw idleValve) --------------------------------

def _valve_events(s):
    return [ln.split(",", 3)[3] for ln in s.lines if ln.startswith("EV,") and ",valve," in ln]


def test_valve_is_closed_in_idle_once_the_cuff_is_empty():
    s = Session()
    s.until(lambda: s.hub.board.valve_closed, limit=6.0)       # boot: empty cuff, closes after the settle
    assert s.t >= hubmod.IDLE_CLOSE_SETTLE_S
    s.say("DUMP")                                              # a dump in IDLE opens it ...
    s.step()
    assert not s.hub.board.valve_closed
    t_open = s.t
    s.until(lambda: s.hub.board.valve_closed, limit=s.t + 6.0)  # ... for the settle time, then closes
    assert s.t - t_open >= hubmod.IDLE_CLOSE_SETTLE_S - 0.1


def test_valve_closes_again_after_a_run():
    s = Session()
    s.run()
    b = s.hub.board
    assert b.state == "IDLE"
    s.until(lambda: b.valve_closed, limit=s.t + 6.0)
    assert _valve_events(s)[-1] == "closed in IDLE"
    # It was open for the whole dump: no close between the dump and IDLE.
    lines = s.lines
    i_dump = max(i for i, ln in enumerate(lines) if ",state,DUMP" in ln)
    i_idle = max(i for i, ln in enumerate(lines) if ",state,IDLE" in ln)
    assert not any(",valve,closed" in ln for ln in lines[i_dump:i_idle])


def test_idle_valve_opens_for_pressure_outside_a_run_or_a_lost_sensor():
    s = Session()
    b = s.hub.board
    s.until(lambda: b.valve_closed, limit=6.0)
    b.p = 60.0                                                 # someone pumped without Start
    s.step(0.2)
    assert not b.valve_closed
    assert _valve_events(s)[-1] == "open in IDLE: cuff pressurised outside a run"
    s.until(lambda: b.valve_closed, limit=s.t + 10.0)          # vents, then closes again
    b.failed.add("pressure")
    s.step(0.2)
    assert not b.valve_closed
    assert _valve_events(s)[-1] == "open in IDLE: no pressure reading"


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


# ---- the proximal band is optional (REQUIRE_PROXIMAL_BAND = false) ------------------

def test_start_runs_without_the_proximal_band_and_still_reads():
    board = ArmBoard(realtime=False)
    board.fail("ppg_P")                           # upper-arm band not connected
    s = Session(board)
    res = analyse(s.run(), 1)
    assert res.flag == "ok" and abs(res.mmhg - SBP) < 5      # the result uses the distal band
    # Still shown as missing: pressure, distal PPG, proximal PPG, distal IMU, proximal IMU.
    assert s.hub.status_line().split(",")[3] == "11011"


def test_start_is_refused_without_the_distal_band():
    board = ArmBoard(realtime=False)
    board.fail("ppg_D")
    s = Session(board)
    s.until(lambda: s.t >= 1.0)
    s.say("START")
    s.step()
    assert s.hub.board.state == "IDLE"
    assert any("START refused: a PPG band is not OK" in ln for ln in s.lines)


def test_losing_the_proximal_band_mid_run_warns_and_the_run_finishes():
    s = Session()
    s.until(lambda: s.t >= 16.0)
    s.say("START")
    s.until(lambda: s.hub.board.p >= hubmod.P_RUN_MIN)
    s.say("RUN")
    s.until(lambda: s.hub.board.state == "DEFLATE")
    s.hub.board.fail("ppg_P")
    s.until(lambda: s.completed())
    assert any(",warning,proximal PPG dropout" in ln for ln in s.lines)
    assert analyse(s.completed()[0], 1).flag == "ok"          # it reached p_stop, no dump


def test_proximal_rule_matches_the_firmware_and_the_phone():
    cfg = (EXPO / "firmware" / "expo_fw" / "config.h").read_text(encoding="utf-8")
    m = re.search(r"REQUIRE_PROXIMAL_BAND\s*=\s*(true|false)", cfg)
    assert (m.group(1) == "true") == hubmod.REQUIRE_PROXIMAL_BAND
    page = (EXPO / "web" / "index.html").read_text(encoding="utf-8")
    blocker = page[page.index("function startBlocker"):page.index("function resultLine")]
    assert "S.sensors[1] !== '1'" in blocker and "S.sensors[2]" not in blocker


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


# ---- the dashboard (served by the results process) --------------------------------

def test_result_payload_is_strict_json_with_the_traces():
    import json
    from expotool.results import payload
    res = result_of(Session())
    p = json.loads(json.dumps(payload(res), allow_nan=False))       # a browser can parse it
    assert p["flag"] == "ok" and abs(p["mmhg"] - SBP) < 5
    on_t, on_p = p["markers"]["refill onset (hinge)"]
    assert p["windows"]["deflate"] < on_t < p["windows"]["dump"]   # times from the start of inflation
    tr = p["traces"]
    assert len(tr["t"]) == len(tr["dv_lp"]) == len(tr["p"]) > 100
    assert tr["hinge"]["t"] and tr["baseline"]["t"] and tr["pulse_env"]


def _collect(ws, until, limit=30.0):
    got, t0 = [], time.monotonic()
    while time.monotonic() - t0 < limit:
        msg = ws.recv(timeout=0.05)
        if msg:
            got += [ln for ln in msg.split("\n") if ln]
            if until(got):
                break
    return got


def test_dashboard_catch_up_is_in_order_and_only_view_goes_back():
    import queue
    from expotool.dashserve import DashServer
    cmds = queue.Queue()
    dash = DashServer(on_command=cmds.put)
    port = dash.start("127.0.0.1", 0)
    try:
        dash.message("hello", next_run=1)
        dash.relay("EV,1000,state,INFLATE\nC,1,1000,INFLATE,10.00,nan,0.000,0.0\n")
        dash.message("run", run=1)
        dash.message("result", run=1, mmhg=120.0, flag="ok")
        time.sleep(0.2)
        page = urllib_get(f"http://127.0.0.1:{port}/")
        assert "<title>MAPQuest Dashboard</title>" in page
        import urllib.error
        import urllib.request
        logo = urllib.request.urlopen(f"http://127.0.0.1:{port}/logo.webp", timeout=5)
        assert logo.headers["Content-Type"] == "image/webp" and logo.read(4) == b"RIFF"
        for other in ("/index.html.bak", "/../host/expo_results.py", "/secrets.h"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{other}", timeout=5)
                raise AssertionError(f"served {other}")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        ws = WSClient(f"ws://127.0.0.1:{port}/ws")
        got = _collect(ws, lambda g: any('"kind":"result"' in ln for ln in g) and g.count(g[-1]) and len(g) >= 6, 3)
        # The latest messages first, then everything in the order it happened.
        hist = got[[i for i, ln in enumerate(got) if ln.startswith("EV,")][0]:]
        kinds = [ln for ln in hist if ln.startswith(("EV,", "A,"))]
        assert kinds[0].startswith("EV,1000,state,INFLATE")
        assert '"kind":"run"' in kinds[1] and '"kind":"result"' in kinds[2]
        for cmd in ("START", "DUMP", "SET p_run_min 200", "view technical"):
            ws.send_text(cmd)
        time.sleep(0.3)
        ws.close()
        sent = []
        while not cmds.empty():
            sent.append(cmds.get_nowait())
        assert sent == ["VIEW TECHNICAL"]          # the mode switch, nothing that touches the cuff
    finally:
        dash.stop()


def urllib_get(url):
    import urllib.request
    return urllib.request.urlopen(url, timeout=5).read().decode()


def test_dashboard_page_needs_no_internet():
    page = (EXPO / "web" / "dashboard.html").read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*"https?:', page)   # the booth laptop is offline
    assert "@import" not in page and "fonts.googleapis" not in page.split("-->", 1)[1]


def test_dashboard_sees_the_whole_run_end_to_end(tmp_path):
    import queue
    import expo_results
    import sim_server
    from expotool.dashserve import DashServer
    _srv, port, stop_srv = sim_server.start_in_thread(speed=8.0)
    url = f"ws://127.0.0.1:{port}/ws"
    cmds = queue.Queue()
    dash = DashServer(on_command=cmds.put)
    dport = dash.start("127.0.0.1", 0)
    mgr = expo_results.Manager(ResultsLog(tmp_path / "r.csv"), out=lambda s: None, dash=dash)
    stop = threading.Event()
    worker = threading.Thread(target=expo_results.run_live, daemon=True,
                              kwargs=dict(url=url, mgr=mgr, ping_s=0.05, out=lambda s: None, stop=stop,
                                          dash=dash, dash_cmds=cmds))
    worker.start()
    phone = WSClient(url)
    monitor = WSClient(f"ws://127.0.0.1:{dport}/ws")
    seen, state, p = [], "?", 0.0
    try:
        phone.send_text("CLAIM")
        monitor.send_text("VIEW TECHNICAL")         # the laptop key, relayed to the board
        t0, last_ping, phase = time.monotonic(), 0.0, "pre"
        while time.monotonic() - t0 < 40:
            if time.monotonic() - last_ping > 0.05:
                phone.send_text("PING")
                last_ping = time.monotonic()
            for ln in (phone.recv(timeout=0.02) or "").split("\n"):
                f = ln.split(",")
                if f[0] == "C" and len(f) > 4:
                    state, p = f[3], float(f[4])
            seen += [ln for ln in (monitor.recv(timeout=0.02) or "").split("\n") if ln]
            if phase == "pre" and time.monotonic() - t0 > 2.5:
                phone.send_text("START")
                phase = "start"
            elif phase == "start" and state == "INFLATE" and p >= hubmod.P_RUN_MIN:
                phone.send_text("RUN")
                phase = "run"
            if any(ln.startswith('A,{"kind":"result"') for ln in seen):
                break
    finally:
        phone.close()
        monitor.close()
        stop.set()
        worker.join(3)
        dash.stop()
        stop_srv()
    assert any(",view,TECHNICAL client" in ln for ln in seen), "the monitor's key reached the board"
    assert any(",view,PUBLIC start" in ln for ln in seen), "and START put it back to public"
    assert sum(ln.startswith("S,") for ln in seen) > 500                 # the stream is relayed
    assert any(ln.startswith('A,{"kind":"run","run":1') for ln in seen)
    assert any(ln.startswith('A,{"kind":"pending"') for ln in seen)
    import json
    res = json.loads(next(ln for ln in seen if ln.startswith('A,{"kind":"result"'))[2:])
    assert res["flag"] == "ok" and abs(res["mmhg"] - SBP) < 5 and res["traces"]["dv_lp"]


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
