"""Line parsing, the INFO block, and t_us unwrapping."""

import math

from irbtool.mock import MockBoard
from irbtool.protocol import (Control, DeviceInfo, Distal, Error, Event, Imu, Meta, Proximal,
                              TimeUnwrapper, Unknown, parse_line)


def test_every_line_type():
    assert parse_line("S,5,1000,130000,110000,7,9424554,64") == Distal(5, 1000, 130000, 110000, 7, 9424554, 64)
    assert parse_line("SP,6,1010,120000,100000") == Proximal(6, 1010, 120000, 100000)
    assert parse_line("M,3,1020,P,1,-2,8192,4,5,-6") == Imu(3, 1020, "P", 1, -2, 8192, 4, 5, -6)
    c = parse_line("C,9,1030,DEFLATE,120.50,121.00,-2.010,12.5")
    assert c == Control(9, 1030, "DEFLATE", 120.5, 121.0, -2.01, 12.5)
    assert parse_line("EV,1040,dump,ppg dropout D") == Event(1040, "dump", "ppg dropout D")
    assert parse_line("EV,1050,state,") == Event(1050, "state", "")
    assert parse_line("E,START refused: cuff not vented") == Error("START refused: cuff not vented")
    m = parse_line("# band=D ppg_ok=1 dark_ir=9")
    assert isinstance(m, Meta) and m.pairs == {"band": "D", "ppg_ok": "1", "dark_ir": "9"}


def test_nan_fields_parse_as_nan():
    c = parse_line("C,1,2,IDLE,nan,nan,nan,100.0")
    assert isinstance(c, Control) and math.isnan(c.p_mmhg) and math.isnan(c.pref_mmhg)


def test_detail_may_contain_commas():
    ev = parse_line("EV,5,run_summary,p_start=160.0,p_end=39.8")
    assert ev == Event(5, "run_summary", "p_start=160.0,p_end=39.8")


def test_malformed_lines_never_raise():
    for bad in ["S,1,2,3", "S,a,b,c,d,e,f,g", "M,1,2,X,1,2,3,4,5,6", "C,1,2,FLYING,1,2,3,4",
                "EV,notanumber,state,IDLE", "EV,5,", "garbage", "SP,1,2,3,4,5", "\x00\x01",
                # ESP-IDF log lines seen on the real board (2026-11-10)
                "E (206) i2c.master: I2C hardware NACK detected",
                "E (54109) task_wdt: Task watchdog got triggered."]:
        assert isinstance(parse_line(bad), Unknown), bad


def test_info_block_is_sorted_into_bands_settings_and_limits():
    board = MockBoard()
    info = DeviceInfo()
    for line in board.read_available().decode().splitlines():
        rec = parse_line(line)
        if isinstance(rec, Meta):
            info.apply(rec)
    assert info.firmware == "mapquest-irb-fw-0.2.1"
    assert info.fs == 100.0
    assert info.bands["D"]["name"] == "distal" and info.bands["P"]["mux_ch"] == "1"
    assert info.setting("p_stop") == 40.0 and info.setting("hold_s") == 10.0
    assert info.limits["p_max_hard"] == "280"
    assert all(info.sensors_ok().values())
    # the band keys must not leak into the general block
    assert "ppg_ok" not in info.general and "name" not in info.general


def test_unwrapper_handles_wrap_and_jitter():
    u = TimeUnwrapper()
    span = 1 << 32
    assert u(span - 100) == span - 100
    assert u(span - 150) == span - 150              # small backwards jitter: no wrap
    assert u(50) == span + 50                       # wrapped
    assert u(span - 120) == span - 120              # a straggler from before the wrap
    assert u(200) == span + 200
