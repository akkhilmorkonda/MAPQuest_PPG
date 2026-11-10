"""The live picture of the board that the GUI draws from.

Fed every record, in order. Pure bookkeeping: no I/O, no timers of its own
(the caller passes the host time), so it is tested without a board.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .protocol import (ACTIVE_STATES, Control, DeviceInfo, Distal, Error, Event, Imu, Meta,
                       Proximal, TimeUnwrapper)

# A stream that has said nothing for this long while streaming is stale.
STALE_S = 1.0

# Dumps that are part of a normal run, not faults.
NORMAL_DUMPS = ("user", "reached stop pressure")

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"


@dataclass
class DeviceModel:
    info: DeviceInfo = field(default_factory=DeviceInfo)
    state: str = "IDLE"
    streaming: bool = False
    p_mmhg: float = math.nan
    pref_mmhg: float = math.nan
    rate_mmhg_s: float = math.nan
    duty_pct: float = math.nan
    last_dump: str | None = None
    fault: str | None = None          # a non-normal dump not yet acknowledged
    run_summary: str | None = None
    errors: list = field(default_factory=list)
    lost: set = field(default_factory=set)         # sensors the board reported lost
    imu_dropout: set = field(default_factory=set)  # bands warned for IMU dropout
    last_seen: dict = field(default_factory=dict)  # stream -> host_s
    t_us: int = 0                                  # latest board time, unwrapped
    stream_started_s: float | None = None          # host time of "# streaming=1"
    _unwrap: TimeUnwrapper = field(default_factory=TimeUnwrapper)

    def apply(self, rec, host_s: float) -> None:
        if isinstance(rec, Distal):
            self._seen("ppg_D", host_s, rec.t_us)
        elif isinstance(rec, Proximal):
            self._seen("ppg_P", host_s, rec.t_us)
        elif isinstance(rec, Imu):
            self._seen(f"imu_{rec.band}", host_s, rec.t_us)
        elif isinstance(rec, Control):
            self._seen("control", host_s, rec.t_us)
            self.state = rec.state
            self.p_mmhg, self.pref_mmhg = rec.p_mmhg, rec.pref_mmhg
            self.rate_mmhg_s, self.duty_pct = rec.rate_mmhg_s, rec.duty_pct
            if not math.isnan(rec.p_mmhg):
                self.last_seen["pressure"] = host_s
        elif isinstance(rec, Event):
            self.t_us = max(self.t_us, self._unwrap(rec.t_us))
            self._event(rec)
        elif isinstance(rec, Error):
            self.errors.append(rec.message)
            del self.errors[:-50]
        elif isinstance(rec, Meta):
            self.info.apply(rec)
            s = rec.pairs.get("streaming")
            if s is not None:
                self.streaming = s == "1"
                if self.streaming:
                    self.stream_started_s = host_s
                    self.last_seen.clear()
            st = rec.pairs.get("state")
            if st:
                self.state = st
            if "sensors_ok" in rec.pairs:            # a fresh INFO: forget old losses
                self.lost.clear()

    def _seen(self, stream: str, host_s: float, t_us: int) -> None:
        self.last_seen[stream] = host_s
        self.t_us = max(self.t_us, self._unwrap(t_us))

    def _event(self, ev: Event) -> None:
        name, detail = ev.name, ev.detail
        if name == "state":
            self.state = detail
        elif name == "dump":
            self.last_dump = detail
            if detail not in NORMAL_DUMPS:
                self.fault = detail
        elif name == "run_summary":
            self.run_summary = detail
        elif name in ("ppg_lost", "imu_lost"):
            self.lost.add(f"{name[:3]}_{detail.strip()[:1]}")
        elif name == "pressure_lost":
            self.lost.add("pressure")
        elif name == "imu_dropout":
            self.imu_dropout.add(detail.strip()[:1])
        elif name == "imu_resumed":
            self.imu_dropout.discard(detail.strip()[:1])

    # -- what the GUI shows -------------------------------------------------

    def acknowledge_fault(self) -> None:
        self.fault = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def lights(self, host_s: float) -> dict:
        """Sensor -> (level, text): one light per sensor (DESIGN 5.1)."""
        present = self.info.sensors_ok()
        out = {}
        for key in ("pressure", "ppg_D", "ppg_P", "imu_D", "imu_P"):
            if key in self.lost:
                out[key] = (BAD, "lost")
            elif not present.get(key, False):
                out[key] = (BAD, "not found")
            elif key.startswith("imu") and key[-1] in self.imu_dropout:
                out[key] = (WARN, "no data")
            elif self.streaming:
                seen = self.last_seen.get(key)
                started = self.stream_started_s or host_s
                if seen is None:
                    out[key] = (WARN, "waiting") if host_s - started < STALE_S else (BAD, "no data")
                elif host_s - seen > STALE_S:
                    out[key] = (BAD, f"silent {host_s - seen:.1f} s")
                else:
                    out[key] = (OK, "ok")
            else:
                out[key] = (OK, "found")
        return out

    def can_start(self, host_s: float) -> tuple[bool, str]:
        if self.state != "IDLE":
            return False, f"state is {self.state}"
        if not self.streaming:
            return False, "not streaming"
        bad = [k for k, (lvl, _) in self.lights(host_s).items()
               if lvl == BAD and not k.startswith("imu")]
        if bad:
            return False, "sensor problem: " + ", ".join(bad)
        if not (self.p_mmhg < 10.0):
            return False, "cuff not vented"
        return True, ""

    def can_run(self, run_min: float) -> tuple[bool, str]:
        if self.state != "INFLATE":
            return False, f"state is {self.state}"
        if not (self.p_mmhg >= run_min):
            return False, f"pressure below Run minimum {run_min:g}"
        return True, ""
