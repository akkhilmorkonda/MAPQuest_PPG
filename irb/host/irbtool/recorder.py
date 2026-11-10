"""Run folders and the session log (irb/DESIGN.md section 6).

    irb_data/<subject>/
      session_<stamp>.log           every command, event and operator action
      <run_id>_<stamp>/
        distal.csv   proximal.csv   imu.csv   control.csv   events.csv
        raw_serial.txt              every line as received: the source of truth
        run.json                    trial info + device settings + outcome

Every CSV starts with the same header: recording info, the firmware's INFO
block and a ``# trial_info=pending`` line. Saving the run replaces that line
with the full trial info. ``distal.csv`` keeps the column names and header keys
of ``data/hr_*.csv``, so ``host/cuff_trial.py`` reads it as it is.

Files are line-buffered: a crash keeps everything up to the last line.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import trialinfo
from .protocol import (Control, DeviceInfo, Distal, Error, Event, Imu, Meta, Proximal,
                       TimeUnwrapper)

REPO = Path(__file__).resolve().parents[3]
DATA_ROOT = REPO / "irb_data"

PENDING_LINE = "# trial_info=pending"
# A saved trial block sits between these two lines, so a second save (a
# corrected reference) replaces it. The first has no '=': header parsers skip it.
TRIAL_BEGIN = "# ---- trial info ----"
TRIAL_END = "# trial_info=complete"
_FOLDER_RE = re.compile(r"^(?P<run>.+)_(?P<stamp>\d{8}_\d{6})$")

DISTAL_COLUMNS = ("idx", "t_us", "ir", "red", "p_idx", "p_raw", "p_status")
PROXIMAL_COLUMNS = ("idx", "t_us", "ir", "red")
IMU_COLUMNS = ("m_idx", "t_us", "band", "ax", "ay", "az", "gx", "gy", "gz")
CONTROL_COLUMNS = ("c_idx", "t_us", "state", "p_mmhg", "pref_mmhg", "rate_mmhg_s", "duty_pct")
EVENT_COLUMNS = ("t_us", "name", "detail", "source", "host_s")


def taken_run_ids(subject_dir: Path) -> set[str]:
    if not subject_dir.is_dir():
        return set()
    out = set()
    for p in subject_dir.iterdir():
        m = _FOLDER_RE.match(p.name)
        if p.is_dir() and m:
            out.add(m.group("run"))
    return out


def _fmt(v: float) -> str:
    return "nan" if v != v else f"{v:.3f}"


def _csv_safe(text: str) -> str:
    return text.replace(",", ";").replace("\n", " ")


class SessionLog:
    """One text log per session: host time, kind, text."""

    def __init__(self, root: Path, subject: str, stamp: str | None = None) -> None:
        d = Path(root) / subject
        d.mkdir(parents=True, exist_ok=True)
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = d / f"session_{stamp}.log"
        self._fh = self.path.open("a", buffering=1, encoding="utf-8")

    def write(self, kind: str, text: str, when: datetime | None = None) -> None:
        """``when``: the time it happened, for lines buffered before the log opened."""
        if self._fh.closed:
            return
        stamp = (when or datetime.now()).isoformat(timespec="milliseconds")
        self._fh.write(f"{stamp}\t{kind}\t{text}\n")

    def close(self) -> None:
        self._fh.close()


class RunRecorder:
    """Writes one run. Open at Start, ``stop()`` when recording ends, then
    ``save()`` with the after-run reference, or ``discard()``."""

    def __init__(self, root: Path, trial: dict, device: DeviceInfo, *,
                 session_t0: float = 0.0, started: datetime | None = None,
                 t_us_now: int = 0, unwrap_from: TimeUnwrapper | None = None) -> None:
        self.started = started or datetime.now()
        self.ended: datetime | None = None
        self.subject = trial["subject_id"]
        self.requested_run_id = trial["run_id"]
        subject_dir = Path(root) / self.subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = trialinfo.unique_run_id(self.requested_run_id, taken_run_ids(subject_dir))
        self.dir = subject_dir / f"{self.run_id}_{self.started:%Y%m%d_%H%M%S}"
        self.dir.mkdir()
        self.trial = dict(trial, run_id=self.run_id)
        self.device = device
        self.session_t0 = session_t0
        self.status = "recording"
        self.dumps: list[str] = []
        self.run_summary: str | None = None
        self.counts = {k: 0 for k in ("distal", "proximal", "imu", "control", "events", "raw")}
        # Continue the caller's unwrapping, so a run that starts after the
        # 71.6 min micros() wrap uses the same time base as the live model.
        self._unwrap = TimeUnwrapper()
        if unwrap_from is not None:
            self._unwrap.offset, self._unwrap.last = unwrap_from.offset, unwrap_from.last
        # Board time of the last line seen; stamps operator marks. Seeded with
        # the caller's latest (unwrapped) board time so a mark made before
        # the first line arrives is not stamped 0.
        self._last_t_us = t_us_now

        common = [
            "# source=irb_session",
            f"# recorded_at={self.started.isoformat(timespec='seconds')}",
            f"# run_id={self.run_id}",
            f"# subject_id={self.subject}",
            f"# session_t0={session_t0:.3f}",
            "# t_us=unwrapped (micros since boot, 32-bit wrap removed)",
            PENDING_LINE,
        ] + device.header_lines()

        self._files = {}
        self._open("distal", "distal.csv", common + self._band_lines("D") +
                   [f"# columns={','.join(DISTAL_COLUMNS)}"], DISTAL_COLUMNS)
        self._open("proximal", "proximal.csv", common + self._band_lines("P") +
                   [f"# columns={','.join(PROXIMAL_COLUMNS)}"], PROXIMAL_COLUMNS)
        self._open("imu", "imu.csv", common + [f"# columns={','.join(IMU_COLUMNS)}"], IMU_COLUMNS)
        self._open("control", "control.csv", common + [f"# columns={','.join(CONTROL_COLUMNS)}"],
                   CONTROL_COLUMNS)
        self._open("events", "events.csv", common + [f"# columns={','.join(EVENT_COLUMNS)}"],
                   EVENT_COLUMNS)
        self._raw = (self.dir / "raw_serial.txt").open("w", buffering=1, encoding="utf-8")
        self._raw.write("# host_s<TAB>line, every line exactly as received\n")
        self._write_json()

    # -- setup -------------------------------------------------------------

    def _band_lines(self, tag: str) -> list[str]:
        """This band's fields under the plain names the existing tools read
        (dark_ir, dark_red, ir_amp, ...): distal.csv looks like a one-sensor recording."""
        band = self.device.bands.get(tag, {})
        lines = [f"# band={tag}"]
        lines += [f"# {k}={v}" for k, v in band.items()]
        return lines

    def _open(self, key: str, name: str, header: list[str], columns) -> None:
        fh = (self.dir / name).open("w", buffering=1, encoding="utf-8", newline="")
        for line in header:
            fh.write(line + "\n")
        fh.write(",".join(columns) + "\n")
        self._files[key] = fh

    # -- writing -----------------------------------------------------------

    def write(self, record, raw_line: str, host_s: float) -> None:
        """One line from the board. Every line goes to raw_serial.txt; data
        and events also go to their CSV."""
        if self.status != "recording":
            return
        self._raw.write(f"{host_s:.3f}\t{raw_line}\n")
        self.counts["raw"] += 1
        f = self._files
        if isinstance(record, Distal):
            t = self._t(record.t_us)
            f["distal"].write(f"{record.idx},{t},{record.ir},{record.red},{record.p_idx},"
                              f"{record.p_raw},{record.p_status}\n")
            self.counts["distal"] += 1
        elif isinstance(record, Proximal):
            t = self._t(record.t_us)
            f["proximal"].write(f"{record.idx},{t},{record.ir},{record.red}\n")
            self.counts["proximal"] += 1
        elif isinstance(record, Imu):
            t = self._t(record.t_us)
            f["imu"].write(f"{record.m_idx},{t},{record.band},{record.ax},{record.ay},{record.az},"
                           f"{record.gx},{record.gy},{record.gz}\n")
            self.counts["imu"] += 1
        elif isinstance(record, Control):
            t = self._t(record.t_us)
            f["control"].write(f"{record.c_idx},{t},{record.state},{_fmt(record.p_mmhg)},"
                               f"{_fmt(record.pref_mmhg)},{_fmt(record.rate_mmhg_s)},"
                               f"{_fmt(record.duty_pct)}\n")
            self.counts["control"] += 1
        elif isinstance(record, Event):
            t = self._t(record.t_us)
            self._event(t, record.name, record.detail, "board", host_s)
            if record.name == "dump":
                self.dumps.append(record.detail)
            elif record.name == "run_summary":
                self.run_summary = record.detail
        elif isinstance(record, Error):
            self._event(self._last_t_us, "error", record.message, "board", host_s)
        elif isinstance(record, Meta):
            pass   # raw only

    def mark(self, name: str, detail: str, host_s: float) -> None:
        """An operator mark (O: Doppler silent, D: Doppler flow back). Stamped
        with the last board time seen; host_s gives the exact host time."""
        if self.status == "recording":
            self._event(self._last_t_us, name, detail, "operator", host_s)

    def _event(self, t_us: int, name: str, detail: str, source: str, host_s: float) -> None:
        self._files["events"].write(f"{t_us},{_csv_safe(name)},{_csv_safe(detail)},{source},{host_s:.3f}\n")
        self.counts["events"] += 1

    def _t(self, t_us: int) -> int:
        t = self._unwrap(t_us)
        self._last_t_us = max(self._last_t_us, t)
        return t

    # -- ending ------------------------------------------------------------

    def stop(self) -> None:
        """Close the data files. The run waits for save() or discard()."""
        if self.status != "recording":
            return
        for fh in self._files.values():
            fh.close()
        self._raw.close()
        self.ended = datetime.now()
        self.status = "unsaved"
        self._write_json()

    def save(self, after: dict) -> None:
        """Merge the after-run fields and write the trial info into every header."""
        if self.status == "recording":
            self.stop()
        if self.status == "discarded":
            raise RuntimeError("run was discarded")
        self.trial.update(after)
        block = "\n".join([TRIAL_BEGIN] + trialinfo.header_lines(self.trial) + [TRIAL_END]) + "\n"
        for name in ("distal.csv", "proximal.csv", "imu.csv", "control.csv", "events.csv"):
            path = self.dir / name
            text = path.read_text(encoding="utf-8")
            head, sep, tail = text.partition(PENDING_LINE + "\n")
            if not sep:                                  # saved before: replace the block
                head, sep, rest = text.partition(TRIAL_BEGIN + "\n")
                _, sep2, tail = rest.partition(TRIAL_END + "\n")
                if not (sep and sep2):
                    raise RuntimeError(f"{name}: no trial block to replace")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(head + block + tail, encoding="utf-8")
            tmp.replace(path)
        self.status = "saved"
        self._write_json()

    def discard(self) -> None:
        if self.status == "recording":
            self.stop()
        shutil.rmtree(self.dir)
        self.status = "discarded"

    def _write_json(self) -> None:
        if self.status == "discarded":
            return
        doc = {
            "run_id": self.run_id,
            "requested_run_id": self.requested_run_id,
            "run_id_suffixed": self.run_id != self.requested_run_id,
            "subject_id": self.subject,
            "folder": self.dir.name,
            "status": self.status,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.ended.isoformat(timespec="seconds") if self.ended else None,
            "session_t0": self.session_t0,
            "trial": self.trial,
            "dumps": self.dumps,
            "run_summary": self.run_summary,
            "line_counts": self.counts,
            "device": {
                "general": self.device.general,
                "bands": self.device.bands,
                "settings": self.device.settings,
                "limits": self.device.limits,
            },
        }
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "run.json")

