"""The dashboard's local server, run inside the results process (DESIGN 3).

The monitor is the laptop's own browser at http://127.0.0.1:8800/. It never
talks to the board: the results process relays the board's stream to it,
adds its own messages (run numbers, analysis results with the traces the
technical view draws, link up/down), and takes back exactly one command,
``VIEW PUBLIC|TECHNICAL`` (the mode switch). Nothing a dashboard sends can
reach the cuff.

Messages to the dashboard are WebSocket text frames: either board lines as
received (``S,...``, ``C,...``, ``EV,...``), or one line ``A,<json>`` from the
results process. A dashboard that opens mid-session first gets the latest
results-process messages and then the last two minutes of everything, in the
order it happened, so it draws the run so far at once.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path

from .ws import OP_CLOSE, OP_TEXT, FrameDecoder, ProtocolError, accept_key, encode_frame

PAGE = Path(__file__).resolve().parents[2] / "web" / "dashboard.html"
HISTORY_S = 120.0
ALLOWED = ("VIEW PUBLIC", "VIEW TECHNICAL")


class DashServer:
    def __init__(self, on_command=None, page: Path = PAGE) -> None:
        self.page = page
        self.on_command = on_command or (lambda cmd: None)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.writers: set = set()
        self.history: deque = deque()             # (monotonic, frame text)
        self.latest: dict = {}                    # kind -> last A message (run, result, link, hello)
        self.port = 0
        self._thread: threading.Thread | None = None

    # -- called from the results process's thread -----------------------------

    def start(self, host: str = "127.0.0.1", port: int = 8800) -> int:
        ready = threading.Event()

        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            server = self.loop.run_until_complete(asyncio.start_server(self._conn, host, port))
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        ready.wait(5)
        return self.port

    def stop(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def relay(self, frame: str) -> None:
        """A frame of board lines, as received."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._send, frame, True)

    def message(self, kind: str, **payload) -> None:
        """A results-process message: ``A,{"kind": ..., ...}``."""
        text = "A," + json.dumps({"kind": kind, **payload}, separators=(",", ":"))
        if self.loop:
            self.loop.call_soon_threadsafe(self._remember, kind, text)

    # -- on the server's own loop ----------------------------------------------

    def _remember(self, kind: str, text: str) -> None:
        if kind == "run":
            self.latest.pop("result", None)       # a new run supersedes the last result
        self.latest[kind] = text
        self._send(text, True)                    # in the history too, in order with the stream

    def _send(self, text: str, keep: bool) -> None:
        if keep:
            now = time.monotonic()
            self.history.append((now, text))
            while self.history and now - self.history[0][0] > HISTORY_S:
                self.history.popleft()
        data = encode_frame(OP_TEXT, text.encode(), mask=False)
        for w in list(self.writers):
            if w.transport.get_write_buffer_size() > 4 * 1024 * 1024:
                w.close()                          # a browser tab that stopped reading
                self.writers.discard(w)
                continue
            w.write(data)

    async def _conn(self, reader, writer) -> None:
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
        if path in ("/", "/index.html", "/dashboard.html"):
            body = self.page.read_bytes()        # read on every load: edits show on refresh
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
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
        # Catch the new tab up in the order things happened: the latest
        # results-process messages first (they may be older than the history),
        # then the last two minutes, which repeat any of them that are recent.
        backlog = list(self.latest.values()) + [t for _, t in self.history]
        for i in range(0, len(backlog), 200):
            writer.write(encode_frame(OP_TEXT, "\n".join(backlog[i:i + 200]).encode(), mask=False))
        self.writers.add(writer)
        dec = FrameDecoder()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for op, payload in dec.feed(data):
                    if op == OP_CLOSE:
                        raise ConnectionError
                    if op == OP_TEXT:
                        cmd = payload.decode("utf-8", "replace").strip().upper()
                        if cmd in ALLOWED:          # the mode switch, nothing else
                            self.on_command(cmd)
        except (ConnectionError, ProtocolError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
