"""The browser-facing surface: static files, the socket, one live pane."""

import asyncio
import base64
import json
import os
import re
import shutil
import struct

from conftest import run_async

from webmon import websocket as ws
from webmon import server as server_module
from webmon.monitor import Monitor
from webmon.server import HttpServer


class Browser:
    """A WebSocket client that speaks exactly what the page speaks."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port: int) -> "Browser":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                "GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: keep-alive, Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert b"101 Switching Protocols" in head
        assert ws.accept_key(key).encode() in head
        return cls(reader, writer)

    def send(self, message: dict) -> None:
        self.writer.write(ws.client_mask_frame(ws.OP_TEXT, json.dumps(message).encode()))

    async def next_frame(self, timeout: float = 10.0):
        return await asyncio.wait_for(ws.read_frame(self.reader), timeout=timeout)

    async def next_json(self, kind: str | None = None, timeout: float = 10.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            frame = await self.next_frame(timeout=max(0.1, deadline - loop.time()))
            if frame.opcode != ws.OP_TEXT:
                continue
            message = json.loads(frame.payload)
            if kind is None or message.get("t") == kind:
                return message
        raise AssertionError(f"no {kind} message arrived")

    async def screen_bytes(self, pane: str, needle: bytes, timeout: float = 15.0) -> bytes:
        number = int(pane.lstrip("%"))
        collected = b""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            frame = await self.next_frame(timeout=max(0.1, deadline - loop.time()))
            if frame.opcode != ws.OP_BINARY:
                continue
            if struct.unpack("!I", frame.payload[:4])[0] != number:
                continue
            collected += frame.payload[4:]
            if needle in collected:
                return collected
        raise AssertionError(f"{needle!r} never arrived for {pane}")

    def close(self) -> None:
        self.writer.close()


async def serving(server, **options):
    """Start a monitor and its HTTP front end, model already populated."""
    monitor = Monitor(server.tmux, server.socket, **options)
    http = HttpServer(monitor, "127.0.0.1", 0)
    await monitor.start()
    await http.start()
    port = http.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while not monitor.model.panes and loop.time() < deadline:
        await asyncio.sleep(0.05)
    assert monitor.model.panes, "the monitor never saw the server"
    return monitor, http, port


async def http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    payload = await asyncio.wait_for(reader.read(), timeout=10)
    writer.close()
    head, _, body = payload.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body


def test_the_page_and_its_assets_are_served(server):
    async def scenario():
        monitor, http, port = await serving(server)
        try:
            return [
                await http_get(port, "/"),
                await http_get(port, "/app.js"),
                await http_get(port, "/vendor/xterm.js"),
                await http_get(port, "/../../pyproject.toml"),
                await http_get(port, "/nope"),
            ]
        finally:
            await http.stop()
            await monitor.stop()

    page, script, xterm, escape, missing = run_async(scenario())
    assert page[0] == 200 and b"<title>webmon</title>" in page[1]
    assert script[0] == 200 and b"WebSocket" in script[1]
    assert xterm[0] == 200 and len(xterm[1]) > 100_000
    assert escape[0] == 404, "a path outside the static root must not be served"
    assert missing[0] == 404


def test_asset_urls_change_when_contents_change(tmp_path, monkeypatch):
    static = tmp_path / "static"
    shutil.copytree(server_module.STATIC_ROOT, static)
    monkeypatch.setattr(server_module, "STATIC_ROOT", static)

    async def scenario():
        http = HttpServer(Monitor("tmux", "/tmp/webmon-unused"), "127.0.0.1", 0)
        await http.start()
        port = http.sockets[0].getsockname()[1]
        try:
            status, before = await http_get(port, "/")
            assert status == 200
            urls = re.findall(rb'(?:src|href)="([^"]+)"', before)
            assert len(urls) == 4
            for url in urls:
                match = re.fullmatch(rb"/(.+)\.([0-9a-f]{16})\.(css|js)", url)
                assert match
                status, body = await http_get(port, url.decode())
                assert status == 200
                asset = f"{match[1].decode()}.{match[3].decode()}"
                assert body == (static / asset).read_bytes()
            assert (await http_get(port, "/index.html"))[1] == before
            for asset in ("app.css", "app.js"):
                target = static / asset
                target.write_bytes(target.read_bytes() + b"\n")
            status, after = await http_get(port, "/")
            updated = re.findall(rb'(?:src|href)="([^"]+)"', after)
            for old, new in zip(urls, updated):
                assert (old == new) == old.startswith(b"/vendor/")
                assert (await http_get(port, new.decode()))[0] == 200
                if old != new:
                    assert (await http_get(port, old.decode()))[0] == 404
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HEAD / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
            await writer.wait_closed()
            head, _, body = response.partition(b"\r\n\r\n")
            assert b"Cache-Control: no-cache" in head
            assert f"Content-Length: {len(after)}".encode() in head
            assert body == b""
        finally:
            await http.stop()

    run_async(scenario())


def test_a_browser_gets_the_model_then_a_pane_it_asks_for(server):
    async def scenario():
        monitor, http, port = await serving(server)
        try:
            server.command(
                "new-window", "-t", "work", "-n", "tick",
                "sh -c 'while :; do printf TICK; sleep 0.2; done'",
            )
            browser = await Browser.connect(port)
            model = await browser.next_json("model")
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                panes = {
                    name: values
                    for name, values in model.get("panes", {}).items()
                    if values.get("window_name") == "tick"
                }
                if panes:
                    break
                model = await browser.next_json("model")
            assert panes, "the new window never reached the browser"
            pane = next(iter(panes))
            browser.send({"t": "view", "pane": pane})
            screen = await browser.next_json("screen")
            live = await browser.screen_bytes(pane, b"TICK")
            browser.close()
            return model, pane, screen, live
        finally:
            await http.stop()
            await monitor.stop()

    model, pane, screen, live = run_async(scenario())
    assert model["socket"] == server.socket
    assert model["sessions"][0]["name"] == "work"
    assert screen["pane"] == pane and screen["data"].startswith("\x1bc")
    assert b"TICK" in live


def test_closing_the_browser_releases_the_panes_it_watched(server):
    async def scenario():
        monitor, http, port = await serving(server)
        try:
            browser = await Browser.connect(port)
            await browser.next_json("model")
            pane = server.pane_ids()[0]
            browser.send({"t": "view", "pane": pane})
            await browser.next_json("screen")
            watching = dict(monitor.pane_watchers)
            browser.close()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while loop.time() < deadline and (monitor.pane_watchers or monitor.watchers):
                await asyncio.sleep(0.05)
            return watching, monitor.pane_watchers, monitor.watchers, monitor._viewers
        finally:
            await http.stop()
            await monitor.stop()

    watching, panes, watchers, viewers = run_async(scenario())
    assert watching, "the pane was never registered"
    assert not panes and not watchers and not viewers


def test_browser_resize_updates_screen_and_disconnect_releases_client(server):
    async def scenario():
        monitor, http, port = await serving(server, stream_columns=100)
        browser = await Browser.connect(port)
        pane = server.pane_ids()[0]
        try:
            browser.send({"t": "view", "pane": pane, "cols": 100, "rows": 35})
            screen = await browser.next_json("screen")
            assert (screen["cols"], screen["rows"]) == (100, 35)
            browser.send({"t": "resize", "pane": pane, "cols": 100, "rows": 65})
            while screen["rows"] != 65:
                screen = await browser.next_json("screen")
            browser.close()
            deadline = asyncio.get_running_loop().time() + 5
            while monitor.stream_sizes.clients and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert not monitor.stream_sizes.clients
        finally:
            browser.close()
            await http.stop()
            await monitor.stop()
    run_async(scenario())
