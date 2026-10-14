from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import shlex
import uuid

from .control import ControlClient


def is_sizing_session(name: str) -> bool:
    return re.fullmatch(r"__webmon_size_[0-9a-f]{32}", name) is not None


def dimensions(message: dict) -> tuple[int, int] | None:
    """Accept bounded terminal dimensions without coercing untrusted values."""
    cols, rows = message.get("cols"), message.get("rows")
    if type(cols) is int and type(rows) is int and 1 <= cols <= 1000 and 1 <= rows <= 1000:
        return cols, rows
    return None


class SizingClient(ControlClient):
    """Own a session whose only window is the browser's watched window."""

    def __init__(self, tmux: str, socket: str) -> None:
        super().__init__(tmux, socket, f"__webmon_size_{uuid.uuid4().hex}", flags="no-output")

    @property
    def argv(self) -> list[str]:
        return [
            self.tmux, "-S", self.socket, "-C", "new-session", "-f", self.flags,
            "-s", self.session, "sleep 86400",
        ]

    async def stop(self) -> None:
        await super().stop()
        process = await asyncio.create_subprocess_exec(
            self.tmux, "-S", self.socket, "kill-session", "-t", f"={self.session}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


@dataclass
class BrowserSize:
    client: SizingClient
    session: str
    window: str
    size: tuple[int, int]


class StreamSizes:
    """Give each browser view an independent tmux session and client size."""

    def __init__(self, tmux: str, socket: str, columns: int | None) -> None:
        self.tmux = tmux
        self.socket = socket
        self.columns = columns
        self.clients: dict[tuple[object, str], BrowserSize] = {}
        self.lock = asyncio.Lock()

    async def _execute(self, client: ControlClient, command: str) -> str:
        reply = await client.execute(command)
        if reply.error:
            raise ConnectionError(f"browser sizing failed: {reply.text}")
        return reply.text.strip()

    async def resize(self, watcher: object, pane: str, session: str, window: str, cols: int, rows: int) -> None:
        if not self.columns:
            return
        key = (watcher, pane)
        size = (min(self.columns, cols), rows)
        async with self.lock:
            current = self.clients.get(key)
            if current is not None and (current.session != session or current.window != window or not current.client.alive):
                del self.clients[key]
                await current.client.stop()
                current = None
            if current is not None and current.size == size:
                return
            client = current.client if current else SizingClient(self.tmux, self.socket)
            try:
                if current is None:
                    await client.start()
                    await self._execute(client, "set-option destroy-unattached on")
                    name = await self._execute(client, "display-message -p '#{client_name}'")
                    action = f"if-shell -F {shlex.quote('#{==:#{hook_client},' + name + '}')} {shlex.quote('kill-session -t =' + client.session)}"
                    await self._execute(client, f"set-hook client-detached {shlex.quote(action)}")
                    await self._execute(client, f"refresh-client -C {size[0]}x{size[1]}")
                    await self._execute(client, f"set-option -w -t {window} window-size smallest")
                    await self._execute(client, f"set-option -w -t {window} aggressive-resize on")
                    index = int(await self._execute(client, "display-message -p '#{window_index}'"))
                    await self._execute(client, f"link-window -k -s {window} -t {client.session}:{index}")
                    await self._execute(client, f"select-window -t {client.session}:{index}")
                else:
                    await self._execute(client, f"refresh-client -C {size[0]}x{size[1]}")
                self.clients[key] = BrowserSize(client, session, window, size)
            except BaseException:
                self.clients.pop(key, None)
                await client.stop()
                raise

    async def release(self, watcher: object, pane: str) -> None:
        async with self.lock:
            current = self.clients.pop((watcher, pane), None)
            if current is not None:
                await current.client.stop()

    async def stop(self) -> None:
        async with self.lock:
            clients, self.clients = self.clients, {}
            for current in clients.values():
                await current.client.stop()
