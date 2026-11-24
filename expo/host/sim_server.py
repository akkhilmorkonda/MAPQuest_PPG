"""Local stand-in for the expo board: serves the iPad page and the WebSocket.

Runs the link simulator's wireless rules (expotool.hub) over the IRB mock
board, so the page and the receiver can be tried with no ESP32:

    python expo/host/sim_server.py                 # http://127.0.0.1:8765/
    python expo/host/sim_server.py --lan           # also reachable from an iPad on the same network

Then open http://127.0.0.1:8765/ (controls) or http://127.0.0.1:8765/?view
(watch only), or run expo_receiver.py --url ws://127.0.0.1:8765/ws.
The page is expo/web/dashboard.html marked for the board, as the firmware embeds it
(the iPad's screen and operator panel), read on every load, so edits show on refresh.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expotool.dashserve import ASSETS, board_page  # noqa: E402  (the page and its files, as the board serves them)
from expotool.hub import LinkSimHub  # noqa: E402
from expotool.ws import OP_CLOSE, OP_TEXT, FrameDecoder, ProtocolError, accept_key, encode_frame  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "web" / "dashboard.html"
TICK_S = 0.04            # the firmware batches every 40 ms (WS_FLUSH_MS)
# A client this far behind is closed, as the board closes one that falls a
# whole ring behind (WS_RING_BYTES). Every client has its own buffer, so a
# stalled one never costs the others data, on the board or here.
MAX_BACKLOG = 16384


class SimServer:
    def __init__(self, hub: LinkSimHub | None = None, page: Path = PAGE, speed: float = 1.0) -> None:
        self.hub = hub or LinkSimHub()
        self.page = page
        self.speed = speed               # > 1 runs the simulated session faster (tests, rehearsal)
        self.writers: dict[int, asyncio.StreamWriter] = {}
        self.t0 = time.monotonic()
        self.server: asyncio.base_events.Server | None = None

    def now(self) -> float:
        return (time.monotonic() - self.t0) * self.speed

    async def start(self, host: str, port: int) -> int:
        self.server = await asyncio.start_server(self._conn, host, port)
        asyncio.get_running_loop().create_task(self._ticker())
        return self.server.sockets[0].getsockname()[1]

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            lines, direct = self.hub.tick(self.now())
            body = "".join(ln + "\n" for ln in lines)
            for cid, w in list(self.writers.items()):
                text = "".join(ln + "\n" for ln in direct.get(cid, [])) + body
                if not text:
                    continue
                if w.transport.get_write_buffer_size() > MAX_BACKLOG:
                    w.close()                           # a stalled client, as the board drops it
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
        elif path in ("/", "/index.html"):
            body = board_page(self.page.read_text(encoding="utf-8")).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()
        elif path in ASSETS and (self.page.parent / path[1:]).is_file():
            body = (self.page.parent / path[1:]).read_bytes()      # as the board serves them
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ASSETS[path]}\r\n".encode()
                         + b"Cache-Control: max-age=86400\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()
        elif path == "/favicon.ico":
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            writer.close()
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            writer.close()

    async def _websocket(self, reader, writer, headers) -> None:
        key = headers.get("sec-websocket-key", "")
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
        await writer.drain()
        peer = writer.get_extra_info("peername") or ("",)
        host = peer[0][7:] if peer[0].startswith("::ffff:") else peer[0]
        cid = self.hub.connect(self.now(), host)
        self.writers[cid] = writer
        dec = FrameDecoder()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for op, payload in dec.feed(data):
                    if op == OP_TEXT:
                        self.hub.receive(cid, payload.decode("utf-8", "replace"), self.now())
                    elif op == OP_CLOSE:
                        raise ConnectionError
                    else:
                        self.hub.receive(cid, "PING", self.now())   # control frames count as liveness
        except (ConnectionError, ProtocolError):
            pass
        finally:
            self.writers.pop(cid, None)
            self.hub.disconnect(cid)
            writer.close()


def start_in_thread(host: str = "127.0.0.1", port: int = 0, hub: LinkSimHub | None = None,
                    speed: float = 1.0):
    """For tests: run a server on a background thread. Returns (server, port, stop)."""
    loop = asyncio.new_event_loop()
    srv = SimServer(hub, speed=speed)
    started = threading.Event()
    box = {}

    def runner():
        asyncio.set_event_loop(loop)
        box["port"] = loop.run_until_complete(srv.start(host, port))
        started.set()
        loop.run_forever()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    started.wait(5)

    def stop():
        loop.call_soon_threadsafe(loop.stop)
        t.join(2)

    return srv, box["port"], stop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local stand-in for the expo board (no hardware).")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="listen on every interface, not just this computer")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="run the simulated session this many times faster (the controller and "
                         "analyser timeouts count simulated time, so keep it at 1 with a real iPad)")
    args = ap.parse_args(argv)
    host = "0.0.0.0" if args.lan else "127.0.0.1"

    async def amain():
        port = await SimServer(speed=args.speed).start(host, args.port)
        print(f"expo sim server on http://{'127.0.0.1' if not args.lan else '<this computer>'}:{port}/  "
              f"(watch only: /?view, receiver: ws://127.0.0.1:{port}/ws)", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
