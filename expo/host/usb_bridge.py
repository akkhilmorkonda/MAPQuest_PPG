"""Run the expo board from this laptop over USB: phone page and dashboard, no Wi-Fi.

The board stays plugged into the laptop. This reads its USB serial stream and
serves the phone page and its WebSocket here, as the board does over Wi-Fi:

    python expo/host/usb_bridge.py                     # the only serial port, http://127.0.0.1:8770/
    python expo/host/usb_bridge.py --port COM8
    python expo/host/usb_bridge.py --wifi-off          # also turn the board's access point off

Then open http://127.0.0.1:8770/ (controls) or /?view (watch only). The
results process and dashboard connect the same way:

    python expo/host/expo_results.py --url ws://127.0.0.1:8770/ws

The Wi-Fi client rules (one controller, the controller-lost dump, ZERO and R
with the stream paused) are applied here: expotool/usbhub.py. The board's own
limits are unchanged. Opening the port resets the XIAO; give it a few
seconds. Closing the bridge sends DUMP if a run is active. With the access
point left on, a phone on the board's Wi-Fi can still take control there too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expotool.usbhub import ACTIVE, UsbHub  # noqa: E402
from expotool.ws import OP_CLOSE, OP_TEXT, FrameDecoder, ProtocolError, accept_key, encode_frame  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"
BAUD = 921600
DEFAULT_PORT = 8770
TICK_S = 0.04                         # the firmware batches every 40 ms (WS_FLUSH_MS)
MAX_BACKLOG = 16384                   # a client this far behind is closed, as on the board
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


class BridgeServer:
    def __init__(self, link: SerialLink, hub: UsbHub | None = None, page: Path = PAGE, say=print) -> None:
        self.link = link
        self.hub = hub or UsbHub(time.monotonic())
        self.page = page
        self.say = say
        self.writers: dict[int, asyncio.StreamWriter] = {}
        self._last_fail = ""

    async def start(self, host: str, port: int) -> int:
        server = await asyncio.start_server(self._conn, host, port)
        asyncio.get_running_loop().create_task(self._ticker())
        return server.sockets[0].getsockname()[1]

    def step(self, now: float) -> tuple[list[str], dict[int, list[str]]]:
        lines, events = self.link.take()
        for ev in events:
            if ev.startswith("up"):
                self.hub.link_up()
                self.say(f"board connected on {ev[3:]}")
            elif ev == "down":
                self.hub.link_lost()
                self.say("board link lost: reconnecting")
            elif ev != self._last_fail:
                self._last_fail = ev
                self.say(f"waiting for the board: {ev[5:]}")
        self.hub.board_lines(lines, now)
        out, direct = self.hub.tick(now)
        self.link.write(self.hub.to_board)
        self.hub.to_board.clear()
        return out, direct

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            out, direct = self.step(time.monotonic())
            body = "".join(ln + "\n" for ln in out)
            for cid, w in list(self.writers.items()):
                text = "".join(ln + "\n" for ln in direct.get(cid, [])) + body
                if not text:
                    continue
                if w.transport.get_write_buffer_size() > MAX_BACKLOG:
                    w.close()
                    continue
                w.write(encode_frame(OP_TEXT, text.encode(), mask=False))

    async def _conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        path = parts[1].split("?")[0] if len(parts) >= 2 else "/"
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            await self._websocket(reader, writer, headers)
            return
        if path in ("/", "/index.html"):
            body = self.page.read_bytes()             # read on every load: edits show on refresh
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        elif path == "/favicon.ico":
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    async def _websocket(self, reader, writer, headers) -> None:
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(headers.get('sec-websocket-key', ''))}\r\n\r\n").encode())
        await writer.drain()
        cid = self.hub.connect(time.monotonic())
        self.writers[cid] = writer
        dec = FrameDecoder()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for op, payload in dec.feed(data):
                    if op == OP_TEXT:
                        self.hub.receive(cid, payload.decode("utf-8", "replace"), time.monotonic())
                    elif op == OP_CLOSE:
                        raise ConnectionError
                    else:
                        self.hub.receive(cid, "PING", time.monotonic())
        except (ConnectionError, ProtocolError):
            pass
        finally:
            self.writers.pop(cid, None)
            self.hub.disconnect(cid)
            writer.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the expo board from this laptop over USB.")
    ap.add_argument("--port", help="serial port, e.g. COM8 (default: the only one connected)")
    ap.add_argument("--http-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--wifi-off", action="store_true",
                    help="turn the board's access point off (RADIO 0), so only this laptop can control it")
    args = ap.parse_args(argv)
    link = SerialLink(args.port, args.baud, args.wifi_off)
    bridge = BridgeServer(link, say=lambda s: print(s, flush=True))

    async def amain():
        port = await bridge.start("127.0.0.1", args.http_port)
        link.start()
        print(f"expo USB bridge: http://127.0.0.1:{port}/  (watch only: /?view, "
              f"results: --url ws://127.0.0.1:{port}/ws)", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    finally:
        if bridge.hub.state in ACTIVE:                # never leave a closed cuff behind
            link.write(["DUMP"])
            print("bridge closed during a run: DUMP sent", flush=True)
            time.sleep(0.2)
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
