"""The browser-facing surface: static files."""

import asyncio
import re
import shutil

from conftest import run_async

from webmon import server as server_module
from webmon.monitor import Monitor
from webmon.server import HttpServer


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
