"""The link simulator's wireless rules, on the Python model (expotool.hub).

Virtual time: every test drives ``hub.tick(now)`` itself.
"""

import pytest

from expotool.hub import CONTROLLER_TIMEOUT_S, LinkSimHub


class Rig:
    def __init__(self):
        self.hub = LinkSimHub()
        self.t = 0.0
        self.seen: dict[int, list[str]] = {}

    def connect(self):
        cid = self.hub.connect(self.t)
        self.seen[cid] = []
        return cid

    def step(self, seconds, ping=(), dt=0.04):
        """Advance; every client in ``ping`` sends PING each tick."""
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t = round(self.t + dt, 6)
            for cid in ping:
                self.hub.receive(cid, "PING", self.t)
            lines, direct = self.hub.tick(self.t)
            for cid in self.seen:
                if cid in self.hub.clients:
                    self.seen[cid] += direct.get(cid, []) + lines

    def send(self, cid, text, ping=()):
        self.hub.receive(cid, text, self.t)
        self.step(0.04, ping=ping)

    def events(self, cid, name):
        return [ln.split(",", 3)[3] for ln in self.seen[cid] if ln.startswith(f"EV,") and ln.split(",")[2] == name]

    def errors(self, cid):
        return [ln[2:] for ln in self.seen[cid] if ln.startswith("E,")]


@pytest.fixture
def rig():
    return Rig()


def test_welcome_and_stream_reach_every_client(rig):
    a, b = rig.connect(), rig.connect()
    rig.step(1.0, ping=(a, b))
    assert rig.seen[a][0] == f"# client_id={a}"
    assert rig.seen[b][0] == f"# client_id={b}"
    for cid in (a, b):
        kinds = {ln.split(",")[0] for ln in rig.seen[cid]}
        assert {"S", "SP", "M", "C", "ST"} <= kinds


def test_first_claim_wins_and_others_are_refused(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(b, "CLAIM", ping=(a, b))
    assert rig.hub.controller == a
    assert any("CLAIM refused: client 1 has control" in e for e in rig.errors(b))
    assert rig.events(b, "controller") == [f"{a} claimed"]


def test_viewer_commands_are_refused_but_dump_works_from_anyone(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(b, "START", ping=(a, b))
    assert rig.hub.board.state == "IDLE"
    assert any(e.startswith("START refused: client 2 does not have control") for e in rig.errors(b))

    rig.send(a, "START", ping=(a, b))
    assert rig.hub.board.state == "INFLATE"
    rig.send(b, "DUMP", ping=(a, b))          # the viewer may always vent the cuff
    assert rig.hub.board.state == "DUMP"
    assert "user" in rig.events(a, "dump")
    assert "client 2" in rig.events(a, "dump_request")


def test_watching_commands_are_open_to_all(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(b, "ECHO 7", ping=(a, b))
    rig.send(b, "P", ping=(a, b))
    assert "# echo=7 client=2" in rig.seen[b]
    assert any(ln.startswith("# p_mmhg=") for ln in rig.seen[b])
    assert not rig.errors(b)


def test_full_simulated_run_from_the_phone(rig):
    a = rig.connect()
    rig.send(a, "CLAIM", ping=(a,))
    rig.send(a, "START", ping=(a,))
    rig.step(5.0, ping=(a,))                  # the simulated bulb pumps to 160
    assert rig.hub.board.p >= 140
    rig.send(a, "RUN", ping=(a,))
    assert rig.hub.board.state == "HOLD"
    rig.step(10.0 + 70.0, ping=(a,))          # hold, then 160 -> 40 at 2 mmHg/s
    assert "reached stop pressure" in rig.events(a, "dump")
    rig.step(3.0, ping=(a,))
    assert rig.hub.board.state == "IDLE"


def test_controller_lost_while_inflating_dumps_the_cuff(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(a, "START", ping=(a, b))
    rig.step(2.0, ping=(a, b))
    rig.step(CONTROLLER_TIMEOUT_S + 0.3, ping=(b,))       # the phone goes quiet
    assert rig.hub.controller == 0
    assert "none lost" in rig.events(b, "controller")
    assert "controller lost" in rig.events(b, "dump")


def test_controller_disconnecting_while_holding_dumps_the_cuff(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(a, "START", ping=(a, b))
    rig.step(5.0, ping=(a, b))
    rig.send(a, "RUN", ping=(a, b))
    rig.hub.disconnect(a)
    rig.step(0.1, ping=(b,))
    assert "controller lost" in rig.events(b, "dump")


def test_controller_lost_while_deflating_only_warns(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(a, "SET hold_s 1", ping=(a, b))
    rig.send(a, "START", ping=(a, b))
    rig.step(5.0, ping=(a, b))
    rig.send(a, "RUN", ping=(a, b))
    rig.step(2.0, ping=(a, b))
    assert rig.hub.board.state == "DEFLATE"
    rig.step(CONTROLLER_TIMEOUT_S + 0.3, ping=(b,))
    assert rig.hub.board.state == "DEFLATE"
    assert any("controller lost during DEFLATE" in w for w in rig.events(b, "warning"))
    assert "controller lost" not in rig.events(b, "dump")


def test_force_claim_only_when_no_run_is_active(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.send(a, "START", ping=(a, b))
    rig.send(b, "CLAIM FORCE", ping=(a, b))
    assert rig.hub.controller == a
    assert any("a run is active" in e for e in rig.errors(b))
    rig.send(a, "DUMP", ping=(a, b))
    rig.step(3.0, ping=(a, b))
    rig.send(b, "CLAIM FORCE", ping=(a, b))
    assert rig.hub.controller == b
    assert f"{b} taken from {a}" in rig.events(a, "controller")


def test_release_refused_during_own_run(rig):
    a = rig.connect()
    rig.send(a, "CLAIM", ping=(a,))
    rig.send(a, "START", ping=(a,))
    rig.send(a, "RELEASE", ping=(a,))
    assert rig.hub.controller == a
    rig.send(a, "DUMP", ping=(a,))
    rig.step(3.0, ping=(a,))
    rig.send(a, "RELEASE", ping=(a,))
    assert rig.hub.controller == 0


def test_zero_reports_a_stream_gap_and_stream_is_serial_only(rig):
    a = rig.connect()
    rig.send(a, "CLAIM", ping=(a,))
    rig.send(a, "ZERO", ping=(a,))
    rig.send(a, "STREAM 0", ping=(a,))
    assert "ZERO" in rig.events(a, "stream_gap")
    assert any("STREAM is serial-only" in e for e in rig.errors(a))


def test_status_line_reports_controller_and_clients(rig):
    a, b = rig.connect(), rig.connect()
    rig.send(a, "CLAIM", ping=(a, b))
    rig.step(1.0, ping=(a, b))
    st = [ln for ln in rig.seen[b] if ln.startswith("ST,")][-1].split(",")
    assert st[2] == "IDLE" and st[3] == "11111" and st[5] == str(a) and st[6] == "2"


def test_no_output_is_buffered_with_no_clients():
    hub = LinkSimHub()
    lines, direct = hub.tick(1.0)
    assert lines == [] and direct == {}
