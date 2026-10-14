"""HTTP and WebSocket front end: static files, one socket per browser.

Authentication is deliberately absent — webmon expects to sit behind a
reverse proxy that has already authenticated the request. What it does own is
backpressure: a browser that cannot keep up with a pane loses screen bytes and
gets a fresh capture, rather than growing a queue in this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import struct
from pathlib import Path

from .monitor import Monitor
from .websocket import OP_TEXT, WebSocket, handshake_response

logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).parent / "static"
STATIC_ASSETS = ("app.css", "app.js", "vendor/xterm.css", "vendor/xterm.js")
# A browser this far behind on screen bytes is not going to catch up on a
# stream; it gets dropped traffic and a repaint instead.
SCREEN_BUFFER_CAP = 2 << 20
SCREEN_BUFFER_LOW = 64 << 10
DRAIN_POLL = 0.25
REQUEST_TIMEOUT = 120.0
MAX_HEADER_BYTES = 32 << 10


def pane_number(pane: str) -> int:
    """Map `%12` to 12 for the binary screen framing."""
    try:
        return int(pane.lstrip("%"))
    except ValueError:
        return 0


class Connection:
    """One browser: model messages as JSON text, screen bytes as binary."""

    def __init__(self, websocket: WebSocket, monitor: Monitor) -> None:
        self.ws = websocket
        self.monitor = monitor
        self.panes: set[str] = set()
        self._dropped: set[str] = set()
        self._drain_task: asyncio.Task[None] | None = None

    # -- Watcher ----------------------------------------------------------

    def deliver_model(self, message: dict[str, object]) -> None:
        self.ws.send_text(json.dumps(message))

    def deliver_reset(self, pane: str, payload: dict[str, object]) -> None:
        self._dropped.discard(pane)
        self.ws.send_text(json.dumps(payload))

    def deliver_screen(self, pane: str, data: bytes) -> None:
        if pane in self._dropped:
            return
        if self.ws.buffered > SCREEN_BUFFER_CAP:
            self._dropped.add(pane)
            self.ws.send_text(json.dumps({"t": "screen-lagged", "pane": pane}))
            return
        self.ws.send_binary(struct.pack("!I", pane_number(pane)) + data)

    # -- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        self.monitor.add_watcher(self)
        self._drain_task = asyncio.create_task(self._drain_loop())
        try:
            while True:
                frame = await self.ws.receive()
                if frame is None:
                    break
                if frame.opcode != OP_TEXT:
                    continue
                await self._on_message(frame.payload)
        finally:
            if self._drain_task is not None:
                self._drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drain_task
            await self.monitor.remove_watcher(self)
            await self.ws.close()

    async def _on_message(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("t")
        pane = message.get("pane")
        if kind == "view" and isinstance(pane, str):
            self.panes.add(pane)
            await self.monitor.watch_pane(pane, self, message)
        elif kind == "unview" and isinstance(pane, str):
            self.panes.discard(pane)
            self._dropped.discard(pane)
            await self.monitor.unwatch_pane(pane, self)
        elif kind == "resize" and isinstance(pane, str) and pane in self.panes:
            await self.monitor.resize_pane(pane, self, message)
        elif kind == "repaint" and isinstance(pane, str) and pane in self.panes:
            await self.monitor.repaint(pane, self)
        elif kind == "ping":
            self.ws.send_text(json.dumps({"t": "pong"}))

    async def _drain_loop(self) -> None:
        """Repaint panes whose stream was dropped once the socket catches up."""
        while not self.ws.closed:
            await asyncio.sleep(DRAIN_POLL)
            if not self._dropped or self.ws.buffered > SCREEN_BUFFER_LOW:
                continue
            for pane in list(self._dropped):
                self._dropped.discard(pane)
                if pane in self.panes:
                    await self.monitor.repaint(pane, self)


class HttpServer:
    """Serves the single page, its assets, and the `/ws` endpoint."""

    def __init__(self, monitor: Monitor, host: str, port: int) -> None:
        self.monitor = monitor
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    @property
    def sockets(self) -> list[object]:
        return list(self._server.sockets) if self._server is not None else []

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop listening and drop live browsers.

        `wait_closed` waits for every handler, and a browser that is simply
        idle never ends one on its own, so the connections are closed here
        rather than waited on.
        """
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._connections):
            with contextlib.suppress(Exception):
                writer.close()
        self._connections.clear()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._connections.add(writer)
        try:
            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                method, path, headers = request
                if path.split("?")[0] == "/ws":
                    response = handshake_response(headers)
                    if response is None:
                        await self._respond(writer, 400, b"expected a websocket upgrade")
                        break
                    writer.write(response)
                    await writer.drain()
                    await Connection(WebSocket(reader, writer), self.monitor).run()
                    return
                keep_alive = await self._serve_static(writer, method, path, headers)
                if not keep_alive:
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.debug("connection failed", exc_info=True)
        finally:
            self._connections.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str]] | None:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_TIMEOUT
            )
        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
        ):
            return None
        if len(head) > MAX_HEADER_BYTES:
            return None
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return None
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return parts[0], parts[1], headers

    async def _serve_static(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
    ) -> bool:
        # One decision for every response on this request: a client that
        # asked to close must not be left waiting for an EOF that only the
        # 200 path used to send.
        keep_alive = headers.get("connection", "").lower() != "close"
        if method not in ("GET", "HEAD"):
            await self._respond(writer, 405, b"method not allowed")
            return False
        name = path.split("?")[0]
        if name == "/":
            name = "/index.html"
        versioned = re.fullmatch(r"/(.+)\.([0-9a-f]{16})\.(css|js)", name)
        if versioned:
            asset = f"{versioned[1]}.{versioned[3]}"
            if asset in STATIC_ASSETS:
                name = f"/{asset}"
        target = (STATIC_ROOT / name.lstrip("/")).resolve()
        root = STATIC_ROOT.resolve()
        if not target.is_file() or (root not in target.parents and target != root):
            await self._respond(writer, 404, b"not found")
            return keep_alive
        try:
            body = target.read_bytes()
            if versioned and hashlib.sha256(body).hexdigest()[:16] != versioned[2]:
                await self._respond(writer, 404, b"not found")
                return keep_alive
            if target == root / "index.html":
                for asset in STATIC_ASSETS:
                    version = hashlib.sha256((root / asset).read_bytes()).hexdigest()[:16]
                    stem, suffix = asset.rsplit(".", 1)
                    body = body.replace(
                        f'"/{asset}"'.encode(), f'"/{stem}.{version}.{suffix}"'.encode()
                    )
        except OSError:
            await self._respond(writer, 404, b"not found")
            return keep_alive
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "text/javascript",
        ):
            content_type += "; charset=utf-8"
        await self._respond(
            writer,
            200,
            b"" if method == "HEAD" else body,
            content_type=content_type,
            content_length=len(body),
        )
        return keep_alive

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
        content_length: int | None = None,
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}
        head = (
            f"HTTP/1.1 {status} {reason.get(status, 'OK')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {content_length if content_length is not None else len(body)}\r\n"
            "Cache-Control: no-cache\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        with contextlib.suppress(ConnectionError):
            await writer.drain()
