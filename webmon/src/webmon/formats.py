from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .protocol import SubscriptionChanged

if TYPE_CHECKING:
    from .control import ControlClient
    from .monitor import Monitor

MAX_TEMPLATES = 64
MAX_TEMPLATE_BYTES = 4096
MAX_VALUE_BYTES = 65536


def validate_template(template: str) -> None:
    if not isinstance(template, str) or len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise ValueError("template must be a string of at most 4096 UTF-8 bytes")
    if any(char in template for char in ("\0", "\r", "\n")):
        raise ValueError("template cannot contain NUL, CR, or LF")
    if "#(" in template or recursive_expansion(template):
        raise ValueError("shell jobs and recursive E/T expansion are not allowed")


def recursive_expansion(template: str) -> bool:
    depth = 0
    for index, char in enumerate(template):
        if template[index:index + 2] == "#{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if depth and char in "ET" and template[index + 1:index + 2] in (";", ":"):
            if template[max(0, index - 2):index] == "#{" or template[index - 1:index] == ";":
                return True
    return False


def tmux_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass
class FormatFeed:
    name: str
    template: str
    listeners: dict[object, Callable[[dict[str, str], str | None], None]] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    ready: bool = False

    def publish(self) -> None:
        if self.ready:
            for callback in list(self.listeners.values()):
                callback(dict(self.values), self.error)


class FormatSubscriptions:
    def __init__(self, monitor: Monitor) -> None:
        self.monitor = monitor
        self.feeds: dict[str, FormatFeed] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    async def subscribe(self, token: object, template: str, callback: Callable) -> None:
        validate_template(template)
        async with self._lock:
            feed = next((item for item in self.feeds.values() if item.template == template), None)
            if feed is None:
                if len(self.feeds) >= MAX_TEMPLATES:
                    raise ValueError("too many active metadata templates")
                self._counter += 1
                feed = FormatFeed(f"webmon-format-{self._counter}", template)
                self.feeds[feed.name] = feed
                feed.listeners[token] = callback
                try:
                    for client in list(self.monitor._state_clients.values()):
                        await self._install(client, feed)
                    await self._snapshot(feed)
                except BaseException:
                    self.feeds.pop(feed.name, None)
                    self._remove(feed)
                    raise
            else:
                feed.listeners[token] = callback
                callback(dict(feed.values), feed.error)

    async def unsubscribe(self, token: object) -> None:
        async with self._lock:
            for name, feed in list(self.feeds.items()):
                feed.listeners.pop(token, None)
                if not feed.listeners:
                    del self.feeds[name]
                    self._remove(feed)

    def _remove(self, feed: FormatFeed) -> None:
        for client in list(self.monitor._state_clients.values()):
            with contextlib.suppress(ConnectionError):
                client.send(f"refresh-client -B {tmux_quote(feed.name)}")

    async def _install(self, client: ControlClient, feed: FormatFeed) -> None:
        reply = await client.execute(f"refresh-client -B {tmux_quote(feed.name + ':%*:' + feed.template)}")
        if reply.error:
            raise ValueError("tmux could not subscribe to the metadata format")

    async def attach(self, client: ControlClient) -> None:
        async with self._lock:
            for feed in list(self.feeds.values()):
                await self._install(client, feed)

    async def refresh(self) -> None:
        async with self._lock:
            for feed in list(self.feeds.values()):
                await self._snapshot(feed)

    async def _snapshot(self, feed: FormatFeed) -> None:
        feed.ready = False
        feed.error = self.monitor.model.error or None
        current = set(self.monitor.model.panes)
        feed.values = {pane: value for pane, value in feed.values.items() if pane in current}
        feed.revisions = {pane: rev for pane, rev in feed.revisions.items() if pane in current}
        for pane in current:
            client = self.monitor._state_clients.get(self.monitor.model.session_of(pane) or "")
            if client is None or not client.alive:
                feed.values.pop(pane, None)
                feed.error = "tmux metadata connection is unavailable"
                continue
            revision = feed.revisions.get(pane, 0)
            try:
                reply = await client.execute(
                    f"list-panes -t {tmux_quote(pane)} "
                    f"-f {tmux_quote('#{==:#{pane_id},' + pane + '}')} "
                    f"-F {tmux_quote(feed.template)}"
                )
                if reply.error:
                    raise ValueError("tmux could not evaluate the metadata format")
                value = reply.text
                if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
                    raise ValueError("expanded metadata exceeds 65536 UTF-8 bytes")
                if feed.revisions.get(pane, 0) == revision:
                    feed.values[pane] = value
            except (ConnectionError, asyncio.TimeoutError, ValueError) as exc:
                feed.values.pop(pane, None)
                feed.error = str(exc)
        feed.ready = True
        feed.publish()

    def changed(self, event: SubscriptionChanged) -> None:
        feed = self.feeds.get(event.name)
        if feed is None:
            return
        if event.pane not in self.monitor.model.panes:
            self.monitor.handle_notification("%layout-change", "")
            return
        if len(event.value.encode("utf-8")) > MAX_VALUE_BYTES:
            feed.values.pop(event.pane, None)
            feed.error = "expanded metadata exceeds 65536 UTF-8 bytes"
        else:
            feed.values[event.pane] = event.value
        feed.revisions[event.pane] = feed.revisions.get(event.pane, 0) + 1
        feed.publish()

    def unavailable(self, error: str) -> None:
        for feed in self.feeds.values():
            feed.values.clear()
            feed.revisions.clear()
            feed.error = error
            feed.ready = True
            feed.publish()
