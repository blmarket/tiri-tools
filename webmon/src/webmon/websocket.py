"""A minimal RFC 6455 server endpoint on top of asyncio streams.

The proxy needs one WebSocket per browser and nothing else — no extensions, no
client role, no permessage-deflate — so this is a few hundred lines instead of
a dependency. Frames the browser sends are always masked; frames the server
sends never are.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from dataclasses import dataclass

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_MESSAGE_BYTES = 1 << 20


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    pass


def accept_key(key: str) -> str:
    """Return the `Sec-WebSocket-Accept` value for a client's key."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(headers: dict[str, str]) -> bytes | None:
    """Build the 101 response for an upgrade request, or None if it is not one."""
    if headers.get("upgrade", "").lower() != "websocket":
        return None
    if "upgrade" not in headers.get("connection", "").lower():
        return None
    key = headers.get("sec-websocket-key")
    if not key:
        return None
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_key(key).encode("ascii") + b"\r\n\r\n"
    )


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """Encode one unmasked server frame."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def close_frame(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8"))


@dataclass
class Frame:
    opcode: int
    payload: bytes
    fin: bool


def unmask(payload: bytes, key: bytes) -> bytes:
    if not key:
        return payload
    masked = bytearray(payload)
    for index in range(len(masked)):
        masked[index] ^= key[index & 3]
    return bytes(masked)


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """Read one frame, unmasking it. Raises WebSocketClosed at end of stream."""
    try:
        header = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise WebSocketClosed(str(exc)) from exc
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    try:
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length > MAX_MESSAGE_BYTES:
            raise WebSocketError(f"frame too large: {length}")
        key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise WebSocketClosed(str(exc)) from exc
    return Frame(opcode, unmask(payload, key), fin)


class WebSocket:
    """One browser connection.

    `send` never blocks on the peer: the transport's own buffer is the queue,
    and `buffered` reports how far behind the peer is so the caller can drop
    screen traffic instead of growing without bound.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def buffered(self) -> int:
        """Bytes queued in the transport but not yet handed to the kernel."""
        transport = self._writer.transport
        try:
            return transport.get_write_buffer_size()
        except AttributeError:
            return 0

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self._send(OP_BINARY, payload)

    def _send(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        try:
            self._writer.write(encode_frame(opcode, payload))
        except (ConnectionError, RuntimeError):
            self._closed = True

    async def drain(self) -> None:
        if self._closed:
            return
        try:
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            self._closed = True

    async def receive(self) -> Frame | None:
        """Return the next data frame, answering pings and closes internally."""
        pending: list[bytes] = []
        pending_opcode = OP_TEXT
        while True:
            try:
                frame = await read_frame(self._reader)
            except WebSocketClosed:
                self._closed = True
                return None
            except WebSocketError:
                await self.close(1009)
                return None
            if frame.opcode == OP_CLOSE:
                await self.close()
                return None
            if frame.opcode == OP_PING:
                self._send(OP_PONG, frame.payload)
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CONTINUATION:
                pending.append(frame.payload)
            else:
                pending = [frame.payload]
                pending_opcode = frame.opcode
            if frame.fin:
                return Frame(pending_opcode, b"".join(pending), True)

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.write(close_frame(code))
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            pass
        try:
            self._writer.close()
        except (ConnectionError, RuntimeError):
            pass


def client_mask_frame(opcode: int, payload: bytes) -> bytes:
    """Encode a masked client frame. Used by the tests, not the server."""
    key = os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + key + unmask(payload, key)
