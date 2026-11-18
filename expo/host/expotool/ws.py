"""Minimal WebSocket (RFC 6455) framing, client and server helpers, stdlib only.

Just enough for this link: text messages, ping/pong, close. No extensions,
no subprotocols. The board's side is ESP-IDF's HTTP server; this module is
the laptop's side (``WSClient``) and the local simulator's (``FrameDecoder``
plus ``encode_frame``, used by ``sim_server.py``).
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1 << 20


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _xor(payload: bytes, key: bytes) -> bytes:
    n = len(payload)
    if not n:
        return payload
    mask = int.from_bytes((key * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ mask).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True) -> bytes:
    """One frame. Clients must mask (``mask=True``); servers must not."""
    n = len(payload)
    b0 = (0x80 if fin else 0) | opcode
    m = 0x80 if mask else 0
    if n < 126:
        head = bytes([b0, m | n])
    elif n < 1 << 16:
        head = bytes([b0, m | 126]) + n.to_bytes(2, "big")
    else:
        head = bytes([b0, m | 127]) + n.to_bytes(8, "big")
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + _xor(payload, key)


class ProtocolError(Exception):
    pass


class FrameDecoder:
    """Incremental decoder: ``feed(bytes)`` returns complete messages as
    ``(opcode, payload)``. Fragmented data messages are reassembled; control
    frames are returned as they arrive."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frag_op: int | None = None
        self._frag = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out = []
        while True:
            frame = self._take()
            if frame is None:
                return out
            fin, op, payload = frame
            if op >= 0x8:                                   # control frame
                out.append((op, payload))
            elif op == OP_CONT:
                if self._frag_op is None:
                    raise ProtocolError("continuation frame without a start")
                self._frag += payload
                if fin:
                    out.append((self._frag_op, bytes(self._frag)))
                    self._frag_op, self._frag = None, bytearray()
            elif fin:
                out.append((op, payload))
            else:
                self._frag_op, self._frag = op, bytearray(payload)
            if len(self._frag) > MAX_MESSAGE:
                raise ProtocolError("message too large")

    def _take(self) -> tuple[bool, int, bytes] | None:
        b = self._buf
        if len(b) < 2:
            return None
        fin, op = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n = bool(b[1] & 0x80), b[1] & 0x7F
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n, pos = int.from_bytes(b[2:4], "big"), 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, pos = int.from_bytes(b[2:10], "big"), 10
        if n > MAX_MESSAGE:
            raise ProtocolError("frame too large")
        key = b""
        if masked:
            if len(b) < pos + 4:
                return None
            key, pos = bytes(b[pos:pos + 4]), pos + 4
        if len(b) < pos + n:
            return None
        payload = bytes(b[pos:pos + n])
        del b[:pos + n]
        return fin, op, (_xor(payload, key) if masked else payload)


class WSClient:
    """Blocking client. ``recv(timeout)`` returns the next text message, or
    ``None`` if nothing arrived in time; it raises ``ConnectionError`` once the
    connection is gone. Pings are answered automatically."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws":
            raise ValueError(f"only ws:// URLs are supported, not {url!r}")
        host, port = u.hostname or "", u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.url = url
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in head:
            if time.monotonic() > deadline or len(head) > 8192:
                raise ConnectionError("no WebSocket handshake reply")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during the handshake")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"handshake refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise ConnectionError("handshake reply has the wrong Sec-WebSocket-Accept")
        self._dec = FrameDecoder()
        self._pending: list[str] = []
        self._closed = False
        self.bytes_in = 0
        self.frames_in = 0
        if rest:
            self._absorb(rest)

    def _absorb(self, data: bytes) -> None:
        self.bytes_in += len(data)
        for op, payload in self._dec.feed(data):
            if op == OP_TEXT:
                self.frames_in += 1
                self._pending.append(payload.decode("utf-8", "replace"))
            elif op == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload, mask=True))
            elif op == OP_CLOSE:
                self._closed = True

    def recv(self, timeout: float | None = 1.0) -> str | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._pending:
            if self._closed:
                raise ConnectionError("server closed the connection")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not data:
                self._closed = True
                raise ConnectionError("connection closed")
            self._absorb(data)
        return self._pending.pop(0)

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(OP_TEXT, text.encode(), mask=True))

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, b"\x03\xe8", mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._closed = True
