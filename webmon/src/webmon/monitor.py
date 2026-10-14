"""Everything that talks to a server: control clients, polls, pane viewing.

The state plane keeps one silent control client per session for change pushes
and treats a periodic `list-panes -a` as the authority. The screen plane adds
a second client per *viewed* session, because output has to be on for it and
off for the state plane.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Iterable
from typing import Protocol

from .control import ControlClient
from .formats import FormatSubscriptions
from .model import Delta, Model, rows_from_poll
from .sizing import StreamSizes, dimensions, is_sizing_session
from .protocol import (
    POLL_FORMAT,
    SUBSCRIPTION_NAME,
    SubscriptionChanged,
    is_session_set_notification,
    is_structure_notification,
    split_state_row,
    subscription_command,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30.0
RECONCILE_INTERVAL = 10.0
FORMAT_REFRESH_INTERVAL = 1.2
# Change notifications arrive in bursts; one poll should answer a whole burst.
POLL_DEBOUNCE = 0.15
RETRY_MAX = 5.0
# How far behind a viewer client may fall before the server pauses the pane.
PAUSE_AFTER_SECONDS = 2
# Long enough that a pane flooding continuously settles into snapshots rather
# than a pause/continue storm.
RESUME_DELAY = 0.25


class Watcher(Protocol):
    """A browser connection, from the monitor's side."""

    def deliver_model(self, message: dict[str, object]) -> None: ...

    def deliver_screen(self, pane: str, data: bytes) -> None: ...

    def deliver_reset(self, pane: str, payload: dict[str, object]) -> None: ...


async def run_command(
    tmux: str, socket: str, arguments: list[str], *, timeout: float = 10.0
) -> tuple[int, str, str]:
    """Run one tmux client command against the socket."""
    process = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        socket,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    return (
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def output_command(pane: str, state: str) -> str:
    """Build `refresh-client -A '%N:state'`.

    The quotes are load-bearing: tmux's command parser rejects a bare token
    that starts with `%`, so an unquoted pane id comes back a syntax error
    rather than a flow-control change.
    """
    return f"refresh-client -A '{pane}:{state}'"


def snapshot_bytes(screen: bytes, cursor: tuple[int, int] | None) -> bytes:
    """Turn a `capture-pane -e` reply into bytes a terminal can be reset to.

    RIS first, so an emulator sitting in the alternate screen or holding a
    stale scroll region starts from the same place the pane's grid describes.
    """
    payload = b"\x1bc" + screen.replace(b"\n", b"\r\n")
    if cursor is not None:
        column, row = cursor
        payload += b"\x1b[%d;%dH" % (row + 1, column + 1)
    return payload


class SessionViewer:
    """The output-carrying control client for one session."""

    def __init__(self, monitor: "Monitor", session_id: str) -> None:
        self.monitor = monitor
        self.session_id = session_id
        self.enabled: set[str] = set()
        self._pending_snapshot: set[str] = set()
        self._paused: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._capture_lock = asyncio.Lock()
        self.client = ControlClient(
            monitor.tmux,
            monitor.socket,
            session_id,
            flags=f"ignore-size,pause-after={PAUSE_AFTER_SECONDS}",
            on_output=self._on_output,
            on_notification=self._on_notification,
        )

    async def start(self) -> None:
        await self.client.start()
        await self.mute_unwatched()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for pane in list(self.enabled):
            await self.disable(pane)
        await self.client.stop()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def mute_unwatched(self) -> None:
        """Turn output off for every pane in this session nobody is viewing.

        New panes arrive with output on, so this runs again on every structure
        change rather than only at attach.
        """
        if not self.client.alive:
            return
        for pane in self.monitor.model.panes_in_session(self.session_id):
            if pane not in self.enabled:
                with contextlib.suppress(ConnectionError):
                    self.client.send(output_command(pane, "off"))

    async def enable(self, pane: str) -> None:
        if pane in self.enabled:
            return
        self.enabled.add(pane)
        with contextlib.suppress(ConnectionError):
            self.client.send(output_command(pane, "on"))

    async def disable(self, pane: str) -> None:
        self.enabled.discard(pane)
        self._paused.discard(pane)
        self._pending_snapshot.discard(pane)
        with contextlib.suppress(ConnectionError):
            self.client.send(output_command(pane, "off"))

    async def snapshot(self, pane: str) -> dict[str, object] | None:
        """Capture the pane's screen and cursor as one reset payload.

        Adjacent capture and cursor commands run without an intervening pane
        read. All consumers receive the reset at the final reply boundary,
        before the control reader can deliver the next output notification.
        """
        async with self._capture_lock:
            if not self.client.alive:
                return None
            payload = None

            def captured(replies) -> None:
                nonlocal payload
                screen, cursor_reply = replies
                if screen.error or cursor_reply.error:
                    return
                cursor: tuple[int, int] | None = None
                parts = cursor_reply.text.split()
                if len(parts) != 4 or not all(part.isdigit() for part in parts):
                    return
                cursor = (int(parts[0]), int(parts[1]))
                payload = {
                    "t": "screen",
                    "pane": pane,
                    "cols": int(parts[2]),
                    "rows": int(parts[3]),
                    "sizing_columns": self.monitor.stream_sizes.columns or 0,
                    "data": snapshot_bytes(screen.raw, cursor).decode("latin-1"),
                }
                for watcher in list(self.monitor.pane_watchers.get(pane, ())):
                    watcher.deliver_reset(pane, payload)
                self._pending_snapshot.discard(pane)

            self._pending_snapshot.add(pane)
            try:
                await self.client.execute_group([
                    f"capture-pane -e -p -t {pane}",
                    f"display-message -p -t {pane} '#{{cursor_x}} #{{cursor_y}} #{{pane_width}} #{{pane_height}}'",
                ], captured)
            except (ConnectionError, asyncio.TimeoutError) as exc:
                logger.debug("snapshot of %s failed: %s", pane, exc)
            finally:
                self._pending_snapshot.discard(pane)
            return payload

    def _on_output(self, pane: str, data: bytes) -> None:
        if pane not in self.enabled or pane in self._pending_snapshot:
            return
        self.monitor.deliver_screen(pane, data)

    def _on_notification(self, name: str, arguments: str) -> None:
        pane = arguments.strip()
        if name == "%pause" and pane in self.enabled:
            self._paused.add(pane)
            self._spawn(self._resume(pane))
            return
        if name == "%continue":
            self._paused.discard(pane)
            return
        if name == "%layout-change":
            window = arguments.split(" ", 1)[0]
            for viewed in list(self.enabled):
                if self.monitor.model.panes.get(viewed, {}).get("window_id") == window:
                    self._spawn(self.snapshot(viewed))
        self.monitor.handle_notification(name, arguments)

    async def _resume(self, pane: str) -> None:
        """Un-pause a pane the server throttled, then repaint it.

        Output produced while paused is gone, so the stream cannot simply
        continue; a fresh capture is the only honest way back.
        """
        await asyncio.sleep(RESUME_DELAY)
        if pane not in self.enabled or not self.client.alive:
            return
        with contextlib.suppress(ConnectionError):
            self.client.send(output_command(pane, "continue"))
        self._paused.discard(pane)
        await self.monitor.repaint(pane)


class Monitor:
    """Owns the connection to one server and the model built from it."""

    def __init__(
        self,
        tmux: str,
        socket: str,
        *,
        poll_interval: float = POLL_INTERVAL,
        reconcile_interval: float = RECONCILE_INTERVAL,
        stream_columns: int | None = None,
    ) -> None:
        self.tmux = tmux
        self.socket = socket
        self.poll_interval = poll_interval
        self.reconcile_interval = reconcile_interval
        self.model = Model()
        self.model.socket = socket
        self.watchers: set[Watcher] = set()
        self.pane_watchers: dict[str, set[Watcher]] = {}
        self._state_clients: dict[str, ControlClient] = {}
        self._viewers: dict[str, SessionViewer] = {}
        self._wake = asyncio.Event()
        self._needs_poll = True
        self._needs_reconcile = True
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._backoff = 0.5
        self.formats = FormatSubscriptions(self)
        self.stream_sizes = StreamSizes(tmux, socket, stream_columns)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="monitor")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for viewer in list(self._viewers.values()):
            await viewer.stop()
        self._viewers.clear()
        await self.stream_sizes.stop()
        for client in list(self._state_clients.values()):
            await client.stop()
        self._state_clients.clear()

    # -- browsers ---------------------------------------------------------

    def add_watcher(self, watcher: Watcher) -> None:
        self.watchers.add(watcher)
        watcher.deliver_model(self.model.snapshot())

    async def remove_watcher(self, watcher: Watcher) -> None:
        self.watchers.discard(watcher)
        for pane in list(self.pane_watchers):
            await self.unwatch_pane(pane, watcher)

    async def watch_pane(self, pane: str, watcher: Watcher, size: dict | None = None) -> None:
        """Start streaming a pane to one browser, snapshot first."""
        session_id = self.model.session_of(pane)
        if session_id is None:
            watcher.deliver_reset(pane, {"t": "screen-gone", "pane": pane})
            return
        self.pane_watchers.setdefault(pane, set()).add(watcher)
        viewer = await self._viewer(session_id)
        if viewer is None:
            return
        await viewer.enable(pane)
        if size is not None:
            await self.resize_pane(pane, watcher, size)
        await viewer.snapshot(pane)

    async def resize_pane(self, pane: str, watcher: Watcher, message: dict) -> None:
        """Apply dimensions only to a stream owned by the requesting browser."""
        size = dimensions(message)
        session = self.model.session_of(pane)
        if size is None or session is None or watcher not in self.pane_watchers.get(pane, ()):
            return
        window = str(self.model.panes[pane].get("window_id", ""))
        if not re.fullmatch(r"@[0-9]+", window):
            return
        await self.stream_sizes.resize(watcher, pane, session, window, *size)

    async def unwatch_pane(self, pane: str, watcher: Watcher) -> None:
        await self.stream_sizes.release(watcher, pane)
        watchers = self.pane_watchers.get(pane)
        if not watchers:
            return
        watchers.discard(watcher)
        if watchers:
            return
        del self.pane_watchers[pane]
        session_id = self.model.session_of(pane)
        viewer = self._viewers.get(session_id or "")
        if viewer is not None:
            await viewer.disable(pane)
            await self._retire_viewer(viewer)

    async def repaint(self, pane: str, watcher: Watcher | None = None) -> None:
        """Re-send a pane's screen, for a resumed pause or a lagging browser."""
        session_id = self.model.session_of(pane)
        viewer = self._viewers.get(session_id or "")
        if viewer is None:
            return
        await viewer.snapshot(pane)

    def deliver_screen(self, pane: str, data: bytes) -> None:
        for watcher in list(self.pane_watchers.get(pane, ())):
            watcher.deliver_screen(pane, data)

    def broadcast(self, message: dict[str, object]) -> None:
        for watcher in list(self.watchers):
            watcher.deliver_model(message)

    # -- server side ------------------------------------------------------

    async def _viewer(self, session_id: str) -> SessionViewer | None:
        viewer = self._viewers.get(session_id)
        if viewer is not None:
            return viewer
        viewer = SessionViewer(self, session_id)
        self._viewers[session_id] = viewer
        try:
            await viewer.start()
        except OSError as exc:
            logger.warning("viewer client for %s failed: %s", session_id, exc)
            self._viewers.pop(session_id, None)
            return None
        return viewer

    async def _retire_viewer(self, viewer: SessionViewer) -> None:
        if viewer.enabled:
            return
        self._viewers.pop(viewer.session_id, None)
        await viewer.stop()

    def handle_notification(self, name: str, arguments: str) -> None:
        if is_structure_notification(name):
            self._needs_poll = True
            self._wake.set()
        if is_session_set_notification(name):
            self._needs_reconcile = True
            self._wake.set()
        if name == "%exit":
            self._needs_reconcile = True
            self._wake.set()

    def _on_subscription(self, event: SubscriptionChanged) -> None:
        if event.name != SUBSCRIPTION_NAME:
            self.formats.changed(event)
            return
        row = split_state_row(event.value)
        if row is None:
            return
        delta = self.model.apply_state_row(row)
        if delta.structure:
            self._needs_poll = True
            self._wake.set()
            return
        if not delta.empty:
            self.broadcast(self.model.delta_message(delta))

    async def _reconcile(self) -> None:
        """Match the set of state clients to the set of live sessions."""
        code, out, err = await run_command(
            self.tmux, self.socket, ["list-sessions", "-F", "#{session_id}\t#{session_name}"]
        )
        if code != 0:
            raise ConnectionError(err.strip() or "no server")
        sessions = {
            identity for line in out.splitlines()
            for identity, name in [line.split("\t", 1)]
            if not is_sizing_session(name)
        }
        for session_id in list(self._state_clients):
            client = self._state_clients[session_id]
            if session_id not in sessions or not client.alive:
                del self._state_clients[session_id]
                await client.stop()
        for session_id in sessions - set(self._state_clients):
            await self._attach_state_client(session_id)
        for session_id in list(self._viewers):
            if session_id not in sessions:
                viewer = self._viewers.pop(session_id)
                await viewer.stop()

    async def _attach_state_client(self, session_id: str) -> None:
        client = ControlClient(
            self.tmux,
            self.socket,
            session_id,
            flags="no-output,ignore-size",
            on_subscription=self._on_subscription,
            on_notification=self.handle_notification,
            on_close=self._on_client_closed,
        )
        try:
            await client.start()
        except OSError as exc:
            logger.warning("state client for %s failed: %s", session_id, exc)
            return
        try:
            client.send(subscription_command())
        except ConnectionError:
            await client.stop()
            return
        self._state_clients[session_id] = client
        await self.formats.attach(client)
        self._needs_poll = True

    def _on_client_closed(self) -> None:
        self._needs_reconcile = True
        self._wake.set()

    async def _poll(self) -> Delta:
        code, out, err = await run_command(
            self.tmux, self.socket, ["list-panes", "-a", "-F", POLL_FORMAT]
        )
        if code != 0:
            raise ConnectionError(err.strip() or "poll failed")
        delta = self.model.apply_poll([
            row for row in rows_from_poll(out)
            if not is_sizing_session(row.get("session_name", ""))
        ])
        return delta

    async def _probe_tier(self) -> str:
        code, out, _ = await run_command(
            self.tmux, self.socket, ["list-panes", "-a", "-F", "#{pane_agent_state}"]
        )
        if code != 0:
            return "unknown"
        return "hmux" if any(line.strip() for line in out.splitlines()) else "stock"

    async def _run(self) -> None:
        next_poll = 0.0
        next_reconcile = 0.0
        next_format_refresh = 0.0
        while not self._stopping:
            now = time.monotonic()
            try:
                if self._needs_reconcile or now >= next_reconcile:
                    self._needs_reconcile = False
                    await self._reconcile()
                    next_reconcile = time.monotonic() + self.reconcile_interval
                if self._needs_poll or now >= next_poll:
                    if self._needs_poll:
                        await asyncio.sleep(POLL_DEBOUNCE)
                    self._needs_poll = False
                    if self.model.tier == "unknown":
                        self.model.tier = await self._probe_tier()
                    delta = await self._poll()
                    next_poll = time.monotonic() + self.poll_interval
                    if self.model.error:
                        self.model.error = ""
                        delta.structure = True
                    if not delta.empty:
                        self.broadcast(self.model.delta_message(delta))
                        await self._remute()
                    await self.formats.refresh()
                    next_format_refresh = time.monotonic() + FORMAT_REFRESH_INTERVAL
                if time.monotonic() >= next_format_refresh:
                    await self.formats.refresh()
                    next_format_refresh = time.monotonic() + FORMAT_REFRESH_INTERVAL
                self._backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._handle_failure(exc)
                next_poll = next_reconcile = next_format_refresh = 0.0
                continue
            delay = max(
                0.05, min(next_poll, next_reconcile, next_format_refresh) - time.monotonic()
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            self._wake.clear()

    async def _remute(self) -> None:
        for pane in list(self.pane_watchers):
            if pane not in self.model.panes:
                for watcher in self.pane_watchers.pop(pane):
                    await self.stream_sizes.release(watcher, pane)
                    watcher.deliver_reset(pane, {"t": "screen-gone", "pane": pane})
        for viewer in list(self._viewers.values()):
            for pane in list(viewer.enabled):
                if pane not in self.model.panes:
                    await viewer.disable(pane)
            await viewer.mute_unwatched()
            await self._retire_viewer(viewer)

    async def _handle_failure(self, exc: Exception) -> None:
        """Report a server that went away and back off before retrying."""
        message = str(exc) or exc.__class__.__name__
        if self.model.error != message:
            logger.info("server unavailable: %s", message)
            self.model.error = message
            self.model.tier = "unknown"
            self.model.panes.clear()
            self.model.order = []
            self.broadcast(self.model.snapshot())
        self.formats.unavailable(message)
        for pane, watchers in list(self.pane_watchers.items()):
            for watcher in list(watchers):
                watcher.deliver_reset(pane, {"t": "screen-gone", "pane": pane})
        self.pane_watchers.clear()
        for client in list(self._state_clients.values()):
            await client.stop()
        self._state_clients.clear()
        for viewer in list(self._viewers.values()):
            await viewer.stop()
        self._viewers.clear()
        await self.stream_sizes.stop()
        await asyncio.sleep(self._backoff)
        self._backoff = min(RETRY_MAX, self._backoff * 2)


def sessions_from(rows: Iterable[str]) -> set[str]:
    return {row.strip() for row in rows if row.strip()}
