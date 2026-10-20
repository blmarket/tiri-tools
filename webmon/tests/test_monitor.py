"""The monitor against a live server: model, viewing, and client lifecycle."""

import asyncio
import json
import re

from conftest import run_async

from webmon.monitor import Monitor, trim_history


class Recorder:
    """A stand-in for a browser connection."""

    def __init__(self) -> None:
        self.models: list[dict] = []
        self.screens: list[tuple[str, bytes]] = []
        self.resets: list[tuple[str, dict]] = []

    def deliver_model(self, message: dict) -> None:
        self.models.append(message)

    def deliver_screen(self, pane: str, data: bytes) -> None:
        self.screens.append((pane, data))

    def deliver_reset(self, pane: str, payload: dict) -> None:
        self.resets.append((pane, payload))

    def bytes_for(self, pane: str) -> bytes:
        return b"".join(data for name, data in self.screens if name == pane)


async def until(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def started(server, **options) -> Monitor:
    monitor = Monitor(server.tmux, server.socket, **options)
    await monitor.start()
    assert await until(lambda: bool(monitor.model.panes)), "model never filled in"
    return monitor


def test_the_model_describes_the_server(server):
    async def scenario():
        monitor = await started(server)
        try:
            pane = monitor.model.panes[server.pane_ids()[0]]
            return monitor.model.tier, pane, monitor.model.sessions()
        finally:
            await monitor.stop()

    tier, pane, sessions = run_async(scenario())
    assert tier in ("hmux", "stock")
    assert pane["session_name"] == "work"
    assert pane["pane_width"] == 80 and pane["pane_height"] == 24
    assert sessions[0]["windows"][0]["panes"]
    if tier == "hmux":
        assert pane["pane_state_emoji"], "an hmux server always names a glyph"


def test_state_clients_are_attached_silently_one_per_session(server):
    async def scenario():
        monitor = await started(server)
        try:
            server.command("new-session", "-d", "-s", "second")
            assert await until(lambda: len(monitor._state_clients) == 2)
            flags = server.command("list-clients", "-F", "#{client_flags}")
            return sorted(monitor._state_clients), flags
        finally:
            await monitor.stop()

    sessions, flags = run_async(scenario())
    assert len(sessions) == 2
    for line in flags.splitlines():
        assert "no-output" in line and "ignore-size" in line


def test_a_new_window_arrives_without_waiting_for_the_next_poll(server):
    async def scenario():
        # A poll this far out cannot be what notices the window.
        monitor = await started(server, poll_interval=600.0)
        try:
            before = set(monitor.model.panes)
            server.command("new-window", "-t", "work", "-n", "late", "sh")
            found = await until(lambda: set(monitor.model.panes) - before, timeout=15)
            return found, monitor.model.panes
        finally:
            await monitor.stop()

    found, panes = run_async(scenario())
    assert found
    assert any(pane["window_name"] == "late" for pane in panes.values())


def test_a_closed_window_drops_its_panes(server):
    async def scenario():
        monitor = await started(server, poll_interval=600.0)
        try:
            server.command("new-window", "-t", "work", "-n", "doomed", "sh")
            assert await until(
                lambda: any(
                    pane["window_name"] == "doomed" for pane in monitor.model.panes.values()
                ),
                timeout=15,
            )
            server.command("kill-window", "-t", "work:doomed")
            return await until(
                lambda: not any(
                    pane["window_name"] == "doomed" for pane in monitor.model.panes.values()
                ),
                timeout=15,
            )
        finally:
            await monitor.stop()

    assert run_async(scenario())


def test_watching_a_pane_paints_a_snapshot_and_then_streams(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            server.command(
                "new-window", "-t", "work", "-n", "tick",
                "sh -c 'while :; do printf TICK; sleep 0.2; done'",
            )
            assert await until(
                lambda: any(
                    pane["window_name"] == "tick" for pane in monitor.model.panes.values()
                ),
                timeout=15,
            )
            pane = next(
                name
                for name, values in monitor.model.panes.items()
                if values["window_name"] == "tick"
            )
            await monitor.watch_pane(pane, watcher)
            streamed = await until(lambda: b"TICK" in watcher.bytes_for(pane), timeout=15)
            return watcher.resets, streamed, pane
        finally:
            await monitor.stop()

    resets, streamed, pane = run_async(scenario())
    assert resets and resets[0][0] == pane
    payload = resets[0][1]
    assert payload["t"] == "screen"
    assert payload["data"].startswith("\x1bc"), "a repaint has to reset the emulator"
    assert (payload["cols"], payload["rows"]) == (80, 24)
    assert streamed, "no live output followed the snapshot"


def test_a_pane_nobody_watches_does_not_stream(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            server.command(
                "new-window", "-t", "work", "-n", "noise",
                "sh -c 'while :; do printf NOISE; sleep 0.05; done'",
            )
            assert await until(
                lambda: any(
                    pane["window_name"] == "noise" for pane in monitor.model.panes.values()
                ),
                timeout=15,
            )
            noisy = next(
                name
                for name, values in monitor.model.panes.items()
                if values["window_name"] == "noise"
            )
            quiet = next(
                name
                for name, values in monitor.model.panes.items()
                if values["window_name"] != "noise"
            )
            await monitor.watch_pane(quiet, watcher)
            await asyncio.sleep(2.0)
            return watcher.bytes_for(noisy)
        finally:
            await monitor.stop()

    assert run_async(scenario()) == b""


def test_the_viewer_client_is_retired_when_the_last_browser_looks_away(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            pane = server.pane_ids()[0]
            await monitor.watch_pane(pane, watcher)
            attached = len(server.command("list-clients", "-F", "#{client_name}").split())
            await monitor.unwatch_pane(pane, watcher)
            retired = await until(
                lambda: len(server.command("list-clients", "-F", "#{client_name}").split())
                < attached
            )
            return attached, retired, monitor._viewers
        finally:
            await monitor.stop()

    attached, retired, viewers = run_async(scenario())
    assert attached == 2, "viewing should add exactly one client"
    assert retired and not viewers


def test_the_monitor_survives_the_server_going_away(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            server.command("kill-server", check=False)
            reported = await until(lambda: bool(monitor.model.error), timeout=15)
            return reported, monitor.model.panes
        finally:
            await monitor.stop()

    reported, panes = run_async(scenario())
    assert reported, "a vanished server should be reported, not raised"
    assert panes == {}


def test_shared_captures_preserve_output_order_for_every_viewer(server):
    class OrderedRecorder(Recorder):
        def __init__(self):
            super().__init__()
            self.events = []

        def deliver_screen(self, pane, data):
            super().deliver_screen(pane, data)
            self.events.append(("output", data))

        def deliver_reset(self, pane, payload):
            super().deliver_reset(pane, payload)
            if payload["t"] == "screen":
                self.events.append(("screen", payload["data"].encode("latin-1")))

    async def scenario():
        monitor = await started(server)
        first, second = OrderedRecorder(), OrderedRecorder()
        try:
            server.command("new-window", "-t", "work", "-n", "ordered", "sh -c 'i=1; while :; do printf \"SEQ%08d\\n\" \"$i\"; i=$((i+1)); sleep 0.01; done'")
            assert await until(lambda: any(p["window_name"] == "ordered" for p in monitor.model.panes.values()))
            pane = next(p for p, values in monitor.model.panes.items() if values["window_name"] == "ordered")
            await monitor.watch_pane(pane, first)
            assert await until(lambda: len(first.screens) >= 3)
            await monitor.watch_pane(pane, second)
            assert len(first.resets) >= 2 and second.resets
            assert await until(lambda: len(second.screens) >= 3)
            await monitor.repaint(pane, second)
            assert len(first.resets) >= 3 and len(second.resets) >= 2
            assert await until(lambda: len(second.screens) >= 6)
            for recorder in (first, second):
                last = None
                for kind, data in recorder.events:
                    numbers = [int(value) for value in re.findall(rb"SEQ(\d{8})", data)]
                    if not numbers:
                        continue
                    if kind == "screen":
                        last = numbers[-1]
                    else:
                        assert last is not None and numbers[0] == last + 1
                        assert numbers == list(range(numbers[0], numbers[-1] + 1))
                        last = numbers[-1]
        finally:
            await monitor.stop()

    run_async(scenario())


def test_a_snapshot_carries_history_that_scrolled_off_before_attach(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            server.command("new-window", "-t", "work", "-n", "long", "sh -c 'seq 1 200; sleep 600'")
            assert await until(lambda: any(p["window_name"] == "long" for p in monitor.model.panes.values()), timeout=15)
            pane = next(p for p, values in monitor.model.panes.items() if values["window_name"] == "long")
            server.wait_for(lambda: "200" in server.command("capture-pane", "-p", "-t", pane))
            await monitor.watch_pane(pane, watcher)
            await monitor.repaint(pane, watcher)
            return [payload for _, payload in watcher.resets]
        finally:
            await monitor.stop()

    first, repainted = run_async(scenario())
    for payload in (first, repainted):
        lines = payload["data"].removeprefix("\x1bc").split("\r\n")
        assert "2" in lines, "a line already scrolled off the screen is missing"
        assert lines.index("200") > len(lines) - payload["rows"] - 1, "the screen has to stay last"


def test_a_fresh_pane_snapshot_is_just_the_screen(server):
    async def scenario():
        monitor = await started(server)
        watcher = Recorder()
        monitor.add_watcher(watcher)
        try:
            server.command("new-window", "-t", "work", "-n", "fresh", "sh -c 'sleep 600'")
            assert await until(lambda: any(p["window_name"] == "fresh" for p in monitor.model.panes.values()), timeout=15)
            pane = next(p for p, values in monitor.model.panes.items() if values["window_name"] == "fresh")
            await monitor.watch_pane(pane, watcher)
            return watcher.resets[0][1]
        finally:
            await monitor.stop()

    payload = run_async(scenario())
    assert len(payload["data"].split("\r\n")) == payload["rows"]


def test_history_is_trimmed_oldest_first_to_its_encoded_budget():
    screen = [b"screen"] * 3
    history = [b"\x1b[1m%04d" % index for index in range(100)]
    kept = trim_history(history + screen, rows=3, budget=200)
    assert kept[-3:] == screen
    assert kept[:-3] == history[len(history) - len(kept) + 3 :]
    assert 0 < len(kept) - 3 < 100
    assert sum(len(json.dumps(line.decode("latin-1"))) + 2 for line in kept[:-3]) <= 200
    assert trim_history(screen, rows=3, budget=0) == screen
