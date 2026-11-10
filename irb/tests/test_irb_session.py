"""End to end against the mock board: the link thread, the device model,
the session state machine and every dump reason."""

import queue
import time

import pytest

from irbtool.link import Link
from irbtool.mock import MockBoard
from irbtool.model import BAD, OK, DeviceModel
from irbtool.protocol import Event


class Session:
    """A link to a mock board, with the model fed from the queue."""

    def __init__(self, **kw):
        self.board = MockBoard(**kw)
        self.q = queue.Queue()
        self.link = Link(self.board, self.q)
        self.model = DeviceModel()
        self.events: list[Event] = []
        self.errors: list[str] = []
        self.sent: list[str] = []
        self.link.start()

    def run(self, seconds):
        """Advance the board, then let the link thread drain it."""
        step = 0.05
        for _ in range(int(round(seconds / step))):
            self.board.advance(step)
        self.pump()

    def pump(self):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.01)
            if not self.board.out and self.q.empty():
                time.sleep(0.02)
                if self.q.empty():
                    break
            self._drain()
        self._drain()

    def _drain(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item.kind == "sent":
                self.sent.append(item.text)
                continue
            if item.kind != "line":
                continue
            self.model.apply(item.record, item.host_s)
            if isinstance(item.record, Event):
                self.events.append(item.record)
            if item.text.startswith("E,"):
                self.errors.append(item.text)

    def send(self, cmd):
        assert self.link.send(cmd)
        self.board.advance(0.01)
        self.pump()

    def dumps(self):
        return [e.detail for e in self.events if e.name == "dump"]

    def states(self):
        return [e.detail for e in self.events if e.name == "state"]

    def close(self):
        self.link.stop()
        self.link.join(1.0)


@pytest.fixture
def s():
    sess = Session()
    sess.pump()
    sess.send("STREAM 1")
    sess.run(0.5)
    yield sess
    sess.close()


def inflate_and_run(s, to=160.0):
    s.send("START")
    s.board.pump_to(to)
    s.run(to / 40.0 + 0.5)
    s.send("RUN")


def test_connect_reads_info_and_lights_go_green(s):
    assert s.model.info.firmware == "mapquest-irb-fw-0.2.1"
    assert s.model.streaming
    lights = s.model.lights(time.monotonic())
    assert all(level == OK for level, _ in lights.values()), lights
    ok, why = s.model.can_start(time.monotonic())
    assert ok, why


def test_full_run_ends_at_stop_pressure(s):
    inflate_and_run(s, 160)
    s.run(10.5)                                   # the 10 s hold
    assert s.model.state == "DEFLATE"
    s.run(62.0)                                   # 160 -> 40 at 2 mmHg/s
    s.run(3.0)                                    # vent, back to IDLE
    assert s.states() == ["INFLATE", "HOLD", "DEFLATE", "DUMP", "IDLE"]
    assert s.dumps() == ["reached stop pressure"]
    assert s.model.fault is None and s.model.run_summary.startswith("p_start=")
    assert s.sent == ["STREAM 1", "START", "RUN"]


def test_run_refused_below_run_minimum(s):
    s.send("START")
    s.board.pump_to(100)
    s.run(3.0)
    s.send("RUN")
    assert s.model.state == "INFLATE"
    assert any("RUN refused" in e for e in s.errors)
    ok, why = s.model.can_run(140)
    assert not ok and "below" in why


def test_start_refused_when_cuff_not_vented(s):
    s.board.p = 30.0
    s.board.valve_closed = False
    s.send("START")                              # the mock vents in ~1 s; still 30 now
    assert s.model.state == "IDLE"
    assert any(e.name == "start_refused" for e in s.events)


def test_manual_dump(s):
    inflate_and_run(s, 160)
    s.run(2.0)
    s.send("DUMP")
    s.run(3.0)
    assert s.dumps() == ["user"] and s.model.state == "IDLE"
    assert s.model.fault is None                 # a user dump is not a fault


@pytest.mark.parametrize("fault,reason", [
    ("pressure", "pressure dropout"),
    ("ppg_D", "ppg dropout D"),
    ("ppg_P", "ppg dropout P"),
])
def test_sensor_dropout_dumps(s, fault, reason):
    inflate_and_run(s, 160)
    s.run(1.0)
    s.board.fail(fault)
    s.run(1.0)
    assert s.dumps() == [reason]
    assert s.model.fault == reason
    s.model.acknowledge_fault()
    assert s.model.fault is None


def test_imu_dropout_only_warns(s):
    inflate_and_run(s, 160)
    s.board.fail("imu_D")
    s.run(1.5)
    assert s.dumps() == []
    assert any(e.name == "imu_dropout" and e.detail == "D" for e in s.events)
    level, _ = s.model.lights(time.monotonic())["imu_D"]
    assert level != OK
    s.board.heal("imu_D")
    s.run(0.5)
    assert any(e.name == "imu_resumed" for e in s.events)


def test_overpressure_dumps(s):
    s.send("SET p_max 200")
    s.send("START")
    s.board.pump_to(230)
    s.run(7.0)
    assert s.dumps() == ["overpressure"]


def test_hold_timeout_dumps(s):
    s.send("SET hold_timeout_s 20")
    s.send("START")
    s.board.pump_to(150)
    s.run(22.0)                                  # never pressed RUN
    assert s.dumps() == ["hold timeout"]


def test_limits_can_only_be_tightened(s):
    s.send("SET p_max 300")
    s.send("SET hold_timeout_s 200")
    assert sum("out of range" in e for e in s.errors) == 2
    s.send("SET p_max 220")
    s.send("INFO")
    assert s.model.info.setting("p_max") == 220.0


def test_link_down_is_reported():
    sess = Session()
    sess.pump()
    sess.board.close()
    sess.pump()
    time.sleep(0.05)
    item_kinds = []
    while not sess.q.empty():
        item_kinds.append(sess.q.get_nowait().kind)
    assert not sess.link.alive
    assert not sess.link.send("DUMP")
    sess.close()


def test_stale_stream_turns_light_red(s):
    s.board.fail("ppg_P")
    s.run(0.2)
    later = time.monotonic() + 2.0
    assert s.model.lights(later)["ppg_P"][0] == BAD
    ok, why = s.model.can_start(later)
    assert not ok and "ppg_P" in why
