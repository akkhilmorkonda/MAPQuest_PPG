"""Link statistics, the receiver end to end over real sockets, and the
firmware contract (constants and formats the Python side mirrors)."""

import re
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import embed_web
from expotool import hub as hubmod
from expotool.hub import LinkSimHub
from expotool.linkstats import EXPECTED_HZ, LinkStats
from expotool.ws import WSClient

EXPO = Path(__file__).resolve().parent.parent
SIM = EXPO / "firmware" / "expo_link_sim"


def stream(seconds, drop_message=None):
    """Feed a hub's broadcast to LinkStats in 40 ms frames, virtual time."""
    hub, stats = LinkSimHub(), LinkStats()
    cid = hub.connect(0.0)
    t, n = 0.0, 0
    while t < seconds:
        t = round(t + 0.04, 6)
        hub.receive(cid, "PING", t)
        lines, direct = hub.tick(t)
        text = "".join(ln + "\n" for ln in direct.get(cid, []) + lines)
        n += 1
        if text and n != drop_message:
            stats.message(text, t)
    return stats


def test_clean_stream_passes_every_rate_check():
    stats = stream(10.0)
    stats.echo(30.0)
    checks = {c.name: c for c in stats.checks()}
    for key in EXPECTED_HZ:
        assert checks[f"{key} rate"].ok, checks[f"{key} rate"].detail
    assert checks["missing samples"].ok
    assert checks["round trip"].ok
    assert checks["unparsed lines"].ok, stats.unknown


def test_a_lost_frame_shows_up_as_missing_samples():
    stats = stream(5.0, drop_message=50)
    assert stats.missing.get("S", 0) >= 3
    assert not {c.name: c for c in stats.checks()}["missing samples"].ok


def test_no_echo_reply_fails_round_trip():
    stats = stream(1.0)
    assert not {c.name: c for c in stats.checks()}["round trip"].ok


# ---- over real sockets --------------------------------------------------------

@pytest.fixture
def server():
    import sim_server
    srv, port, stop = sim_server.start_in_thread()
    yield srv, port
    stop()


def test_page_is_served(server):
    _, port = server
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
    assert "<title>MAPQuest Expo</title>" in body


def test_phone_and_laptop_end_to_end(server):
    _, port = server
    url = f"ws://127.0.0.1:{port}/ws"
    phone, laptop = WSClient(url), WSClient(url)
    seen = {"phone": [], "laptop": []}
    stop = threading.Event()

    def pump(name, ws):
        while not stop.is_set():
            try:
                msg = ws.recv(timeout=0.1)
            except ConnectionError:
                return
            if msg:
                seen[name] += msg.splitlines()

    threads = [threading.Thread(target=pump, args=a, daemon=True) for a in (("phone", phone), ("laptop", laptop))]
    for th in threads:
        th.start()
    try:
        phone.send_text("CLAIM")
        phone.send_text("START")
        t0 = time.monotonic()                 # listen for 1.5 s: ~150 distal samples
        while time.monotonic() - t0 < 1.5:
            phone.send_text("PING")
            time.sleep(0.1)
    finally:
        stop.set()
        for th in threads:
            th.join(1)
        phone.close()
        laptop.close()
    assert seen["phone"][0].startswith("# client_id=")
    assert any(ln.endswith(",controller,1 claimed") for ln in seen["laptop"])
    assert any(ln.endswith(",state,INFLATE") for ln in seen["laptop"]), "the laptop saw the phone's START"
    assert sum(ln.startswith("S,") for ln in seen["laptop"]) > 100


def test_a_stalled_device_costs_the_others_nothing(server):
    """A device that stops reading (screen lock, walked away) must not leave
    gaps in another device's stream. The board's per-client cursors and the
    stand-in's per-client buffers both promise this."""
    import base64
    import os
    import socket
    _, port = server
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    stalled.sendall((f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    laptop = WSClient(f"ws://127.0.0.1:{port}/ws")
    stats = LinkStats()
    try:
        t0 = time.monotonic()                 # the stalled socket is never read again
        while time.monotonic() - t0 < 2.0:
            msg = laptop.recv(timeout=0.1)
            if msg:
                stats.message(msg, time.monotonic() - t0)
    finally:
        laptop.close()
        stalled.close()
    assert stats.lines.get("S", 0) > 150
    assert sum(stats.missing.values()) == 0, stats.missing


def test_receiver_cli_grades_a_short_capture(server, tmp_path):
    import expo_receiver
    _, port = server
    out = []
    save = tmp_path / "cap.txt"
    stats = expo_receiver.run(f"ws://127.0.0.1:{port}/ws", 3.0, save, True, out=out.append)
    assert stats.elapsed > 2.0
    assert stats.rtts_ms, "ECHO round trips measured"
    assert save.read_text().startswith("# saved_by=expo_receiver")
    expo_receiver.report(stats, out=out.append)
    assert any(line.startswith("RESULT:") for line in out)


# ---- firmware contract ----------------------------------------------------------

def test_web_page_header_is_current():
    assert embed_web.main(["--check"]) == 0, "run python expo/tools/embed_web.py"


def _ino():
    return (SIM / "expo_link_sim.ino").read_text(encoding="utf-8")


def test_controller_timeout_matches_firmware():
    cfg = (SIM / "config.h").read_text(encoding="utf-8")
    ms = int(re.search(r"CONTROLLER_TIMEOUT_MS\s*=\s*(\d+)", cfg).group(1))
    assert ms == int(hubmod.CONTROLLER_TIMEOUT_S * 1000)


def test_stand_in_cuts_slow_clients_where_the_board_does():
    import sim_server
    cfg = (SIM / "config.h").read_text(encoding="utf-8")
    ring = int(re.search(r"WS_RING_BYTES\s*=\s*(\d+)", cfg).group(1))
    assert sim_server.MAX_BACKLOG == ring


def test_status_columns_match_firmware():
    m = re.search(r'columns_ST=([a-z_,]+)"\s*\n\s*"([a-z_,]+)"', _ino())
    assert (m.group(1) + m.group(2)).split(",") == list(hubmod.ST_COLUMNS)


def test_open_commands_match_firmware():
    m = re.search(r"kOpenToAll\[\] = \{([^}]*)\}", _ino())
    assert tuple(re.findall(r'"([A-Z]+)"', m.group(1))) == hubmod.OPEN_TO_ALL


# ---- expo_fw, the real firmware, keeps the same contract -------------------------

FW = EXPO / "firmware" / "expo_fw"


def _fw():
    return (FW / "expo_fw.ino").read_text(encoding="utf-8")


def test_both_sketches_carry_the_same_wifi_link():
    for name in ("wifi_link.h", "wifi_link.cpp"):
        assert (FW / name).read_bytes() == (SIM / name).read_bytes(), \
            f"copy expo_link_sim/{name} to expo_fw/ (an Arduino sketch only builds its own folder)"


def test_expo_fw_rules_match_the_link_simulator():
    m = re.search(r"kOpenToAll\[\] = \{([^}]*)\}", _fw())
    assert tuple(re.findall(r'"([A-Z]+)"', m.group(1))) == hubmod.OPEN_TO_ALL
    m = re.search(r"kController\[\] = \{([^}]*)\}", _fw())
    controller = set(re.findall(r'"([A-Z]+)"', m.group(1)))
    # Exactly what the phone page sends beyond the open commands, nothing more.
    page = (EXPO / "web" / "index.html").read_text(encoding="utf-8")
    sent = set(re.findall(r"(?:cmd|send)\('([A-Z]+)", page)) - set(hubmod.OPEN_TO_ALL) - {"PING"}
    assert controller == sent
    for bench in ("DARK", "AMP", "SCAN", "VALVE", "TEST", "STREAM", "RADIO"):
        assert bench not in controller


def test_idle_valve_settings_match_the_link_simulator():
    cfg = (FW / "config.h").read_text(encoding="utf-8")
    assert float(re.search(r"IDLE_VENT_ABOVE_MMHG\s*=\s*([\d.]+)", cfg).group(1)) == hubmod.IDLE_VENT_ABOVE
    assert int(re.search(r"IDLE_CLOSE_SETTLE_MS\s*=\s*(\d+)", cfg).group(1)) == int(hubmod.IDLE_CLOSE_SETTLE_S * 1000)
    fw = _fw()
    assert "idleValve(now, pAvg)" in fw                        # called from the IDLE control window
    # Every opening restarts the settle wait: valveLockOpen() clears it.
    lock_open = re.search(r"static void valveLockOpen\(\) \{(.*?)\n\}", fw, re.S).group(1)
    assert "g_idle_empty_ms = 0" in lock_open
    # The settle start is never later than `now` (an unsigned difference would wrap).
    body = re.search(r"static void idleValve\(.*?\n\}", fw, re.S).group(0)
    start = re.search(r"g_idle_empty_ms == 0\) g_idle_empty_ms = ([^;]+);", body).group(1)
    assert "|" not in start and "+" not in start, start


def test_refresh_reprobes_the_sensors_for_the_controller_only():
    hub = LinkSimHub()
    a, b = hub.connect(0.0), hub.connect(0.0)
    hub.receive(a, "CLAIM", 0.0)
    hub.tick(0.1)
    hub.receive(b, "R", 0.2)
    lines, _ = hub.tick(0.3)
    assert any("R refused: client" in ln for ln in lines)
    hub.receive(a, "R", 0.4)
    lines, _ = hub.tick(0.5)
    assert not any("refused" in ln for ln in lines), lines
    assert any(ln.startswith("# band=D") for ln in lines)        # INFO: every sensor's status
    assert any(",stream_gap,R" in ln for ln in lines)


def test_phone_refresh_never_reconnects_during_a_run():
    page = (EXPO / "web" / "index.html").read_text(encoding="utf-8")
    body = re.search(r"\$\('btnRefresh'\)\.onclick = \(\) => \{(.*?)\n\};", page, re.S).group(1)
    i_run, i_close, i_r = body.index("S.state !== 'IDLE'"), body.index("ws.close()"), body.index("cmd('R')")
    assert i_run < i_close and i_run < i_r                     # the run check comes first


def test_expo_fw_status_columns_and_timeout_match():
    m = re.search(r'columns_ST=([a-z_,]+)"\s*\n\s*"([a-z_,]+)"', _fw())
    assert (m.group(1) + m.group(2)).split(",") == list(hubmod.ST_COLUMNS)
    cfg = (FW / "config.h").read_text(encoding="utf-8")
    assert int(re.search(r"CONTROLLER_TIMEOUT_MS\s*=\s*(\d+)", cfg).group(1)) == int(hubmod.CONTROLLER_TIMEOUT_S * 1000)
    st = re.search(r'dataLine\("ST,([^"]*)\\n"', _fw()).group(1)
    assert st.count(",") + 1 == len(hubmod.ST_COLUMNS)   # one format field per column


def test_expo_fw_keeps_the_irb_safety_limits():
    irb = (EXPO.parent / "irb" / "firmware" / "irb_fw" / "config.h").read_text(encoding="utf-8")
    fw = (FW / "config.h").read_text(encoding="utf-8")
    for key in ("P_MAX_HARD_MMHG", "HOLD_TIMEOUT_HARD_S", "VENTED_BELOW_MMHG", "PRESSURE_DROPOUT_MS",
                "PPG_DROPOUT_MS", "WDT_TIMEOUT_MS", "PIN_VALVE", "DEFAULT_P_STOP"):
        pat = rf"{key}\s*=\s*([^;]+);"
        assert re.search(pat, fw).group(1) == re.search(pat, irb).group(1), key


def test_secrets_are_not_tracked_but_the_example_is():
    gi = (EXPO.parent / ".gitignore").read_text(encoding="utf-8")
    assert "expo/firmware/*/secrets.h" in gi
    assert (SIM / "secrets.example.h").exists()
