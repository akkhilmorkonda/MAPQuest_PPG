"""Firmware line protocol (irb/DESIGN.md 4.4, irb/README.md phase 2).

Every line from the board becomes one record. A malformed line becomes
``Unknown`` rather than raising: a garbled byte on the serial link must
never take down a session that is controlling a cuff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

STATES = ("IDLE", "INFLATE", "HOLD", "DEFLATE", "DUMP")
ACTIVE_STATES = ("INFLATE", "HOLD", "DEFLATE")
BANDS = ("D", "P")

# Column lists, as the firmware announces them in INFO (columns_<tag>=...).
COLUMNS = {
    "S": ("idx", "t_us", "ir", "red", "p_idx", "p_raw", "p_status"),
    "SP": ("idx", "t_us", "ir", "red"),
    "M": ("m_idx", "t_us", "band", "ax", "ay", "az", "gx", "gy", "gz"),
    "C": ("c_idx", "t_us", "state", "p_mmhg", "pref_mmhg", "rate_mmhg_s", "duty_pct"),
    "EV": ("t_us", "name", "detail"),
}


@dataclass(frozen=True)
class Distal:
    idx: int
    t_us: int
    ir: int
    red: int
    p_idx: int
    p_raw: int
    p_status: int


@dataclass(frozen=True)
class Proximal:
    idx: int
    t_us: int
    ir: int
    red: int


@dataclass(frozen=True)
class Imu:
    m_idx: int
    t_us: int
    band: str
    ax: int
    ay: int
    az: int
    gx: int
    gy: int
    gz: int


@dataclass(frozen=True)
class Control:
    c_idx: int
    t_us: int
    state: str
    p_mmhg: float      # NaN when the window had no good reading
    pref_mmhg: float   # NaN outside DEFLATE
    rate_mmhg_s: float
    duty_pct: float


@dataclass(frozen=True)
class Event:
    t_us: int
    name: str
    detail: str


@dataclass(frozen=True)
class Error:
    message: str


@dataclass(frozen=True)
class Meta:
    text: str                      # the line without the leading '#'
    pairs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Unknown:
    line: str


Record = Distal | Proximal | Imu | Control | Event | Error | Meta | Unknown


def _f(text: str) -> float:
    v = float(text)                # "nan" parses to NaN, which is what we want
    return v


def parse_meta(body: str) -> dict:
    """``key=value`` tokens of a metadata line. Tokens without '=' are skipped."""
    pairs = {}
    for token in body.split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key:
                pairs[key] = value
    return pairs


def parse_line(line: str) -> Record:
    line = line.strip()
    if not line:
        return Unknown(line)
    if line.startswith("#"):
        body = line[1:].strip()
        return Meta(body, parse_meta(body))
    tag, _, rest = line.partition(",")
    try:
        if tag == "S":
            f = rest.split(",")
            if len(f) != 7:
                return Unknown(line)
            return Distal(*(int(x) for x in f))
        if tag == "SP":
            f = rest.split(",")
            if len(f) != 4:
                return Unknown(line)
            return Proximal(*(int(x) for x in f))
        if tag == "M":
            f = rest.split(",")
            if len(f) != 9 or f[2] not in BANDS:
                return Unknown(line)
            return Imu(int(f[0]), int(f[1]), f[2], *(int(x) for x in f[3:]))
        if tag == "C":
            f = rest.split(",")
            if len(f) != 7 or f[2] not in STATES:
                return Unknown(line)
            return Control(int(f[0]), int(f[1]), f[2], _f(f[3]), _f(f[4]), _f(f[5]), _f(f[6]))
        if tag == "EV":
            t, _, rest2 = rest.partition(",")
            name, _, detail = rest2.partition(",")
            if not name:
                return Unknown(line)
            return Event(int(t), name, detail)
        if tag == "E":
            return Error(rest)
    except ValueError:
        return Unknown(line)
    return Unknown(line)


class TimeUnwrapper:
    """``t_us`` is a 32-bit ``micros()``: it wraps after 71.6 minutes.

    Lines from different streams arrive slightly out of order in t_us (a FIFO
    burst is stamped when it is read), so only a backwards jump of more than
    half the range counts as a wrap.
    """

    SPAN = 1 << 32

    def __init__(self) -> None:
        self.offset = 0
        self.last: int | None = None

    def __call__(self, t_us: int) -> int:
        if self.last is not None:
            if t_us < self.last - self.SPAN // 2:
                self.offset += self.SPAN
            elif t_us > self.last + self.SPAN // 2 and self.offset >= self.SPAN:
                # a late straggler from before the wrap
                return t_us + self.offset - self.SPAN
        self.last = t_us
        return t_us + self.offset


@dataclass
class DeviceInfo:
    """The firmware's INFO block, gathered from successive Meta records."""

    general: dict = field(default_factory=dict)
    bands: dict = field(default_factory=lambda: {b: {} for b in BANDS})
    settings: dict = field(default_factory=dict)   # from "# set ..."
    limits: dict = field(default_factory=dict)     # from "# limits ..."

    def apply(self, meta: Meta) -> None:
        pairs = meta.pairs
        if not pairs:
            return
        words = meta.text.split()
        head = words[0] if words else ""
        if head == "set":
            self.settings.update(pairs)
        elif head == "limits":
            self.limits.update(pairs)
        elif pairs.get("band") in BANDS:
            self.bands[pairs["band"]].update({k: v for k, v in pairs.items() if k != "band"})
        else:
            self.general.update(pairs)

    # Convenience views ---------------------------------------------------

    @property
    def firmware(self) -> str:
        return self.general.get("firmware", "unknown")

    @property
    def fs(self) -> float:
        try:
            return float(self.general.get("fs", "100"))
        except ValueError:
            return 100.0

    def setting(self, key: str, default: float = math.nan) -> float:
        try:
            return float(self.settings.get(key, default))
        except ValueError:
            return default

    def sensors_ok(self) -> dict:
        """Which of the five sensors the board reported as present."""
        b = self.bands
        return {
            "pressure": self.general.get("pressure_ok") == "1",
            "ppg_D": b["D"].get("ppg_ok") == "1",
            "ppg_P": b["P"].get("ppg_ok") == "1",
            "imu_D": b["D"].get("imu_ok") == "1",
            "imu_P": b["P"].get("imu_ok") == "1",
        }

    def header_lines(self) -> list[str]:
        """Every field as one ``# key=value`` line (one key per line, so the
        existing ``host/mapquest/io.Meta`` parser reads them unambiguously)."""
        out = [f"# {k}={v}" for k, v in self.general.items()]
        for tag in BANDS:
            out += [f"# band_{tag}_{k}={v}" for k, v in self.bands[tag].items()]
        out += [f"# set_{k}={v}" for k, v in self.settings.items()]
        out += [f"# limit_{k}={v}" for k, v in self.limits.items()]
        return out
