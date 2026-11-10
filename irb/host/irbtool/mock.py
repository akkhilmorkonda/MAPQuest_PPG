"""A simulated IRB board speaking the firmware's protocol (irb_fw 0.2.1).

Used by the tests and, later, by the GUI's ``--mock`` mode. It follows the
firmware's command rules, state machine and dump reasons; it does not
simulate the PI loop (deflation simply follows the target ramp).

Time is virtual. Tests call ``advance(seconds)``; with ``realtime=True``
every read advances by the wall-clock time since the last one.

Operator and fault hooks:
    pump_to(mmHg)            the operator squeezes the bulb toward this pressure
    fail(sensor)             "pressure", "ppg_D", "ppg_P", "imu_D", "imu_P"
    heal(sensor)
"""

from __future__ import annotations

import math
import random
import time

FIRMWARE = "mapquest-irb-fw-0.2.1"
OUTPUT_MIN, OUTPUT_MAX = 1677722, 15099494
MMHG_PER_PSI = 51.71493
ROOM_ABS_MMHG = 760.0
FULL_SCALE = 262143

P_MAX_HARD = 280.0
HOLD_TIMEOUT_HARD = 120.0
VENTED_BELOW = 10.0
PRESSURE_DROPOUT_S = 0.25
PPG_DROPOUT_S = 0.5
IMU_DROPOUT_S = 0.5
DUMP_SETTLE_MAX_S = 15.0

SET_RULES = {   # key -> (lo, hi) as callables of the current settings, as the firmware
    "rate": lambda s: (0.5, 5.0),
    "kp": lambda s: (0.0, 1.0),
    "ki": lambda s: (0.0, 1.0),
    "kd": lambda s: (0.0, 1.0),
    "u0": lambda s: (0.0, 1.0),
    "hold_s": lambda s: (0.0, 60.0),
    "p_stop": lambda s: (20.0, 100.0),
    "p_run_min": lambda s: (s["p_stop"] + 10.0, s["p_max"] - 10.0),
    "p_max": lambda s: (s["p_run_min"] + 10.0, P_MAX_HARD),
    "hold_timeout_s": lambda s: (s["hold_s"] + 10.0, HOLD_TIMEOUT_HARD),
}


def _abs_to_counts(abs_mmhg: float) -> int:
    psi = abs_mmhg / MMHG_PER_PSI
    return int(OUTPUT_MIN + psi / 25.0 * (OUTPUT_MAX - OUTPUT_MIN))


class MockBoard:
    def __init__(self, *, realtime: bool = False, seed: int = 1, t0_us: int = 1_000_000,
                 sbp: float = 120.0, map_: float = 90.0) -> None:
        self.realtime = realtime
        self.rng = random.Random(seed)
        self.t = 0.0                       # virtual seconds since boot
        self.t0_us = t0_us                 # micros() at t = 0 (set near 2^32 to test the wrap)
        self.sbp, self.map = sbp, map_
        self.out = bytearray()
        self._inbuf = b""
        self._wall = time.monotonic()
        self.closed = False

        self.settings = {"rate": 2.0, "kp": 0.08, "ki": 0.04, "kd": 0.0, "u0": 0.04,
                         "hold_s": 10.0, "p_stop": 40.0, "p_run_min": 140.0,
                         "p_max": P_MAX_HARD, "hold_timeout_s": HOLD_TIMEOUT_HARD}
        self.state = "IDLE"
        self.streaming = False
        self.valve_closed = False
        self.p = 0.0                       # cuff, mmHg relative
        self.pump_target: float | None = None
        self.pump_rate = 40.0
        self.zeroed = True                 # the firmware zeroes at boot
        self.failed: set[str] = set()
        self.amps = {"D": [31, 31], "P": [31, 31]}
        self.dark = {}

        self.idx = {"D": 0, "P": 0}
        self.m_idx = {"D": 0, "P": 0}
        self.c_idx = 0
        self.p_idx = 0
        self.last = {"pressure": 0.0, "ppg_D": 0.0, "ppg_P": 0.0, "imu_D": 0.0, "imu_P": 0.0}
        self.imu_warned: set[str] = set()
        self.volume = 0.0                  # distal relative blood volume, um

        self.close_t = self.hold_t = self.run_t = self.dump_t = 0.0
        self.p_run_start = 0.0
        self._next = {"ppg": 0.0, "imu": 0.0, "pressure": 0.0, "win": 0.0}
        self._boot()

    # -- transport interface (irbtool.link.Transport) ------------------------

    def read_available(self) -> bytes:
        if self.closed:
            raise OSError("mock port closed")
        if self.realtime:
            now = time.monotonic()
            self.advance(min(now - self._wall, 0.5))
            self._wall = now
        data = bytes(self.out)
        self.out.clear()
        return data

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("mock port closed")
        self._inbuf += data
        while b"\n" in self._inbuf:
            line, self._inbuf = self._inbuf.split(b"\n", 1)
            self.command(line.decode().strip())

    def close(self) -> None:
        self.closed = True

    # -- operator and fault hooks ------------------------------------------

    def pump_to(self, mmhg: float, rate: float = 40.0) -> None:
        self.pump_target, self.pump_rate = mmhg, rate

    def fail(self, sensor: str) -> None:
        self.failed.add(sensor)

    def heal(self, sensor: str) -> None:
        self.failed.discard(sensor)

    # -- output ------------------------------------------------------------

    def _us(self) -> int:
        return (self.t0_us + int(round(self.t * 1e6))) % (1 << 32)

    def _emit(self, line: str) -> None:
        self.out += (line + "\n").encode()

    def _event(self, name: str, detail: str = "") -> None:
        self._emit(f"EV,{self._us()},{name},{detail}")

    def _boot(self) -> None:
        self._emit(f"# {FIRMWARE} booting, reset_reason=power_on")
        self._emit("# cuff_zero_abs_mmhg=760.000 zero_n=50")
        self._info()
        self._emit("# ready: all 5 sensors found")

    def _info(self) -> None:
        e, s = self._emit, self.settings
        e(f"# firmware={FIRMWARE}")
        e("# board=esp32-wroom-32d")
        e("# reset_reason=power_on")
        e("# watchdog_ok=1 watchdog_ms=1000")
        e("# i2c_hz=400000 sda=21 scl=22")
        e("# mux_ok=1 mux_addr=0x70")
        for tag, name, ch in (("D", "distal", 0), ("P", "proximal", 1)):
            ppg = int(f"ppg_{tag}" not in self.failed)
            imu = int(f"imu_{tag}" not in self.failed)
            ir, red = self.amps[tag]
            e(f"# band={tag} name={name} mux_ch={ch} ppg_ok={ppg} part_id=0x15 rev=0x03 "
              f"ir_amp={ir} red_amp={red} ir_amp_ma={ir * 0.2:.1f} red_amp_ma={red * 0.2:.1f} "
              f"imu_ok={imu} imu_chip_id=0x24 imu_configured={imu}")
            if tag in self.dark:
                e(f"# band={tag} dark_ir={self.dark[tag][0]} dark_red={self.dark[tag][1]}")
        e("# mode=spo2 channels=ir,red fs=100.00 internal_sps=400 smp_ave=4 "
          "pulse_width_us=411 resolution_bits=18 adc_range_na=4096")
        e("# imu=bmi270 imu_odr_hz=50 imu_acc_range_g=4 imu_gyr_range_dps=500 "
          "imu_acc_lsb_per_g=8192.0 imu_gyr_lsb_per_dps=65.536")
        pok = int("pressure" not in self.failed)
        e(f"# pressure=mprls pressure_ok={pok} pressure_addr=0x18 pressure_range_psi=0-25 "
          f"pressure_transfer=A pressure_output_min={OUTPUT_MIN} pressure_output_max={OUTPUT_MAX} "
          f"pressure_rate_hz=100 pressure_status=0x40 pressure_integrity=ok")
        e(f"# cuff_zero_abs_mmhg={ROOM_ABS_MMHG:.3f}")
        e("# set " + " ".join(f"{k}={v:.4f}" for k, v in s.items()))
        e(f"# limits p_max_hard={P_MAX_HARD:.0f} hold_timeout_hard_s={HOLD_TIMEOUT_HARD:.0f} "
          f"vented_below={VENTED_BELOW:.0f} pressure_dropout_ms=250 ppg_dropout_ms=500 "
          f"imu_dropout_ms=500 valve_window_ms=100 valve_min_pulse_ms=5")
        e("# columns_S=idx,t_us,ir,red,p_idx,p_raw,p_status")
        e("# columns_SP=idx,t_us,ir,red")
        e("# columns_M=m_idx,t_us,band,ax,ay,az,gx,gy,gz")
        e("# columns_C=c_idx,t_us,state,p_mmhg,pref_mmhg,rate_mmhg_s,duty_pct")
        e("# columns_EV=t_us,name,detail")
        e(f"# valve_pin=25 valve_type=normally_open valve={'CLOSED' if self.valve_closed else 'open'}")
        ok = 5 - len(self.failed & {"pressure", "ppg_D", "ppg_P", "imu_D", "imu_P"})
        e(f"# state={self.state} streaming={int(self.streaming)} sensors_ok={ok}/5")

    # -- commands ------------------------------------------------------------

    def _idle(self, cmd: str, not_streaming: bool) -> bool:
        if self.state != "IDLE":
            self._emit(f"E,{cmd} refused: state is {self.state}")
            return False
        if not_streaming and self.streaming:
            self._emit(f"E,{cmd} refused while streaming: send STREAM 0 first")
            return False
        return True

    def command(self, line: str) -> None:
        verb, _, arg = line.partition(" ")
        arg = arg.strip()
        if verb == "DUMP":
            self._dump("user")
        elif verb == "START":
            self._start()
        elif verb == "RUN":
            self._run()
        elif verb == "INFO":
            self._info()
        elif verb == "STREAM":
            if arg.startswith("1"):
                self.idx = {"D": 0, "P": 0}
                self.m_idx = {"D": 0, "P": 0}
                self.c_idx = 0
                for k in self.last:
                    self.last[k] = self.t
                self._info()
                self._emit("# streaming=1")
                self.streaming = True
            else:
                self.streaming = False
                self._emit("# streaming=0")
        elif verb == "SET":
            self._set(arg)
        elif verb == "AMP":
            if self._idle("AMP", False):
                parts = arg.split()
                if len(parts) == 3 and parts[0] in self.amps and parts[1].upper() in ("IR", "RED"):
                    i = 0 if parts[1].upper() == "IR" else 1
                    self.amps[parts[0]][i] = int(parts[2])
                    self._emit(f"# band={parts[0]} {parts[1].lower()}_amp={parts[2]}")
                    self._event("amp", f"{parts[0]} {parts[1].lower()}={parts[2]}")
                else:
                    self._emit("E,AMP needs <D|P> <IR|RED> <0-255>")
        elif verb == "ZERO":
            if self._idle("ZERO", True):
                self.zeroed = True
                self._emit(f"# cuff_zero_abs_mmhg={ROOM_ABS_MMHG + self.p:.3f} zero_n=50")
        elif verb == "DARK":
            if arg[:1] not in ("D", "P"):
                self._emit("E,DARK needs a band: D or P")
            elif self._idle("DARK", True):
                self.dark[arg[0]] = (9, 13)
                self._emit(f"# band={arg[0]} dark_ir=9 dark_red=13 dark_n=200")
        elif verb == "R":
            if self._idle("R", True):
                self._info()
        elif verb == "P":
            self._emit(f"# p_mmhg={self.p:.2f} p_raw={_abs_to_counts(ROOM_ABS_MMHG + self.p)} "
                       f"p_status=0x40 zeroed=1")
        elif verb:
            self._emit(f"E,unknown command: {line}")

    def _start(self) -> None:
        if not self._idle("START", False):
            return
        why = None
        if "pressure" in self.failed:
            why = "no pressure sensor"
        elif self.p >= VENTED_BELOW:
            why = "cuff not vented"
        elif "ppg_D" in self.failed or "ppg_P" in self.failed:
            why = "a PPG band is not OK"
        if why:
            self._emit(f"E,START refused: {why}")
            self._event("start_refused", why)
            return
        self.close_t = self.t
        self.valve_closed = True
        self._state("INFLATE")

    def _run(self) -> None:
        if self.state != "INFLATE":
            self._emit(f"E,RUN refused: state is {self.state} (needs INFLATE)")
            return
        if self.p < self.settings["p_run_min"]:
            d = f"p={self.p:.1f} below p_run_min={self.settings['p_run_min']:.1f}"
            self._emit(f"E,RUN refused: {d}")
            self._event("run_refused", d)
            return
        self.hold_t = self.t
        self._state("HOLD")

    def _set(self, arg: str) -> None:
        parts = arg.split()
        # As the firmware: IDLE only, except p_run_min during INFLATE.
        run_gate = self.state == "INFLATE" and parts[:1] == ["p_run_min"]
        if not run_gate and not self._idle("SET", False):
            return
        if len(parts) != 2:
            self._emit("E,SET needs <key> <value>")
            return
        key, text = parts
        try:
            v = float(text)
        except ValueError:
            self._emit("E,SET value is not a number")
            return
        if key not in SET_RULES:
            self._emit(f"E,SET unknown key: {key}")
            return
        lo, hi = SET_RULES[key](self.settings)
        if not lo <= v <= hi:
            self._emit(f"E,SET {key}={v:.3f} out of range [{lo:.3f}, {hi:.3f}]")
            return
        self.settings[key] = v
        self._emit(f"# set {key}={v:.4f}")
        self._event("set", f"{key}={v:.4f}")

    def _state(self, s: str) -> None:
        self.state = s
        self._event("state", s)

    def _dump(self, reason: str) -> None:
        was_active = self.state in ("INFLATE", "HOLD", "DEFLATE")
        self.valve_closed = False
        self.pump_target = None
        self.dump_t = self.t
        if self.state != "IDLE":
            self.state = "DUMP"
        self._event("dump", reason)
        if was_active:
            self._event("state", "DUMP")

    # -- simulation ----------------------------------------------------------

    def advance(self, seconds: float) -> None:
        end = self.t + seconds
        dt = 0.001
        while self.t < end - 1e-9:
            self.t = round(self.t + dt, 6)
            self._physics(dt)
            if self.t >= self._next["pressure"] - 1e-9:
                self._next["pressure"] += 0.01
                if "pressure" not in self.failed:
                    self.p_idx += 1
                    self.last["pressure"] = self.t
            if self.t >= self._next["ppg"] - 1e-9:
                self._next["ppg"] += 0.01
                self._ppg()
            if self.t >= self._next["imu"] - 1e-9:
                self._next["imu"] += 0.02
                self._imu()
            if self.t >= self._next["win"] - 1e-9:
                self._next["win"] += 0.1
                self._window()
            self._safety()

    def _physics(self, dt: float) -> None:
        s = self.settings
        if not self.valve_closed:
            self.p *= math.exp(-dt / 0.5)          # vents with a 0.5 s time constant
            if self.p < 0.01:
                self.p = 0.0
        elif self.state == "DEFLATE":
            self.p = max(self.p - s["rate"] * dt, 0.0)
        elif self.pump_target is not None and self.p < self.pump_target:
            self.p = min(self.p + self.pump_rate * dt, self.pump_target)
        else:
            self.p = max(self.p - 0.05 * dt, 0.0)  # a slow leak while held
        # distal blood volume: drains slowly under occlusion, refills below systolic
        if self.p > self.sbp:
            self.volume -= 0.3 * dt
        elif self.p > 20:
            self.volume += 2.0 * dt * min(1.0, (self.sbp - self.p) / 10.0)
        else:
            self.volume *= math.exp(-dt / 5.0)

    def _ppg(self) -> None:
        for tag in ("D", "P"):
            if f"ppg_{tag}" in self.failed:
                continue
            self.last[f"ppg_{tag}"] = self.t
            pulse = 0.0015 * math.sin(2 * math.pi * 1.2 * self.t)
            vol = self.volume if tag == "D" else 0.0
            # Counts scale with LED current (amp 31 = 6.2 mA lands near mid-scale).
            ir_amp, red_amp = self.amps[tag]
            ir = 9 + 4200 * ir_amp * (1 - 0.0005 * vol + pulse) + self.rng.gauss(0, 20)
            red = 13 + 3500 * red_amp * (1 - 0.0008 * vol + pulse) + self.rng.gauss(0, 20)
            ir, red = (int(min(max(v, 0), FULL_SCALE)) for v in (ir, red))
            if self.streaming:
                if tag == "D":
                    raw = _abs_to_counts(ROOM_ABS_MMHG + self.p)
                    self._emit(f"S,{self.idx['D']},{self._us()},{ir},{red},{self.p_idx},{raw},64")
                else:
                    self._emit(f"SP,{self.idx['P']},{self._us()},{ir},{red}")
            self.idx[tag] += 1

    def _imu(self) -> None:
        for tag in ("D", "P"):
            if f"imu_{tag}" in self.failed:
                continue
            self.last[f"imu_{tag}"] = self.t
            if tag in self.imu_warned:
                self.imu_warned.discard(tag)
                self._event("imu_resumed", tag)
            if self.streaming:
                n = lambda: int(self.rng.gauss(0, 30))  # noqa: E731
                self._emit(f"M,{self.m_idx[tag]},{self._us()},{tag},{n()},{n()},{8192 + n()},"
                           f"{n()},{n()},{n()}")
            self.m_idx[tag] += 1

    def _window(self) -> None:
        s = self.settings
        pressure_ok = "pressure" not in self.failed
        p = self.p if pressure_ok else math.nan
        pref = math.nan
        if self.state in ("INFLATE", "HOLD"):
            if pressure_ok and p > 20.0 and self.t - self.close_t > s["hold_timeout_s"]:
                self._dump("hold timeout")
            elif self.state == "HOLD" and self.t - self.hold_t >= s["hold_s"] and pressure_ok:
                self.run_t, self.p_run_start = self.t, self.p
                self._state("DEFLATE")
        elif self.state == "DEFLATE":
            t = self.t - self.run_t
            pref = self.p_run_start - s["rate"] * t
            max_run = (self.p_run_start - s["p_stop"]) / s["rate"] * 2 + 15
            if pressure_ok and p <= s["p_stop"]:
                self._event("run_summary",
                            f"p_start={self.p_run_start:.1f} p_end={p:.1f} t_s={t:.1f} "
                            f"mean_rate={(self.p_run_start - p) / t if t else 0:.2f} "
                            f"target={s['rate']:.2f} rate_rms_err=0.05")
                self._dump("reached stop pressure")
            elif t > max_run:
                self._dump("run timeout")
        elif self.state == "DUMP":
            if (pressure_ok and p < VENTED_BELOW) or self.t - self.dump_t > DUMP_SETTLE_MAX_S:
                self.state = "IDLE"
                self._event("state", "IDLE")
        if self.streaming:
            duty = 100.0 if not self.valve_closed else (5.0 if self.state == "DEFLATE" else 0.0)
            rate = -s["rate"] if self.state == "DEFLATE" else 0.0
            self._emit(f"C,{self.c_idx},{self._us()},{self.state},{p:.2f},{pref:.2f},{rate:.3f},{duty:.1f}")
        self.c_idx += 1

    def _safety(self) -> None:
        if self.state in ("INFLATE", "HOLD", "DEFLATE"):
            if self.t - self.last["pressure"] > PRESSURE_DROPOUT_S:
                return self._dump("pressure dropout")
            if self.p > self.settings["p_max"]:
                return self._dump("overpressure")
            for tag in ("D", "P"):
                if self.t - self.last[f"ppg_{tag}"] > PPG_DROPOUT_S:
                    return self._dump(f"ppg dropout {tag}")
        for tag in ("D", "P"):
            if tag not in self.imu_warned and self.t - self.last[f"imu_{tag}"] > IMU_DROPOUT_S:
                self.imu_warned.add(tag)
                self._event("imu_dropout", tag)
