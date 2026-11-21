"""The USB bridge: the board's Wi-Fi client rules applied on the laptop."""

import asyncio
import threading
import time

from expotool.usbhub import CONTROLLER_TIMEOUT_S, UsbHub
from expotool.ws import WSClient

# A status line as expo_fw sends it: the board's own controller (0), Wi-Fi
# clients (0) and analyser (0) are what the bridge must replace.
ST = "ST,1000,IDLE,11011,1,0,0,150.0,10.0,2.00,280.0,0,900,PUBLIC,0"


def lines(hub, now):
    out, direct = hub.tick(now)
    return out, direct


def test_welcome_claim_and_status_rewrite():
    hub = UsbHub(0.0)
    a = hub.connect(0.0)
    hub.receive(a, "CLAIM", 0.1)
    hub.board_lines([ST, "# controller=0 controller_timeout_ms=2000 run_by_wifi=0",
                     "# view=PUBLIC analyser=0"], 0.2)
    out, direct = lines(hub, 0.2)
    assert direct[a][0] == f"# client_id={a}"
    assert any(ln.startswith("EV,") and ",controller," in ln and ln.endswith(f"{a} claimed") for ln in out)
    st = next(ln for ln in out if ln.startswith("ST,")).split(",")
    assert st[5] == str(a) and st[6] == "1"                     # ours, not the board's Wi-Fi side
    assert f"# controller={a} controller_timeout_ms=2000 run_by_wifi=0" in out
    assert hub.to_board == []                                   # CLAIM never reaches the board


def test_only_the_controller_drives_the_cuff():
    hub = UsbHub(0.0)
    a, b = hub.connect(0.0), hub.connect(0.0)
    hub.receive(a, "CLAIM", 0.0)
    hub.receive(b, "START", 0.0)
    hub.receive(b, "DUMP", 0.0)                                 # DUMP: anyone, always
    hub.receive(a, "START", 0.0)
    out, _ = lines(hub, 0.0)
    assert any("START refused: client 2 does not have control" in ln for ln in out)
    assert hub.to_board == ["DUMP", "START"]


def test_zero_and_refresh_pause_the_usb_stream():
    hub = UsbHub(0.0)
    a = hub.connect(0.0)
    hub.receive(a, "CLAIM\nZERO\nR", 0.0)
    out, _ = lines(hub, 0.0)
    assert hub.to_board == ["STREAM 0", "ZERO", "STREAM 1", "STREAM 0", "R", "STREAM 1"]
    assert sum(",stream_gap," in ln for ln in out) == 2


def test_bench_commands_and_wide_sets_are_refused():
    hub = UsbHub(0.0)
    a = hub.connect(0.0)
    hub.receive(a, "CLAIM\nVALVE TEST\nSET p_max 400\nSET rate 3", 0.0)
    out, _ = lines(hub, 0.0)
    assert hub.to_board == ["SET rate 3"]
    assert any("VALVE refused" in ln for ln in out) and any("SET p_max refused" in ln for ln in out)


def test_a_lost_controller_dumps_during_inflate_and_warns_during_deflate():
    for state, expect_dump in (("INFLATE", True), ("HOLD", True), ("DEFLATE", False), ("IDLE", False)):
        hub = UsbHub(0.0)
        a = hub.connect(0.0)
        hub.receive(a, "CLAIM\nSTART", 0.0)
        hub.to_board.clear()
        hub.board_lines([f"EV,5000,state,{state}"], 0.5)
        out, _ = lines(hub, CONTROLLER_TIMEOUT_S + 1.0)        # the tab went silent
        assert ("DUMP" in hub.to_board) == expect_dump, state
        assert any(",controller,none lost" in ln for ln in out)
        if state == "DEFLATE":
            assert any("controller lost during DEFLATE" in ln for ln in out)


def test_the_boards_own_controller_events_are_not_passed_on():
    hub = UsbHub(0.0)
    hub.connect(0.0)
    hub.board_lines(["EV,1,controller,none lost", "EV,2,state,INFLATE", "S,1,3,4,5,6,7,64"], 0.0)
    out, _ = lines(hub, 0.0)
    assert out == ["EV,2,state,INFLATE", "S,1,3,4,5,6,7,64"]


def test_a_silent_board_is_asked_for_its_stream_again():
    hub = UsbHub(0.0)
    hub.connect(0.0)
    lines(hub, 1.0)
    assert hub.to_board == []
    lines(hub, 4.0)
    assert hub.to_board == ["STREAM 1"]
    hub.to_board.clear()
    hub.board_lines([ST], 4.1)
    lines(hub, 8.0)
    assert hub.to_board == ["STREAM 1"]                         # stale again after 3 s


class FakeLink:
    """SerialLink's interface, with a scripted board behind it."""

    def __init__(self):
        self.sent = []
        self.pending = [ST]
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            out, self.pending = self.pending, []
        return out, []

    def write(self, lines):
        with self.lock:
            self.sent.extend(lines)
            for ln in lines:
                if ln == "START":
                    self.pending.append("EV,2000,state,INFLATE")


def test_the_page_protocol_end_to_end_over_a_socket():
    import usb_bridge
    link = FakeLink()
    srv = usb_bridge.BridgeServer(link, say=lambda s: None)
    loop = asyncio.new_event_loop()
    box, ready = {}, threading.Event()

    def runner():
        asyncio.set_event_loop(loop)
        box["port"] = loop.run_until_complete(srv.start("127.0.0.1", 0))
        ready.set()
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()
    assert ready.wait(5)
    try:
        ws = WSClient(f"ws://127.0.0.1:{box['port']}/ws")
        ws.send_text("CLAIM")
        ws.send_text("START")
        got, t0 = [], time.monotonic()
        while time.monotonic() - t0 < 3 and not any(",state,INFLATE" in g for g in got):
            ws.send_text("PING")
            got += [ln for ln in (ws.recv(timeout=0.1) or "").split("\n") if ln]
            with link.lock:
                link.pending.append(ST)
        ws.close()
        assert got[0].startswith("# client_id=")
        assert any(",state,INFLATE" in g for g in got)
        assert "START" in link.sent
        st = [g for g in got if g.startswith("ST,")][-1].split(",")
        assert st[5] == got[0].split("=")[1]                    # the page sees itself in control
    finally:
        loop.call_soon_threadsafe(loop.stop)
