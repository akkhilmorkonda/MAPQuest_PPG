"""Serial link to the board, on a reader thread.

The reader turns bytes into lines and lines into records, and puts
``LinkItem``s on a queue for the GUI (or a test) to drain. Commands sent are
queued too, so the session log sees both directions in order.

Opening the port does NOT reset the board: the WROOM dev board wires DTR/RTS
to EN/IO0, and pyserial's default toggling would reboot it. A reboot is safe
(the valve is normally open) but would end a run mid-measurement, so both
lines are held released, as in valve_code/cuff_tuner.py.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from .protocol import Record, parse_line

BAUD = 921600


class Transport(Protocol):
    def read_available(self) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class SerialTransport:
    def __init__(self, port: str, baud: int = BAUD) -> None:
        import serial  # imported here so tests with the mock need no pyserial
        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.timeout = 0
        s.write_timeout = 1.0
        s.dtr = False
        s.rts = False
        s.open()
        self._s = s

    def read_available(self) -> bytes:
        n = self._s.in_waiting          # raises promptly when the port is gone
        return self._s.read(n) if n else b""

    def write(self, data: bytes) -> None:
        self._s.write(data)
        self._s.flush()

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass


def list_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    return [(p.device, p.description) for p in lp.comports()]


@dataclass(frozen=True)
class LinkItem:
    kind: str          # "line", "sent" or "down"
    host_s: float      # time.monotonic() when it happened
    text: str          # the raw line, the command, or why the link went down
    record: Record | None = None


class Link(threading.Thread):
    """Reads the transport until stopped or until it fails."""

    def __init__(self, transport: Transport, out: queue.Queue, clock=time.monotonic) -> None:
        super().__init__(daemon=True, name="irb-link")
        self.transport = transport
        self.out = out
        self.clock = clock
        self._stop = threading.Event()
        self._buf = b""
        self._write_lock = threading.Lock()
        self.alive = True

    def send(self, command: str) -> bool:
        """Send one command line. False if the link is down."""
        if not self.alive:
            return False
        try:
            with self._write_lock:
                self.transport.write((command.strip() + "\n").encode())
        except Exception as exc:          # noqa: BLE001 - any failure means the link is gone
            self._down(f"write failed: {exc}")
            return False
        self.out.put(LinkItem("sent", self.clock(), command.strip()))
        return True

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.poll_once():
                time.sleep(0.002)

    def poll_once(self) -> bool:
        """Read whatever is waiting and queue its lines. True if anything came.
        Called by the thread, or directly (without starting it) in tests."""
        if not self.alive:
            return False
        try:
            data = self.transport.read_available()
        except Exception as exc:      # noqa: BLE001
            self._down(f"read failed: {exc}")
            return False
        if not data:
            return False
        self._buf += data
        now = self.clock()
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode("ascii", errors="replace").rstrip("\r")
            if line:
                self.out.put(LinkItem("line", now, line, parse_line(line)))
        return True

    def stop(self) -> None:
        self._stop.set()
        self.alive = False
        self.transport.close()

    def _down(self, why: str) -> None:
        if self.alive:
            self.alive = False
            self.out.put(LinkItem("down", self.clock(), why))
        self._stop.set()
        self.transport.close()
