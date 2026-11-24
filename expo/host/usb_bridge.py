"""The board's USB serial link, for the bench GUI (host/bench_gui.py).

``SerialLink`` reads the board's serial stream on a background thread, sends
command lines, and reopens the port if the board goes away. Opening the port
resets the XIAO, so it waits a moment and then sends ``STREAM 1``.

expo 2026-11-23: this file used to also serve the phone page over USB
(BridgeServer, expotool/usbhub.py). The phone is retired: the iPad loads its
page from the board over Wi-Fi. The name stays because bench_gui.py imports
SerialLink from here.
"""

from __future__ import annotations

import threading
import time

BAUD = 921600
REOPEN_S = 1.0


class SerialLink:
    """The serial port on a reader thread; reconnects if the board goes away."""

    def __init__(self, port: str | None, baud: int = BAUD, wifi_off: bool = False) -> None:
        self.port, self.baud, self.wifi_off = port, baud, wifi_off
        self.ser = None
        self.lines: list[str] = []
        self.events: list[str] = []        # "up" / "down"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def take(self) -> tuple[list[str], list[str]]:
        with self.lock:
            lines, events, self.lines, self.events = self.lines, self.events, [], []
        return lines, events

    def write(self, lines: list[str]) -> None:
        ser = self.ser
        if ser is None or not lines:
            return
        try:
            ser.write("".join(ln + "\n" for ln in lines).encode())
        except Exception:                   # the reader notices and reconnects
            pass

    def _pick(self) -> str | None:
        if self.port:
            return self.port
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        return ports[0] if len(ports) == 1 else None

    def _run(self) -> None:
        import serial
        buf = b""
        while not self.stop.is_set():
            if self.ser is None:
                port = self._pick()
                try:
                    if port is None:
                        raise OSError("no single serial port found (pass --port)")
                    self.ser = serial.Serial(port, self.baud, timeout=0.05)
                    time.sleep(2.5)                         # opening the port resets the XIAO
                    # (the boot lines are kept: they say why a sensor was missed)
                    self.write(["STREAM 1"] + (["RADIO 0"] if self.wifi_off else []))
                    with self.lock:
                        self.events.append(f"up {port}")
                except Exception as e:
                    self.ser = None
                    with self.lock:
                        self.events.append(f"fail {e}")
                    self.stop.wait(REOPEN_S)
                    continue
            try:
                data = self.ser.read(65536)
            except Exception:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                with self.lock:
                    self.events.append("down")
                continue
            if not data:
                continue
            buf += data
            *done, buf = buf.split(b"\n")
            if done:
                with self.lock:
                    self.lines.extend(d.decode("ascii", "replace") for d in done)

    def close(self) -> None:
        self.stop.set()
        self._thread.join(1)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
